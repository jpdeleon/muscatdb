#!/usr/bin/env python3
"""Compare exposure-calculator peak predictions with stars in test observations.

The script is intended for short ``test`` observations, where several exposure
times, focus positions, or airmasses were tried.  It does not modify the
database or FITS files.  For every observation group it:

* reads the observation metadata from ``frames``;
* finds Gaia stars in the FITS WCS footprint;
* measures a local, background-subtracted peak for each star in sampled FITS
  frames; and
* predicts the peak with :func:`muscat_db.exposure.calc_peak`.

The output is one CSV row per star and observation group.  Magnitudes are
looked up in the same Pan-STARRS/SkyMapper plus Gaia fallback path used by the
web exposure calculator.  Network catalogue lookups are cached in this process.

Example::

    uv run python scripts/compare_observed_peaks.py \
        --target TOI6109 --instrument sinistro --instrument muscat3 \
        --output "$HOME/temp/toi6109_peak_comparison.csv"

Only muscat3 and sinistro are enabled by default because their BANZAI products
carry WCS.  ``--fits-root`` can be used when the standard data directories are
not mounted.
"""

from __future__ import annotations

import argparse
import csv
import glob
import logging
import sqlite3
import sys
from collections import defaultdict
from pathlib import Path

import numpy as np
from astropy.io import fits
from astropy.wcs import WCS

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from muscat_db.database import db_path
from muscat_db.exposure import calc_peak, lookup_magnitudes_with_fallback
from muscat_db.fov import cached_query_gaia_field
from muscat_db.photometry import raw_data_dir

LOG = logging.getLogger("compare_observed_peaks")
DEFAULT_INSTRUMENTS = ("sinistro", "muscat3")
DEFAULT_MAX_FRAMES = 200
DEFAULT_SAMPLES = 10
DEFAULT_MAX_STARS = 30


def parse_sexagesimal(value: str | None, is_ra: bool) -> float | None:
    """Return decimal degrees from an RA/Dec database value."""
    if not value:
        return None
    try:
        parts = str(value).strip().split(":")
        if len(parts) != 3:
            return float(value)
        sign = -1.0 if str(value).strip().startswith("-") else 1.0
        result = abs(float(parts[0])) + float(parts[1]) / 60 + float(parts[2]) / 3600
        return sign * result * (15.0 if is_ra else 1.0)
    except (TypeError, ValueError):
        return None


def band_from_filter(value: str | None) -> str | None:
    """Map an observation filter to the exposure calculator band."""
    if not value:
        return None
    value = value.lower()
    for names, band in (
        (("g_narrow",), "g_narrow"),
        (("r_narrow",), "r_narrow"),
        (("i_narrow",), "i_narrow"),
        (("z_narrow",), "z_narrow"),
        (("na_d",), "Na_D"),
        (("g",), "gp"), (("r",), "rp"), (("i",), "ip"), (("z",), "zs"),
    ):
        if any(name in value for name in names):
            return band
    return None


def measure_star_peak(
    data: np.ndarray,
    x: float,
    y: float,
    search_radius: int = 8,
    aperture_radius: int = 2,
    background_inner: int = 8,
    background_outer: int = 13,
) -> float | None:
    """Measure the brightest local pixel after subtracting an annular background.

    The WCS position can be offset by a few pixels, so the brightest pixel is
    searched for in a small box.  An annulus estimates the local sky and avoids
    using the full-frame median, which is important for large Sinistro images.
    """
    if data.ndim < 2 or not np.isfinite(x) or not np.isfinite(y):
        return None
    height, width = data.shape[-2:]
    cx, cy = int(round(x)), int(round(y))
    if cx < 0 or cy < 0 or cx >= width or cy >= height:
        return None
    y0, y1 = max(0, cy - search_radius), min(height, cy + search_radius + 1)
    x0, x1 = max(0, cx - search_radius), min(width, cx + search_radius + 1)
    search = np.asarray(data[y0:y1, x0:x1], dtype=float)
    if search.size == 0 or not np.isfinite(search).any():
        return None
    peak_index = np.nanargmax(search)
    py, px = np.unravel_index(peak_index, search.shape)
    px += x0
    py += y0

    yy, xx = np.ogrid[:height, :width]
    distance = np.hypot(xx - px, yy - py)
    annulus = np.asarray(data[(distance >= background_inner) & (distance <= background_outer)], dtype=float)
    annulus = annulus[np.isfinite(annulus)]
    if len(annulus) < 10:
        return None
    background = float(np.median(annulus))
    source = np.asarray(data[
        max(0, py - aperture_radius):min(height, py + aperture_radius + 1),
        max(0, px - aperture_radius):min(width, px + aperture_radius + 1),
    ], dtype=float)
    source = source[np.isfinite(source)]
    return max(0.0, float(np.max(source) - background)) if len(source) else None


def _science_data(hdul: fits.HDUList) -> np.ndarray | None:
    for hdu in hdul:
        if hdu.data is not None and getattr(hdu.data, "ndim", 0) >= 2:
            return np.asarray(hdu.data)
    return None


def _fits_paths(row: dict, fits_root: Path | None) -> list[str]:
    directory = (fits_root / row["instrument"] / row["obsdate"] if fits_root else raw_data_dir(row["instrument"], row["obsdate"]))
    return sorted(set(glob.glob(str(directory / f"{row['filename']}*"))))


def _observation_groups(conn: sqlite3.Connection, target: str, instruments: tuple[str, ...], max_frames: int) -> list[dict]:
    placeholders = ",".join("?" for _ in instruments)
    rows = conn.execute(
        f"""SELECT instrument, obsdate, ccd, object, filename, exptime, filter,
                   airmass, focus, ra, declination, read_mode, COUNT(*) AS nframes
            FROM frames
            WHERE object LIKE ? AND instrument IN ({placeholders})
            GROUP BY instrument, obsdate, ccd, object, filter, exptime, focus
            HAVING COUNT(*) < ?
            ORDER BY obsdate, instrument, ccd, filter, exptime""",
        (f"%{target}%", *instruments, max_frames),
    )
    return [dict(row) for row in rows]


def _catalog_stars(ra: float, dec: float, max_stars: int) -> list[dict]:
    field = cached_query_gaia_field(ra, dec, radius_arcsec=1200, max_mag=18)
    if field.error:
        raise RuntimeError(field.error)
    order = list(np.argsort(field.gmag))
    # Keep the nearest Gaia source even if a crowded field has more than
    # ``max_stars`` brighter neighbours.  The target is needed for the target
    # versus comparison-star classification and must not be dropped by a
    # brightness-only cut.
    target_index = int(np.argmin((field.ra - ra) ** 2 + (field.dec - dec) ** 2)) if len(field) else None
    if target_index is not None and target_index not in order[:max_stars]:
        order = order[: max(0, max_stars - 1)] + [target_index]
    stars = []
    for index in order[:max_stars]:
        gmag = float(field.gmag[index])
        if not np.isfinite(gmag):
            continue
        bp_rp = float(field.bp_rp[index]) if np.isfinite(field.bp_rp[index]) else None
        mags, source, approximate = lookup_magnitudes_with_fallback(
            float(field.ra[index]), float(field.dec[index]), gmag, bp_rp
        )
        stars.append({
            "ra": float(field.ra[index]), "dec": float(field.dec[index]),
            "gmag": gmag, "bp_rp": bp_rp, "mags": mags or {},
            "magnitude_source": source or "", "magnitude_approximate": approximate,
        })
    return stars


def compare(groups: list[dict], samples: int, max_stars: int, fits_root: Path | None) -> list[dict]:
    results = []
    catalog_cache: dict[tuple[float, float], list[dict]] = {}
    for group in groups:
        band = band_from_filter(group["filter"])
        if band is None or not group["exptime"]:
            LOG.warning("Skipping unsupported/incomplete group: %s", group)
            continue
        ra = parse_sexagesimal(group["ra"], True)
        dec = parse_sexagesimal(group["declination"], False)
        paths = _fits_paths(group, fits_root)
        if ra is None or dec is None or not paths:
            LOG.warning("No coordinates or FITS files for %s", group)
            continue
        cache_key = (round(ra, 5), round(dec, 5))
        if cache_key not in catalog_cache:
            catalog_cache[cache_key] = _catalog_stars(ra, dec, max_stars)
        stars = catalog_cache[cache_key]
        sample_paths = paths if len(paths) <= samples else [paths[int(i * (len(paths) - 1) / (samples - 1))] for i in range(samples)]
        observed: dict[int, list[float]] = defaultdict(list)
        seen_in_image: set[int] = set()
        for path in sample_paths:
            try:
                with fits.open(path, memmap=False) as hdul:
                    data = _science_data(hdul)
                    if data is None:
                        continue
                    wcs = WCS(hdul[0].header).celestial
                    x, y = wcs.world_to_pixel_values([s["ra"] for s in stars], [s["dec"] for s in stars])
                    for i, (px, py) in enumerate(zip(x, y)):
                        if 0 <= px < data.shape[-1] and 0 <= py < data.shape[-2]:
                            seen_in_image.add(i)
                        peak = measure_star_peak(data, px, py)
                        if peak is not None:
                            observed[i].append(peak)
            except Exception as exc:
                LOG.warning("Could not measure %s: %s", path, exc)
        focus = float(group["focus"] or 0.0)
        airmass = float(group["airmass"] or 1.1)
        for i, star in enumerate(stars):
            if i not in seen_in_image:
                continue
            magnitude = star["mags"].get(band)
            prediction = None
            if magnitude is not None:
                prediction = calc_peak(group["instrument"], band, float(magnitude), focus, float(group["exptime"]), airmass)
            obs_peak = float(np.median(observed[i])) if observed[i] else None
            distance = ((star["ra"] - ra) * np.cos(np.deg2rad(dec))) ** 2 + (star["dec"] - dec) ** 2
            results.append({
                "target": group["object"], "instrument": group["instrument"], "obsdate": group["obsdate"],
                "ccd": group["ccd"], "filter": group["filter"], "band": band, "exptime_s": group["exptime"],
                "focus": focus, "airmass": airmass, "nframes": group["nframes"], "n_sampled": len(sample_paths),
                "star_type": "target" if distance < (3 / 3600) ** 2 else "comparison",
                "star_ra": star["ra"], "star_dec": star["dec"], "gmag": star["gmag"], "magnitude": magnitude,
                "magnitude_source": star["magnitude_source"], "magnitude_approximate": star["magnitude_approximate"],
                "observed_peak_adu": obs_peak,
                "predicted_peak_adu": prediction["peak_adu"] if prediction else None,
                "observed_minus_predicted_pct": ((obs_peak / prediction["peak_adu"] - 1) * 100) if obs_peak and prediction else None,
                "predicted_saturated": prediction["is_saturated"] if prediction else None,
                "status": "ok" if obs_peak is not None and prediction else ("no_magnitude" if obs_peak is not None else "no_measurement"),
            })
    return results


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--target", default="TOI6109")
    parser.add_argument("--instrument", action="append", choices=("muscat3", "sinistro"), dest="instruments")
    parser.add_argument("--max-frames", type=int, default=DEFAULT_MAX_FRAMES)
    parser.add_argument("--samples", type=int, default=DEFAULT_SAMPLES)
    parser.add_argument("--max-stars", type=int, default=DEFAULT_MAX_STARS)
    parser.add_argument("--db", type=Path, default=None)
    parser.add_argument("--fits-root", type=Path, default=None, help="Root containing <instrument>/<obsdate> directories")
    parser.add_argument("--output", type=Path, default=Path.home() / "temp/toi6109_peak_comparison.csv")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
    instruments = tuple(args.instruments or DEFAULT_INSTRUMENTS)
    with sqlite3.connect(args.db or db_path()) as conn:
        conn.row_factory = sqlite3.Row
        groups = _observation_groups(conn, args.target, instruments, args.max_frames)
    LOG.info("Found %d test observation groups", len(groups))
    rows = compare(groups, max(1, args.samples), max(1, args.max_stars), args.fits_root)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open("w", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0]) if rows else ["status"])
        writer.writeheader()
        writer.writerows(rows)
    LOG.info("Wrote %d star comparisons to %s", len(rows), args.output)


if __name__ == "__main__":
    main()
