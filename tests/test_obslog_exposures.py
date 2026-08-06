"""Obs-log exposure lookup for the LCO schedule "Show ObsLog" button."""

from __future__ import annotations

import sqlite3

from fastapi.testclient import TestClient

from muscat_db.database import SCHEMA, get_exposure_log_for_objects, get_frame_objects
from muscat_db.web import app


def _seed(path):
    con = sqlite3.connect(path)
    con.executescript(SCHEMA)
    rows = [
        # (instrument, obsdate, ccd, filename, object, exptime, read_mode, filter, focus)
        ("sinistro", "260701", 0, "f1.fits", "HIP 67522", 30.0, "central_2k_2x2", "rp", 0.0),
        ("sinistro", "260701", 0, "f2.fits", "HIP 67522", 30.0, "central_2k_2x2", "rp", 0.0),
        ("sinistro", "260810", 0, "f3.fits", "HIP67522", 45.0, "full_frame", "ip", -2.0),
        ("muscat3", "260101", 0, "f4.fits", "hip 67522", 20.0, "MUSCAT_FAST", "g", 0.0),
        ("sinistro", "260701", 0, "f5.fits", "WASP-12", 60.0, "central_2k_2x2", "rp", 0.0),
    ]
    con.executemany(
        "INSERT INTO frames (instrument, obsdate, ccd, filename, object, exptime, read_mode, filter, focus)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        rows,
    )
    con.commit()
    con.close()


def test_get_frame_objects_and_grouping(tmp_path):
    path = str(tmp_path / "obslog.db")
    _seed(path)
    objs = get_frame_objects(path)
    assert set(objs) == {"HIP 67522", "HIP67522", "hip 67522", "WASP-12"}

    # All three HIP 67522 spellings normalize together -> three distinct configs.
    hip_objs = ["HIP 67522", "HIP67522", "hip 67522"]
    groups = get_exposure_log_for_objects(path, hip_objs)
    assert len(groups) == 3
    # The two rp/30s frames collapse into one group with nframes=2.
    rp30 = next(g for g in groups if g["exptime"] == 30.0)
    assert rp30["nframes"] == 2
    assert rp30["read_mode"] == "central_2k_2x2"
    assert rp30["filter"] == "rp"
    # Newest first (260810 > 260701 > 260101).
    assert groups[0]["last_date"] == "260810"


def test_get_exposure_log_empty_objects(tmp_path):
    path = str(tmp_path / "obslog.db")
    _seed(path)
    assert get_exposure_log_for_objects(path, []) == []


def test_obslog_exposures_route(monkeypatch, tmp_path):
    path = str(tmp_path / "obslog.db")
    _seed(path)
    monkeypatch.setenv("MUSCAT_DB_PATH", path)
    client = TestClient(app)

    data = client.get("/api/lco/obslog-exposures", params={"target": "HIP67522"}).json()
    assert data["ok"] is True
    assert len(data["exposures"]) == 3          # WASP-12 excluded
    exptimes = sorted(e["exptime"] for e in data["exposures"])
    assert exptimes == [20.0, 30.0, 45.0]

    empty = client.get("/api/lco/obslog-exposures", params={"target": "NONEXISTENT"}).json()
    assert empty["ok"] is True
    assert empty["exposures"] == []

    bad = client.get("/api/lco/obslog-exposures", params={"target": "   "})
    assert bad.status_code == 400
