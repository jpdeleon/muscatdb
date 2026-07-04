"""Tests for the FOV pointing/orientation optimizer (muscat_db.fov).

These cover the pure-geometry and scoring layers, which run offline. The Gaia
query and the full ``optimize`` orchestration need the network and are not
exercised here.
"""

import numpy as np
import pytest

from muscat_db import fov


# ── footprint sizes from the VOTable XML ─────────────────────────────────────

@pytest.mark.parametrize(
    "instrument, half_arcsec",
    [
        ("muscat", 180.0),
        ("muscat2", 222.0),
        ("muscat3", 273.0),
        ("muscat4", 273.0),  # shares the MuSCAT3 footprint
    ],
)
def test_load_fov_halfsize_from_xml(instrument, half_arcsec):
    assert fov.load_fov_halfsize_arcsec(instrument) == pytest.approx(half_arcsec)


def test_load_fov_halfsize_fallback_for_sinistro():
    # No XML; uses default central_2k_2x2 mode (13' FOV, ~6.6 arcmin half-width).
    half = fov.load_fov_halfsize_arcsec("sinistro")
    assert 350.0 < half < 450.0
    # Full frame mode is larger
    assert fov.SINISTRO_MODES["full_frame"] > fov.SINISTRO_MODES["central_2k_2x2"]


def test_load_fov_halfsize_unknown_instrument_raises():
    with pytest.raises(ValueError):
        fov.load_fov_halfsize_arcsec("not_a_real_instrument")


# ── tangent-plane projection round trip ──────────────────────────────────────

@pytest.mark.parametrize("ra0, dec0", [(180.0, 29.67), (10.0, -45.0), (359.5, 0.0)])
@pytest.mark.parametrize("east, north", [(0.0, 0.0), (100.0, -50.0), (-180.0, 200.0)])
def test_tangent_roundtrip(ra0, dec0, east, north):
    ra, dec = fov.tangent_to_radec(east, north, ra0, dec0)
    e2, n2 = fov.radec_to_tangent(np.array([ra]), np.array([dec]), ra0, dec0)
    assert float(e2[0]) == pytest.approx(east, abs=1e-4)
    assert float(n2[0]) == pytest.approx(north, abs=1e-4)


def test_tangent_origin_maps_to_center():
    assert fov.tangent_to_radec(0.0, 0.0, 123.0, -10.0) == pytest.approx((123.0, -10.0))


# ── square membership ────────────────────────────────────────────────────────

def test_inside_square_axis_aligned():
    east = np.array([0.0, 150.0, 200.0])
    north = np.array([0.0, 150.0, 0.0])
    mask = fov.inside_square(east, north, cx=0.0, cy=0.0, half=180.0, pa_deg=0.0)
    assert mask.tolist() == [True, True, False]


def test_inside_square_offset_center_captures_cluster():
    # A point at (160, 0) is outside a centered field but inside one shifted east.
    east, north = np.array([160.0]), np.array([0.0])
    assert not fov.inside_square(east, north, 0.0, 0.0, 150.0, 0.0)[0]
    assert fov.inside_square(east, north, 60.0, 0.0, 150.0, 0.0)[0]


def test_inside_square_rotation_matters_at_corners():
    # Near a corner: rotating the field by 45 deg can push a point out.
    east, north = np.array([170.0]), np.array([170.0])
    assert fov.inside_square(east, north, 0.0, 0.0, 180.0, 0.0)[0]
    assert not fov.inside_square(east, north, 0.0, 0.0, 180.0, 45.0)[0]


def test_footprint_corners_count_and_span():
    corners = fov.footprint_corners_radec(0.0, 0.0, 180.0, 0.0, 180.0, 0.0)
    assert len(corners) == 4
    # Re-project corners; they should sit at ~+/-180 arcsec from center.
    ras = [c[0] for c in corners]
    decs = [c[1] for c in corners]
    e, n = fov.radec_to_tangent(np.array(ras), np.array(decs), 180.0, 0.0)
    assert np.allclose(np.abs(e), 180.0, atol=0.5)
    assert np.allclose(np.abs(n), 180.0, atol=0.5)


# ── comparison-star scoring ──────────────────────────────────────────────────

def test_comparison_weight_peaks_near_target_brightness():
    g = np.array([11.0, 11.3, 15.0])  # target ~ 11.0
    w = fov.comparison_weights(g, None, target_g=11.0, target_bp_rp=None)
    # Slightly fainter (matching IDEAL_DMAG) beats much fainter.
    assert w[1] > w[2]
    assert w[1] == pytest.approx(1.0, abs=0.05)


def test_comparison_weight_penalizes_much_brighter():
    g = np.array([11.0, 8.5])  # one comp 2.5 mag brighter than the target
    w = fov.comparison_weights(g, None, target_g=11.0, target_bp_rp=None)
    assert w[1] < w[0] * fov.BRIGHT_PENALTY + 1e-6


def test_comparison_weight_color_similarity_bonus():
    g = np.array([11.3, 11.3])
    bp_rp = np.array([0.8, 2.5])  # first matches target color, second does not
    w = fov.comparison_weights(g, bp_rp, target_g=11.0, target_bp_rp=0.8)
    assert w[0] > w[1]


# ── pointing + PA search ─────────────────────────────────────────────────────

def test_optimize_pointing_keeps_target_inside_with_margin():
    east = np.array([140.0, 150.0, 160.0])
    north = np.array([0.0, 10.0, -10.0])
    weights = np.array([1.0, 1.0, 1.0])
    sol = fov.optimize_pointing(east, north, weights, half=180.0, margin=30.0)
    # Target (origin) must remain inside by the margin.
    inside = fov.inside_square(
        np.array([0.0]), np.array([0.0]),
        sol.center_east, sol.center_north, sol.half_arcsec - sol.margin_arcsec,
        sol.pa_deg,
    )[0]
    assert inside
    # All three comps fit, so they should all be captured.
    assert sol.in_field.sum() == 3
    assert sol.score == pytest.approx(3.0)


def test_optimize_pointing_prefers_richer_side():
    # Cluster of three useful comps to the east; one weak comp to the west.
    east = np.array([150.0, 160.0, 140.0, -160.0])
    north = np.array([5.0, -10.0, 20.0, 0.0])
    weights = np.array([1.0, 1.0, 1.0, 0.2])
    sol = fov.optimize_pointing(east, north, weights, half=120.0, margin=20.0)
    # The optimizer should shift east (positive) to grab the rich cluster.
    assert sol.center_east > 0
    assert sol.in_field[:3].sum() == 3


def test_optimize_pointing_rejects_oversized_margin():
    with pytest.raises(ValueError):
        fov.optimize_pointing(
            np.array([0.0]), np.array([0.0]), np.array([1.0]),
            half=100.0, margin=120.0,
        )


def test_optimize_magnitude_filtering(monkeypatch):
    # Mock query_gaia_field to return a specific set of stars
    mock_stars = fov.StarField(
        ra=np.array([10.0, 10.01, 10.02, 10.03, 10.04]),
        dec=np.array([-20.0, -20.01, -20.02, -20.03, -20.04]),
        gmag=np.array([12.0, 10.0, 11.5, 14.5, 17.0]), # target is star index 0 (12.0 mag)
        bp_rp=np.array([0.8, 0.8, 0.8, 0.8, 0.8]),
        source="mock"
    )
    monkeypatch.setattr(fov, "query_gaia_field", lambda *args, **kwargs: mock_stars)

    # 1. Absolute limits: min_mag=11.0, max_mag=15.0
    # Expected comps: stars in [11.0, 15.0] (index 2: 11.5, index 3: 14.5)
    res = fov.optimize("muscat3", ra=10.0, dec=-20.0, min_mag=11.0, max_mag=15.0)
    assert res.ok
    comp_gmags = [c["gmag"] for c in res.comps]
    assert 11.5 in comp_gmags
    assert 14.5 in comp_gmags
    assert 10.0 not in comp_gmags
    assert 17.0 not in comp_gmags

    # 2. Relative limits (mag_delta=1.0)
    # Target Gmag is 12.0. Expected range: [11.0, 13.0]
    # Expected comps: stars in [11.0, 13.0] (index 2: 11.5)
    res = fov.optimize("muscat3", ra=10.0, dec=-20.0, mag_delta=1.0)
    assert res.ok
    comp_gmags = [c["gmag"] for c in res.comps]
    assert 11.5 in comp_gmags
    assert 10.0 not in comp_gmags
    assert 14.5 not in comp_gmags
