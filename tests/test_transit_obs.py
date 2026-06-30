"""Unit tests for transit observability (muscat_db/transit_obs.py).

Uses real astropy with fixed times (no Time.now), so results are deterministic.
"""

from __future__ import annotations

import math

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
        assert r["rating"] in ("full", "partial", "none")
        assert set(r["sites"]).issubset({"ogg", "coj"})
        if r["rating"] == "none":
            assert r["best_site"] is None
        else:
            assert r["best_site"] in r["sites"]
        if r["rating"] == "full":
            assert r["best_site"] in r["sites"]


def test_classify_moon_constraint_forces_none():
    # No sample can be 180 deg from the Moon, so a 180 deg minimum rejects all.
    res = T.classify_transits(97.64, 29.67, _windows(4), "muscat", 2.5, moon_sep_min=180.0)
    assert all(r["rating"] == "none" for r in res)


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
