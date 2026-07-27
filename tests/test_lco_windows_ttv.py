"""Scheduling windows built from a harmonic TTV model.

The rest of the page schedules on a linear ephemeris (``t0 + epoch * period``),
which is the assumption a TTV fit exists to reject. This source uses the model's
predicted transit centres directly so the deviations survive into the submitted
window.

Two things are easy to get wrong and are pinned here: the page always posts a
t0/period pair, so the ttv branch must win over the "explicit t0/period" shortcut;
and the model's times are in the fit's own system, so ``t_offset`` must be applied
or a BKJD fit schedules 2454833 days off.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from muscat_db.web import app

_MODEL = {
    "ok": True,
    "run_name": "default",
    "t_offset": 0.0,
    "points": {
        # deliberately uneven spacing: a linear ephemeris cannot reproduce these
        "b": [
            {"epoch": 100, "tc": 2461000.5},
            {"epoch": 101, "tc": 2461002.55},   # 0.05 d late
            {"epoch": 102, "tc": 2461004.45},   # 0.05 d early
        ],
    },
}

_BASE = {
    "target": "HIP 67522",
    "planet": "b",
    "source": "ttv",
    "duration": 2.0,
    "range_start": "2025-11-01",
    "range_end": "2025-12-31",
    "pad_before_min": 30,
    "pad_after_min": 30,
}


def _post(payload):
    return TestClient(app).post("/api/lco/windows", json=payload)


def test_windows_follow_the_model_not_a_straight_line(monkeypatch):
    monkeypatch.setattr("muscat_db.web.ttv.get_ttv_model", lambda *a, **k: _MODEL)
    body = _post(_BASE).json()

    assert body["ok"] is True
    mids = [w["mid_bjd"] for w in body["windows"]]
    assert mids == [2461000.5, 2461002.55, 2461004.45]
    # The uneven spacing is the point: a linear fit through these would not
    # reproduce them.
    gaps = [round(b - a, 4) for a, b in zip(mids, mids[1:])]
    assert gaps == [2.05, 1.9]


def test_ttv_source_wins_over_posted_t0_and_period(monkeypatch):
    """The page always sends t0/period; the ttv branch must not defer to them."""
    monkeypatch.setattr("muscat_db.web.ttv.get_ttv_model", lambda *a, **k: _MODEL)
    monkeypatch.setattr(
        "muscat_db.web._query_target_planets_catalog",
        lambda t: pytest.fail("catalog must not be consulted for source=ttv"),
    )
    body = _post({**_BASE, "t0": 2461000.5, "period": 2.0}).json()

    assert body["ok"] is True
    assert [w["mid_bjd"] for w in body["windows"]] == [2461000.5, 2461002.55, 2461004.45]


def test_t_offset_is_applied(monkeypatch):
    """A BKJD fit stores times 2454833 d below BJD; scheduling needs true BJD."""
    shifted = {
        **_MODEL,
        "t_offset": 2454833.0,
        "points": {"b": [{"epoch": 100, "tc": 6167.5}]},
    }
    monkeypatch.setattr("muscat_db.web.ttv.get_ttv_model", lambda *a, **k: shifted)
    body = _post(_BASE).json()

    assert body["windows"][0]["mid_bjd"] == pytest.approx(2461000.5)


def test_windows_carry_the_keys_the_pipeline_consumes(monkeypatch):
    """classify_transits, _clip_windows_to_observability and _repeat_duration all
    read these keys, so a divergent shape breaks them silently."""
    monkeypatch.setattr("muscat_db.web.ttv.get_ttv_model", lambda *a, **k: _MODEL)
    w = _post(_BASE).json()["windows"][0]

    assert set(w) >= {"epoch", "epoch_abs", "mid_bjd", "mid", "start", "end"}
    assert w["mid"].endswith("Z") and w["start"].endswith("Z")
    assert w["epoch"] == 0 and w["epoch_abs"] == 100     # displayed epoch is relative


def test_padding_and_duration_frame_the_window(monkeypatch):
    monkeypatch.setattr("muscat_db.web.ttv.get_ttv_model", lambda *a, **k: _MODEL)
    w = _post(_BASE).json()["windows"][0]

    # 2 h duration -> 1 h each side, plus 30 min padding each side.
    from muscat_db import transit_obs
    assert w["start"] == transit_obs._jd_to_iso_z(2461000.5 - 1.5 / 24.0)
    assert w["end"] == transit_obs._jd_to_iso_z(2461000.5 + 1.5 / 24.0)


def test_transits_outside_the_range_are_dropped(monkeypatch):
    monkeypatch.setattr("muscat_db.web.ttv.get_ttv_model", lambda *a, **k: _MODEL)
    body = _post({**_BASE, "range_start": "2026-01-01", "range_end": "2026-01-31"}).json()
    assert body["windows"] == []


def test_missing_duration_is_rejected(monkeypatch):
    monkeypatch.setattr(
        "muscat_db.web.ttv.get_ttv_model",
        lambda *a, **k: pytest.fail("must not run the model without a duration"),
    )
    payload = {k: v for k, v in _BASE.items() if k != "duration"}
    assert _post(payload).status_code == 400


def test_model_failure_is_surfaced(monkeypatch):
    monkeypatch.setattr(
        "muscat_db.web.ttv.get_ttv_model",
        lambda *a, **k: {"ok": False, "error": "harmonic conda environment is unavailable"},
    )
    r = _post(_BASE)
    assert r.status_code == 400
    assert "harmonic" in r.json()["error"]


def test_planet_without_predictions_is_reported(monkeypatch):
    monkeypatch.setattr("muscat_db.web.ttv.get_ttv_model", lambda *a, **k: _MODEL)
    r = _post({**_BASE, "planet": "c"})
    assert r.status_code == 400
    assert "planet c" in r.json()["error"]


def test_observability_is_attached_like_the_linear_path(monkeypatch):
    monkeypatch.setattr("muscat_db.web.ttv.get_ttv_model", lambda *a, **k: _MODEL)
    monkeypatch.setattr(
        "muscat_db.transit_obs.classify_transits",
        lambda *a, **k: [{"rating": "full", "sites": ["ogg"]}] * 3,
    )
    body = _post({**_BASE, "ra": 206.9, "dec": -68.0}).json()
    assert body["windows"][0]["observability"]["rating"] == "full"


def test_observability_failure_does_not_lose_the_windows(monkeypatch):
    """Same contract as the linear path: ratings are best-effort."""
    monkeypatch.setattr("muscat_db.web.ttv.get_ttv_model", lambda *a, **k: _MODEL)

    def _boom(*a, **k):
        raise RuntimeError("astropy exploded")

    monkeypatch.setattr("muscat_db.transit_obs.classify_transits", _boom)
    body = _post({**_BASE, "ra": 206.9, "dec": -68.0}).json()
    assert len(body["windows"]) == 3
    assert "obs_error" in body


# ---------------------------------------------------------------------------
# Information-gain ranking
#
# Which transits are worth observing, not merely which are observable. The rank
# is advisory and merged onto the windows by epoch, so a ranking failure must
# leave the windows themselves untouched.
# ---------------------------------------------------------------------------
_RANKING = {
    "ok": True,
    "rank_by": "ttv",
    "sigmas": {"b": 0.00112},
    "rows": [
        {"planet": "b", "epoch": 100, "greedy_rank": 2, "gain_ttv": 0.80, "gain_total": 1.10},
        {"planet": "b", "epoch": 101, "greedy_rank": 1, "gain_ttv": 1.40, "gain_total": 1.90},
        {"planet": "b", "epoch": 102, "greedy_rank": 3, "gain_ttv": 0.20, "gain_total": 0.55},
    ],
}


def test_ranking_is_merged_onto_the_matching_epochs(monkeypatch):
    monkeypatch.setattr("muscat_db.web.ttv.get_ttv_model", lambda *a, **k: _MODEL)
    monkeypatch.setattr("muscat_db.web.ttv.get_ttv_ranking", lambda *a, **k: _RANKING)
    body = _post({**_BASE, "rank": True}).json()

    ranks = {w["epoch_abs"]: w["rank"] for w in body["windows"]}
    assert ranks == {100: 2, 101: 1, 102: 3}
    assert body["windows"][1]["gain_ttv"] == 1.40
    assert body["rank_by"] == "ttv"


def test_ranking_is_opt_in(monkeypatch):
    """It costs a pseudo-inverse per candidate, so it must not run unasked."""
    monkeypatch.setattr("muscat_db.web.ttv.get_ttv_model", lambda *a, **k: _MODEL)
    monkeypatch.setattr(
        "muscat_db.web.ttv.get_ttv_ranking",
        lambda *a, **k: pytest.fail("ranking must not run unless requested"),
    )
    body = _post(_BASE).json()
    assert "rank" not in body["windows"][0]


def test_ranking_failure_keeps_the_windows(monkeypatch):
    monkeypatch.setattr("muscat_db.web.ttv.get_ttv_model", lambda *a, **k: _MODEL)
    monkeypatch.setattr(
        "muscat_db.web.ttv.get_ttv_ranking",
        lambda *a, **k: {"ok": False, "error": "harmonic conda environment is unavailable"},
    )
    body = _post({**_BASE, "rank": True}).json()

    assert len(body["windows"]) == 3
    assert "harmonic" in body["rank_error"]
    assert "rank" not in body["windows"][0]


def test_ranking_rows_for_other_planets_are_ignored(monkeypatch):
    """Rows are keyed by epoch, which repeats across planets."""
    mixed = {**_RANKING, "rows": [
        {"planet": "c", "epoch": 100, "greedy_rank": 1, "gain_ttv": 9.9, "gain_total": 9.9},
        {"planet": "b", "epoch": 100, "greedy_rank": 7, "gain_ttv": 0.1, "gain_total": 0.2},
    ]}
    monkeypatch.setattr("muscat_db.web.ttv.get_ttv_model", lambda *a, **k: _MODEL)
    monkeypatch.setattr("muscat_db.web.ttv.get_ttv_ranking", lambda *a, **k: mixed)
    body = _post({**_BASE, "rank": True}).json()

    assert body["windows"][0]["rank"] == 7      # planet b's row, not planet c's


def test_invalid_rank_by_is_rejected(tmp_path, monkeypatch):
    from muscat_db import ttv_fit
    monkeypatch.setenv("MUSCAT_TTV_DIR", str(tmp_path))
    assert ttv_fit.get_ttv_ranking("HIP 67522", "default", "2025-11-01", "2025-12-31",
                                   rank_by="bogus")["ok"] is False
