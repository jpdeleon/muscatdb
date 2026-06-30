#!/usr/bin/env python
"""Legacy exposure time calculator for MuSCAT3 observations.

DEPRECATED: Use `muscat_db.exposure.calc_exptime()` instead.

This module provides empirical exposure time estimation based on
https://github.com/akihikofukui/peak_count_estimator

**LIMITATIONS:**
- Fixed airmass at 1.1 (no atmospheric extinction correction)
- Empirical coefficients from MuSCAT3 only
- Does not account for seeing variations beyond PSF focus calibration

The modern exposure calculator in `muscat_db.exposure` adds:
- Airmass-dependent atmospheric extinction
- Database-calibrated coefficients per instrument
- Focus interpolation for intermediate values
- Narrowband filter width corrections

**Migration Guide:**
```python
# Old (legacy):
from scripts.exp_time_calculator_legacy import exposure_time_calculator
exp_times = exposure_time_calculator(
    {"gp": 12.5, "rp": 12.0, "ip": 11.8, "zs": 11.5},
    f=0, peak=40000, narrow=False
)

# New (recommended):
from muscat_db.exposure import calc_exptime
for band, mag in {"gp": 12.5, "rp": 12.0, "ip": 11.8, "zs": 11.5}.items():
    result = calc_exptime(
        instrument="muscat3",
        band=band,
        mag=mag,
        focus_mm=0.0,
        target_adu=40000,
        airmass=1.1,
    )
    print(f"{band}: {result['exptime']:.1f}s")
```
"""

from __future__ import annotations

import warnings
from dataclasses import dataclass
from typing import TypedDict

import numpy as np


# ============================================================================
# Empirical coefficients from peak_count_estimator
# ============================================================================
# These are log10(peak_ADU) for the reference conditions:
# - magnitude = 0 (used as zero-point)
# - exposure time = 60s
# - airmass = 1.1
# - seeing = 0.8"
# Formula: log10(peak_ADU) = coef - 0.4*mag + log10(exp/60)


@dataclass(frozen=True)
class BandData:
    """Empirical coefficients and PSF data for a single filter band."""

    name: str
    coef: np.ndarray
    fwhm_pix: np.ndarray
    gain: float
    narrow_scale: float = 1.0


# MuSCAT3 empirical data (per-focus coefficients and FWHM in pixels)
MUSCAT3_BANDS: dict[str, BandData] = {
    "gp": BandData(
        name="g (Pan-STARRS)",
        coef=np.array([10.51276637, 10.2636757, 9.99203702, 9.72257331,
                       9.51637384, 9.44039911, 9.28810117]),
        fwhm_pix=np.array([3.0, 3.95666667, 5.91166667, 9.135,
                           12.93333333, 15.49833333, 19.60166667]),
        gain=1.9,
        narrow_scale=0.34,  # g_narrow ~10nm vs gp ~140nm
    ),
    "rp": BandData(
        name="r (Pan-STARRS)",
        coef=np.array([10.509745, 10.28773524, 10.23136096, 9.9569031,
                       9.64339266, 9.53424511, 9.35282711]),
        fwhm_pix=np.array([3.0, 3.88833333, 4.19666667, 6.24833333,
                           10.24666667, 12.75666667, 16.68833333]),
        gain=1.88,
        narrow_scale=1.0 / 30,  # r_narrow ~10nm vs rp ~100nm
    ),
    "ip": BandData(
        name="i (Pan-STARRS)",
        coef=np.array([10.24247762, 10.08295809, 9.88549464, 9.57156006,
                       9.30655855, 9.24200499, 9.05599536]),
        fwhm_pix=np.array([3.0, 3.50166667, 4.62666667, 7.545,
                           11.53833333, 13.89833333, 17.99333333]),
        gain=1.8,
        narrow_scale=0.29,  # i_narrow ~5nm vs ip ~100nm
    ),
    "zs": BandData(
        name="z (SkyMapper)",
        coef=np.array([9.84187978, 9.67468481, 9.55641629, 9.25645003,
                       8.966079, 8.89227852, 8.70435107]),
        fwhm_pix=np.array([3.0, 3.6, 4.36333333, 6.81333333,
                           10.76, 13.35333333, 17.45]),
        gain=2.0,
        narrow_scale=0.46,  # z_narrow ~5nm vs zs ~100nm
    ),
}


class ExposureTimeResult(TypedDict):
    """Exposure time calculation result."""

    band: str
    mag: float
    exptime: float  # seconds
    fwhm_pix: float
    narrow: bool


def exposure_time_calculator(
    mags: dict[str, float],
    f: int,
    peak: float = 40000,
    narrow: bool = False,
) -> dict[str, float]:
    """Estimate exposure times for target magnitudes (LEGACY, airmass=1.1 only).

    DEPRECATED: Use `muscat_db.exposure.calc_exptime()` for better accuracy.

    Computes exposure time needed to reach a target peak ADU for each band,
    assuming fixed airmass=1.1 and natural seeing~0.8". Does not account for
    atmospheric extinction or realistic observing conditions.

    **Formula:**
    ```
    log10(peak_ADU) = coef[f] - 0.4*mag + log10(exp/60)
    → exp = target_adu * 60 / 10^(coef[f] - 0.4*mag)
    ```

    Args:
        mags: dict of band→magnitude pairs (e.g., {"gp": 12.5, "rp": 12.0, ...})
        f: focus index (0-6 mm), maps to coef array index
        peak: target peak ADU (default 40000 for MuSCAT3 full well ~99000)
        narrow: if True, apply narrowband filter correction (reduces light collection)

    Returns:
        dict of band→exposure_time_seconds

    Raises:
        KeyError: if band not found in MUSCAT3_BANDS
        IndexError: if focus index out of range [0, 6]

    Example:
        >>> exp_times = exposure_time_calculator(
        ...     {"gp": 12.5, "rp": 12.0, "ip": 11.8, "zs": 11.5},
        ...     f=0, peak=40000, narrow=False
        ... )
        >>> print(exp_times["rp"])
        4.7
    """
    warnings.warn(
        "exposure_time_calculator() is deprecated and assumes airmass=1.1. "
        "Use muscat_db.exposure.calc_exptime() for better accuracy including "
        "atmospheric extinction and database calibrations.",
        DeprecationWarning,
        stacklevel=2,
    )

    if not 0 <= f <= 6:
        raise IndexError(f"Focus index must be 0-6, got {f}")

    exp_times: dict[str, float] = {}

    # Iterate in order: gp, rp, ip, zs (to match original band indices)
    for band_idx, band_name in enumerate(["gp", "rp", "ip", "zs"]):
        if band_name not in mags:
            continue

        mag = mags[band_name]
        band_data = MUSCAT3_BANDS[band_name]

        # Get coefficient at this focus index
        coef = band_data.coef[f]

        # Z-band has a special case in the original formula (uses mag-1 instead of mag)
        # This is equivalent to adding 0.4 to the effective coefficient
        if band_name == "zs":
            mag_eff = mag - 1.0
        else:
            mag_eff = mag

        # Compute log10(peak_ADU) at reference exposure (60s)
        logpeak = coef - 0.4 * mag_eff

        # Apply narrowband correction if requested
        if narrow:
            narrow_peak = 10**logpeak * band_data.narrow_scale
            logpeak = np.log10(narrow_peak)

        # Solve for exposure time: peak = 10^coef * 10^(-0.4*mag_eff) * exp/60
        exptime = 60.0 * peak / (10.0**logpeak)
        exp_times[band_name] = round(float(exptime), 1)

    return exp_times


def print_exposure_times(
    mags: dict[str, float],
    f: int,
    peak: float = 40000,
    narrow: bool = False,
) -> None:
    """Print exposure times in a readable table (convenience function).

    Args:
        mags: dict of band→magnitude pairs
        f: focus index (0-6)
        peak: target peak ADU
        narrow: apply narrowband correction
    """
    exp_times = exposure_time_calculator(mags, f, peak, narrow)

    filter_type = "narrowband" if narrow else "broadband"
    print(f"\n{'Band':<6} {'Mag':<8} {'Exp Time':<12} Filter")
    print("-" * 40)

    for band in ["gp", "rp", "ip", "zs"]:
        if band in exp_times:
            mag = mags.get(band, "—")
            exp = exp_times[band]
            print(f"{band:<6} {mag:<8.2f} {exp:<12.1f}s {filter_type}")


if __name__ == "__main__":
    # Example usage
    example_mags = {"gp": 12.5, "rp": 12.0, "ip": 11.8, "zs": 11.5}

    print("=" * 60)
    print("LEGACY EXPOSURE TIME CALCULATOR - MuSCAT3")
    print("=" * 60)
    print("\n⚠️  This script is deprecated. See module docstring for migration.")

    for focus_mm in [0, 2, 4]:
        print(f"\n📍 Focus: {focus_mm} mm (index {focus_mm})")
        print_exposure_times(example_mags, f=focus_mm, peak=40000, narrow=False)