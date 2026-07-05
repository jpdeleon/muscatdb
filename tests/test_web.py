from __future__ import annotations

import os
import sqlite3
import tempfile
import getpass
import pytest
from fastapi.testclient import TestClient

from muscat_db.database import save_job, get_persisted_jobs
from muscat_db.web import app, _annotate_lco_archive_results

@pytest.fixture
def mock_db(monkeypatch):
    """Set up a temporary database for testing web endpoints."""
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    monkeypatch.setenv("MUSCAT_DB_PATH", path)
    
    # Initialize the database schema
    conn = sqlite3.connect(path)
    from muscat_db.database import SCHEMA
    conn.executescript(SCHEMA)
    conn.commit()
    conn.close()
    
    # Mock sync_jobs so it doesn't clean up our mock active jobs
    monkeypatch.setattr("muscat_db.photometry.sync_jobs", lambda: None)
    monkeypatch.setattr("muscat_db.transit_fit.sync_jobs", lambda: None)
    # Mock discover_orphan_fits so it doesn't load production files from disk
    monkeypatch.setattr("muscat_db.transit_fit._discover_orphan_fits", lambda existing: [])
    monkeypatch.setattr("muscat_db.lco.archive_download_jobs", lambda: [])
    
    yield path
    try:
        os.unlink(path)
    except OSError:
        pass

def test_jobs_status_response_counts_and_started_at(mock_db, monkeypatch):
    # Save a running job, a cancelling job, and a done job on different targets to avoid key collisions
    save_job(
        type_="photometry",
        inst="muscat3",
        date="260101",
        target="WASP-12b",
        state="running",
        returncode=None,
        elapsed=10,
        started_at=1700000000.0,
        run_name="Run1",
        user_name="test_user1"
    )
    save_job(
        type_="transit_fit",
        inst="muscat3",
        date="260101",
        target="HAT-P-1b",
        state="cancelling",
        returncode=None,
        elapsed=20,
        started_at=1700000100.0,
        run_name="Run2"
        # defaults to getpass.getuser()
    )
    save_job(
        type_="photometry",
        inst="muscat3",
        date="260101",
        target="TrES-3b",
        state="done",
        returncode=0,
        elapsed=100,
        started_at=1700000200.0,
        run_name="Run3",
        user_name="test_user3"
    )

    client = TestClient(app)
    response = client.get("/jobs/status")
    assert response.status_code == 200
    data = response.json()
    
    # 1. Check counts
    assert data["counts"]["running"] == 2
    assert data["counts"]["done"] == 1
    assert data["counts"]["pending"] == 0
    assert data["counts"]["error"] == 0
    assert data["counts"]["cancelled"] == 0
    
    # 2. Check running list includes raw started_at and user_name
    running_jobs = data["running"]
    assert len(running_jobs) == 2
    
    user1_found = False
    default_user_found = False
    for job in running_jobs:
        assert "started_at" in job
        assert "user_name" in job
        if job["user_name"] == "test_user1":
            user1_found = True
        elif job["user_name"] == getpass.getuser():
            default_user_found = True
            
    assert user1_found
    assert default_user_found


def test_jobs_status_elapsed_uses_latest_rerun_started_at(mock_db, monkeypatch):
    save_job(
        type_="photometry",
        inst="muscat3",
        date="260101",
        target="WASP-12b",
        state="done",
        returncode=0,
        elapsed=100,
        started_at=1000.0,
    )
    save_job(
        type_="photometry",
        inst="muscat3",
        date="260101",
        target="WASP-12b",
        state="running",
        returncode=None,
        elapsed=0,
        started_at=2000.0,
    )
    monkeypatch.setattr("muscat_db.web._last_running", set())
    monkeypatch.setattr("muscat_db.web.time.time", lambda: 2030.0)

    response = TestClient(app).get("/jobs/status")

    assert response.status_code == 200
    data = response.json()
    assert len(data["running"]) == 1
    assert data["running"][0]["elapsed"] == 30


def test_jobs_page_always_shows_lco_archive_download_section(mock_db):
    r = TestClient(app).get("/jobs")

    assert r.status_code == 200
    assert "LCO Archive Downloads" in r.text
    assert 'id="lco-archive-jobs-section"' in r.text
    assert 'data-always-visible="1"' in r.text
    assert "No LCO archive downloads are currently tracked" in r.text


def test_jobs_page_includes_lco_archive_download(mock_db, monkeypatch):
    monkeypatch.setattr(
        "muscat_db.lco.archive_download_jobs",
        lambda: [{
            "job_id": "abc123",
            "state": "running",
            "frames_total": 2628,
            "frames_done": 1119,
            "results": [],
            "instruments": ["muscat3"],
            "obsdates": ["260102"],
            "objects": ["WASP-12"],
            "dest_dirs": ["/data/MuSCAT3/260102"],
            "started_at": 1700000000.0,
            "finished_at": None,
            "error": None,
        }],
    )
    r = TestClient(app).get("/jobs")
    assert r.status_code == 200
    assert "LCO Archive Downloads" in r.text
    assert 'data-type="lco_archive_download"' in r.text
    assert "1119/2628 frames" in r.text
    assert "/data/MuSCAT3/260102" in r.text
    assert "WASP-12" in r.text


def test_jobs_page_lco_archive_done_row_has_scan_ingest_buttons(mock_db, monkeypatch):
    monkeypatch.setattr(
        "muscat_db.lco.archive_download_jobs",
        lambda: [{
            "job_id": "abc123",
            "state": "done",
            "frames_total": 2,
            "frames_done": 2,
            "phase": "done",
            "funpack_total": 2,
            "funpack_done": 2,
            "results": [],
            "funpack_results": [{"status": "unpacked"}, {"status": "exists"}],
            "instruments": ["muscat3"],
            "obsdates": ["260102"],
            "objects": ["WASP-12"],
            "dest_dirs": ["/data/MuSCAT3/260102"],
            "started_at": 1700000000.0,
            "finished_at": 1700000010.0,
            "error": None,
        }],
    )
    r = TestClient(app).get("/jobs")
    assert r.status_code == 200
    assert "muscat-db scan muscat3 260102" in r.text
    assert "muscat-db ingest-date muscat3 260102" in r.text
    assert "lco-actions-head" in r.text
    assert "lco-actions-cell" in r.text
    assert 'data-lco-followup-ready="1"' in r.text
    assert "runLcoArchiveCommand(this, 'scan')" in r.text
    assert "runLcoArchiveCommand(this, 'ingest-date')" in r.text


def test_jobs_page_persists_lco_archive_done_row_across_refresh(mock_db, monkeypatch):
    completed_job = {
        "job_id": "abc123",
        "state": "done",
        "frames_total": 2,
        "frames_done": 2,
        "phase": "done",
        "funpack_total": 2,
        "funpack_done": 2,
        "results": [],
        "funpack_results": [{"status": "unpacked"}, {"status": "exists"}],
        "instruments": ["muscat3"],
        "obsdates": ["260102"],
        "objects": ["WASP-12"],
        "dest_dirs": ["/data/MuSCAT3/260102"],
        "started_at": 1700000000.0,
        "finished_at": 1700000010.0,
        "error": None,
    }
    monkeypatch.setattr("muscat_db.lco.archive_download_jobs", lambda: [completed_job])
    first = TestClient(app).get("/jobs")
    assert first.status_code == 200
    assert "muscat-db scan muscat3 260102" in first.text

    monkeypatch.setattr("muscat_db.lco.archive_download_jobs", lambda: [])
    refreshed = TestClient(app).get("/jobs")

    assert refreshed.status_code == 200
    assert "muscat-db scan muscat3 260102" in refreshed.text
    assert "muscat-db ingest-date muscat3 260102" in refreshed.text
    assert 'data-key="lco_archive_download:abc123"' in refreshed.text


def test_jobs_status_includes_lco_archive_download(mock_db, monkeypatch):
    monkeypatch.setattr(
        "muscat_db.lco.archive_download_jobs",
        lambda: [{
            "job_id": "abc123",
            "state": "running",
            "frames_total": 2628,
            "frames_done": 1119,
            "results": [],
            "instruments": ["muscat3"],
            "obsdates": ["260102"],
            "objects": ["WASP-12"],
            "dest_dirs": ["/data/MuSCAT3/260102"],
            "started_at": 1700000000.0,
            "finished_at": None,
            "error": None,
        }],
    )
    data = TestClient(app).get("/jobs/status").json()
    assert data["counts"]["running"] == 1
    assert data["running"][0]["key"] == "lco_archive_download:abc123"
    assert data["running"][0]["type"] == "lco_archive_download"
    assert data["running"][0]["inst"] == "muscat3"
    assert data["running"][0]["date"] == "260102"
    assert data["running"][0]["target"] == "WASP-12"
    assert data["running"][0]["run_name"] == "1119/2628 frames"
    assert data["running"][0]["details"] == "/data/MuSCAT3/260102"
    active = TestClient(app).get("/jobs/status?active_only=1").json()["active"]
    assert active == [{"key": "lco_archive_download:abc123", "state": "running"}]


def test_jobs_status_returns_terminal_lco_archive_even_if_baseline_missed(mock_db, monkeypatch):
    monkeypatch.setattr("muscat_db.web._last_running", set())
    monkeypatch.setattr(
        "muscat_db.lco.archive_download_jobs",
        lambda: [{
            "job_id": "abc123",
            "state": "done",
            "frames_total": 2,
            "frames_done": 2,
            "phase": "done",
            "funpack_total": 2,
            "funpack_done": 2,
            "results": [],
            "funpack_results": [{"status": "unpacked"}, {"status": "exists"}],
            "instruments": ["muscat3"],
            "obsdates": ["260102"],
            "objects": ["WASP-12"],
            "dest_dirs": ["/data/MuSCAT3/260102"],
            "started_at": 1700000000.0,
            "finished_at": 1700000010.0,
            "error": None,
        }],
    )

    data = TestClient(app).get("/jobs/status").json()

    finished = data["finished"]["lco_archive_download:abc123"]
    assert finished["key"] == "lco_archive_download:abc123"
    assert finished["type"] == "lco_archive_download"
    assert finished["inst"] == "muscat3"
    assert finished["date"] == "260102"
    assert finished["target"] == "WASP-12"
    assert finished["state"] == "done"
    assert finished["run_name"] == "2/2 frames"
    assert finished["details"] == "/data/MuSCAT3/260102"
    assert finished["action_inst"] == "muscat3"
    assert finished["action_date"] == "260102"
    assert finished["can_run_dataset_action"] is True


def test_jobs_lco_archive_scan_endpoint(mock_db, monkeypatch):
    called = {}

    def fake_scan(inst, obsdate):
        called["args"] = (inst, obsdate)
        return {"total": 2, "per_ccd": {0: 2}}

    monkeypatch.setattr("muscat_db.scanner.scan_date", fake_scan)
    r = TestClient(app).post("/jobs/lco-archive/scan", json={"inst": "muscat3", "date": "260102"})
    assert r.status_code == 200
    assert r.json()["command"] == "muscat-db scan muscat3 260102"
    assert called["args"] == ("muscat3", "260102")


def test_jobs_lco_archive_ingest_date_endpoint(mock_db, monkeypatch):
    called = {}

    def fake_ingest(db, inst, obsdate):
        called["args"] = (db, inst, obsdate)
        return 2

    monkeypatch.setattr("muscat_db.database.ingest_date", fake_ingest)
    r = TestClient(app).post("/jobs/lco-archive/ingest-date", json={"inst": "muscat3", "date": "260102"})
    assert r.status_code == 200
    assert r.json()["command"] == "muscat-db ingest-date muscat3 260102"
    assert r.json()["count"] == 2
    assert called["args"][1:] == ("muscat3", "260102")


def test_target_without_name_redirects_to_database_search(mock_db):
    response = TestClient(app).get("/target", follow_redirects=False)

    assert response.status_code == 303
    assert response.headers["location"] == "/"


def test_index_exposes_normalized_target_direct_link(mock_db, monkeypatch):
    monkeypatch.setattr(
        "muscat_db.web._get_targets",
        lambda _db: [{
            "object": "V1298Tau_b",
            "is_identified": True,
            "norm_name": "V1298TAU",
            "ra": "04:05:23.4940",
            "declination": "+20:11:36.595",
            "filters": ["gp", "rp"],
            "filter_chips": [
                {"label": "gp", "color": "g", "narrow": False},
                {"label": "rp", "color": "r", "narrow": False},
            ],
            "n_frames": 42,
            "airmass_min": 1.1,
            "airmass_max": 1.4,
            "instruments": ["muscat4"],
            "dates": ["260101"],
            "date_to_inst": {"260101": "muscat4"},
            "note": "young star",
        }],
    )

    response = TestClient(app).get("/")

    assert response.status_code == 200
    html = response.text
    assert "Normalized Target" in html
    assert 'data-norm-name="V1298TAU"' in html
    assert 'href="/target?name=V1298TAU"' in html
    assert "V1298Tau_b V1298TAU" in html


def test_target_detail_stores_last_viewed_target(mock_db, monkeypatch):
    monkeypatch.setattr(
        "muscat_db.web._get_datasets_for_normalized_target",
        lambda _db, norm_name: ([], "2026-07-01"),
    )

    response = TestClient(app).get("/target?name=V1298Tau_b")

    assert response.status_code == 200
    html = response.text
    assert 'id="target-nav-link" href="/target"' in html
    assert 'id="photometry-nav-link" href="/photometry"' in html
    assert 'id="transit-fit-nav-link" href="/transit-fit"' in html
    assert 'id="ephemeris-nav-link" href="/ephemeris"' in html
    assert "MuscatRouteState.rememberTarget(\"V1298TAU\")" in html


def test_ephemeris_targets_are_normalized_unique_names(mock_db):
    save_job(
        type_="transit_fit",
        inst="muscat4",
        date="260101",
        target="V1298Tau_b",
        state="done",
        returncode=0,
        elapsed=10,
        started_at=1000.0,
    )
    save_job(
        type_="transit_fit",
        inst="muscat4",
        date="260102",
        target="V1298Tauc",
        state="done",
        returncode=0,
        elapsed=12,
        started_at=1001.0,
    )
    save_job(
        type_="transit_fit",
        inst="sinistro",
        date="260103",
        target="HIP 67522",
        state="done",
        returncode=0,
        elapsed=14,
        started_at=1002.0,
    )

    response = TestClient(app).get("/api/ephemeris/targets")

    assert response.status_code == 200
    assert response.json()["targets"] == ["HIP67522", "V1298TAU"]


def test_jobs_rerun_restores_persisted_run_identity(mock_db, monkeypatch):
    import json

    save_job(
        type_="photometry",
        inst="muscat3",
        date="260101",
        target="WASP-12b",
        state="done",
        returncode=0,
        elapsed=100,
        started_at=1700000200.0,
        run_type="full",
        params=json.dumps({"test_run": False, "options": {"bands": ["gp"]}}),
        run_id="science_run",
        run_name="Science Run",
    )
    key = get_persisted_jobs()[0]["key"]
    captured = {}

    def fake_start_run(inst, date, target, options, test_run, user_name=None):
        captured.update(
            inst=inst,
            date=date,
            target=target,
            options=options,
            test_run=test_run,
            user_name=user_name,
        )
        return {"ok": True, "key": "rerun-key"}

    monkeypatch.setattr("muscat_db.web.phot.start_run", fake_start_run)

    response = TestClient(app).post("/jobs/rerun", json={"key": key})

    assert response.status_code == 200
    assert captured["options"]["bands"] == ["gp"]
    assert captured["options"]["run_name"] == "Science Run"
    assert captured["test_run"] is False


def test_validate_no_duplicate_datasets():
    import pathlib
    from muscat_db.transit_fit import validate_no_duplicate_datasets
    
    # 1. Non-sinistro (Muscat3): different bands -> OK
    csvs1 = [
        pathlib.Path("WASP-12b_muscat3_g_260101.csv"),
        pathlib.Path("WASP-12b_muscat3_r_260101.csv"),
    ]
    assert validate_no_duplicate_datasets("muscat3", "260101", csvs1) is None
    
    # 2. Non-sinistro (Muscat3): duplicate bands -> Error
    csvs2 = [
        pathlib.Path("WASP-12b_muscat3_g_260101.csv"),
        pathlib.Path("WASP-12b_muscat3_g_260101_run2.csv"),
    ]
    err = validate_no_duplicate_datasets("muscat3", "260101", csvs2)
    assert err is not None
    assert "Multiple lightcurves selected for the same band 'g'" in err

    # 3. Sinistro: same band but different sites -> OK
    csvs3 = [
        pathlib.Path("WASP-12b_sinistro_cpt_g_260101.csv"),
        pathlib.Path("WASP-12b_sinistro_lsc_g_260101.csv"),
    ]
    assert validate_no_duplicate_datasets("sinistro", "260101", csvs3) is None

    # 4. Sinistro: same band, same site -> Error
    csvs4 = [
        pathlib.Path("WASP-12b_sinistro_cpt_g_260101.csv"),
        pathlib.Path("WASP-12b_sinistro_cpt_g_260101_run2.csv"),
    ]
    err2 = validate_no_duplicate_datasets("sinistro", "260101", csvs4)
    assert err2 is not None
    assert "Multiple lightcurves selected for the same dataset: band 'g' (site: cpt)" in err2



# --------------------------------------------------------------------------- #
# LCO scheduling & archive endpoints (HTTP mocked — no live LCO calls)
# --------------------------------------------------------------------------- #


def test_lco_pages_render_and_nav_links_it():
    client = TestClient(app)
    page = client.get("/lco")
    assert page.status_code == 200
    assert "Schedule Observations" in page.text
    archive = client.get("/lco/archive")
    assert archive.status_code == 200
    assert "Search LCO Archive" in archive.text and "Download selected" in archive.text
    # Nav (from base.html) links to /lco/schedule on every page.
    assert 'href="/lco/schedule"' in client.get("/logs").text


def test_lco_config_reports_booleans_and_hides_token(monkeypatch):
    monkeypatch.setenv("LCO_API_TOKEN", "super-secret-token")
    monkeypatch.delenv("MUSCAT_LCO_DIR", raising=False)
    monkeypatch.delenv("MUSCAT_DATA_DIR", raising=False)
    client = TestClient(app)
    r = client.get("/api/lco/config")
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True
    assert body["token_configured"] is True
    assert body["global_token_configured"] is True
    assert body["user_token_configured"] is False
    assert body["token_source"] == "global"
    assert body["download_root_configured"] is False
    assert body["download_root"] is None
    assert body["submit_allowed"] is False
    assert "super-secret-token" not in r.text


def test_lco_settings_save_and_status_are_per_nginx_user(mock_db, monkeypatch):
    monkeypatch.setenv("MUSCAT_DB_SECRET", "settings-secret")
    monkeypatch.delenv("LCO_API_TOKEN", raising=False)
    # TestClient's default peer is ("testclient", 50000); the auth middleware
    # only honors X-Forwarded-User from a loopback peer, so simulate nginx.
    client = TestClient(app, client=("127.0.0.1", 12345))
    headers = {"X-Forwarded-User": "alice"}
    # POST requires a same-origin Origin/Referer header (CSRF defense); GETs don't.
    post_headers = {**headers, "Origin": "http://testserver"}

    missing = client.get("/api/settings/lco-token-status")
    assert missing.status_code == 401

    saved = client.post("/api/settings/lco-token", headers=post_headers, json={"token": "alice-token"})
    assert saved.status_code == 200
    assert saved.json()["user_token_configured"] is True
    assert "alice-token" not in saved.text

    status = client.get("/api/settings/lco-token-status", headers=headers).json()
    assert status["ok"] is True
    assert status["user"] == "alice"
    assert status["user_token_configured"] is True
    assert status["global_token_configured"] is False

    config = client.get("/api/lco/config", headers=headers).json()
    assert config["token_configured"] is True
    assert config["token_source"] == "user"
    assert "alice-token" not in str(config)


def test_lco_token_save_rejects_cross_origin_request(mock_db, monkeypatch):
    """A POST with a foreign Origin (or none at all) must not save the token.

    Regression test for the CSRF gap: relying on CORS preflight isn't enough
    since FastAPI parses the body as JSON regardless of declared Content-Type.
    """
    monkeypatch.setenv("MUSCAT_DB_SECRET", "settings-secret")
    client = TestClient(app, client=("127.0.0.1", 12345))
    headers = {"X-Forwarded-User": "alice"}

    no_origin = client.post("/api/settings/lco-token", headers=headers, json={"token": "x"})
    assert no_origin.status_code == 403

    foreign_origin = client.post(
        "/api/settings/lco-token",
        headers={**headers, "Origin": "http://evil.example"},
        json={"token": "x"},
    )
    assert foreign_origin.status_code == 403

    status = client.get("/api/settings/lco-token-status", headers=headers).json()
    assert status["user_token_configured"] is False


def test_lco_proposals_receive_nginx_user(mock_db, monkeypatch):
    captured = {}

    def fake_proposals(user_name=None, token=None):
        captured["user_name"] = user_name
        captured["token"] = token
        return {"results": [{"id": "TEST2026A"}], "count": 1}

    monkeypatch.setattr("muscat_db.lco.get_proposals", fake_proposals)
    r = TestClient(app, client=("127.0.0.1", 12345)).get(
        "/api/lco/proposals", headers={"X-Forwarded-User": "alice"}
    )
    assert r.status_code == 200
    assert captured == {"user_name": "alice", "token": None}


def test_x_forwarded_user_ignored_from_non_loopback_peer(monkeypatch):
    """A spoofed header from a non-loopback peer must not authenticate the user.

    This is the regression test for the auth-bypass this middleware fixes:
    previously any client reaching uvicorn directly (e.g. --nginx forgotten,
    or default 0.0.0.0 bind) could set X-Forwarded-User and impersonate.
    """
    captured = {}

    def fake_proposals(user_name=None, token=None):
        captured["user_name"] = user_name
        captured["token"] = token
        return {"results": [], "count": 0}

    monkeypatch.setattr("muscat_db.lco.get_proposals", fake_proposals)
    # Default TestClient peer ("testclient", 50000) is not loopback.
    r = TestClient(app).get("/api/lco/proposals", headers={"X-Forwarded-User": "mallory"})
    assert r.status_code == 200
    assert captured == {"user_name": None, "token": None}


def test_lco_config_exposes_download_root_path(monkeypatch):
    monkeypatch.setenv("MUSCAT_LCO_DIR", "/data")
    body = TestClient(app).get("/api/lco/config").json()
    assert body["download_root_configured"] is True
    assert body["download_root"] == "/data"


def test_lco_proposals_proxied(monkeypatch):
    monkeypatch.setattr("muscat_db.lco.get_proposals",
                        lambda token=None: {"results": [{"id": "TEST2026A"}], "count": 1})
    r = TestClient(app).get("/api/lco/proposals")
    assert r.status_code == 200
    assert r.json()["results"][0]["id"] == "TEST2026A"


def test_lco_windows_from_catalog_lookup(monkeypatch):
    monkeypatch.setattr(
        "muscat_db.web._query_target_planets_catalog",
        lambda target: {"b": {"t0": 2459000.5, "period": 2.0, "duration": 2.0}},
    )
    r = TestClient(app).post("/api/lco/windows", json={
        "target": "WASP-12", "planet": "b",
        "range_start": "2026-01-01", "range_end": "2026-01-10",
        "pad_before_min": 30, "pad_after_min": 30,
    })
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] and len(body["windows"]) >= 1
    assert body["duration"] == 2.0 and body["period"] == 2.0


def test_lco_windows_requires_duration_when_catalog_lacks_it(monkeypatch):
    monkeypatch.setattr(
        "muscat_db.web._query_target_planets_catalog",
        lambda target: {"b": {"t0": 2459000.5, "period": 2.0}},  # no duration
    )
    r = TestClient(app).post("/api/lco/windows", json={
        "target": "X", "planet": "b", "range_start": "2026-01-01", "range_end": "2026-01-10",
    })
    assert r.status_code == 400
    assert "duration" in r.json()["error"].lower()


def _ipp_params():
    return {
        "kind": "sinistro", "name": "s", "proposal": "TEST2026A",
        "target_name": "WASP-12 b", "ra": 97.64, "dec": 29.67,
        "filter": "rp", "exposure_time": 60,
        "windows": [{"start": "2026-01-01T00:00:00", "end": "2026-01-01T06:00:00"}],
    }


def test_lco_ipp_dry_run_returns_payload_and_hash(monkeypatch):
    monkeypatch.setattr("muscat_db.lco.max_allowable_ipp",
                        lambda payload, token=None: {"max_allowable_ipp_value": 1.5})
    r = TestClient(app).post("/api/lco/ipp", json=_ipp_params())
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] and body["payload_hash"]
    assert body["payload"]["requests"][0]["configurations"][0]["instrument_type"] == "1M0-SCICAM-SINISTRO"
    assert body["ipp"]["max_allowable_ipp_value"] == 1.5


def test_lco_submit_requires_confirm():
    r = TestClient(app).post("/api/lco/submit", json={**_ipp_params()})
    assert r.status_code == 400
    assert "confirm" in r.json()["error"].lower()


def test_lco_submit_rejected_without_matching_dry_run(monkeypatch):
    # submit_requestgroup must never be reached without a matching hash.
    monkeypatch.setattr("muscat_db.lco.submit_requestgroup",
                        lambda payload, token=None: (_ for _ in ()).throw(AssertionError("must not submit")))
    r = TestClient(app).post("/api/lco/submit",
                             json={**_ipp_params(), "confirm": True, "dry_run_hash": "deadbeef"})
    assert r.status_code == 409
    assert "dry-run" in r.json()["error"].lower()


def test_lco_submit_succeeds_with_matching_hash(monkeypatch):
    import muscat_db.lco as _lco
    params = _ipp_params()
    good_hash = _lco.payload_hash(_lco.build_requestgroup(params["kind"], params))
    monkeypatch.setattr("muscat_db.lco.submit_requestgroup",
                        lambda payload, token=None: {"id": 12345, "state": "PENDING"})
    r = TestClient(app).post("/api/lco/submit",
                             json={**params, "confirm": True, "dry_run_hash": good_hash})
    assert r.status_code == 200
    assert r.json()["result"]["id"] == 12345


def test_lco_archive_frames_search(monkeypatch):
    monkeypatch.setattr(
        "muscat_db.lco.archive_search",
        lambda filters, token=None: {"count": 1, "results": [{"filename": "ogg2m001-ep05-20260102-0001-e91.fits.fz", "SITEID": "ogg", "TELID": "2m0a"}]},
    )
    r = TestClient(app).get("/api/lco/archive/frames", params={"OBJECT": "WASP-12", "limit": "10", "fuzzy_name": "1"})
    assert r.status_code == 200
    assert r.json()["match_mode"] == "name"
    assert r.json()["results"][0]["SITEID"] == "ogg"
    assert r.json()["results"][0]["archive_instrument"] == "muscat3"


def test_lco_archive_frames_by_request_id(monkeypatch):
    captured = {}

    def _fake_search_all(filters, max_frames=5000):
        captured.update(filters)
        return {
            "count": 2,
            "truncated": False,
            "results": [
                {"filename": "coj2m002-ep07-20260703-0874-e91.fits.fz", "SITEID": "coj", "TELID": "2m0a", "INSTRUME": "ep07", "OBJECT": "TOI-4381", "DATE_OBS": "2026-07-03T10:00:00"},
                {"filename": "coj2m002-ep08-20260703-0874-e91.fits.fz", "SITEID": "coj", "TELID": "2m0a", "INSTRUME": "ep08", "OBJECT": "TOI-4381", "DATE_OBS": "2026-07-03T10:00:00"},
            ],
        }

    # Should not touch the coordinate resolver at all on the request-id path.
    def _boom(name):
        raise AssertionError("coordinate resolution must be skipped for request_id")

    monkeypatch.setattr("muscat_db.lco.archive_search_all", _fake_search_all)
    monkeypatch.setattr("muscat_db.web._resolve_archive_coords", _boom)

    r = TestClient(app).get("/api/lco/archive/frames", params={"request_id": "4236675", "reduction_level": "91"})
    assert r.status_code == 200
    data = r.json()
    assert data["match_mode"] == "request_id"
    assert data["request_id"] == "4236675"
    assert data["count"] == 2
    assert captured == {"request_id": "4236675", "reduction_level": "91", "limit": "1000"}
    assert data["results"][0]["archive_instrument"] == "muscat4"


def test_lco_archive_frames_request_id_must_be_numeric():
    r = TestClient(app).get("/api/lco/archive/frames", params={"request_id": "4236675abc"})
    assert r.status_code == 400
    assert r.json()["ok"] is False


def test_lco_archive_frames_coordinate_primary_by_default(monkeypatch):
    captured = {}

    def _fake_search(filters, token=None):
        captured.update(filters)
        return {"count": 1, "results": [{"filename": "ogg2m001-ep05-20260102-0001-e91.fits.fz", "SITEID": "ogg", "TELID": "2m0a"}]}

    monkeypatch.setattr("muscat_db.lco.archive_search", _fake_search)
    monkeypatch.setattr("muscat_db.web._resolve_archive_coords", lambda name: (97.6367, 29.6725, "catalog"))

    r = TestClient(app).get("/api/lco/archive/frames", params={"OBJECT": "WASP-12", "limit": "10"})
    assert r.status_code == 200
    data = r.json()
    # Coordinate-primary: query by footprint coverage, not OBJECT header text.
    assert captured.get("covers") == "POINT(97.6367 29.6725)"
    assert "OBJECT" not in captured
    assert data["match_mode"] == "coord"
    assert data["resolved_ra"] == 97.6367
    assert data["resolved_source"] == "catalog"


def test_lco_archive_frames_coordinate_unresolved_returns_422(monkeypatch):
    monkeypatch.setattr("muscat_db.lco.archive_search", lambda filters, token=None: {"count": 0, "results": []})
    monkeypatch.setattr("muscat_db.web._resolve_archive_coords", lambda name: None)

    r = TestClient(app).get("/api/lco/archive/frames", params={"OBJECT": "NoSuchTarget"})
    assert r.status_code == 422
    assert r.json()["ok"] is False


def test_lco_archive_frames_coordinate_requires_name(monkeypatch):
    monkeypatch.setattr("muscat_db.lco.archive_search", lambda filters, token=None: {"count": 0, "results": []})
    r = TestClient(app).get("/api/lco/archive/frames", params={"limit": "10"})
    assert r.status_code == 400


def test_lco_archive_frames_telescope_class_filters_locally(monkeypatch):
    monkeypatch.setattr(
        "muscat_db.lco.archive_search",
        lambda filters, token=None: {
            "count": 2,
            "results": [
                {"filename": "a.fits.fz", "SITEID": "ogg", "TELID": "2m0a"},
                {"filename": "b.fits.fz", "SITEID": "ogg", "TELID": "1m0a"},
            ],
        },
    )
    r = TestClient(app).get("/api/lco/archive/frames", params={"TELID": "2m0", "fuzzy_name": "1"})
    assert r.status_code == 200
    data = r.json()
    assert data["count"] == 1
    assert data["results"][0]["TELID"] == "2m0a"


def test_lco_archive_frames_groups_overnight_dataset_and_marks_existing(mock_db, monkeypatch):
    conn = sqlite3.connect(mock_db)
    conn.execute(
        "INSERT INTO frames (instrument, obsdate, ccd, filename, object) VALUES (?, ?, ?, ?, ?)",
        ("muscat3", "260101", 0, "ogg2m001-ep05-20260102-0001-e91.fits.fz", "WASP-12"),
    )
    conn.commit()
    conn.close()

    monkeypatch.setattr(
        "muscat_db.lco.archive_search",
        lambda filters, token=None: {
            "count": 2,
            "results": [
                {
                    "filename": "ogg2m001-ep05-20260102-0001-e91.fits.fz",
                    "OBJECT": "WASP-12",
                    "SITEID": "ogg",
                    "TELID": "2m0a",
                    "INSTRUME": "ep05",
                    "DATE_OBS": "2026-01-02T08:00:00Z",
                },
                {
                    "filename": "ogg2m001-ep05-20260102-0002-e91.fits.fz",
                    "OBJECT": "WASP-12",
                    "SITEID": "ogg",
                    "TELID": "2m0a",
                    "INSTRUME": "ep05",
                    "DATE_OBS": "2026-01-02T10:00:00Z",
                },
            ],
        },
    )

    r = TestClient(app).get(
        "/api/lco/archive/frames",
        params={"instrument": "muscat3", "OBJECT": "WASP-12", "limit": "10", "fuzzy_name": "1"},
    )
    assert r.status_code == 200
    data = r.json()
    assert data["dataset_count"] == 1
    assert data["results"][0]["dataset_date"] == "2026-01-01"
    assert data["results"][0]["dataset_id"] == data["results"][1]["dataset_id"]
    assert data["results"][0]["dataset_exists"] is True
    assert data["results"][0]["dataset_existing_count"] == 1
    assert data["results"][0]["dataset_frame_count"] == 2


def test_lco_archive_frames_same_date_same_target_same_site_stay_one_dataset(mock_db, monkeypatch):
    monkeypatch.setattr(
        "muscat_db.lco.archive_search",
        lambda filters, token=None: {
            "count": 3,
            "results": [
                {
                    "filename": "lsc1m001-fa01-20260102-0001-e91.fits.fz",
                    "OBJECT": "WASP-12",
                    "SITEID": "lsc",
                    "TELID": "1m0a",
                    "INSTRUME": "fa01",
                    "DATE_OBS": "2026-01-02T01:00:00Z",
                },
                {
                    "filename": "lsc1m002-fa02-20260102-0002-e91.fits.fz",
                    "OBJECT": "WASP-12",
                    "SITEID": "lsc",
                    "TELID": "1m0b",
                    "INSTRUME": "fa02",
                    "DATE_OBS": "2026-01-02T02:00:00Z",
                },
                {
                    "filename": "lsc1m001-fa01-20260102-0003-e91.fits.fz",
                    "OBJECT": "WASP-12",
                    "SITEID": "lsc",
                    "TELID": "1m0a",
                    "INSTRUME": "fa01",
                    "DATE_OBS": "2026-01-02T09:00:00Z",
                },
            ],
        },
    )

    r = TestClient(app).get(
        "/api/lco/archive/frames",
        params={"instrument": "sinistro", "OBJECT": "WASP-12", "limit": "10", "fuzzy_name": "1"},
    )
    assert r.status_code == 200
    data = r.json()
    assert data["dataset_count"] == 1
    ids = {row["dataset_id"] for row in data["results"]}
    assert len(ids) == 1


def test_lco_archive_dataset_exists_matches_by_coordinates_not_name(mock_db):
    conn = sqlite3.connect(mock_db)
    conn.execute(
        "INSERT INTO frames (instrument, obsdate, ccd, filename, object, ra, declination) VALUES (?, ?, ?, ?, ?, ?, ?)",
        ("muscat3", "260101", 0, "ogg2m001-ep05-20260101-0009-e91.fits.fz", "Alias Target", "06:30:00", "+29:40:00"),
    )
    conn.commit()
    conn.close()

    out, n = _annotate_lco_archive_results(
        "muscat3",
        [
            {
                "filename": "ogg2m001-ep05-20260102-0001-e91.fits.fz",
                "OBJECT": "WASP-12",
                "SITEID": "ogg",
                "TELID": "2m0a",
                "INSTRUME": "ep05",
                "DATE_OBS": "2026-01-02T08:00:00Z",
                "RA": 97.5,
                "DEC": 29.6666667,
            }
        ],
    )
    assert n == 1
    assert out[0]["dataset_exists"] is True
    assert out[0]["dataset_existing_count"] == 1
    assert out[0]["dataset_matched_object"] == "Alias Target"


def test_lco_archive_download_per_file_results(monkeypatch):
    monkeypatch.setattr(
        "muscat_db.lco.download_frames",
        lambda frames, overwrite=False: [{"filename": "f.fits", "instrument": "muscat3", "status": "downloaded", "bytes": 1024}],
    )
    r = TestClient(app).post("/api/lco/archive/download", json={
        "frames": [{"filename": "ogg2m001-ep05-20260102-0001-e91.fits.fz", "SITEID": "ogg", "TELID": "2m0a", "url": "https://x/y", "DAY_OBS": "2026-01-01"}],
    })
    assert r.status_code == 200
    assert r.json()["results"][0]["status"] == "downloaded"
    assert r.json()["results"][0]["instrument"] == "muscat3"


def test_lco_archive_download_can_start_background_job(monkeypatch):
    def fake_start(frames, overwrite=False):
        assert overwrite is True
        assert frames[0]["filename"] == "ogg2m001-ep05-20260102-0001-e91.fits.fz"
        return {
            "job_id": "job123",
            "state": "pending",
            "frames_total": 1,
            "frames_done": 0,
            "results": [],
            "started_at": 123.0,
            "finished_at": None,
            "error": None,
        }

    monkeypatch.setattr("muscat_db.lco.start_archive_download", fake_start)
    r = TestClient(app).post("/api/lco/archive/download", json={
        "background": True,
        "overwrite": True,
        "frames": [{"filename": "ogg2m001-ep05-20260102-0001-e91.fits.fz", "SITEID": "ogg", "TELID": "2m0a"}],
    })
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True
    assert body["job_id"] == "job123"
    assert body["state"] == "pending"
    assert body["frames_done"] == 0


def test_lco_archive_download_status_endpoint(mock_db, monkeypatch):
    monkeypatch.setattr(
        "muscat_db.lco.archive_download_status",
        lambda job_id: {
            "job_id": job_id,
            "state": "done",
            "frames_total": 1,
            "frames_done": 1,
            "results": [{"filename": "f.fits", "status": "downloaded"}],
            "started_at": 123.0,
            "finished_at": 124.0,
            "error": None,
        },
    )
    r = TestClient(app).get("/api/lco/archive/download/job123")
    assert r.status_code == 200
    assert r.json()["state"] == "done"
    assert r.json()["results"][0]["filename"] == "f.fits"


def test_lco_archive_download_status_endpoint_persists_done_job(mock_db, monkeypatch):
    monkeypatch.setattr(
        "muscat_db.lco.archive_download_status",
        lambda job_id: {
            "job_id": job_id,
            "state": "done",
            "frames_total": 2,
            "frames_done": 2,
            "phase": "done",
            "funpack_total": 2,
            "funpack_done": 2,
            "results": [],
            "funpack_results": [{"status": "unpacked"}, {"status": "exists"}],
            "instruments": ["muscat3"],
            "obsdates": ["260102"],
            "objects": ["WASP-12"],
            "dest_dirs": ["/data/MuSCAT3/260102"],
            "started_at": 1700000000.0,
            "finished_at": 1700000010.0,
            "error": None,
        },
    )

    r = TestClient(app).get("/api/lco/archive/download/abc123")

    assert r.status_code == 200
    monkeypatch.setattr("muscat_db.lco.archive_download_jobs", lambda: [])
    refreshed = TestClient(app).get("/jobs")
    assert "muscat-db scan muscat3 260102" in refreshed.text
    assert 'data-key="lco_archive_download:abc123"' in refreshed.text


def test_lco_schedule_page_has_obs_column_constraints_plotly_and_persistence():
    page = TestClient(app).get("/lco/schedule").text
    # Observability column + filter.
    assert "<th>Transit obs</th>" in page and "<th>Visibility</th>" in page
    assert 'id="win-filter"' in page
    # Configurable constraints and coordinates.
    assert 'id="sch-obs-airmass"' in page and 'id="sch-twilight"' in page and 'id="sch-moon-sep"' in page
    assert 'id="sch-coords"' in page
    # Inline astropy figure drawn with Plotly.
    assert "plotly-2.24.1" in page and 'id="vis-plot"' in page and "/api/lco/visibility" in page
    # Dynamic cross-check link to the LCO-generated visibility PNG (target/date-specific).
    assert "visibility.lco.global/visibility.png" in page
    assert 'id="lco-vis-link"' in page and 'target="_blank"' in page
    # Schedule state persists across reloads.
    assert "lco:schedule:options" in page and "lco:schedule:state" in page


def test_lco_archive_page_has_archive_persistence():
    page = TestClient(app).get("/lco/archive").text
    assert "Search LCO Archive" in page and "Download selected" in page
    assert "lco:archive:options" in page and "lco:archive:state" in page
    assert "Save under instrument" not in page


def test_lco_archive_download_rejects_unknown_inferred_instrument():
    r = TestClient(app).post("/api/lco/archive/download",
                             json={"frames": [{"filename": "mystery.fits", "url": "https://x/y", "DAY_OBS": "2026-01-01"}]})
    assert r.status_code == 200
    assert r.json()["results"][0]["status"] == "error"
    assert "infer destination instrument" in r.json()["results"][0]["error"]


def test_lco_windows_source_nasa_uses_nasa_catalog(monkeypatch):
    called = {}
    def fake_nasa(target):
        called["nasa"] = True
        return {"b": {"t0": 2459000.5, "period": 1.09, "duration": 3.0}}
    monkeypatch.setattr("muscat_db.web._query_target_planets_nasa", fake_nasa)
    monkeypatch.setattr("muscat_db.web._query_target_planets_toi",
                        lambda t: pytest.fail("TOI must not be queried for source=nasa"))
    r = TestClient(app).post("/api/lco/windows", json={
        "target": "X", "planet": "b", "source": "nasa",
        "range_start": "2026-01-01", "range_end": "2026-01-05",
    })
    assert r.status_code == 200 and r.json()["ok"]
    assert called.get("nasa") and r.json()["duration"] == 3.0


@pytest.mark.parametrize("source", ["linear", "dataset_0"])
def test_lco_windows_fit_sources_require_prefetched_ephemeris(source):
    # The linear fit and individual datasets are resolved client-side; without
    # t0/period the endpoint must guide the user to Fetch first rather than
    # silently falling back to a catalog.
    r = TestClient(app).post("/api/lco/windows", json={
        "target": "X", "planet": "b", "source": source,
        "range_start": "2026-01-01", "range_end": "2026-01-05",
    })
    assert r.status_code == 400
    assert "fetch ephemeris" in r.json()["error"].lower()


def test_lco_windows_explicit_t0_period_bypasses_source(monkeypatch):
    monkeypatch.setattr("muscat_db.web._query_target_planets_catalog",
                        lambda t: pytest.fail("catalog must not be queried when t0/period given"))
    r = TestClient(app).post("/api/lco/windows", json={
        "t0": 2459000.5, "period": 2.0, "duration": 2.0, "source": "linear",
        "range_start": "2026-01-01", "range_end": "2026-01-10",
    })
    assert r.status_code == 200 and r.json()["ok"]
    assert len(r.json()["windows"]) >= 1


# --------------------------------------------------------------------------- #
# Transit observability (astropy) + visibility plot endpoint
# --------------------------------------------------------------------------- #


def test_lco_windows_attaches_observability(monkeypatch):
    monkeypatch.setattr(
        "muscat_db.web._query_target_planets_catalog",
        lambda target: {"b": {"t0": 2461080.0, "period": 1.0, "duration": 2.5}},
    )
    r = TestClient(app).post("/api/lco/windows", json={
        "target": "X", "planet": "b", "source": "catalog", "kind": "muscat",
        "ra": 97.64, "dec": 29.67, "range_start": "2026-03-15", "range_end": "2026-03-18",
        "obs_airmass": 2.0, "twilight": "nautical", "moon_sep_min": 30,
    })
    assert r.status_code == 200
    wins = r.json()["windows"]
    assert wins and all("observability" in w for w in wins)
    for w in wins:
        assert w["observability"]["rating"] in ("full", "partial", "none")


def test_lco_windows_omits_observability_without_radec(monkeypatch):
    monkeypatch.setattr(
        "muscat_db.web._query_target_planets_catalog",
        lambda target: {"b": {"t0": 2461080.0, "period": 1.0, "duration": 2.5}},
    )
    r = TestClient(app).post("/api/lco/windows", json={
        "target": "X", "planet": "b", "source": "catalog",
        "range_start": "2026-03-15", "range_end": "2026-03-17",
    })
    assert r.status_code == 200
    assert all("observability" not in w for w in r.json()["windows"])


def test_lco_windows_degrades_gracefully_on_obs_error(monkeypatch):
    monkeypatch.setattr(
        "muscat_db.web._query_target_planets_catalog",
        lambda target: {"b": {"t0": 2461080.0, "period": 1.0, "duration": 2.5}},
    )

    def boom(*a, **k):
        raise RuntimeError("astropy exploded")

    monkeypatch.setattr("muscat_db.transit_obs.classify_transits", boom)
    r = TestClient(app).post("/api/lco/windows", json={
        "target": "X", "planet": "b", "source": "catalog", "kind": "muscat",
        "ra": 97.64, "dec": 29.67, "range_start": "2026-03-15", "range_end": "2026-03-17",
    })
    assert r.status_code == 200
    body = r.json()
    assert body["windows"] and "obs_error" in body  # windows still returned


def test_lco_visibility_endpoint_returns_series():
    r = TestClient(app).get("/api/lco/visibility", params={
        "ra": 97.64, "dec": 29.67, "mid": "2026-03-15T10:00:00",
        "duration": 2.5, "site": "ogg", "obs_airmass": 2.0,
        "twilight": "nautical", "moon_sep_min": 30,
    })
    assert r.status_code == 200
    d = r.json()
    assert d["ok"] and d["site"] == "ogg"
    n = len(d["times"])
    assert n > 100 and len(d["target_alt"]) == n and len(d["moon_alt"]) == n
    assert d["ingress"] < d["egress"] and d["alt_limit"] == 30.0


def test_lco_visibility_endpoint_rejects_unknown_site():
    r = TestClient(app).get("/api/lco/visibility", params={
        "ra": 97.64, "dec": 29.67, "mid": "2026-03-15T10:00:00",
        "duration": 2.5, "site": "jwst",
    })
    assert r.status_code == 400
    assert "site" in r.json()["error"].lower()


# --------------------------- TOI page: Boyle2026 merge ------------------------

def _boyle_cat_data(tics):
    """Minimal TOI cat_data with just the tic column (all _merge_boyle_columns needs)."""
    return {"tic": tics}


def test_merge_boyle_columns_joins_by_tic_id(monkeypatch):
    from muscat_db import web
    cols = {k: [] for k, _ in web._BOYLE_COLUMNS}
    cols["ruwe"] = [1.5, 2.5]
    cols["non_single_star"] = [0, 1]
    cols["adopted_period"] = [3.25, None]
    cols["adopted_period_unc"] = [0.04, None]
    cols["flag_multiple_periods"] = [0, 1]
    cols["flag_possible_binary"] = [1, 0]
    cols["final_n_contams"] = [0.0, 2.0]
    cols["flag_doubled_period"] = [0, 0]
    cols["n_secs"] = [2, 5]
    cols["n_sec_ratio"] = [1.0, 0.5]
    cols["median_amplitude"] = [0.94, 1.7]
    cols["sectors"] = ["38,65", "1,2"]
    cols["sector_periods"] = ["3.25,3.28", "9.9,9.8"]
    monkeypatch.setattr(web, "_load_boyle_catalog", lambda: (cols, {358: 0, 529: 1}))

    merged, n = web._merge_boyle_columns(_boyle_cat_data(["358", "999", "TIC 529"]))
    assert n == 2
    assert merged["ruwe"] == [1.5, None, 2.5]           # row 1 unmatched -> None
    assert merged["flag_possible_binary"] == [1, None, 0]
    assert merged["sectors"] == ["38,65", "", "1,2"]    # string columns default ""
    assert merged["adopted_period"] == [3.25, None, None]


def test_merge_boyle_columns_degrades_without_catalog(monkeypatch, tmp_path):
    from muscat_db import web
    monkeypatch.setattr(web, "_BOYLE_PATH", tmp_path / "missing.feather")
    web._boyle_cache.clear()
    merged, n = web._merge_boyle_columns(_boyle_cat_data(["358", ""]))
    assert n == 0
    assert merged["ruwe"] == [None, None]
    assert merged["sectors"] == ["", ""]


def test_toi_page_includes_boyle_payload(monkeypatch):
    from muscat_db import web
    cols = {k: [None] for k, _ in web._BOYLE_COLUMNS}
    cols["ruwe"] = [1.01]
    cols["sectors"] = ["38,65"]
    cols["sector_periods"] = ["2.19,2.19"]
    monkeypatch.setattr(web, "_load_boyle_catalog", lambda: (cols, {50365310: 0}))
    monkeypatch.setattr(web, "_load_toi_catalog", lambda: {
        "data": {
            "toi": ["100.01"], "tic": ["50365310"], "name": [""], "disp": ["PC"],
            "period": [1.0], "duration": [2.0], "depth": [500.0], "radius": [1.2],
            "teq": [900.0], "insol": [10.0], "tmag": [9.5], "steff": [5000.0],
            "srad": [0.9], "dist": [50.0], "ra": [10.0], "dec": [-20.0],
            "period_err": [None], "duration_err": [None], "depth_err": [None],
            "radius_err": [None], "tmag_err": [None], "steff_err": [None],
            "srad_err": [None], "dist_err": [None],
        },
        "n": 1, "updated": "2026-07-01",
    })
    r = TestClient(app).get("/toi")
    assert r.status_code == 200
    assert '"ruwe":[1.01]' in r.text
    assert '"sector_periods":["2.19,2.19"]' in r.text
    assert "Boyle2026" in r.text
    # Fast-rotator (P_rot < 10 d) filter chip and its Boyle+2026 provenance note.
    # The filter itself runs client-side, but the chip markup, the note element,
    # and the arXiv citation link are rendered server-side and must be present.
    assert 'data-group="rot"' in r.text
    assert 'data-key="fast"' in r.text
    assert 'id="toi-rot-note"' in r.text
    assert "arxiv.org/abs/2603.05586" in r.text
    # "In muscat-db" membership filter chip.
    assert 'data-group="indb"' in r.text


# ── /nexsci (NASA Exoplanet Archive) page ──────────────────────────────────
def _nexsci_cat_data(names, hosts, tics):
    """Build a full nexsci column dict (all keys the loader produces) with the
    given string columns and null numerics, for monkeypatching the loader."""
    n = len(names)
    str_keys = ["name", "host", "tic", "method", "facility", "spectype"]
    num_keys = ["year", "ra", "dec", "period", "sma", "radius", "mass", "teq",
                "insol", "steff", "srad", "smass", "dist", "vmag", "tmag", "gmag", "kmag"]
    data = {k: [""] * n for k in str_keys}
    data.update({k: [None] * n for k in num_keys})
    data["name"] = list(names)
    data["host"] = list(hosts)
    data["tic"] = list(tics)
    return data


def test_nexsci_page_renders_with_payload_and_archive_link(mock_db, monkeypatch):
    from muscat_db import web
    web._toi_db_cache.clear()
    data = _nexsci_cat_data(
        ["TOI-2000 b", "Kepler-999 b"],
        ["TOI-2000", "Kepler-999"],
        ["TIC 273875149", "TIC 999999999"],
    )
    data["method"] = ["Transit", "Radial Velocity"]
    data["radius"] = [2.5, 11.0]
    data["period"] = [3.1, 400.0]
    monkeypatch.setattr(
        web, "_load_nexsci_catalog", lambda: {"data": data, "n": 2, "updated": "2026-07-05"}
    )
    r = TestClient(app).get("/nexsci")
    assert r.status_code == 200
    # Column-oriented JSON payload is embedded verbatim.
    assert '"name":["TOI-2000 b","Kepler-999 b"]' in r.text
    # Empty targets table -> nothing is in muscat-db.
    assert '"indb":[0,0]' in r.text
    # Archive-overview fallback URL prefix is present in the page JS.
    assert "exoplanetarchive.ipac.caltech.edu/overview/" in r.text
    # Nav link renders beside TOI.
    assert 'href="/nexsci"' in r.text


def test_nexsci_nav_link_present_on_other_pages(mock_db):
    body = TestClient(app).get("/logs").text
    assert 'href="/nexsci"' in body
    assert "NExScI" in body


def test_nexsci_db_membership_matches_tic_and_host(mock_db):
    from muscat_db import web
    web._toi_db_cache.clear()
    conn = sqlite3.connect(mock_db)
    conn.executemany(
        "INSERT INTO targets (object, n_dates, n_frames, is_identified, phot_status, fit_status)"
        " VALUES (?, 0, 0, 1, 'none', 'none')",
        [("TIC 12345",), ("TOI-2000",)],
    )
    conn.commit()
    conn.close()
    data = _nexsci_cat_data(
        ["Foo b", "TOI-2000 b", "Bar c"],
        ["Foo", "TOI-2000", "Bar"],
        ["TIC 12345", "", "TIC 777"],
    )
    indb, tname = web._nexsci_db_membership(data, mock_db)
    # row0 matched by TIC, row1 by normalized host name, row2 not in DB.
    assert indb == [1, 1, 0]
    assert tname == ["TIC12345", "TOI2000", ""]


def test_photometry_download_all_endpoints(mock_db, monkeypatch, tmp_path):
    # Mock MUSCAT_PROSE_DIR environment variable
    monkeypatch.setenv("MUSCAT_PROSE_DIR", str(tmp_path))

    # Create output directories for legacy and named run
    inst, date, target = "muscat3", "260101", "WASP12"

    # Legacy output files
    legacy_dir = tmp_path / inst / date
    legacy_dir.mkdir(parents=True)

    csv_file = legacy_dir / "WASP12_muscat3_gp_260101.csv"
    csv_file.write_text("BJD,flux\n1,1")

    log_file = legacy_dir / "WASP12_muscat3_260101.log"
    log_file.write_text("INFO: log content")

    npz_file = legacy_dir / "WASP12_muscat3_260101.npz"
    npz_file.write_text("dummy npz")

    # Other target's file in legacy dir (should NOT be zipped)
    other_csv = legacy_dir / "HAT-P-1_muscat3_gp_260101.csv"
    other_csv.write_text("BJD,flux\n1,1")

    # Named run output files
    run_id = "test-run"
    run_dir = legacy_dir / "_runs" / "WASP12" / "test-run"
    run_dir.mkdir(parents=True)

    run_csv = run_dir / "WASP12_muscat3_gp_260101.csv"
    run_csv.write_text("BJD,flux\n2,2")

    # Test client
    client = TestClient(app)

    # 1. Test legacy download-all
    r = client.get(f"/photometry/download-all/{inst}/{date}/{target}")
    assert r.status_code == 200
    assert r.headers["content-type"] == "application/zip"

    import zipfile
    import io
    with zipfile.ZipFile(io.BytesIO(r.content)) as z:
        namelist = z.namelist()
        assert "WASP12_muscat3_gp_260101.csv" in namelist
        assert "WASP12_muscat3_260101.log" in namelist
        assert "WASP12_muscat3_260101.npz" in namelist
        assert "HAT-P-1_muscat3_gp_260101.csv" not in namelist

    # 2. Test named run download-all
    r = client.get(f"/photometry/download-all/{inst}/{date}/{target}/run/{run_id}")
    assert r.status_code == 200
    with zipfile.ZipFile(io.BytesIO(r.content)) as z:
        namelist = z.namelist()
        assert "WASP12_muscat3_gp_260101.csv" in namelist


def test_transit_fit_download_all_endpoints(mock_db, monkeypatch, tmp_path):
    # Mock MUSCAT_TIMER_DIR environment variable
    monkeypatch.setenv("MUSCAT_TIMER_DIR", str(tmp_path))

    inst, date, target = "muscat3", "260101", "WASP12"

    # Legacy fit output files
    legacy_dir = tmp_path / inst / date / "WASP12"
    legacy_dir.mkdir(parents=True)

    fit_yaml = legacy_dir / "fit.yaml"
    fit_yaml.write_text("fit settings")
    sys_yaml = legacy_dir / "sys.yaml"
    sys_yaml.write_text("sys settings")

    # Legacy fit plots in out/
    out_dir = legacy_dir / "out"
    out_dir.mkdir()
    plot_file = out_dir / "fit.png"
    plot_file.write_text("dummy png")

    # Subdirectory of a named run inside the legacy directory (should NOT be zipped in legacy download)
    named_run_dir = legacy_dir / "run-abc"
    named_run_dir.mkdir()
    named_run_fit = named_run_dir / "fit.yaml"
    named_run_fit.write_text("named run fit settings")

    # Test client
    client = TestClient(app)

    # 1. Test legacy transit-fit download-all
    r = client.get(f"/transit-fit/download-all/{inst}/{date}/{target}")
    assert r.status_code == 200
    assert r.headers["content-type"] == "application/zip"

    import zipfile
    import io
    with zipfile.ZipFile(io.BytesIO(r.content)) as z:
        namelist = z.namelist()
        assert "fit.yaml" in namelist
        assert "sys.yaml" in namelist
        assert "out/fit.png" in namelist
        # Make sure it did not recurse into run-abc
        assert "run-abc/fit.yaml" not in namelist

    # 2. Test named run transit-fit download-all
    r = client.get(f"/transit-fit/download-all/{inst}/{date}/{target}/run/run-abc")
    assert r.status_code == 200
    with zipfile.ZipFile(io.BytesIO(r.content)) as z:
        namelist = z.namelist()
        assert "fit.yaml" in namelist


def test_api_target_publications_token_missing(monkeypatch):
    monkeypatch.delenv("ADS_API_TOKEN", raising=False)
    monkeypatch.delenv("ADS_DEV_KEY", raising=False)
    monkeypatch.delenv("ADS_TOKEN", raising=False)
    
    r = TestClient(app).get("/api/target/publications", params={"q": "WASP-12"})
    assert r.status_code == 400
    assert r.json()["token_missing"] is True
    assert "not configured" in r.json()["error"]


def test_api_target_publications_success(monkeypatch, mocker):
    monkeypatch.setenv("ADS_API_TOKEN", "fake_token")
    
    mock_response = mocker.MagicMock()
    mock_response.__enter__.return_value = mock_response
    mock_response.read.return_value = b'{"response": {"docs": [{"bibcode": "2020ApJ...123..456A", "title": ["A Great Paper"], "author": ["Astronomer, A."], "pubdate": "2020-01-00", "pub": "ApJ", "citation_count": 10}]}}'
    mock_urlopen = mocker.patch("urllib.request.urlopen", return_value=mock_response)
    
    r = TestClient(app).get("/api/target/publications", params={"q": "WASP-12"})
    assert r.status_code == 200
    
    called_req = mock_urlopen.call_args[0][0]
    called_url = called_req.get_full_url() if hasattr(called_req, "get_full_url") else str(called_req)
    assert "fq=collection%3Aastronomy" in called_url
    assert "q=WASP-12" in called_url

    data = r.json()
    assert data["ok"] is True
    assert len(data["papers"]) == 1
    assert data["papers"][0]["bibcode"] == "2020ApJ...123..456A"
    assert data["papers"][0]["title"] == ["A Great Paper"]
    assert data["papers"][0]["author"] == ["Astronomer, A."]



def test_api_target_publications_empty_query():
    r = TestClient(app).get("/api/target/publications", params={"q": ""})
    assert r.status_code == 400
    assert r.json()["ok"] is False


def test_api_ads_config_configured(monkeypatch):
    monkeypatch.setenv("ADS_API_TOKEN", "fake_token")
    r = TestClient(app).get("/api/ads/config")
    assert r.status_code == 200
    assert r.json()["token_configured"] is True


def test_api_ads_config_not_configured(monkeypatch):
    monkeypatch.delenv("ADS_API_TOKEN", raising=False)
    monkeypatch.delenv("ADS_DEV_KEY", raising=False)
    monkeypatch.delenv("ADS_TOKEN", raising=False)
    r = TestClient(app).get("/api/ads/config")
    assert r.status_code == 200
    assert r.json()["token_configured"] is False
