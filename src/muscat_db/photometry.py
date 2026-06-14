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
import os
import re
import shlex
import shutil
import signal
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
    "target_coord": "",        # "" -> resolve via MAST; "RA Dec" -> bypass name resolution
    "gif_stride": 100,
    "overwrite": True,
}

ALLOWED_EXTS = {".png", ".gif", ".csv", ".npz", ".log"}
_RUN_LOG_NAME = "_webrun.log"  # combined stdout/stderr of a web-launched run
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
    """Target names inferred from product filenames already in the output dir."""
    rdir = results_dir(inst, date)
    if not rdir.is_dir():
        return []
    pat = re.compile(
        rf"^(?P<t>.+?)_{re.escape(inst)}_(?:[A-Za-z0-9_]+_)?{re.escape(date)}(?:[._]|$)"
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

    multi = _stem(target, inst, date)
    band_re = re.compile(
        rf"^{re.escape(multi.rsplit('_', 1)[0])}_"
        rf"(?P<band>[A-Za-z0-9_]+)_{re.escape(date)}(?P<rest>.*)$"
    )
    logs: list[Path] = []

    for p in sorted(rdir.iterdir()):
        if not p.is_file():
            continue
        name = p.name
        if p.suffix == ".log":
            logs.append(p)
            continue

        for suf, key in _SUMMARY_SUFFIX.items():
            if name == multi + suf:
                out["summary"][key] = name
                out["has_any"] = True
                break
        else:
            if name == multi + ".npz":
                out["npz"] = name
                out["has_any"] = True
                continue
            m = band_re.match(name)
            if not m:
                continue
            rest = m.group("rest")
            key = _BAND_SUFFIX.get(rest)
            if key is None:
                continue
            out["bands"].setdefault(m.group("band"), {})[key] = name
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

    # Order bands canonically (gp, rp, ip, zs) then any extras.
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
        csv_name = band_data.get("csv")
        if csv_name:
            csv_path = rdir / csv_name
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

    for key in ("ref_band", "aper_radii", "annulus", "aper_unit", "ccd_trim", "target_id", "comparison_ids", "target_coord"):
        if raw.get(key) is not None:
            o[key] = str(raw[key]).strip()

    for key in ("refid", "n_stars_align"):  # optional ints; "" keeps default
        if key in raw:
            val = str(raw.get(key, "")).strip()
            o[key] = "" if val == "" else (_to_int(val) if _to_int(val) is not None else "")

    for key in ("test_run_frames", "max_num_stars", "cutout_size", "gif_stride"):
        if str(raw.get(key, "")).strip() != "":
            iv = _to_int(raw[key])
            if iv is not None:
                o[key] = iv

    for key in ("min_star_separation", "bin_size_minutes"):
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

    # Numeric overrides: only emit when the user changed them from the default.
    for flag, key in (
        ("--test_run_frames", "test_run_frames"),
        ("--min_star_separation", "min_star_separation"),
        ("--max_num_stars", "max_num_stars"),
        ("--cutout_size", "cutout_size"),
        ("--bin_size_minutes", "bin_size_minutes"),
        ("--gif_stride", "gif_stride"),
    ):
        val = o.get(key)
        if val in (None, ""):
            continue
        if float(val) != float(RUN_DEFAULTS[key]):
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


_JOBS: dict[str, Job] = {}
_LOCK = threading.Lock()


def job_key(inst: str, date: str, target: str) -> str:
    return f"{inst}/{date}/{target.replace(' ', '')}"


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

        rdir = results_dir(inst, date)
        rdir.mkdir(parents=True, exist_ok=True)
        cmd = build_command(inst, date, target, opts, test_run=test_run)
        log_path = rdir / _RUN_LOG_NAME
        logf = open(log_path, "w")
        logf.write(f"$ {shlex.join(cmd)}\n\n")
        logf.flush()
        try:
            proc = subprocess.Popen(
                cmd,
                cwd=str(prose_project_dir()),
                stdout=logf,
                stderr=subprocess.STDOUT,
                text=True,
                # Own session/process group so Cancel can kill the whole tree
                # (prose spawns multiprocessing workers via SequenceParallel).
                start_new_session=True,
            )
        except (FileNotFoundError, OSError) as exc:
            logf.write(f"\nfailed to launch pipeline: {exc}\n")
            logf.close()
            return {"ok": False, "error": f"failed to launch pipeline: {exc}"}
        _JOBS[key] = Job(
            key=key, inst=inst, date=date, target=target,
            cmd=cmd, proc=proc, logf=logf, log_path=log_path,
        )
        # Record new job in the database
        from muscat_db.database import save_job
        save_job(
            type_="photometry",
            inst=inst,
            date=date,
            target=target,
            state="running",
            returncode=None,
            elapsed=0,
            started_at=_JOBS[key].started_at,
            run_type="test" if test_run else "full"
        )
    return {"ok": True, "key": key}


def _tail(path: Path, n: int = 200) -> str:
    if not path.is_file():
        return ""
    try:
        with open(path, errors="replace") as f:
            return "".join(deque(f, maxlen=n))
    except OSError:
        return ""


def job_status(inst: str, date: str, target: str) -> dict:
    """Poll a launched job and return its state plus a tail of the run log."""
    key = job_key(inst, date, target)
    with _LOCK:
        job = _JOBS.get(key)
        if job is None:
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
                try:
                    job.logf.close()
                except OSError:
                    pass
        log_path = job.log_path
        elapsed = job.elapsed if job.state not in ("running", "cancelling") and job.elapsed is not None else round(time.time() - job.started_at)
    return {
        "state": state,
        "returncode": rc,
        "log": _tail(log_path),
        "elapsed": round(elapsed),
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


def cancel_run(inst: str, date: str, target: str) -> dict:
    """Cancel a running reduction. Sends SIGTERM to the job's process group
    and escalates to SIGKILL after a short grace period."""
    key = job_key(inst, date, target)
    with _LOCK:
        job = _JOBS.get(key)
        if job is None:
            return {"ok": False, "error": "no job to cancel"}
        if job.proc.poll() is not None:
            return {"ok": True, "already_finished": True}
        job.cancelled = True
        proc = job.proc
        
        # Immediately record cancellation in the database
        from muscat_db.database import save_job
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
        db_jobs = get_persisted_jobs()
        running_keys = {j["key"] for j in db_jobs if j["state"] == "running" and j["type"] == "photometry"}
        
        for key, job in _JOBS.items():
            db_key = f"photometry:{job.inst}/{job.date}/{job.target.replace(' ', '')}"
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

