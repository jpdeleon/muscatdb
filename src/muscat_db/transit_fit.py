"""Helpers for the Transit Fit page: manage config generation (fit.yaml, sys.yaml),
run the transit-fit pipeline, poll logs, and return outputs/plots.
"""
from __future__ import annotations

import csv
import datetime
import json
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
from dataclasses import dataclass, field
from typing import IO
import yaml

from muscat_db.instruments import INSTRUMENTS
from muscat_db.photometry import output_base, valid_date, _conda_env_python, _tail, _to_float
from muscat_db.cache import register_cache

_REPO_ROOT = pathlib.Path(__file__).parent.parent.parent.resolve()


def fit_output_dir(inst: str, date: str, target: str) -> pathlib.Path:
    """Return a transit-fit output directory confined below the timer root.

    Spaces are removed from the target directory component. ``ValueError`` is
    raised for an empty target or one containing ``..``, ``/``, or ``\\``.
    """
    base = pathlib.Path(os.environ.get("MUSCAT_TIMER_DIR", "/ut2/jerome/ql/timer")).expanduser().resolve(strict=False)
    target_dir = _target_dir_name(target)
    path = (base / inst / date / target_dir).resolve(strict=False)
    try:
        path.relative_to(base)
    except ValueError as exc:
        raise ValueError("invalid target") from exc
    return path


def _target_dir_name(target: str) -> str:
    target_dir = (target or "").replace(" ", "")
    if (
        not target_dir
        or ".." in target_dir
        or "/" in target_dir
        or "\\" in target_dir
        or target_dir in {".", ".."}
    ):
        raise ValueError("invalid target")
    return target_dir


def log_path(inst: str, date: str, target: str) -> pathlib.Path | None:
    try:
        rdir = fit_output_dir(inst, date, target)
    except ValueError:
        return None
    p = rdir / "timer-fit.log"
    return p if p.is_file() else None


@dataclass
class TransitFitJob:
    key: str
    inst: str
    date: str
    target: str
    cmd: list[str]
    proc: subprocess.Popen
    logf: IO
    log_path: pathlib.Path
    started_at: float = field(default_factory=time.time)
    state: str = "running"      # running | done | error | cancelled
    returncode: int | None = None
    cancelled: bool = False
    elapsed: int | None = None
    run_type: str = "full"      # "test" | "full"


_FIT_JOBS: dict[str, TransitFitJob] = {}
_FIT_LOCK = threading.Lock()
_MAX_FULL_JOBS = 1


def _count_running_full() -> int:
    """Number of currently-running full (non-test) transit fit jobs."""
    return sum(1 for j in _FIT_JOBS.values() if j.run_type == "full" and j.proc.poll() is None)


def fit_job_key(inst: str, date: str, target: str) -> str:
    """Return a job key using the validated target directory name."""
    return f"{inst}/{date}/{_target_dir_name(target)}"


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
    """Find the CSV lightcurves outputted by the Photometry page for a target."""
    rdir = output_base() / inst / date
    if not rdir.is_dir():
        return []

    target_clean = target.replace(" ", "").replace("-", "").lower()
    csvs = []
    for f in rdir.glob("*.csv"):
        if f.name.startswith("_") or "summary" in f.name:
            continue
        fname = f.name.lower()
        if inst.lower() in fname and date in fname:
            t_part = fname.split(f"_{inst.lower()}_")[0]
            t_clean = t_part.replace("-", "")
            if t_clean == target_clean:
                csvs.append(f)
    return sorted(csvs)


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
        pass

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
        # Reject obviously invalid ranges early; blanks fall back to the pipeline
        # default bounds and are validated by timer.
        for p in tokens:
            lo = _planet_value(o, param, p, tokens[0])
            hi = _planet_value(o, f"{param}_unc", p, tokens[0])
            if lo is None or hi is None:
                continue
            if not lo < hi:
                return f"{param} uniform bounds (planet {p}) must have low < high"
            if param == "ror" and (lo < 0 or hi > 1):
                return f"{param} uniform bounds (planet {p}) must stay within [0, 1]"
            if param == "b" and lo < 0:
                return f"{param} uniform lower bound (planet {p}) must be 0 or greater"

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
    csvs: list[pathlib.Path],
    options: dict,
) -> None:
    """Copy light-curve CSVs into ``rdir`` and write fit.yaml / sys.yaml.

    Shared by :func:`start_fit` (real run directory) and :func:`compute_logp`
    (throwaway temp directory) so both build identical timer inputs from the
    form options.
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
    fit_data["planets"] = planets_str
    planet_list = [p.strip() for p in planets_str.split(",") if p.strip()] or ["b"]

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
    # "Overwrite" maps to timer's ``clobber``: when true, timer ignores any saved
    # *.pkl results and re-runs the fit from scratch. Default (unchecked) is false.
    fit_data["clobber"] = options.get("overwrite") == "true"

    # Sampler options (timer defaults: tune/draws 2000, chains/cores 2).
    fit_data["tune"] = _int_opt("tune", 2000)
    fit_data["draws"] = _int_opt("draws", 2000)
    fit_data["chains"] = _int_opt("chains", 2)
    fit_data["cores"] = _int_opt("cores", 2)

    # Model options (timer defaults: include_mean and use_custom_optimizer on).
    fit_data["include_mean"] = _bool_opt("include_mean", default=True)
    fit_data["use_custom_optimizer"] = _bool_opt("use_custom_optimizer", default=True)

    # Gaussian-process noise model. Only emit the ``gp`` block when enabled:
    # timer reads gp['log_amp'] etc. directly, so use_gp=true with no block
    # would KeyError. Hyperparameters are log10-space (Matern-3/2 kernel).
    fit_data["use_gp"] = _bool_opt("use_gp", default=False)
    if fit_data["use_gp"]:
        gp_block: dict = {
            "log_amp": _float_opt("gp_log_amp", -3.0),
            "log_amp_unc": _float_opt("gp_log_amp_unc", 2.0),
            "log_amp_prior": options.get("gp_log_amp_prior") or "gaussian",
            "log_scale": _float_opt("gp_log_scale", -1.0),
            "log_scale_unc": _float_opt("gp_log_scale_unc", 2.0),
            "log_scale_prior": options.get("gp_log_scale_prior") or "gaussian",
        }
        per_dataset = [
            p for p, key in (("log_amp", "gp_per_dataset_log_amp"),
                             ("log_scale", "gp_per_dataset_log_scale"))
            if _bool_opt(key)
        ]
        if per_dataset:
            gp_block["per_dataset"] = per_dataset
        fit_data["gp"] = gp_block

    fit_data["fixed"] = options.get("fixed") or ["period", "u_star"]

    # Prior shapes. The GUI sends each parameter as two keys (``ror`` /
    # ``ror_unc``); for Gaussian they are [value, unc] (written to sys.yaml), for
    # Uniform they are [low, high] bounds listed here so timer builds a uniform
    # prior. A uniform prior over a held-fixed parameter is contradictory, so
    # fixed wins.
    first_planet = planet_list[0]

    def _uniform_bounds(param: str, p: str) -> list[float]:
        lo_default, hi_default = _UNIFORM_DEFAULT_BOUNDS[param]
        lo = _planet_value(options, param, p, first_planet)
        hi = _planet_value(options, f"{param}_unc", p, first_planet)
        return [lo if lo is not None else lo_default, hi if hi is not None else hi_default]

    fixed_params = set(fit_data["fixed"])
    uniform_block: dict = {}
    for param in _PRIOR_PARAMS:
        if param in fixed_params:
            continue
        if _prior_choice(options, param, first_planet, first_planet) != "uniform":
            continue
        bounds = [_uniform_bounds(param, p) for p in planet_list]
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
                lo, hi = _uniform_bounds(param, p)
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
        selected = set(selected_csvs)
        csvs = [c for c in csvs if c.name in selected]
        if not csvs:
            return {"ok": False, "error": "No lightcurves selected for logP computation."}

    timer_py = _conda_env_python("timer")
    if not timer_py:
        return {"ok": False, "error": "timer conda environment not found"}
    if not _LOGP_HELPER.is_file():
        return {"ok": False, "error": "logP helper script is missing"}

    tmpdir = pathlib.Path(tempfile.mkdtemp(prefix="muscat_logp_"))
    try:
        _write_fit_inputs(tmpdir, inst, date, csvs, options)
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
        rdir = fit_output_dir(inst, date, target)
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
        selected = set(selected_csvs)
        csvs = [c for c in csvs if c.name in selected]
        if not csvs:
            return {"ok": False, "error": "No lightcurves selected for fitting."}

    # Working directory
    rdir.mkdir(parents=True, exist_ok=True)

    # Preserve existing products so timer can reuse them when clobber is false.
    # When overwrite is selected, fit.yaml sets clobber=true and timer owns the
    # invalidation/replacement of its cached results.
    _write_fit_inputs(rdir, inst, date, csvs, options)

    key = fit_job_key(inst, date, target)
    run_type = "test" if test_run else "full"

    with _FIT_LOCK:
        # Queue full jobs when at capacity
        if run_type == "full" and _count_running_full() >= _MAX_FULL_JOBS:
            from muscat_db.database import save_job
            try:
                save_job(
                    type_="transit_fit",
                    inst=inst, date=date, target=target,
                    state="pending",
                    returncode=None, elapsed=0,
                    started_at=time.time(),
                    run_type=run_type,
                    params=json.dumps({"test_run": test_run, "options": options, "selected_csvs": selected_csvs}, separators=(",", ":"))
                )
            except Exception:
                return {"ok": False, "error": "database not writable"}
            return {"ok": True, "key": key, "queued": True}

    # Launch process
    cmd = [*_timer_prefix(), "-v", str(rdir)]
    if test_run:
        cmd.append("--test_run")
    log_path = rdir / "timer-fit.log"
    logf = open(log_path, "w")
    logf.write(f"$ {shlex.join(cmd)}\n\n")
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
    except (FileNotFoundError, OSError) as exc:
        logf.write(f"\nfailed to launch fitting: {exc}\n")
        logf.close()
        return {"ok": False, "error": f"failed to launch fitting: {exc}"}

    with _FIT_LOCK:
        _FIT_JOBS[key] = TransitFitJob(
            key=key, inst=inst, date=date, target=target,
            cmd=cmd, proc=proc, logf=logf, log_path=log_path,
            run_type=run_type,
        )
        # Record new job in the database
        from muscat_db.database import save_job
        save_job(
            type_="transit_fit",
            inst=inst,
            date=date,
            target=target,
            state="running",
            returncode=None,
            elapsed=0,
            started_at=_FIT_JOBS[key].started_at,
            run_type=run_type,
            params=json.dumps({"test_run": test_run, "options": options, "selected_csvs": selected_csvs}, separators=(",", ":"))
        )

    return {"ok": True, "key": key}


def job_status(inst: str, date: str, target: str) -> dict:
    """Retrieve logs and status of an active transit fitting job."""
    try:
        key = fit_job_key(inst, date, target)
    except ValueError as exc:
        return {"state": "none", "log": "", "returncode": None, "elapsed": 0, "error": str(exc)}
    with _FIT_LOCK:
        job = _FIT_JOBS.get(key)
        if job is None:
            # Check if output exists on disk
            rdir = fit_output_dir(inst, date, target)
            log_path = rdir / "timer-fit.log"
            if log_path.is_file():
                # Read completed job log
                return {"state": "done", "log": _tail(log_path), "returncode": 0, "elapsed": 0}
            return {"state": "none", "log": "", "returncode": None, "elapsed": 0}
        
        rc = job.proc.poll()
        if rc is None:
            state = "cancelling" if job.cancelled else "running"
        else:
            if job.cancelled:
                state = "cancelled"
            else:
                state = "done" if rc == 0 else "error"
            if job.state in ("running",):
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
    }


def _kill_after(proc: subprocess.Popen, grace: float = 6.0) -> None:
    try:
        proc.wait(timeout=grace)
        return
    except subprocess.TimeoutExpired:
        pass
    try:
        os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
    except OSError:
        try: proc.kill()
        except OSError: pass


def cancel_fit(inst: str, date: str, target: str) -> dict:
    """Terminate the running or pending fitting process."""
    try:
        key = fit_job_key(inst, date, target)
    except ValueError as exc:
        return {"ok": False, "error": str(exc)}
    with _FIT_LOCK:
        from muscat_db.database import save_job, get_persisted_jobs
        job = _FIT_JOBS.get(key)
        if job is None:
            # May be a pending job (in DB but not yet launched)
            db_jobs = get_persisted_jobs()
            db_key = f"transit_fit:{key}"
            found = [j for j in db_jobs if j["key"] == db_key]
            if found and found[0]["state"] == "pending":
                save_job(
                    type_="transit_fit", inst=inst, date=date, target=target,
                    state="cancelled", returncode=-1, elapsed=0,
                    started_at=found[0]["started_at"],
                    error_desc="Cancelled by user"
                )
                return {"ok": True, "key": key}
            return {"ok": False, "error": "no job to cancel"}
        if job.proc.poll() is not None:
            return {"ok": True, "already_finished": True}
        job.cancelled = True
        proc = job.proc
        # Immediately record cancellation in the database
        save_job(
            type_="transit_fit",
            inst=inst,
            date=date,
            target=target,
            state="cancelled",
            returncode=-1,
            elapsed=round(time.time() - job.started_at),
            started_at=job.started_at,
            error_desc="Cancelled by user"
        )

    try:
        os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
    except OSError:
        try: proc.terminate()
        except OSError: pass
        
    threading.Thread(target=_kill_after, args=(proc,), daemon=True).start()
    return {"ok": True, "key": key}


def get_all_jobs() -> list[dict]:
    """Retrieve all background fitting jobs."""
    with _FIT_LOCK:
        res = []
        for key, job in _FIT_JOBS.items():
            rc = job.proc.poll()
            if rc is None:
                state = "cancelling" if job.cancelled else "running"
            else:
                if job.cancelled:
                    state = "cancelled"
                else:
                    state = "done" if rc == 0 else "error"
                if job.state in ("running",):
                    job.state = state
                    job.returncode = rc
                    job.elapsed = round(time.time() - job.started_at)
                    try: job.logf.close()
                    except OSError: pass
            
            elapsed = job.elapsed if job.state not in ("running", "cancelling") and job.elapsed is not None else round(time.time() - job.started_at)
            res.append({
                "key": job.key,
                "inst": job.inst,
                "date": job.date,
                "target": job.target,
                "type": "transit_fit",
                "state": state,
                "returncode": rc,
                "elapsed": round(elapsed),
                "started_at": job.started_at,
            })
        return sorted(res, key=lambda j: j["started_at"], reverse=True)


_fit_outputs_cache = register_cache(ttl=300.0)


@_fit_outputs_cache
def get_fit_outputs(inst: str, date: str, target: str) -> dict:
    """Check and retrieve output files, plots, and summary values from completed run."""
    outputs = {
        "has_any": False,
        "plots": [],
        "summary": None,
        "has_log": False,
        "has_fit_yaml": False,
        "has_sys_yaml": False,
        "extra_files": []
    }

    try:
        rdir = fit_output_dir(inst, date, target)
    except ValueError:
        return outputs
    out_dir = rdir / "out"

    if (rdir / "timer-fit.log").is_file():
        outputs["has_log"] = True

    if (rdir / "fit.yaml").is_file():
        outputs["has_fit_yaml"] = True

    if (rdir / "sys.yaml").is_file():
        outputs["has_sys_yaml"] = True

    if not out_dir.is_dir():
        return outputs

    # Show every PNG plot produced by the run, sorted by name.
    for p in sorted(out_dir.glob("*.png")):
        if p.is_file():
            try:
                mtime = p.stat().st_mtime
                created_at = datetime.datetime.fromtimestamp(mtime).strftime('%Y-%m-%d %H:%M')
            except Exception:
                created_at = "Unknown"
            outputs["plots"].append({"file": p.name, "created_at": created_at})
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
            pass

    return outputs


def _discover_orphan_fits(existing: set[str]) -> list[dict]:
    """Scan disk for completed fits not yet in the jobs table.

    Walks ``$MUSCAT_TIMER_DIR/<inst>/<date>/<target>/out/*.png``
    and returns synthetic job dicts (state="done") for any fit whose
    key (``transit_fit:{inst}/{date}/{target}``) is not in *existing*.
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
                key = f"transit_fit:{inst}/{date}/{target}"
                if key in existing:
                    continue
                out_dir = target_dir / "out"
                if not out_dir.is_dir():
                    continue
                pngs = sorted(out_dir.glob("*.png"))
                if not pngs:
                    continue
                started_at = out_dir.stat().st_mtime
                orphans.append({
                    "key": key,
                    "type": "transit_fit",
                    "instrument": inst,
                    "obsdate": date,
                    "target": target,
                    "state": "done",
                    "returncode": 0,
                    "elapsed": 0,
                    "started_at": started_at,
                    "error_desc": None,
                    "run_type": "",
                    "inst": inst,
                    "date": date,
                })
    return orphans


def sync_jobs() -> None:
    from muscat_db.database import save_job, get_persisted_jobs
    with _FIT_LOCK:
        db_jobs = get_persisted_jobs()
        running_keys = {j["key"] for j in db_jobs if j["state"] == "running" and j["type"] == "transit_fit"}
        
        for key, job in _FIT_JOBS.items():
            db_key = f"transit_fit:{job.inst}/{job.date}/{job.target.replace(' ', '')}"
            rc = job.proc.poll()
            if rc is None:
                state = "cancelling" if job.cancelled else "running"
            else:
                if job.cancelled:
                    state = "cancelled"
                else:
                    state = "done" if rc == 0 else "error"
                if job.state in ("running",):
                    job.state = state
                    job.returncode = rc
                    job.elapsed = round(time.time() - job.started_at)
                    try:
                        job.logf.close()
                    except OSError:
                        pass
            
            elapsed = job.elapsed if job.state not in ("running", "cancelling") and job.elapsed is not None else round(time.time() - job.started_at)
            error_desc = ""
            if state == "error":
                from muscat_db.photometry import _get_error_desc
                error_desc = _get_error_desc(job.log_path)
            elif state == "cancelled":
                error_desc = "Cancelled by user"
            
            save_job(
                type_="transit_fit",
                inst=job.inst,
                date=job.date,
                target=job.target,
                state=state,
                returncode=rc,
                elapsed=round(elapsed),
                started_at=job.started_at,
                error_desc=error_desc
            )
            running_keys.discard(db_key)
            
        for db_key in running_keys:
            _, rest = db_key.split(":", 1)
            inst, date, target = rest.split("/", 2)
            started_at = time.time()
            elapsed = 0
            for j in db_jobs:
                if j["key"] == db_key:
                    started_at = j["started_at"]
                    elapsed = j["elapsed"]
                    break
            save_job(
                type_="transit_fit",
                inst=inst,
                date=date,
                target=target,
                state="error",
                returncode=-1,
                elapsed=elapsed,
                started_at=started_at,
                error_desc="Process lost (server restart)"
            )

        # Launch pending full jobs if capacity allows
        if _count_running_full() < _MAX_FULL_JOBS:
            db_jobs = get_persisted_jobs()
            pending = [j for j in db_jobs if j["state"] == "pending" and j["type"] == "transit_fit"]
            pending.sort(key=lambda j: j["started_at"])
            for entry in pending:
                if _count_running_full() >= _MAX_FULL_JOBS:
                    break
                if entry["key"] in _FIT_JOBS:
                    save_job(type_="transit_fit", inst=entry["inst"], date=entry["date"], target=entry["target"], state="error", returncode=-1, elapsed=0, started_at=entry["started_at"], error_desc="Duplicate entry")
                    continue
                try:
                    p = json.loads(entry.get("params") or "{}")
                except (json.JSONDecodeError, TypeError):
                    p = {}
                opts = p.get("options", {})
                test_run = p.get("test_run", False)
                selected_csvs = p.get("selected_csvs")
                inst, date, target = entry["inst"], entry["date"], entry["target"]
                try:
                    key = fit_job_key(inst, date, target)
                    rdir = fit_output_dir(inst, date, target)
                except ValueError:
                    save_job(
                        type_="transit_fit",
                        inst=inst,
                        date=date,
                        target=target,
                        state="error",
                        returncode=-1,
                        elapsed=0,
                        started_at=entry["started_at"],
                        error_desc="Invalid target",
                        run_type=entry.get("run_type", ""),
                        params=entry.get("params", ""),
                    )
                    continue
                rdir.mkdir(parents=True, exist_ok=True)
                _write_fit_inputs(rdir, inst, date, get_csv_lightcurves(inst, date, target), opts)
                cmd = [*_timer_prefix(), "-v", str(rdir)]
                if test_run:
                    cmd.append("--test_run")
                log_path = rdir / "timer-fit.log"
                try:
                    logf = open(log_path, "w")
                    logf.write(f"$ {shlex.join(cmd)}\n\n")
                    logf.flush()
                    proc = subprocess.Popen(cmd, cwd=str(rdir), stdout=logf, stderr=subprocess.STDOUT, text=True, start_new_session=True)
                except (FileNotFoundError, OSError) as exc:
                    try: logf.close()
                    except OSError: pass
                    save_job(type_="transit_fit", inst=inst, date=date, target=target, state="error", returncode=-1, elapsed=0, started_at=entry["started_at"], error_desc=f"Failed to launch: {exc}")
                    continue
                run_type = "test" if test_run else "full"
                _FIT_JOBS[key] = TransitFitJob(key=key, inst=inst, date=date, target=target, cmd=cmd, proc=proc, logf=logf, log_path=log_path, run_type=run_type)
                try:
                    save_job(type_="transit_fit", inst=inst, date=date, target=target, state="running", returncode=None, elapsed=0, started_at=_FIT_JOBS[key].started_at, run_type=run_type, params=entry.get("params", ""))
                except Exception:
                    try: proc.terminate()
                    except OSError: pass
                    try: logf.close()
                    except OSError: pass
                    _FIT_JOBS.pop(key, None)
                    save_job(type_="transit_fit", inst=inst, date=date, target=target, state="error", returncode=-1, elapsed=0, started_at=entry["started_at"], error_desc="Database error")
