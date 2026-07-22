"""Per-user ephemeris Google Sheet settings: DB accessors + HTTP routes."""

from __future__ import annotations

from fastapi.testclient import TestClient

from muscat_db import web
from muscat_db.database import (
    get_user_ephem_sheet,
    get_user_settings,
    set_user_ephem_sheet,
    user_ephem_sheet_configured,
)
from muscat_db.web import app


VALID_ID = "ABCDEFGHIJKLMNOPQRSTUVWX"
SHEET_URL = f"https://docs.google.com/spreadsheets/d/{VALID_ID}/edit"
PROXY_SECRET = "proxy-secret"
AUTH_HEADERS = {"X-Forwarded-User": "alice", "X-MuSCAT-Proxy-Secret": PROXY_SECRET}


def _db_env(monkeypatch, tmp_path):
    monkeypatch.setenv("MUSCAT_DB_PATH", str(tmp_path / "users.db"))
    monkeypatch.setenv("MUSCAT_DB_SECRET", "test-secret")


def _authed_client(monkeypatch, tmp_path):
    _db_env(monkeypatch, tmp_path)
    monkeypatch.setenv("MUSCAT_REQUIRE_AUTH", "1")
    monkeypatch.setenv("MUSCAT_PROXY_SECRET", PROXY_SECRET)
    return TestClient(app, client=("127.0.0.1", 12345))


# --- database accessors --------------------------------------------------

def test_set_get_roundtrip(monkeypatch, tmp_path):
    _db_env(monkeypatch, tmp_path)
    set_user_ephem_sheet("alice", SHEET_URL, "eph", "centers")
    cfg = get_user_ephem_sheet("alice")
    assert cfg == {
        "url": SHEET_URL,
        "ephem_tab": "eph",
        "tc_tab": "centers",
        "ephem_cols": {},
        "tc_cols": {},
    }
    assert user_ephem_sheet_configured("alice") is True


def test_blank_url_clears_configuration(monkeypatch, tmp_path):
    _db_env(monkeypatch, tmp_path)
    set_user_ephem_sheet("alice", SHEET_URL, "eph", "centers")
    set_user_ephem_sheet("alice", "")
    assert get_user_ephem_sheet("alice") is None
    assert user_ephem_sheet_configured("alice") is False


def test_absent_tab_names_stored_empty(monkeypatch, tmp_path):
    _db_env(monkeypatch, tmp_path)
    set_user_ephem_sheet("alice", SHEET_URL)
    cfg = get_user_ephem_sheet("alice")
    assert cfg["ephem_tab"] == ""
    assert cfg["tc_tab"] == ""


def test_url_is_encrypted_at_rest(monkeypatch, tmp_path):
    _db_env(monkeypatch, tmp_path)
    set_user_ephem_sheet("alice", f"https://docs.google.com/spreadsheets/d/{VALID_ID}/edit")
    raw = get_user_settings("alice")
    assert "ephem_sheet_url_enc" in raw
    assert VALID_ID not in raw["ephem_sheet_url_enc"]


def test_configured_returns_none_user_false(monkeypatch, tmp_path):
    _db_env(monkeypatch, tmp_path)
    assert user_ephem_sheet_configured(None) is False
    assert get_user_ephem_sheet("") is None


# --- HTTP routes ---------------------------------------------------------

def test_status_requires_authentication():
    resp = TestClient(app).get("/api/settings/ephem-sheet-status")
    assert resp.status_code == 401


def test_settings_save_and_status_roundtrip(monkeypatch, tmp_path):
    client = _authed_client(monkeypatch, tmp_path)

    before = client.get("/api/settings/ephem-sheet-status", headers=AUTH_HEADERS).json()
    assert before["configured"] is False
    # Unset tabs report the resolver defaults.
    assert before["ephem_tab"] == "ephemeris"
    assert before["tc_tab"] == "tc"

    saved = client.post(
        "/api/settings/ephem-sheet",
        headers=AUTH_HEADERS,
        json={"url": SHEET_URL, "ephem_tab": "eph", "tc_tab": "centers"},
    )
    assert saved.status_code == 200, saved.text
    assert saved.json()["configured"] is True

    after = client.get("/api/settings/ephem-sheet-status", headers=AUTH_HEADERS).json()
    assert after["configured"] is True
    assert after["ephem_tab"] == "eph"
    assert after["tc_tab"] == "centers"


def test_settings_save_rejects_non_google_url(monkeypatch, tmp_path):
    client = _authed_client(monkeypatch, tmp_path)
    resp = client.post(
        "/api/settings/ephem-sheet",
        headers=AUTH_HEADERS,
        json={"url": f"https://evil.example.com/spreadsheets/d/{VALID_ID}/edit"},
    )
    assert resp.status_code == 400
    assert "docs.google.com" in resp.json()["error"]


def _fake_tab_columns(url, tab):
    if "tc" in tab or tab == "centers":
        return ["target", "planet", "epoch", "tc", "tc_unc"]
    return ["target", "planet", "t0", "period", "duration"]


def test_columns_route_lists_and_suggests(monkeypatch, tmp_path):
    client = _authed_client(monkeypatch, tmp_path)
    monkeypatch.setattr(web.gsheet_ephemeris, "tab_columns", _fake_tab_columns)
    resp = client.post(
        "/api/settings/ephem-sheet-columns",
        headers=AUTH_HEADERS,
        json={"url": SHEET_URL},
    )
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["ephem_columns"] == ["target", "planet", "t0", "period", "duration"]
    assert data["tc_columns"] == ["target", "planet", "epoch", "tc", "tc_unc"]
    assert data["ephem_suggested"]["t0"] == "t0"
    assert data["tc_suggested"]["tc"] == "tc"


def test_columns_route_uses_saved_url_when_blank(monkeypatch, tmp_path):
    client = _authed_client(monkeypatch, tmp_path)
    set_user_ephem_sheet("alice", SHEET_URL)
    monkeypatch.setattr(web.gsheet_ephemeris, "tab_columns", _fake_tab_columns)
    # Blank URL -> server falls back to the saved sheet.
    resp = client.post(
        "/api/settings/ephem-sheet-columns", headers=AUTH_HEADERS, json={"url": ""}
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["ephem_columns"]


def test_columns_route_requires_url_or_saved_sheet(monkeypatch, tmp_path):
    client = _authed_client(monkeypatch, tmp_path)
    resp = client.post(
        "/api/settings/ephem-sheet-columns", headers=AUTH_HEADERS, json={"url": ""}
    )
    assert resp.status_code == 400


def test_save_persists_column_maps(monkeypatch, tmp_path):
    client = _authed_client(monkeypatch, tmp_path)
    resp = client.post(
        "/api/settings/ephem-sheet",
        headers=AUTH_HEADERS,
        json={
            "url": SHEET_URL,
            "ephem_cols": {"t0": "Mid", "period": "Porb"},
            "tc_cols": {"tc": "BJD_mid"},
        },
    )
    assert resp.status_code == 200, resp.text
    status = client.get("/api/settings/ephem-sheet-status", headers=AUTH_HEADERS).json()
    assert status["ephem_cols"] == {"t0": "Mid", "period": "Porb"}
    assert status["tc_cols"] == {"tc": "BJD_mid"}


def test_keep_url_updates_columns_in_place(monkeypatch, tmp_path):
    client = _authed_client(monkeypatch, tmp_path)
    client.post(
        "/api/settings/ephem-sheet",
        headers=AUTH_HEADERS,
        json={"url": SHEET_URL, "ephem_cols": {"t0": "Mid"}},
    )
    # No URL, keep_url=True: update column maps on the saved sheet.
    resp = client.post(
        "/api/settings/ephem-sheet",
        headers=AUTH_HEADERS,
        json={"url": "", "keep_url": True, "ephem_cols": {"t0": "Tzero", "period": "P"}},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["configured"] is True
    status = client.get("/api/settings/ephem-sheet-status", headers=AUTH_HEADERS).json()
    assert status["configured"] is True
    assert status["ephem_cols"] == {"t0": "Tzero", "period": "P"}
