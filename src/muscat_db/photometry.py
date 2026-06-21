"""Helpers for the photometry page: locate prose pipeline outputs, serve
artifacts safely, and launch reductions as background jobs.

The prose pipeline (``../ext_tools/prose2``,
``python -m prose.scripts.run_photometry``) writes a flat directory of products
per instrument/date under ``$MUSCAT_PROSE_DIR/<inst>/<date>/`` with filenames

    {target}_{inst}_{band}_{date}_ref.png        # per-band reference image
    {target}_{inst}_{band}_{date}_apertures.png  # per-band aperture overlay
    {target}_{inst}_{band}_{date}_alignment.png  # per-band alignment diagnostic
    {target}_{inst}_{band}_{date}.gif            # per-band animation
    {target}_{inst}_{band}_{date}.csv            # per-band light curve
    {target}_{inst}_{date}_lightcurves.png       # multi-band summary
    {target}_{inst}_{date}_covariates.png        # multi-band summary
    {target}_{inst}_{date}_stacks.png            # multi-band summary
    {target}_{inst}_{date}.npz                   # data archive
    {iso-timestamp}.log                          # pipeline log

This module never trusts user input for filesystem access: see
``safe_artifact_path``.
"""

from __future__ import annotations

import csv as _csv
import datetime
import hashlib
import json
import os
import re
import shlex
import shutil
import signal
import sqlite3
import subprocess
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import IO

from muscat_db.instruments import INSTRUMENTS
from muscat_db.cache import register_cache

# --------------------------- configuration ---------------------------
# All paths are env-overridable so the page works in dev and on the server.
_HERE = Path(__file__).resolve().parent          # .../src/muscat_db
_REPO_ROOT = _HERE.parent.parent                 # .../muscat-db
_DEFAULT_PROSE_PROJECT = _REPO_ROOT.parent / "ext_tools" / "prose2"
_DEFAULT_OUTPUT_BASE = "/ut2/jerome/ql/prose"

DEFAULT_BANDS = ["gp", "rp", "ip", "zs"]
# Default values for every optional run_photometry argument the form exposes.
# Kept here so the template, normalizer, and command builder share one source.
RUN_DEFAULTS: dict = {
    "bands": DEFAULT_BANDS,
    "ref_band": "",            # "" -> per-band self-reference (pipeline default)
    "refid": "",               # "" -> pipeline default (0 / middle frame)
    "aper_radii": "",          # "MIN,MAX,DR"; "" -> Gaia heuristic
    "annulus": "",             # "RIN,ROUT"; required with aper_radii
    "aper_unit": "pix",        # pix | fwhm (only applies with aper_radii)
    "make_gif": True,
    "plot_gaia_sources": True,
    "use_barycorrpy": False,
    "test_run_frames": 10,
    "min_star_separation": 10.0,
    "max_num_stars": 10,
    "n_stars_align": "",       # "" -> same as max_num_stars
    "cutout_size": 35,
    "ccd_trim": "",            # "Y,X"; "" -> no trim (pipeline default)
    "bin_size_minutes": 10.0,
    "target_id": "",           # "" -> auto
    "comparison_ids": "",      # "" -> auto, or "1,2,3"
    "avoid_comparison_ids": "",  # "" -> none; "1,2,3" -> --avoid_cids (requires --ref_band)
    "target_coord": "",        # "" -> resolve via MAST; "RA Dec" -> bypass name resolution
    "gif_stride": 100,
    "overwrite": True,
    "sig_bkg": None,           # None -> sigma clipping disabled for bkg axis
    "sig_fwhm": None,          # None -> sigma clipping disabled for fwhm axis
    "sig_dx": None,            # None -> sigma clipping disabled for dx axis
    "sig_dy": None,            # None -> sigma clipping disabled for dy axis
    "min_star_area": 10,
}

# Narrow-band tokens, kept after the broadband four in the canonical order.
NARROW_BANDS = ["g_narrow", "Na_D", "i_narrow", "z_narrow"]

# Raw obslog FILTER value -> prose `--bands` token. Mirrors prose's
# ``prose/utils.py:_FILTER_ALIASES`` (the source of truth); kept in sync here
# because the web process cannot import prose (it runs only in the "prose"
# conda env via subprocess). Unknown filters (e.g. Sinistro R/V/B) are not
# listed and pass through unchanged — run_photometry's ``_resolve_band`` falls
# back to the raw value, so ``--bands R V`` works for those frames.
_FILTER_BAND_ALIAS = {
    "gp": "gp", "g": "gp",
    "rp": "rp", "r": "rp", "rp*diffuser": "rp",
    "ip": "ip", "i": "ip",
    "zs": "zs", "z": "zs", "zp": "zs", "z_s": "zs", "zp*diffuser": "zs",
    "g_narrow": "g_narrow", "r_narrow": "r_narrow",
    "i_narrow": "i_narrow", "z_narrow": "z_narrow",
    "g_wide": "g_wide", "Na_D": "Na_D",
}


def bands_from_filters(filters: list[str]) -> list[str]:
    """Map raw obslog FILTER values to ordered, de-duplicated ``--bands`` tokens.

    Each raw filter is normalized via :data:`_FILTER_BAND_ALIAS`; unknown values
    (e.g. Sinistro ``R``/``V``/``B``) pass through unchanged. The result is
    ordered canonically — broadband (gp, rp, ip, zs), then narrowbands, then any
    extras in first-seen order — so the UI shows a stable, familiar layout.
    Returns ``[]`` for empty input.
    """
    seen: set[str] = set()
    tokens: list[str] = []
    for f in filters or []:
        if not f:
            continue
        # Exact match only, like prose's _resolve_band: do NOT case-fold, or
        # Johnson "R"/"V" would collapse into Sloan "rp"/etc.
        token = _FILTER_BAND_ALIAS.get(f, f)
        if token not in seen:
            seen.add(token)
            tokens.append(token)
    order = {b: i for i, b in enumerate([*DEFAULT_BANDS, *NARROW_BANDS])}
    return sorted(tokens, key=lambda b: (order.get(b, len(order)), tokens.index(b)))


ALLOWED_EXTS = {".png", ".gif", ".csv", ".npz", ".log"}
_RUN_LOG_NAME = "_webrun.log"
_CONDA_ENV_DEFAULT = "prose"   # prose deps live in a conda env named "prose"
_MODULE = "prose.scripts.run_photometry"

_DATE_RE = re.compile(r"^\d{6}$")
# A served filename is a single path segment of safe characters only.
_NAME_RE = re.compile(r"^[A-Za-z0-9._+-]+$")

# Summary (multi-band) plot suffixes -> short key used by the template.
_SUMMARY_SUFFIX = {
    "_lightcurves.png": "lightcurves",
    "_raw_flux.png": "raw_flux",
    "_covariates.png": "covariates",
    "_systematics.png": "covariates",   # backward compat with old pipeline
    "_stacks.png": "stacks",
}
# Per-band product suffixes -> short key.
_BAND_SUFFIX = {
    "_ref.png": "ref",
    "_apertures.png": "apertures",
    "_alignment.png": "alignment",
    ".gif": "gif",
    ".csv": "csv",
}


def output_base() -> Path:
    return Path(os.environ.get("MUSCAT_PROSE_DIR", _DEFAULT_OUTPUT_BASE))


def prose_project_dir() -> Path:
    return Path(os.environ.get("MUSCAT_PROSE_PROJECT", str(_DEFAULT_PROSE_PROJECT)))


def prose_python() -> str | None:
    """Explicit interpreter for prose, if configured (highest priority)."""
    return os.environ.get("MUSCAT_PROSE_PYTHON") or None


def prose_conda_env() -> str:
    """Name of the conda env that supplies prose's dependencies."""
    return os.environ.get("MUSCAT_PROSE_CONDA_ENV", _CONDA_ENV_DEFAULT)


def _conda_env_python(env: str) -> str | None:
    """Resolve ``envs/<env>/bin/python`` from the active conda install or the
    usual install locations. Returns the interpreter path, or ``None``."""
    bases: list[Path] = []
    exe = os.environ.get("CONDA_EXE")
    if exe:
        # .../miniconda3/bin/conda -> .../miniconda3
        bases.append(Path(exe).resolve().parent.parent)
    home = Path.home()
    bases += [
        home / "miniconda3", home / "anaconda3", home / "miniforge3",
        home / ".conda", Path("/opt/conda"),
    ]
    seen: set[Path] = set()
    for base in bases:
        if base in seen:
            continue
        seen.add(base)
        cand = base / "envs" / env / "bin" / "python"
        if cand.is_file():
            return str(cand)
    return None


def _prose_prefix() -> list[str]:
    """Resolve how to invoke the prose pipeline module, most robust first.

    The local prose source (``prose_project_dir``) is put on ``sys.path`` by
    running with that directory as cwd (see ``start_run``); the interpreter
    only needs prose's dependencies, which live in the conda env.
    """
    explicit = prose_python()
    if explicit:
        return [explicit, "-m", _MODULE]
    env = prose_conda_env()
    conda_py = _conda_env_python(env)
    if conda_py:
        return [conda_py, "-m", _MODULE]
    if shutil.which("conda"):
        return ["conda", "run", "-n", env, "--no-capture-output",
                "python", "-m", _MODULE]
    # Last resort: let uv resolve an interpreter from the project directory.
    return ["uv", "run", "--project", str(prose_project_dir()),
            "python", "-m", _MODULE]


def valid_date(date: str) -> bool:
    return bool(_DATE_RE.match(date or ""))


def results_dir(inst: str, date: str) -> Path:
    return output_base() / inst / date


def raw_data_dir(inst: str, date: str) -> Path:
    base = os.environ.get("MUSCAT_DATA_DIR")
    if not base:
        cfg = INSTRUMENTS.get(inst)
        base = cfg.data_dir if cfg is not None else f"/data/{inst}"
    return Path(base) / date


def _stem(target: str, inst: str, date: str, band: str | None = None) -> str:
    """Mirror prose ``build_stem`` (spaces stripped from the target)."""
    t = target.replace(" ", "")
    return f"{t}_{inst}_{band}_{date}" if band else f"{t}_{inst}_{date}"


# --------------------------- output discovery ---------------------------


def output_dates(inst: str) -> list[str]:
    """6-digit date dirs that already have a prose output folder, newest first."""
    base = output_base() / inst
    if not base.is_dir():
        return []
    return sorted(
        (p.name for p in base.iterdir() if p.is_dir() and _DATE_RE.match(p.name)),
        reverse=True,
    )


def discovered_targets(inst: str, date: str) -> list[str]:
    """Target names inferred from product filenames already in the output dir.

    The pipeline embeds the date from the FITS header into each filename, which
    may differ from the directory name (obs-night vs UT date). We therefore
    match on the inst token only and accept any 6-digit date token.
    """
    rdir = results_dir(inst, date)
    if not rdir.is_dir():
        return []
    # Match: <target>_<inst>_[<band>_]<6digits>[._...]
    pat = re.compile(
        rf"^(?P<t>.+?)_{re.escape(inst)}_(?:[A-Za-z0-9_]+_)?\d{{6}}(?:[._]|$)"
    )
    found: set[str] = set()
    for p in rdir.iterdir():
        if not p.is_file() or p.suffix == ".log":
            continue
        m = pat.match(p.name)
        if m:
            found.add(m.group("t"))
    return sorted(found)


def list_outputs(inst: str, date: str, target: str) -> dict:
    """Classify the existing products for one (inst, date, target).

    Returns a dict with ``summary`` (key->filename), ``bands``
    (band->{ref,apertures,alignment,gif,csv}), ``npz``, ``log`` (newest), and
    ``has_any``. Only filenames are returned; serve them via the file route.

    The date token embedded in filenames by the pipeline is taken from the FITS
    header and may differ from the directory name (obs-night vs UT date). We
    therefore build regexes that accept any 6-digit date token instead of
    requiring an exact match against the passed-in ``date``.
    """
    out: dict = {
        "summary": {},
        "bands": {},
        "npz": None,
        "log": None,
        "has_any": False,
        "masters": [],
    }
    rdir = results_dir(inst, date)
    if not rdir.is_dir():
        return out

    t = target.replace(" ", "")
    inst_esc = re.escape(inst)
    t_esc = re.escape(t)

    # Multi-band summary stem: <target>_<inst>_<date6>  (no band token)
    # Allow any 6-digit date so obs-night and UT-date both match.
    summary_re = re.compile(
        rf"^{t_esc}_{inst_esc}_(?P<file_date>\d{{6}})(?P<rest>.*)$"
    )
    # Per-band stem: <target>_<inst>_<band>_<date6>
    band_re = re.compile(
        rf"^{t_esc}_{inst_esc}_(?P<band>[A-Za-z0-9]+)_(?P<file_date>\d{{6}})(?P<rest>.*)$"
    )
    logs: list[Path] = []

    for p in sorted(rdir.iterdir()):
        if not p.is_file():
            continue
        name = p.name
        if p.suffix == ".log":
            logs.append(p)
            continue

        try:
            mtime = p.stat().st_mtime
            created_at = datetime.datetime.fromtimestamp(mtime).strftime('%Y-%m-%d %H:%M')
        except Exception:
            created_at = "Unknown"

        # Try summary suffixes first (no band token between inst and date).
        ms = summary_re.match(name)
        if ms:
            rest = ms.group("rest")
            if rest == ".npz":
                existing = out.get("npz")
                if existing is None or mtime > out.get("_npz_mtime", 0):
                    out["npz"] = name
                    out["_npz_mtime"] = mtime
                out["has_any"] = True
                continue
            key = _SUMMARY_SUFFIX.get(rest)
            if key is not None:
                existing = out["summary"].get(key)
                if existing is None or mtime > existing.get("_mtime", 0):
                    out["summary"][key] = {"file": name, "created_at": created_at, "_mtime": mtime}
                    out["has_any"] = True
                continue
            # If the summary regex matched but rest is unrecognised, fall
            # through to the band regex (it is more specific).

        mb = band_re.match(name)
        if not mb:
            continue
        rest = mb.group("rest")
        key = _BAND_SUFFIX.get(rest)
        if key is None:
            continue
        band = mb.group("band")
        existing = out["bands"].setdefault(band, {}).get(key)
        if existing is None or mtime > existing.get("_mtime", 0):
            out["bands"][band][key] = {"file": name, "created_at": created_at, "_mtime": mtime}
            out["has_any"] = True

    if inst in ("muscat", "muscat2"):
        try:
            cal_dir = Path(str(raw_data_dir(inst, date)) + "_calibrated")
            for p in sorted(cal_dir.glob("master_*.png")):
                if p.is_file():
                    out["masters"].append(p.name)
        except OSError:
            pass

    if logs:
        out["log"] = max(logs, key=lambda p: p.stat().st_mtime).name

    # Strip internal keys and order bands canonically (gp, rp, ip, zs)
    for d in out["summary"].values():
        d.pop("_mtime", None)
    for band_d in out["bands"].values():
        for d in band_d.values():
            d.pop("_mtime", None)
    out.pop("_npz_mtime", None)
    ordered = {b: out["bands"][b] for b in DEFAULT_BANDS if b in out["bands"]}
    for b, v in out["bands"].items():
        ordered.setdefault(b, v)
    out["bands"] = ordered
    return out


def csv_preview(path: Path, n: int = 8) -> tuple[list[str], list[list[str]]]:
    """Header row + first ``n`` data rows of a light-curve CSV."""
    try:
        with open(path, newline="") as f:
            reader = _csv.reader(f)
            headers = next(reader, [])
            rows = []
            for i, row in enumerate(reader):
                if i >= n:
                    break
                rows.append(row)
        return headers, rows
    except OSError:
        return [], []


# --------------------------- safe file serving ---------------------------


def safe_artifact_path(inst: str, date: str, name: str) -> Path | None:
    """Resolve a served filename to a real file, or ``None`` if anything about
    the request is unsafe (bad instrument/date/name, traversal, wrong ext)."""
    if inst not in INSTRUMENTS or not valid_date(date):
        return None
    if ".." in name or "/" in name or not _NAME_RE.match(name):
        return None
    if Path(name).suffix.lower() not in ALLOWED_EXTS:
        return None

    # For muscat/muscat2 master images, they live in <raw_data_dir>_calibrated
    if inst in ("muscat", "muscat2") and name.startswith("master_") and name.endswith(".png"):
        raw_dir = raw_data_dir(inst, date)
        cal_dir = Path(str(raw_dir) + "_calibrated").resolve()
        candidate = (cal_dir / name).resolve()
        try:
            candidate.relative_to(cal_dir)
        except ValueError:
            return None
        return candidate if candidate.is_file() else None

    base = output_base().resolve()
    candidate = (base / inst / date / name).resolve()
    try:
        candidate.relative_to(base)
    except ValueError:
        return None
    return candidate if candidate.is_file() else None


_phot_status_cache = register_cache(ttl=300.0)


def get_photometry_status(inst: str, date: str, target: str) -> str:
    """Determine the status of photometry for a target: none, test, or full.

    Uses the results-directory mtime as the cache key so the result is
    automatically invalidated whenever files are created or removed there,
    while still benefiting from the global ``clear_all_caches()`` call that
    fires after every ``build-db`` run.
    """
    rdir = results_dir(inst, date)
    try:
        mtime = rdir.stat().st_mtime
    except OSError:
        return "none"

    return _get_status_mtime(inst, date, target, mtime)


@_phot_status_cache
def _get_status_mtime(inst: str, date: str, target: str, mtime: float) -> str:
    """Inner cached worker keyed on (inst, date, target, mtime)."""
    rdir = results_dir(inst, date)
    return _calculate_photometry_status(inst, date, target, rdir)


def _calculate_photometry_status(inst: str, date: str, target: str, rdir: Path) -> str:
    out = list_outputs(inst, date, target)
    if not out.get("has_any"):
        return "none"

    # Fallback/Optimization: Check CSV files. If any CSV has more than 15 lines, it is a full run.
    csv_found = False
    max_rows = 0
    for band_data in out.get("bands", {}).values():
        csv_info = band_data.get("csv")
        if csv_info:
            csv_path = rdir / csv_info["file"]
            if csv_path.is_file():
                csv_found = True
                try:
                    with open(csv_path, errors="replace") as f:
                        row_count = sum(1 for _ in f)
                        if row_count > max_rows:
                            max_rows = row_count
                except Exception:
                    pass
    if csv_found and max_rows > 15:
        return "full"

    # Check log files to distinguish test vs full
    log_files = list(rdir.glob("*.log"))
    if (rdir / _RUN_LOG_NAME).is_file():
        log_files.append(rdir / _RUN_LOG_NAME)

    has_target_log = False
    has_full_log = False
    target_clean = target.replace(" ", "")

    for lf in log_files:
        try:
            content = lf.read_text(errors="replace")
            if target in content or target_clean in content:
                has_target_log = True
                # If the log doesn't indicate a test run, then it's a full run
                if "--test_run" not in content and "--test-run" not in content and "test-run:" not in content:
                    has_full_log = True
                    break
        except Exception:
            pass

    if has_target_log:
        return "full" if has_full_log else "test"

    return "full"


# --------------------------- command building ---------------------------


def _to_int(v) -> int | None:
    try:
        return int(float(str(v).strip()))
    except (TypeError, ValueError):
        return None


def _to_float(v) -> float | None:
    try:
        return float(str(v).strip())
    except (TypeError, ValueError):
        return None


def _to_bool(v) -> bool:
    if isinstance(v, bool):
        return v
    if v is None:
        return False
    return str(v).strip().lower() in ("1", "true", "on", "yes")


_TRIPLE_RE = re.compile(r"^-?\d+(\.\d+)?,-?\d+(\.\d+)?,-?\d+(\.\d+)?$")
_PAIR_RE = re.compile(r"^-?\d+(\.\d+)?,-?\d+(\.\d+)?$")
_INTPAIR_RE = re.compile(r"^\d+,\d+$")
_BAND_RE = re.compile(r"^[A-Za-z0-9_]+$")


def normalize_run_options(raw: dict | None) -> dict:
    """Coerce a raw form/JSON dict into typed run options merged over defaults.

    Mirrors quicklook's ``_parse_params``: empty strings keep the pipeline
    default (omitted from the command), numbers are coerced, checkboxes become
    booleans. Unknown keys are ignored.
    """
    raw = raw or {}
    o = dict(RUN_DEFAULTS)

    bands = raw.get("bands")
    if isinstance(bands, str):
        bands = [b for b in re.split(r"[,\s]+", bands) if b]
    if "bands" in raw:  # present-but-empty must surface as an error, not default
        o["bands"] = [str(b).strip() for b in (bands or []) if str(b).strip()]

    for key in ("ref_band", "aper_radii", "annulus", "aper_unit", "ccd_trim", "target_id", "comparison_ids", "avoid_comparison_ids", "target_coord"):
        if raw.get(key) is not None:
            o[key] = str(raw[key]).strip()

    for key in ("refid", "n_stars_align"):  # optional ints; "" keeps default
        if key in raw:
            val = str(raw.get(key, "")).strip()
            o[key] = "" if val == "" else (_to_int(val) if _to_int(val) is not None else "")

    for key in ("test_run_frames", "max_num_stars", "cutout_size", "gif_stride", "min_star_area"):
        if str(raw.get(key, "")).strip() != "":
            iv = _to_int(raw[key])
            if iv is not None:
                o[key] = iv

    for key in ("min_star_separation", "bin_size_minutes", "sig_bkg", "sig_fwhm", "sig_dx", "sig_dy"):
        if str(raw.get(key, "")).strip() != "":
            fv = _to_float(raw[key])
            if fv is not None:
                o[key] = fv

    for key in ("make_gif", "plot_gaia_sources", "use_barycorrpy", "overwrite"):
        if key in raw:
            o[key] = _to_bool(raw[key])

    return o


def validate_run_options(o: dict) -> str | None:
    """Return a user-facing error string for invalid options, else ``None``."""
    if not o.get("bands"):
        return "select at least one band"
    if any(not _BAND_RE.match(b) for b in o["bands"]):
        return "band names may only contain letters, digits and underscores"
    ar = (o.get("aper_radii") or "").replace(" ", "")
    an = (o.get("annulus") or "").replace(" ", "")
    if ar and not _TRIPLE_RE.match(ar):
        return "aperture radii must be MIN,MAX,DR (e.g. 10,20,2)"
    if an and not _PAIR_RE.match(an):
        return "annulus must be RIN,ROUT (e.g. 25,40)"
    if ar and not an:
        return "annulus (RIN,ROUT) is required when aperture radii is set"
    if an and not ar:
        return "aperture radii (MIN,MAX,DR) is required when annulus is set"
    ct = (o.get("ccd_trim") or "").replace(" ", "")
    if ct and not _INTPAIR_RE.match(ct):
        return "CCD trim must be two integers Y,X (e.g. 10,10)"
    if o.get("aper_unit", "pix") not in ("pix", "fwhm"):
        return "aperture unit must be 'pix' or 'fwhm'"
    return None


def build_command(
    inst: str,
    date: str,
    target: str,
    options: dict | None = None,
    *,
    test_run: bool = True,
) -> list[str]:
    """argv for a prose reduction, including any non-default options.

    ``--overwrite`` (on by default) lets a new target coexist with others
    already reduced in the same inst/date directory.
    """
    o = normalize_run_options(options)
    args = [
        *_prose_prefix(),
        "--target_name", target,
        "--data_dir", str(raw_data_dir(inst, date)),
        "--results_dir", str(results_dir(inst, date)),
        "--bands", *o["bands"],
    ]
    if o.get("ref_band"):
        args += ["--ref_band", o["ref_band"]]
    if o.get("refid") not in (None, ""):
        args += ["--refid", str(o["refid"])]

    ar = (o.get("aper_radii") or "").replace(" ", "")
    an = (o.get("annulus") or "").replace(" ", "")
    if ar:
        args += ["--aper_radii", ar]
    if an:
        args += ["--annulus", an]
    if ar and o.get("aper_unit", "pix") != "pix":
        args += ["--aper_unit", o["aper_unit"]]

    if o.get("target_coord") not in (None, ""):
        parts = o["target_coord"].split(None, 1)
        if len(parts) == 2:
            args += ["--target_coord", *parts]
    if o.get("target_id") not in (None, ""):
        args += ["--tID", o["target_id"]]
    if o.get("comparison_ids") not in (None, ""):
        cids = [c.strip() for c in o["comparison_ids"].split(",") if c.strip()]
        if cids:
            args += ["--cID", *cids]
    if o.get("avoid_comparison_ids") not in (None, ""):
        aids = [a.strip() for a in o["avoid_comparison_ids"].split(",") if a.strip()]
        if aids:
            args += ["--avoid_cids", *aids]

    # Numeric overrides: only emit when the user changed them from the default.
    for flag, key in (
        ("--min_star_separation", "min_star_separation"),
        ("--max_num_stars", "max_num_stars"),
        ("--cutout_size", "cutout_size"),
        ("--bin_size_minutes", "bin_size_minutes"),
        ("--gif_stride", "gif_stride"),
        ("--sig_bkg", "sig_bkg"),
        ("--sig_fwhm", "sig_fwhm"),
        ("--sig_dx", "sig_dx"),
        ("--sig_dy", "sig_dy"),
        ("--min_star_area", "min_star_area"),
    ):
        val = o.get(key)
        if val in (None, ""):
            continue
        default = RUN_DEFAULTS.get(key)
        if default is None or float(val) != float(default):
            args += [flag, str(val)]
    if o.get("n_stars_align") not in (None, ""):
        args += ["--n_stars_align", str(o["n_stars_align"])]
    if (o.get("ccd_trim") or "").replace(" ", ""):
        args += ["--ccd_trim", o["ccd_trim"].replace(" ", "")]

    if o.get("make_gif", False):
        args.append("--gif")
    if o.get("plot_gaia_sources", True):
        args.append("--plot_gaia_sources")
    if o.get("use_barycorrpy"):
        args.append("--use_barycorrpy")

    args.append("--verbose")
    if test_run:
        args.append("--test_run")
        v = o.get("test_run_frames")
        if v not in (None, "") and v != RUN_DEFAULTS.get("test_run_frames"):
            args += ["--test_run_frames", str(v)]
    if o.get("overwrite", True):
        args.append("--overwrite")
    return args


def command_str(
    inst: str,
    date: str,
    target: str,
    options: dict | None = None,
    *,
    test_run: bool = False,
) -> str:
    return shlex.join(build_command(inst, date, target, options, test_run=test_run))


# --------------------------- background job runner ---------------------------


@dataclass
class Job:
    key: str
    inst: str
    date: str
    target: str
    cmd: list[str]
    proc: subprocess.Popen
    logf: IO
    log_path: Path
    started_at: float = field(default_factory=time.time)
    state: str = "running"  # running | done | error | cancelled
    returncode: int | None = None
    cancelled: bool = False
    elapsed: int | None = None
    run_type: str = "full"  # "test" | "full"


_JOBS: dict[str, Job] = {}
_LOCK = threading.Lock()
_MAX_FULL_JOBS = 1

# Watchdog limits for hung reductions. A healthy run writes to its log
# continuously, so a long silence means it has stalled; the absolute cap is a
# backstop. Both are env-tunable. Observed legitimate full runs finish in <35 min.
_STALL_LIMIT_S = int(os.environ.get("MUSCAT_PHOT_STALL_LIMIT_S", 25 * 60))
_MAX_RUNTIME_S = int(os.environ.get("MUSCAT_PHOT_MAX_RUNTIME_S", 3 * 60 * 60))


def _count_running_full() -> int:
    """Number of currently-running full (non-test) photometry jobs."""
    return sum(1 for j in _JOBS.values() if j.run_type == "full" and j.proc.poll() is None)


def job_key(inst: str, date: str, target: str) -> str:
    return f"{inst}/{date}/{target.replace(' ', '')}"


def _run_log_path(rdir: Path, inst: str, date: str, target: str) -> Path:
    """Return a deterministic, target-specific web-run log path."""
    digest = hashlib.sha256(job_key(inst, date, target).encode()).hexdigest()[:16]
    return rdir / f"_webrun_{digest}.log"


def log_path(inst: str, date: str, target: str) -> Path | None:
    rdir = results_dir(inst, date) if results_dir(inst, date) else None
    if rdir is None:
        return None
    p = _run_log_path(rdir, inst, date, target)
    return p if p.is_file() else None


def start_run(
    inst: str,
    date: str,
    target: str,
    options: dict | None = None,
    test_run: bool = True,
) -> dict:
    """Launch a reduction in the background. Returns ``{ok, key}`` or
    ``{ok: False, error}``. A run already in flight for the same key is reused.
    """
    if inst not in INSTRUMENTS:
        return {"ok": False, "error": f"unknown instrument {inst!r}"}
    if not valid_date(date):
        return {"ok": False, "error": "date must be 6-digit yymmdd"}
    if not (target or "").strip():
        return {"ok": False, "error": "target is required"}
    opts = normalize_run_options(options)
    err = validate_run_options(opts)
    if err:
        return {"ok": False, "error": err}
    rawdir = raw_data_dir(inst, date)
    if not rawdir.is_dir():
        return {"ok": False, "error": f"raw data not found: {rawdir}"}

    key = job_key(inst, date, target)
    with _LOCK:
        existing = _JOBS.get(key)
        if existing is not None and existing.proc.poll() is None:
            return {"ok": True, "key": key, "already_running": True}

        run_type = "test" if test_run else "full"

        # Queue full jobs when at capacity
        if run_type == "full" and _count_running_full() >= _MAX_FULL_JOBS:
            from muscat_db.database import save_job
            try:
                save_job(
                    type_="photometry",
                    inst=inst, date=date, target=target,
                    state="pending",
                    returncode=None, elapsed=0,
                    started_at=time.time(),
                    run_type=run_type,
                    params=json.dumps({"test_run": test_run, "options": opts}, separators=(",", ":"))
                )
            except sqlite3.OperationalError as exc:
                return {"ok": False, "error": f"database not writable: {exc}"}
            return {"ok": True, "key": key, "queued": True}

        rdir = results_dir(inst, date)
        rdir.mkdir(parents=True, exist_ok=True)
        cmd = build_command(inst, date, target, opts, test_run=test_run)
        log_path = _run_log_path(rdir, inst, date, target)
        logf = open(log_path, "w")
        logf.write(f"$ {shlex.join(cmd)}\n\n")
        logf.flush()
        try:
            env = {**os.environ, "PYTHONUNBUFFERED": "1"}
            proc = subprocess.Popen(
                cmd,
                cwd=str(prose_project_dir()),
                stdout=logf,
                stderr=subprocess.STDOUT,
                text=True,
                # Own session/process group so Cancel can kill the whole tree
                # (prose spawns multiprocessing workers via SequenceParallel).
                start_new_session=True,
                env=env,
            )
        except (FileNotFoundError, OSError) as exc:
            logf.write(f"\nfailed to launch pipeline: {exc}\n")
            logf.close()
            return {"ok": False, "error": f"failed to launch pipeline: {exc}"}
        _JOBS[key] = Job(
            key=key, inst=inst, date=date, target=target,
            cmd=cmd, proc=proc, logf=logf, log_path=log_path,
            run_type=run_type,
        )
        # Record new job in the database
        from muscat_db.database import save_job
        try:
                save_job(
                type_="photometry",
                inst=inst,
                date=date,
                target=target,
                state="running",
                returncode=None,
                elapsed=0,
                started_at=_JOBS[key].started_at,
                run_type=run_type,
                params=json.dumps({"test_run": test_run, "options": opts}, separators=(",", ":"))
            )
        except sqlite3.OperationalError as exc:
            # DB write failed (e.g. read-only database). Roll back the launched
            # process and job so we don't leak a running pipeline we can't track.
            try:
                proc.terminate()
            except OSError:
                pass
            try:
                logf.close()
            except OSError:
                pass
            _JOBS.pop(key, None)
            return {"ok": False, "error": f"database not writable: {exc}"}
    return {"ok": True, "key": key}


def _tail(path: Path, n: int = 200) -> str:
    if not path.is_file():
        return ""
    try:
        with open(path, errors="replace") as f:
            return "".join(deque(f, maxlen=n))
    except OSError:
        return ""


def _log_has_partial_failure(path: Path | None) -> bool:
    if path is None or not path.is_file():
        return False
    return "photometry PARTIAL FAILURE" in _tail(path, n=1000)


def _terminal_job_state(
    returncode: int,
    cancelled: bool,
    log_path_: Path | None,
) -> str:
    """Map process completion to state, treating logged partial runs as errors."""
    if cancelled:
        return "cancelled"
    if returncode != 0:
        return "error"
    if _log_has_partial_failure(log_path_):
        return "error"
    return "done"


def _pending_status(inst: str, date: str, target: str) -> dict | None:
    """Return a queued-job status dict if a pending DB entry exists, else None.

    A full run launched while the single full-job slot is occupied is recorded
    in the DB as ``pending`` but not added to ``_JOBS``; surface that here so the
    photometry page can show a "queued" state instead of silently resetting.
    """
    from muscat_db.database import get_persisted_jobs

    db_key = f"photometry:{job_key(inst, date, target)}"
    try:
        for entry in get_persisted_jobs():
            if (
                entry["key"] == db_key
                and entry["type"] == "photometry"
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
        pass
    return None


def _persisted_status(inst: str, date: str, target: str) -> dict | None:
    """Return a terminal-state status dict from the DB for a job no longer in
    ``_JOBS``.

    A run can leave ``_JOBS`` while still ending in error/cancelled/done: the
    watchdog kills hung runs and pops them, and a server restart loses the
    in-memory job entirely. Without this fallback ``job_status`` returns
    ``"none"``, and the page silently freezes the log instead of showing the
    failure. Surface the persisted outcome (plus its log tail and error
    description) so the final state is never lost.
    """
    from muscat_db.database import get_persisted_jobs

    db_key = f"photometry:{job_key(inst, date, target)}"
    try:
        for entry in get_persisted_jobs():  # newest-first; one row per key
            if entry["key"] != db_key or entry["type"] != "photometry":
                continue
            state = entry["state"]
            if state not in ("done", "error", "cancelled"):
                return None  # running/pending handled by the caller
            lp = log_path(inst, date, target)
            error_desc = entry.get("error_desc") or ""
            if state == "error" and not error_desc and lp:
                error_desc = _get_error_desc(lp)
            elif state == "cancelled" and not error_desc:
                error_desc = "Cancelled by user"
            return {
                "state": state,
                "returncode": entry.get("returncode"),
                "log": _tail(lp) if lp else "",
                "elapsed": round(entry.get("elapsed") or 0),
                "error_desc": error_desc,
            }
    except Exception:
        pass
    return None


def job_status(inst: str, date: str, target: str) -> dict:
    """Poll a job and return its state plus its target-specific log tail.

    A zero exit status is still an error when the pipeline logged
    ``photometry PARTIAL FAILURE``.
    """
    key = job_key(inst, date, target)
    with _LOCK:
        job = _JOBS.get(key)
        if job is None:
            pending = _pending_status(inst, date, target)
            if pending is not None:
                return pending
            persisted = _persisted_status(inst, date, target)
            if persisted is not None:
                return persisted
            return {"state": "none", "log": "", "returncode": None, "elapsed": 0}
        rc = job.proc.poll()
        if rc is None:
            state = "cancelling" if job.cancelled else "running"
        else:
            state = _terminal_job_state(rc, job.cancelled, job.log_path)
            if job.state in ("running",):
                job.state = state
                job.returncode = rc
                job.elapsed = round(time.time() - job.started_at)
                try:
                    job.logf.close()
                except OSError:
                    pass
        log_path = job.log_path
        elapsed = job.elapsed if job.state not in ("running", "cancelling") and job.elapsed is not None else round(time.time() - job.started_at)
    error_desc = ""
    if state == "error" and log_path:
        error_desc = _get_error_desc(log_path)
    elif state == "cancelled":
        error_desc = "Cancelled by user"
    return {
        "state": state,
        "returncode": rc,
        "log": _tail(log_path),
        "elapsed": round(elapsed),
        "error_desc": error_desc,
    }


def get_all_jobs() -> list[dict]:
    """Retrieve all background jobs, polling/updating their state."""
    with _LOCK:
        res = []
        for key, job in _JOBS.items():
            rc = job.proc.poll()
            if rc is None:
                state = "cancelling" if job.cancelled else "running"
            else:
                state = _terminal_job_state(rc, job.cancelled, job.log_path)
                if job.state in ("running",):
                    job.state = state
                    job.returncode = rc
                    job.elapsed = round(time.time() - job.started_at)
                    try:
                        job.logf.close()
                    except OSError:
                        pass
            
            elapsed = job.elapsed if job.state not in ("running", "cancelling") and job.elapsed is not None else round(time.time() - job.started_at)
            res.append({
                "key": job.key,
                "inst": job.inst,
                "date": job.date,
                "target": job.target,
                "state": state,
                "returncode": rc,
                "elapsed": round(elapsed),
                "started_at": job.started_at,
            })
        return sorted(res, key=lambda j: j["started_at"], reverse=True)


def _kill_after(proc: subprocess.Popen, grace: float = 6.0) -> None:
    """Escalate to SIGKILL on the process group if SIGTERM was ignored."""
    try:
        proc.wait(timeout=grace)
        return
    except subprocess.TimeoutExpired:
        pass
    try:
        os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
    except (ProcessLookupError, PermissionError, OSError):
        try:
            proc.kill()
        except OSError:
            pass


def _terminate_pg(proc: subprocess.Popen) -> None:
    """SIGTERM a job's whole process group, escalating to SIGKILL in the background."""
    try:
        os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
    except (ProcessLookupError, PermissionError, OSError):
        try:
            proc.terminate()
        except OSError:
            pass
    threading.Thread(target=_kill_after, args=(proc,), daemon=True).start()


def _watchdog_breach(job: "Job", now: float) -> str | None:
    """Return a kill reason if a running job looks hung, else None."""
    if now - job.started_at > _MAX_RUNTIME_S:
        return f"exceeded max runtime ({_MAX_RUNTIME_S // 3600}h)"
    try:
        mtime = job.log_path.stat().st_mtime
    except OSError:
        mtime = job.started_at
    stall = now - mtime
    if stall > _STALL_LIMIT_S:
        return f"no log output for {int(stall // 60)} min"
    return None


def cancel_run(inst: str, date: str, target: str) -> dict:
    """Cancel a running or pending reduction. Sends SIGTERM to the job's process group
    and escalates to SIGKILL after a short grace period."""
    key = job_key(inst, date, target)
    with _LOCK:
        from muscat_db.database import save_job, get_persisted_jobs
        job = _JOBS.get(key)
        if job is None:
            # May be a pending job (in DB but not yet launched)
            db_jobs = get_persisted_jobs()
            db_key = f"photometry:{key}"
            found = [j for j in db_jobs if j["key"] == db_key]
            if found and found[0]["state"] == "pending":
                save_job(
                    type_="photometry", inst=inst, date=date, target=target,
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
            type_="photometry",
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
    except (ProcessLookupError, PermissionError, OSError):
        try:
            proc.terminate()
        except OSError:
            pass
    threading.Thread(target=_kill_after, args=(proc,), daemon=True).start()
    return {"ok": True, "key": key}


def _get_error_desc(log_path: Path) -> str:
    if not log_path.is_file():
        return "Unknown error"
    try:
        with open(log_path, errors="replace") as f:
            lines = [line.strip() for line in f if line.strip()]
        if not lines:
            return "Empty log file"
        for line in reversed(lines):
            if "Error" in line or "ERROR" in line or "Exception" in line or "failed" in line:
                # Remove ANSI escape codes
                clean = re.sub(r'\x1b\[[0-9;]*m', '', line)
                if " - ERROR: " in clean:
                    clean = clean.split(" - ERROR: ")[1]
                elif " - WARNING: " in clean:
                    clean = clean.split(" - WARNING: ")[1]
                return clean[:100]
        return re.sub(r'\x1b\[[0-9;]*m', '', lines[-1])[:100]
    except Exception:
        return "Failed to parse log"


def sync_jobs() -> None:
    from muscat_db.database import save_job, get_persisted_jobs
    with _LOCK:
        # Watchdog: kill runs that have hung (no log output, or past the absolute
        # cap) and record them as errors. This frees the single full-job slot so the
        # queue-drain below can promote a pending job in the same pass.
        now = time.time()
        for key in list(_JOBS.keys()):
            job = _JOBS[key]
            if job.cancelled or job.state != "running" or job.proc.poll() is not None:
                continue
            reason = _watchdog_breach(job, now)
            if reason is None:
                continue
            _terminate_pg(job.proc)
            try:
                job.logf.close()
            except OSError:
                pass
            save_job(
                type_="photometry", inst=job.inst, date=job.date, target=job.target,
                state="error", returncode=-1,
                elapsed=round(now - job.started_at), started_at=job.started_at,
                error_desc=f"watchdog: {reason}", run_type=job.run_type,
            )
            _JOBS.pop(key, None)

        db_jobs = get_persisted_jobs()
        for entry in db_jobs:
            if entry["type"] != "photometry" or entry["state"] != "done":
                continue
            entry_log_path = log_path(entry["inst"], entry["date"], entry["target"])
            if not _log_has_partial_failure(entry_log_path):
                continue
            save_job(
                type_="photometry",
                inst=entry["inst"],
                date=entry["date"],
                target=entry["target"],
                state="error",
                returncode=entry.get("returncode"),
                elapsed=entry.get("elapsed") or 0,
                started_at=entry.get("started_at") or time.time(),
                error_desc=_get_error_desc(entry_log_path) if entry_log_path else "Partial failure",
                run_type=entry.get("run_type") or "",
                params=entry.get("params") or "",
            )

        running_keys = {j["key"] for j in db_jobs if j["state"] == "running" and j["type"] == "photometry"}
        
        for key, job in _JOBS.items():
            db_key = f"photometry:{job.inst}/{job.date}/{job.target.replace(' ', '')}"
            rc = job.proc.poll()
            if rc is None:
                state = "cancelling" if job.cancelled else "running"
            else:
                state = _terminal_job_state(rc, job.cancelled, job.log_path)
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
                error_desc = _get_error_desc(job.log_path)
            elif state == "cancelled":
                error_desc = "Cancelled by user"
            
            save_job(
                type_="photometry",
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
                type_="photometry",
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
            pending = [j for j in db_jobs if j["state"] == "pending" and j["type"] == "photometry"]
            pending.sort(key=lambda j: j["started_at"])
            for entry in pending:
                if _count_running_full() >= _MAX_FULL_JOBS:
                    break
                if entry["key"] in _JOBS:
                    save_job(type_="photometry", inst=entry["inst"], date=entry["date"], target=entry["target"], state="error", returncode=-1, elapsed=0, started_at=entry["started_at"], error_desc="Duplicate entry")
                    continue
                try:
                    p = json.loads(entry.get("params") or "{}")
                except (json.JSONDecodeError, TypeError):
                    p = {}
                opts = p.get("options", {})
                test_run = p.get("test_run", True)
                inst, date, target = entry["inst"], entry["date"], entry["target"]
                key = job_key(inst, date, target)
                cmd = build_command(inst, date, target, opts, test_run=test_run)
                rdir = results_dir(inst, date)
                rdir.mkdir(parents=True, exist_ok=True)
                pending_log_path = _run_log_path(rdir, inst, date, target)
                try:
                    logf = open(pending_log_path, "w")
                    logf.write(f"$ {shlex.join(cmd)}\n\n")
                    logf.flush()
                    proc_env = {**os.environ, "PYTHONUNBUFFERED": "1"}
                    proc = subprocess.Popen(cmd, cwd=str(prose_project_dir()), stdout=logf, stderr=subprocess.STDOUT, text=True, start_new_session=True, env=proc_env)
                except (FileNotFoundError, OSError) as exc:
                    try: logf.close()
                    except OSError: pass
                    save_job(type_="photometry", inst=inst, date=date, target=target, state="error", returncode=-1, elapsed=0, started_at=entry["started_at"], error_desc=f"Failed to launch: {exc}")
                    continue
                run_type = "test" if test_run else "full"
                _JOBS[key] = Job(key=key, inst=inst, date=date, target=target, cmd=cmd, proc=proc, logf=logf, log_path=pending_log_path, run_type=run_type)
                try:
                    save_job(type_="photometry", inst=inst, date=date, target=target, state="running", returncode=None, elapsed=0, started_at=_JOBS[key].started_at, run_type=run_type, params=entry.get("params", ""))
                except sqlite3.OperationalError as exc:
                    try: proc.terminate()
                    except OSError: pass
                    try: logf.close()
                    except OSError: pass
                    _JOBS.pop(key, None)
                    save_job(type_="photometry", inst=inst, date=date, target=target, state="error", returncode=-1, elapsed=0, started_at=entry["started_at"], error_desc=f"Database not writable: {exc}")
