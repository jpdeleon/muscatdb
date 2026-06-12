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
    "glob": "*.fits",
    "gif_stride": 100,
    "make_gif": True,
    "plot_gaia_sources": True,
    "use_barycorrpy": False,
    "test_run_frames": 10,
    "min_star_separation": 10.0,
    "max_num_stars": 10,
    "n_stars_align": "",       # "" -> same as max_num_stars
    "cutout_size": 35,
    "ccd_trim": "10,10",       # "Y,X"
    "bin_size_minutes": 10.0,
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
    base = output_base().resolve()
    candidate = (base / inst / date / name).resolve()
    try:
        candidate.relative_to(base)
    except ValueError:
        return None
    return candidate if candidate.is_file() else None


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

    for key in ("ref_band", "aper_radii", "annulus", "aper_unit", "glob", "ccd_trim"):
        if raw.get(key) is not None:
            o[key] = str(raw[key]).strip()

    for key in ("refid", "n_stars_align"):  # optional ints; "" keeps default
        if key in raw:
            val = str(raw.get(key, "")).strip()
            o[key] = "" if val == "" else (_to_int(val) if _to_int(val) is not None else "")

    for key in ("gif_stride", "test_run_frames", "max_num_stars", "cutout_size"):
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

    if o.get("glob", "*.fits") not in ("", "*.fits"):
        args += ["--glob", o["glob"]]

    # Numeric overrides: only emit when the user changed them from the default.
    for flag, key in (
        ("--gif_stride", "gif_stride"),
        ("--test_run_frames", "test_run_frames"),
        ("--min_star_separation", "min_star_separation"),
        ("--max_num_stars", "max_num_stars"),
        ("--cutout_size", "cutout_size"),
        ("--bin_size_minutes", "bin_size_minutes"),
    ):
        val = o.get(key)
        if val in (None, ""):
            continue
        if float(val) != float(RUN_DEFAULTS[key]):
            args += [flag, str(val)]
    if o.get("n_stars_align") not in (None, ""):
        args += ["--n_stars_align", str(o["n_stars_align"])]
    if (o.get("ccd_trim") or "").replace(" ", "") not in ("", "10,10"):
        args += ["--ccd_trim", o["ccd_trim"].replace(" ", "")]

    if not o.get("make_gif", True):
        args.append("--no_gif")
    if not o.get("plot_gaia_sources", True):
        args.append("--no_plot_gaia_sources")
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
                try:
                    job.logf.close()
                except OSError:
                    pass
        log_path = job.log_path
        elapsed = time.time() - job.started_at
    return {
        "state": state,
        "returncode": rc,
        "log": _tail(log_path),
        "elapsed": round(elapsed),
    }


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

    try:
        os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
    except (ProcessLookupError, PermissionError, OSError):
        try:
            proc.terminate()
        except OSError:
            pass
    threading.Thread(target=_kill_after, args=(proc,), daemon=True).start()
    return {"ok": True, "key": key}
