from __future__ import annotations

import sqlite3
from pathlib import Path
from unittest.mock import Mock

import pytest
from fastapi.testclient import TestClient

from muscat_db import database, exposure, lco, web
from muscat_db.web import app


def test_active_job_poll_is_indexed_and_does_not_reconcile(monkeypatch):
    class ActiveOnlyStore:
        def active(self):
            return [{"key": "photometry:x", "state": "running"}]

        def all(self):
            pytest.fail("active poll must not load job history")

    monkeypatch.setattr(web, "get_job_store", lambda: ActiveOnlyStore())
    monkeypatch.setattr(lco, "archive_download_jobs", lambda: [])
    monkeypatch.setattr(web, "_reconcile_all_jobs", lambda: pytest.fail("poll must not reconcile"))

    response = TestClient(app).get("/api/jobs/status?active_only=1")

    assert response.status_code == 200
    assert response.json() == {"active": [{"key": "photometry:x", "state": "running"}]}


def test_get_targets_read_path_does_not_apply_schema(tmp_path, monkeypatch):
    db_path = tmp_path / "targets.db"
    with sqlite3.connect(db_path) as conn:
        conn.executescript(database.SCHEMA)
    monkeypatch.setattr(database, "_apply_schema", lambda _conn: pytest.fail("DDL in read path"))

    assert database.get_targets(str(db_path)) == []


def test_catalog_batch_has_hard_item_limit(monkeypatch):
    monkeypatch.setattr(web, "_CATALOG_BATCH_MAX_ITEMS", 1)

    response = TestClient(app).post(
        "/api/exposure/lookup-mags-batch",
        json={"stars": [{"ra": 1, "dec": 2}, {"ra": 3, "dec": 4}]},
    )

    assert response.status_code == 413
    assert "At most 1 stars" in response.json()["error"]


def test_archive_foreground_batch_has_small_limit(monkeypatch):
    monkeypatch.setenv("MUSCAT_LCO_ARCHIVE_FOREGROUND_MAX_FRAMES", "1")
    monkeypatch.setattr(lco, "download_frames", lambda *_args, **_kwargs: pytest.fail("must reject first"))

    response = TestClient(app).post(
        "/api/lco/archive/download",
        json={"frames": [{"filename": "a.fits"}, {"filename": "b.fits"}]},
    )

    assert response.status_code == 413
    assert "use background mode" in response.json()["error"]


def test_archive_jobs_are_compact_and_limited_per_user(monkeypatch):
    jobs = {}
    submit = Mock()
    monkeypatch.setattr(lco, "_ARCHIVE_DOWNLOAD_JOBS", jobs)
    monkeypatch.setattr(lco, "_ARCHIVE_DOWNLOAD_MAX_PER_USER", 1)
    monkeypatch.setattr(lco._ARCHIVE_DOWNLOAD_EXECUTOR, "submit", submit)
    frame = {
        "filename": "a.fits.fz",
        "SITEID": "ogg",
        "TELID": "2m0a",
        "DATE_OBS": "2026-01-02T00:00:00",
        "url": "https://archive-api.lco.global/a.fits.fz",
        "large_unused_field": "x" * 1000,
    }

    first = lco.start_archive_download([frame], user_name="alice")
    with pytest.raises(lco.LcoError) as exc:
        lco.start_archive_download([frame], user_name="alice")

    assert exc.value.status == 429
    assert "large_unused_field" not in jobs[first["job_id"]]["frames"][0]
    submit.assert_called_once()


def test_zip_generation_enforces_budget_and_reuses_manifest_cache(tmp_path, monkeypatch):
    source = tmp_path / "result.csv"
    source.write_text("bjd,flux\n1,1\n")
    cache = tmp_path / "cache"
    monkeypatch.setattr(web.phot, "prose_tmpdir", lambda: cache)
    monkeypatch.setattr(web, "_ZIP_FREE_RESERVE_BYTES", 0)
    monkeypatch.setattr(web, "_ZIP_MAX_INPUT_BYTES", 1024)

    first = web._create_zip_response([(source, "result.csv")], "outputs.zip")
    second = web._create_zip_response([(source, "result.csv")], "outputs.zip")

    assert Path(first.path).is_file()
    assert first.path == second.path

    monkeypatch.setattr(web, "_ZIP_MAX_INPUT_BYTES", 1)
    with pytest.raises(web.HTTPException) as exc:
        web._create_zip_response([(source, "result.csv")], "outputs.zip")
    assert exc.value.status_code == 413


def test_calibration_jobs_are_tracked_deduplicated_and_cancellable(tmp_path, monkeypatch):
    monkeypatch.setenv("MUSCAT_DB_PATH", str(tmp_path / "calibration.db"))
    monkeypatch.setattr(exposure._CALIBRATION_EXECUTOR, "submit", Mock())
    with exposure._CALIBRATION_LOCK:
        exposure._CALIBRATION_CANCEL.clear()

    job = exposure.start_calibration("muscat3")
    with pytest.raises(RuntimeError, match="already active"):
        exposure.start_calibration("muscat3")
    cancelling = exposure.cancel_calibration(job["job_id"])

    assert job["state"] == "pending"
    assert cancelling["state"] == "cancelling"
    assert exposure._CALIBRATION_CANCEL[job["job_id"]].is_set()
