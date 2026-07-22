"""Integration tests for the Google Sheet sources on /api/lco/windows.

The sheet fetch itself is stubbed (``gsheet_ephemeris.query_*`` monkeypatched)
so no network is touched; the user's sheet *configuration* is real (written to a
temp DB via ``set_user_ephem_sheet``).
"""

from __future__ import annotations

from fastapi.testclient import TestClient

from muscat_db import web
from muscat_db.database import set_user_ephem_sheet
from muscat_db.web import app


VALID_ID = "ABCDEFGHIJKLMNOPQRSTUVWX"
SHEET_URL = f"https://docs.google.com/spreadsheets/d/{VALID_ID}/edit"
PROXY_SECRET = "proxy-secret"
AUTH_HEADERS = {"X-Forwarded-User": "alice", "X-MuSCAT-Proxy-Secret": PROXY_SECRET}


def _authed_client(monkeypatch, tmp_path):
    monkeypatch.setenv("MUSCAT_DB_PATH", str(tmp_path / "windows.db"))
    monkeypatch.setenv("MUSCAT_DB_SECRET", "test-secret")
    monkeypatch.setenv("MUSCAT_REQUIRE_AUTH", "1")
    monkeypatch.setenv("MUSCAT_PROXY_SECRET", PROXY_SECRET)
    return TestClient(app, client=("127.0.0.1", 12345))


_WINDOW_BODY = {
    "target": "WASP-12",
    "planet": "b",
    "range_start": "2026-07-01",
    "range_end": "2026-07-12",
    "pad_before_min": 0,
    "pad_after_min": 0,
}


def test_windows_gsheet_source_resolves_ephemeris(monkeypatch, tmp_path):
    client = _authed_client(monkeypatch, tmp_path)
    set_user_ephem_sheet("alice", SHEET_URL)
    monkeypatch.setattr(
        web.gsheet_ephemeris,
        "query_target_ephemeris",
        lambda target, url, tab, col_map=None: {"b": {"t0": 2459000.5, "period": 3.0, "duration": 2.0}},
    )

    resp = client.post(
        "/api/lco/windows", headers=AUTH_HEADERS, json={**_WINDOW_BODY, "source": "gsheet"}
    )
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["ok"] is True
    assert data["planet"] == "b"
    assert data["period"] == 3.0
    assert data["duration"] == 2.0
    assert len(data["windows"]) >= 1


def test_windows_gsheet_source_accepts_candidate_planet(monkeypatch, tmp_path):
    # The sheet keys the planet under "b" (from a ".01" name); a request that
    # asks for planet ".01" must resolve to that "b" entry.
    client = _authed_client(monkeypatch, tmp_path)
    set_user_ephem_sheet("alice", SHEET_URL)
    monkeypatch.setattr(
        web.gsheet_ephemeris,
        "query_target_ephemeris",
        lambda target, url, tab, col_map=None: {"b": {"t0": 2459000.5, "period": 3.0, "duration": 2.0}},
    )
    resp = client.post(
        "/api/lco/windows",
        headers=AUTH_HEADERS,
        json={**_WINDOW_BODY, "planet": ".01", "source": "gsheet"},
    )
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["ok"] is True
    assert data["planet"] == "b"
    assert data["period"] == 3.0


def test_windows_gsheet_tc_source_runs_linear_fit(monkeypatch, tmp_path):
    client = _authed_client(monkeypatch, tmp_path)
    set_user_ephem_sheet("alice", SHEET_URL)
    # Two transit centers 10 epochs apart -> period ~= 3.0 d, plus a catalog
    # duration seed the fit itself does not produce.
    monkeypatch.setattr(
        web.gsheet_ephemeris,
        "query_target_transit_centers",
        lambda target, url, tab, col_map=None: {
            "rows": [
                {"planet": "b", "source_epoch": 0, "tc": 2459000.5, "tc_unc": 0.001},
                {"planet": "b", "source_epoch": 10, "tc": 2459030.5, "tc_unc": 0.001},
            ],
            "time_system": None,
        },
    )
    monkeypatch.setattr(
        web.gsheet_ephemeris, "query_target_ephemeris", lambda target, url, tab, col_map=None: {}
    )
    monkeypatch.setattr(
        web, "_query_target_planets_catalog",
        lambda target: {"b": {"t0": 2459000.5, "period": 3.0, "duration": 2.4}},
    )

    resp = client.post(
        "/api/lco/windows", headers=AUTH_HEADERS, json={**_WINDOW_BODY, "source": "gsheet_tc"}
    )
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["ok"] is True
    assert data["period"] == 3.0
    # Duration was backfilled from the catalog seed.
    assert data["duration"] == 2.4
    assert len(data["windows"]) >= 1


def test_windows_gsheet_requires_configured_sheet(monkeypatch, tmp_path):
    client = _authed_client(monkeypatch, tmp_path)
    # No set_user_ephem_sheet call: alice has no sheet.
    resp = client.post(
        "/api/lco/windows", headers=AUTH_HEADERS, json={**_WINDOW_BODY, "source": "gsheet"}
    )
    assert resp.status_code == 400
    assert "Google Sheet" in resp.json()["error"]
