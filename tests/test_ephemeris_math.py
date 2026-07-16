"""Unit tests for muscat_db.ephemeris_math.fit_linear_ephemeris.

Locks in correctness for the weighted/unweighted linear ephemeris fit that
used to live inline in web.py's api_ephemeris_calculate route handler (see
docs/architecture_audit.md, finding H2).
"""

from __future__ import annotations

import pytest

from muscat_db.ephemeris_math import (
    assign_epoch,
    fit_linear_ephemeris,
    is_sigma_outlier,
)


def test_fit_recovers_known_period_and_t0_unweighted():
    """A synthetic, noise-free linear series must be recovered exactly."""
    t0_true = 2460000.123456
    period_true = 3.14159
    epochs = list(range(-5, 6))  # 11 points, includes E=0
    tcs = [t0_true + e * period_true for e in epochs]
    uncs = [0.001] * len(epochs)

    result = fit_linear_ephemeris(epochs, tcs, uncs, t0_ref=0.0, period_ref=1.0, fit_method="unweighted")

    assert result["was_fit"] is True
    assert result["fit_method"] == "unweighted"
    assert result["t0_fit"] == pytest.approx(t0_true)
    assert result["period_fit"] == pytest.approx(period_true)
    # A perfect line has zero residual variance -> zero-width uncertainties.
    assert result["t0_fit_unc"] == pytest.approx(0.0, abs=1e-9)
    assert result["period_fit_unc"] == pytest.approx(0.0, abs=1e-9)
    assert result["E_center"] == 0  # symmetric epoch range centers on 0


def test_fit_recovers_known_period_and_t0_weighted():
    """Same synthetic series, weighted mode: still recovers the true line."""
    t0_true = 2459500.5
    period_true = 1.2345
    epochs = [0, 10, 20, 30, 40]
    tcs = [t0_true + e * period_true for e in epochs]
    uncs = [0.01, 0.02, 0.01, 0.03, 0.01]

    result = fit_linear_ephemeris(epochs, tcs, uncs, t0_ref=0.0, period_ref=1.0, fit_method="weighted")

    assert result["was_fit"] is True
    assert result["fit_method"] == "weighted"
    assert result["t0_fit"] == pytest.approx(t0_true)
    assert result["period_fit"] == pytest.approx(period_true)
    assert result["t0_fit_unc"] > 0.0
    assert result["period_fit_unc"] > 0.0


def test_fit_falls_back_to_reference_with_fewer_than_two_points():
    """With 0 or 1 usable points, no fit is attempted and the reference
    ephemeris (t0_ref/period_ref) is echoed back unchanged with zero
    uncertainty -- matching the historical inline behavior exactly."""
    t0_ref = 2450000.0
    period_ref = 5.0

    empty = fit_linear_ephemeris([], [], [], t0_ref, period_ref)
    assert empty["was_fit"] is False
    assert empty["fit_method"] == "none"
    assert empty["t0_fit"] == t0_ref
    assert empty["period_fit"] == period_ref
    assert empty["t0_fit_unc"] == 0.0
    assert empty["period_fit_unc"] == 0.0
    assert empty["t0_fit_centered"] == t0_ref
    assert empty["t0_fit_centered_unc"] == 0.0
    assert empty["E_center"] == 0

    single = fit_linear_ephemeris([3], [2451500.0], [0.01], t0_ref, period_ref)
    assert single["was_fit"] is False
    assert single["t0_fit"] == t0_ref
    assert single["period_fit"] == period_ref


def test_fit_two_points_is_the_minimum_that_succeeds():
    """Exactly 2 points is the minimum for a determined line fit; the fit
    passes through both points exactly (zero residual by construction)."""
    t0_ref = 2450000.0
    period_ref = 1.0
    epochs = [0, 7]
    tcs = [2458000.0, 2458000.0 + 7 * 2.5]
    uncs = [0.005, 0.005]

    result = fit_linear_ephemeris(epochs, tcs, uncs, t0_ref, period_ref, fit_method="unweighted")

    assert result["was_fit"] is True
    assert result["period_fit"] == pytest.approx(2.5)
    assert result["t0_fit"] == pytest.approx(2458000.0)
    # Only 2 points -> zero degrees of freedom for the unweighted residual
    # variance estimate, so the historical implementation defines sigma_sq=0.0
    # (dof <= 0 guard) and the reported uncertainties are exactly zero.
    assert result["t0_fit_unc"] == 0.0
    assert result["period_fit_unc"] == 0.0


def test_fit_unweighted_accepts_zero_uncertainty_inputs():
    """Unweighted mode never divides by ``uncs`` (weights are uniform and the
    uncertainty estimate comes from residual variance), so zero-uncertainty
    inputs must not raise -- unlike weighted mode, which divides by
    ``unc**2`` and is expected to raise on a zero input (callers filter those
    points out before calling, per the docstring's precondition)."""
    epochs = [0, 1, 2, 3]
    tcs = [100.0, 101.0, 102.0, 103.0]
    uncs = [0.0, 0.0, 0.0, 0.0]

    result = fit_linear_ephemeris(epochs, tcs, uncs, t0_ref=0.0, period_ref=1.0, fit_method="unweighted")

    assert result["was_fit"] is True
    assert result["period_fit"] == pytest.approx(1.0)
    assert result["t0_fit"] == pytest.approx(100.0)


# ---------------------------------------------------------------------------
# assign_epoch / is_sigma_outlier -- helpers used to place and sanity-check
# manually entered transit centers on the O-C plot (Reading-2 feature).
# ---------------------------------------------------------------------------


def test_assign_epoch_rounds_to_nearest_integer():
    """E = round((tc - t0) / P), matching the historical inline
    ``int(round(...))`` used for database transit centers."""
    t0 = 2458000.0
    period = 2.5
    assert assign_epoch(2458000.0, t0, period) == 0
    assert assign_epoch(2458000.0 + 2.5 * 7, t0, period) == 7
    assert assign_epoch(2458000.0 - 2.5 * 3, t0, period) == -3
    # A small timing deviation (a would-be TTV) stays on its integer epoch.
    assert assign_epoch(2458000.0 + 2.5 * 7 + 0.4, t0, period) == 7


def test_assign_epoch_matches_inline_formula_for_dense_range():
    """Exhaustively agree with the exact expression the endpoint used."""
    t0 = 2459123.456
    period = 3.9
    for e in range(-50, 51):
        tc = t0 + e * period
        assert assign_epoch(tc, t0, period) == int(round((tc - t0) / period))


def test_is_sigma_outlier_flags_only_beyond_threshold():
    """|O-C| > n_sigma * unc, sign-independent."""
    assert is_sigma_outlier(0.004, 0.001) is False   # 4 sigma -> within
    assert is_sigma_outlier(0.006, 0.001) is True     # 6 sigma -> outlier
    assert is_sigma_outlier(-0.006, 0.001) is True    # magnitude only
    # Exactly 5 sigma is the boundary and is NOT flagged (strict > ).
    assert is_sigma_outlier(0.005, 0.001) is False


def test_is_sigma_outlier_requires_positive_sigma_scale():
    """A non-positive or missing uncertainty has no sigma scale, so nothing
    can be declared an outlier (weighting requires unc > 0 anyway)."""
    assert is_sigma_outlier(1.0, 0.0) is False
    assert is_sigma_outlier(1.0, -0.5) is False
    assert is_sigma_outlier(1.0, None) is False


def test_is_sigma_outlier_custom_threshold():
    assert is_sigma_outlier(0.0025, 0.001, n_sigma=2.0) is True
    assert is_sigma_outlier(0.0015, 0.001, n_sigma=2.0) is False
