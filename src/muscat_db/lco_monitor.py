"""Durable monitoring for observation requests submitted through the LCO UI.

Each accepted child Request is persisted in SQLite.  A single lease-owning
worker polls request-scoped LCO endpoints, downloads new final BANZAI products
as they appear, and scans/ingests the affected instrument nights once every raw
science frame has a corresponding final product.
"""

from __future__ import annotations

import json
import logging
import os
import socket
import threading
import time
import uuid
from pathlib import Path
from typing import Any

from muscat_db import lco
from muscat_db.database import db_path, get_conn

logger = logging.getLogger(__name__)

TERMINAL_REQUEST_STATES = {"COMPLETED", "WINDOW_EXPIRED", "CANCELED"}
TERMINAL_MONITOR_STATES = {"complete", "terminal_no_data"}

_POLL_S = max(60.0, float(os.environ.get("MUSCAT_LCO_MONITOR_POLL_S", "300")))
_FAST_POLL_AFTER_WINDOW_S = max(
    0.0, float(os.environ.get("MUSCAT_LCO_MONITOR_FAST_AFTER_WINDOW_S", "7200"))
)
_MAX_POLL_S = max(_POLL_S, float(os.environ.get("MUSCAT_LCO_MONITOR_MAX_POLL_S", "3600")))
_ERROR_MAX_POLL_S = max(
    _POLL_S, float(os.environ.get("MUSCAT_LCO_MONITOR_ERROR_MAX_POLL_S", "3600"))
)
_LOOP_S = max(2.0, float(os.environ.get("MUSCAT_LCO_MONITOR_LOOP_S", "15")))
_BATCH_SIZE = max(1, int(os.environ.get("MUSCAT_LCO_MONITOR_BATCH_SIZE", "2")))
_DOWNLOAD_CHECK_S = max(2.0, float(os.environ.get("MUSCAT_LCO_MONITOR_DOWNLOAD_CHECK_S", "10")))
_NO_DATA_GRACE_S = max(300.0, float(os.environ.get("MUSCAT_LCO_MONITOR_NO_DATA_GRACE_S", "86400")))
_LEASE_S = max(30.0, float(os.environ.get("MUSCAT_LCO_MONITOR_LEASE_S", "90")))
_SCAN_WORKERS = max(1, int(os.environ.get("MUSCAT_LCO_MONITOR_SCAN_WORKERS", "1")))


def _json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)


def _iso_timestamp(value: Any) -> float | None:
    if not value:
        return None
    import datetime

    try:
        parsed = datetime.datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=datetime.timezone.utc)
    return parsed.timestamp()


def _request_parts(result: dict, payload: dict) -> list[tuple[int, dict, dict]]:
    """Return ``(request_id, response child, submitted child)`` tuples."""
    response_children = result.get("requests") or []
    submitted_children = payload.get("requests") or []
    parts: list[tuple[int, dict, dict]] = []
    for index, child in enumerate(response_children):
        response_child = child if isinstance(child, dict) else {"id": child}
        identifier = response_child.get("id")
        if identifier is None:
            continue
        submitted_child = (
            submitted_children[index]
            if index < len(submitted_children) and isinstance(submitted_children[index], dict)
            else {}
        )
        parts.append((int(identifier), response_child, submitted_child))
    return parts


def _request_windows(response_child: dict, submitted_child: dict, payload: dict) -> list[dict]:
    windows = (
        response_child.get("windows")
        or submitted_child.get("windows")
        or payload.get("windows")
        or []
    )
    return [window for window in windows if isinstance(window, dict)]


def _next_initial_poll(windows: list[dict], now: float) -> float:
    starts = [
        stamp for window in windows if (stamp := _iso_timestamp(window.get("start"))) is not None
    ]
    if starts and min(starts) > now:
        return min(starts)
    return now + min(_POLL_S, 60.0)


def record_submission(
    result: dict,
    payload: dict,
    user_name: str | None,
    *,
    path: str | None = None,
    now: float | None = None,
) -> list[dict]:
    """Persist every child Request from a successful LCO submission response."""
    now = time.time() if now is None else now
    requestgroup_id = result.get("id")
    if requestgroup_id is None:
        raise ValueError("LCO submission response has no request-group ID")
    parts = _request_parts(result, payload)
    if not parts:
        raise ValueError("LCO submission response has no child request IDs")

    clean_payload = {
        key: value
        for key, value in payload.items()
        if key not in {"confirm", "dry_run_hash", "dry_run_hash_a", "dry_run_hash_b"}
    }
    rows: list[dict] = []
    with get_conn(path or db_path(), row_factory=None) as conn:
        for request_id, response_child, submitted_child in parts:
            windows = _request_windows(response_child, submitted_child, clean_payload)
            starts = [str(w.get("start") or "") for w in windows if w.get("start")]
            ends = [str(w.get("end") or "") for w in windows if w.get("end")]
            target = clean_payload.get("target_name") or ""
            if not target:
                configurations = (
                    response_child.get("configurations")
                    or submitted_child.get("configurations")
                    or []
                )
                if configurations and isinstance(configurations[0], dict):
                    target = (configurations[0].get("target") or {}).get("name") or ""
            conn.execute(
                """
                INSERT INTO lco_observation_requests (
                    request_id, requestgroup_id, name, proposal, target, instrument,
                    user_name, request_state, monitor_state, window_start, window_end,
                    payload_json, result_json, next_poll_at, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'monitoring', ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(request_id) DO UPDATE SET
                    requestgroup_id=excluded.requestgroup_id,
                    result_json=excluded.result_json,
                    updated_at=excluded.updated_at
                """,
                (
                    request_id,
                    int(requestgroup_id),
                    str(result.get("name") or clean_payload.get("name") or ""),
                    str(result.get("proposal") or clean_payload.get("proposal") or ""),
                    str(target),
                    str(clean_payload.get("kind") or ""),
                    str(user_name or ""),
                    str(response_child.get("state") or result.get("state") or "PENDING").upper(),
                    min(starts) if starts else "",
                    max(ends) if ends else "",
                    _json(clean_payload),
                    _json(result),
                    _next_initial_poll(windows, now),
                    now,
                    now,
                ),
            )
            row = conn.execute(
                "SELECT * FROM lco_observation_requests WHERE request_id=?", (request_id,)
            ).fetchone()
            columns = [
                description[0]
                for description in conn.execute(
                    "SELECT * FROM lco_observation_requests LIMIT 0"
                ).description
            ]
            rows.append(dict(zip(columns, row)))
        conn.commit()
    return rows


def list_requests(path: str | None = None, limit: int = 200) -> list[dict]:
    with get_conn(path or db_path(), row_factory=None) as conn:
        cursor = conn.execute(
            """SELECT request_id,requestgroup_id,name,proposal,target,instrument,user_name,
                      request_state,monitor_state,window_start,window_end,raw_frame_count,
                      reduced_frame_count,downloaded_count,next_poll_at,error_count,last_error,
                      last_polled_at,created_at,updated_at,completed_at
               FROM lco_observation_requests ORDER BY created_at DESC LIMIT ?""",
            (max(1, min(int(limit), 1000)),),
        )
        columns = [description[0] for description in cursor.description]
        return [dict(zip(columns, row)) for row in cursor.fetchall()]


def _frame_identity(frame: dict) -> str:
    basename = str(frame.get("basename") or frame.get("filename") or "")
    if basename:
        for suffix in (".fits.fz", ".fits", ".fz"):
            if basename.lower().endswith(suffix):
                basename = basename[: -len(suffix)]
                break
        import re

        # Raw and final products retain the same camera/date/frame stem. This
        # intentionally includes the camera token (ep02/ep03/...), because the
        # four simultaneous MuSCAT products can share one LCO observation_id.
        return re.sub(r"-e\d+$", "", basename, flags=re.IGNORECASE)

    observation_id = frame.get("observation_id") or frame.get("OBSID")
    return f"observation:{observation_id}" if observation_id not in (None, "") else ""


def _frame_key(frame: dict) -> str:
    identifier = frame.get("id")
    if identifier not in (None, ""):
        return str(identifier)
    return str(frame.get("filename") or frame.get("basename") or _frame_identity(frame))


def _upsert_reduced_frames(conn, request_id: int, frames: list[dict], now: float) -> None:
    for frame in frames:
        instrument, obsdate, _dest = lco.frame_destination(frame)
        filename = str(frame.get("filename") or frame.get("basename") or "")
        conn.execute(
            """
            INSERT INTO lco_observation_frames (
                request_id, frame_id, observation_id, filename, instrument,
                obsdate, state, metadata_json, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, 'pending', ?, ?)
            ON CONFLICT(request_id, frame_id) DO UPDATE SET
                metadata_json=excluded.metadata_json,
                updated_at=excluded.updated_at
            """,
            (
                request_id,
                _frame_key(frame),
                str(frame.get("observation_id") or frame.get("OBSID") or ""),
                filename,
                instrument,
                obsdate,
                _json(frame),
                now,
            ),
        )


def _request_child(group: dict, request_id: int) -> dict:
    for child in group.get("requests") or []:
        if isinstance(child, dict) and str(child.get("id")) == str(request_id):
            return child
    return {}


def _poll_delay(unchanged_polls: int, *, window_end: str = "", now: float | None = None) -> float:
    window_end_at = _iso_timestamp(window_end)
    if (
        window_end_at is not None
        and (time.time() if now is None else now) <= window_end_at + _FAST_POLL_AFTER_WINDOW_S
    ):
        return _POLL_S
    exponent = min(max(unchanged_polls, 0), 4)
    return min(_MAX_POLL_S, _POLL_S * (2**exponent))


def _error_delay(error_count: int) -> float:
    exponent = min(max(error_count - 1, 0), 6)
    return min(_ERROR_MAX_POLL_S, _POLL_S * (2**exponent))


def _download_rows(conn, request_id: int) -> list[dict]:
    cursor = conn.execute(
        "SELECT frame_id, metadata_json FROM lco_observation_frames "
        "WHERE request_id=? AND state IN ('pending','error') ORDER BY filename",
        (request_id,),
    )
    return [{"frame_id": row[0], "metadata": json.loads(row[1])} for row in cursor.fetchall()]


def _prepared_downloads(snapshot: dict) -> tuple[set[str], dict[str, str]]:
    download_results = {
        str(row.get("filename") or ""): row for row in snapshot.get("results") or []
    }
    funpack_results = {
        str(row.get("filename") or ""): row for row in snapshot.get("funpack_results") or []
    }
    ready: set[str] = set()
    errors: dict[str, str] = {}
    for filename, result in download_results.items():
        if result.get("status") not in {"downloaded", "exists"}:
            errors[filename] = str(result.get("error") or "download failed")
            continue
        dest = str(result.get("dest") or "")
        if dest.endswith(".fz"):
            unpack = funpack_results.get(Path(dest).name)
            if not unpack or unpack.get("status") not in {"unpacked", "exists"}:
                errors[filename] = str((unpack or {}).get("error") or "funpack did not complete")
                continue
        ready.add(filename)
    return ready, errors


def _finish_download(request: dict, snapshot: dict, *, path: str, now: float) -> bool:
    ready, errors = _prepared_downloads(snapshot)
    with get_conn(path) as conn:
        for filename in ready:
            conn.execute(
                "UPDATE lco_observation_frames SET state='downloaded', error='', updated_at=? "
                "WHERE request_id=? AND filename=?",
                (now, request["request_id"], filename),
            )
        for filename, error in errors.items():
            conn.execute(
                "UPDATE lco_observation_frames SET state='error', error=?, updated_at=? "
                "WHERE request_id=? AND filename=?",
                (error, now, request["request_id"], filename),
            )
        downloaded = conn.execute(
            "SELECT COUNT(*) FROM lco_observation_frames WHERE request_id=? AND state='downloaded'",
            (request["request_id"],),
        ).fetchone()[0]
        conn.execute(
            "UPDATE lco_observation_requests SET download_job_id='', monitor_state='monitoring', "
            "downloaded_count=?, next_poll_at=?, updated_at=?, last_error=? WHERE request_id=?",
            (
                downloaded,
                now if not errors else now + _error_delay(int(request.get("error_count") or 0) + 1),
                now,
                "; ".join(sorted(set(errors.values())))[:2000],
                request["request_id"],
            ),
        )
        conn.commit()
    return not errors


def _process_datasets(request_id: int, *, path: str, now: float) -> None:
    from muscat_db.database import ingest_date
    from muscat_db.scanner import scan_date

    with get_conn(path) as conn:
        datasets = conn.execute(
            "SELECT DISTINCT instrument, obsdate FROM lco_observation_frames "
            "WHERE request_id=? AND state='downloaded' ORDER BY instrument, obsdate",
            (request_id,),
        ).fetchall()
        conn.execute(
            "UPDATE lco_observation_requests SET monitor_state='processing', updated_at=? WHERE request_id=?",
            (now, request_id),
        )
        conn.commit()

    root = lco.download_root()
    if root is None:
        raise RuntimeError("MUSCAT_LCO_DIR or MUSCAT_DATA_DIR must be configured")
    for instrument, obsdate in datasets:
        result = scan_date(
            instrument,
            obsdate,
            max_workers=_SCAN_WORKERS,
            data_root=str(root),
        )
        if not result or not result.get("total"):
            raise RuntimeError(f"scan found no reduced FITS files for {instrument} {obsdate}")
        ingest_date(path, instrument, obsdate)

    with get_conn(path) as conn:
        conn.execute(
            "UPDATE lco_observation_frames SET state='ingested', error='', updated_at=? WHERE request_id=?",
            (now, request_id),
        )
        conn.execute(
            "UPDATE lco_observation_requests SET monitor_state='complete', downloaded_count=reduced_frame_count, "
            "download_job_id='', last_error='', completed_at=?, updated_at=? WHERE request_id=?",
            (now, now, request_id),
        )
        conn.commit()


def _mark_error(request_id: int, error: Exception, *, path: str, now: float) -> None:
    with get_conn(path) as conn:
        current = conn.execute(
            "SELECT error_count FROM lco_observation_requests WHERE request_id=?", (request_id,)
        ).fetchone()
        count = int(current[0] if current else 0) + 1
        conn.execute(
            "UPDATE lco_observation_requests SET monitor_state='monitoring', error_count=?, last_error=?, "
            "next_poll_at=?, updated_at=? WHERE request_id=?",
            (count, str(error)[:2000], now + _error_delay(count), now, request_id),
        )
        conn.commit()


def process_request(request: dict, *, path: str | None = None, now: float | None = None) -> None:
    """Advance one persisted Request by one bounded monitoring step."""
    path = path or db_path()
    now = time.time() if now is None else now
    request_id = int(request["request_id"])
    try:
        job_id = str(request.get("download_job_id") or "")
        if job_id:
            try:
                snapshot = lco.archive_download_status(job_id)
            except lco.LcoError as exc:
                if exc.status != 404:
                    raise
                # In-memory download jobs disappear on restart. Atomic files and
                # persisted frame metadata make safely re-queuing them idempotent.
                snapshot = {"state": "lost", "results": [], "funpack_results": []}
            if snapshot.get("state") in {"pending", "running"}:
                with get_conn(path) as conn:
                    conn.execute(
                        "UPDATE lco_observation_requests SET next_poll_at=?, updated_at=? WHERE request_id=?",
                        (now + _DOWNLOAD_CHECK_S, now, request_id),
                    )
                    conn.commit()
                return
            if snapshot.get("state") == "done":
                if not _finish_download(request, snapshot, path=path, now=now):
                    return
                request = {**request, "download_job_id": ""}
            else:
                with get_conn(path) as conn:
                    conn.execute(
                        "UPDATE lco_observation_requests SET download_job_id='', monitor_state='monitoring', "
                        "next_poll_at=?, updated_at=? WHERE request_id=?",
                        (now, now, request_id),
                    )
                    conn.commit()

        # A scan/ingest retry does not need another three LCO API calls: the
        # terminal request state, complete raw/final counts, and prepared local
        # files are already durable.
        if (
            str(request.get("request_state") or "").upper() in TERMINAL_REQUEST_STATES
            and int(request.get("raw_frame_count") or 0) > 0
            and int(request.get("reduced_frame_count") or 0)
            >= int(request.get("raw_frame_count") or 0)
            and int(request.get("downloaded_count") or 0)
            >= int(request.get("reduced_frame_count") or 0)
        ):
            _process_datasets(request_id, path=path, now=now)
            return

        # Background polling, not an interactive submit: keep observing requests
        # that were submitted under the server's global token (user_name unset or
        # without a saved per-user token) by allowing the global fallback here.
        group = lco.get_requestgroup(
            request["requestgroup_id"],
            user_name=request.get("user_name") or None,
            require_own_token=False,
        )
        child = _request_child(group, request_id)
        request_state = str(
            child.get("state") or group.get("state") or request.get("request_state") or "PENDING"
        ).upper()
        raw_page = lco.archive_search_all(
            {"request_id": request_id, "reduction_level": 0, "limit": "1000"},
            user_name=request.get("user_name") or None,
        )
        reduced_page = lco.archive_search_all(
            {"request_id": request_id, "reduction_level": 91, "limit": "1000"},
            user_name=request.get("user_name") or None,
        )
        if raw_page.get("truncated") or reduced_page.get("truncated"):
            raise RuntimeError(
                "LCO archive result exceeded the safety cap; refusing partial ingestion"
            )
        raw_frames = list(raw_page.get("results") or [])
        reduced_frames = list(reduced_page.get("results") or [])
        raw_ids = {_frame_identity(frame) for frame in raw_frames}
        reduced_ids = {_frame_identity(frame) for frame in reduced_frames}
        raw_count = len(raw_ids)
        reduced_count = len(reduced_ids)
        unchanged = (
            int(request.get("unchanged_polls") or 0) + 1
            if raw_count == int(request.get("raw_frame_count") or 0)
            and reduced_count == int(request.get("reduced_frame_count") or 0)
            else 0
        )

        with get_conn(path) as conn:
            _upsert_reduced_frames(conn, request_id, reduced_frames, now)
            previous_terminal_seen = request.get("terminal_seen_at")
            terminal_seen = (
                previous_terminal_seen or now if request_state in TERMINAL_REQUEST_STATES else None
            )
            conn.execute(
                "UPDATE lco_observation_requests SET request_state=?, raw_frame_count=?, "
                "reduced_frame_count=?, unchanged_polls=?, error_count=0, last_error='', "
                "last_polled_at=?, terminal_seen_at=?, next_poll_at=?, updated_at=? WHERE request_id=?",
                (
                    request_state,
                    raw_count,
                    reduced_count,
                    unchanged,
                    now,
                    terminal_seen,
                    now
                    + _poll_delay(
                        unchanged, window_end=str(request.get("window_end") or ""), now=now
                    ),
                    now,
                    request_id,
                ),
            )
            pending = _download_rows(conn, request_id)
            downloaded = conn.execute(
                "SELECT COUNT(*) FROM lco_observation_frames WHERE request_id=? AND state IN ('downloaded','ingested')",
                (request_id,),
            ).fetchone()[0]
            conn.execute(
                "UPDATE lco_observation_requests SET downloaded_count=? WHERE request_id=?",
                (downloaded, request_id),
            )
            conn.commit()

        if pending:
            snapshot = lco.start_archive_download(
                [row["metadata"] for row in pending], overwrite=False, auto_ingest=False
            )
            with get_conn(path) as conn:
                conn.execute(
                    "UPDATE lco_observation_requests SET monitor_state='downloading', download_job_id=?, "
                    "next_poll_at=?, updated_at=? WHERE request_id=?",
                    (snapshot["job_id"], now + _DOWNLOAD_CHECK_S, now, request_id),
                )
                conn.commit()
            return

        reduction_complete = bool(raw_ids) and raw_ids.issubset(reduced_ids)
        if request_state in TERMINAL_REQUEST_STATES and reduction_complete:
            _process_datasets(request_id, path=path, now=now)
        elif (
            request_state in TERMINAL_REQUEST_STATES
            and not raw_ids
            and terminal_seen is not None
            and now - float(terminal_seen) >= _NO_DATA_GRACE_S
        ):
            with get_conn(path) as conn:
                conn.execute(
                    "UPDATE lco_observation_requests SET monitor_state='terminal_no_data', completed_at=?, "
                    "updated_at=?, next_poll_at=? WHERE request_id=?",
                    (now, now, now, request_id),
                )
                conn.commit()
    except Exception as exc:
        logger.warning("LCO request %s monitoring step failed: %s", request_id, exc)
        _mark_error(request_id, exc, path=path, now=now)


def process_due(
    *, path: str | None = None, now: float | None = None, limit: int = _BATCH_SIZE
) -> int:
    """Advance due non-terminal requests, returning the number processed."""
    path = path or db_path()
    now = time.time() if now is None else now
    with get_conn(path, row_factory=None) as conn:
        cursor = conn.execute(
            "SELECT * FROM lco_observation_requests WHERE monitor_state NOT IN ('complete','terminal_no_data') "
            "AND next_poll_at<=? ORDER BY next_poll_at LIMIT ?",
            (now, limit),
        )
        columns = [description[0] for description in cursor.description]
        requests = [dict(zip(columns, row)) for row in cursor.fetchall()]
    for request in requests:
        process_request(request, path=path, now=now)
    return len(requests)


def _acquire_lease(path: str, owner: str, now: float) -> bool:
    with get_conn(path) as conn:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute(
            "SELECT owner, expires_at FROM lco_monitor_leases WHERE name='observation-monitor'"
        ).fetchone()
        if row and row[0] != owner and float(row[1]) > now:
            conn.rollback()
            return False
        conn.execute(
            "INSERT INTO lco_monitor_leases(name,owner,expires_at) VALUES('observation-monitor',?,?) "
            "ON CONFLICT(name) DO UPDATE SET owner=excluded.owner, expires_at=excluded.expires_at",
            (owner, now + _LEASE_S),
        )
        conn.commit()
    return True


def _renew_lease(path: str, owner: str, now: float) -> bool:
    with get_conn(path) as conn:
        changed = conn.execute(
            "UPDATE lco_monitor_leases SET expires_at=? "
            "WHERE name='observation-monitor' AND owner=?",
            (now + _LEASE_S, owner),
        )
        conn.commit()
        return bool(changed.rowcount)


def _release_lease(path: str, owner: str) -> None:
    with get_conn(path) as conn:
        conn.execute(
            "DELETE FROM lco_monitor_leases WHERE name='observation-monitor' AND owner=?",
            (owner,),
        )
        conn.commit()


class ObservationMonitor:
    """One daemon loop per process; a DB lease elects the active worker."""

    def __init__(self, path: str | None = None):
        self.path = path or db_path()
        self.owner = f"{socket.gethostname()}:{os.getpid()}:{uuid.uuid4().hex[:8]}"
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._heartbeat_thread: threading.Thread | None = None

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._run, name="lco-observation-monitor", daemon=True
        )
        self._thread.start()
        self._heartbeat_thread = threading.Thread(
            target=self._heartbeat, name="lco-observation-monitor-lease", daemon=True
        )
        self._heartbeat_thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=min(_LOOP_S + 2, 30))
        if self._heartbeat_thread and self._heartbeat_thread.is_alive():
            self._heartbeat_thread.join(timeout=5)
        if not self._thread or not self._thread.is_alive():
            _release_lease(self.path, self.owner)

    def _run(self) -> None:
        while not self._stop.is_set():
            now = time.time()
            try:
                if _acquire_lease(self.path, self.owner, now):
                    process_due(path=self.path, now=now)
            except Exception:
                logger.exception("LCO observation monitor loop failed")
            self._stop.wait(_LOOP_S)

    def _heartbeat(self) -> None:
        interval = max(10.0, _LEASE_S / 3.0)
        while not self._stop.wait(interval):
            try:
                _renew_lease(self.path, self.owner, time.time())
            except Exception:
                logger.exception("LCO observation monitor lease renewal failed")
