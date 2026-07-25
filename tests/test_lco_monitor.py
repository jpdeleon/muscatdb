from __future__ import annotations

import json
import sqlite3

import pytest

from muscat_db import lco, lco_monitor
from muscat_db.database import SCHEMA, get_conn as get_conn_for


@pytest.fixture
def monitor_db(tmp_path):
    path = tmp_path / "monitor.db"
    with sqlite3.connect(path) as conn:
        conn.executescript(SCHEMA)
    return str(path)


def _submission(*, request_id=101, requestgroup_id=10, state="PENDING"):
    result = {
        "id": requestgroup_id,
        "name": "TOI-123 transit",
        "proposal": "LCO2026B-001",
        "state": state,
        "requests": [
            {
                "id": request_id,
                "state": state,
                "windows": [{"start": "2026-07-20T10:00:00Z", "end": "2026-07-20T12:00:00Z"}],
            }
        ],
    }
    payload = {
        "kind": "muscat3",
        "name": "TOI-123 transit",
        "proposal": "LCO2026B-001",
        "target_name": "TOI-123",
        "confirm": True,
        "dry_run_hash": "secret-ish-transient-value",
        "requests": [{"windows": result["requests"][0]["windows"]}],
    }
    return result, payload


def _frame(level: int, number: int, *, observation_id: int | None = None):
    observation_id = number if observation_id is None else observation_id
    return {
        "id": level * 1000 + number,
        "observation_id": observation_id,
        "filename": f"ogg2m001-ep05-20260720-{number:04d}-e{level:02d}.fits.fz",
        "observation_date": "2026-07-20T10:01:00Z",
        "site_id": "ogg",
        "telescope_id": "2m0a",
        "instrument_id": "ep05",
        "target_name": "TOI-123",
        "url": "https://archive-api.lco.global/frame.fits.fz",
    }


def _row(path, request_id=101):
    with sqlite3.connect(path) as conn:
        conn.row_factory = sqlite3.Row
        return dict(
            conn.execute(
                "SELECT * FROM lco_observation_requests WHERE request_id=?", (request_id,)
            ).fetchone()
        )


def test_record_submission_persists_each_child_and_waits_for_window(monitor_db):
    result, payload = _submission()
    rows = lco_monitor.record_submission(
        result, payload, "alice", path=monitor_db, now=1_700_000_000
    )

    assert [row["request_id"] for row in rows] == [101]
    row = _row(monitor_db)
    assert row["requestgroup_id"] == 10
    assert row["target"] == "TOI-123"
    assert row["instrument"] == "muscat3"
    assert row["user_name"] == "alice"
    assert row["next_poll_at"] == pytest.approx(lco_monitor._iso_timestamp("2026-07-20T10:00:00Z"))
    stored = json.loads(row["payload_json"])
    assert "confirm" not in stored
    assert "dry_run_hash" not in stored


def test_get_request_payload_matches_request_or_group_id(monitor_db):
    result, payload = _submission(request_id=101, requestgroup_id=10)
    lco_monitor.record_submission(result, payload, "alice", path=monitor_db, now=1_700_000_000)

    by_request = lco_monitor.get_request_payload(101, path=monitor_db)
    by_group = lco_monitor.get_request_payload(10, path=monitor_db)
    assert by_request is not None and by_group is not None
    # Returns the stored (cleaned) form params, not the transient submit fields.
    assert by_request["target_name"] == "TOI-123"
    assert by_request["kind"] == "muscat3"
    assert "confirm" not in by_request and "dry_run_hash" not in by_request
    assert by_group == by_request
    assert lco_monitor.get_request_payload(999999, path=monitor_db) is None


def test_record_submission_rejects_response_without_child_request_ids(monitor_db):
    result, payload = _submission()
    result["requests"] = []
    with pytest.raises(ValueError, match="child request IDs"):
        lco_monitor.record_submission(result, payload, None, path=monitor_db)


def test_frame_identity_keeps_simultaneous_muscat_cameras_distinct():
    ep02_raw = _frame(0, 1, observation_id=77)
    ep02_final = _frame(91, 1, observation_id=77)
    ep03_raw = {**ep02_raw, "filename": ep02_raw["filename"].replace("ep05", "ep03")}

    assert lco_monitor._frame_identity(ep02_raw) == lco_monitor._frame_identity(ep02_final)
    assert lco_monitor._frame_identity(ep02_raw) != lco_monitor._frame_identity(ep03_raw)


def test_monitor_downloads_final_frames_then_scans_and_ingests(monitor_db, tmp_path, monkeypatch):
    monkeypatch.setenv("MUSCAT_LCO_DIR", str(tmp_path))
    result, payload = _submission(state="COMPLETED")
    lco_monitor.record_submission(result, payload, "alice", path=monitor_db, now=100)
    raw = _frame(0, 1)
    reduced = _frame(91, 1)

    monkeypatch.setattr(
        "muscat_db.lco.get_requestgroup",
        lambda group_id, user_name=None, **kwargs: result,
    )
    pages = iter(
        [
            {"count": 1, "results": [raw], "truncated": False},
            {"count": 1, "results": [reduced], "truncated": False},
        ]
    )
    monkeypatch.setattr(
        "muscat_db.lco.archive_search_all",
        lambda filters, user_name=None: next(pages),
    )
    queued = []

    def fake_start(frames, overwrite=False, auto_ingest=False, user_name=None):
        assert auto_ingest is False
        queued.extend(frames)
        return {"job_id": "download-1", "state": "pending"}

    monkeypatch.setattr("muscat_db.lco.start_archive_download", fake_start)
    lco_monitor.process_request(_row(monitor_db), path=monitor_db, now=200)

    downloading = _row(monitor_db)
    assert downloading["monitor_state"] == "downloading"
    assert downloading["download_job_id"] == "download-1"
    assert [frame["id"] for frame in queued] == [91001]

    dest = tmp_path / "MuSCAT3" / "260720" / reduced["filename"]
    monkeypatch.setattr(
        "muscat_db.lco.archive_download_status",
        lambda job_id: {
            "state": "done",
            "results": [
                {"filename": reduced["filename"], "status": "downloaded", "dest": str(dest)}
            ],
            "funpack_results": [{"filename": reduced["filename"], "status": "unpacked"}],
        },
    )
    # The completion step rechecks LCO before ingesting.
    pages = iter(
        [
            {"count": 1, "results": [raw], "truncated": False},
            {"count": 1, "results": [reduced], "truncated": False},
        ]
    )
    monkeypatch.setattr(
        "muscat_db.lco.archive_search_all",
        lambda filters, user_name=None: next(pages),
    )
    monkeypatch.setattr("muscat_db.lco.download_root", lambda: tmp_path)
    scanned = []
    ingested = []

    def fake_scan(instrument, obsdate, max_workers=None, data_root=None):
        scanned.append((instrument, obsdate, max_workers, data_root))
        return {"total": 1, "per_ccd": {3: 1}}

    def fake_ingest(path, instrument, obsdate):
        ingested.append((path, instrument, obsdate))
        return 1

    monkeypatch.setattr("muscat_db.scanner.scan_date", fake_scan)
    monkeypatch.setattr("muscat_db.database.ingest_date", fake_ingest)
    lco_monitor.process_request(downloading, path=monitor_db, now=220)

    complete = _row(monitor_db)
    assert complete["monitor_state"] == "complete"
    assert complete["raw_frame_count"] == 1
    assert complete["reduced_frame_count"] == 1
    assert complete["downloaded_count"] == 1
    assert scanned == [("muscat3", "260720", 1, str(tmp_path))]
    assert ingested == [(monitor_db, "muscat3", "260720")]


def test_monitor_downloads_incrementally_and_waits_for_every_raw_frame(
    monitor_db, tmp_path, monkeypatch
):
    monkeypatch.setenv("MUSCAT_LCO_DIR", str(tmp_path))
    result, payload = _submission(state="COMPLETED")
    lco_monitor.record_submission(result, payload, None, path=monitor_db, now=100)
    raw = [_frame(0, 1), _frame(0, 2)]
    reduced = [_frame(91, 1)]
    monkeypatch.setattr("muscat_db.lco.get_requestgroup", lambda *args, **kwargs: result)
    pages = iter(
        [
            {"results": raw},
            {"results": reduced},
        ]
    )
    monkeypatch.setattr("muscat_db.lco.archive_search_all", lambda *args, **kwargs: next(pages))
    monkeypatch.setattr(
        "muscat_db.lco.start_archive_download",
        lambda frames, overwrite=False, auto_ingest=False, user_name=None: {"job_id": "one", "state": "pending"},
    )

    lco_monitor.process_request(_row(monitor_db), path=monitor_db, now=200)
    row = _row(monitor_db)
    assert row["monitor_state"] == "downloading"
    assert row["raw_frame_count"] == 2
    assert row["reduced_frame_count"] == 1


def test_terminal_unobserved_request_waits_for_archive_lag_grace(monitor_db, monkeypatch):
    result, payload = _submission(state="WINDOW_EXPIRED")
    lco_monitor.record_submission(result, payload, None, path=monitor_db, now=100)
    monkeypatch.setattr("muscat_db.lco.get_requestgroup", lambda *args, **kwargs: result)
    monkeypatch.setattr(
        "muscat_db.lco.archive_search_all", lambda *args, **kwargs: {"count": 0, "results": []}
    )

    lco_monitor.process_request(_row(monitor_db), path=monitor_db, now=200)
    first = _row(monitor_db)
    assert first["monitor_state"] == "monitoring"
    assert first["terminal_seen_at"] == 200

    lco_monitor.process_request(first, path=monitor_db, now=200 + lco_monitor._NO_DATA_GRACE_S)
    assert _row(monitor_db)["monitor_state"] == "terminal_no_data"


def test_monitor_queues_downloads_one_acceptable_batch_at_a_time(
    monitor_db, tmp_path, monkeypatch
):
    """More pending frames than one download call accepts must not wedge.

    start_archive_download rejects an oversized batch with 413, which the outer
    handler turns into a retry of the identical batch -- forever. A request must
    instead queue a batch that fits and pick up the rest on the next pass.
    """
    monkeypatch.setenv("MUSCAT_LCO_DIR", str(tmp_path))
    monkeypatch.setattr("muscat_db.lco._ARCHIVE_DOWNLOAD_MAX_FRAMES", 3)
    result, payload = _submission(state="COMPLETED")
    lco_monitor.record_submission(result, payload, None, path=monitor_db, now=100)
    frames = [_frame(91, i) for i in range(1, 6)]  # 5 final products, cap is 3
    raws = [_frame(0, i) for i in range(1, 6)]

    monkeypatch.setattr("muscat_db.lco.get_requestgroup", lambda *args, **kwargs: result)
    monkeypatch.setattr(
        "muscat_db.lco.archive_search_all",
        lambda filters, user_name=None: {
            "count": 5,
            "results": raws if int(filters["reduction_level"]) == 0 else frames,
            "truncated": False,
        },
    )
    queued = []

    def fake_start(batch, overwrite=False, auto_ingest=False, user_name=None):
        # Mirror the real guard so an oversized batch fails here the way it would
        # in production, instead of passing silently.
        if len(batch) > lco._ARCHIVE_DOWNLOAD_MAX_FRAMES:
            raise lco.LcoError("too many frames", status=413)
        queued.append(list(batch))
        return {"job_id": "download-1", "state": "pending"}

    monkeypatch.setattr("muscat_db.lco.start_archive_download", fake_start)
    lco_monitor.process_request(_row(monitor_db), path=monitor_db, now=200)

    row = _row(monitor_db)
    assert row["monitor_state"] == "downloading"
    assert row["last_error"] in ("", None)  # no 413 recorded
    assert len(queued) == 1 and len(queued[0]) == 3
    with sqlite3.connect(monitor_db) as conn:
        still_pending = conn.execute(
            "SELECT COUNT(*) FROM lco_observation_frames WHERE request_id=101 AND state='pending'"
        ).fetchone()[0]
    assert still_pending == 5  # nothing downloaded yet; the other 2 wait their turn


def test_download_batch_respects_frame_and_payload_caps(monkeypatch):
    monkeypatch.setattr("muscat_db.lco._ARCHIVE_DOWNLOAD_MAX_FRAMES", 3)
    frames = [{"filename": f"f{i}.fits.fz", "url": "https://archive/x"} for i in range(10)]
    assert len(lco.download_batch(frames)) == 3

    # A byte cap tighter than the frame cap trims further, and never to nothing.
    monkeypatch.setattr("muscat_db.lco._ARCHIVE_DOWNLOAD_MAX_PAYLOAD_BYTES", 80)
    trimmed = lco.download_batch(frames)
    assert 1 <= len(trimmed) < 3


def test_terminal_request_with_partial_reduction_ingests_after_grace(
    monitor_db, tmp_path, monkeypatch
):
    """BANZAI may never reduce every raw frame (QC drops are routine).

    Such a request satisfies neither the all-reduced completion branch nor the
    no-data branch, so before this it polled forever and never ingested the
    frames that did reduce.
    """
    monkeypatch.setenv("MUSCAT_LCO_DIR", str(tmp_path))
    result, payload = _submission(state="COMPLETED")
    lco_monitor.record_submission(result, payload, None, path=monitor_db, now=100)
    raws = [_frame(0, i) for i in range(1, 4)]     # 3 raw frames
    reduced = [_frame(91, i) for i in range(1, 3)]  # only 2 ever reduce

    monkeypatch.setattr("muscat_db.lco.get_requestgroup", lambda *args, **kwargs: result)
    monkeypatch.setattr(
        "muscat_db.lco.archive_search_all",
        lambda filters, user_name=None: {
            "count": 3,
            "results": raws if int(filters["reduction_level"]) == 0 else reduced,
            "truncated": False,
        },
    )
    monkeypatch.setattr(
        "muscat_db.lco.start_archive_download",
        lambda batch, overwrite=False, auto_ingest=False, user_name=None: {"job_id": "dl-1", "state": "pending"},
    )
    lco_monitor.process_request(_row(monitor_db), path=monitor_db, now=200)
    assert _row(monitor_db)["terminal_seen_at"] == 200

    # The two available products download successfully.
    monkeypatch.setattr(
        "muscat_db.lco.archive_download_status",
        lambda job_id: {
            "state": "done",
            "results": [
                {
                    "filename": f["filename"],
                    "status": "downloaded",
                    "dest": str(tmp_path / "MuSCAT3" / "260720" / f["filename"]),
                }
                for f in reduced
            ],
            "funpack_results": [
                {"filename": f["filename"], "status": "unpacked"} for f in reduced
            ],
        },
    )
    monkeypatch.setattr("muscat_db.lco.download_root", lambda: tmp_path)
    scanned, ingested = [], []
    monkeypatch.setattr(
        "muscat_db.scanner.scan_date",
        lambda instrument, obsdate, max_workers=None, data_root=None: (
            scanned.append((instrument, obsdate)) or {"total": 2, "per_ccd": {3: 2}}
        ),
    )
    monkeypatch.setattr(
        "muscat_db.database.ingest_date",
        lambda path, instrument, obsdate: ingested.append((instrument, obsdate)) or 2,
    )

    # Still inside the grace window: hold, in case the last product is just late.
    lco_monitor.process_request(_row(monitor_db), path=monitor_db, now=220)
    waiting = _row(monitor_db)
    assert waiting["monitor_state"] == "monitoring"
    assert waiting["downloaded_count"] == 2
    assert not ingested

    # Past the grace window: settle on what actually arrived.
    lco_monitor.process_request(
        waiting, path=monitor_db, now=200 + lco_monitor._NO_DATA_GRACE_S + 1
    )
    settled = _row(monitor_db)
    assert settled["monitor_state"] == "complete"
    assert settled["raw_frame_count"] == 3
    assert settled["reduced_frame_count"] == 2
    assert scanned == [("muscat3", "260720")]
    assert ingested == [("muscat3", "260720")]


def test_frame_download_is_abandoned_after_repeated_failures(
    monitor_db, tmp_path, monkeypatch
):
    """A frame the archive never serves must stop blocking the request.

    _download_rows re-selects 'error' frames, so without a retry budget one
    permanently unavailable product keeps `pending` non-empty forever and the
    request can never reach a terminal state.
    """
    monkeypatch.setenv("MUSCAT_LCO_DIR", str(tmp_path))
    monkeypatch.setattr(lco_monitor, "_MAX_FRAME_ATTEMPTS", 3)
    result, payload = _submission(state="COMPLETED")
    lco_monitor.record_submission(result, payload, None, path=monitor_db, now=100)
    reduced = _frame(91, 1)
    now = 200
    with get_conn_for(monitor_db) as conn:
        lco_monitor._upsert_reduced_frames(conn, 101, [reduced], now)
        conn.commit()

    snapshot = {
        "state": "done",
        "results": [
            {"filename": reduced["filename"], "status": "error", "error": "404 not found"}
        ],
        "funpack_results": [],
    }
    request = _row(monitor_db)
    for attempt in range(1, 4):
        lco_monitor._finish_download(request, snapshot, path=monitor_db, now=now + attempt)
        with sqlite3.connect(monitor_db) as conn:
            state, attempts = conn.execute(
                "SELECT state, attempts FROM lco_observation_frames WHERE request_id=101"
            ).fetchone()
        assert attempts == attempt
        # Retryable until the budget is spent, then abandoned for good.
        assert state == ("abandoned" if attempt >= 3 else "error")

    # An abandoned frame is no longer re-queued, so the request can finish.
    with get_conn_for(monitor_db) as conn:
        assert lco_monitor._download_rows(conn, 101) == []


def test_monitor_downloads_use_a_dedicated_slot_bucket(monitor_db, tmp_path, monkeypatch):
    """Server-initiated downloads must not share the interactive 'anonymous'
    bucket, whose small per-user cap would 429 the third concurrent request into
    error backoff for no reason."""
    monkeypatch.setenv("MUSCAT_LCO_DIR", str(tmp_path))
    result, payload = _submission(state="COMPLETED")
    lco_monitor.record_submission(result, payload, None, path=monitor_db, now=100)
    raws, reduced = [_frame(0, 1)], [_frame(91, 1)]
    monkeypatch.setattr("muscat_db.lco.get_requestgroup", lambda *a, **k: result)
    monkeypatch.setattr(
        "muscat_db.lco.archive_search_all",
        lambda filters, user_name=None: {
            "count": 1,
            "results": raws if int(filters["reduction_level"]) == 0 else reduced,
            "truncated": False,
        },
    )
    seen = {}

    def fake_start(batch, overwrite=False, auto_ingest=False, user_name=None):
        seen["user_name"] = user_name
        return {"job_id": "dl", "state": "pending"}

    monkeypatch.setattr("muscat_db.lco.start_archive_download", fake_start)
    lco_monitor.process_request(_row(monitor_db), path=monitor_db, now=200)
    assert seen["user_name"] == lco_monitor._MONITOR_DOWNLOAD_USER
    assert seen["user_name"] != "anonymous"


def test_monitor_errors_back_off_and_remain_retryable(monitor_db, monkeypatch):
    result, payload = _submission()
    lco_monitor.record_submission(result, payload, None, path=monitor_db, now=100)
    monkeypatch.setattr(
        "muscat_db.lco.get_requestgroup",
        lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("rate limited")),
    )

    lco_monitor.process_request(_row(monitor_db), path=monitor_db, now=200)
    row = _row(monitor_db)
    assert row["monitor_state"] == "monitoring"
    assert row["error_count"] == 1
    assert row["next_poll_at"] >= 200 + lco_monitor._POLL_S
    assert "rate limited" in row["last_error"]


def test_database_lease_allows_only_one_live_owner(monitor_db):
    assert lco_monitor._acquire_lease(monitor_db, "worker-a", 100)
    assert not lco_monitor._acquire_lease(monitor_db, "worker-b", 101)
    assert lco_monitor._acquire_lease(monitor_db, "worker-b", 100 + lco_monitor._LEASE_S + 1)
