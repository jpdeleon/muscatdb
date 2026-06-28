from __future__ import annotations

import os
import sqlite3
import tempfile
import getpass
import pytest
from fastapi.testclient import TestClient

from muscat_db.database import save_job, db_path, get_persisted_jobs
from muscat_db.web import app

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


def test_lco_page_renders_and_nav_links_it():
    client = TestClient(app)
    page = client.get("/lco")
    assert page.status_code == 200
    assert "Schedule Observations" in page.text and "Download LCO Data" in page.text
    # Nav (from base.html) links to /lco on every page.
    assert ">LCO<" in client.get("/logs").text


def test_lco_config_reports_booleans_and_hides_token(monkeypatch):
    monkeypatch.setenv("LCO_API_TOKEN", "super-secret-token")
    monkeypatch.delenv("MUSCAT_LCO_DIR", raising=False)
    client = TestClient(app)
    r = client.get("/api/lco/config")
    assert r.status_code == 200
    body = r.json()
    assert body == {"ok": True, "token_configured": True,
                    "download_root_configured": False, "submit_allowed": False}
    assert "super-secret-token" not in r.text


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
        lambda filters, token=None: {"count": 1, "results": [{"filename": "f.fits.fz", "SITEID": "ogg"}]},
    )
    r = TestClient(app).get("/api/lco/archive/frames", params={"OBJECT": "WASP-12", "limit": "10"})
    assert r.status_code == 200
    assert r.json()["results"][0]["SITEID"] == "ogg"


def test_lco_archive_download_per_file_results(monkeypatch):
    monkeypatch.setattr(
        "muscat_db.lco.download_frames",
        lambda inst, frames, overwrite=False: [{"filename": "f.fits", "status": "downloaded", "bytes": 1024}],
    )
    r = TestClient(app).post("/api/lco/archive/download", json={
        "instrument": "muscat3",
        "frames": [{"filename": "f.fits", "url": "https://x/y", "DAY_OBS": "2026-01-01"}],
    })
    assert r.status_code == 200
    assert r.json()["results"][0]["status"] == "downloaded"


def test_lco_page_has_obs_column_constraints_and_plotly_figure():
    page = TestClient(app).get("/lco").text
    # Observability column + filter.
    assert "<th>Transit obs</th>" in page and "<th>Visibility</th>" in page
    assert 'id="win-filter"' in page
    # Configurable constraints.
    assert 'id="sch-obs-airmass"' in page and 'id="sch-twilight"' in page and 'id="sch-moon-sep"' in page
    # Inline astropy figure drawn with Plotly.
    assert "plotly-2.24.1" in page and 'id="vis-plot"' in page and "/api/lco/visibility" in page
    # Dynamic cross-check link to the LCO-generated visibility PNG (target/date-specific).
    assert "visibility.lco.global/visibility.png" in page
    assert 'id="lco-vis-link"' in page and 'target="_blank"' in page


def test_lco_archive_download_rejects_unknown_instrument():
    r = TestClient(app).post("/api/lco/archive/download",
                             json={"instrument": "hubble", "frames": [{"filename": "f"}]})
    assert r.status_code == 400


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
