"""Helpers for the photometry page: locate prose pipeline outputs, serve
artifacts safely, and launch reductions as background jobs.

The prose pipeline (``../ext_tools/prose2``,
``python -m prose.scripts.run_photometry``) writes a flat directory of products
per instrument/date under ``$MUSCAT_PROSE_DIR/<inst>/<date>/`` with filenames

    {target}_{inst}_{band}_{date}_ref.png        # per-band reference image
    {target}_{inst}_{band}_{date}_apertures.png  # per-band aperture overlay
    {target}_{inst}_{band}_{date}_cutouts.png    # per-band star cutout montage
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
import logging
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
from muscat_db.band_utils import DEFAULT_BANDS, NARROW_BANDS, _FILTER_BAND_ALIAS, bands_from_filters  # noqa: F401

logger = logging.getLogger(__name__)

# --------------------------- configuration ---------------------------
# All paths are env-overridable so the page works in dev and on the server.
_HERE = Path(__file__).resolve().parent          # .../src/muscat_db
_REPO_ROOT = _HERE.parent.parent                 # .../muscat-db
_DEFAULT_PROSE_PROJECT = _REPO_ROOT.parent / "ext_tools" / "prose2"
_DEFAULT_OUTPUT_BASE = "/ut2/jerome/ql/prose"
# Temp dir for spawned pipeline jobs. The root filesystem holding /tmp is small
# and prone to filling up (astropy's mmap probe and FITS I/O write ephemerals
# there), so default to a roomy home-backed location instead of user-space /tmp.
# Derived from the home directory rather than a hardcoded user path so it is
# portable across machines/users (planned celery/redis multi-server setup).
# ``Path.home()`` resolves via the password database, so this still works when
# ``$HOME`` is unset (cron/systemd workers) -- unlike a literal ``$HOME`` in .env.
# Override with ``MUSCAT_TMPDIR`` when home is on a small/full filesystem.
_DEFAULT_TMPDIR = str(Path.home() / ".muscatdb" / "tmp")

# Default values for every optional run_photometry argument the form exposes.
# Kept here so the template, normalizer, and command builder share one source.
RUN_DEFAULTS: dict = {
    "run_name": "default",
    "bands": DEFAULT_BANDS,
    "ref_band": "",            # "" -> per-band self-reference (pipeline default)
    "refid": "",               # "" -> pipeline default (0 / middle frame)
    "aper_radii": "",          # "MIN,MAX,DR"; "" -> Gaia heuristic
    "annulus": "",             # "RIN,ROUT"; required with aper_radii
    "aper_unit": "pix",        # pix | fwhm (only applies with aper_radii)
    "make_gif": True,
    "plot_gaia_sources": True,
    "cmap": "gray",            # colormap for image-display plots (--cmap)
    "use_barycorrpy": False,
    "test_run_frames": 10,
    "min_star_separation": 10.0,
    "max_num_stars": 10,
    "n_stars_align": "",       # "" -> same as max_num_stars
    "cutout_size": 35,
    "ccd_trim": "",            # "Y,X"; "" -> no trim (pipeline default)
    "edge_margin": "",         # px from CCD edge to exclude comps; "" -> auto (half cutout), 0 -> off
    "bin_size_minutes": 10.0,
    "target_id": "",           # "" -> auto
    "comparison_ids": "",      # "" -> auto, or "1,2,3"
    "avoid_comparison_ids": "",  # "" -> none; "1,2,3" -> --avoid_cids (requires --ref_band)
    "avoid_nearby_star_mode": "auto",  # off | auto | custom
    "avoid_nearby_star": "",  # arcsec; used when avoid_nearby_star_mode == "custom"
    "target_coord": "",        # "" -> resolve via MAST; "RA Dec" -> bypass name resolution
    "gif_stride": 100,
    "overwrite": True,
    "sig_bkg": None,           # None -> sigma clipping disabled for bkg axis
    "sig_fwhm": None,          # None -> sigma clipping disabled for fwhm axis
    "sig_dx": None,            # None -> sigma clipping disabled for dx axis
    "sig_dy": None,            # None -> sigma clipping disabled for dy axis
    "min_star_area": 10,
    "wcs_method": "astrometry.net",
    "calib_dir": "",
    "site": "",                # sinistro only: "" -> all sites; else one of SINISTRO_SITES
    "mode": "",                # sinistro only: "" -> all modes; else one of SINISTRO_MODES
}

# Valid sinistro --site / --mode values, mirrored from prose2's run_photometry.py.
SINISTRO_SITES = ("lsc", "cpt", "coj", "tfn", "elp")
SINISTRO_MODES = ("central_2k_2x2", "full_frame")

# Colormaps offered for image-display plots (--cmap). run_photometry.py accepts
# any matplotlib name, so this curated set is the GUI/validation allowlist and
# must stay in sync with the <select> options in templates/photometry.html.
CMAP_CHOICES = (
    "gray", "gray_r",
    "coolwarm", "RdBu", "RdGy", "PiYG", "PRGn", "BrBG", "PuOr",
    "RdYlBu", "RdYlGn", "Spectral",
)


ALLOWED_EXTS = {".png", ".gif", ".csv", ".npz", ".log", ".txt"}
_RUN_LOG_NAME = "_webrun.log"
_RUNS_DIR_NAME = "_runs"
_RUN_META_NAME = "_webrun_meta.json"
_CONDA_ENV_DEFAULT = "prose"   # prose deps live in a conda env named "prose"
_MODULE = "prose.scripts.run_photometry"

_DATE_RE = re.compile(r"^\d{6}$")
# A served filename is a single path segment of safe characters only.
_NAME_RE = re.compile(r"^[A-Za-z0-9._+:\-]+$")
_RUN_NAME_MAX = 40
_RUN_SLUG_RE = re.compile(r"[^a-z0-9]+")

# Summary (multi-band) plot suffixes -> short key used by the template.
_SUMMARY_SUFFIX = {
    "_lightcurves.png": "lightcurves",
    "_raw_flux.png": "raw_flux",
    "_covariates.png": "covariates",
    "_systematics.png": "covariates",   # backward compat with old pipeline
    "_stacks.png": "stacks",
    "_nearby_stars.csv": "nearby_stars",
}
# Per-band product suffixes -> short key.
_BAND_SUFFIX = {
    "_ref.png": "ref",
    "_apertures.png": "apertures",
    "_cutouts.png": "cutouts",
    "_alignment.png": "alignment",
    ".gif": "gif",
    ".csv": "csv",
}


def output_base() -> Path:
    # ``.expanduser()`` for parity with the timer dir getter (transit_fit.py), so
    # a ``~``-prefixed MUSCAT_PROSE_DIR resolves instead of creating a literal '~'.
    return Path(os.environ.get("MUSCAT_PROSE_DIR", _DEFAULT_OUTPUT_BASE)).expanduser()


def prose_project_dir() -> Path:
    return Path(os.environ.get("MUSCAT_PROSE_PROJECT", str(_DEFAULT_PROSE_PROJECT)))


def prose_tmpdir() -> str:
    """Temp dir handed to spawned pipeline jobs (overridable via MUSCAT_TMPDIR)."""
    return os.environ.get("MUSCAT_TMPDIR", _DEFAULT_TMPDIR)


def _job_env() -> dict[str, str]:
    """Environment for spawned pipeline subprocesses.

    Routes all ephemeral files (TMPDIR/TMP/TEMP) to a raid-backed directory so
    jobs never trip over a full root ``/tmp``. The dir is created if missing;
    if that fails we fall back to the inherited environment rather than block
    the launch.
    """
    env = {**os.environ, "PYTHONUNBUFFERED": "1"}
    tmpdir = prose_tmpdir()
    try:
        Path(tmpdir).mkdir(parents=True, exist_ok=True)
        env["TMPDIR"] = tmpdir
        env["TMP"] = tmpdir
        env["TEMP"] = tmpdir
    except OSError as exc:
        logger.warning("could not prepare TMPDIR %s (%s); using inherited temp", tmpdir, exc)
    return env


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


def _target_dir_name(target: str) -> str:
    name = (target or "").replace(" ", "")
    if not name or ".." in name or "/" in name or "\\" in name:
        raise ValueError("invalid target")
    return name


def _run_dir_name(run_id: str) -> str:
    rid = (run_id or "").strip()
    if not rid or ".." in rid or "/" in rid or "\\" in rid or rid in {".", ".."}:
        raise ValueError("invalid run id")
    return rid


def slugify_run_name(run_name: str | None) -> str:
    """Slug a user run label. Blank input maps to ``default``."""
    s = _RUN_SLUG_RE.sub("_", (run_name or "").strip().lower()).strip("_")
    return s[:_RUN_NAME_MAX].strip("_") or "default"


def build_run_id(inst: str, site: str | None, mode: str | None, run_name: str | None) -> str:
    """Compose a photometry run id using the transit-fit convention.

    Non-sinistro runs are identified by the run-name slug only. Sinistro runs
    include site and only non-default readout mode: ``central_2k_2x2`` is
    omitted, while ``full_frame`` remains explicit.
    """
    slug = slugify_run_name(run_name)
    if inst != "sinistro":
        return slug
    mode_part = (mode or "").strip().lower()
    if mode_part == "central_2k_2x2":
        mode_part = ""
    parts = [
        (site or "").strip().lower(),
        mode_part,
        slug,
    ]
    return "-".join(p for p in parts if p)


def run_output_dir(inst: str, date: str, target: str, run_id: str | None = None) -> Path:
    """Output directory for a legacy or named photometry run."""
    base = results_dir(inst, date)
    if not run_id:
        return base
    return base / _RUNS_DIR_NAME / _target_dir_name(target) / _run_dir_name(run_id)


def _parse_run_dir_name(inst: str, name: str) -> tuple[str, str, str]:
    """Best-effort split of a run-id dir name into (site, mode, run_name)."""
    if inst != "sinistro":
        return "", "", name
    parts = name.split("-")
    site = mode = ""
    if parts and parts[0] in SINISTRO_SITES:
        site, parts = parts[0], parts[1:]
    if parts and parts[0] in SINISTRO_MODES:
        mode, parts = parts[0], parts[1:]
    elif site:
        mode = "central_2k_2x2"
    return site, mode, "-".join(parts)


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
    runs_dir = rdir / _RUNS_DIR_NAME
    if runs_dir.is_dir():
        for d in runs_dir.iterdir():
            if d.is_dir():
                found.add(d.name)
    return sorted(found)


def list_outputs(
    inst: str,
    date: str,
    target: str,
    site: str | None = None,
    mode: str | None = None,
    run_id: str | None = None,
) -> dict:
    """Classify the existing products for one (inst, date, target).

    Returns a dict with ``summary`` (key->filename), ``bands``
    (band->{ref,apertures,alignment,gif,csv}), ``npz``, ``log`` (newest),
    ``has_any``, ``sites``/``modes`` (distinct sinistro sites/readout modes
    present, for the filter chips), ``site``/``mode`` (the ones actually shown),
    and ``ref_header`` (the reference-frame header sidecar, site/mode-scoped).
    Only filenames are returned; serve them via the file route.

    A single sinistro date+target can hold products from more than one LCO site
    and more than one readout mode (identical bands per combination). To avoid
    silently collapsing them via newest-wins, products are restricted to one
    (site, mode) at a time: ``site``/``mode`` select them when given and present,
    otherwise the newest reduction is shown by default. Mode is read from the
    ``_full`` filename token prose appends for ``full_frame`` (``central_2k_2x2``
    has no token). For non-sinistro instruments there is no site/mode dimension
    and ``sites``/``modes`` stay empty.

    The date token embedded in filenames by the pipeline is taken from the FITS
    header and may differ from the directory name (obs-night vs UT date). We
    therefore build regexes that accept any 6-digit date token instead of
    requiring an exact match against the passed-in ``date``.
    """
    out: dict = {
        "summary": {},
        "summary_items": [],
        "bands": {},
        "npz": None,
        "log": None,
        "has_any": False,
        "masters": [],
        "sites": [],
        "site": None,
        "modes": [],
        "mode": None,
        "ref_header": None,
    }
    try:
        rdir = run_output_dir(inst, date, target, run_id)
    except ValueError:
        return out
    if not rdir.is_dir():
        return out

    t = target.replace(" ", "")
    inst_esc = re.escape(inst)
    t_esc = re.escape(t)

    # Sinistro filenames optionally carry a site token between inst and the
    # band/date when reduced with ``--site`` (prose ``build_stem``):
    #   <target>_sinistro_<site>_<date6>            (summary)
    #   <target>_sinistro_<site>_<band>_<date6>     (per-band)
    # Constrain the token to the known site codes so it can't be confused with a
    # band name that itself contains underscores (e.g. g_narrow, Na_D). For all
    # other instruments there is no site token and this slot is omitted.
    site_opt = (
        rf"(?:(?P<site>{'|'.join(SINISTRO_SITES)})_)?" if inst == "sinistro" else ""
    )
    # Prose appends ``_full`` after the date for full_frame; central_2k_2x2 has
    # no token. Captured between the date and the product suffix (e.g.
    # ..._250710_full_lightcurves.png, ..._250710_full.npz, ..._gp_250710_full.csv).
    mode_opt = r"(?P<mode>_full)?" if inst == "sinistro" else ""

    # Summary stems exist in two generations:
    #   <target>_<inst>_[<site>_]<date6>[_full]                (legacy)
    #   <target>_<inst>_[<site>_]<bands>_<date6>[_full]        (band-set scoped)
    # Allow any 6-digit date so obs-night and UT-date both match.
    summary_re = re.compile(
        rf"^{t_esc}_{inst_esc}_{site_opt}(?P<file_date>\d{{6}}){mode_opt}(?P<rest>.*)$"
    )
    summary_bandset_re = re.compile(
        rf"^{t_esc}_{inst_esc}_{site_opt}(?P<bands>[A-Za-z0-9_]+?)_(?P<file_date>\d{{6}}){mode_opt}(?P<rest>.*)$"
    )
    # Per-band stem: <target>_<inst>_[<site>_]<band>_<date6>[_full]. The band
    # token may itself contain underscores (narrow-band/Johnson filters:
    # g_narrow, Na_D, z_s), so allow ``_`` in the band and match it lazily up to
    # the 6-digit date.
    band_re = re.compile(
        rf"^{t_esc}_{inst_esc}_{site_opt}(?P<band>[A-Za-z0-9_]+?)_(?P<file_date>\d{{6}}){mode_opt}(?P<rest>.*)$"
    )

    def _mode_of(m: re.Match) -> str:
        """Canonical readout mode for a matched product file."""
        return "full_frame" if m.groupdict().get("mode") else "central_2k_2x2"

    # First pass (sinistro only): discover which sites and readout modes are
    # present so multi-site/multi-mode dates expose one chip per value and default
    # to the most recently reduced combination rather than a newest-wins mix.
    # Mode chips are scoped to the chosen site (each site may carry its own modes),
    # so switching site never lands on an empty (site, mode) pairing by default.
    effective_site: str | None = None
    effective_mode: str | None = None
    if inst == "sinistro":
        # records: (site_or_None, canonical_mode, mtime)
        records: list[tuple[str | None, str, float]] = []
        for p in rdir.iterdir():
            if not p.is_file() or p.suffix == ".log":
                continue
            m = summary_re.match(p.name) or summary_bandset_re.match(p.name) or band_re.match(p.name)
            if not m:
                continue
            try:
                mt = p.stat().st_mtime
            except OSError:
                mt = 0.0
            records.append((m.groupdict().get("site"), _mode_of(m), mt))

        site_mtime: dict[str, float] = {}
        for s, _m, mt in records:
            if s and mt > site_mtime.get(s, -1.0):
                site_mtime[s] = mt
        out["sites"] = sorted(site_mtime)
        if site and site in site_mtime:
            effective_site = site
        elif site_mtime:
            effective_site = max(site_mtime, key=site_mtime.get)  # newest wins
        out["site"] = effective_site

        # Modes available for the chosen site (or all records when no site token).
        mode_mtime: dict[str, float] = {}
        for s, md, mt in records:
            if effective_site is not None and s != effective_site:
                continue
            if mt > mode_mtime.get(md, -1.0):
                mode_mtime[md] = mt
        out["modes"] = sorted(mode_mtime)
        if mode and mode in mode_mtime:
            effective_mode = mode
        elif mode_mtime:
            effective_mode = max(mode_mtime, key=mode_mtime.get)  # newest wins
        out["mode"] = effective_mode

    logs: list[Path] = []

    for p in sorted(rdir.iterdir()):
        if not p.is_file():
            continue
        name = p.name
        if p.suffix == ".log":
            logs.append(p)
            continue

        try:
            st = p.stat()
            mtime = st.st_mtime
            version = str(st.st_mtime_ns)
            created_at = datetime.datetime.fromtimestamp(mtime).strftime('%Y-%m-%d %H:%M')
        except Exception:
            mtime = 0.0
            version = "0"
            created_at = "Unknown"

        # Try summary suffixes first, including band-set-scoped summary stems.
        ms = summary_re.match(name) or summary_bandset_re.match(name)
        if ms:
            # When a site/mode is in force, only that combination is shown.
            if effective_site is not None and ms.group("site") != effective_site:
                continue
            if effective_mode is not None and _mode_of(ms) != effective_mode:
                continue
            rest = ms.group("rest")
            if rest == ".npz":
                existing = out.get("npz")
                if existing is None or mtime > out.get("_npz_mtime", 0):
                    out["npz"] = name
                    out["_npz_mtime"] = mtime
                out["has_any"] = True
                continue
            if rest == "_ref_header.txt":
                if out["ref_header"] is None or mtime > out.get("_ref_header_mtime", 0):
                    out["ref_header"] = name
                    out["_ref_header_mtime"] = mtime
                out["has_any"] = True
                continue
            key = _SUMMARY_SUFFIX.get(rest)
            if key is not None:
                item = {
                    "key": key,
                    "file": name,
                    "created_at": created_at,
                    "version": version,
                    "_mtime": mtime,
                }
                if key != "nearby_stars":
                    out["summary_items"].append(item)
                existing = out["summary"].get(key)
                if existing is None or mtime > existing.get("_mtime", 0):
                    out["summary"][key] = item
                out["has_any"] = True
                continue
            # If the summary regex matched but rest is unrecognised, fall
            # through to the band regex (it is more specific).

        mb = band_re.match(name)
        if not mb:
            continue
        if effective_site is not None and mb.group("site") != effective_site:
            continue
        if effective_mode is not None and _mode_of(mb) != effective_mode:
            continue
        rest = mb.group("rest")
        key = _BAND_SUFFIX.get(rest)
        if key is None:
            continue
        band = mb.group("band")
        existing = out["bands"].setdefault(band, {}).get(key)
        if existing is None or mtime > existing.get("_mtime", 0):
            out["bands"][band][key] = {
                "file": name,
                "created_at": created_at,
                "version": version,
                "_mtime": mtime,
            }
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
    out["summary_items"].sort(
        key=lambda item: (
            ["lightcurves", "raw_flux", "covariates", "stacks"].index(item["key"])
            if item["key"] in {"lightcurves", "raw_flux", "covariates", "stacks"}
            else 99,
            -item.get("_mtime", 0),
            item["file"],
        )
    )
    for d in out["summary"].values():
        d.pop("_mtime", None)
    for d in out["summary_items"]:
        d.pop("_mtime", None)
    for band_d in out["bands"].values():
        for d in band_d.values():
            d.pop("_mtime", None)
    out.pop("_npz_mtime", None)
    out.pop("_ref_header_mtime", None)
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


@dataclass
class RunDescriptor:
    run_id: str
    site: str = ""
    mode: str = ""
    run_name: str = ""
    mtime: float = 0.0
    is_legacy: bool = False
    run_type: str = "full"


def _read_run_meta(d: Path) -> dict:
    try:
        return json.loads((d / _RUN_META_NAME).read_text())
    except (OSError, json.JSONDecodeError):
        return {}


def _dir_mtime(d: Path) -> float:
    try:
        return max((p.stat().st_mtime for p in d.iterdir() if p.is_file()), default=d.stat().st_mtime)
    except OSError:
        return 0.0


def _write_run_meta(d: Path, *, inst: str, date: str, target: str, run_id: str, site: str, mode: str, run_name: str, run_type: str) -> None:
    meta = {
        "inst": inst,
        "date": date,
        "target": target,
        "run_id": run_id,
        "site": site,
        "mode": mode,
        "run_name": run_name,
        "run_type": run_type,
        "created_at": datetime.datetime.now(datetime.UTC).isoformat(),
    }
    try:
        (d / _RUN_META_NAME).write_text(json.dumps(meta, sort_keys=True, indent=2))
    except OSError:
        pass


def list_photometry_runs(
    inst: str, date: str, target: str
) -> tuple[list[RunDescriptor], dict[str | None, dict]]:
    """Enumerate legacy and named photometry runs for a target, newest-first.

    Returns ``(runs, run_outputs)`` where ``run_outputs`` maps each run's
    ``run_id`` (``None`` for the legacy run) to its pre-computed
    ``list_outputs()`` result.  Callers that need the outputs for the selected
    run can pull them from the dict instead of calling ``list_outputs()`` again,
    avoiding a redundant directory scan.
    """
    runs: list[RunDescriptor] = []
    run_outputs: dict[str | None, dict] = {}

    legacy = list_outputs(inst, date, target)
    run_outputs[None] = legacy
    legacy_dir = run_output_dir(inst, date, target)
    if legacy.get("has_any"):
        runs.append(RunDescriptor(
            run_id="",
            site="",
            mode="",
            run_name="legacy",
            mtime=_dir_mtime(legacy_dir),
            is_legacy=True,
            run_type="full",
        ))

    try:
        runs_root = results_dir(inst, date) / _RUNS_DIR_NAME / _target_dir_name(target)
    except ValueError:
        return runs, run_outputs
    if runs_root.is_dir():
        for d in sorted(runs_root.iterdir()):
            if not d.is_dir():
                continue
            out = list_outputs(inst, date, target, run_id=d.name)
            run_outputs[d.name] = out
            if not out.get("has_any"):
                continue
            meta = _read_run_meta(d)
            if meta.get("run_id") or meta.get("run_name"):
                site = str(meta.get("site") or "")
                mode = str(meta.get("mode") or "")
                run_name = str(meta.get("run_name") or "")
                run_type = str(meta.get("run_type") or "full")
            else:
                site, mode, run_name = _parse_run_dir_name(inst, d.name)
                run_type = "full"
            runs.append(RunDescriptor(
                run_id=d.name,
                site=site,
                mode=mode,
                run_name=run_name or d.name,
                mtime=_dir_mtime(d),
                is_legacy=False,
                run_type=run_type,
            ))
    runs.sort(key=lambda r: r.mtime, reverse=True)
    return runs, run_outputs


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


def safe_run_artifact_path(inst: str, date: str, target: str, run_id: str, name: str) -> Path | None:
    """Resolve a named-run artifact path, or ``None`` when invalid/missing."""
    if inst not in INSTRUMENTS or not valid_date(date):
        return None
    if ".." in name or "/" in name or not _NAME_RE.match(name):
        return None
    if Path(name).suffix.lower() not in ALLOWED_EXTS:
        return None
    try:
        rdir = run_output_dir(inst, date, target, run_id).resolve()
    except ValueError:
        return None
    candidate = (rdir / name).resolve()
    try:
        candidate.relative_to(rdir)
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

    for key in ("run_name", "ref_band", "aper_radii", "annulus", "aper_unit", "ccd_trim", "target_id", "comparison_ids", "avoid_comparison_ids", "avoid_nearby_star_mode", "avoid_nearby_star", "target_coord", "wcs_method", "calib_dir", "site", "mode", "cmap"):
        if raw.get(key) is not None:
            o[key] = str(raw[key]).strip()

    for key in ("refid", "n_stars_align"):  # optional ints; "" keeps default
        if key in raw:
            val = str(raw.get(key, "")).strip()
            o[key] = "" if val == "" else (_to_int(val) if _to_int(val) is not None else "")

    for key in ("test_run_frames", "max_num_stars", "cutout_size", "gif_stride", "min_star_area", "edge_margin"):
        if str(raw.get(key, "")).strip() != "":
            iv = _to_int(raw[key])
            if iv is not None:
                o[key] = iv

    for key in ("min_star_separation", "avoid_nearby_star", "bin_size_minutes", "sig_bkg", "sig_fwhm", "sig_dx", "sig_dy"):
        if str(raw.get(key, "")).strip() != "":
            fv = _to_float(raw[key])
            if fv is not None:
                o[key] = fv

    for key in ("make_gif", "plot_gaia_sources", "use_barycorrpy"):
        if key in raw:
            o[key] = _to_bool(raw[key])
    # Overwrite requires special handling: explicitly False if present and falsy,
    # otherwise default to True. This ensures unchecking the box is honored.
    if "overwrite" in raw:
        o["overwrite"] = _to_bool(raw["overwrite"])
    # If not in raw dict at all, default to True (keep existing behavior)

    # Backward-compat: older UI stored a checkbox + optional value instead of a
    # 3-state mode. Translate that persisted shape when the new mode is absent.
    if "avoid_nearby_star_mode" not in raw and "avoid_nearby_stars" in raw:
        enabled = _to_bool(raw.get("avoid_nearby_stars"))
        if not enabled:
            o["avoid_nearby_star_mode"] = "off"
        else:
            o["avoid_nearby_star_mode"] = (
                "custom" if str(raw.get("avoid_nearby_star", "")).strip() != "" else "auto"
            )

    return o


def validate_run_options(o: dict, inst: str | None = None) -> str | None:
    """Return a user-facing error string for invalid options, else ``None``."""
    if not o.get("bands"):
        return "select at least one band"
    if any(not _BAND_RE.match(b) for b in o["bands"]):
        return "band names may only contain letters, digits and underscores"
    ref_band = (o.get("ref_band") or "").strip()
    if inst == "sinistro" and ref_band and len(o.get("bands") or []) > 1:
        return "reference band is disabled for multi-band Sinistro reductions because simultaneous bands can be from different telescopes/pointings"
    if ref_band and ref_band not in set(o.get("bands") or []):
        return "reference band must be one of the selected bands"
    if (o.get("avoid_comparison_ids") or "").strip() and not (o.get("ref_band") or "").strip():
        return "avoid comparison IDs requires a reference band"
    if o.get("avoid_nearby_star_mode") not in ("off", "auto", "custom"):
        return "avoid nearby stars mode must be one of off, auto, or custom"
    if o.get("avoid_nearby_star_mode") == "custom":
        nearby = o.get("avoid_nearby_star")
        if nearby not in (None, ""):
            try:
                nearby_f = float(nearby)
            except (TypeError, ValueError):
                return "nearby-star separation must be a number in arcsec"
            if nearby_f <= 0:
                return "nearby-star separation must be > 0 arcsec"
        else:
            return "nearby-star separation is required in custom mode"
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
    if o.get("wcs_method") not in ("twirl", "astrometry.net"):
        return "WCS method must be 'twirl' or 'astrometry.net'"
    # Colormap validation: default is "gray" (black=low, white=high).
    # Note: prose2's run_photometry has a bug where gray and gray_r render
    # identically; this is a prose2 issue and needs to be fixed there.
    if (o.get("cmap") or "gray") not in CMAP_CHOICES:
        return f"colormap must be one of {', '.join(CMAP_CHOICES)}"
    site = (o.get("site") or "").strip().lower()
    if site and site not in SINISTRO_SITES:
        return f"site must be one of {', '.join(SINISTRO_SITES)}"
    mode = (o.get("mode") or "").strip()
    if mode and mode not in SINISTRO_MODES:
        return f"mode must be one of {', '.join(SINISTRO_MODES)}"
    return None


def build_command(
    inst: str,
    date: str,
    target: str,
    options: dict | None = None,
    *,
    test_run: bool = True,
    run_id: str | None = None,
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
        "--results_dir", str(run_output_dir(inst, date, target, run_id)),
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
    if o.get("avoid_nearby_star_mode") != "off":
        nearby = o.get("avoid_nearby_star")
        if o.get("avoid_nearby_star_mode") == "auto" or nearby in (None, ""):
            args.append("--avoid_nearby_star")
        else:
            args += ["--avoid_nearby_star", str(nearby)]

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
    # Empty -> auto (half cutout); explicit int (incl. 0 to disable) is emitted.
    if str(o.get("edge_margin", "")).strip() != "":
        args += ["--edge_margin", str(o["edge_margin"])]

    if o.get("wcs_method", "astrometry.net") != "astrometry.net":
        args += ["--wcs_method", o["wcs_method"]]
    # --site / --mode are sinistro-only filters; ignore for other instruments.
    if inst == "sinistro":
        site = (o.get("site") or "").strip()
        if site:
            args += ["--site", site]
        mode = (o.get("mode") or "").strip()
        if mode:
            args += ["--mode", mode]
    if inst in ("muscat", "muscat2"):
        args += ["--calib_dir", o.get("calib_dir") or str(results_dir(inst, date)) + "_calibrated"]
    if o.get("make_gif", False):
        args.append("--gif")
    if o.get("plot_gaia_sources", True):
        args.append("--plot_gaia_sources")
    cmap = (o.get("cmap") or "").strip()
    if cmap and cmap != RUN_DEFAULTS["cmap"]:
        args += ["--cmap", cmap]
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
    run_id: str | None = None,
) -> str:
    if run_id is None:
        opts = normalize_run_options(options)
        run_id = build_run_id(inst, opts.get("site"), opts.get("mode"), opts.get("run_name"))
    return shlex.join(build_command(inst, date, target, options, test_run=test_run, run_id=run_id))


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
    run_id: str = ""
    site: str = ""
    mode: str = ""
    run_name: str = ""


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


def job_key(inst: str, date: str, target: str, run_id: str = "") -> str:
    base = f"{inst}/{date}/{target.replace(' ', '')}"
    return f"{base}/{run_id}" if run_id else base


def _run_log_path(rdir: Path, inst: str, date: str, target: str, run_id: str = "") -> Path:
    """Return a deterministic, target-specific web-run log path."""
    digest = hashlib.sha256(job_key(inst, date, target, run_id).encode()).hexdigest()[:16]
    return rdir / f"_webrun_{digest}.log"


def log_path(inst: str, date: str, target: str, run_id: str = "") -> Path | None:
    try:
        rdir = run_output_dir(inst, date, target, run_id or None)
    except ValueError:
        return None
    p = _run_log_path(rdir, inst, date, target, run_id)
    return p if p.is_file() else None


def _full_reduction_exists(
    inst: str,
    date: str,
    target: str,
    run_id: str,
) -> bool:
    """Check if a full reduction already exists for the given run_id."""
    try:
        rdir = run_output_dir(inst, date, target, run_id)
    except ValueError:
        return False
    return rdir.is_dir()


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
    err = validate_run_options(opts, inst=inst)
    if err:
        return {"ok": False, "error": err}
    rawdir = raw_data_dir(inst, date)
    if not rawdir.is_dir():
        return {"ok": False, "error": f"raw data not found: {rawdir}"}

    run_name = str(opts.get("run_name") or "").strip()
    site = (opts.get("site") or "").strip().lower() if inst == "sinistro" else ""
    mode = (opts.get("mode") or "").strip().lower() if inst == "sinistro" else ""
    run_id = build_run_id(inst, site, mode, run_name)
    key = job_key(inst, date, target, run_id)
    run_type = "test" if test_run else "full"

    with _LOCK:
        existing = _JOBS.get(key)
        # For full runs at capacity, allow queuing even if a job with the same key exists
        # (the existing job will be reused if still running; if not, a new one will be queued)
        at_capacity = run_type == "full" and _count_running_full() >= _MAX_FULL_JOBS
        overwrite = opts.get("overwrite", True)
        logger.info(f"start_run: {inst}/{date}/{target} run_id={run_id} overwrite={overwrite} existing={existing is not None} at_capacity={at_capacity}")

        if existing is not None and existing.proc.poll() is None and not at_capacity:
            # If overwrite is True, cancel the existing job and start a new one
            if overwrite:
                logger.info(f"start_run: cancelling existing job for {key} (overwrite=True)")
                try:
                    existing.proc.terminate()
                    if existing.logf:
                        try:
                            existing.logf.close()
                        except OSError:
                            pass
                except OSError:
                    pass
                _JOBS.pop(key, None)
            else:
                logger.info(f"start_run: reusing existing job for {key} (overwrite=False)")
                return {"ok": True, "key": key, "already_running": True, "run_id": run_id}

        if run_type == "full":
            if _full_reduction_exists(inst, date, target, run_id) and not overwrite:
                logger.info(f"start_run: refusing to overwrite existing full reduction for {key} (overwrite=False)")
                return {
                    "ok": False,
                    "error": "full reduction already exists for this target; enable 'Overwrite existing products' to replace",
                }

        # Queue full jobs when at capacity
        if at_capacity:
            from muscat_db.database import save_job
            try:
                save_job(
                    type_="photometry",
                    inst=inst, date=date, target=target,
                    state="pending",
                    returncode=None, elapsed=0,
                    started_at=time.time(),
                    run_type=run_type,
                    params=json.dumps({"test_run": test_run, "options": opts, "run_id": run_id, "site": site, "mode": mode, "run_name": run_name}, separators=(",", ":")),
                    run_id=run_id,
                    run_name=run_name,
                )
            except sqlite3.OperationalError as exc:
                return {"ok": False, "error": f"database not writable: {exc}"}
            return {"ok": True, "key": key, "queued": True, "run_id": run_id}

        try:
            rdir = run_output_dir(inst, date, target, run_id)
        except ValueError as exc:
            return {"ok": False, "error": str(exc)}
        rdir.mkdir(parents=True, exist_ok=True)
        _write_run_meta(rdir, inst=inst, date=date, target=target, run_id=run_id, site=site, mode=mode, run_name=run_name, run_type=run_type)
        cmd = build_command(inst, date, target, opts, test_run=test_run, run_id=run_id)
        log_path = _run_log_path(rdir, inst, date, target, run_id)
        logf = open(log_path, "w")
        logf.write(f"$ {shlex.join(cmd)}\n\n")
        logf.flush()
        try:
            env = _job_env()
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
            run_type=run_type, run_id=run_id, site=site, mode=mode, run_name=run_name,
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
                params=json.dumps({"test_run": test_run, "options": opts, "run_id": run_id, "site": site, "mode": mode, "run_name": run_name}, separators=(",", ":")),
                run_id=run_id,
                run_name=run_name,
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
    return {"ok": True, "key": key, "run_id": run_id}


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


# The pipeline is launched with start_new_session=True and prose spawns
# multiprocessing workers (SequenceParallel) that keep appending to the log
# *after* the tracked parent process returns. Declaring the job terminal at
# parent-exit freezes the photometry page's live log mid-output while the Jobs
# page (which reads the file directly) keeps advancing. Treat a finished parent
# as still "finalizing" until its log has been quiescent for this grace window,
# so the live view keeps streaming the trailing output. Env-tunable, in the
# style of _STALL_LIMIT_S / _MAX_RUNTIME_S.
_FINALIZE_GRACE_S = int(os.environ.get("MUSCAT_PHOT_FINALIZE_GRACE_S", 8))

# Once prose logs a terminal result line (SUCCEEDED / PARTIAL FAILURE / FAILED),
# its main work is done and the only remaining writes are worker teardown, so a
# much shorter quiescence window is enough to declare the job terminal. Before
# that line appears we keep the conservative default above so the live log never
# freezes mid-run. This lets a successful short run reload promptly instead of
# always waiting out the full window.
_FINALIZE_GRACE_TERMINAL_S = int(
    os.environ.get("MUSCAT_PHOT_FINALIZE_GRACE_TERMINAL_S", 2)
)

# Result lines emitted by prose's run_photometry once a run is decided. Any of
# these marks the end of the pipeline's own output (see run_photometry.py).
_TERMINAL_LOG_MARKERS = (
    "photometry SUCCEEDED",
    "photometry PARTIAL FAILURE",
    "photometry FAILED",
)


def _log_has_terminal_marker(path: Path | None) -> bool:
    """True once prose has logged a final result line. After this, remaining log
    writes are worker teardown, so the finalize window can be shortened."""
    if path is None or not path.is_file():
        return False
    tail = _tail(path, n=1000)
    return any(marker in tail for marker in _TERMINAL_LOG_MARKERS)


def _finalize_grace_s(log_path_: Path | None) -> int:
    """Effective finalize quiescence window for a log: the short terminal window
    once a result line is logged, else the conservative default. The ``min``
    guards against a default set below the terminal window — there is never a
    reason to wait longer after the result line than before it."""
    if _log_has_terminal_marker(log_path_):
        return min(_FINALIZE_GRACE_TERMINAL_S, _FINALIZE_GRACE_S)
    return _FINALIZE_GRACE_S


def _log_quiescent(log_path_: Path, now: float) -> bool:
    """True when the log has not been written for at least the finalize grace
    window. The window shrinks once prose logs a terminal result line (trailing
    output by then is just worker teardown). A missing/unreadable log means
    nothing more is coming, so it counts as quiescent; each append by a
    still-running worker refreshes the mtime and keeps the job finalizing."""
    try:
        mtime = log_path_.stat().st_mtime
    except OSError:
        return True
    return (now - mtime) >= _finalize_grace_s(log_path_)


def _resolve_job_state(job: "Job", now: float | None = None) -> tuple[str, int | None, bool]:
    """Resolve a tracked job's live state as ``(state, returncode, is_terminal)``.

    While the parent process runs the job is ``running``/``cancelling``. Once it
    exits, a non-cancelled job is reported as ``finalizing`` (non-terminal) until
    its log goes quiescent, so the live view keeps streaming the output workers
    emit after parent-exit. A cancelled job goes terminal immediately to keep the
    Cancel flow responsive.
    """
    rc = job.proc.poll()
    if rc is None:
        return ("cancelling" if job.cancelled else "running"), None, False
    if not job.cancelled and not _log_quiescent(job.log_path, now if now is not None else time.time()):
        return "finalizing", rc, False
    return _terminal_job_state(rc, job.cancelled, job.log_path), rc, True


def _pending_status(inst: str, date: str, target: str, run_id: str = "") -> dict | None:
    """Return a queued-job status dict if a pending DB entry exists, else None.

    A full run launched while the single full-job slot is occupied is recorded
    in the DB as ``pending`` but not added to ``_JOBS``; surface that here so the
    photometry page can show a "queued" state instead of silently resetting.
    """
    from muscat_db.database import get_persisted_jobs

    db_key = f"photometry:{job_key(inst, date, target, run_id)}"
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


def _persisted_status(inst: str, date: str, target: str, run_id: str = "") -> dict | None:
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

    db_key = f"photometry:{job_key(inst, date, target, run_id)}"
    try:
        for entry in get_persisted_jobs():  # newest-first; one row per key
            if entry["key"] != db_key or entry["type"] != "photometry":
                continue
            state = entry["state"]
            if state not in ("done", "error", "cancelled"):
                return None  # running/pending handled by the caller
            lp = log_path(inst, date, target, run_id)
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


def job_status(inst: str, date: str, target: str, run_id: str = "") -> dict:
    """Poll a job and return its state plus its target-specific log tail.

    A zero exit status is still an error when the pipeline logged
    ``photometry PARTIAL FAILURE``.
    """
    key = job_key(inst, date, target, run_id)
    with _LOCK:
        job = _JOBS.get(key)
        if job is None:
            pending = _pending_status(inst, date, target, run_id)
            if pending is not None:
                return pending
            persisted = _persisted_status(inst, date, target, run_id)
            if persisted is not None:
                return persisted
            return {"state": "none", "log": "", "returncode": None, "elapsed": 0}
        state, rc, is_terminal = _resolve_job_state(job)
        if is_terminal and job.state == "running":
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
        "run_type": job.run_type if job else "full",
    }


def get_all_jobs() -> list[dict]:
    """Retrieve all background jobs, polling/updating their state."""
    with _LOCK:
        res = []
        for key, job in _JOBS.items():
            state, rc, is_terminal = _resolve_job_state(job)
            if is_terminal and job.state == "running":
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
                "run_id": job.run_id,
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


def delete_reduction(inst: str, date: str, target: str, run_id: str = "") -> dict:
    """Delete all reduction products for one (inst, date, target) from disk.

    Removes files matching the target's stem in the results directory, plus
    the web-run log. Also clears the persisted job record so the Jobs page
    no longer shows a stale entry. Never touches other targets.
    """
    try:
        rdir = run_output_dir(inst, date, target, run_id or None)
    except ValueError as exc:
        return {"ok": False, "error": str(exc)}
    if not rdir.is_dir():
        return {"ok": True, "count": 0}
    t = target.replace(" ", "")
    stem = f"{t}_{inst}"
    web_log = _run_log_path(rdir, inst, date, target, run_id)
    removed = 0
    if run_id:
        try:
            for p in rdir.rglob("*"):
                if p.is_file():
                    removed += 1
            shutil.rmtree(rdir)
        except OSError as exc:
            return {"ok": False, "error": f"failed to delete run: {exc}"}
    else:
        for p in list(rdir.iterdir()):
            if not p.is_file():
                continue
            if p.name.startswith(stem):
                try:
                    p.unlink()
                    removed += 1
                except OSError:
                    pass
        if web_log.is_file():
            try:
                web_log.unlink()
                removed += 1
            except OSError:
                pass
    from muscat_db.database import db_path, clear_all_caches
    try:
        conn = sqlite3.connect(db_path())
        conn.execute("DELETE FROM jobs WHERE key = ?", (f"photometry:{job_key(inst, date, target, run_id)}",))
        conn.commit()
        conn.close()
        clear_all_caches()
    except Exception:
        pass
    try:
        del _JOBS[job_key(inst, date, target, run_id)]
    except KeyError:
        pass
    return {"ok": True, "count": removed}


def cancel_run(inst: str, date: str, target: str, run_id: str = "") -> dict:
    """Cancel a running or pending reduction. Sends SIGTERM to the job's process group
    and escalates to SIGKILL after a short grace period."""
    key = job_key(inst, date, target, run_id)
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
                    error_desc="Cancelled by user",
                    run_id=run_id,
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
            error_desc="Cancelled by user",
            run_id=run_id,
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
                run_id=job.run_id,
            )
            _JOBS.pop(key, None)

        db_jobs = get_persisted_jobs()
        for entry in db_jobs:
            if entry["type"] != "photometry" or entry["state"] != "done":
                continue
            entry_run_id = entry.get("run_id") or ""
            entry_log_path = log_path(entry["inst"], entry["date"], entry["target"], entry_run_id)
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
                run_id=entry_run_id,
            )

        running_keys = {j["key"] for j in db_jobs if j["state"] == "running" and j["type"] == "photometry"}
        db_by_key = {j["key"]: j for j in db_jobs}

        for key, job in _JOBS.items():
            db_key = f"photometry:{job_key(job.inst, job.date, job.target, job.run_id)}"
            state, rc, is_terminal = _resolve_job_state(job)
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
            # with the photometry page until the log truly goes quiescent.
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

            save_job(
                type_="photometry",
                inst=job.inst,
                date=job.date,
                target=job.target,
                state=persist_state,
                returncode=persist_rc,
                elapsed=round(elapsed),
                started_at=job.started_at,
                error_desc=error_desc,
                run_id=job.run_id,
            )
            
        for db_key in running_keys:
            _, rest = db_key.split(":", 1)
            parts = rest.split("/")
            if len(parts) < 3:
                continue
            inst, date, target = parts[:3]
            run_id = "/".join(parts[3:]) if len(parts) > 3 else ""
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
                error_desc="Process lost (server restart)",
                run_id=run_id,
            )

        # Launch pending full jobs if capacity allows
        if _count_running_full() < _MAX_FULL_JOBS:
            db_jobs = get_persisted_jobs()
            pending = [j for j in db_jobs if j["state"] == "pending" and j["type"] == "photometry"]
            pending.sort(key=lambda j: j["started_at"])
            for entry in pending:
                if _count_running_full() >= _MAX_FULL_JOBS:
                    break
                run_id = entry.get("run_id") or ""
                # The DB key is prefixed ("photometry:..."), so compare against the
                # in-memory job key (unprefixed) to detect a job that is already
                # running and must not be relaunched from its stale pending row.
                if job_key(entry["inst"], entry["date"], entry["target"], run_id) in _JOBS:
                    save_job(type_="photometry", inst=entry["inst"], date=entry["date"], target=entry["target"], state="error", returncode=-1, elapsed=0, started_at=entry["started_at"], error_desc="Duplicate entry", run_id=run_id, run_name=entry.get("run_name", ""))
                    continue
                try:
                    p = json.loads(entry.get("params") or "{}")
                except (json.JSONDecodeError, TypeError):
                    p = {}
                opts = p.get("options", {})
                test_run = p.get("test_run", True)
                inst, date, target = entry["inst"], entry["date"], entry["target"]
                run_id = p.get("run_id") or entry.get("run_id") or build_run_id(inst, opts.get("site"), opts.get("mode"), opts.get("run_name"))
                site = p.get("site", opts.get("site", "")) if inst == "sinistro" else ""
                mode = p.get("mode", opts.get("mode", "")) if inst == "sinistro" else ""
                run_name = p.get("run_name", opts.get("run_name", ""))
                key = job_key(inst, date, target, run_id)
                cmd = build_command(inst, date, target, opts, test_run=test_run, run_id=run_id)
                try:
                    rdir = run_output_dir(inst, date, target, run_id)
                except ValueError as exc:
                    save_job(type_="photometry", inst=inst, date=date, target=target, state="error", returncode=-1, elapsed=0, started_at=entry["started_at"], error_desc=str(exc), run_id=run_id, run_name=run_name)
                    continue
                rdir.mkdir(parents=True, exist_ok=True)
                run_type = "test" if test_run else "full"
                _write_run_meta(rdir, inst=inst, date=date, target=target, run_id=run_id, site=site, mode=mode, run_name=run_name, run_type=run_type)
                pending_log_path = _run_log_path(rdir, inst, date, target, run_id)
                try:
                    logf = open(pending_log_path, "w")
                    logf.write(f"$ {shlex.join(cmd)}\n\n")
                    logf.flush()
                    proc_env = _job_env()
                    proc = subprocess.Popen(cmd, cwd=str(prose_project_dir()), stdout=logf, stderr=subprocess.STDOUT, text=True, start_new_session=True, env=proc_env)
                except (FileNotFoundError, OSError) as exc:
                    try: logf.close()
                    except OSError: pass
                    save_job(type_="photometry", inst=inst, date=date, target=target, state="error", returncode=-1, elapsed=0, started_at=entry["started_at"], error_desc=f"Failed to launch: {exc}", run_id=run_id, run_name=run_name)
                    continue
                _JOBS[key] = Job(key=key, inst=inst, date=date, target=target, cmd=cmd, proc=proc, logf=logf, log_path=pending_log_path, run_type=run_type, run_id=run_id, site=site, mode=mode, run_name=run_name)
                try:
                    save_job(type_="photometry", inst=inst, date=date, target=target, state="running", returncode=None, elapsed=0, started_at=_JOBS[key].started_at, run_type=run_type, params=entry.get("params", ""), run_id=run_id, run_name=run_name)
                except sqlite3.OperationalError as exc:
                    try: proc.terminate()
                    except OSError: pass
                    try: logf.close()
                    except OSError: pass
                    _JOBS.pop(key, None)
                    save_job(type_="photometry", inst=inst, date=date, target=target, state="error", returncode=-1, elapsed=0, started_at=entry["started_at"], error_desc=f"Database not writable: {exc}", run_id=run_id, run_name=run_name)
