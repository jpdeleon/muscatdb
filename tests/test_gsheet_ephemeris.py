"""Unit tests for the Google Sheet ephemeris source (muscat_db.gsheet_ephemeris).

Network is never touched: parser tests monkeypatch ``_fetch_tab_csv`` to return
CSV text directly, and the fetch/cache tests monkeypatch ``_sync_get`` with a
fake response.
"""

from __future__ import annotations

import pytest

from muscat_db import gsheet_ephemeris as gs


VALID_ID = "1AbcDEfghIJKlmnOPqrstUVwxyz0123456789ABCDEF"
SHEET_URL = f"https://docs.google.com/spreadsheets/d/{VALID_ID}/edit#gid=0"


@pytest.fixture(autouse=True)
def _clear_fetch_cache():
    # Clear the underlying TTLCache directly: parser tests monkeypatch the
    # decorated _fetch_tab_csv with a plain lambda, so its .cache_clear attr may
    # be gone by teardown.
    gs._fetch_cache.clear()
    yield
    gs._fetch_cache.clear()


# --- sheet_id_from (SSRF guard) -----------------------------------------

def test_sheet_id_from_accepts_full_url():
    assert gs.sheet_id_from(SHEET_URL) == VALID_ID


def test_sheet_id_from_accepts_bare_id():
    assert gs.sheet_id_from(VALID_ID) == VALID_ID


def test_sheet_id_from_rejects_non_google_host():
    with pytest.raises(gs.GsheetError):
        gs.sheet_id_from(f"https://evil.example.com/spreadsheets/d/{VALID_ID}/edit")


def test_sheet_id_from_rejects_empty():
    with pytest.raises(gs.GsheetError):
        gs.sheet_id_from("   ")


def test_sheet_id_from_rejects_short_bare_token():
    with pytest.raises(gs.GsheetError):
        gs.sheet_id_from("not-a-sheet")


def test_gviz_url_addresses_tab_by_name_over_google_host():
    url = gs._gviz_csv_url(VALID_ID, "My Tab")
    assert url.startswith(f"https://docs.google.com/spreadsheets/d/{VALID_ID}/gviz/tq?")
    assert "tqx=out%3Acsv" in url
    assert "sheet=My+Tab" in url


# --- ephemeris tab parsing ----------------------------------------------

EPHEM_CSV = (
    "target,planet,t0,period,duration,t0_unc,period_unc,duration_unc\n"
    "WASP-12,b,2456305.4555,1.0914203,3.0,0.0002,0.0000001,0.1\n"
    "WASP-99,b,2459000.5,2.5,2.1,,,\n"
)


def test_query_target_ephemeris_parses_and_filters(monkeypatch):
    monkeypatch.setattr(gs, "_fetch_tab_csv", lambda sid, tab: EPHEM_CSV)
    out = gs.query_target_ephemeris("WASP-12", SHEET_URL, "ephemeris")
    assert set(out) == {"b"}
    b = out["b"]
    assert b["t0"] == pytest.approx(2456305.4555)
    assert b["period"] == pytest.approx(1.0914203)
    assert b["duration"] == pytest.approx(3.0)
    assert b["t0_unc"] == pytest.approx(0.0002)
    assert b["period_unc"] == pytest.approx(0.0000001)
    assert b["duration_unc"] == pytest.approx(0.1)


def test_query_target_ephemeris_omits_absent_uncertainties(monkeypatch):
    monkeypatch.setattr(gs, "_fetch_tab_csv", lambda sid, tab: EPHEM_CSV)
    out = gs.query_target_ephemeris("WASP-99", SHEET_URL, "ephemeris")
    assert out["b"]["t0"] == pytest.approx(2459000.5)
    assert out["b"]["duration"] == pytest.approx(2.1)
    assert "t0_unc" not in out["b"]
    assert "period_unc" not in out["b"]
    assert "duration_unc" not in out["b"]


def test_query_target_ephemeris_defaults_planet_letter_b(monkeypatch):
    csv_text = "target,t0,period,duration\nWASP-12,2456305.4555,1.0914203,3.0\n"
    monkeypatch.setattr(gs, "_fetch_tab_csv", lambda sid, tab: csv_text)
    out = gs.query_target_ephemeris("WASP-12", SHEET_URL)
    assert set(out) == {"b"}


def test_query_target_ephemeris_handles_unit_annotated_headers(monkeypatch):
    csv_text = (
        "Target,Planet,T0 (BJD),Period (days),Duration (hours)\n"
        "WASP-12,b,2456305.4555,1.0914203,3.0\n"
    )
    monkeypatch.setattr(gs, "_fetch_tab_csv", lambda sid, tab: csv_text)
    out = gs.query_target_ephemeris("WASP-12", SHEET_URL)
    assert out["b"]["period"] == pytest.approx(1.0914203)
    assert out["b"]["duration"] == pytest.approx(3.0)


def test_query_target_ephemeris_skips_nonfinite_rows(monkeypatch):
    csv_text = (
        "target,planet,t0,period\n"
        "WASP-12,b,notanumber,1.0914\n"
        "WASP-12,c,2456305.4,2.5\n"
    )
    monkeypatch.setattr(gs, "_fetch_tab_csv", lambda sid, tab: csv_text)
    out = gs.query_target_ephemeris("WASP-12", SHEET_URL)
    assert set(out) == {"c"}


def test_query_target_ephemeris_empty_when_period_column_missing(monkeypatch):
    csv_text = "target,planet,t0\nWASP-12,b,2456305.4\n"
    monkeypatch.setattr(gs, "_fetch_tab_csv", lambda sid, tab: csv_text)
    assert gs.query_target_ephemeris("WASP-12", SHEET_URL) == {}


def test_query_target_ephemeris_empty_when_sheet_unreachable(monkeypatch):
    monkeypatch.setattr(gs, "_fetch_tab_csv", lambda sid, tab: "")
    assert gs.query_target_ephemeris("WASP-12", SHEET_URL) == {}


# --- transit-centers tab parsing ----------------------------------------

TC_CSV = (
    "target,planet,epoch,tc,tc_unc\n"
    "WASP-12,b,0,2456305.4555,0.001\n"
    "WASP-12,b,1,2456306.5469,0.001\n"
    "WASP-99,b,0,2459000.5,0.001\n"
)


def test_query_target_transit_centers_filters_by_target(monkeypatch):
    monkeypatch.setattr(gs, "_fetch_tab_csv", lambda sid, tab: TC_CSV)
    out = gs.query_target_transit_centers("WASP-12", SHEET_URL, "tc")
    rows = out["rows"]
    assert len(rows) == 2
    assert all(r["planet"] == "b" for r in rows)
    assert {r["source_epoch"] for r in rows} == {0, 1}


def test_query_target_transit_centers_empty_when_no_match(monkeypatch):
    monkeypatch.setattr(gs, "_fetch_tab_csv", lambda sid, tab: TC_CSV)
    out = gs.query_target_transit_centers("NONEXISTENT-TARGET", SHEET_URL, "tc")
    assert out["rows"] == []


# --- fetch + cache + error degradation ----------------------------------

class _FakeResp:
    def __init__(self, text: str):
        self.text = text


def test_fetch_tab_csv_caches_across_calls(monkeypatch):
    calls = {"n": 0}

    def fake_get(url, headers=None, timeout=None):
        calls["n"] += 1
        return _FakeResp("target,t0,period\nWASP-12,1.0,2.0\n")

    monkeypatch.setattr(gs, "_sync_get", fake_get)
    first = gs._fetch_tab_csv(VALID_ID, "ephemeris")
    second = gs._fetch_tab_csv(VALID_ID, "ephemeris")
    assert first == second
    assert calls["n"] == 1


def test_fetch_tab_csv_degrades_to_empty_on_error(monkeypatch):
    def boom(url, headers=None, timeout=None):
        raise RuntimeError("network down")

    monkeypatch.setattr(gs, "_sync_get", boom)
    assert gs._fetch_tab_csv(VALID_ID, "ephemeris") == ""


# --- explicit column overrides ------------------------------------------

CUSTOM_EPHEM_CSV = (
    "Star,Mid,Porb,Width,MidErr\n"
    "WASP-12,2456305.4555,1.0914203,3.0,0.0002\n"
)


def test_query_target_ephemeris_uses_column_overrides(monkeypatch):
    monkeypatch.setattr(gs, "_fetch_tab_csv", lambda sid, tab: CUSTOM_EPHEM_CSV)
    col_map = {
        "target": "Star", "t0": "Mid", "period": "Porb",
        "duration": "Width", "t0_unc": "MidErr",
    }
    out = gs.query_target_ephemeris("WASP-12", SHEET_URL, "ephemeris", col_map)
    assert out["b"]["t0"] == pytest.approx(2456305.4555)
    assert out["b"]["period"] == pytest.approx(1.0914203)
    assert out["b"]["duration"] == pytest.approx(3.0)
    assert out["b"]["t0_unc"] == pytest.approx(0.0002)


def test_query_target_ephemeris_override_falls_back_to_alias(monkeypatch):
    # Override only t0/period; duration is auto-detected from a standard header.
    csv_text = "Star,Mid,Porb,duration\nWASP-12,2456305.4555,1.0914203,3.0\n"
    monkeypatch.setattr(gs, "_fetch_tab_csv", lambda sid, tab: csv_text)
    out = gs.query_target_ephemeris(
        "WASP-12", SHEET_URL, "ephemeris", {"target": "Star", "t0": "Mid", "period": "Porb"}
    )
    assert out["b"]["duration"] == pytest.approx(3.0)


def test_planet_from_name_extracts_marker():
    assert gs._planet_from_name("HIP 67522 c") == "c"
    assert gs._planet_from_name("HIP 67522 b") == "b"
    assert gs._planet_from_name("WASP-12") == ""       # bare host -> caller defaults to b
    assert gs._planet_from_name("TOI-1234.02") == "c"  # candidate suffix .02 -> c


def test_planet_label_parses_candidate_numbers():
    # TOI/TFOP candidate numbering (1-based) via leading dot or zero padding.
    assert gs._planet_label(".01") == "b"
    assert gs._planet_label("01") == "b"
    assert gs._planet_label(".02") == "c"
    assert gs._planet_label("02") == "c"
    # Plain letters and bare zero-based indices are preserved.
    assert gs._planet_label("b") == "b"
    assert gs._planet_label("1") == "c"   # bare int stays zero-based (0=b, 1=c)
    assert gs._planet_label("0") == "b"
    assert gs._planet_label("") == ""
    assert gs._planet_label(".99") == ""  # out of range


def test_query_target_ephemeris_parses_candidate_planet_column(monkeypatch):
    # TIC target with TOI-style candidate numbers in the planet column.
    csv_text = (
        "name,planet,t0,period,duration\n"
        "TIC 88297141,.01,2459001.1,3.5,2.0\n"
        "TIC 88297141,.02,2459002.2,9.9,2.5\n"
    )
    monkeypatch.setattr(gs, "_fetch_tab_csv", lambda sid, tab: csv_text)
    out = gs.query_target_ephemeris(
        "TIC88297141", SHEET_URL, "ephemeris",
        {"target": "name", "planet": "planet", "t0": "t0", "period": "period", "duration": "duration"},
    )
    assert set(out) == {"b", "c"}
    assert out["b"]["period"] == pytest.approx(3.5)
    assert out["c"]["period"] == pytest.approx(9.9)


def test_query_target_transit_centers_parses_candidate_planet_column(monkeypatch):
    csv_text = (
        "name,planet,tc,tc_err\n"
        "TIC 88297141,01,2459001.1,0.001\n"
        "TIC 88297141,02,2459002.2,0.001\n"
    )
    monkeypatch.setattr(gs, "_fetch_tab_csv", lambda sid, tab: csv_text)
    out = gs.query_target_transit_centers(
        "TIC88297141", SHEET_URL, "tc",
        {"target": "name", "planet": "planet", "tc": "tc", "tc_unc": "tc_err"},
    )
    assert sorted(r["planet"] for r in out["rows"]) == ["b", "c"]


def test_query_target_ephemeris_derives_planet_when_no_planet_column(monkeypatch):
    # The planet is encoded in the name column and there is NO planet column;
    # both rows must survive as distinct planets (regression for b/c collapse).
    csv_text = (
        "name,t0,period,duration\n"
        "HIP 67522 b,2458604.02,6.9595,4.85\n"
        "HIP 67522 c,2458602.50,14.3349,5.66\n"
    )
    monkeypatch.setattr(gs, "_fetch_tab_csv", lambda sid, tab: csv_text)
    out = gs.query_target_ephemeris("HIP67522", SHEET_URL, "ephemeris", {"target": "name"})
    assert set(out) == {"b", "c"}
    assert out["b"]["period"] == pytest.approx(6.9595)
    assert out["c"]["period"] == pytest.approx(14.3349)


def test_query_target_transit_centers_derives_planet_when_no_planet_column(monkeypatch):
    csv_text = (
        "name,tc,tc_err\n"
        "HIP 67522 b,2459000.5,0.001\n"
        "HIP 67522 c,2459010.3,0.001\n"
    )
    monkeypatch.setattr(gs, "_fetch_tab_csv", lambda sid, tab: csv_text)
    out = gs.query_target_transit_centers(
        "HIP67522", SHEET_URL, "tc", {"target": "name", "tc": "tc", "tc_unc": "tc_err"}
    )
    planets = sorted(r["planet"] for r in out["rows"])
    assert planets == ["b", "c"]


CUSTOM_TC_CSV = (
    "Star,pl,E,BJD_mid,BJD_err\n"
    "WASP-12,b,0,2456305.4555,0.001\n"
    "WASP-12,b,1,2456306.5469,0.001\n"
    "WASP-99,b,0,2459000.5,0.001\n"
)


def test_query_target_transit_centers_uses_column_overrides(monkeypatch):
    monkeypatch.setattr(gs, "_fetch_tab_csv", lambda sid, tab: CUSTOM_TC_CSV)
    col_map = {
        "target": "Star", "planet": "pl", "epoch": "E",
        "tc": "BJD_mid", "tc_unc": "BJD_err",
    }
    out = gs.query_target_transit_centers("WASP-12", SHEET_URL, "tc", col_map)
    rows = out["rows"]
    assert len(rows) == 2
    assert {r["source_epoch"] for r in rows} == {0, 1}
    assert all(r["tc_unc"] == pytest.approx(0.001) for r in rows)


# --- column listing + suggestions ---------------------------------------

def test_tab_columns_returns_header_row(monkeypatch):
    monkeypatch.setattr(gs, "_fetch_tab_csv", lambda sid, tab: EPHEM_CSV)
    cols = gs.tab_columns(SHEET_URL, "ephemeris")
    assert cols[:4] == ["target", "planet", "t0", "period"]


def test_tab_columns_empty_when_unreachable(monkeypatch):
    monkeypatch.setattr(gs, "_fetch_tab_csv", lambda sid, tab: "")
    assert gs.tab_columns(SHEET_URL, "ephemeris") == []


def test_suggest_ephem_columns_maps_standard_headers():
    cols = ["target", "planet", "t0", "period", "duration"]
    suggested = gs.suggest_ephem_columns(cols)
    assert suggested["t0"] == "t0"
    assert suggested["period"] == "period"
    assert suggested["duration"] == "duration"


def test_suggest_tc_columns_maps_standard_headers():
    cols = ["target", "planet", "epoch", "tc", "tc_unc"]
    suggested = gs.suggest_tc_columns(cols)
    assert suggested["tc"] == "tc"
    assert suggested["tc_unc"] == "tc_unc"
    assert suggested["planet"] == "planet"
