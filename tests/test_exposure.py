"""Tests for the exposure time calculator (muscat_db.exposure).

Covers the pure-calculation layer (no network): calc_all_bands' multi-source
support (target + FOV comparison stars) and the griz-lookup fallback chain
used to resolve comparison-star photometry.
"""

from unittest.mock import patch

import numpy as np
import pytest
from astropy.io import fits
from astropy.table import Table
from astropy.wcs import WCS

from muscat_db import exposure


def _wcs_header(nx: int, ny: int, crval=(10.0, 20.0), scale_arcsec=0.267):
    """Minimal celestial TAN WCS header for a test image."""
    w = WCS(naxis=2)
    w.wcs.crpix = [nx / 2.0, ny / 2.0]
    w.wcs.crval = list(crval)
    w.wcs.cdelt = [-scale_arcsec / 3600.0, scale_arcsec / 3600.0]
    w.wcs.ctype = ["RA---TAN", "DEC--TAN"]
    return w


# ── _measure_peak: measures the *target star's* peak, not a frame percentile ──


def test_measure_peak_locates_target_via_wcs(tmp_path):
    nx = ny = 200
    data = np.full((ny, nx), 100.0)  # background
    # bright 5x5 star well away from the frame centre
    sx, sy = 140, 60
    data[sy - 2:sy + 3, sx - 2:sx + 3] = 5000.0
    w = _wcs_header(nx, ny)
    fits_path = tmp_path / "sci.fits"
    fits.writeto(fits_path, data, header=w.to_header())

    star_sky = w.pixel_to_world(sx, sy)
    peak = exposure._measure_peak(
        str(fits_path), ra=star_sky.ra.deg, dec=star_sky.dec.deg
    )
    assert peak == pytest.approx(4900.0, abs=1.0)  # 5000 - background 100


def test_measure_peak_ignores_cosmic_ray_spike(tmp_path):
    nx = ny = 200
    data = np.full((ny, nx), 100.0)
    sx, sy = 100, 100
    data[sy - 2:sy + 3, sx - 2:sx + 3] = 5000.0  # 25-px star
    data[sy + 10, sx + 10] = 999999.0            # single hot pixel in the box
    fits_path = tmp_path / "sci.fits"
    fits.writeto(fits_path, data)  # no WCS -> frame-centre fallback

    peak = exposure._measure_peak(str(fits_path))
    # _PEAK_RANK-th brightest pixel is still the star, not the cosmic ray
    assert peak == pytest.approx(4900.0, abs=1.0)


def test_measure_peak_falls_back_to_frame_centre_without_wcs(tmp_path):
    nx = ny = 200
    data = np.full((ny, nx), 50.0)
    data[ny // 2 - 2:ny // 2 + 3, nx // 2 - 2:nx // 2 + 3] = 3000.0
    fits_path = tmp_path / "raw.fits"
    fits.writeto(fits_path, data)  # muscat/muscat2-style: no WCS

    peak = exposure._measure_peak(str(fits_path))
    assert peak == pytest.approx(2950.0, abs=1.0)


def test_measure_peak_reads_banzai_sci_extension(tmp_path):
    nx = ny = 120
    data = np.full((ny, nx), 200.0)
    data[ny // 2 - 2:ny // 2 + 3, nx // 2 - 2:nx // 2 + 3] = 8000.0
    hdul = fits.HDUList([
        fits.PrimaryHDU(),  # BANZAI: empty primary
        fits.ImageHDU(data, name="SCI"),
    ])
    fits_path = tmp_path / "e91.fits"
    hdul.writeto(fits_path)

    peak = exposure._measure_peak(str(fits_path))
    assert peak == pytest.approx(7800.0, abs=1.0)


# ── _electron_gain: no double-count of gain for BANZAI-reduced data ───────────


@pytest.mark.parametrize("instrument", ["muscat3", "muscat4", "sinistro"])
def test_electron_gain_is_unity_for_banzai_instruments(instrument):
    assert exposure._electron_gain(instrument) == 1.0


@pytest.mark.parametrize("instrument", ["muscat", "muscat2"])
def test_electron_gain_uses_ccd_gain_for_raw_instruments(instrument):
    expected = exposure.INSTRUMENT_PARAMS[instrument]["gain"]
    assert exposure._electron_gain(instrument) == expected


def test_calc_peak_does_not_double_count_gain_for_muscat3():
    # BANZAI pixels are already electrons, so reported electrons must equal the
    # native peak (previously multiplied by the 1.8 CCD gain).
    r = exposure.calc_peak("muscat3", "gp", 12.0, focus_mm=0, exptime=30.0)
    assert r["peak_electrons"] == pytest.approx(r["peak_adu"])


# ── _extract_mags: prefer the most complete source over a nearer fragment ─────


def test_extract_mags_prefers_complete_source_over_nearer_fragment():
    col_map = {"gmag": "gp", "rmag": "rp", "imag": "ip", "zmag": "zs"}
    # Row 0 is nearest but a deblended fragment (only r,i, with a spurious r);
    # row 1 is the real star 0.6" further out with a full, consistent griz set.
    cat = Table(
        {
            "_r": [0.41, 1.04],
            "gmag": [np.nan, 11.77],
            "rmag": [14.29, 10.74],
            "imag": [11.74, 10.63],
            "zmag": [np.nan, 10.70],
        }
    )
    mags = exposure._extract_mags(cat, col_map)
    assert mags == {"gp": 11.77, "rp": 10.74, "ip": 10.63, "zs": 10.70}
    assert mags["rp"] != 14.29  # the fragment's spurious magnitude is rejected


def test_extract_mags_backfills_missing_band_from_next_source():
    col_map = {"gmag": "gp", "rmag": "rp", "imag": "ip", "zmag": "zs"}
    # Primary (most complete) has 3 bands; zs only exists on the second source.
    cat = Table(
        {
            "_r": [0.5, 2.0],
            "gmag": [11.0, np.nan],
            "rmag": [10.5, np.nan],
            "imag": [10.4, np.nan],
            "zmag": [np.nan, 10.3],
        }
    )
    mags = exposure._extract_mags(cat, col_map)
    assert mags == {"gp": 11.0, "rp": 10.5, "ip": 10.4, "zs": 10.3}


def test_measure_header_fwhm_pix_converts_banzai_arcsec_to_pixels(tmp_path):
    fits_path = tmp_path / "frame.fits"
    fits.writeto(fits_path, np.zeros((2, 2)), header=fits.Header({"L1FWHM": 2.67}))

    assert exposure._measure_header_fwhm_pix(str(fits_path), "muscat3") == pytest.approx(10.0)


@pytest.mark.parametrize("value", [None, -1.0, 0.0, "nan"])
def test_measure_header_fwhm_pix_rejects_missing_or_invalid_values(tmp_path, value):
    fits_path = tmp_path / "frame.fits"
    header = fits.Header()
    if value is not None:
        header["L1FWHM"] = value
    fits.writeto(fits_path, np.zeros((2, 2)), header=header)

    assert exposure._measure_header_fwhm_pix(str(fits_path), "muscat4") is None


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
