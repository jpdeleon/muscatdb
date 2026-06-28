"""Unit tests for the isolated LCO helper module (muscat_db/lco.py).

Pure logic only: window generation, payload construction, validation, path
resolution, and the no-overwrite download guard. No live LCO calls.
"""

from __future__ import annotations

import pathlib

import pytest

from muscat_db import lco


# --------------------------------------------------------------------------- #
# generate_windows
# --------------------------------------------------------------------------- #


def test_generate_windows_counts_cycles_in_range():
    # t0 at 2459000.5, period 2 d, over a 10-day range -> ~5 mid-transits.
    start = lco._jd_to_dt(2459000.5).date().isoformat()
    end = lco._jd_to_dt(2459010.5).date().isoformat()
    windows = lco.generate_windows(2459000.5, 2.0, 2.0, start, end, 0, 0)
    assert len(windows) == 6  # epochs 0..5 inclusive
    assert all(w["start"] < w["mid"] < w["end"] for w in windows)


def test_generate_windows_applies_duration_and_padding():
    # duration 2h + 30 min padding each side -> window spans 2h + 1h = 3h.
    # Float endpoints are interpreted as JD (strings are ISO).
    w = lco.generate_windows(2459000.5, 5.0, 2.0, 2459000.0, 2459001.0, 30, 30)[0]
    from datetime import datetime

    start = datetime.fromisoformat(w["start"])
    end = datetime.fromisoformat(w["end"])
    span_hours = (end - start).total_seconds() / 3600.0
    assert span_hours == pytest.approx(3.0, abs=1 / 60)


def test_generate_windows_rejects_inverted_range():
    with pytest.raises(lco.LcoError):
        lco.generate_windows(2459000.5, 2.0, 2.0, "2026-01-10", "2026-01-01")


def test_generate_windows_rejects_nonpositive_period_and_duration():
    with pytest.raises(lco.LcoError):
        lco.generate_windows(2459000.5, 0.0, 2.0, "2026-01-01", "2026-01-10")
    with pytest.raises(lco.LcoError):
        lco.generate_windows(2459000.5, 2.0, 0.0, "2026-01-01", "2026-01-10")


def test_generate_windows_caps_absurd_ranges():
    with pytest.raises(lco.LcoError):
        # period 0.01 d over 1000 d -> 100k windows -> rejected
        lco.generate_windows(2459000.5, 0.01, 1.0, "2026-01-01", "2028-09-27")


def test_generate_windows_empty_when_no_transit_in_range():
    # A short range (JD floats) that falls entirely between two transits.
    out = lco.generate_windows(2459000.5, 100.0, 2.0, 2459001.0, 2459002.0)
    assert out == []


# --------------------------------------------------------------------------- #
# build_requestgroup
# --------------------------------------------------------------------------- #


def _windows():
    return [{"start": "2026-01-01T00:00:00", "end": "2026-01-01T06:00:00"}]


def test_build_requestgroup_muscat_shape():
    rg = lco.build_requestgroup(
        "muscat",
        {
            "name": "wasp12",
            "proposal": "TEST2026A",
            "target_name": "WASP-12 b",
            "ra": 97.64,
            "dec": 29.67,
            "exposure_times": {"g": 30, "r": 30, "i": 30, "z": 60},
            "exposure_mode": "SYNCHRONOUS",
            "narrowband": {"g": "out", "r": "out", "i": "out", "z": "out"},
            "windows": _windows(),
        },
    )
    cfg = rg["requests"][0]["configurations"][0]
    assert cfg["instrument_type"] == "2M0-SCICAM-MUSCAT"
    assert rg["requests"][0]["location"]["telescope_class"] == "2m0"
    ic = cfg["instrument_configs"][0]
    assert ic["extra_params"]["exposure_mode"] == "SYNCHRONOUS"
    assert ic["extra_params"]["exposure_time_z"] == 60.0
    assert ic["exposure_time"] == 60.0  # longest channel
    assert ic["optical_elements"]["narrowband_g_position"] == "out"
    assert rg["operator"] == "SINGLE" and rg["observation_type"] == "NORMAL"


def test_build_requestgroup_muscat3_and_muscat4():
    # muscat3 defaults site to ogg
    rg3 = lco.build_requestgroup(
        "muscat3",
        {
            "name": "wasp12",
            "proposal": "TEST2026A",
            "target_name": "WASP-12 b",
            "ra": 97.64,
            "dec": 29.67,
            "exposure_times": {"g": 30},
            "windows": _windows(),
        },
    )
    assert rg3["requests"][0]["configurations"][0]["instrument_type"] == "2M0-SCICAM-MUSCAT"
    assert rg3["requests"][0]["location"]["site"] == "ogg"

    # muscat4 defaults site to coj
    rg4 = lco.build_requestgroup(
        "muscat4",
        {
            "name": "wasp12",
            "proposal": "TEST2026A",
            "target_name": "WASP-12 b",
            "ra": 97.64,
            "dec": 29.67,
            "exposure_times": {"g": 30},
            "windows": _windows(),
        },
    )
    assert rg4["requests"][0]["configurations"][0]["instrument_type"] == "2M0-SCICAM-MUSCAT"
    assert rg4["requests"][0]["location"]["site"] == "coj"

    # explicit site is preserved
    rg_explicit = lco.build_requestgroup(
        "muscat4",
        {
            "name": "wasp12",
            "proposal": "TEST2026A",
            "target_name": "WASP-12 b",
            "ra": 97.64,
            "dec": 29.67,
            "exposure_times": {"g": 30},
            "windows": _windows(),
            "site": "lsc",
        },
    )
    assert rg_explicit["requests"][0]["location"]["site"] == "lsc"


def test_build_requestgroup_sinistro_shape():
    rg = lco.build_requestgroup(
        "sinistro",
        {
            "name": "s",
            "proposal": "TEST2026A",
            "target_name": "WASP-12 b",
            "ra": 97.64,
            "dec": 29.67,
            "filter": "rp",
            "exposure_time": 60,
            "exposure_count": 3,
            "windows": _windows(),
        },
    )
    cfg = rg["requests"][0]["configurations"][0]
    assert cfg["instrument_type"] == "1M0-SCICAM-SINISTRO"
    assert rg["requests"][0]["location"]["telescope_class"] == "1m0"
    ic = cfg["instrument_configs"][0]
    assert ic["optical_elements"]["filter"] == "rp"
    assert ic["exposure_count"] == 3


def test_build_requestgroup_muscat_requires_an_exposure_time():
    with pytest.raises(lco.LcoError):
        lco.build_requestgroup(
            "muscat",
            {"name": "x", "proposal": "P", "target_name": "T", "ra": 1, "dec": 1,
             "exposure_times": {}, "windows": _windows()},
        )


def test_build_requestgroup_rejects_bad_radec_and_proposal():
    base = {"name": "x", "proposal": "P", "target_name": "T", "ra": 1, "dec": 1,
            "filter": "rp", "exposure_time": 30, "windows": _windows()}
    with pytest.raises(lco.LcoError):
        lco.build_requestgroup("sinistro", {**base, "ra": 999})
    with pytest.raises(lco.LcoError):
        lco.build_requestgroup("sinistro", {**base, "proposal": "bad id!"})


def test_build_requestgroup_requires_windows():
    with pytest.raises(lco.LcoError):
        lco.build_requestgroup(
            "sinistro",
            {"name": "x", "proposal": "P", "target_name": "T", "ra": 1, "dec": 1,
             "filter": "rp", "exposure_time": 30, "windows": []},
        )


def test_build_requestgroup_unknown_kind():
    with pytest.raises(lco.LcoError):
        lco.build_requestgroup("hubble", {"windows": _windows()})


def test_payload_hash_is_stable_and_order_independent():
    a = lco.build_requestgroup("sinistro", {"name": "x", "proposal": "P", "target_name": "T",
                                            "ra": 1, "dec": 1, "filter": "rp", "exposure_time": 30,
                                            "windows": _windows()})
    b = lco.build_requestgroup("sinistro", {"target_name": "T", "ra": 1, "dec": 1, "filter": "rp",
                                            "exposure_time": 30, "windows": _windows(),
                                            "name": "x", "proposal": "P"})
    assert lco.payload_hash(a) == lco.payload_hash(b)


# --------------------------------------------------------------------------- #
# token / submit gating
# --------------------------------------------------------------------------- #


def test_load_token_raises_when_unset(monkeypatch):
    monkeypatch.delenv("LCO_API_TOKEN", raising=False)
    assert lco.has_token() is False
    with pytest.raises(lco.LcoError) as ei:
        lco.load_token()
    assert ei.value.status == 503


def test_submit_blocked_when_switch_off(monkeypatch):
    monkeypatch.setenv("LCO_API_TOKEN", "secret")
    monkeypatch.setenv("MUSCAT_LCO_ALLOW_SUBMIT", "0")
    with pytest.raises(lco.LcoError) as ei:
        lco.submit_requestgroup({"name": "x"})
    assert ei.value.status == 403


def test_config_state_reports_booleans_no_secret(monkeypatch):
    monkeypatch.setenv("LCO_API_TOKEN", "secret")
    monkeypatch.setenv("MUSCAT_LCO_DIR", "/tmp/lco")
    monkeypatch.setenv("MUSCAT_LCO_ALLOW_SUBMIT", "1")
    state = lco.config_state()
    assert state == {"token_configured": True, "download_root_configured": True, "submit_allowed": True}
    assert "secret" not in str(state)


# --------------------------------------------------------------------------- #
# download path resolution + no-overwrite guard
# --------------------------------------------------------------------------- #


def test_download_dir_uses_lco_root_when_set(monkeypatch, tmp_path):
    monkeypatch.setenv("MUSCAT_LCO_DIR", str(tmp_path))
    d = lco.download_dir("muscat3", "260101")
    assert d == tmp_path / "muscat3" / "260101"


def test_download_dir_falls_back_to_data_dir(monkeypatch, tmp_path):
    monkeypatch.delenv("MUSCAT_LCO_DIR", raising=False)
    monkeypatch.setenv("MUSCAT_DATA_DIR", str(tmp_path))
    d = lco.download_dir("sinistro", "260101")
    assert d == tmp_path / "260101"


def test_download_dir_validates_instrument_and_date(monkeypatch, tmp_path):
    monkeypatch.setenv("MUSCAT_LCO_DIR", str(tmp_path))
    with pytest.raises(lco.LcoError):
        lco.download_dir("hubble", "260101")
    with pytest.raises(lco.LcoError):
        lco.download_dir("muscat3", "2026-01-01")


def test_frame_dest_rejects_path_traversal(monkeypatch, tmp_path):
    monkeypatch.setenv("MUSCAT_LCO_DIR", str(tmp_path))
    for bad in ["../evil.fits", "a/b.fits", "..", ""]:
        with pytest.raises(lco.LcoError):
            lco.frame_dest("muscat3", "260101", bad)
    good = lco.frame_dest("muscat3", "260101", "ogg2m001-ep05-20260101-0001-e91.fits.fz")
    assert good.parent == tmp_path / "muscat3" / "260101"


def test_frame_date_dir_from_day_obs_and_date_obs():
    assert lco.frame_date_dir({"DAY_OBS": "2026-01-02"}) == "260102"
    assert lco.frame_date_dir({"DATE_OBS": "2026-01-02T05:33:00.123Z"}) == "260102"
    with pytest.raises(lco.LcoError):
        lco.frame_date_dir({})


def test_download_frame_existing_file_not_overwritten(tmp_path):
    dest = tmp_path / "x.fits"
    dest.write_text("original")
    res = lco.download_frame("https://example.com/x.fits", dest, overwrite=False)
    assert res["status"] == "exists"
    assert dest.read_text() == "original"  # untouched


def test_download_frame_rejects_bad_url(tmp_path):
    with pytest.raises(lco.LcoError):
        lco.download_frame("ftp://nope", tmp_path / "x.fits")


def test_download_frames_captures_per_file_errors(monkeypatch, tmp_path):
    monkeypatch.setenv("MUSCAT_LCO_DIR", str(tmp_path))
    frames = [
        {"filename": "../evil.fits", "url": "https://x/y", "DAY_OBS": "2026-01-01"},
        {"filename": "ok-20260101-0001-e91.fits", "url": "https://x/y", "DAY_OBS": "2026-01-01"},
    ]
    # Pre-create the "ok" file so download short-circuits to "exists" (no network).
    (tmp_path / "muscat3" / "260101").mkdir(parents=True)
    (tmp_path / "muscat3" / "260101" / "ok-20260101-0001-e91.fits").write_text("x")
    results = lco.download_frames("muscat3", frames, overwrite=False)
    assert results[0]["status"] == "error"
    assert results[1]["status"] == "exists"


# --------------------------------------------------------------------------- #
# HTTP layer (_request retry/backoff) with urlopen mocked
# --------------------------------------------------------------------------- #

import io
import urllib.error


class _FakeResp:
    def __init__(self, payload):
        self._data = payload.encode() if isinstance(payload, str) else payload

    def read(self):
        return self._data

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def test_request_success(monkeypatch):
    monkeypatch.setenv("LCO_API_TOKEN", "tok")
    monkeypatch.setattr(lco.urllib.request, "urlopen",
                        lambda req, timeout=None: _FakeResp('{"results": [1, 2]}'))
    out = lco._request("GET", lco.OBS_PORTAL_BASE + "/proposals/")
    assert out["results"] == [1, 2]


def test_request_4xx_is_not_retried(monkeypatch):
    monkeypatch.setenv("LCO_API_TOKEN", "tok")
    calls = {"n": 0}

    def boom(req, timeout=None):
        calls["n"] += 1
        raise urllib.error.HTTPError(req.full_url, 400, "bad", {}, io.BytesIO(b'{"detail": "nope"}'))

    monkeypatch.setattr(lco.urllib.request, "urlopen", boom)
    with pytest.raises(lco.LcoError) as ei:
        lco._request("GET", lco.OBS_PORTAL_BASE + "/x/")
    assert ei.value.status == 400 and calls["n"] == 1  # no retry on caller error


def test_request_retries_5xx_then_succeeds(monkeypatch):
    monkeypatch.setenv("LCO_API_TOKEN", "tok")
    monkeypatch.setattr(lco.time, "sleep", lambda s: None)
    state = {"tries": 0}

    def flaky(req, timeout=None):
        state["tries"] += 1
        if state["tries"] == 1:
            raise urllib.error.HTTPError(req.full_url, 503, "busy", {}, io.BytesIO(b""))
        return _FakeResp('{"ok_field": 1}')

    monkeypatch.setattr(lco.urllib.request, "urlopen", flaky)
    out = lco._request("GET", lco.OBS_PORTAL_BASE + "/x/")
    assert out["ok_field"] == 1 and state["tries"] == 2


def test_archive_search_forwards_only_allowed_params(monkeypatch):
    captured = {}

    def fake_request(method, url, **kw):
        captured["params"] = kw.get("params")
        return {"results": []}

    monkeypatch.setattr(lco, "_request", fake_request)
    lco.archive_search({"OBJECT": "WASP-12", "limit": "10", "bogus": "x"})
    assert "bogus" not in captured["params"]
    assert captured["params"]["OBJECT"] == "WASP-12"
    assert captured["params"]["limit"] == 10  # coerced + capped


def test_get_requestgroups_validates_proposal(monkeypatch):
    monkeypatch.setattr(lco, "_request", lambda *a, **k: {"results": []})
    with pytest.raises(lco.LcoError):
        lco.get_requestgroups("bad id!")
    assert lco.get_requestgroups("TEST2026A")["results"] == []
