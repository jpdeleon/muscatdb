#!/usr/bin/env python3
"""
Test exposure time calculator predictions against real MuSCAT3 observations.

This script compares the predicted peak ADU (from the exposure calculator) against
the actual measured peak ADU from FITS frames to validate the calculator's accuracy.

Key Questions:
1. How well do predictions match reality?
2. Are there systematic offsets per band?
3. How does airmass affect the accuracy?
4. Are there outliers or problematic observations?

Peak Count Measurement:
- Samples 10 equally-spaced frames from each observation dataset
- Measures peak from each frame using 99.9th percentile minus median baseline
- Returns median peak across the 10 samples for robust measurement
- This reduces noise from cosmic rays and frame-to-frame seeing variations
"""

from __future__ import annotations

import sys
import logging
import csv
import glob
from pathlib import Path
from typing import NamedTuple
import sqlite3

import numpy as np
from astropy.io import fits

# Setup paths
PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from muscat_db.database import db_path
from muscat_db.exposure import (
    calc_peak,
    lookup_magnitudes,
    load_coeffs,
)
from muscat_db.photometry import raw_data_dir

logger = logging.getLogger(__name__)
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s"
)


class PeakComparison(NamedTuple):
    """Single observation comparison result."""
    instrument: str
    obsdate: str
    object_name: str
    band: str
    filter: str
    magnitude: float | None
    exptime: float
    airmass: float
    focus_mm: float
    observed_peak: float | None
    predicted_peak: float
    error_pct: float | None
    status: str  # 'ok', 'no_peak', 'no_magnitude', 'saturated'


def measure_peak_from_frame(data: np.ndarray) -> float | None:
    """Measure peak pixel value from frame data.

    Uses 99.9th percentile minus median (baseline) - robust against cosmic rays.
    """
    if data is None or data.ndim < 2 or data.size == 0:
        return None

    try:
        baseline = np.median(data)
        peak = np.percentile(data, 99.9) - baseline
        return max(0.0, float(peak))
    except Exception as e:
        logger.debug(f"Failed to measure peak: {e}")
        return None


def measure_peak_from_dataset(
    obsdate: str,
    filename_base: str,
    instrument: str = "muscat3",
    n_samples: int = 10,
) -> float | None:
    """Measure peak from multiple equally-spaced frames in a dataset.

    Samples n_samples frames evenly distributed across the observation sequence,
    then returns the median peak to reduce noise from cosmic rays and frame-to-frame variations.

    Args:
        obsdate: Observation date (YYMMDD)
        filename_base: Base filename without extension or frame number
        instrument: Instrument name
        n_samples: Number of equally-spaced frames to sample

    Returns:
        Median peak across sampled frames, or None if measurement fails
    """
    raw_dir = raw_data_dir(instrument, obsdate)

    # Find all frames matching this dataset
    # MuSCAT3 frame numbering: filename is like "ogg2m001-ep02-20201015-0083-e91"
    # We'll look for files that start with the base and have incrementing numbers
    pattern = str(raw_dir / f"{filename_base}*")
    matching_files = sorted(glob.glob(pattern))

    if not matching_files:
        logger.debug(f"No frames found matching {pattern}")
        return None

    # Select equally-spaced frames
    if len(matching_files) <= n_samples:
        frame_indices = list(range(len(matching_files)))
    else:
        frame_indices = [
            int(i * (len(matching_files) - 1) / (n_samples - 1))
            for i in range(n_samples)
        ]

    peaks = []
    for idx in frame_indices:
        fits_path = matching_files[idx]
        try:
            with fits.open(fits_path, memmap=False) as hdul:
                data = hdul[0].data
                if data is None:
                    # Try science extension
                    for hdu in hdul[1:]:
                        if hdu.data is not None and hdu.data.ndim >= 2:
                            data = hdu.data
                            break

                peak = measure_peak_from_frame(data)
                if peak is not None:
                    peaks.append(peak)
        except Exception as e:
            logger.debug(f"Failed to read frame {fits_path}: {e}")
            continue

    if not peaks:
        return None

    # Return median of sampled peaks
    median_peak = float(np.median(peaks))
    if len(peaks) > 1:
        logger.debug(
            f"Measured {len(peaks)} frames: peaks {[f'{p:.0f}' for p in peaks]}, "
            f"median={median_peak:.0f}"
        )
    return median_peak


def get_frames_from_db(
    instrument: str = "muscat3",
    limit: int | None = None,
    object_filter: str | None = None,
) -> list[dict]:
    """Get frame data from the database.

    Args:
        instrument: Instrument filter (default: muscat3)
        limit: Maximum number of frames to return
        object_filter: Object name substring filter

    Returns:
        List of frame dictionaries
    """
    conn = sqlite3.connect(db_path())
    conn.row_factory = sqlite3.Row

    query = """
        SELECT
            instrument, obsdate, ccd, filename, object,
            exptime, filter, airmass, focus, ra, declination
        FROM frames
        WHERE instrument = ? AND filter IS NOT NULL AND object IS NOT NULL
    """
    params = [instrument]

    if object_filter:
        query += " AND LOWER(object) LIKE LOWER(?)"
        params.append(f"%{object_filter}%")

    query += " ORDER BY obsdate DESC"

    if limit:
        query += f" LIMIT {limit}"

    cursor = conn.execute(query, params)
    frames = [dict(row) for row in cursor.fetchall()]
    conn.close()

    return frames


def _band_from_filter(filter_str: str) -> str | None:
    """Map filter string to band name."""
    if not filter_str:
        return None
    f = filter_str.lower()

    mapping = {
        "g": "gp", "g-band": "gp",
        "r": "rp", "r-band": "rp",
        "i": "ip", "i-band": "ip",
        "z": "zs", "z-band": "zs",
        "g_narrow": "g_narrow",
        "r_narrow": "r_narrow",
        "i_narrow": "i_narrow",
        "z_narrow": "z_narrow",
        "na_d": "Na_D",
    }

    for key, band in mapping.items():
        if key in f:
            return band
    return None


def _parse_sexagesimal(coord_str: str | None, is_ra: bool = True) -> float | None:
    """Parse sexagesimal coordinate string to decimal degrees.

    Args:
        coord_str: Coordinate in format 'hh:mm:ss.s' (RA) or 'dd:mm:ss.s' (Dec)
        is_ra: True for RA (multiply by 15), False for Dec

    Returns:
        Decimal degrees or None if invalid
    """
    if not coord_str:
        return None

    try:
        parts = str(coord_str).strip().split(":")
        if len(parts) != 3:
            return None

        h_or_d = float(parts[0])
        m = float(parts[1])
        s = float(parts[2])

        result = h_or_d + m / 60.0 + s / 3600.0

        # RA is in hours, needs to be multiplied by 15 to get degrees
        if is_ra:
            result *= 15.0

        # Handle negative declinations
        if coord_str.strip().startswith("-"):
            result = -abs(result)
        return result
    except (ValueError, AttributeError):
        return None


def test_observations(
    instrument: str = "muscat3",
    max_frames: int = 100,
    object_name: str | None = None,
) -> list[dict]:
    """Test exposure predictions against real observations.

    Args:
        instrument: Instrument to test
        max_frames: Maximum number of frames to process
        object_name: Optional target name filter

    Returns:
        List of comparison result dictionaries
    """
    print(f"\n{'='*80}")
    print(f"Testing {instrument} exposure predictions")
    print(f"{'='*80}\n")

    try:
        frames = get_frames_from_db(instrument, limit=max_frames, object_filter=object_name)
    except Exception as exc:  # e.g. sqlite3.OperationalError: no such table: frames
        import pytest

        pytest.skip(f"obslog DB unavailable ({exc}); skipping off-host/CI")
    if not frames:
        import pytest

        pytest.skip("no frames in obslog DB; skipping off-host/CI")
    print(f"Found {len(frames)} frames to test\n")

    results = []
    load_coeffs(instrument)

    for i, frame in enumerate(frames, 1):
        if i % 20 == 0:
            print(f"Processing frame {i}/{len(frames)}...")

        # Extract frame info
        obsdate = frame["obsdate"]
        filename = frame["filename"]
        band = _band_from_filter(frame["filter"])

        if band is None:
            continue

        # Measure peak from multiple frames in the dataset
        # This samples 10 equally-spaced frames to get a robust measurement
        observed_peak = measure_peak_from_dataset(obsdate, filename, instrument)

        # Look up magnitude (parse RA/Dec from sexagesimal format)
        ra = _parse_sexagesimal(frame["ra"], is_ra=True)
        dec = _parse_sexagesimal(frame["declination"], is_ra=False)

        if ra is not None and dec is not None:
            try:
                mags = lookup_magnitudes(ra, dec, radius_arcsec=3.0)
                magnitude = mags.get(band) if mags else None
            except (ValueError, TypeError, Exception):
                magnitude = None
        else:
            magnitude = None

        # Get observation parameters
        exptime = frame["exptime"] or 0
        airmass = frame["airmass"] or 1.1
        focus_mm = frame["focus"] or 0.0

        # Predict peak
        if magnitude and exptime > 0:
            try:
                result = calc_peak(
                    instrument=instrument,
                    band=band,
                    mag=magnitude,
                    focus_mm=focus_mm,
                    exptime=exptime,
                    airmass=airmass,
                )
                predicted_peak = result.get("peak_adu", 0)
            except Exception as e:
                logger.debug(f"Failed to predict peak: {e}")
                predicted_peak = 0
        else:
            predicted_peak = 0

        # Compute error
        if observed_peak and predicted_peak:
            error_pct = (observed_peak - predicted_peak) / predicted_peak * 100
            is_saturated = observed_peak > 0.9 * 99000  # ~90% of full well
            status = "saturated" if is_saturated else "ok"
        else:
            error_pct = None
            status = "no_peak" if not observed_peak else "no_magnitude"

        # Store result
        comparison = PeakComparison(
            instrument=instrument,
            obsdate=obsdate,
            object_name=frame.get("object", "unknown"),
            band=band,
            filter=frame.get("filter", "unknown"),
            magnitude=magnitude,
            exptime=exptime,
            airmass=airmass,
            focus_mm=focus_mm,
            observed_peak=observed_peak,
            predicted_peak=predicted_peak,
            error_pct=error_pct,
            status=status,
        )
        results.append(comparison._asdict())

    return results


def analyze_results(results: list[dict]) -> None:
    """Analyze and print comparison statistics."""
    if not results:
        print("No results to analyze")
        return

    print(f"\n{'='*80}")
    print("ANALYSIS SUMMARY")
    print(f"{'='*80}\n")

    # Overall statistics
    valid = [r for r in results if r["status"] == "ok" and r["error_pct"] is not None]
    if valid:
        errors = [r["error_pct"] for r in valid]
        print(f"✓ Valid predictions: {len(valid)}/{len(results)}")
        print(f"  Mean error: {np.mean(errors):+.1f}%")
        print(f"  Std dev:    {np.std(errors):.1f}%")
        print(f"  Min error:  {np.min(errors):+.1f}%")
        print(f"  Max error:  {np.max(errors):+.1f}%")
        print(f"  Median:     {np.median(errors):+.1f}%")

    # Per-band statistics
    print(f"\n{'Band':<8} {'N':<6} {'Mean Error':<15} {'Std Dev':<12} {'Range':<25}")
    print("-" * 66)
    bands = sorted(set(r["band"] for r in results))
    for band in bands:
        band_data = [r for r in valid if r["band"] == band]
        if band_data:
            errors = [r["error_pct"] for r in band_data]
            mean_err = np.mean(errors)
            std_err = np.std(errors)
            min_err = np.min(errors)
            max_err = np.max(errors)
            print(f"{band:<8} {len(band_data):<6} {mean_err:>+.1f}%{'':<9} {std_err:>6.1f}% {min_err:>+7.1f}% to {max_err:>+7.1f}%")

    # Per-airmass statistics
    print(f"\n{'Airmass':<10} {'N':<6} {'Mean Error':<15} {'Range':<25}")
    print("-" * 56)
    for am_bin in [1.0, 1.2, 1.5, 1.8, 2.0]:
        am_data = [
            r for r in valid
            if r["airmass"] >= am_bin - 0.1 and r["airmass"] < am_bin + 0.1
        ]
        if am_data:
            errors = [r["error_pct"] for r in am_data]
            mean_err = np.mean(errors)
            min_err = np.min(errors)
            max_err = np.max(errors)
            print(f"{am_bin:>6.1f} ±0.1 {len(am_data):<6} {mean_err:>+.1f}%{'':<9} {min_err:>+7.1f}% to {max_err:>+7.1f}%")

    # Status breakdown
    print("\nStatus Breakdown:")
    statuses = set(r["status"] for r in results)
    for status in sorted(statuses):
        count = len([r for r in results if r["status"] == status])
        pct = count / len(results) * 100
        print(f"  {status:<15}: {count:>4} ({pct:>5.1f}%)")

    # Outliers (>30% error)
    outliers = [r for r in valid if abs(r["error_pct"]) > 30]
    if outliers:
        print(f"\n⚠️  Outliers (>30% error): {len(outliers)}")
        print(f"{'Object':<20} {'Band':<6} {'Error':<12} {'Obs':<12} {'Pred':<12} {'Mag':<8}")
        print("-" * 80)
        for row in sorted(outliers, key=lambda x: abs(x["error_pct"]), reverse=True)[:10]:
            mag_str = f"{row['magnitude']:.2f}" if row["magnitude"] else "N/A"
            obs_str = f"{row['observed_peak']:.0f}" if row["observed_peak"] else "—"
            pred_str = f"{row['predicted_peak']:.0f}" if row["predicted_peak"] else "—"
            print(f"{str(row['object_name'])[:20]:<20} {row['band']:<6} {row['error_pct']:>+10.1f}% {obs_str:>12} {pred_str:>12} {mag_str:>6}")


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--instrument", default="muscat3", help="Instrument to test")
    parser.add_argument("--max-frames", type=int, default=100, help="Maximum frames to process")
    parser.add_argument("--object", help="Optional target name filter")
    parser.add_argument("--output", help="Save results to CSV")

    args = parser.parse_args()

    # Run tests
    results = test_observations(
        instrument=args.instrument,
        max_frames=args.max_frames,
        object_name=args.object,
    )

    # Analyze
    analyze_results(results)

    # Save if requested
    if args.output:
        if results:
            with open(args.output, 'w', newline='') as f:
                writer = csv.DictWriter(f, fieldnames=results[0].keys())
                writer.writeheader()
                writer.writerows(results)
            print(f"\n✓ Results saved to {args.output}")
        else:
            print("No results to save")
