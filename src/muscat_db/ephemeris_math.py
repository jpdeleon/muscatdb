"""Linear ephemeris (transit-timing O-C) least-squares fitting.

Extracted from the ``api_ephemeris_calculate`` route handler in ``web.py``
(see docs/architecture_audit.md, finding H2) so the weighted/unweighted
least-squares fit, its variance propagation, and the epoch-centering trick
are independently unit-testable instead of living inline in a FastAPI route.

This module is output-identical to the historical inline implementation: same
formulas, same edge-case behavior (fewer than 2 usable points, or a singular
normal-equations matrix, both fall back to the reference ephemeris
unchanged). It intentionally does not introduce numpy/astropy so floating
point results match the prior implementation bit-for-bit.
"""

from __future__ import annotations

from typing import Sequence, TypedDict


class EphemerisFit(TypedDict):
    """Result of :func:`fit_linear_ephemeris`.

    ``t0_fit``/``period_fit`` are extrapolated back to the catalog epoch
    (E=0); ``t0_fit_centered``/``t0_fit_centered_unc`` are the fit evaluated
    at the centered epoch ``E_center`` (numerically the best-determined
    point on the line, useful for display). When ``was_fit`` is False, all
    "_fit" fields equal the reference ``t0_ref``/``period_ref`` passed in and
    the uncertainties are 0.0.
    """

    was_fit: bool
    fit_method: str
    t0_fit: float
    t0_fit_unc: float
    period_fit: float
    period_fit_unc: float
    t0_fit_centered: float
    t0_fit_centered_unc: float
    E_center: int


def assign_epoch(tc: float, t0: float, period: float) -> int:
    """Nearest integer transit epoch for a transit center ``tc``.

    ``E = round((tc - t0) / P)``, matching the epoch assignment historically
    done inline in ``web.api_ephemeris_calculate`` (``int(round(...))``, i.e.
    Python's round-half-to-even). Used both for database transit centers and
    for manually entered ones so they land on the same epoch grid. ``period``
    must be non-zero.
    """
    return int(round((tc - t0) / period))


def is_sigma_outlier(oc_days: float, unc: float | None, n_sigma: float = 5.0) -> bool:
    """True when an O-C residual exceeds ``n_sigma`` times its own uncertainty.

    ``|O-C| > n_sigma * unc`` (strict; exactly ``n_sigma`` is not flagged),
    magnitude only. Used to flag a manually entered transit center whose
    deviation from the fitted linear ephemeris is too large to be a plausible
    transit-timing variation -- typically a data-entry error such as a wrong
    epoch alias, a JD-vs-BJD offset, or a UTC-vs-TDB time-system mismatch.

    Returns False for a missing or non-positive ``unc`` (no meaningful sigma
    scale); manual-point weighting requires ``unc > 0`` anyway.
    """
    if unc is None or unc <= 0:
        return False
    return abs(oc_days) > n_sigma * unc


def fit_linear_ephemeris(
    epochs: Sequence[int],
    tcs: Sequence[float],
    uncs: Sequence[float],
    t0_ref: float,
    period_ref: float,
    fit_method: str = "unweighted",
) -> EphemerisFit:
    """Fit a linear ephemeris ``T(E) = t0 + E * P`` to transit-center points.

    ``epochs``, ``tcs``, and ``uncs`` must already be filtered to the points
    that should participate in the regression (equal length; for
    ``fit_method="weighted"`` every ``uncs[i]`` must be > 0, since the weight
    is ``1 / uncs[i]**2``). Selecting which points are eligible (e.g. a
    "checked" flag from job bookkeeping) is caller business logic, not part
    of this pure-math function.

    With fewer than 2 points, or a singular normal-equations matrix (e.g. all
    epochs identical), no fit is attempted: ``was_fit`` is False and the
    ``t0_ref``/``period_ref`` reference ephemeris is echoed back unchanged
    with zero uncertainty.

    ``fit_method``:

    - ``"weighted"``: inverse-variance weights (``1 / unc**2``); the
      T0/period uncertainties come directly from the weighted normal-equations
      covariance.
    - ``"unweighted"`` (default): uniform weights; the T0/period uncertainties
      are derived from the residual variance (``sum((tc - fit)**2) / dof``)
      propagated through the same covariance structure. ``uncs`` is unused in
      this mode other than being accepted for a uniform call signature.

    Epochs are centered at their mid-range value (``E_center``) before the
    regression -- this keeps the design matrix well-conditioned -- then the
    fit is extrapolated back to the catalog epoch (E=0), propagating the
    T0/period covariance term through the extrapolation.

    Returns an :class:`EphemerisFit` dict (JSON-serializable) with the same
    keys and rounding-free values previously computed inline in
    ``web.api_ephemeris_calculate``; the route handler applies its own
    ``round()`` calls when building the JSON response, unchanged.
    """
    n = len(epochs)
    result: EphemerisFit = {
        "was_fit": False,
        "fit_method": "none",
        "t0_fit": t0_ref,
        "t0_fit_unc": 0.0,
        "period_fit": period_ref,
        "period_fit_unc": 0.0,
        "t0_fit_centered": t0_ref,
        "t0_fit_centered_unc": 0.0,
        "E_center": 0,
    }
    if n < 2:
        return result

    e_min = min(epochs)
    e_max = max(epochs)
    e_center = e_min + int((e_max - e_min) // 2)

    sw = swx = swy = swxx = swxy = 0.0
    for epoch, tc, unc in zip(epochs, tcs, uncs):
        x = epoch - e_center
        y = tc
        w = 1.0 / (unc ** 2) if fit_method == "weighted" else 1.0
        sw += w
        swx += w * x
        swy += w * y
        swxx += w * (x ** 2)
        swxy += w * x * y

    delta = sw * swxx - (swx ** 2)
    if delta <= 0.0:
        result["E_center"] = e_center
        return result

    t0_centered = (swxx * swy - swx * swxy) / delta
    period_fit = (sw * swxy - swx * swy) / delta

    if fit_method == "weighted":
        t0_centered_unc = (swxx / delta) ** 0.5
        period_fit_unc = (sw / delta) ** 0.5
        sigma_sq = None
    else:
        residuals_sum_sq = sum(
            (tc - (t0_centered + (epoch - e_center) * period_fit)) ** 2
            for epoch, tc in zip(epochs, tcs)
        )
        dof = n - 2
        sigma_sq = residuals_sum_sq / dof if dof > 0 else 0.0
        t0_centered_unc = (sigma_sq * swxx / delta) ** 0.5
        period_fit_unc = (sigma_sq * sw / delta) ** 0.5

    t0_fit = t0_centered - e_center * period_fit

    # Var(t0_fit) = Var(t0_centered) + E_center^2 * Var(P) - 2*E_center*Cov(t0_centered, P)
    var_t0_factor = swxx + (e_center ** 2) * sw + 2.0 * e_center * swx
    if fit_method == "weighted":
        t0_fit_unc = (var_t0_factor / delta) ** 0.5
    else:
        t0_fit_unc = (sigma_sq * var_t0_factor / delta) ** 0.5

    return {
        "was_fit": True,
        "fit_method": fit_method,
        "t0_fit": t0_fit,
        "t0_fit_unc": t0_fit_unc,
        "period_fit": period_fit,
        "period_fit_unc": period_fit_unc,
        "t0_fit_centered": t0_centered,
        "t0_fit_centered_unc": t0_centered_unc,
        "E_center": e_center,
    }
