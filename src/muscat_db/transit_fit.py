"""Helpers for the Transit Fit page: manage config generation (fit.yaml, sys.yaml),
run the transit-fit pipeline, poll logs, and return outputs/plots.
"""
from __future__ import annotations

import csv
import datetime
import json
import logging
import math
import os
import pathlib
import re
import shlex
import shutil
import signal
import subprocess
import tempfile
import threading
import time
from dataclasses import dataclass
from typing import IO
import yaml

from muscat_db import jobs, database
from muscat_db.job_store import get_job_store
from muscat_db import __meta__, __muscatdb_version__, __version__
from muscat_db.instruments import INSTRUMENTS
from muscat_db.photometry import (
    output_base, valid_date, _conda_env_python, _tail, _to_float, _get_error_desc,
    SINISTRO_SITES, SINISTRO_MODES,
)
from muscat_db.cache import register_cache

_REPO_ROOT = pathlib.Path(__file__).parent.parent.parent.resolve()
logger = logging.getLogger(__name__)

_TIMER_VERSION: str | None = None
_TIMER_VERSION_LOCK = threading.Lock()


def _timer_version() -> str:
    """Return the installed timer package version, cached after first lookup."""
    global _TIMER_VERSION
    if _TIMER_VERSION is not None:
        return _TIMER_VERSION
    with _TIMER_VERSION_LOCK:
        if _TIMER_VERSION is not None:
            return _TIMER_VERSION
        timer_py = _conda_env_python("timer")
        if timer_py is None:
            _TIMER_VERSION = "unknown"
        else:
            try:
                result = subprocess.run(
                    [timer_py, "-c", "from importlib.metadata import version; print(version('timer'))"],
                    capture_output=True, text=True, timeout=10,
                )
                _TIMER_VERSION = result.stdout.strip() if result.returncode == 0 and result.stdout.strip() else "unknown"
            except Exception:
                _TIMER_VERSION = "unknown"
    return _TIMER_VERSION


def _write_log_banner(logf: IO, cmd: list[str], options: dict | None = None) -> None:
    """Write a versioned startup header then the command line and parsed args to *logf*.

    This is the very first content written to every timer-fit.log so that
    each run is clearly stamped with the muscat-db version and wall-clock time.
    """
    separator = "=" * 60
    now_utc = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    logf.write(f"{separator}\n")
    logf.write(f"muscat-db v{__version__}  |  timer-fit v{_timer_version()}  |  {now_utc}\n")
    logf.write("command: transit-fit\n")
    logf.write(f"{separator}\n\n")
    logf.write(f"$ {shlex.join(cmd)}\n\n")

    if options is not None:
        logf.write("--- options ---\n")
        for k, v in sorted(options.items()):
            if (k == "stellar_ref" or k.startswith("pl_ref")) and isinstance(v, str) and v:
                val = re.sub(
                    r'<a\b[^>]*href="([^"]+)"[^>]*>(.*?)</a>',
                    lambda m: f"{re.sub(r'<[^>]+>', '', m.group(2))} ({m.group(1)})",
                    v
                )
                val = re.sub(r'<[^>]+>', '', val)
                logf.write(f"  {k}: {val!r}\n")
            else:
                logf.write(f"  {k}: {v!r}\n")
        logf.write("\n")


def fit_output_dir(inst: str, date: str, target: str, run_id: str | None = None) -> pathlib.Path:
    """Return a transit-fit output directory confined below the timer root.

    Spaces are removed from the target directory component. ``ValueError`` is
    raised for an empty target or one containing ``..``, ``/``, or ``\\``.

    When ``run_id`` is given, the fit is isolated in a per-run subdirectory
    ``{target}/{run_id}/`` so distinct runs (different site/mode/run-name) never
    overwrite each other. ``run_id=None`` reproduces the legacy ``{target}/``
    path so pre-existing fits keep resolving.
    """
    base = pathlib.Path(os.environ.get("MUSCAT_TIMER_DIR", "/ut2/jerome/ql/timer")).expanduser().resolve(strict=False)
    parts = [base, inst, date, _target_dir_name(target)]
    if run_id:
        parts.append(_run_dir_name(run_id))
    path = pathlib.Path(*[str(p) for p in parts]).resolve(strict=False)
    try:
        path.relative_to(base)
    except ValueError as exc:
        raise ValueError("invalid target") from exc
    return path


# Run-id / path-segment helpers and the run-id join live in muscat_db.jobs so
# photometry and transit-fit share one implementation (audit C1). Re-exported
# here under their historical names for callers and tests.
_target_dir_name = jobs.target_dir_name
_run_dir_name = jobs.run_dir_name
slugify_run_name = jobs.slugify_run_name
build_run_id = jobs.build_run_id


def log_path(inst: str, date: str, target: str, run_id: str = "") -> pathlib.Path | None:
    try:
        rdir = fit_output_dir(inst, date, target, run_id or None)
    except ValueError:
        return None
    p = rdir / "timer-fit.log"
    return p if p.is_file() else None


# The in-memory job record and the finalizing state machine are shared with
# photometry via muscat_db.jobs (audit C1). ``TransitFitJob`` keeps its name.
TransitFitJob = jobs.PipelineJob

_FIT_JOBS: dict[str, TransitFitJob] = {}
_FIT_LOCK = threading.Lock()
_MAX_FULL_JOBS = 1

# Finalizing grace-window settings (env-tunable), mirroring photometry so the
# transit-fit live log keeps streaming the worker output timer emits after the
# tracked parent exits, instead of freezing at parent-exit (audit C1). timer has
# no partial-failure concept, so a zero exit is always ``done``.
_FINALIZE_GRACE_S = int(os.environ.get("MUSCAT_FIT_FINALIZE_GRACE_S", 8))
_FINALIZE_GRACE_TERMINAL_S = int(os.environ.get("MUSCAT_FIT_FINALIZE_GRACE_TERMINAL_S", 2))
# Result line timer logs once a fit has completed; remaining writes are teardown.
_TERMINAL_LOG_MARKERS = ("Timer-fit completed successfully",)


def _finalize_config() -> jobs.FinalizeConfig:
    """Build the finalizing config from the current module-level settings (per
    call, so the env-tunable values stay overridable at runtime)."""
    return jobs.FinalizeConfig(
        grace_s=_FINALIZE_GRACE_S,
        grace_terminal_s=_FINALIZE_GRACE_TERMINAL_S,
        terminal_markers=_TERMINAL_LOG_MARKERS,
        partial_failure_marker=None,
    )


def _count_running_full() -> int:
    """Number of currently-running full (non-test) transit fit jobs."""
    return jobs.count_running_full(_FIT_JOBS)


def fit_job_key(inst: str, date: str, target: str, run_id: str = "") -> str:
    """Return a job key using the validated target directory name.

    When ``run_id`` is set the key is run-scoped so distinct runs of the same
    target are independent jobs; an empty ``run_id`` reproduces the legacy key.
    """
    base = f"{inst}/{date}/{_target_dir_name(target)}"
    return f"{base}/{run_id}" if run_id else base


def csv_site_mode(name: str) -> tuple[str | None, str | None]:
    """``(site, canonical_mode)`` parsed from a sinistro lightcurve CSV name.

    Mirrors prose ``build_stem``: the LCO site is an ``_<site>_`` token and the
    readout mode is the trailing ``_full`` token (``full_frame``; absence means
    ``central_2k_2x2``). ``site`` is ``None`` when no site token is present.
    """
    stem = name[:-4] if name.lower().endswith(".csv") else name
    mode = "central_2k_2x2"
    if stem.endswith("_full"):
        mode = "full_frame"
        stem = stem[:-5]
    site = next((t for t in stem.lower().split("_") if t in SINISTRO_SITES), None)
    return site, mode


def selected_site_mode(inst: str, csv_names: list[str]) -> tuple[str, str]:
    """Derive ``(site_token, mode_token)`` for a run from its selected CSVs.

    Single shared value -> that value; more than one -> ``mixed`` (mixing is
    allowed); none / non-sinistro -> ``""``. Used to compose the run id.
    """
    if inst != "sinistro" or not csv_names:
        return "", ""
    sites = {s for s, _ in map(csv_site_mode, csv_names) if s}
    modes = {m for _, m in map(csv_site_mode, csv_names) if m}
    site = next(iter(sites)) if len(sites) == 1 else ("mixed" if sites else "")
    mode = next(iter(modes)) if len(modes) == 1 else ("mixed" if modes else "")
    return site, mode


def validate_no_duplicate_datasets(inst: str, date: str, csvs: list[pathlib.Path]) -> str | None:
    """Ensure no selected lightcurves represent the same physical dataset (site, mode, band)."""
    seen_keys = set()
    for c in csvs:
        parts = c.name.split(f"_{inst}_")
        raw_band = parts[1].split(f"_{date}")[0] if len(parts) > 1 else "gp"
        mapped_band = _normalize_band(raw_band)
        site, mode = csv_site_mode(c.name)
        key = (site or "", mode or "", mapped_band)
        if key in seen_keys:
            if inst == "sinistro":
                site_str = f" (site: {site})" if site else ""
                mode_str = f" (mode: {mode})" if mode else ""
                return f"Multiple lightcurves selected for the same dataset: band '{mapped_band}'{site_str}{mode_str}. Please select only one run."
            else:
                return f"Multiple lightcurves selected for the same band '{mapped_band}'. Please select only one run."
        seen_keys.add(key)
    return None


def _timer_prefix() -> list[str]:
    """Resolve how to invoke the timer-fit tool, using the timer conda env."""
    env = "timer"
    conda_py = _conda_env_python(env)
    if conda_py:
        timer_fit_path = pathlib.Path(conda_py).parent / "timer-fit"
        if timer_fit_path.is_file():
            return [str(timer_fit_path)]
        return [conda_py, "-m", "timer.fit"]

    if shutil.which("conda"):
        return ["conda", "run", "-n", env, "--no-capture-output", "timer-fit"]

    if shutil.which("timer-fit"):
        return ["timer-fit"]
    return ["timer-fit"]


def get_csv_lightcurves(inst: str, date: str, target: str) -> list[pathlib.Path]:
    """Find the CSV lightcurves outputted by the Photometry page for a target.

    Photometry now stores named runs under ``_runs/<target>/<run_id>/`` while
    older reductions wrote CSVs directly in ``<inst>/<date>/``. Transit-fit
    accepts both layouts so one-band Sinistro reductions remain selectable.
    """
    rdir = output_base() / inst / date
    if not rdir.is_dir():
        return []

    target_clean = target.replace(" ", "").replace("-", "").lower()
    inst_token = f"_{inst.lower()}_"

    def matches_lightcurve(f: pathlib.Path) -> bool:
        if f.name.startswith("_") or "summary" in f.name or "nearby_stars" in f.name:
            return False
        fname = f.name.lower()
        if inst_token not in fname:
            return False
        t_part = fname.split(inst_token, 1)[0]
        return t_part.replace("-", "") == target_clean

    search_dirs = [rdir]
    try:
        runs_root = rdir / "_runs" / _target_dir_name(target)
    except ValueError:
        runs_root = None
    if runs_root is not None and runs_root.is_dir():
        search_dirs.extend(d for d in sorted(runs_root.iterdir()) if d.is_dir())

    csvs = []
    seen: set[pathlib.Path] = set()
    for search_dir in search_dirs:
        for f in search_dir.glob("*.csv"):
            if not matches_lightcurve(f):
                continue
            resolved = f.resolve(strict=False)
            if resolved in seen:
                continue
            seen.add(resolved)
            csvs.append(f)
    return sorted(csvs, key=lambda p: (p.name, str(p.parent)))


def get_target_parameters(target_name: str) -> dict:
    """Retrieve default stellar and planetary parameters from the catalog."""
    params = {
        "teff": 5778.0, "teff_unc": 100.0,
        "logg": 4.4, "logg_unc": 0.1,
        "feh": 0.0, "feh_unc": 0.1,
        "planets": "b",
        "period": 1.0, "period_unc": 0.001,
        "t0": 2450000.0, "t0_unc": 0.01,
        "dur": 0.1, "dur_unc": 0.01,
        "ror": 0.05, "ror_unc": 0.005,
        "b": 0.0, "b_unc": 0.1,
    }

    csv_path = _REPO_ROOT / "data" / "muscatdb_targets_old.csv"
    if not csv_path.exists():
        return params

    norm_target = target_name.replace("-", "").replace(" ", "").replace("_", "").upper()
    try:
        with open(csv_path, errors="replace") as f:
            reader = csv.DictReader(f, delimiter=";")
            for row in reader:
                name_val = (row.get("name") or "").strip()
                norm_name = name_val.replace("-", "").replace(" ", "").replace("_", "").upper()
                if norm_target in norm_name or norm_name in norm_target:
                    # Parse stellar details
                    if row.get("Teff_GAIA_sg2"):
                        try: params["teff"] = float(row["Teff_GAIA_sg2"])
                        except ValueError: pass
                    # Parse planet period
                    if row.get("period"):
                        try: params["period"] = float(row["period"])
                        except ValueError: pass
                    elif row.get("period_sg1"):
                        try: params["period"] = float(row["period_sg1"])
                        except ValueError: pass
                    if row.get("period_error"):
                        try: params["period_unc"] = float(row["period_error"])
                        except ValueError: pass
                    elif row.get("period_error_sg1"):
                        try: params["period_unc"] = float(row["period_error_sg1"])
                        except ValueError: pass
                    # Parse planet t0
                    if row.get("t0"):
                        try: params["t0"] = float(row["t0"])
                        except ValueError: pass
                    elif row.get("t0_sg1"):
                        try: params["t0"] = float(row["t0_sg1"])
                        except ValueError: pass
                    if row.get("t0_error"):
                        try: params["t0_unc"] = float(row["t0_error"])
                        except ValueError: pass
                    elif row.get("t0_error_sg1"):
                        try: params["t0_unc"] = float(row["t0_error_sg1"])
                        except ValueError: pass
                    # Parse planet duration
                    if row.get("duration"):
                        try: params["dur"] = float(row["duration"])
                        except ValueError: pass
                    elif row.get("duration_sg1"):
                        try: params["dur"] = float(row["duration_sg1"])
                        except ValueError: pass
                    if row.get("duration_error_sg1"):
                        try: params["dur_unc"] = float(row["duration_error_sg1"])
                        except ValueError: pass
                    # Parse planet ror
                    if row.get("Rp_sg1"):
                        try: params["ror"] = float(row["Rp_sg1"])
                        except ValueError: pass
                    # Parse planet b
                    if row.get("Rs_a"):
                        try: params["b"] = float(row["Rs_a"])
                        except ValueError: pass
                    break
    except Exception:
        logger.debug("failed to read default target parameters for %s", target_name, exc_info=True)

    return params


# Parameters the user may hold fixed during the fit (the "Fixed Parameters"
# checkboxes in the fitting configuration form).
_FIXABLE_PARAMS = {"t0", "period", "dur", "u_star", "b", "ror"}
# A single planet designation token, e.g. "b" or "c".
_PLANET_TOKEN_RE = re.compile(r"^[A-Za-z]$")

# Numeric fields in "Fitting Configurations & Parameters": (key, label, rule).
#   "pos"    -> must be a finite number > 0
#   "nonneg" -> must be a finite number >= 0
#   "num"    -> any finite number
# An empty value is allowed for every field; it keeps the pipeline default.
_FIT_NUMERIC_FIELDS: list[tuple[str, str, str]] = [
    ("tc_pred_unc", "tc_pred uncertainty",  "pos"),
    ("teff",        "Teff (K)",             "pos"),
    ("teff_unc",    "Teff uncertainty",     "pos"),
    ("logg",        "log g",                "num"),
    ("logg_unc",    "log g uncertainty",    "pos"),
    ("feh",         "[Fe/H]",               "num"),
    ("feh_unc",     "[Fe/H] uncertainty",   "pos"),
    ("period",      "Period (days)",        "pos"),
    ("period_unc",  "Period uncertainty",   "pos"),
    ("t0",          "Epoch t0 (BJD)",       "pos"),
    ("t0_unc",      "t0 uncertainty",       "pos"),
    ("dur",         "Duration (days)",      "pos"),
    ("dur_unc",     "Duration uncertainty", "pos"),
    ("ror",         "Rp/R*",                "pos"),
    ("ror_unc",     "Rp/R* uncertainty",    "pos"),
    ("b",           "Impact parameter b",   "nonneg"),
    ("b_unc",       "b uncertainty",        "pos"),
]

# Per-planet fitting parameters whose prior shape is user-selectable.
# timer applies a Gaussian prior (value ± uncertainty from sys.yaml) by default;
# listing a parameter in fit.yaml's ``uniform`` block instead switches it to a
# Uniform prior over [value - unc, value + unc]. timer fits t0 with a Uniform
# prior unconditionally, so t0 is intentionally not selectable here.
_PRIOR_PARAMS = ("period", "dur", "ror", "b")
_PRIOR_CHOICES = {"gaussian", "uniform"}
# Fallback [low, high] bounds for a Uniform parameter whose fields are left
# blank. b and ror use their physical limits (matching the GUI auto-fill);
# period and dur have no universal range, so fall back to a tight window around
# the sys.yaml defaults. Normally the GUI fills these on selecting Uniform.
_UNIFORM_DEFAULT_BOUNDS: dict[str, tuple[float, float]] = {
    "period": (0.999, 1.001),
    "dur":    (0.09, 0.11),
    "ror":    (0.0, 0.5),
    "b":      (0.0, 1.0),
}


def _prior_choice(options: dict, param: str, planet: str, first_planet: str) -> str:
    """Return the prior shape ('gaussian'|'uniform') for *param* of *planet*.

    Falls back to the unsuffixed key for the first planet (matching how the
    numeric fields broadcast), then to 'gaussian'.
    """
    raw = options.get(f"{param}_prior_{planet}")
    if raw is None and planet == first_planet:
        raw = options.get(f"{param}_prior")
    return str(raw or "gaussian").strip().lower()


def _planet_value(options: dict, key: str, planet: str, first_planet: str):
    """Return a planet's numeric field as float, or ``None`` when blank/invalid.

    Mirrors the first-planet broadcast: the unsuffixed key (e.g. ``ror``) backs
    the suffixed one (``ror_b``) only for the first planet.
    """
    raw = options.get(f"{key}_{planet}")
    if raw is None and planet == first_planet:
        raw = options.get(key)
    return _to_float(raw)


def _uniform_bounds(options: dict, param: str, planet: str, first_planet: str) -> list[float]:
    lo_default, hi_default = _UNIFORM_DEFAULT_BOUNDS[param]
    lo = _planet_value(options, param, planet, first_planet)
    hi = _planet_value(options, f"{param}_unc", planet, first_planet)
    return [lo if lo is not None else lo_default, hi if hi is not None else hi_default]


def validate_fit_options(options: dict | None) -> str | None:
    """Return a user-facing error string for invalid fitting options, else ``None``.

    Validates every field in the "Fitting Configurations & Parameters" form so
    bad input is rejected with a clear message instead of crashing the pipeline
    or writing a malformed sys.yaml. An empty field keeps the pipeline default.
    """
    o = options or {}

    # Planets: comma-separated single letters (defaults to "b" when blank).
    tokens = [p.strip() for p in (o.get("planets") or "b").split(",") if p.strip()]
    if not tokens:
        return "planets is required (e.g. b or b,c)"
    if any(not _PLANET_TOKEN_RE.match(t) for t in tokens):
        return "planets must be single letters separated by commas (e.g. b or b,c)"
    if len(tokens) != len(set(tokens)):
        return "planet designations must be unique"

    # tc_pred is optional; validate only when provided.
    if str(o.get("tc_pred", "")).strip() and _to_float(o.get("tc_pred")) is None:
        return "tc_pred must be a number (BJD), or empty for auto"

    planet_keys = {"period", "period_unc", "t0", "t0_unc", "dur", "dur_unc", "ror", "ror_unc", "b", "b_unc"}

    # Validate stellar parameters (non-planet-specific)
    for key, label, rule in _FIT_NUMERIC_FIELDS:
        if key in planet_keys:
            continue
        if str(o.get(key, "")).strip() == "":
            continue  # blank -> pipeline default
        val = _to_float(o.get(key))
        if val is None or not math.isfinite(val):
            return f"{label} must be a number"
        if rule == "pos" and val <= 0:
            return f"{label} must be greater than 0"
        if rule == "nonneg" and val < 0:
            return f"{label} must be 0 or greater"

    # Validate planetary parameters for each planet. Parameters switched to a
    # Uniform prior carry [low, high] bounds (not value ± unc) in these fields,
    # so they are validated separately in the prior-shapes block below.
    for p in tokens:
        for key, label, rule in _FIT_NUMERIC_FIELDS:
            if key not in planet_keys:
                continue
            base = key[:-4] if key.endswith("_unc") else key
            if base in _PRIOR_PARAMS and _prior_choice(o, base, p, tokens[0]) == "uniform":
                continue

            pval = o.get(f"{key}_{p}")
            if pval is None and p == tokens[0]:
                pval = o.get(key)

            if pval is None or str(pval).strip() == "":
                continue

            val = _to_float(pval)
            if val is None or not math.isfinite(val):
                return f"{label} (planet {p}) must be a number"
            if rule == "pos" and val <= 0:
                return f"{label} (planet {p}) must be greater than 0"
            if rule == "nonneg" and val < 0:
                return f"{label} (planet {p}) must be 0 or greater"

    # Rp/R* is a radius ratio; reject obviously unphysical values (Gaussian mean
    # only; Uniform bounds are range-checked in the prior-shapes block).
    for p in tokens:
        if _prior_choice(o, "ror", p, tokens[0]) == "uniform":
            continue
        ror_val = o.get(f"ror_{p}")
        if ror_val is None and p == tokens[0]:
            ror_val = o.get("ror")
        if ror_val is not None:
            ror = _to_float(ror_val)
            if ror is not None and ror >= 1:
                return f"Rp/R* (planet {p}) must be less than 1"

    # Fixed parameters must be drawn from the known set.
    fixed = o.get("fixed")
    if fixed is not None:
        if not isinstance(fixed, (list, tuple)):
            return "fixed parameters must be a list"
        unknown = [str(f) for f in fixed if str(f) not in _FIXABLE_PARAMS]
        if unknown:
            allowed = ", ".join(sorted(_FIXABLE_PARAMS))
            return f"unknown fixed parameter(s): {', '.join(unknown)} (allowed: {allowed})"

    # Prior shapes. timer applies one prior shape per parameter across all
    # planets, so a parameter cannot mix Gaussian and Uniform between planets,
    # and a Uniform parameter cannot also be held fixed.
    fixed_set = {str(f) for f in (fixed or [])}
    for param in _PRIOR_PARAMS:
        choices = []
        for p in tokens:
            choice = _prior_choice(o, param, p, tokens[0])
            if choice not in _PRIOR_CHOICES:
                return f"{param} prior (planet {p}) must be gaussian or uniform"
            choices.append(choice)
        if "uniform" not in choices:
            continue
        if len(set(choices)) > 1:
            return (
                f"{param} prior must be the same for every planet "
                "(timer applies one prior shape per parameter): set all "
                "planets to gaussian or all to uniform"
            )
        if param in fixed_set:
            return f"{param} cannot be both fixed and use a uniform prior"

        # A Uniform parameter's two keys hold explicit [low, high] bounds.
        for p in tokens:
            for key, name in ((param, "low"), (f"{param}_unc", "high")):
                raw = o.get(f"{key}_{p}")
                if raw is None and p == tokens[0]:
                    raw = o.get(key)
                if raw is not None and str(raw).strip():
                    value = _to_float(raw)
                    if value is None or not math.isfinite(value):
                        return f"{param} uniform {name} bound (planet {p}) must be a number"

            lo, hi = _uniform_bounds(o, param, p, tokens[0])
            if not lo < hi:
                return f"{param} uniform bounds (planet {p}) must have low < high"
            if param in {"period", "dur"} and (lo <= 0 or hi <= 0):
                return f"{param} uniform bounds (planet {p}) must be greater than 0"
            if param == "ror" and (lo < 0 or hi > 1):
                return f"{param} uniform bounds (planet {p}) must stay within [0, 1]"
            if param == "b" and lo < 0:
                return f"{param} uniform lower bound (planet {p}) must be 0 or greater"

    if o.get("use_gp") == "true":
        for param, value_default, unc_default in (
            ("log_amp", -3.0, 2.0),
            ("log_scale", -1.0, 2.0),
        ):
            prior = str(o.get(f"gp_{param}_prior") or "gaussian").strip().lower()
            if prior not in _PRIOR_CHOICES:
                return f"GP {param} prior must be gaussian or uniform"

            values = []
            for suffix, default in (("", value_default), ("_unc", unc_default)):
                key = f"gp_{param}{suffix}"
                raw = o.get(key)
                if raw is None or str(raw).strip() == "":
                    values.append(default)
                    continue
                value = _to_float(raw)
                if value is None or not math.isfinite(value):
                    return f"GP {param}{suffix} must be a number"
                values.append(value)

            first, second = values
            if prior == "uniform" and not first < second:
                return f"GP {param} uniform bounds must have low < high"
            if prior == "gaussian" and second <= 0:
                return f"GP {param} uncertainty must be greater than 0"

    return None


class _SysDumper(yaml.SafeDumper):
    """Dump sys.yaml with leaf ``[value, unc]`` pairs in flow style (mappings
    stay block) and floats free of representation noise."""


def _represent_clean_float(dumper: yaml.Dumper, value: float):
    """Render a float cleanly: strip representation noise (0.0005600000000000001
    -> ``0.00056``), emit whole numbers as ints (``5475``), and keep the result
    parseable as a YAML float (no ``!!float`` tags)."""
    if value != value or value in (float("inf"), float("-inf")):
        return dumper.represent_scalar("tag:yaml.org,2002:float", repr(value))
    # Whole numbers (except 0.0) render as plain ints, e.g. teff [5475, 127].
    if value != 0 and value == int(value) and abs(value) < 1e15:
        return dumper.represent_int(int(value))

    # Shortest decimal that round-trips within float-precision tolerance.
    text = repr(value)
    for precision in range(4, 17):
        candidate = f"{value:.{precision}g}"
        if abs(float(candidate) - value) <= abs(value or 1) * 1e-12:
            text = candidate
            break
    # PyYAML tags a float "!!float" unless its text reads as a float: the
    # mantissa needs a decimal point even in exponent form (3e-05 -> 3.0e-05).
    if "e" in text or "E" in text:
        mantissa, _, exponent = text.replace("E", "e").partition("e")
        if "." not in mantissa:
            mantissa += ".0"
        text = f"{mantissa}e{exponent}"
    elif "." not in text:
        text += ".0"
    return dumper.represent_scalar("tag:yaml.org,2002:float", text)


_SysDumper.add_representer(float, _represent_clean_float)


def _normalize_band(raw: str) -> str:
    """Map a raw filter token from a light-curve filename to a band name that
    timer/limbdark understands, preserving narrow-band identity.

    Broadband Sloan filters  -> 'g' / 'r' / 'i' / 'z'
    Narrow-band Sloan filters -> 'g_narrow' / 'r_narrow' / 'i_narrow' / 'z_narrow'
    Sodium D narrow-band      -> 'Na_D'

    The previous substring matching (``'r' in 'i_narrow'``) collapsed every
    narrow band onto a broadband char, so timer saw only the unique broadbands
    and dropped the narrow bands from chromatic plots.
    """
    low = raw.strip().lower()
    # Strip site code prefix if present (e.g. cpt_, lsc_ for Sinistro site codes)
    # so that the actual filter name is extracted and normalized correctly.
    for site in SINISTRO_SITES:
        if low.startswith(f"{site}_"):
            low = low[len(site) + 1:]
            break
    # Sodium D: real tokens start with "na" (Na_D, NaD, Na). Narrow Sloan tokens
    # start with their filter letter (e.g. "i_narrow"), so this is unambiguous.
    if low.startswith("na"):
        return "Na_D"
    is_narrow = "narrow" in low or low.endswith("_nb")
    base = low[0] if low[:1] in "griz" else "g"
    return f"{base}_narrow" if is_narrow else base



def _band_sort_key(band: str) -> tuple:
    """Sort key for band display order.

    Broadband:  g → r → i → z
    Narrowband: g_narrow → Na_D → i_narrow → z_narrow
    """
    order = {
        "g": (0, 0), "r": (0, 1), "i": (0, 2), "z": (0, 3),
        "g_narrow": (1, 0), "Na_D": (1, 1), "i_narrow": (1, 2), "z_narrow": (1, 3),
    }
    return order.get(band, (9, 9))


def _write_fit_inputs(
    rdir: pathlib.Path,
    inst: str,
    date: str,
    target: str,
    csvs: list[pathlib.Path],
    options: dict,
    site: str = "",
    mode: str = "",
    run_name: str = "",
    run_id: str = "",
    run_type: str = "",
) -> None:
    """Copy light-curve CSVs into ``rdir`` and write fit.yaml / sys.yaml.

    Shared by :func:`start_fit` (real run directory) and :func:`compute_logp`
    (throwaway temp directory) so both build identical timer inputs from the
    form options. ``site``/``mode``/``run_name``/``run_id`` are recorded in
    meta.yaml so run discovery never has to parse the directory name.
    """
    for c in csvs:
        shutil.copy2(c, rdir / c.name)

    # fit.yaml
    trend_val = 1 if options.get("trend", "true") == "true" else 0

    # Option parsers: the form sends every value as a string (or omits it).
    def _bool_opt(key: str, default: bool = False) -> bool:
        v = options.get(key)
        return default if v is None else v == "true"

    def _int_opt(key: str, default: int) -> int:
        try: return int(float(options.get(key) or default))
        except (TypeError, ValueError): return default

    def _float_opt(key: str, default: float) -> float:
        try: return float(options.get(key) or default)
        except (TypeError, ValueError): return default

    def _trim_opt(key: str):
        v = options.get(key)
        if v is None or str(v).strip() == "": return None
        try: return int(float(v))
        except (TypeError, ValueError): return None

    # Per-dataset detrending options (applied uniformly to every light curve).
    detrend = {
        "spline": _bool_opt("spline"),
        "spline_knots": _int_opt("spline_knots", 5),
        "add_bias": _bool_opt("add_bias"),
        "quadratic": _bool_opt("quadratic"),
        "clip": _bool_opt("clip"),
        "clip_nsig": _float_opt("clip_nsig", 7),
        "chunk_offset": _bool_opt("chunk_offset"),
        "chunk_thresh": _float_opt("chunk_thresh", 0),
        "trim_beg": _trim_opt("trim_beg"),
        "trim_end": _trim_opt("trim_end"),
    }

    # Sort CSVs by canonical band order so chromatic plots always appear
    # g→r→i→z (broadband) or g_narrow→Na_D→i_narrow→z_narrow (narrowband).
    def _csv_band_key(c: pathlib.Path) -> tuple:
        parts = c.name.split(f"_{inst}_")
        raw_band = parts[1].split(f"_{date}")[0] if len(parts) > 1 else "gp"
        return _band_sort_key(_normalize_band(raw_band))

    fit_data: dict = {"data": {}}
    for c in sorted(csvs, key=_csv_band_key):
        fname = c.name
        parts = fname.split(f"_{inst}_")
        band = parts[1].split(f"_{date}")[0] if len(parts) > 1 else "gp"

        mapped_band = _normalize_band(band)

        fit_data["data"][band] = {
            "file": fname,
            "band": mapped_band,
            "trend": trend_val,
            "binsize": 0.00139,
            "format": "afphot",
            **detrend,
        }

    planets_str = (options.get("planets") or "b").strip()
    planet_list = [p.strip() for p in planets_str.split(",") if p.strip()] or ["b"]
    fit_data["planets"] = "".join(planet_list)

    # Per-planet predicted transit centers. timer fits a per-planet t0
    # (shape = nplanets) seeded by tc_pred: a scalar broadcasts to all planets,
    # a list gives each planet its own center. Write a scalar for single-planet
    # fits (back-compatible) and a list when there are multiple planets.
    def _planet_float(prefix: str, p: str):
        raw = options.get(f"{prefix}_{p}")
        try: return float(str(raw).strip())
        except (TypeError, ValueError): return None

    DEFAULT_TC_UNC = 0.04
    tc_vals = [_planet_float("tc_pred", p) for p in planet_list]
    unc_vals = [_planet_float("tc_pred_unc", p) for p in planet_list]

    if any(v is not None for v in tc_vals):
        # Align the list 1:1 with planets: fill any planet missing a tc_pred with
        # its own t0, then the mean of the provided centers.
        provided = [v for v in tc_vals if v is not None]
        fallback = sum(provided) / len(provided)
        filled = [
            v if v is not None else (_planet_float("t0", p) if _planet_float("t0", p) is not None else fallback)
            for p, v in zip(planet_list, tc_vals)
        ]
        uncs = [u if u is not None else DEFAULT_TC_UNC for u in unc_vals]
        if len(planet_list) == 1:
            fit_data["tc_pred"] = filled[0]
            fit_data["tc_pred_unc"] = uncs[0]
        else:
            fit_data["tc_pred"] = filled
            fit_data["tc_pred_unc"] = uncs
    else:
        # No predicted centers given; keep a single t0-prior width.
        provided_unc = [u for u in unc_vals if u is not None]
        fit_data["tc_pred_unc"] = provided_unc[0] if provided_unc else DEFAULT_TC_UNC

    fit_data["chromatic"] = options.get("chromatic") != "false"
    # Run mode maps to timer's ``clobber``: "new" (start fresh) -> clobber=true,
    # "continue" (resume sampling) -> clobber=false. For backwards compatibility,
    # also check legacy "overwrite" option.
    run_mode = options.get("run_mode")
    if not run_mode:
        # Legacy support: map old "overwrite" option to clobber behavior
        if options.get("overwrite") == "true":
            run_mode = "new"
        elif options.get("overwrite") == "false":
            run_mode = "continue"
        else:
            run_mode = "new"
    fit_data["clobber"] = run_mode == "new"
    fit_data["plot_midtransit"] = options.get("plot_midtransit") == "true"
    fit_data["plot_ingress_egress"] = options.get("plot_ingress_egress") == "true"

    # Sampler options (timer defaults: tune/draws 2000, chains/cores 2).
    fit_data["tune"] = _int_opt("tune", 2000)
    fit_data["draws"] = _int_opt("draws", 2000)
    fit_data["chains"] = _int_opt("chains", 2)
    fit_data["cores"] = _int_opt("cores", 2)

    # Model options (timer defaults: include_mean and use_custom_optimizer on).
    fit_data["include_mean"] = _bool_opt("include_mean", default=True)
    fit_data["use_custom_optimizer"] = _bool_opt("use_custom_optimizer", default=True)
    fit_data["secondary_eclipse"] = _bool_opt("secondary_eclipse", default=False)
    fit_data["fit_basis"] = str(options.get("fit_basis") or "duration").strip().lower()

    # Gaussian-process noise model. Only emit the ``gp`` block when enabled:
    # timer reads gp['log_amp'] etc. directly, so use_gp=true with no block
    # would KeyError. Hyperparameters are log10-space (Matern-3/2 kernel).
    fit_data["use_gp"] = _bool_opt("use_gp", default=False)
    if fit_data["use_gp"]:
        def _gp_values(param: str, value_default: float, unc_default: float) -> tuple[float, float]:
            value = _float_opt(f"gp_{param}", value_default)
            unc = _float_opt(f"gp_{param}_unc", unc_default)
            prior = str(options.get(f"gp_{param}_prior") or "gaussian").strip().lower()
            if prior == "uniform":
                return (value + unc) / 2, unc - value
            return value, unc

        log_amp, log_amp_unc = _gp_values("log_amp", -3.0, 2.0)
        log_scale, log_scale_unc = _gp_values("log_scale", -1.0, 2.0)
        gp_block: dict = {
            "log_amp": log_amp,
            "log_amp_unc": log_amp_unc,
            "log_amp_prior": str(options.get("gp_log_amp_prior") or "gaussian").strip().lower(),
            "log_scale": log_scale,
            "log_scale_unc": log_scale_unc,
            "log_scale_prior": str(options.get("gp_log_scale_prior") or "gaussian").strip().lower(),
        }
        per_dataset = [
            p for p, key in (("log_amp", "gp_per_dataset_log_amp"),
                             ("log_scale", "gp_per_dataset_log_scale"))
            if _bool_opt(key)
        ]
        if per_dataset:
            gp_block["per_dataset"] = per_dataset
        fit_data["gp"] = gp_block

    fit_data["include_bump"] = _bool_opt("include_bump", default=False)
    fit_data["chromatic_bump"] = _bool_opt("chromatic_bump", default=False)
    if fit_data["include_bump"]:
        def _bump_values(param: str, val_default: float, unc_default: float) -> tuple[float | list[float], float | list[float], str]:
            raw_val = options.get(f"bump_{param}", "")
            prior = str(options.get(f"bump_{param}_prior") or "gaussian").strip().lower()

            s = str(raw_val).strip()
            if not s:
                if prior == "uniform":
                    low, high = val_default - 3 * unc_default, val_default + 3 * unc_default
                    return (low + high) / 2.0, high - low, prior
                return val_default, unc_default, prior

            pairs = [p.strip() for p in s.split(";") if p.strip()]
            vals_val = []
            vals_unc = []
            for p in pairs:
                parts = [x.strip() for x in p.split(",") if x.strip()]
                if len(parts) >= 2:
                    try:
                        v, u = float(parts[0]), float(parts[1])
                        if prior == "uniform":
                            vals_val.append((v + u) / 2.0)
                            vals_unc.append(u - v)
                        else:
                            vals_val.append(v)
                            vals_unc.append(u)
                    except ValueError:
                        pass

            if not vals_val:
                if prior == "uniform":
                    low, high = val_default - 3 * unc_default, val_default + 3 * unc_default
                    return (low + high) / 2.0, high - low, prior
                return val_default, unc_default, prior

            if len(vals_val) == 1:
                return vals_val[0], vals_unc[0], prior
            return vals_val, vals_unc, prior

        tcenter, tcenter_unc, tcenter_prior = _bump_values("tcenter", 0.0, 0.1)
        width, width_unc, width_prior = _bump_values("width", 0.02, 0.01)
        ampl, ampl_unc, ampl_prior = _bump_values("ampl", 0.01, 0.01)

        fit_data["bump"] = {
            "tcenter": tcenter,
            "tcenter_unc": tcenter_unc,
            "tcenter_prior": tcenter_prior,
            "width": width,
            "width_unc": width_unc,
            "width_prior": width_prior,
            "ampl": ampl,
            "ampl_unc": ampl_unc,
            "ampl_prior": ampl_prior,
        }

    fit_data["include_flare"] = _bool_opt("include_flare", default=False)
    fit_data["chromatic_flare"] = _bool_opt("chromatic_flare", default=False)
    if fit_data["include_flare"]:
        def _flare_values(param: str, val_default: float, unc_default: float) -> tuple[float | list[float], float | list[float], str]:
            raw_val = options.get(f"flare_{param}", "")
            prior = str(options.get(f"flare_{param}_prior") or "gaussian").strip().lower()

            s = str(raw_val).strip()
            if not s:
                if prior == "uniform":
                    low, high = val_default - 3 * unc_default, val_default + 3 * unc_default
                    return (low + high) / 2.0, high - low, prior
                return val_default, unc_default, prior

            pairs = [p.strip() for p in s.split(";") if p.strip()]
            vals_val = []
            vals_unc = []
            for p in pairs:
                parts = [x.strip() for x in p.split(",") if x.strip()]
                if len(parts) >= 2:
                    try:
                        v, u = float(parts[0]), float(parts[1])
                        if prior == "uniform":
                            vals_val.append((v + u) / 2.0)
                            vals_unc.append(u - v)
                        else:
                            vals_val.append(v)
                            vals_unc.append(u)
                    except ValueError:
                        pass

            if not vals_val:
                if prior == "uniform":
                    low, high = val_default - 3 * unc_default, val_default + 3 * unc_default
                    return (low + high) / 2.0, high - low, prior
                return val_default, unc_default, prior

            if len(vals_val) == 1:
                return vals_val[0], vals_unc[0], prior
            return vals_val, vals_unc, prior

        tpeak, tpeak_unc, tpeak_prior = _flare_values("tpeak", 0.0, 0.1)
        fwhm, fwhm_unc, fwhm_prior = _flare_values("fwhm", 0.02, 0.01)
        ampl, ampl_unc, ampl_prior = _flare_values("ampl", 0.01, 0.01)

        fit_data["flare"] = {
            "tpeak": tpeak,
            "tpeak_unc": tpeak_unc,
            "tpeak_prior": tpeak_prior,
            "fwhm": fwhm,
            "fwhm_unc": fwhm_unc,
            "fwhm_prior": fwhm_prior,
            "ampl": ampl,
            "ampl_unc": ampl_unc,
            "ampl_prior": ampl_prior,
        }

    fixed = options.get("fixed")
    fit_data["fixed"] = ["period", "u_star"] if fixed is None else fixed

    # Prior shapes. The GUI sends each parameter as two keys (``ror`` /
    # ``ror_unc``); for Gaussian they are [value, unc] (written to sys.yaml), for
    # Uniform they are [low, high] bounds listed here so timer builds a uniform
    # prior. A uniform prior over a held-fixed parameter is contradictory, so
    # fixed wins.
    first_planet = planet_list[0]

    fixed_params = set(fit_data["fixed"])
    uniform_block: dict = {}
    for param in _PRIOR_PARAMS:
        if param in fixed_params:
            continue
        if _prior_choice(options, param, first_planet, first_planet) != "uniform":
            continue
        bounds = [_uniform_bounds(options, param, p, first_planet) for p in planet_list]
        # Single planet -> flat [low, high]; multiple -> per-planet [[low, high], ...].
        uniform_block[param] = bounds[0] if len(planet_list) == 1 else bounds
    if uniform_block:
        fit_data["uniform"] = uniform_block

    with open(rdir / "fit.yaml", "w") as f:
        # sort_keys=False preserves the canonical band order built above
        # (g_narrow before Na_D, etc.); safe_dump's default re-alphabetizes
        # keys, which would float capital "Na_D" ahead of lowercase "g_narrow".
        yaml.safe_dump(fit_data, f, default_flow_style=False, sort_keys=False)

    # sys.yaml
    sys_data: dict = {
        "star": {
            "teff": [float(options.get("teff") or 5778.0), float(options.get("teff_unc") or 100.0)],
            "logg": [float(options.get("logg") or 4.4), float(options.get("logg_unc") or 0.1)],
            "feh": [float(options.get("feh") or 0.0), float(options.get("feh_unc") or 0.1)],
        },
        "planets": {},
    }
    for p in [p.strip() for p in planets_str.split(",") if p.strip()]:
        def get_val(key, default):
            return options.get(f"{key}_{p}") or options.get(key) or default

        def planet_pair(param, val_default, unc_default):
            # Uniform: derive a central [value, unc] from the [low, high] bounds
            # so sys.yaml still seeds a sensible (in-bounds) initial value; timer
            # reads the actual prior range from fit.yaml's uniform block.
            if _prior_choice(options, param, p, first_planet) == "uniform":
                lo, hi = _uniform_bounds(options, param, p, first_planet)
                return [(lo + hi) / 2, (hi - lo) / 2]
            return [float(get_val(param, val_default)), float(get_val(f"{param}_unc", unc_default))]

        sys_data["planets"][p] = {
            "period": planet_pair("period", 1.0, 0.001),
            "t0": [float(get_val("t0", 2450000.0)), float(get_val("t0_unc", 0.01))],
            "dur": planet_pair("dur", 0.1, 0.01),
            "ror": planet_pair("ror", 0.05, 0.005),
            "b": planet_pair("b", 0.0, 0.1),
        }

    with open(rdir / "sys.yaml", "w") as f:
        yaml.dump(
            sys_data, f, Dumper=_SysDumper,
            default_flow_style=None, sort_keys=False,
        )

    # meta.yaml
    now_utc = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    meta_data = {
        "__muscatdb_version__": __muscatdb_version__,
        "__meta__": __meta__,
        "timer_version": _timer_version(),
        "created_at": now_utc,
        "instrument": inst,
        "date": date,
        "target": target,
        "site": site or "",
        "mode": mode or "",
        "run_name": run_name or "",
        "run_id": run_id or "",
        "run_type": run_type or "",
    }
    with open(rdir / "meta.yaml", "w") as f:
        yaml.safe_dump(meta_data, f, default_flow_style=False, sort_keys=False)


# Path to the standalone helper run inside the `timer` conda env.
_LOGP_HELPER = pathlib.Path(__file__).parent / "_logp_helper.py"
# Building + compiling the PyMC model can take ~1 minute; allow generous margin.
_LOGP_TIMEOUT = 300


def compute_logp(inst: str, date: str, target: str, options: dict, selected_csvs: list[str] | None = None) -> dict:
    """Compute the transit model's log-probability for the given form options.

    Builds the timer model from the entered parameters and evaluates
    ``model.logp()`` at the prior point, in a throwaway directory so existing
    run products are untouched. Returns ``{"ok": True, "logp": <str>,
    "text": "logP= <value>"}`` or ``{"ok": False, "error": ...}``.
    """
    if inst not in INSTRUMENTS:
        return {"ok": False, "error": f"unknown instrument {inst!r}"}
    if not valid_date(date):
        return {"ok": False, "error": "date must be 6-digit yymmdd"}
    if not (target or "").strip():
        return {"ok": False, "error": "target is required"}
    try:
        fit_output_dir(inst, date, target)
    except ValueError as exc:
        return {"ok": False, "error": str(exc)}

    err = validate_fit_options(options)
    if err:
        return {"ok": False, "error": err}

    csvs = get_csv_lightcurves(inst, date, target)
    if not csvs:
        return {"ok": False, "error": "No photometry CSV lightcurves found for this target."}
    if selected_csvs is not None:
        selected = set(str(p) for p in selected_csvs)
        csvs = [c for c in csvs if str(c) in selected]
        if not csvs:
            return {"ok": False, "error": "No lightcurves selected for logP computation."}

    err = validate_no_duplicate_datasets(inst, date, csvs)
    if err:
        return {"ok": False, "error": err}

    timer_py = _conda_env_python("timer")
    if not timer_py:
        return {"ok": False, "error": "timer conda environment not found"}
    if not _LOGP_HELPER.is_file():
        return {"ok": False, "error": "logP helper script is missing"}

    tmpdir = pathlib.Path(tempfile.mkdtemp(prefix="muscat_logp_"))
    try:
        _write_fit_inputs(tmpdir, inst, date, target, csvs, options)
        try:
            proc = subprocess.run(
                [timer_py, str(_LOGP_HELPER), str(tmpdir)],
                capture_output=True, text=True, timeout=_LOGP_TIMEOUT,
            )
        except subprocess.TimeoutExpired:
            return {"ok": False, "error": f"logP computation timed out after {_LOGP_TIMEOUT}s"}

        # The helper prints a single "logP= ..." (or "logP error: ...") line
        # amid timer's own stdout noise; pick it out.
        line = next(
            (ln.strip() for ln in (proc.stdout or "").splitlines()
             if ln.strip().startswith("logP")),
            None,
        )
        if line is None:
            combined = ((proc.stdout or "") + (proc.stderr or "")).strip()
            return {"ok": False, "error": "logP computation produced no result", "detail": combined[-800:]}
        if line.startswith("logP error:"):
            return {"ok": False, "error": line[len("logP error:"):].strip() or "logP computation failed"}

        value = line.split("=", 1)[1].strip() if "=" in line else line
        return {"ok": True, "logp": value, "text": line}
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


def _fit_reduction_exists(
    inst: str,
    date: str,
    target: str,
    run_id: str,
) -> bool:
    """Check if a transit fit output directory already exists for the given run_id."""
    try:
        rdir = fit_output_dir(inst, date, target, run_id)
    except ValueError:
        return False
    return rdir.is_dir()


def start_fit(
    inst: str,
    date: str,
    target: str,
    options: dict,
    test_run: bool = False,
    selected_csvs: list[str] | None = None,
) -> dict:
    """Prepare inputs and launch a transit fit using the timer-fit script.

    If *selected_csvs* is given, only CSVs whose filename appears in the list
    are included (useful when the user unchecks some lightcurves in the UI).
    """
    if inst not in INSTRUMENTS:
        return {"ok": False, "error": f"unknown instrument {inst!r}"}
    if not valid_date(date):
        return {"ok": False, "error": "date must be 6-digit yymmdd"}
    if not (target or "").strip():
        return {"ok": False, "error": "target is required"}
    try:
        _target_dir_name(target)  # fail fast on a bad target name
    except ValueError as exc:
        return {"ok": False, "error": str(exc)}

    # Validate the fitting configuration before touching the filesystem so bad
    # input fails fast without deleting prior run products.
    err = validate_fit_options(options)
    if err:
        return {"ok": False, "error": err}

    csvs = get_csv_lightcurves(inst, date, target)
    if not csvs:
        return {"ok": False, "error": "No photometry CSV lightcurves found for this target."}
    if selected_csvs is not None:
        selected = set(str(p) for p in selected_csvs)
        csvs = [c for c in csvs if str(c) in selected]
        if not csvs:
            return {"ok": False, "error": "No lightcurves selected for fitting."}

    err = validate_no_duplicate_datasets(inst, date, csvs)
    if err:
        return {"ok": False, "error": err}

    # Identify this run: site/mode derived from the selected lightcurves (mixing
    # allowed -> "mixed"), plus the user's run-name label. The run id isolates the
    # working directory so distinct runs never overwrite each other.
    run_name = str(options.get("run_name") or "").strip()
    site, mode = selected_site_mode(inst, [c.name for c in csvs])
    run_id = build_run_id(site, mode, run_name)
    try:
        rdir = fit_output_dir(inst, date, target, run_id)
    except ValueError as exc:
        return {"ok": False, "error": str(exc)}

    run_type = "test" if test_run else "full"

    # Validate run mode for full fits with existing output
    if run_type == "full":
        run_mode = options.get("run_mode")
        if not run_mode:
            # Legacy support: map old "overwrite" option to new "run_mode"
            # overwrite="true" (checked) -> run_mode="new" (start fresh)
            # overwrite="false" (unchecked) -> run_mode="continue" (resume)
            if options.get("overwrite") == "true":
                run_mode = "new"
            elif options.get("overwrite") == "false":
                run_mode = "continue"
            else:
                run_mode = "new"  # default to "new" for fresh fits

        has_results = _fit_reduction_exists(inst, date, target, run_id)
        if has_results and run_mode not in ("new", "continue"):
            return {
                "ok": False,
                "error": "fit results exist; choose 'New Fit' (start fresh) or 'Continue Sampling'",
            }

    key = fit_job_key(inst, date, target, run_id)
    params_json = json.dumps(
        {"test_run": test_run, "options": options, "selected_csvs": selected_csvs,
         "run_id": run_id, "site": site, "mode": mode, "run_name": run_name},
        separators=(",", ":"),
    )

    with _FIT_LOCK:
        existing = _FIT_JOBS.get(key)

        if existing is not None and existing.proc.poll() is None:
            return {"ok": True, "key": key, "already_running": True, "run_id": run_id}

        at_capacity = run_type == "full" and _count_running_full() >= _MAX_FULL_JOBS

        # Queue full jobs when at capacity
        if at_capacity:
            try:
                get_job_store().enqueue(
                    type_="transit_fit",
                    inst=inst, date=date, target=target, run_id=run_id,
                    started_at=time.time(),
                    run_type=run_type,
                    params=params_json,
                    run_name=run_name,
                )
            except Exception:
                return {"ok": False, "error": "database not writable"}
            return {"ok": True, "key": key, "queued": True, "run_id": run_id}

    # Working directory
    rdir.mkdir(parents=True, exist_ok=True)

    # Preserve existing products so timer can reuse them when clobber is false.
    # When overwrite is selected, fit.yaml sets clobber=true and timer owns the
    # invalidation/replacement of its cached results.
    # Full fits always clobber — otherwise timer reuses the cached test-run
    # trace (20 draws) and exits immediately, misleading the user into thinking
    # their full-fit request was silently ignored.
    _write_fit_inputs(rdir, inst, date, target, csvs, options,
                      site=site, mode=mode, run_name=run_name, run_id=run_id,
                      run_type=run_type)

    # Clear cached outputs so the next page load reads fresh results from disk.
    _fit_outputs_cache.clear()

    # Launch process
    cmd = [*_timer_prefix(), "-v", str(rdir)]
    if test_run:
        cmd.append("--test_run")
    log_path = rdir / "timer-fit.log"
    logf = open(log_path, "w")
    _write_log_banner(logf, cmd, options)
    logf.flush()

    try:
        proc = subprocess.Popen(
            cmd,
            cwd=str(rdir),
            stdout=logf,
            stderr=subprocess.STDOUT,
            text=True,
            start_new_session=True,
        )
        try:
            with open(rdir / "timer-fit.pid", "w") as pidf:
                pidf.write(str(proc.pid))
        except Exception:
            logger.debug("failed to write timer-fit.pid in %s", rdir, exc_info=True)
    except (FileNotFoundError, OSError) as exc:
        logf.write(f"\nfailed to launch fitting: {exc}\n")
        logf.close()
        return {"ok": False, "error": f"failed to launch fitting: {exc}"}

    with _FIT_LOCK:
        _FIT_JOBS[key] = TransitFitJob(
            key=key, inst=inst, date=date, target=target,
            cmd=cmd, proc=proc, logf=logf, log_path=log_path,
            run_type=run_type, run_id=run_id, site=site, mode=mode, run_name=run_name,
        )
        # Record new job in the database
        get_job_store().save(
            type_="transit_fit",
            inst=inst,
            date=date,
            target=target,
            run_id=run_id,
            state="running",
            returncode=None,
            elapsed=0,
            started_at=_FIT_JOBS[key].started_at,
            run_type=run_type,
            params=params_json,
            run_name=run_name,
        )

    return {"ok": True, "key": key, "run_id": run_id}


def _pending_status(inst: str, date: str, target: str, run_id: str = "") -> dict | None:
    """Return a queued-job status dict if a pending DB entry exists, else None.

    A full run launched while the single full-job slot is occupied is recorded
    in the DB as ``pending`` but not added to ``_FIT_JOBS``; surface that here so the
    transit fitting page can show a "queued" state instead of silently resetting.
    """
    try:
        db_key = f"transit_fit:{fit_job_key(inst, date, target, run_id)}"
    except ValueError:
        return None
    try:
        for entry in get_job_store().all():
            if (
                entry["key"] == db_key
                and entry["type"] == "transit_fit"
                and entry["state"] == "pending"
            ):
                started = entry.get("started_at") or time.time()
                return {
                    "state": "pending",
                    "returncode": None,
                    "log": "",
                    "elapsed": round(time.time() - started),
                }
    except Exception:
        logger.debug("failed to read pending transit-fit status for %s/%s/%s/%s", inst, date, target, run_id, exc_info=True)
    return None


def _running_status(inst: str, date: str, target: str, run_id: str = "") -> dict | None:
    """Return an active running-job status dict if a running DB entry exists, else None.

    A full run launched while the single full-job slot is occupied is recorded
    in the DB as ``running``; surface that here if the process is active but not in ``_FIT_JOBS``.
    """
    try:
        db_key = f"transit_fit:{fit_job_key(inst, date, target, run_id)}"
    except ValueError:
        return None
    try:
        for entry in get_job_store().all():
            if (
                entry["key"] == db_key
                and entry["type"] == "transit_fit"
                and entry["state"] == "running"
            ):
                try:
                    rdir = fit_output_dir(inst, date, target, run_id or None)
                    lp = rdir / "timer-fit.log"
                except ValueError:
                    lp = None
                started = entry.get("started_at") or time.time()
                return {
                    "state": "running",
                    "returncode": None,
                    "log": _tail(lp) if (lp and lp.is_file()) else "",
                    "elapsed": round(time.time() - started),
                }
    except Exception:
        logger.debug("failed to read running transit-fit status for %s/%s/%s/%s", inst, date, target, run_id, exc_info=True)
    return None


def _persisted_status(inst: str, date: str, target: str, run_id: str = "") -> dict | None:
    """Return a terminal-state status dict from the DB for a job no longer in ``_FIT_JOBS``."""
    try:
        db_key = f"transit_fit:{fit_job_key(inst, date, target, run_id)}"
    except ValueError:
        return None
    try:
        for entry in get_job_store().all():  # newest-first; one row per key
            if entry["key"] != db_key or entry["type"] != "transit_fit":
                continue
            state = entry["state"]
            if state not in ("done", "error", "cancelled"):
                return None  # running/pending handled by the caller
            
            try:
                rdir = fit_output_dir(inst, date, target, run_id or None)
                lp = rdir / "timer-fit.log"
            except ValueError:
                lp = None
            
            return {
                "state": state,
                "returncode": entry.get("returncode"),
                "log": _tail(lp) if (lp and lp.is_file()) else "",
                "elapsed": round(entry.get("elapsed") or 0),
            }
    except Exception:
        logger.debug("failed to read persisted transit-fit status for %s/%s/%s/%s", inst, date, target, run_id, exc_info=True)
    return None


def job_status(inst: str, date: str, target: str, run_id: str = "") -> dict:
    """Retrieve logs and status of an active transit fitting job."""
    try:
        key = fit_job_key(inst, date, target, run_id)
    except ValueError as exc:
        return {"state": "none", "log": "", "returncode": None, "elapsed": 0, "error": str(exc)}
    with _FIT_LOCK:
        job = _FIT_JOBS.get(key)
        if job is None:
            pending = _pending_status(inst, date, target, run_id)
            if pending is not None:
                return pending
            running = _running_status(inst, date, target, run_id)
            if running is not None:
                return running
            persisted = _persisted_status(inst, date, target, run_id)
            if persisted is not None:
                return persisted
            # Check if output exists on disk
            try:
                rdir = fit_output_dir(inst, date, target, run_id or None)
                log_path = rdir / "timer-fit.log"
                if log_path.is_file():
                    # Read completed job log
                    return {"state": "done", "log": _tail(log_path), "returncode": 0, "elapsed": 0}
            except ValueError:
                pass
            return {"state": "none", "log": "", "returncode": None, "elapsed": 0}
        
        state, rc, is_terminal = jobs.resolve_job_state(job, _finalize_config())
        if is_terminal and job.state == "running":
            job.state = state
            job.returncode = rc
            job.elapsed = round(time.time() - job.started_at)
            try: job.logf.close()
            except OSError: pass
        log_path = job.log_path
        elapsed = job.elapsed if job.state not in ("running", "cancelling") and job.elapsed is not None else round(time.time() - job.started_at)

    return {
        "state": state,
        "returncode": rc,
        "log": _tail(log_path),
        "elapsed": round(elapsed),
        "run_type": job.run_type if job else "full",
    }


# Process-group kill helper lives in the shared runner (audit C1).
_kill_after = jobs.kill_after


def delete_fit(inst: str, date: str, target: str, run_id: str = "") -> dict:
    """Delete the selected run's fit results for (inst, date, target) from disk and DB.

    When *run_id* is empty (legacy single-dir run), removes the run's output
    files from the base target directory without touching named-run
    subdirectories. When *run_id* names a specific run, removes that
    subdirectory entirely. Only the matching DB job record and in-memory
    entry are cleared; other runs for the same target are preserved.
    """
    try:
        if run_id:
            rdir = fit_output_dir(inst, date, target, run_id)
        else:
            rdir = fit_output_dir(inst, date, target)
    except ValueError:
        return {"ok": False, "error": "invalid target"}

    removed = 0
    if run_id:
        # Named run subdirectory — remove the whole thing.
        if rdir.is_dir():
            try:
                shutil.rmtree(rdir)
                removed += 1
            except OSError:
                pass
    else:
        # Legacy single-dir run — remove output files from the base target
        # directory without removing the directory itself or named-run
        # subdirectories beneath it.
        if rdir.is_dir():
            legacy_targets = [
                rdir / "out",
                rdir / "timer-fit.log",
                rdir / "fit.yaml",
                rdir / "sys.yaml",
                rdir / "meta.yaml",
            ]
            for p in legacy_targets:
                try:
                    if p.is_dir():
                        shutil.rmtree(p)
                        removed += 1
                    elif p.is_file():
                        p.unlink()
                        removed += 1
                except OSError:
                    pass

    db_key = f"transit_fit:{inst}/{date}/{_target_dir_name(target)}"
    if run_id:
        db_key = f"{db_key}/{_run_dir_name(run_id)}"
    get_job_store().delete(db_key)

    job_key = f"{inst}/{date}/{_target_dir_name(target)}"
    if run_id:
        job_key = f"{job_key}/{_run_dir_name(run_id)}"
    with _FIT_LOCK:
        _FIT_JOBS.pop(job_key, None)

    _fit_outputs_cache.clear()

    return {"ok": True, "count": removed}


def cancel_fit(inst: str, date: str, target: str, run_id: str = "") -> dict:
    """Terminate the running or pending fitting process."""
    try:
        key = fit_job_key(inst, date, target, run_id)
    except ValueError as exc:
        return {"ok": False, "error": str(exc)}
    store = get_job_store()
    with _FIT_LOCK:
        job = _FIT_JOBS.get(key)
        if job is None:
            # May be a pending job (in DB but not yet launched)
            db_key = f"transit_fit:{key}"
            found = [j for j in store.all() if j["key"] == db_key]
            if found and found[0]["state"] == "pending":
                store.save(
                    type_="transit_fit", inst=inst, date=date, target=target, run_id=run_id,
                    state="cancelled", returncode=-1, elapsed=0,
                    started_at=found[0]["started_at"],
                    error_desc="Cancelled by user",
                    run_name=found[0].get("run_name", ""),
                )
                return {"ok": True, "key": key}
            return {"ok": False, "error": "no job to cancel"}
        if job.proc.poll() is not None:
            return {"ok": True, "already_finished": True}
        job.cancelled = True
        proc = job.proc
        # Immediately record cancellation in the database
        store.save(
            type_="transit_fit",
            inst=inst,
            date=date,
            target=target,
            run_id=run_id,
            state="cancelled",
            returncode=-1,
            elapsed=round(time.time() - job.started_at),
            started_at=job.started_at,
            error_desc="Cancelled by user",
            run_name=job.run_name,
        )

    try:
        os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
    except OSError:
        try: proc.terminate()
        except OSError: pass
        
    threading.Thread(target=_kill_after, args=(proc,), daemon=True).start()
    return {"ok": True, "key": key}


_fit_outputs_cache = register_cache(ttl=300.0)


def has_fit_outputs(inst: str, date: str, target: str) -> bool:
    """True when *any* transit-fit run (legacy or named) has produced outputs.

    Fits are written to either the legacy ``{target}/out/`` directory or a
    per-run ``{target}/{run_id}/out/`` subdirectory. Checking only the legacy
    layout via ``get_fit_outputs(run_id=None)`` misses every modern run and made
    the Targets/target pages report Fit status ``none`` for run-scoped fits.
    Delegates to :func:`list_fit_runs`, which enumerates both layouts and only
    counts runs that actually hold outputs.
    """
    return bool(list_fit_runs(inst, date, target))


def get_fit_outputs(inst: str, date: str, target: str, run_id: str | None = None) -> dict:
    """Check and retrieve output files, plots, and summary values from a completed run.

    The run directory's (and its ``out/`` subdir's) mtime is folded into the
    cache key so the result auto-invalidates the moment fit outputs are written
    or removed — mirroring :func:`photometry.get_photometry_status` — instead of
    lingering for up to the cache TTL after a job finishes. This keeps the
    Targets and Transit-fit pages' Fit status live and lets
    :func:`database.refresh_target_status` persist an accurate ``fit_status``.
    """
    try:
        rdir = fit_output_dir(inst, date, target, run_id or None)
    except ValueError:
        # No resolvable run dir (bad run_id / target): key on a stable sentinel;
        # the inner worker re-raises→handles the ValueError and returns "empty".
        return _get_fit_outputs_mtime(inst, date, target, run_id, -1.0)

    mtime = 0.0
    for d in (rdir, rdir / "out"):
        try:
            mtime = max(mtime, d.stat().st_mtime)
        except OSError:
            pass
    return _get_fit_outputs_mtime(inst, date, target, run_id, mtime)


@_fit_outputs_cache
def _get_fit_outputs_mtime(
    inst: str, date: str, target: str, run_id: str | None, _cache_mtime: float
) -> dict:
    """Inner cached worker. ``_cache_mtime`` participates only in the cache key
    (see :func:`get_fit_outputs`) and is otherwise unused."""
    outputs = {
        "has_any": False,
        "plots": [],
        "systematics_plots": [],
        "summary": None,
        "has_log": False,
        "has_fit_yaml": False,
        "has_sys_yaml": False,
        "has_meta_yaml": False,
        "extra_files": []
    }

    try:
        rdir = fit_output_dir(inst, date, target, run_id or None)
    except ValueError:
        return outputs
    out_dir = rdir / "out"

    if (rdir / "timer-fit.log").is_file():
        outputs["has_log"] = True

    if (rdir / "fit.yaml").is_file():
        outputs["has_fit_yaml"] = True

    if (rdir / "sys.yaml").is_file():
        outputs["has_sys_yaml"] = True

    if (rdir / "meta.yaml").is_file():
        outputs["has_meta_yaml"] = True

    if not out_dir.is_dir():
        return outputs

    # Show PNG plots produced by the run, sorted by name. Timer systematics
    # plots are grouped separately in the UI because there can be one per band.
    for p in sorted(out_dir.glob("*.png")):
        if p.is_file():
            try:
                st = p.stat()
                mtime = st.st_mtime
                version = str(st.st_mtime_ns)
                created_at = datetime.datetime.fromtimestamp(mtime).strftime('%Y-%m-%d %H:%M')
            except Exception:
                version = "0"
                created_at = "Unknown"
            plot_info = {
                "file": p.name,
                "created_at": created_at,
                "version": version,
            }
            if p.name.startswith("sys-"):
                outputs["systematics_plots"].append(plot_info)
            else:
                outputs["plots"].append(plot_info)
            outputs["has_any"] = True

    # Collect any other output files for download (exclude plots already shown
    # and files that get their own dedicated link below).
    _linked = {"summary.csv"}
    for p in sorted(out_dir.iterdir()):
        if not p.is_file():
            continue
        if p.suffix.lower() == ".png" or p.name in _linked:
            continue
        outputs["extra_files"].append(p.name)
        outputs["has_any"] = True

    # Parse summary.csv
    summary_path = out_dir / "summary.csv"
    if summary_path.is_file():
        try:
            summary_rows = []
            with open(summary_path) as f:
                reader = csv.reader(f)
                headers = next(reader)  # E.g. ["", "mean", "sd", "eti89_lb", "eti89_ub", ...]
                headers[0] = "parameter"
                for row in reader:
                    if row:
                        summary_rows.append(dict(zip(headers, row)))
            if summary_rows:
                outputs["summary"] = {
                    "headers": headers,
                    "rows": summary_rows
                }
                outputs["has_any"] = True
        except Exception:
            logger.debug("failed to read timer summary preview from %s", out_dir / "summary.csv", exc_info=True)

    return outputs


@dataclass
class RunDescriptor:
    run_id: str
    site: str
    mode: str
    run_name: str
    mtime: float
    is_legacy: bool


def _read_run_meta(d: pathlib.Path) -> dict:
    try:
        with open(d / "meta.yaml") as f:
            return yaml.safe_load(f) or {}
    except (OSError, yaml.YAMLError):
        return {}


def _run_has_outputs(d: pathlib.Path) -> bool:
    out = d / "out"
    if not out.is_dir():
        return False
    try:
        return any(out.glob("*.png")) or (out / "summary.csv").is_file()
    except OSError:
        return False


def _parse_run_dir_name(name: str) -> tuple[str, str, str]:
    """Best-effort split of a run-id dir name into (site, mode, run_name).

    Used only as a fallback when meta.yaml lacks identity keys. Components are
    hyphen-joined and never themselves contain ``-``. Newer sinistro run ids
    omit the default ``central_2k_2x2`` mode, so a site-prefixed name with no
    explicit mode is treated as central mode.
    """
    parts = name.split("-")
    site = mode = ""
    if parts and parts[0] in (set(SINISTRO_SITES) | {"mixed"}):
        site, parts = parts[0], parts[1:]
    if parts and parts[0] in (set(SINISTRO_MODES) | {"mixed"}):
        mode, parts = parts[0], parts[1:]
    elif site:
        mode = "central_2k_2x2"
    return site, mode, "-".join(parts)


def list_fit_runs(inst: str, date: str, target: str) -> list[RunDescriptor]:
    """Enumerate the runs that exist for a target, newest-first.

    Each ``{target}/<run_id>/`` holding outputs is a run; a legacy
    ``{target}/out/`` (no run subdir) is surfaced as an ``is_legacy`` run with
    ``run_id=""``. Identity is read from each run's meta.yaml, falling back to
    splitting the dir name on ``-``.
    """
    try:
        tdir = fit_output_dir(inst, date, target)
    except ValueError:
        return []
    if not tdir.is_dir():
        return []
    runs: list[RunDescriptor] = []

    if _run_has_outputs(tdir):  # legacy single-dir run
        meta = _read_run_meta(tdir)
        runs.append(RunDescriptor(
            run_id="",
            site=str(meta.get("site") or ""),
            mode=str(meta.get("mode") or ""),
            run_name=str(meta.get("run_name") or "") or "legacy",
            mtime=(tdir / "out").stat().st_mtime,
            is_legacy=True,
        ))

    for d in sorted(tdir.iterdir()):
        if not d.is_dir() or d.name == "out" or not _run_has_outputs(d):
            continue
        meta = _read_run_meta(d)
        if meta.get("run_id") or meta.get("run_name"):
            site = str(meta.get("site") or "")
            mode = str(meta.get("mode") or "")
            run_name = str(meta.get("run_name") or "")
        else:
            site, mode, run_name = _parse_run_dir_name(d.name)
        runs.append(RunDescriptor(
            run_id=d.name,
            site=site,
            mode=mode,
            run_name=run_name or d.name,
            mtime=(d / "out").stat().st_mtime,
            is_legacy=False,
        ))

    runs.sort(key=lambda r: r.mtime, reverse=True)
    return runs


def _detect_run_type(rdir: pathlib.Path) -> str:
    # 1. Try reading meta.yaml
    try:
        with open(rdir / "meta.yaml") as f:
            meta = yaml.safe_load(f) or {}
            if "run_type" in meta and meta["run_type"]:
                return str(meta["run_type"])
    except Exception:
        logger.debug("failed to detect run_type for %s from meta.yaml", rdir, exc_info=True)

    # 2. Try reading timer-fit.log to see if "--test_run" was used
    try:
        log_file = rdir / "timer-fit.log"
        if log_file.is_file():
            with open(log_file) as lf:
                for _ in range(30):
                    line = lf.readline()
                    if not line:
                        break
                    if line.startswith("$ ") and "--test_run" in line:
                        return "test"
    except Exception:
        logger.debug("failed to detect run_type for %s from timer-fit.log", rdir, exc_info=True)

    return "full"


def _discover_orphan_fits(existing: set[str]) -> list[dict]:
    """Scan disk for completed fits not yet in the jobs table.

    Walks ``$MUSCAT_TIMER_DIR/<inst>/<date>/<target>/`` and returns synthetic job
    dicts (state="done") for any run — legacy ``{target}/out/`` (key
    ``transit_fit:{inst}/{date}/{target}``) and per-run ``{target}/<run_id>/out/``
    (key ``…/{target}/{run_id}``) — whose key is not in *existing*.
    """
    base = pathlib.Path(os.environ.get("MUSCAT_TIMER_DIR", "/ut2/jerome/ql/timer"))
    orphans: list[dict] = []
    if not base.is_dir():
        return orphans
    for inst_dir in sorted(base.iterdir()):
        if not inst_dir.is_dir():
            continue
        for date_dir in sorted(inst_dir.iterdir()):
            if not date_dir.is_dir():
                continue
            for target_dir in sorted(date_dir.iterdir()):
                if not target_dir.is_dir():
                    continue
                target = target_dir.name
                inst = inst_dir.name
                date = date_dir.name

                # Each run dir = legacy {target}/ (run_id="") plus per-run
                # {target}/<run_id>/. Skip a child literally named "out" so the
                # legacy out/ is not misread as a run.
                run_dirs: list[tuple[str, pathlib.Path]] = [("", target_dir)]
                for child in sorted(target_dir.iterdir()):
                    if child.is_dir() and child.name != "out":
                        run_dirs.append((child.name, child))

                for run_id, rdir in run_dirs:
                    out_dir = rdir / "out"
                    if not out_dir.is_dir() or not sorted(out_dir.glob("*.png")):
                        continue
                    key = f"transit_fit:{inst}/{date}/{target}"
                    if run_id:
                        key = f"{key}/{run_id}"
                    if key in existing:
                        continue
                    meta = _read_run_meta(rdir)
                    orphans.append({
                        "key": key,
                        "type": "transit_fit",
                        "instrument": inst,
                        "obsdate": date,
                        "target": target,
                        "state": "done",
                        "returncode": 0,
                        "elapsed": 0,
                        "started_at": out_dir.stat().st_mtime,
                        "error_desc": None,
                        "run_type": _detect_run_type(rdir),
                        "run_id": run_id,
                        "run_name": str(meta.get("run_name") or ""),
                        "inst": inst,
                        "date": date,
                    })
    return orphans

def _is_pid_running(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def _detect_process_running(rdir: pathlib.Path) -> bool:
    pid_file = rdir / "timer-fit.pid"
    if pid_file.is_file():
        try:
            with open(pid_file) as f:
                pid = int(f.read().strip())
            return _is_pid_running(pid)
        except Exception:
            logger.debug("failed to read timer-fit.pid in %s", rdir, exc_info=True)
    return False


def sync_jobs() -> None:
    store = get_job_store()
    with _FIT_LOCK:
        db_jobs = store.all()
        running_keys = {j["key"] for j in db_jobs if j["state"] == "running" and j["type"] == "transit_fit"}
        db_by_key = {j["key"]: j for j in db_jobs}

        for key, job in _FIT_JOBS.items():
            db_key = f"transit_fit:{fit_job_key(job.inst, job.date, job.target, job.run_id)}"
            state, rc, is_terminal = jobs.resolve_job_state(job, _finalize_config())
            if is_terminal and job.state == "running":
                job.state = state
                job.returncode = rc
                job.elapsed = round(time.time() - job.started_at)
                try:
                    job.logf.close()
                except OSError:
                    pass

            # 'finalizing' is a live-view-only state; the DB tracks only
            # running/terminal, so persist a finalizing job as still running.
            # This keeps the Jobs page (which reads state from the DB) consistent
            # with the transit-fit page until the log truly goes quiescent.
            persist_state = "running" if state == "finalizing" else state
            persist_rc = None if state == "finalizing" else rc

            # Only persist when the row actually changed. A steadily-running job
            # whose DB row already says "running" needs no rewrite; elapsed is
            # computed live in the web layer, so we no longer write every 2s poll
            # just to bump it (each write also fired clear_all_caches, nullifying
            # the directory caches). Terminal transitions still write through.
            existing = db_by_key.get(db_key)

            # For terminal jobs: use stored elapsed (runtime), not time since start
            # For running jobs: calculate from current time
            if job.state not in ("running", "cancelling") and job.elapsed is not None:
                elapsed = job.elapsed  # Use calculated runtime when job hit terminal state
            elif existing is not None and existing.get("state") not in ("running", "cancelling"):
                elapsed = existing.get("elapsed") or 0  # Use existing DB value for completed jobs
            else:
                elapsed = round(time.time() - job.started_at)  # Calculate for running jobs
            unchanged = (
                existing is not None
                and existing.get("state") == persist_state
                and existing.get("returncode") == persist_rc
            )
            running_keys.discard(db_key)
            if unchanged:
                continue

            error_desc = ""
            if persist_state == "error":
                error_desc = _get_error_desc(job.log_path)
            elif persist_state == "cancelled":
                error_desc = "Cancelled by user"

            store.save(
                type_="transit_fit",
                inst=job.inst,
                date=job.date,
                target=job.target,
                run_id=job.run_id,
                state=persist_state,
                returncode=persist_rc,
                elapsed=round(elapsed),
                started_at=job.started_at,
                error_desc=error_desc,
                run_name=job.run_name,
            )

            # A terminal transition may have produced new fit outputs; refresh the
            # target's persisted Phot/Fit status so the Targets page reflects it
            # on the next refresh instead of waiting for the daily build_db cron.
            if persist_state in ("done", "error", "cancelled"):
                database.refresh_target_status(job.target)

        for db_key in running_keys:
            # Read identity from the DB row's columns (robust to run_id in the key).
            row = next((j for j in db_jobs if j["key"] == db_key), None)
            if row is None:
                continue
            inst = row["inst"]
            date = row["date"]
            target = row["target"]
            run_id = row.get("run_id", "")
            
            # Check if outputs exist on disk (meaning the process finished successfully in the background)
            completed_ok = False
            rdir = None
            try:
                rdir = fit_output_dir(inst, date, target, run_id or None)
                if _run_has_outputs(rdir):
                    log_path = rdir / "timer-fit.log"
                    if log_path.is_file():
                        with open(log_path, errors="replace") as lf:
                            log_content = lf.read()
                            if "Timer-fit completed successfully" in log_content:
                                completed_ok = True
            except Exception:
                logger.debug("failed to inspect orphan fit completion for %s", rdir, exc_info=True)
                
            if completed_ok:
                store.save(
                    type_="transit_fit",
                    inst=inst,
                    date=date,
                    target=target,
                    run_id=run_id,
                    state="done",
                    returncode=0,
                    elapsed=row["elapsed"],
                    started_at=row["started_at"],
                    error_desc=""
                )
                database.refresh_target_status(target)
            elif rdir is not None and _detect_process_running(rdir):
                # Process is still running on the system, leave state as "running"
                continue
            else:
                store.save(
                    type_="transit_fit",
                    inst=inst,
                    date=date,
                    target=target,
                    run_id=run_id,
                    state="error",
                    returncode=-1,
                    elapsed=row["elapsed"],
                    started_at=row["started_at"],
                    error_desc="Process lost (server restart)"
                )
                database.refresh_target_status(target)

        # Launch pending full jobs if capacity allows
        if _count_running_full() < _MAX_FULL_JOBS:
            pending = store.pending("transit_fit")
            for entry in pending:
                if _count_running_full() >= _MAX_FULL_JOBS:
                    break
                # The DB key is prefixed ("transit_fit:..."), so compare against the
                # in-memory job key (unprefixed) to detect a job that is already
                # running and must not be relaunched from its stale pending row.
                entry_run_id = entry.get("run_id", "")
                try:
                    mem_key = fit_job_key(entry["inst"], entry["date"], entry["target"], entry_run_id)
                except ValueError:
                    mem_key = None
                if mem_key is not None and mem_key in _FIT_JOBS:
                    store.save(type_="transit_fit", inst=entry["inst"], date=entry["date"], target=entry["target"], run_id=entry_run_id, state="error", returncode=-1, elapsed=0, started_at=entry["started_at"], error_desc="Duplicate entry", run_name=entry.get("run_name", ""))
                    continue
                try:
                    p = json.loads(entry.get("params") or "{}")
                except (json.JSONDecodeError, TypeError):
                    p = {}
                opts = p.get("options", {})
                test_run = p.get("test_run", False)
                run_type = "test" if test_run else "full"
                selected_csvs = p.get("selected_csvs")
                run_id = p.get("run_id") or entry.get("run_id", "")
                site = p.get("site", "")
                mode = p.get("mode", "")
                run_name = p.get("run_name", "")
                inst, date, target = entry["inst"], entry["date"], entry["target"]
                try:
                    key = fit_job_key(inst, date, target, run_id)
                    rdir = fit_output_dir(inst, date, target, run_id or None)
                except ValueError:
                    store.save(
                        type_="transit_fit",
                        inst=inst,
                        date=date,
                        target=target,
                        run_id=run_id,
                        state="error",
                        returncode=-1,
                        elapsed=0,
                    started_at=entry["started_at"],
                    error_desc="Invalid target",
                    run_type=entry.get("run_type", ""),
                    params=entry.get("params", ""),
                    run_name=run_name,
                )
                    continue
                rdir.mkdir(parents=True, exist_ok=True)
                csvs = get_csv_lightcurves(inst, date, target)
                if selected_csvs is not None:
                    selected = set(str(p) for p in selected_csvs)
                    csvs = [c for c in csvs if str(c) in selected]
                _write_fit_inputs(rdir, inst, date, target, csvs, opts,
                                  site=site, mode=mode, run_name=run_name, run_id=run_id,
                                  run_type=run_type)
                _fit_outputs_cache.clear()
                cmd = [*_timer_prefix(), "-v", str(rdir)]
                if test_run:
                    cmd.append("--test_run")
                log_path = rdir / "timer-fit.log"
                try:
                    logf = open(log_path, "w")
                    _write_log_banner(logf, cmd, opts)
                    logf.flush()
                    proc = subprocess.Popen(cmd, cwd=str(rdir), stdout=logf, stderr=subprocess.STDOUT, text=True, start_new_session=True)
                    try:
                        with open(rdir / "timer-fit.pid", "w") as pidf:
                            pidf.write(str(proc.pid))
                    except Exception:
                        logger.debug("failed to write timer-fit.pid for queued run %s", rdir, exc_info=True)
                except (FileNotFoundError, OSError) as exc:
                    try: logf.close()
                    except OSError: pass
                    store.save(type_="transit_fit", inst=inst, date=date, target=target, run_id=run_id, state="error", returncode=-1, elapsed=0, started_at=entry["started_at"], error_desc=f"Failed to launch: {exc}", run_name=run_name)
                    continue
                run_type = "test" if test_run else "full"
                _FIT_JOBS[key] = TransitFitJob(key=key, inst=inst, date=date, target=target, cmd=cmd, proc=proc, logf=logf, log_path=log_path, run_type=run_type, run_id=run_id, site=site, mode=mode, run_name=run_name)
                try:
                    store.save(type_="transit_fit", inst=inst, date=date, target=target, run_id=run_id, state="running", returncode=None, elapsed=0, started_at=_FIT_JOBS[key].started_at, run_type=run_type, params=entry.get("params", ""), run_name=run_name)
                except Exception:
                    logger.debug("failed to persist queued transit-fit launch for %s", run_id, exc_info=True)
                    try: proc.terminate()
                    except OSError: pass
                    try: logf.close()
                    except OSError: pass
                    _FIT_JOBS.pop(key, None)
                    store.save(type_="transit_fit", inst=inst, date=date, target=target, run_id=run_id, state="error", returncode=-1, elapsed=0, started_at=entry["started_at"], error_desc="Database error", run_name=run_name)
