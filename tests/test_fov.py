"""Tests for the FOV pointing/orientation optimizer (muscat_db.fov).

These cover the pure-geometry and scoring layers, which run offline. The Gaia
query and the full ``optimize`` orchestration need the network and are not
exercised here.
"""

import numpy as np
import pytest

from muscat_db import fov


@pytest.fixture(autouse=True)
def _clear_gaia_cache():
    # cached_query_gaia_field's LRU cache is module-level state; several tests
    # below reuse the same (ra, dec, radius) as a plain "some coordinates"
    # convention, which would otherwise let one test's mocked result leak into
    # another's via a cache hit.
    fov._gaia_cache.clear()
    yield
    fov._gaia_cache.clear()


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


def test_optimize_pointing_avoids_bright_star_by_shifting():
    # A useful comp cluster to the east; a forbidden bright star to the west
    # that is far enough that the field can exclude it by pointing east.
    east = np.array([120.0, 130.0])
    north = np.array([0.0, 10.0])
    weights = np.array([1.0, 1.0])
    avoid_east = np.array([-150.0])
    avoid_north = np.array([0.0])
    sol = fov.optimize_pointing(
        east, north, weights, half=100.0, margin=20.0,
        avoid_east=avoid_east, avoid_north=avoid_north,
    )
    assert sol.score >= 0  # a feasible pointing exists
    # The forbidden star must lie outside the chosen footprint.
    assert not fov.inside_square(
        avoid_east, avoid_north, sol.center_east, sol.center_north,
        sol.half_arcsec, sol.pa_deg,
    ).any()


def test_optimize_pointing_infeasible_when_bright_star_unavoidable():
    # A bright star sitting on the target (origin) can never be excluded while
    # keeping the target in the field: no feasible pointing → sentinel score.
    east = np.array([50.0])
    north = np.array([0.0])
    weights = np.array([1.0])
    sol = fov.optimize_pointing(
        east, north, weights, half=180.0, margin=30.0,
        avoid_east=np.array([0.0]), avoid_north=np.array([0.0]),
    )
    assert sol.score < 0


def test_optimize_avoid_mag_excludes_bright_star(monkeypatch):
    # Target at index 0 (Gmag 12). A very bright star (Gmag 7) sits far to one
    # side; with avoid_mag=9 the optimizer must keep it out of the footprint.
    mock_stars = fov.StarField(
        ra=np.array([10.0, 10.03, 9.94]),
        dec=np.array([-20.0, -20.0, -20.0]),
        gmag=np.array([12.0, 12.5, 7.0]),  # index 2 is the bright star to avoid
        bp_rp=np.array([0.8, 0.8, 0.8]),
        source="mock",
    )
    monkeypatch.setattr(fov, "query_gaia_field", lambda *a, **k: mock_stars)

    res = fov.optimize("muscat3", ra=10.0, dec=-20.0, avoid_mag=9.0)
    assert res.ok
    assert res.avoid_mag == 9.0
    assert res.n_avoided == 1
    # The bright star is reported as avoided and stays out of the comp list.
    assert res.avoided and res.avoided[0]["gmag"] == 7.0
    assert 7.0 not in [c["gmag"] for c in res.comps]
    # And it is geometrically outside the optimized footprint.
    half = res.fov_half_arcsec
    ae, an = fov.radec_to_tangent(
        np.array([res.avoided[0]["ra"]]), np.array([res.avoided[0]["dec"]]),
        res.ra, res.dec,
    )
    assert not fov.inside_square(
        ae, an, res.offset_east_arcsec, res.offset_north_arcsec, half, res.pa_deg,
    ).any()


def test_optimize_avoid_mag_infeasible_returns_error(monkeypatch):
    # A bright star right on the target cannot be avoided → error, not crash.
    mock_stars = fov.StarField(
        ra=np.array([10.0, 10.0006]),  # ~2" apart, both effectively on target
        dec=np.array([-20.0, -20.0]),
        gmag=np.array([12.0, 6.0]),
        bp_rp=np.array([0.8, 0.8]),
        source="mock",
    )
    monkeypatch.setattr(fov, "query_gaia_field", lambda *a, **k: mock_stars)

    res = fov.optimize("muscat3", ra=10.0, dec=-20.0, avoid_mag=9.0)
    assert not res.ok
    assert res.error and "brighter than" in res.error


def test_optimize_rejects_target_not_observable_for_instrument(monkeypatch):
    # All current sites sit at southern latitudes (~-30 to -32 deg), so a
    # target at dec=+60 never clears MIN_ALTITUDE_DEG above the horizon.
    def _fail_if_called(*_a, **_k):
        raise AssertionError("query_gaia_field must not be called for an unobservable target")

    monkeypatch.setattr(fov, "query_gaia_field", _fail_if_called)

    res = fov.optimize("muscat3", ra=10.0, dec=60.0)
    assert not res.ok
    assert res.error and "not observable" in res.error
    assert "muscat3" in res.error


def test_pm_at_returns_none_for_mismatched_length():
    # StarField built without pmra/pmdec defaults to an empty array.
    assert fov._pm_at(np.array([]), 0, 3) is None


def test_pm_at_returns_none_for_nan():
    assert fov._pm_at(np.array([1.0, float("nan")]), 1, 2) is None


def test_pm_at_returns_value():
    assert fov._pm_at(np.array([1.0, 2.5]), 1, 2) == pytest.approx(2.5)


def test_optimize_includes_target_and_comp_proper_motion(monkeypatch):
    mock_stars = fov.StarField(
        ra=np.array([10.0, 10.03]),
        dec=np.array([-20.0, -20.0]),
        gmag=np.array([12.0, 12.5]),
        bp_rp=np.array([0.8, 0.8]),
        pmra=np.array([15.2, -3.4]),
        pmdec=np.array([-8.1, 22.0]),
        source="mock",
    )
    monkeypatch.setattr(fov, "query_gaia_field", lambda *a, **k: mock_stars)

    res = fov.optimize("muscat3", ra=10.0, dec=-20.0)
    assert res.ok
    assert res.target_pmra == pytest.approx(15.2)
    assert res.target_pmdec == pytest.approx(-8.1)
    assert res.n_comps == 1
    assert res.comps[0]["pmra"] == pytest.approx(-3.4)
    assert res.comps[0]["pmdec"] == pytest.approx(22.0)


def test_optimize_comp_pm_is_none_when_star_field_lacks_pm_data(monkeypatch):
    # A StarField built without pmra/pmdec (older caller, or a catalog response
    # that omitted them) must not crash optimize(); PM is just unavailable.
    mock_stars = fov.StarField(
        ra=np.array([10.0, 10.03]),
        dec=np.array([-20.0, -20.0]),
        gmag=np.array([12.0, 12.5]),
        bp_rp=np.array([0.8, 0.8]),
        source="mock",
    )
    monkeypatch.setattr(fov, "query_gaia_field", lambda *a, **k: mock_stars)

    res = fov.optimize("muscat3", ra=10.0, dec=-20.0)
    assert res.ok
    assert res.target_pmra is None
    assert res.target_pmdec is None
    assert res.n_comps == 1
    assert res.comps[0]["pmra"] is None
    assert res.comps[0]["pmdec"] is None


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


# ── VizieR server-error detection ────────────────────────────────────────────
# A VizieR outage (e.g. its own database backend unreachable) comes back as an
# HTTP 200 VOTable with QUERY_STATUS=ERROR, which astroquery otherwise parses
# into an indistinguishable-from-empty TableList. These guard against that
# regressing into the misleading "No Gaia sources returned" message.

_VIZIER_DB_DOWN_VOTABLE = """<?xml version="1.0" encoding="UTF-8"?>
<VOTABLE version="1.4">
<INFO ID="Error" name="Error" value="The database is not currently reachable."/>
<INFO name="Error" value=" "/>
<INFO name="Error" value=" -- no connection"/>
<INFO name="Error" value="Postgres connect error"/>
<INFO name="QUERY_STATUS" value="ERROR">
 -- no connection
</INFO>
</VOTABLE>
"""

_VIZIER_EMPTY_RESULT_VOTABLE = """<?xml version="1.0" encoding="UTF-8"?>
<VOTABLE version="1.4">
<INFO name="QUERY_STATUS" value="OK"/>
</VOTABLE>
"""


def test_vizier_server_error_detects_database_outage():
    msg = fov._vizier_server_error(_VIZIER_DB_DOWN_VOTABLE)
    assert msg == "The database is not currently reachable."


def test_vizier_server_error_none_for_normal_response():
    assert fov._vizier_server_error(_VIZIER_EMPTY_RESULT_VOTABLE) is None


def test_vizier_server_error_falls_back_when_no_useful_detail():
    text = (
        '<INFO name="Error" value=" "/>'
        '<INFO name="QUERY_STATUS" value="ERROR">boom</INFO>'
    )
    assert fov._vizier_server_error(text) == "VizieR reported an unspecified server error"


def test_query_gaia_vizier_surfaces_server_error(monkeypatch):
    class _FakeResponse:
        text = _VIZIER_DB_DOWN_VOTABLE

    # Replace the whole Vizier class (which _query_gaia_vizier imports fresh) so
    # the fake response is returned regardless of how a given astroquery version
    # binds query_region_async. Patching only the class *method* is fragile:
    # some versions bind it per-instance, so the mock is bypassed and the real
    # (network) path runs — which is why this failed only on CI.
    class _FakeVizier:
        def __init__(self, *a, **k):
            pass

        def query_region_async(self, *a, **k):
            return _FakeResponse()

    monkeypatch.setattr("astroquery.vizier.Vizier", _FakeVizier)

    result = fov._query_gaia_vizier(10.0, -20.0, radius_arcsec=60.0, min_mag=0.0, max_mag=18.0)
    assert len(result) == 0
    assert result.error == "VizieR server error: The database is not currently reachable."


# ── ESA-first, VizieR-fallback orchestration ─────────────────────────────────

def test_query_gaia_field_uses_esa_when_it_succeeds(monkeypatch):
    esa_stars = fov.StarField(
        ra=np.array([10.0]), dec=np.array([-20.0]),
        gmag=np.array([12.0]), bp_rp=np.array([0.8]),
        source="Gaia DR3 (ESA)",
    )
    monkeypatch.setattr(fov, "_query_gaia_esa", lambda *a, **k: esa_stars)
    monkeypatch.setattr(
        fov, "_query_gaia_vizier",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("VizieR should not be called")),
    )

    result = fov.query_gaia_field(10.0, -20.0, radius_arcsec=60.0)
    assert result is esa_stars
    assert result.error is None


def test_query_gaia_field_falls_back_to_vizier_when_esa_fails(monkeypatch):
    failed_esa = fov.StarField(
        np.array([]), np.array([]), np.array([]), np.array([]),
        source="Gaia DR3 (ESA)", error="ESA Gaia query failed: timed out",
    )
    vizier_stars = fov.StarField(
        ra=np.array([10.0]), dec=np.array([-20.0]),
        gmag=np.array([12.0]), bp_rp=np.array([0.8]),
        source="Gaia DR3 (VizieR)",
    )
    monkeypatch.setattr(fov, "_query_gaia_esa", lambda *a, **k: failed_esa)
    monkeypatch.setattr(fov, "_query_gaia_vizier", lambda *a, **k: vizier_stars)

    result = fov.query_gaia_field(10.0, -20.0, radius_arcsec=60.0)
    assert result is vizier_stars
    assert result.error is None


def test_query_gaia_field_reports_combined_error_when_both_sources_fail(monkeypatch):
    failed_esa = fov.StarField(
        np.array([]), np.array([]), np.array([]), np.array([]),
        source="Gaia DR3 (ESA)", error="ESA Gaia query failed: timed out",
    )
    failed_vizier = fov.StarField(
        np.array([]), np.array([]), np.array([]), np.array([]),
        source="Gaia DR3 (VizieR)",
        error="VizieR server error: The database is not currently reachable.",
    )
    monkeypatch.setattr(fov, "_query_gaia_esa", lambda *a, **k: failed_esa)
    monkeypatch.setattr(fov, "_query_gaia_vizier", lambda *a, **k: failed_vizier)

    result = fov.query_gaia_field(10.0, -20.0, radius_arcsec=60.0)
    assert len(result) == 0
    assert "ESA Gaia query failed: timed out" in result.error
    assert "The database is not currently reachable." in result.error


# ── Gaia cone-search caching ──────────────────────────────────────────────────

def test_cached_query_gaia_field_hits_cache_on_repeat_call(monkeypatch):
    calls = []
    stars = fov.StarField(
        ra=np.array([10.0]), dec=np.array([-20.0]),
        gmag=np.array([12.0]), bp_rp=np.array([0.8]), source="mock",
    )

    def _fake_query(*args, **kwargs):
        calls.append((args, kwargs))
        return stars

    monkeypatch.setattr(fov, "query_gaia_field", _fake_query)

    first = fov.cached_query_gaia_field(10.0, -20.0, 60.0, min_mag=0.0, max_mag=18.0)
    second = fov.cached_query_gaia_field(10.0, -20.0, 60.0, min_mag=0.0, max_mag=18.0)

    assert first is stars
    assert second is stars
    assert len(calls) == 1  # second call served from cache, no re-query


def test_cached_query_gaia_field_does_not_cache_errors(monkeypatch):
    calls = {"n": 0}

    def _fake_query(*args, **kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            return fov.StarField(
                np.array([]), np.array([]), np.array([]), np.array([]),
                source="mock", error="transient outage",
            )
        return fov.StarField(
            ra=np.array([10.0]), dec=np.array([-20.0]),
            gmag=np.array([12.0]), bp_rp=np.array([0.8]), source="mock",
        )

    monkeypatch.setattr(fov, "query_gaia_field", _fake_query)

    first = fov.cached_query_gaia_field(10.0, -20.0, 60.0)
    second = fov.cached_query_gaia_field(10.0, -20.0, 60.0)

    assert first.error == "transient outage"
    assert second.error is None  # not served from cache; the retry actually ran
    assert calls["n"] == 2


def test_cached_query_gaia_field_distinguishes_different_fields(monkeypatch):
    def _fake_query(ra, dec, radius_arcsec, min_mag=0.0, max_mag=18.0):
        return fov.StarField(
            ra=np.array([ra]), dec=np.array([dec]),
            gmag=np.array([12.0]), bp_rp=np.array([0.8]), source="mock",
        )

    monkeypatch.setattr(fov, "query_gaia_field", _fake_query)

    a = fov.cached_query_gaia_field(10.0, -20.0, 60.0)
    b = fov.cached_query_gaia_field(50.0, 10.0, 60.0)
    assert a.ra[0] == 10.0
    assert b.ra[0] == 50.0
