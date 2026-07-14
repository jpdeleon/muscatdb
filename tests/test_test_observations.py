import datetime as dt

import pytest

from muscat_db import lco, test_observations as subject


def _payload(**overrides):
    payload = {
        "kind": "sinistro", "target_name": "WASP-12", "site": "coj", "filter": "rp",
        "exposure_time": 30, "exposure_budget_s": 600, "estimated_overhead_s": 5,
        "fov_candidates": [
            {"center_ra": 1.0, "center_dec": 2.0, "pa_deg": 0, "edge_margin_arcsec": 40},
            {"center_ra": 1.01, "center_dec": 2.01, "pa_deg": 15, "edge_margin_arcsec": 35},
        ],
        "capabilities": {}, "provenance": {"calculator": "unit-test"},
    }
    payload.update(overrides)
    return payload


def test_plan_is_deterministic_and_retains_both_fovs():
    first = subject.generate_plan(_payload())
    second = subject.generate_plan(_payload())
    assert first == second
    assert {item["fov_index"] for item in first["configurations"]} == {0, 1}
    assert all(item["repeats"] >= 3 for item in first["configurations"])
    assert first["estimated_exposure_s"] <= 600
    assert first["estimated_wall_clock_s"] > first["estimated_exposure_s"]


def test_focus_requires_fresh_verified_capability():
    unsupported = subject.generate_plan(_payload(defocus_mm=2))
    assert {item["defocus_mm"] for item in unsupported["configurations"]} == {2}
    fresh = dt.datetime.now(dt.timezone.utc).isoformat()
    supported = subject.generate_plan(_payload(defocus_mm=2, capabilities={
        "fetched_at": fresh, "defocus": {"field": "defocus", "writable": True, "min": 0, "max": 6, "step": 1},
    }))
    assert {item["defocus_mm"] for item in supported["configurations"]} <= {1, 2, 3}


def test_clipping_deduplicates_and_reports_removed():
    plan = subject.generate_plan(_payload(exposure_time=1, exposure_limits={"min": 1, "max": 1}))
    assert any("duplicate" in item["reason"] for item in plan["removed_combinations"])


def test_budget_too_small_for_both_nominal_fovs_fails():
    with pytest.raises(subject.TestObservationError, match="both FOVs"):
        subject.generate_plan(_payload(exposure_budget_s=100))


def test_record_round_trip(tmp_path):
    path = tmp_path / "test.sqlite"
    created = subject.create_record(subject.generate_plan(_payload()), path)
    assert created["state"] == "draft"
    updated = subject.update_record(created["id"], state="validated", payload_hash="abc", path=path)
    assert updated["payload_hash"] == "abc"


def test_ordered_configs_leave_normal_payload_unchanged():
    params = {
        "name": "normal", "proposal": "P", "target_name": "T", "ra": 1, "dec": 2,
        "site": "coj", "windows": [], "exposure_time": 30, "filter": "rp",
        "readout_mode": "central_2k_2x2", "exposure_count": 3, "type": "EXPOSE",
    }
    normal = lco.build_requestgroup("sinistro", params)
    assert normal == lco.build_requestgroup("sinistro", dict(params))
    multi = lco.build_requestgroup("sinistro", params, configurations=[
        {"exposure_time": 10}, {"exposure_time": 20},
    ])
    assert [c["instrument_configs"][0]["exposure_time"] for c in multi["requests"][0]["configurations"]] == [10, 20]
