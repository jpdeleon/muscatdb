# src/muscat_db/lco.py
"""
Helper module for interacting with the LCO API.
"""
from __future__ import annotations

import datetime
import concurrent.futures
import hashlib
import json
import math
import os
import re
import shutil
import socket
import subprocess
import urllib.error
import urllib.request
import urllib.parse
from pathlib import Path
import threading
import time
import uuid

# A frame filename / path segment: letters, digits and the punctuation LCO uses
# in archive names. Excludes "/" and "\" so a crafted payload can't traverse.
_SAFE_SEGMENT_RE = re.compile(r"^[A-Za-z0-9._+:\-]+$")
_DOWNLOAD_INSTRUMENT_DIRS = {
    "sinistro": "Sinistro",
    "muscat": "MuSCAT",
    "muscat2": "MuSCAT2",
    "muscat3": "MuSCAT3",
    "muscat4": "MuSCAT4",
}


class LcoError(Exception):
    """Structured LCO API error."""

    def __init__(self, message: str, status: int = 500, detail: str | None = None):
        self.message = message
        self.status = status
        self.detail = detail
        super().__init__(f"[{status}] {message}" + (f" - {detail}" if detail else ""))

    def to_dict(self) -> dict:
        return {"ok": False, "error": self.message, "detail": self.detail, "status": self.status}


def _get_lco_api_token() -> str:
    """Return the LCO API token from the environment, raising an error if absent."""
    token = os.environ.get("LCO_API_TOKEN")
    if not token:
        raise LcoError(
            "LCO_API_TOKEN is not configured",
            status=503,
            detail="The server is missing the LCO_API_TOKEN secret needed to make this call.",
        )
    return token


def config_state() -> dict:
    """Return the configuration state for LCO variables. No secrets exposed."""
    token_configured = bool(os.environ.get("LCO_API_TOKEN"))
    download_root_configured = bool(os.environ.get("MUSCAT_LCO_DIR"))
    submit_flag_enabled = os.environ.get("MUSCAT_LCO_ALLOW_SUBMIT") == "1"
    root = download_root()
    return {
        "token_configured": token_configured,
        "download_root_configured": download_root_configured,
        "download_root": str(root) if root else None,
        "submit_allowed": token_configured and download_root_configured and submit_flag_enabled,
    }


def _lco_api_request(url: str, method: str = "GET", data: dict | None = None) -> dict:
    """Make an authenticated request to the LCO API.

    Both the observation portal (observe.lco.global) and the Science Archive
    (archive-api.lco.global) authenticate with the same DRF token using the
    ``Token`` scheme. Using ``Bearer`` makes the archive return HTTP 401
    ``{"detail": "No Such User"}``.
    """
    token = _get_lco_api_token()
    headers = {"Authorization": "Token " + token, "Content-Type": "application/json"}
    body = json.dumps(data).encode("utf-8") if data is not None else None
    req = urllib.request.Request(url, data=body, headers=headers, method=method)

    try:
        with urllib.request.urlopen(req, timeout=15) as response:
            if 200 <= response.status < 300:
                return json.loads(response.read().decode())
            raise LcoError(
                f"LCO API returned HTTP {response.status}",
                status=response.status,
                detail=response.read().decode(),
            )
    except urllib.error.HTTPError as e:
        try:
            detail = e.read().decode()
        except Exception:
            detail = str(e)
        raise LcoError(f"LCO API request failed with HTTP {e.code}", status=e.code, detail=detail)
    except Exception as e:
        raise LcoError("LCO API request failed", detail=str(e))


def get_proposals() -> dict:
    """Fetch the current user's active proposals."""
    return _lco_api_request("https://observe.lco.global/api/proposals/?state=ACTIVE")


def get_requestgroups(proposal: str) -> dict:
    """Fetch request groups for a given proposal."""
    if not proposal:
        raise LcoError("Proposal ID is required", status=400)
    url = f"https://observe.lco.global/api/requestgroups/?proposal={urllib.parse.quote(proposal)}"
    return _lco_api_request(url)


def archive_search(filters: dict) -> dict:
    """Search the LCO archive."""
    base_url = "https://archive-api.lco.global/frames/"
    params = urllib.parse.urlencode({k: v for k, v in filters.items() if v})
    url = f"{base_url}?{params}"
    return _lco_api_request(url)


# Safety cap so a single request-id fetch can't spin forever paginating a
# pathologically large observation request.
_ARCHIVE_MAX_FRAMES = 10_000


def archive_search_all(filters: dict, max_frames: int = _ARCHIVE_MAX_FRAMES) -> dict:
    """Search the LCO archive, following pagination until exhausted or capped.

    The archive paginates ``frames/`` results (``next`` holds the fully-formed
    next-page URL, already carrying the same query params). A single observation
    request can span thousands of frames, so ``archive_search`` (one page) is not
    enough to pull a whole dataset by ``request_id``. Stops at ``max_frames`` and
    reports ``truncated`` so the caller can warn the user.
    """
    base_url = "https://archive-api.lco.global/frames/"
    params = urllib.parse.urlencode({k: v for k, v in filters.items() if v})
    url: str | None = f"{base_url}?{params}"
    results: list[dict] = []
    total: int | None = None
    while url and len(results) < max_frames:
        page = _lco_api_request(url)
        if total is None:
            total = page.get("count")
        results.extend(page.get("results") or [])
        url = page.get("next")
    truncated = bool(url) and len(results) >= max_frames
    return {"count": total, "results": results[:max_frames], "truncated": truncated}


def infer_archive_instrument(frame: dict) -> str:
    """Infer the muscat-db instrument name from LCO archive frame metadata."""
    site = str(frame.get("SITEID") or "").lower()
    tel = str(frame.get("TELID") or "").lower()
    instrume = str(frame.get("INSTRUME") or "").lower()
    filename = str(frame.get("filename") or frame.get("basename") or "").lower()

    if not site and filename:
        if filename.startswith("ogg"):
            site = "ogg"
        elif filename.startswith("coj"):
            site = "coj"
        elif filename.startswith(("lsc", "cpt", "tfn", "elp")):
            site = filename[:3]

    if not tel and filename:
        if "2m0" in filename:
            tel = "2m0"
        elif "1m0" in filename:
            tel = "1m0"

    if not instrume and filename:
        if "-ep" in filename or "muscat" in filename:
            instrume = "muscat"
        elif "-fa" in filename or "-kb" in filename or "sinistro" in filename:
            instrume = "sinistro"

    if site == "ogg" and tel.startswith("2m0") and ("muscat" in instrume or "ep" in instrume):
        return "muscat3"
    if site == "coj" and tel.startswith("2m0") and ("muscat" in instrume or "ep" in instrume):
        return "muscat4"
    if tel.startswith("1m0"):
        return "sinistro"

    raise LcoError(
        "Could not infer destination instrument",
        detail=f"site={site}, tel={tel}, instrume={instrume}, filename={filename}",
    )


def _safe_segment(value: str, kind: str) -> str:
    """Return *value* if it is a single safe path segment, else raise.

    Blocks the traversal vector where a crafted frame payload (filename,
    DATE_OBS-derived obsdate, ...) escapes the download root via ``/`` or ``..``.
    """
    v = (value or "").strip()
    if (
        not v
        or v in (".", "..")
        or "/" in v
        or "\\" in v
        or ".." in v
        or not _SAFE_SEGMENT_RE.match(v)
    ):
        raise LcoError(f"unsafe {kind}: {value!r}", status=400)
    return v


def download_root() -> Path | None:
    """Return the configured download root, or ``None`` if unset.

    Single source of truth for where archive frames land: ``MUSCAT_LCO_DIR``
    takes precedence, then ``MUSCAT_DATA_DIR``. Kept side-effect free (no raise)
    so callers that only want to *display* the location (config, UI hints) share
    the same resolution as the code that actually writes files.
    """
    lco_dir = os.environ.get("MUSCAT_LCO_DIR")
    if lco_dir:
        return Path(lco_dir)
    data_dir = os.environ.get("MUSCAT_DATA_DIR")
    if data_dir:
        return Path(data_dir)
    return None


def download_instrument_dir(instrument: str) -> str:
    """Return the case-sensitive archive-download directory for an instrument."""
    key = (instrument or "").strip().lower()
    return _DOWNLOAD_INSTRUMENT_DIRS.get(key, instrument)


def frame_dest(instrument: str, obsdate: str, filename: str) -> Path:
    """Return the destination path for a downloaded frame."""
    root = download_root()
    if root is None:
        raise LcoError("MUSCAT_LCO_DIR or MUSCAT_DATA_DIR must be set", status=503)
    # Validate every segment so a crafted frame payload can't traverse out of the
    # download root (arbitrary file write via urlretrieve). Confirm the resolved
    # path stays under the root as a final backstop.
    instrument = _safe_segment(download_instrument_dir(instrument), "instrument")
    obsdate = _safe_segment(obsdate, "obsdate")
    filename = _safe_segment(filename, "filename")
    root = root.resolve()
    dest = (root / instrument / obsdate / filename).resolve()
    try:
        dest.relative_to(root)
    except ValueError as exc:
        raise LcoError(f"unsafe frame path: {filename!r}", status=400) from exc
    return dest


def _validate_download_url(url: str) -> str:
    """Only allow fetching over https from the LCO archive or its S3 backing.

    The download endpoint hands the frame's ``url`` straight to ``urlretrieve``;
    without this an arbitrary URL (or a ``file://`` path) turns the endpoint into
    an SSRF / local-file-read primitive.
    """
    parsed = urllib.parse.urlparse(url or "")
    host = (parsed.hostname or "").lower()
    if parsed.scheme != "https" or not (
        host == "archive-api.lco.global"
        or host.endswith(".lco.global")
        or host.endswith(".amazonaws.com")
    ):
        raise LcoError("refusing to download from untrusted URL", status=400, detail=url)
    return url


# Per-frame download timeout (seconds), applied to each socket read. A stalled
# archive/S3 connection must fail fast rather than block the request thread — and
# under `serve --reload`, the whole server — indefinitely. Overridable via env
# for slow links or unusually large frames.
_DOWNLOAD_TIMEOUT_S = float(os.environ.get("MUSCAT_LCO_DOWNLOAD_TIMEOUT_S", "120"))
_DOWNLOAD_CHUNK = 1 << 20  # 1 MiB
_FUNPACK_TIMEOUT_S = float(os.environ.get("MUSCAT_LCO_FUNPACK_TIMEOUT_S", "300"))


def _download_to_file(url: str, dest: Path, timeout: float = _DOWNLOAD_TIMEOUT_S) -> None:
    """Stream *url* to *dest* atomically, with a per-read socket timeout.

    Writes to a sibling ``.part`` file and atomically renames on success so an
    interrupted or stalled download never leaves a truncated ``.fits.fz`` in
    place. ``timeout`` applies to each socket read, so a hung connection raises
    ``TimeoutError`` instead of blocking forever (the bug that wedged the server
    when a bare ``urlretrieve`` stalled mid-dataset).
    """
    tmp = dest.with_name(dest.name + ".part")
    try:
        with urllib.request.urlopen(url, timeout=timeout) as response:
            with open(tmp, "wb") as fh:
                shutil.copyfileobj(response, fh, _DOWNLOAD_CHUNK)
        tmp.replace(dest)
    except (urllib.error.URLError, socket.timeout, TimeoutError, OSError):
        # Drop the partial file so a retry starts clean; re-raise for the caller
        # to record as this frame's error without aborting the rest of the batch.
        tmp.unlink(missing_ok=True)
        raise
    finally:
        # Belt-and-suspenders: on success tmp was renamed away; on any exit path
        # ensure no stray .part lingers.
        tmp.unlink(missing_ok=True)


def _download_frame(frame: dict, overwrite: bool = False) -> dict:
    filename = frame.get("filename") or frame.get("basename")
    if not filename:
        return {"filename": "unknown", "status": "error", "error": "missing filename"}

    status = {"filename": filename, "status": "pending"}
    try:
        instrument = infer_archive_instrument(frame)
        date_obs = (frame.get("DATE_OBS") or frame.get("DAY_OBS") or "").split("T")[0].replace("-", "")
        if len(date_obs) >= 6:
            obsdate = date_obs[2:]
        else:
            raise LcoError("Could not determine obsdate")

        dest = frame_dest(instrument, obsdate, filename)
        status["dest"] = str(dest)

        if dest.exists() and not overwrite:
            status["status"] = "exists"
            return status

        dest.parent.mkdir(parents=True, exist_ok=True)

        url = frame.get("url")
        if not url:
            status["status"] = "error"
            status["error"] = "missing download url"
            return status

        _validate_download_url(url)
        _download_to_file(url, dest)
        status["status"] = "downloaded"

    except LcoError as e:
        status["status"] = "error"
        status["error"] = e.message
    except Exception as e:
        status["status"] = "error"
        status["error"] = str(e)
    return status


def download_frames(frames: list[dict], overwrite: bool = False) -> list[dict]:
    """Download frames from the LCO archive."""
    return [_download_frame(frame, overwrite=overwrite) for frame in frames]


def _funpack_dest(path: Path) -> Path | None:
    if path.name.endswith(".fits.fz"):
        return path.with_name(path.name[:-3])
    if path.name.endswith(".fz"):
        return path.with_name(path.name[:-3])
    return None


def _funpack_file(path: Path, timeout: float = _FUNPACK_TIMEOUT_S) -> dict:
    out = _funpack_dest(path)
    status = {
        "filename": path.name,
        "src": str(path),
        "dest": str(out) if out else "",
        "status": "pending",
    }
    if out is None:
        status["status"] = "skipped"
        status["error"] = "not an fpacked FITS filename"
        return status
    if out.exists():
        status["status"] = "exists"
        return status
    funpack = shutil.which("funpack")
    if not funpack:
        status["status"] = "error"
        status["error"] = "funpack is not installed"
        return status
    try:
        proc = subprocess.run(
            [funpack, "-O", str(out), str(path)],
            check=False,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except OSError as exc:
        status["status"] = "error"
        status["error"] = str(exc)
        return status
    except subprocess.TimeoutExpired:
        status["status"] = "error"
        status["error"] = f"funpack timed out after {timeout:g}s"
        return status
    if proc.returncode != 0:
        status["status"] = "error"
        status["error"] = (proc.stderr or proc.stdout or f"funpack exited {proc.returncode}").strip()
        return status
    status["status"] = "unpacked"
    return status


def _funpack_paths(results: list[dict]) -> list[Path]:
    paths = []
    seen: set[str] = set()
    for result in results:
        if result.get("status") not in {"downloaded", "exists"}:
            continue
        dest = result.get("dest")
        if not dest:
            continue
        path = Path(dest)
        if str(path) in seen:
            continue
        seen.add(str(path))
        if path.name.endswith(".fz"):
            paths.append(path)
    return paths


def _funpack_download_results(results: list[dict]) -> list[dict]:
    return [_funpack_file(path) for path in _funpack_paths(results)]


_ARCHIVE_DOWNLOAD_WORKERS = max(1, int(os.environ.get("MUSCAT_LCO_ARCHIVE_DOWNLOAD_WORKERS", "1")))
_ARCHIVE_DOWNLOAD_FRAME_WORKERS = max(1, int(os.environ.get("MUSCAT_LCO_ARCHIVE_DOWNLOAD_FRAME_WORKERS", "8")))
_ARCHIVE_FUNPACK_WORKERS = max(1, int(os.environ.get("MUSCAT_LCO_ARCHIVE_FUNPACK_WORKERS", "2")))
_ARCHIVE_DOWNLOAD_JOB_TTL_S = max(60, int(os.environ.get("MUSCAT_LCO_ARCHIVE_DOWNLOAD_JOB_TTL_S", "86400")))
_ARCHIVE_DOWNLOAD_MAX_JOBS = max(10, int(os.environ.get("MUSCAT_LCO_ARCHIVE_DOWNLOAD_MAX_JOBS", "200")))
_ARCHIVE_DOWNLOAD_EXECUTOR = concurrent.futures.ThreadPoolExecutor(
    max_workers=_ARCHIVE_DOWNLOAD_WORKERS,
    thread_name_prefix="lco-archive-download",
)
_ARCHIVE_DOWNLOAD_LOCK = threading.Lock()
_ARCHIVE_DOWNLOAD_JOBS: dict[str, dict] = {}


def _archive_download_snapshot(job: dict) -> dict:
    frames = list(job["frames"])
    results = [dict(r) for r in job["results"]]
    funpack_results = [dict(r) for r in job.get("funpack_results", [])]
    instruments: list[str] = []
    obsdates: list[str] = []
    objects: list[str] = []
    dest_dirs: list[str] = []

    def add_unique(values: list[str], value: str | None) -> None:
        if value and value not in values:
            values.append(value)

    for frame in frames:
        add_unique(objects, str(frame.get("OBJECT") or frame.get("object") or "").strip())
        try:
            inst = infer_archive_instrument(frame)
            add_unique(instruments, inst)
            date_obs = (frame.get("DATE_OBS") or frame.get("DAY_OBS") or "").split("T")[0].replace("-", "")
            if len(date_obs) >= 6:
                obsdate = date_obs[2:]
                add_unique(obsdates, obsdate)
                filename = frame.get("filename") or frame.get("basename")
                if filename:
                    add_unique(dest_dirs, str(frame_dest(inst, obsdate, filename).parent))
        except Exception:
            pass

    for result in results:
        dest = result.get("dest")
        if dest:
            add_unique(dest_dirs, str(Path(dest).parent))

    return {
        "job_id": job["job_id"],
        "state": job["state"],
        "frames_total": job["frames_total"],
        "frames_done": len(results),
        "results": results,
        "phase": job.get("phase", "pending"),
        "funpack_total": job.get("funpack_total", 0),
        "funpack_done": len(funpack_results),
        "funpack_results": funpack_results,
        "instruments": instruments,
        "obsdates": obsdates,
        "objects": objects,
        "dest_dirs": dest_dirs,
        "started_at": job["started_at"],
        "finished_at": job.get("finished_at"),
        "error": job.get("error"),
    }


def _prune_archive_download_jobs(now: float | None = None, reserve_slots: int = 0) -> None:
    now = now if now is not None else time.time()
    finished = [
        (jid, job.get("finished_at") or 0)
        for jid, job in _ARCHIVE_DOWNLOAD_JOBS.items()
        if job["state"] in {"done", "error"}
    ]
    for jid, finished_at in finished:
        if finished_at and now - finished_at > _ARCHIVE_DOWNLOAD_JOB_TTL_S:
            _ARCHIVE_DOWNLOAD_JOBS.pop(jid, None)

    target_size = max(0, _ARCHIVE_DOWNLOAD_MAX_JOBS - reserve_slots)
    overflow = len(_ARCHIVE_DOWNLOAD_JOBS) - target_size
    if overflow > 0:
        finished = [
            (jid, job.get("finished_at") or 0)
            for jid, job in _ARCHIVE_DOWNLOAD_JOBS.items()
            if job["state"] in {"done", "error"}
        ]
        for jid, _finished_at in sorted(finished, key=lambda item: item[1])[:overflow]:
            _ARCHIVE_DOWNLOAD_JOBS.pop(jid, None)


def _run_archive_download_job(job_id: str) -> None:
    with _ARCHIVE_DOWNLOAD_LOCK:
        job = _ARCHIVE_DOWNLOAD_JOBS.get(job_id)
        if job is None:
            return
        job["state"] = "running"
        job["phase"] = "downloading"
        frames = list(job["frames"])
        overwrite = bool(job["overwrite"])

    try:
        max_workers = min(_ARCHIVE_DOWNLOAD_FRAME_WORKERS, len(frames))
        with concurrent.futures.ThreadPoolExecutor(
            max_workers=max_workers,
            thread_name_prefix=f"lco-archive-frame-{job_id}",
        ) as pool:
            futures = [pool.submit(_download_frame, frame, overwrite=overwrite) for frame in frames]
            for future in concurrent.futures.as_completed(futures):
                result = future.result()
                with _ARCHIVE_DOWNLOAD_LOCK:
                    current = _ARCHIVE_DOWNLOAD_JOBS.get(job_id)
                    if current is None:
                        return
                    current["results"].append(result)
        with _ARCHIVE_DOWNLOAD_LOCK:
            current = _ARCHIVE_DOWNLOAD_JOBS.get(job_id)
            if current is None:
                return
            current["phase"] = "funpacking"
            results = [dict(r) for r in current["results"]]
            funpack_paths = _funpack_paths(results)
            current["funpack_total"] = len(funpack_paths)
        funpack_failed = False
        if funpack_paths:
            max_workers = min(_ARCHIVE_FUNPACK_WORKERS, len(funpack_paths))
            with concurrent.futures.ThreadPoolExecutor(
                max_workers=max_workers,
                thread_name_prefix=f"lco-archive-funpack-{job_id}",
            ) as pool:
                futures = {pool.submit(_funpack_file, path): path for path in funpack_paths}
                for future in concurrent.futures.as_completed(futures):
                    path = futures[future]
                    try:
                        result = future.result()
                    except Exception as exc:
                        result = {
                            "filename": path.name,
                            "src": str(path),
                            "dest": str(_funpack_dest(path) or ""),
                            "status": "error",
                            "error": str(exc),
                        }
                    if result.get("status") == "error":
                        funpack_failed = True
                    with _ARCHIVE_DOWNLOAD_LOCK:
                        current = _ARCHIVE_DOWNLOAD_JOBS.get(job_id)
                        if current is None:
                            return
                        current["funpack_results"].append(result)
        with _ARCHIVE_DOWNLOAD_LOCK:
            current = _ARCHIVE_DOWNLOAD_JOBS.get(job_id)
            if current is not None:
                current["phase"] = "done"
                current["state"] = "error" if funpack_failed else "done"
                if funpack_failed:
                    current["error"] = "One or more funpack commands failed"
                current["finished_at"] = time.time()
                _prune_archive_download_jobs(current["finished_at"])
    except Exception as exc:
        with _ARCHIVE_DOWNLOAD_LOCK:
            current = _ARCHIVE_DOWNLOAD_JOBS.get(job_id)
            if current is not None:
                current["state"] = "error"
                current["error"] = str(exc)
                current["finished_at"] = time.time()
                _prune_archive_download_jobs(current["finished_at"])


def start_archive_download(frames: list[dict], overwrite: bool = False) -> dict:
    """Queue an LCO archive download in a dedicated worker and return its state."""
    if not isinstance(frames, list) or not frames:
        raise LcoError("no frames selected", status=400)
    job_id = uuid.uuid4().hex[:16]
    now = time.time()
    job = {
        "job_id": job_id,
        "state": "pending",
        "frames": [dict(frame) for frame in frames],
        "frames_total": len(frames),
        "overwrite": overwrite,
        "results": [],
        "funpack_results": [],
        "funpack_total": 0,
        "phase": "pending",
        "started_at": now,
        "finished_at": None,
        "error": None,
    }
    with _ARCHIVE_DOWNLOAD_LOCK:
        _prune_archive_download_jobs(now, reserve_slots=1)
        if len(_ARCHIVE_DOWNLOAD_JOBS) >= _ARCHIVE_DOWNLOAD_MAX_JOBS:
            raise LcoError(
                "Too many LCO archive download jobs are queued",
                status=429,
                detail=(
                    f"At most {_ARCHIVE_DOWNLOAD_MAX_JOBS} archive download jobs are tracked "
                    "in this server process. Wait for queued jobs to finish before submitting more."
                ),
            )
        _ARCHIVE_DOWNLOAD_JOBS[job_id] = job
        snapshot = _archive_download_snapshot(job)
    _ARCHIVE_DOWNLOAD_EXECUTOR.submit(_run_archive_download_job, job_id)
    return snapshot


def archive_download_status(job_id: str) -> dict:
    """Return the current state for a queued archive-download job."""
    with _ARCHIVE_DOWNLOAD_LOCK:
        _prune_archive_download_jobs()
        job = _ARCHIVE_DOWNLOAD_JOBS.get(job_id)
        if job is None:
            raise LcoError("LCO archive download job not found", status=404)
        return _archive_download_snapshot(job)


def archive_download_jobs() -> list[dict]:
    """Return LCO archive-download jobs known to this server process."""
    with _ARCHIVE_DOWNLOAD_LOCK:
        _prune_archive_download_jobs()
        jobs = [_archive_download_snapshot(job) for job in _ARCHIVE_DOWNLOAD_JOBS.values()]
    jobs.sort(key=lambda job: job.get("started_at") or 0, reverse=True)
    return jobs


def generate_windows(t0: float, period: float, duration_h: float, start_dt: str, end_dt: str, pad_before_min: float, pad_after_min: float) -> list[dict]:
    """Generate transit windows within a date range.

    Epochs are normalized to the first transit within the date range for clarity
    (epoch 0 = first transit in the range, not absolute count from t0).
    """
    if not all([start_dt, end_dt]):
        raise LcoError("Date range is required", status=400)

    start = datetime.datetime.fromisoformat(start_dt + "T00:00:00").replace(tzinfo=datetime.timezone.utc)
    end = datetime.datetime.fromisoformat(end_dt + "T23:59:59").replace(tzinfo=datetime.timezone.utc)

    # JD for Unix epoch is 2440587.5. BJD is close enough for this purpose.
    t0_dt = datetime.datetime.fromtimestamp((t0 - 2440587.5) * 86400, tz=datetime.timezone.utc)

    epoch_at_start = math.floor((start - t0_dt).total_seconds() / (period * 86400.0))

    windows = []
    current_epoch = epoch_at_start
    relative_epoch = 0  # Reset to 0 for the first window in range
    first_in_range = True

    while True:
        mid_bjd = t0 + current_epoch * period
        # Recalculate mid_dt from BJD each time to avoid float drift
        mid_dt = datetime.datetime.fromtimestamp((mid_bjd - 2440587.5) * 86400, tz=datetime.timezone.utc)

        if mid_dt > end:
            break

        if mid_dt >= start:
            if first_in_range:
                relative_epoch = current_epoch  # Store absolute epoch for first transit
                first_in_range = False

            start_obs = mid_dt - datetime.timedelta(hours=duration_h / 2.0, minutes=pad_before_min)
            end_obs = mid_dt + datetime.timedelta(hours=duration_h / 2.0, minutes=pad_after_min)

            windows.append({
                "epoch": int(current_epoch - relative_epoch),  # Display relative epoch (0-indexed)
                "epoch_abs": int(current_epoch),  # Store absolute epoch for reference
                "mid_bjd": mid_bjd,
                "mid": mid_dt.isoformat().replace("+00:00", "Z"),
                "start": start_obs.isoformat().replace("+00:00", "Z"),
                "end": end_obs.isoformat().replace("+00:00", "Z"),
            })

        current_epoch += 1
        if len(windows) > 1000: # safety break
             break

    return windows

def payload_hash(payload: dict) -> str:
    """Create a stable hash of the requestgroup payload."""
    # Serialize with sorted keys to ensure a consistent hash
    s = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(s.encode("utf-8")).hexdigest()

def build_requestgroup(kind: str, params: dict) -> dict:
    """Construct the requestgroup payload for an observation."""
    if not all(params.get(k) for k in ["name", "proposal", "target_name", "ra", "dec"]):
        raise LcoError("Missing required scheduling parameters", status=400)

    target = {
        "name": params["target_name"],
        "type": "ICRS",
        "ra": params["ra"],
        "dec": params["dec"],
    }

    # These constraints are defined at the request level, but get copied into
    # the configuration level by this function, as per the LCO examples.
    
    # Set default airmass and lunar distance based on instrument kind
    if kind in ("muscat", "muscat3", "muscat4"):
        default_max_airmass = 2.5
        default_min_lunar_distance = 18
    else:
        default_max_airmass = 1.6
        default_min_lunar_distance = 30

    constraints = {
        "max_airmass": params.get("max_airmass", default_max_airmass),
        "min_lunar_distance": params.get("min_lunar_distance", default_min_lunar_distance),
    }

    configurations = []
    instrument_type = ""
    if kind in ("muscat", "muscat3", "muscat4"):
        if not params.get("exposure_times"):
            raise LcoError("Exposure times are required for MuSCAT instruments", status=400)
        
        # For MuSCAT, one instrument_config is created per filter.
        instrument_configs = [
            {
                "exposure_time": params["exposure_times"].get(b, 0),
                "exposure_count": params.get("exposure_count", 1),
                "mode": params.get("readout_mode", "MUSCAT_FAST"),
                "optical_elements": {
                    "filter": b,
                    "narrowband_g_position": params.get("narrowband", {}).get("g", "out"),
                    "narrowband_i_position": params.get("narrowband", {}).get("i", "out"),
                    "narrowband_r_position": params.get("narrowband", {}).get("r", "out"),
                    "narrowband_z_position": params.get("narrowband", {}).get("z", "out"),
                },
                "extra_params": {
                    "exposure_mode": params.get("exposure_mode", "ASYNCHRONOUS"),
                    "exposure_time_g": params["exposure_times"].get("g", 0),
                    "exposure_time_i": params["exposure_times"].get("i", 0),
                    "exposure_time_r": params["exposure_times"].get("r", 0),
                    "exposure_time_z": params["exposure_times"].get("z", 0),
                },
            } for b in ["g", "r", "i", "z"] if params["exposure_times"].get(b, 0) > 0
        ]
        instrument_type = "2M0-SCICAM-MUSCAT"
        configurations.append({
            "type": params.get("type", "REPEAT_EXPOSE"),
            "repeat_duration": params.get("repeat_duration"),
            "instrument_type": instrument_type,
            "instrument_configs": instrument_configs,
            "acquisition_config": {"mode": "WCS"},
            "guiding_config": {"mode": params.get("guiding_config", "ON"), "optional": True},
            "constraints": {
                "max_airmass": params.get("max_airmass", 2.5),
                "min_lunar_distance": params.get("min_lunar_distance", 18),
                "max_seeing": params.get("max_seeing"),
                "min_transparency": params.get("min_transparency"),
                "extra_params": {}
            },
            "target": target
        })
    elif kind == "sinistro":
        mode = params.get("readout_mode", "central_2k_2x2")
        binning = 2 if "2x2" in mode else 1
        instrument_configs = [{
            "exposure_count": params.get("exposure_count", 1),
            "exposure_time": params.get("exposure_time", 60),
            "mode": mode,
            "optical_elements": {"filter": params.get("filter", "rp")},
            "extra_params": {
                "bin_x": binning,
                "bin_y": binning,
                "offset_ra": 0,
                "offset_dec": 0
            }
        }]
        instrument_type = "1M0-SCICAM-SINISTRO"
        configurations.append({
            "type": params.get("type", "EXPOSE"),
            "instrument_type": instrument_type,
            "instrument_configs": instrument_configs,
            "acquisition_config": {"mode": "OFF"},
            "guiding_config": {"mode": params.get("guiding_config", "ON"), "optional": True},
            "constraints": constraints,
            "target": target
        })
    else:
        raise LcoError(f"Unsupported instrument kind for scheduling: {kind}", status=400)

    location = {}
    if params.get("site"):
        location["telescope_class"] = "1m0" if kind == "sinistro" else "2m0"
        location["site"] = params["site"]
    
    obs_type = "NORMAL"

    return {
        "name": params["name"],
        "proposal": params["proposal"],
        "ipp_value": params.get("ipp_value", 1.0),
        "operator": "SINGLE",
        "observation_type": params.get("observation_type", obs_type),
        "requests": [{
            "target": target,
            "constraints": constraints,
            "location": location,
            "windows": params.get("windows", []),
            "instrument_type": instrument_type,
            "configurations": configurations,
        }]
    }

def max_allowable_ipp(request_group: dict) -> dict:
    """Run the max-allowable-IPP dry-run."""
    url = "https://observe.lco.global/api/requestgroups/max_allowable_ipp/"
    return _lco_api_request(url, method="POST", data=request_group)

def submit_requestgroup(request_group: dict) -> dict:
    """Submit a live observation request."""
    if os.environ.get("MUSCAT_LCO_ALLOW_SUBMIT") != "1":
        raise LcoError(
            "Live submission is disabled on the server",
            status=403,
            detail="To enable, set MUSCAT_LCO_ALLOW_SUBMIT=1 in the server environment.",
        )
    url = "https://observe.lco.global/api/requestgroups/"
    return _lco_api_request(url, method="POST", data=request_group)
