"""Tests for the exposure time calculator (muscat_db.exposure).

Covers the pure-calculation layer (no network): calc_all_bands' multi-source
support (target + FOV comparison stars) and the griz-lookup fallback chain
used to resolve comparison-star photometry.
"""

from unittest.mock import patch

import pytest

from muscat_db import exposure


# ── calc_all_bands with extra_sources ────────────────────────────────────────


def test_calc_all_bands_single_source_matches_target_only_behavior():
    # No extra_sources: recommended_exptime is just the min across the
    # target's own bands, same as before this feature existed.
    result = exposure.calc_all_bands(
        "muscat3", {"gp": 12.0, "rp": 11.5}, focus_mm=0, sat_frac=0.5,
    )
    assert all(r["is_target"] for r in result["results"])
    assert all(r["source_label"] == "Target" for r in result["results"])
    assert result["recommended_exptime"] == min(r["exptime"] for r in result["results"])


def test_calc_all_bands_recommended_exptime_limited_by_comparison_star():
    # A comparison star much brighter than the target in gp saturates sooner,
    # so it -- not any of the target's own bands -- must set the ceiling.
    result = exposure.calc_all_bands(
        "muscat3",
        {"gp": 12.0, "rp": 12.0, "ip": 12.0, "zs": 12.0},
        focus_mm=0,
        sat_frac=0.5,
        extra_sources=[{"label": "Comp 1", "mags": {"gp": 8.0}}],
    )
    target_rows = [r for r in result["results"] if r["is_target"]]
    comp_rows = [r for r in result["results"] if not r["is_target"]]
    assert len(comp_rows) == 1
    assert comp_rows[0]["source_label"] == "Comp 1"
    assert comp_rows[0]["exptime"] < min(r["exptime"] for r in target_rows)
    assert result["recommended_exptime"] == comp_rows[0]["exptime"]


def test_calc_all_bands_multiple_extra_sources_all_included():
    result = exposure.calc_all_bands(
        "muscat3",
        {"gp": 12.0},
        focus_mm=0,
        sat_frac=0.5,
        extra_sources=[
            {"label": "Comp 1", "mags": {"gp": 11.0}},
            {"label": "Comp 2", "mags": {"gp": 13.0}},
        ],
    )
    labels = [r["source_label"] for r in result["results"]]
    assert labels == ["Target", "Comp 1", "Comp 2"]  # band-major, target first
    assert result["recommended_exptime"] == min(r["exptime"] for r in result["results"])


def test_calc_all_bands_extra_source_without_label_gets_default():
    result = exposure.calc_all_bands(
        "muscat3", {"gp": 12.0}, focus_mm=0, sat_frac=0.5,
        extra_sources=[{"label": None, "mags": {"gp": 11.0}}],
    )
    comp_rows = [r for r in result["results"] if not r["is_target"]]
    assert comp_rows[0]["source_label"] == "Comp 1"


def test_calc_all_bands_peak_mode_also_tags_sources():
    result = exposure.calc_all_bands(
        "muscat3", {"gp": 12.0}, focus_mm=0, mode="peak", exptime=30.0,
        extra_sources=[{"label": "Comp 1", "mags": {"gp": 11.0}}],
    )
    assert result["recommended_exptime"] is None  # only meaningful in exptime mode
    labels = {r["source_label"] for r in result["results"]}
    assert labels == {"Target", "Comp 1"}


# ── lookup_magnitudes_with_fallback ──────────────────────────────────────────


def test_lookup_magnitudes_with_fallback_uses_real_catalog_when_available():
    with patch.object(
        exposure, "lookup_magnitudes",
        return_value=({"gp": 11.0, "rp": 10.5}, "Pan-STARRS DR1 (within 3\")"),
    ):
        mags, source, is_approx = exposure.lookup_magnitudes_with_fallback(
            10.0, -20.0, gmag=11.2, bp_rp=0.8,
        )
    assert mags == {"gp": 11.0, "rp": 10.5}
    assert source == "Pan-STARRS DR1 (within 3\")"
    assert is_approx is False


def test_lookup_magnitudes_with_fallback_reports_no_photometry_when_catalog_and_gaia_both_miss():
    # gaia_to_griz_transform currently has no verified coefficients and always
    # returns None (see the TODO in exposure.py) -- confirm the wrapper
    # degrades to "no photometry" rather than fabricating a result.
    with patch.object(exposure, "lookup_magnitudes", return_value=(None, None)):
        mags, source, is_approx = exposure.lookup_magnitudes_with_fallback(
            10.0, -20.0, gmag=11.2, bp_rp=0.8,
        )
    assert mags is None
    assert source is None
    assert is_approx is False


def test_lookup_magnitudes_with_fallback_uses_gaia_transform_when_catalog_misses():
    # Exercises the fallback wiring itself (not the real transform, which is
    # unimplemented pending verified coefficients): a stubbed transform must
    # be picked up, tagged as approximate, and logged as such.
    with patch.object(exposure, "lookup_magnitudes", return_value=(None, None)), \
         patch.object(exposure, "gaia_to_griz_transform", return_value={"gp": 11.3}):
        mags, source, is_approx = exposure.lookup_magnitudes_with_fallback(
            10.0, -20.0, gmag=11.2, bp_rp=0.8,
        )
    assert mags == {"gp": 11.3}
    assert is_approx is True
    assert source is not None and "approx" in source.lower()


def test_lookup_magnitudes_with_fallback_skips_gaia_transform_without_gmag():
    with patch.object(exposure, "lookup_magnitudes", return_value=(None, None)) as mock_lookup, \
         patch.object(exposure, "gaia_to_griz_transform") as mock_transform:
        mags, source, is_approx = exposure.lookup_magnitudes_with_fallback(10.0, -20.0)
    mock_lookup.assert_called_once()
    mock_transform.assert_not_called()
    assert mags is None and source is None and is_approx is False


# ── gaia_to_griz_transform ────────────────────────────────────────────────────


def test_gaia_to_griz_transform_returns_none_without_color():
    assert exposure.gaia_to_griz_transform(11.2, None) is None


def test_gaia_to_griz_transform_returns_none_for_nan_color():
    assert exposure.gaia_to_griz_transform(11.2, float("nan")) is None
