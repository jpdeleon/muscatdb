"""Unit tests for transit observability (muscat_db/transit_obs.py).

Uses real astropy with fixed times (no Time.now), so results are deterministic.
"""

from __future__ import annotations


import pytest

from muscat_db import transit_obs as T


# --------------------------------------------------------------------------- #
# constraint helpers
# --------------------------------------------------------------------------- #


def test_alt_limit_from_airmass():
    assert T.alt_limit_from_airmass(2.0) == pytest.approx(30.0, abs=1e-6)
    assert T.alt_limit_from_airmass(1.6) == pytest.approx(38.68, abs=0.1)
    assert T.alt_limit_from_airmass(1.0) == pytest.approx(90.0, abs=1e-6)


def test_alt_limit_rejects_bad_airmass():
    with pytest.raises(T.TransitObsError):
        T.alt_limit_from_airmass(0.5)


def test_twilight_limit_mapping_and_validation():
    assert T.twilight_limit("civil") == -6.0
    assert T.twilight_limit("nautical") == -12.0
    assert T.twilight_limit("astronomical") == -18.0
    assert T.twilight_limit(None) == -12.0  # default nautical
    with pytest.raises(T.TransitObsError):
        T.twilight_limit("dusk")


def test_sites_for_kind():
    assert T.sites_for_kind("muscat") == ["ogg", "coj"]
    assert set(T.sites_for_kind("sinistro")) == {"lsc", "cpt", "coj", "tfn", "elp"}
    with pytest.raises(T.TransitObsError):
        T.sites_for_kind("hubble")


def test_resolve_site_list_priority():
    # Explicit sites win, normalised and de-duplicated.
    assert T.resolve_site_list(["COJ", "ogg", "coj"]) == ["coj", "ogg"]
    # Falls back to the kind when no sites are given.
    assert T.resolve_site_list(None, "muscat4") == ["coj"]
    # Empty/omitted with no kind => the full LCO network.
    assert T.resolve_site_list() == list(T.LCO_SITES)
    assert T.resolve_site_list([], None) == list(T.LCO_SITES)


def test_resolve_site_list_rejects_unknown_site():
    with pytest.raises(T.TransitObsError):
        T.resolve_site_list(["ogg", "atlantis"])


# --------------------------------------------------------------------------- #
# classify_transits
# --------------------------------------------------------------------------- #


def _windows(n=6, start_hour=4):
    return [{"epoch": i, "mid": f"2026-03-15T{start_hour + i:02d}:30:00"} for i in range(n)]


def test_classify_returns_one_aligned_entry_per_window():
    wins = _windows(5)
    res = T.classify_transits(97.64, 29.67, wins, "muscat", 2.5)
    assert len(res) == len(wins)
    for r in res:
        assert r["rating"] in ("full", "split_gap", "split_overlap", "partial", "none")
        assert set(r["sites"]).issubset({"ogg", "coj"})
        if r["rating"] == "none":
            assert r["best_site"] is None
        else:
            assert r["best_site"] in r["sites"]
        if r["rating"] == "full":
            assert r["best_site"] in r["sites"]


def test_classify_moon_phase_cap_forces_none():
    # max_lunar_phase=0 admits only a perfectly new (0% illuminated) Moon, which
    # never holds across a real window, so every window is rejected. (A 180 deg
    # min-separation no longer forces none: it is skipped whenever the Moon is
    # below the horizon or < 10% illuminated.)
    res = T.classify_transits(97.64, 29.67, _windows(4), "muscat", 2.5, max_lunar_phase=0.0)
    assert all(r["rating"] == "none" for r in res)


def test_observable_mask_separation_cut_and_phase_cap():
    from astropy.time import Time
    from astropy.coordinates import SkyCoord, get_body

    loc = T._earth_location("lsc")
    # Full-moon night, Moon well above the horizon at LSC; target sits on the Moon.
    t = Time(["2024-01-25T04:00:00"])
    moon = get_body("moon", Time("2024-01-25T04:00:00"))
    tgt = SkyCoord(moon.ra, moon.dec)

    mask, _talt, malt, _salt, msep, illum = T._observable_mask(
        tgt, loc, t, 20.0, -12.0, moon_sep_min=30.0)
    assert malt[0] > 0 and illum[0] > 0.9 and msep[0] < 5.0  # Moon up, bright, on-target
    assert not bool(mask[0])  # rejected by the min-separation cut

    # No separation cut, but the phase cap drops the bright Moon.
    mask_phase, *_ = T._observable_mask(
        tgt, loc, t, 20.0, -12.0, moon_sep_min=0.0, max_lunar_phase=0.5)
    assert not bool(mask_phase[0])


def test_classify_is_monotonic_in_strictness():
    wins = _windows(8)
    lenient = T.classify_transits(97.64, 29.67, wins, "sinistro", 2.5,
                                  max_airmass=40, twilight="civil", moon_sep_min=0)
    strict = T.classify_transits(97.64, 29.67, wins, "sinistro", 2.5,
                                 max_airmass=1.1, twilight="astronomical", moon_sep_min=120)
    n_lenient = sum(r["rating"] != "none" for r in lenient)
    n_strict = sum(r["rating"] != "none" for r in strict)
    assert n_lenient >= n_strict


def test_classify_empty_windows():
    assert T.classify_transits(97.64, 29.67, [], "muscat", 2.5) == []


def test_classify_explicit_sites_override_kind():
    # Passing ``sites`` restricts the evaluation regardless of ``kind``.
    wins = _windows(6)
    res = T.classify_transits(97.64, 29.67, wins, "muscat", 2.5, sites=["lsc"])
    for r in res:
        assert set(r["sites"]).issubset({"lsc"})


def test_classify_full_network_default_when_no_sites_or_kind():
    # kind=None and no sites => the full LCO network is considered, so reported
    # sites can include codes outside any single instrument kind.
    wins = _windows(8, start_hour=0)
    res = T.classify_transits(97.64, 29.67, wins, None, 2.5, max_airmass=40,
                              twilight="civil", moon_sep_min=0, sites=None)
    seen = set()
    for r in res:
        seen.update(r["sites"])
    assert seen.issubset(set(T.LCO_SITES))


def test_classify_checks_padding_observability():
    # A window that is rated "full" on the bare transit:
    wins = [{"epoch": 0, "mid": "2026-03-15T07:30:00"}]
    res_no_pad = T.classify_transits(97.64, 29.67, wins, "muscat", 2.0)
    assert res_no_pad[0]["rating"] == "full"

    # Add a start time in the middle of the day (20 UTC / 9:30 AM local), when
    # the sun is up, making the padded baseline unobservable.
    wins_with_bad_pad = [{
        "epoch": 0,
        "start": "2026-03-15T20:00:00",
        "mid": "2026-03-15T07:30:00",
        "end": "2026-03-15T08:30:00",
    }]
    # Default: padding is span-only metadata, so the bare transit still rates "full".
    res_default = T.classify_transits(97.64, 29.67, wins_with_bad_pad, "muscat", 2.0)
    assert res_default[0]["rating"] == "full"

    # include_padding=True: the unobservable baseline demotes it below "full".
    res_with_bad_pad = T.classify_transits(
        97.64, 29.67, wins_with_bad_pad, "muscat", 2.0, include_padding=True
    )
    assert res_with_bad_pad[0]["rating"] != "full"


# --------------------------------------------------------------------------- #
# split rating (two-site relay)
#
# These use ``monkeypatch`` on ``_earth_location``/``_observable_mask`` so the
# per-site coverage pattern is deterministic and independent of real
# astronomical geometry -- what's under test is the pair-search/rating logic
# in ``classify_transits``, not the astropy math (already covered above).
# --------------------------------------------------------------------------- #


def _patch_site_masks(monkeypatch, site_true_ranges):
    """Route ``_observable_mask`` to a per-site boolean pattern keyed by site
    name. ``site_true_ranges`` maps site -> (start_idx, end_idx) marked True
    (end exclusive); sites not present are all-False (no coverage)."""

    import numpy as np

    monkeypatch.setattr(T, "_earth_location", lambda site: site)

    def fake_observable_mask(target, location, times, alt_min, sun_alt_max,
                             moon_sep_min, max_lunar_phase=1.0):
        n = len(times)
        mask = np.zeros(n, dtype=bool)
        rng = site_true_ranges.get(location)
        if rng:
            mask[rng[0]:rng[1]] = True
        zeros = np.zeros(n)
        return mask, zeros, zeros, zeros, zeros, zeros

    monkeypatch.setattr(T, "_observable_mask", fake_observable_mask)


def _n_samp_for(duration_hours):
    return max(5, int(round(duration_hours * 60.0 / T._CLASSIFY_STEP_MIN)) + 1)


def test_classify_split_overlap_when_two_sites_meet_cleanly(monkeypatch):
    duration = 2.0
    n = _n_samp_for(duration)  # 41 samples for a 2h transit at 3-min cadence
    mid = n // 2
    # lsc covers the early ~55%, cpt the late ~55% -- overlapping in the middle,
    # together spanning the whole transit with no gap.
    _patch_site_masks(monkeypatch, {"lsc": (0, mid + 2), "cpt": (mid - 2, n)})

    wins = [{"epoch": 0, "mid": "2026-03-15T07:30:00"}]
    res = T.classify_transits(97.64, 29.67, wins, None, duration, sites=["lsc", "cpt", "tfn"])

    assert res[0]["rating"] == "split_overlap"
    assert res[0]["split_sites"] == ["lsc", "cpt"]  # chronological: lsc starts first
    assert res[0]["split_gap_min"] == 0.0
    assert res[0]["split_overlap_min"] > 0.0
    sites_by_name = {w["site"]: w for w in res[0]["split_windows"]}
    assert set(sites_by_name) == {"lsc", "cpt"}
    assert not sites_by_name["lsc"]["fragmented"]
    assert not sites_by_name["cpt"]["fragmented"]


def test_classify_split_gap_when_relay_leaves_a_gap(monkeypatch):
    duration = 2.0
    n = _n_samp_for(duration)
    # lsc covers only the first third, cpt only the last third: a real gap
    # remains in the middle even combined -- this is a relay (2+ sites with
    # coverage), so it must resolve to "split_gap", not a vague "partial".
    _patch_site_masks(monkeypatch, {"lsc": (0, n // 3), "cpt": (2 * n // 3, n)})

    wins = [{"epoch": 0, "mid": "2026-03-15T07:30:00"}]
    res = T.classify_transits(97.64, 29.67, wins, None, duration, sites=["lsc", "cpt"])

    assert res[0]["rating"] == "split_gap"
    assert res[0]["split_sites"] == ["lsc", "cpt"]
    assert res[0]["split_gap_min"] > 0.0
    assert res[0]["split_overlap_min"] == 0.0


def test_classify_split_falls_back_to_partial_when_union_misses_a_contact(monkeypatch):
    duration = 2.0
    n = _n_samp_for(duration)
    # cpt sees only a middle sliver (no contact); lsc sees the back half incl.
    # egress (the last sample). The pair's union covers egress but NOT ingress
    # (sample 0), so it is not a genuine relay -- it falls back to a partial on
    # the useful single site (lsc), not a misleading cpt->lsc split.
    _patch_site_masks(monkeypatch, {"cpt": (n // 2 - 3, n // 2), "lsc": (n // 2, n)})
    wins = [{"epoch": 0, "mid": "2026-03-15T07:30:00"}]
    res = T.classify_transits(97.64, 29.67, wins, None, duration, sites=["lsc", "cpt", "tfn"])
    assert res[0]["rating"] == "partial"
    assert res[0]["best_site"] == "lsc"
    assert "split_sites" not in res[0]


def test_classify_split_falls_back_to_none_when_union_misses_both_contacts(monkeypatch):
    duration = 2.0
    n = _n_samp_for(duration)
    # Two disjoint middle slivers, neither touching ingress (sample 0) nor
    # egress (last sample): no contact anywhere, so the transit is "none".
    _patch_site_masks(monkeypatch, {"cpt": (n // 3, n // 3 + 3), "lsc": (2 * n // 3, 2 * n // 3 + 3)})
    wins = [{"epoch": 0, "mid": "2026-03-15T07:30:00"}]
    res = T.classify_transits(97.64, 29.67, wins, None, duration, sites=["lsc", "cpt", "tfn"])
    assert res[0]["rating"] == "none"
    assert res[0]["best_site"] is None
    assert res[0]["sites"] == []


def test_classify_partial_reserved_for_single_site(monkeypatch):
    duration = 2.0
    n = _n_samp_for(duration)
    # Only lsc has coverage, and it spans the front half -- so it includes the
    # ingress contact (sample 0). One site + a contact observed => "partial".
    _patch_site_masks(monkeypatch, {"lsc": (0, n // 2)})

    wins = [{"epoch": 0, "mid": "2026-03-15T07:30:00"}]
    res = T.classify_transits(97.64, 29.67, wins, None, duration, sites=["lsc", "cpt", "tfn"])

    assert res[0]["rating"] == "partial"
    assert res[0]["sites"] == ["lsc"]
    assert res[0]["best_site"] == "lsc"
    assert "split_sites" not in res[0]


def test_classify_partial_requires_a_contact_not_mid_only(monkeypatch):
    duration = 2.0
    n = _n_samp_for(duration)
    # The single covering site sees only the middle of the transit -- neither
    # ingress (sample 0) nor egress (last sample). With no contact observed this
    # is not a useful partial and is rated "none".
    _patch_site_masks(monkeypatch, {"lsc": (n // 2 - 5, n // 2 + 6)})

    wins = [{"epoch": 0, "mid": "2026-03-15T07:30:00"}]
    res = T.classify_transits(97.64, 29.67, wins, None, duration, sites=["lsc", "cpt", "tfn"])

    assert res[0]["rating"] == "none"
    assert res[0]["sites"] == []
    assert res[0]["best_site"] is None


def test_classify_partial_on_egress_contact(monkeypatch):
    duration = 2.0
    n = _n_samp_for(duration)
    # A single site covering the back half sees egress (the last sample): a
    # genuine partial even though ingress is missed.
    _patch_site_masks(monkeypatch, {"lsc": (n // 2, n)})

    wins = [{"epoch": 0, "mid": "2026-03-15T07:30:00"}]
    res = T.classify_transits(97.64, 29.67, wins, None, duration, sites=["lsc", "cpt", "tfn"])

    assert res[0]["rating"] == "partial"
    assert res[0]["best_site"] == "lsc"


def test_classify_full_site_skips_pair_search(monkeypatch):
    duration = 2.0
    n = _n_samp_for(duration)
    # ogg covers the whole transit alone; coj covers only part of it. A valid
    # split pair may exist, but a full single site is always preferred.
    _patch_site_masks(monkeypatch, {"ogg": (0, n), "coj": (0, n // 2)})

    wins = [{"epoch": 0, "mid": "2026-03-15T07:30:00"}]
    res = T.classify_transits(97.64, 29.67, wins, None, duration, sites=["ogg", "coj"])

    assert res[0]["rating"] == "full"
    assert res[0]["best_site"] == "ogg"
    assert "split_sites" not in res[0]


def test_classify_ingress_bracket_flags(monkeypatch):
    # duration=2h, pad 30 min each side -> the padded grid spans 3h
    # (06:00-09:00). At 3-min cadence that's n=61 samples; ingress (06:30)
    # sits at index 10, egress (08:30) at index 50.
    duration = 2.0
    pad_min = 30.0
    n = _n_samp_for(duration + 2 * pad_min / 60.0)
    assert n == 61
    ingress_idx = 10

    wins = [{
        "epoch": 0, "mid": "2026-03-15T07:30:00",
        "start": "2026-03-15T06:00:00", "end": "2026-03-15T09:00:00",
    }]

    # OK case: lsc covers from the grid start through well past ingress+30min
    # (index 25); cpt covers from before ingress through the end. No gap
    # (split_overlap), and lsc's own coverage brackets the ingress contact
    # cleanly on both sides.
    _patch_site_masks(monkeypatch, {"lsc": (0, ingress_idx + 15), "cpt": (ingress_idx + 8, n)})
    res = T.classify_transits(97.64, 29.67, wins, None, duration, sites=["lsc", "cpt"],
                               include_padding=True, pad_before_min=pad_min, pad_after_min=pad_min)
    assert res[0]["rating"] == "split_overlap"
    assert res[0]["ingress_bracket_ok"] is True

    # Failing case: lsc's coverage stops right at ingress itself, leaving no
    # post-ingress baseline from lsc alone -- the union still has no gap
    # (cpt already picks up just before ingress), but the bracket fails.
    _patch_site_masks(monkeypatch, {"lsc": (0, ingress_idx + 1), "cpt": (ingress_idx - 1, n)})
    res2 = T.classify_transits(97.64, 29.67, wins, None, duration, sites=["lsc", "cpt"],
                                include_padding=True, pad_before_min=pad_min, pad_after_min=pad_min)
    assert res2[0]["rating"] == "split_overlap"
    assert res2[0]["ingress_bracket_ok"] is False


def test_site_coverage_span_detects_fragmentation():
    import numpy as np

    contiguous = np.array([False, True, True, True, False])
    fragmented = np.array([True, False, False, True, False])
    offsets = np.linspace(0.0, 1.0, len(contiguous))

    start, end, frag = T._site_coverage_span(contiguous, offsets, 0.0, 1.0)
    assert frag is False
    assert start == pytest.approx(0.25)
    assert end == pytest.approx(0.75)

    _, _, frag2 = T._site_coverage_span(fragmented, offsets, 0.0, 1.0)
    assert frag2 is True


# --------------------------------------------------------------------------- #
# visibility_series
# --------------------------------------------------------------------------- #


def test_visibility_series_structure():
    s = T.visibility_series(97.64, 29.67, "2026-03-15T10:00:00", 2.5, "ogg",
                            max_airmass=2.0, twilight="nautical", moon_sep_min=30)
    n = len(s["times"])
    assert n > 100
    for key in ("target_alt", "moon_alt", "sun_alt", "moon_sep"):
        assert len(s[key]) == n
    assert s["site"] == "ogg"
    assert s["alt_limit"] == pytest.approx(30.0, abs=1e-6)
    assert s["sun_alt_limit"] == -12.0
    assert s["ingress"] < s["mid"] if "mid" in s else s["ingress"] < s["egress"]
    assert s["ingress"] < s["egress"]
    assert 0.0 <= s["observable_fraction"] <= 1.0
    assert 0.0 <= s["moon_sep_mid"] <= 180.0


def test_visibility_series_rejects_unknown_site():
    with pytest.raises(T.TransitObsError):
        T.visibility_series(97.64, 29.67, "2026-03-15T10:00:00", 2.5, "jwst")
