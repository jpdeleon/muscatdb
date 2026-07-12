"""Exposure Time Calculator for MuSCAT instruments.

Calibrates peak-count coefficients from observed FITS frames, then predicts
exposure times or peak counts for any target + band + focus + airmass.

Calibration formula (empirical, following peak_count_estimator):
    log10(peak_ADU) = coef - 0.4 * (mag + k * (airmass - 1.1)) + log10(exp / 60)

    → peak_ADU = 10^coef * 10^(-0.4*mag_eff) * exp / 60
    → exp  = target_ADU * 60 / 10^(coef - 0.4*mag_eff)
"""

from __future__ import annotations

import math
import time
import sqlite3
import pathlib
import logging
import threading

import numpy as np
from astropy.io import fits
from astropy.coordinates import SkyCoord
import astropy.units as u
from concurrent.futures import ThreadPoolExecutor, as_completed
try:
    from astroquery.vizier import Vizier as _Vizier
    _HAS_ASTROQUERY = True
except ImportError:
    _Vizier = None  # type: ignore
    _HAS_ASTROQUERY = False

from muscat_db.instruments import INSTRUMENTS
from muscat_db.database import db_path, SCHEMA

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# Atmospheric extinction coefficients (mag/airmass above 1 airmass)
# Standard values for a good site, per band.
EXTINCTION = {
    "g": 0.15,
    "r": 0.09,
    "i": 0.07,
    "z": 0.05,
    "gp": 0.15,
    "rp": 0.09,
    "ip": 0.07,
    "zs": 0.05,
    "g_narrow": 0.15,
    "Na_D": 0.08,
    "i_narrow": 0.07,
    "z_narrow": 0.05,
    "r_narrow": 0.09,
}

# MuSCAT SDSS-like filter mapping for Pan-STARRS lookup.
# g/r/i/z from PS1 map to MuSCAT gp/rp/ip/zs
PS1_TO_BAND = {"gmag": "gp", "rmag": "rp", "imag": "ip", "zmag": "zs"}

# Catalogs queried for griz photometry, in priority order. Pan-STARRS DR1
# covers Dec > -30; SkyMapper DR2 fills the southern sky. ``dec_range`` gates
# each catalog to where it has coverage so we don't waste queries.
_GRIZ_CATALOGS = (
    {
        "name": "II/349/ps1",  # Pan-STARRS DR1
        "label": "Pan-STARRS DR1",
        "cols": {"gmag": "gp", "rmag": "rp", "imag": "ip", "zmag": "zs"},
        "dec_range": (-30.0, 90.0),
    },
    {
        "name": "II/358/smss",  # SkyMapper DR2
        "label": "SkyMapper DR2",
        "cols": {"gPSF": "gp", "rPSF": "rp", "iPSF": "ip", "zPSF": "zs"},
        "dec_range": (-90.0, 10.0),
    },
)

# Progressively widen the cone search until a match is found.
_LOOKUP_RADII_ARCSEC = (3.0, 5.0, 10.0)

# Retry transient Vizier failures with exponential backoff.
_VIZIER_RETRIES = 3
_VIZIER_BACKOFF_SEC = 1.0

# Telescope reference values used to scale the MuSCAT3 calibration and set
# saturation limits. gain is in electrons/ADU, pixel_scale in arcsec/pixel,
# and aperture_m in metres. full_well is in electrons.
#
# muscat/muscat2 saturate at the same level in every band (prose2's
# muscat_*.telescope / muscat2_*.telescope files all agree per instrument),
# so one full_well constant per instrument is accurate for them.
#
# muscat3/muscat4 do NOT: prose2's muscat3_*.telescope / muscat4_*.telescope
# files give a different `saturation` (ADU) per band, and live BANZAI headers
# confirm the same ordering (ip/zs saturate well before gp/rp). full_well is
# therefore per band here: telescope-file saturation[ADU] x gain[e-/ADU].
# Narrowband filters (g_narrow, i_narrow, ...) fall back to their broadband
# parent via _NARROW_TO_BROADBAND (see _full_well_gain below).
#
# sinistro has no per-band saturation in its .telescope file (site/camera
# dependent -- LCO runs a dozen+ physical Sinistro units across sites) and
# BANZAI headers show real full wells of ~217,000-299,000 e- with gain
# already normalized to 1.0. full_well below is the median MAXLIN sampled
# from 9 distinct Sinistro telescopes (coj/cpt/elp/lsc/tfn); MAXLIN (onset of
# non-linearity) is used rather than the sometimes-higher SATURATE keyword
# since it's the safer threshold for precision photometry. This remains a
# per-instrument approximation -- individual sites can differ.
INSTRUMENT_PARAMS = {
    "muscat":  {"full_well": 55000, "gain": 1.0, "pixel_scale": 0.358, "aperture_m": 1.88},
    "muscat2": {"full_well": 62000, "gain": 1.0, "pixel_scale": 0.44, "aperture_m": 1.52},
    "muscat3": {
        "full_well": {"gp": 113684, "rp": 114894, "ip": 82001, "zs": 90000},
        "gain": 1.8, "pixel_scale": 0.267, "aperture_m": 2.0,
    },
    "muscat4": {
        "full_well": {"gp": 115200, "rp": 115200, "ip": 82800, "zs": 115200},
        "gain": 1.8, "pixel_scale": 0.267, "aperture_m": 2.0,
    },
    "sinistro": {"full_well": 246400, "gain": 1.0, "pixel_scale": 0.39, "aperture_m": 1.0},
}

# Empirical coefficients for MuSCAT3 from peak_count_estimator.
# coef_b[band][focus_idx] = log10(peak_ADU) for mag=0, exp=60s, airmass=1.1, seeing=0.8"
# focus_idx maps: 0→0mm, 1→1mm, 2→2mm, 3→3mm, 4→4mm, 5→5mm, 6→6mm
# These serve as defaults; DB calibration overrides them.
_MUSCAT3_COEF_B = {
    "gp": [10.51276637, 10.2636757,  9.99203702,  9.72257331,  9.51637384,  9.44039911, 9.28810117],
    "rp": [10.509745,   10.28773524, 10.23136096,  9.9569031,   9.64339266,  9.53424511, 9.35282711],
    "ip": [10.24247762, 10.08295809,  9.88549464,  9.57156006,  9.30655855,  9.24200499, 9.05599536],
    # zs coefs are pre-adjusted (+0.4) relative to the original peak_count_estimator
    # so that all bands use the same formula: logpeak = coef - 0.4 * mag
    "zs": [10.24187978, 10.07468481,  9.95641629,  9.65645003,  9.366079,    9.29227852, 9.10435107],
}
_MUSCAT3_FWHM_PIX = {
    "gp": [3.0, 3.95666667, 5.91166667, 9.135,     12.93333333, 15.49833333, 19.60166667],
    "rp": [3.0, 3.88833333, 4.19666667, 6.24833333, 10.24666667, 12.75666667, 16.68833333],
    "ip": [3.0, 3.50166667, 4.62666667, 7.545,      11.53833333, 13.89833333, 17.99333333],
    "zs": [3.0, 3.6,        4.36333333, 6.81333333, 10.76,       13.35333333, 17.45      ],
}
_MUSCAT3_GAIN = {"gp": 1.9, "rp": 1.88, "ip": 1.8, "zs": 2.0}

# Default coefficient for uncalibrated instruments/bands.
_DEFAULT_COEF = 10.0
_DEFAULT_FWHM = 3.0

# Narrowband → broadband parent mapping
_NARROW_TO_BROADBAND = {
    "g_narrow": "gp",
    "r_narrow": "rp",
    "i_narrow": "ip",
    "z_narrow": "zs",
    "Na_D": "rp",
}


def _full_well_gain(instrument: str, band: str) -> tuple[float, float]:
    """Full well [e-] and gain [e-/ADU] for one instrument+band.

    muscat3/muscat4 store full_well per band (see INSTRUMENT_PARAMS); every
    other instrument shares one saturation level across all its bands.
    Narrowband filters resolve to their broadband parent; an unrecognized
    band falls back to the lowest (most conservative) band on record rather
    than raising, since a band variant we don't yet know about should be
    treated as if it could saturate first.
    """
    params = INSTRUMENT_PARAMS.get(instrument, {})
    gain = float(params.get("gain", 1.0))
    full_well = params.get("full_well", 100000)
    if isinstance(full_well, dict):
        key = band if band in full_well else _NARROW_TO_BROADBAND.get(band, band)
        full_well = full_well.get(key, min(full_well.values()))
    return float(full_well), gain


# Approximate filter FWHM bandwidth, in nm. Placeholder values pending real
# transmission-curve measurements; update here if exact filter specs become
# available. Used to scale exposure time by filter width: a narrower filter
# passes proportionally fewer photons/sec for the same source (assuming a
# ~flat SED over the band), so it needs a proportionally longer exposure to
# reach the same peak ADU. The star's *magnitude* does not change between
# broad and narrow bands -- only the coefficient (photons/sec) does.
_FILTER_WIDTH_NM = {
    "gp": 140.0, "rp": 100.0, "ip": 100.0, "zs": 100.0,
    "g_narrow": 10.0, "Na_D": 5.0, "i_narrow": 5.0, "z_narrow": 5.0, "r_narrow": 10.0,
}


def _narrowband_offset(band: str) -> float:
    """log10(filter_width_ratio) to apply to a broadband coef for ``band``.

    Returns 0.0 for broadband/unknown bands (no scaling).
    """
    parent = _NARROW_TO_BROADBAND.get(band)
    if parent is None:
        return 0.0
    narrow_w = _FILTER_WIDTH_NM.get(band)
    parent_w = _FILTER_WIDTH_NM.get(parent)
    if not narrow_w or not parent_w:
        return 0.0
    return math.log10(narrow_w / parent_w)

# Band → focus_idx converter
_FOCUS_MM = [0.0, 1.0, 2.0, 3.0, 4.0, 5.0, 6.0]


def _muscat3_coef(band: str, focus_mm: float) -> tuple[float, float]:
    """Interpolate MuSCAT3 empirical coef and FWHM for a given band and focus (mm).

    Narrowbands are derived from their broadband parent with a filter-width offset.
    """
    # Resolve narrowband → broadband parent
    parent = _NARROW_TO_BROADBAND.get(band)
    coefs = _MUSCAT3_COEF_B.get(parent or band)
    fwhms = _MUSCAT3_FWHM_PIX.get(parent or band)
    if coefs is None or fwhms is None:
        return (_DEFAULT_COEF, _DEFAULT_FWHM)
    focus_mm = max(0.0, min(6.0, focus_mm))
    idx = focus_mm  # integer focus mm maps directly to index
    lo = int(idx)
    hi = min(lo + 1, 6)
    frac = idx - lo
    if frac == 0:
        c = coefs[lo]
        f = fwhms[lo]
    else:
        c = coefs[lo] + (coefs[hi] - coefs[lo]) * frac
        f = fwhms[lo] + (fwhms[hi] - fwhms[lo]) * frac
    # Apply narrowband offset
    offset = _narrowband_offset(band)
    return (c + offset, f)

# ---------------------------------------------------------------------------
# DB table
# ---------------------------------------------------------------------------

SCHEMA_EXPOSURE = """
CREATE TABLE IF NOT EXISTS exposure_coeffs (
    instrument  TEXT NOT NULL,
    band        TEXT NOT NULL,
    focus_mm    REAL NOT NULL,
    coef        REAL NOT NULL,
    fwhm_pix    REAL NOT NULL,
    n_frames    INTEGER NOT NULL DEFAULT 0,
    updated_at  TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (instrument, band, focus_mm)
);

CREATE TABLE IF NOT EXISTS exposure_jobs (
    id          TEXT PRIMARY KEY,
    instrument  TEXT NOT NULL,
    state       TEXT NOT NULL DEFAULT 'pending',
    progress    TEXT NOT NULL DEFAULT '',
    started_at  REAL NOT NULL,
    updated_at  REAL NOT NULL
);
"""

# Ensure the exposure schema is included when creating the DB.
# We add it to the SCHEMA used by database.py.

# ---------------------------------------------------------------------------
# Helper: read peak from a FITS image
# ---------------------------------------------------------------------------


def _measure_peak(fits_path: str) -> float | None:
    """Measure the peak pixel value (ADU) from a FITS image.

    Skips bias level (median of the image) and returns the 99th percentile
    value as a robust peak estimate (less sensitive to cosmic rays than max).

    For calibrated images, bias should already be removed, but we still clip
    a small baseline offset.
    """
    try:
        with fits.open(fits_path, memmap=False) as hdul:
            data = hdul[0].data
            if data is None:
                return None
            # Use the first science extension if primary is empty
            if data.ndim == 0 or data.size == 0:
                for hdu in hdul[1:]:
                    if hdu.data is not None and hdu.data.ndim >= 2:
                        data = hdu.data
                        break
            if data is None or data.ndim < 2 or data.size == 0:
                return None
            # Subtract baseline (median of the whole image = bias/background)
            baseline = np.median(data)
            peak = np.percentile(data, 99.9) - baseline
            return max(0.0, float(peak))
    except Exception as exc:
        logger.debug("Failed to read peak from %s: %s", fits_path, exc)
        return None


def _measure_header_fwhm_pix(fits_path: str, instrument: str) -> float | None:
    """Read BANZAI's FWHM estimate and convert arcseconds to pixels.

    ``L1FWHM`` is the BANZAI per-frame seeing estimate in arcseconds.  A
    missing, non-finite, or non-positive value is not a measurement and must
    not be replaced by a plausible-looking constant during calibration.
    """
    pixel_scale = INSTRUMENT_PARAMS.get(instrument, {}).get("pixel_scale")
    if not pixel_scale or pixel_scale <= 0:
        return None
    try:
        value = float(fits.getheader(fits_path, 0).get("L1FWHM"))
    except (OSError, TypeError, ValueError):
        return None
    if not math.isfinite(value) or value <= 0:
        return None
    return value / float(pixel_scale)


# ---------------------------------------------------------------------------
# Magnitude lookup (Pan-STARRS DR1 via Vizier)
# ---------------------------------------------------------------------------

# Thresholds: exclude stars fainter than this from calibration (likely too noisy)
_MAX_CALIB_MAG = 19.0


def _clean_mag(val) -> float | None:
    """Coerce a Vizier table cell to a usable magnitude, or None.

    Rejects masked cells, NaN, non-numeric values, and non-positive magnitudes
    (which signal a missing/flagged measurement in these catalogs).
    """
    if val is None:
        return None
    # astropy masked columns expose a per-cell ``mask`` attribute.
    if getattr(val, "mask", False):
        return None
    try:
        fval = float(val)
    except (TypeError, ValueError):
        return None
    if math.isnan(fval) or fval <= 0:
        return None
    return fval


def _query_vizier_catalog(coord, catalog: str, columns: list[str], radius_arcsec: float):
    """Query a single Vizier catalog with retry/backoff.

    Returns the matched, distance-sorted table, or None if no rows or all
    attempts failed.
    """
    _Vizier.ROW_LIMIT = -1
    _Vizier.columns = columns
    last_exc: Exception | None = None
    for attempt in range(_VIZIER_RETRIES):
        try:
            result = _Vizier.query_region(
                coord, radius=radius_arcsec * u.arcsec, catalog=catalog
            )
        except Exception as exc:  # transient network/server error → retry
            last_exc = exc
            time.sleep(_VIZIER_BACKOFF_SEC * (2 ** attempt))
            continue
        if not result or catalog not in [r.meta.get("name") for r in result]:
            return None
        cat = result[catalog]
        if "_r" in cat.colnames:
            cat.sort("_r")
        return cat if len(cat) else None
    logger.warning("Vizier query for %s failed after %d attempts: %s",
                   catalog, _VIZIER_RETRIES, last_exc)
    return None


def _extract_mags(cat, col_map: dict[str, str]) -> dict[str, float]:
    """Pull g/r/i/z mags from a distance-sorted table.

    Prefers the nearest source, but backfills any band missing from the nearest
    entry using the next-closest source that has it (catalog rows sometimes mask
    individual bands).
    """
    mags: dict[str, float] = {}
    for entry in cat:
        for col, band in col_map.items():
            if band in mags or col not in cat.colnames:
                continue
            cleaned = _clean_mag(entry[col])
            if cleaned is not None:
                mags[band] = cleaned
        if len(mags) == len(col_map):
            break
    return mags


def lookup_magnitudes(
    ra: float,
    dec: float,
    radius_arcsec: float | None = None,
    return_source: bool = False,
):
    """Query catalogs for g/r/i/z magnitudes at the given position.

    Tries Pan-STARRS DR1 first, then SkyMapper DR2 for southern targets, each
    gated to its declination coverage. Within a catalog the cone search widens
    progressively (3→5→10 arcsec) until a match is found, queries are retried on
    transient failures, and missing bands are backfilled from nearby sources.

    Returns a dict like {'gp': 12.5, 'rp': 12.0, 'ip': 11.8, 'zs': 11.5}
    or None if no match found. When ``return_source`` is True, returns a
    ``(mags, source)`` tuple where ``source`` describes the catalog and match
    radius (or None when nothing matched).

    Requires ``astroquery`` (install via ``pip install astroquery`` or
    use the ``prose`` conda env which includes it).
    """
    def _result(mags, source):
        return (mags, source) if return_source else mags

    if not _HAS_ASTROQUERY:
        logger.warning("astroquery not available; install it for catalog lookup")
        return _result(None, None)
    try:
        coord = SkyCoord(ra=ra, dec=dec, unit=(u.deg, u.deg), frame="icrs")
    except Exception as exc:
        logger.warning("Invalid coordinates (%r, %r): %s", ra, dec, exc)
        return _result(None, None)

    radii = (radius_arcsec,) if radius_arcsec is not None else _LOOKUP_RADII_ARCSEC

    for entry in _GRIZ_CATALOGS:
        dec_lo, dec_hi = entry["dec_range"]
        if not (dec_lo <= dec <= dec_hi):
            continue
        col_map = entry["cols"]
        columns = ["_r", *col_map.keys()]
        for radius in radii:
            cat = _query_vizier_catalog(coord, entry["name"], columns, radius)
            if cat is None:
                continue
            mags = _extract_mags(cat, col_map)
            if mags:
                source = f"{entry['label']} (within {radius:.0f}\")"
                logger.info(
                    "Found %d band(s) for (%.4f, %.4f) in %s",
                    len(mags), ra, dec, source,
                )
                return _result(mags, source)

    logger.info("No griz photometry found for (%.4f, %.4f)", ra, dec)
    return _result(None, None)


# ---------------------------------------------------------------------------
# Gaia (G, BP-RP) -> griz fallback, for comparison stars with no PS1/SkyMapper match
# ---------------------------------------------------------------------------

# TODO(gaia-griz-transform, under development -- do not fill in from memory):
# pending independently verified coefficients for a published Gaia G/BP-RP ->
# SDSS-like griz color transform (e.g. Evans et al. 2018 Table 5, or the Gaia
# EDR3/DR3 "photometric relationships with other photometric systems"
# documentation). Two attempts to source this have both failed verification:
# (1) an automated fetch of the ESA documentation table produced internally
# inconsistent numbers across two retrievals (different polynomial degree for
# the same relationship); (2) a coefficient table supplied for this feature
# (data/gaia_ps1_color_transform.md) cites arXiv:2601.05486 as support, but
# that paper is on an unrelated topic (an all-sky photometric standard-star
# database), indicating the table's numbers cannot be trusted either. Until a
# properly-cited copy of the real source table is verified, this always
# returns None, and lookup_magnitudes_with_fallback degrades to reporting "no
# photometry available" for stars with no direct catalog match rather than
# silently approximating a saturation-relevant magnitude with unverified
# numbers.
def gaia_to_griz_transform(gmag: float, bp_rp: float | None) -> dict[str, float] | None:
    """Approximate gp/rp/ip/zs from Gaia (G, BP-RP) via a published color transform.

    NOT YET IMPLEMENTED -- always returns ``None``; see the TODO above.

    Used only as a fallback when Pan-STARRS DR1 / SkyMapper DR2 have no match
    for a star (see :func:`lookup_magnitudes_with_fallback`). Once
    implemented, must return ``None`` if the transform isn't available
    (missing color, or out of the fit's valid range) so the caller can report
    "no photometry" rather than a bad estimate.
    """
    if bp_rp is None or not math.isfinite(bp_rp):
        return None
    return None


def lookup_magnitudes_with_fallback(
    ra: float,
    dec: float,
    gmag: float | None = None,
    bp_rp: float | None = None,
) -> tuple[dict[str, float] | None, str | None, bool]:
    """Griz magnitudes for a star: real catalog first, Gaia-transform fallback second.

    Tries the same Pan-STARRS DR1 / SkyMapper DR2 lookup used for the primary
    target (:func:`lookup_magnitudes`). If that finds no match and a Gaia
    ``(gmag, bp_rp)`` is supplied, falls back to
    :func:`gaia_to_griz_transform`. Which path was used (or that neither
    found anything) is always logged and returned explicitly via
    ``is_approx`` / ``source``, since a UI showing this magnitude needs to be
    able to tell real photometry from an approximation.

    Returns ``(mags, source, is_approx)``; ``mags`` is ``None`` if neither
    path found anything.
    """
    mags, source = lookup_magnitudes(ra, dec, return_source=True)
    if mags:
        logger.info("Griz for (%.4f, %.4f): real catalog match (%s)", ra, dec, source)
        return mags, source, False

    if gmag is not None:
        transformed = gaia_to_griz_transform(gmag, bp_rp)
        if transformed:
            source = "Gaia DR3 color transform (approx)"
            logger.info(
                "Griz for (%.4f, %.4f): no catalog match, using %s (G=%.2f, BP-RP=%s)",
                ra, dec, source, gmag, bp_rp,
            )
            return transformed, source, True

    logger.info(
        "Griz for (%.4f, %.4f): no catalog match; Gaia color-transform fallback is "
        "under development (gaia_to_griz_transform) and not yet available",
        ra, dec,
    )
    return None, None, False


def resolve_target_coords(target_name: str) -> tuple[float, float] | None:
    """Resolve a target name to (ra, dec) in degrees using SIMBAD via astropy."""
    try:
        coord = SkyCoord.from_name(target_name)
        return (float(coord.ra.deg), float(coord.dec.deg))
    except Exception as exc:
        logger.warning("Could not resolve target '%s': %s", target_name, exc)
        return None


# ---------------------------------------------------------------------------
# Coeff storage helpers
# ---------------------------------------------------------------------------


def _conn():
    """Get a connection to the main muscat DB."""
    c = sqlite3.connect(db_path(), timeout=30)
    c.execute("PRAGMA journal_mode=WAL")
    c.executescript(SCHEMA)
    c.executescript(SCHEMA_EXPOSURE)
    return c


def save_coeff(instrument: str, band: str, focus_mm: float, coef: float, fwhm_pix: float, n_frames: int):
    conn = _conn()
    # Round focus to nearest 0.5 mm for binning
    focus_bin = round(focus_mm * 2) / 2
    conn.execute(
        """INSERT OR REPLACE INTO exposure_coeffs
           (instrument, band, focus_mm, coef, fwhm_pix, n_frames, updated_at)
           VALUES (?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)""",
        (instrument, band, focus_bin, coef, fwhm_pix, n_frames),
    )
    conn.commit()
    conn.close()


def load_coeffs(instrument: str) -> dict[tuple[str, float], tuple[float, float, int]]:
    """Load all coefficients for an instrument.

    Returns {(band, focus_mm): (coef, fwhm_pix, n_frames)}
    """
    conn = _conn()
    rows = conn.execute(
        "SELECT band, focus_mm, coef, fwhm_pix, n_frames FROM exposure_coeffs WHERE instrument = ?",
        (instrument,),
    ).fetchall()
    conn.close()
    return {(r[0], r[1]): (r[2], r[3], r[4]) for r in rows}


def _scale_coef(instrument: str, band: str, focus_mm: float) -> tuple[float, float]:
    """Scale MuSCAT3 empirical coef for other instruments.

    The scaling accounts for differences in collecting area and pixel scale:
    peak ∝ A_tel / pixel_scale²  (more area = more photons, finer pixels = spread more)

    For uncalibrated instruments, this provides a rough estimate.
    """
    coef, fwhm = _muscat3_coef(band, focus_mm)
    params = INSTRUMENT_PARAMS.get(instrument, {})
    muscat3_params = INSTRUMENT_PARAMS.get("muscat3", {})
    area_ratio = (params.get("aperture_m", 1.0) / muscat3_params.get("aperture_m", 2.0)) ** 2
    ps_ratio = (muscat3_params.get("pixel_scale", 0.267) / params.get("pixel_scale", 0.267)) ** 2
    gain_ratio = params.get("gain", 1.8) / muscat3_params.get("gain", 1.8)
    coef = coef + math.log10(area_ratio) + math.log10(ps_ratio) + math.log10(gain_ratio)
    # Scale FWHM by pixel scale ratio (coarser pixels = fewer pixels for same PSF)
    fwhm = fwhm * (params.get("pixel_scale", 0.267) / muscat3_params.get("pixel_scale", 0.267))
    return (coef, fwhm)


def get_coeff(instrument: str, band: str, focus_mm: float, coeffs: dict | None = None):
    """Get (coef, fwhm_pix) for a given instrument+band+focus, interpolating if needed.

    Returns (coef, fwhm_pix), preferring DB-calibrated values. Falls back to
    MuSCAT3 empirical data (scaled for other instruments) when uncalibrated.
    """
    if coeffs is None:
        coeffs = load_coeffs(instrument)

    focus_bin = round(focus_mm * 2) / 2

    # Exact match in DB coeffs
    key = (band, focus_bin)
    if key in coeffs:
        c, f, _ = coeffs[key]
        return (c, f)

    # Nearest-neighbor interpolation among DB coeffs
    candidates = [
        (fb, c, f)
        for (b, fb), (c, f, _) in coeffs.items()
        if b == band
    ]
    if candidates:
        candidates.sort(key=lambda x: abs(x[0] - focus_bin))
        return (candidates[0][1], candidates[0][2])

    # No DB coeffs: fall back to MuSCAT3 empirical data (scaled for other instruments)
    return _scale_coef(instrument, band, focus_mm)


def calibration_status(instrument: str) -> dict:
    """Return count of calibrated bands and focus points for an instrument."""
    conn = _conn()
    rows = conn.execute(
        """SELECT band, COUNT(*), SUM(n_frames), MAX(updated_at)
           FROM exposure_coeffs WHERE instrument = ?
           GROUP BY band ORDER BY band""",
        (instrument,),
    ).fetchall()
    conn.close()
    return {
        "n_bands": len(rows),
        "bands": [{"band": r[0], "n_focus": r[1], "n_frames": r[2], "updated_at": r[3]} for r in rows],
    }


# ---------------------------------------------------------------------------
# Calibration engine
# ---------------------------------------------------------------------------


def _band_from_filter(filter_val: str) -> str | None:
    """Map raw FITS FILTER value to a canonical band name for the calculator."""
    from muscat_db.band_utils import _FILTER_BAND_ALIAS
    return _FILTER_BAND_ALIAS.get(filter_val)


def _fits_exists(instrument: str, obsdate: str, filename: str) -> str | None:
    """Try to locate a FITS file on disk.

    The filename column in the DB stores the base name without .fits.
    """
    cfg = INSTRUMENTS.get(instrument)
    if not cfg:
        return None
    base = pathlib.Path(cfg.data_dir) / obsdate
    for suffix in (".fits", ".fits.fz", ".fz"):
        p = base / f"{filename}{suffix}"
        if p.is_file():
            return str(p)
    return None


def calibrate_instrument(
    instrument: str,
    max_frames_per_bin: int = 50,
    max_workers: int = 4,
    force: bool = False,
) -> dict:
    """Calibrate exposure coefficients for an instrument from observed FITS frames.

    Strategy:
    1. Query DB for all frames with valid filter, focus, exptime, airmass.
    2. Group by (band, focus_bin) to ensure diverse sampling.
    3. For each frame: read FITS peak, look up target magnitude.
    4. Solve for coefficient.
    5. Average per (band, focus_bin) and save.

    Returns {"ok": bool, "bands_calibrated": int, "total_frames": int, ...}
    """
    from muscat_db.database import _TARGET_EXCLUDE_EXACT

    conn = _conn()
    exact_clause = ", ".join(f"'{s}'" for s in _TARGET_EXCLUDE_EXACT)

    # Get frames with valid data for calibration
    frames = conn.execute(
        f"""SELECT filename, obsdate, object, filter, exptime, airmass, focus, ccd, ra, declination
            FROM frames
            WHERE instrument = ?
              AND filter IS NOT NULL AND filter != ''
              AND object IS NOT NULL AND TRIM(object) <> ''
              AND exptime IS NOT NULL AND exptime > 0
              AND airmass IS NOT NULL AND airmass > 0
              AND LOWER(TRIM(object)) NOT IN ({exact_clause})
              AND LOWER(TRIM(object)) NOT LIKE '%flat%'
              AND LOWER(TRIM(object)) NOT LIKE 'dark%'
              AND LOWER(TRIM(object)) NOT LIKE 'bias%'
              AND LOWER(TRIM(object)) NOT LIKE '%test%'
              AND TRIM(object) NOT GLOB '*:*:*'
            ORDER BY RANDOM()""",
        (instrument,),
    ).fetchall()
    conn.close()

    if not frames:
        return {"ok": False, "error": "No usable frames found in DB"}

    # Group by (band, focus_bin) and sample up to max_frames_per_bin
    from collections import defaultdict
    bins: dict[tuple[str, float], list[dict]] = defaultdict(list)

    for row in frames:
        band = _band_from_filter(row[3])
        if band is None:
            continue
        exptime = float(row[4]) if row[4] else 0
        airmass = float(row[5]) if row[5] else 1.0
        focus_val = float(row[6]) if row[6] else 0.0
        if exptime <= 0:
            continue
        focus_bin = round(focus_val * 2) / 2
        key = (band, focus_bin)
        if len(bins[key]) < max_frames_per_bin:
            bins[key].append({
                "filename": row[0],
                "obsdate": row[1],
                "object": row[2],
                "filter": row[3],
                "exptime": exptime,
                "airmass": airmass,
                "focus": focus_val,
                "ccd": row[7],
                "ra": row[8],
                "dec": row[9],
                "band": band,
            })

    if not bins:
        return {"ok": False, "error": "No frames matched known band filters"}

    total_jobs = sum(len(v) for v in bins.values())
    logger.info("Calibrating %s: %d frames in %d band/focus bins",
                instrument, total_jobs, len(bins))

    # Cache SIMBAD name → coordinate lookups (shared across threads)
    _simbad_cache: dict[str, tuple[float, float] | None] = {}
    _simbad_lock = threading.Lock()

    def _resolve(item: dict) -> tuple[float, float] | None:
        """Resolve target to (ra, dec) via SIMBAD with caching."""
        obj = item["object"]
        with _simbad_lock:
            if obj in _simbad_cache:
                return _simbad_cache[obj]
        try:
            coord = SkyCoord.from_name(obj)
            result = (float(coord.ra.deg), float(coord.dec.deg))
        except Exception:
            result = None
        with _simbad_lock:
            _simbad_cache[obj] = result
        return result

    # Cache Vizier queries
    _vizier_cache: dict[tuple[float, float], dict | None] = {}
    _vizier_lock = threading.Lock()

    def _lookup(ra: float, dec: float) -> dict | None:
        key = (round(ra, 4), round(dec, 4))
        with _vizier_lock:
            if key in _vizier_cache:
                return _vizier_cache[key]
        # Tight radius for calibration: the SIMBAD-resolved coordinate is the
        # actual target, so avoid widening into neighboring sources.
        mags = lookup_magnitudes(ra, dec, radius_arcsec=3.0)
        with _vizier_lock:
            _vizier_cache[key] = mags
        return mags

    # Process frames: read FITS peak + look up magnitude
    def _process(item: dict) -> dict | None:
        fits_path = _fits_exists(instrument, item["obsdate"], item["filename"])
        if not fits_path:
            return None
        peak_adu = _measure_peak(fits_path)
        if peak_adu is None or peak_adu <= 0:
            return None
        fwhm_pix = _measure_header_fwhm_pix(fits_path, instrument)

        # Resolve target name via SIMBAD (cached) and look up Pan-STARRS mags
        coords = _resolve(item)
        if not coords:
            return None
        mags = _lookup(*coords)
        if not mags:
            return None

        band = item["band"]
        mag = mags.get(band)
        if mag is None or mag > _MAX_CALIB_MAG:
            return None

        item["mags"] = mags
        item["peak_adu"] = peak_adu
        item["fwhm_pix"] = fwhm_pix
        return item

    processed = []
    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        futures = {executor.submit(_process, item): item for item_list in bins.values() for item in item_list}
        for future in as_completed(futures):
            try:
                result = future.result()
                if result:
                    processed.append(result)
            except Exception as exc:
                logger.debug("Frame processing failed: %s", exc)

    if not processed:
        return {"ok": False, "error": "No frames could be calibrated (check FITS paths and magnitude lookups)"}

    # Aggregate by (band, focus_bin)
    agg: dict[tuple[str, float], list[float]] = defaultdict(list)
    fwhm_data: dict[tuple[str, float], list[float]] = defaultdict(list)
    mag_used: dict[tuple[str, float], list[float]] = defaultdict(list)

    for item in processed:
        band = item["band"]
        focus_bin = round(item["focus"] * 2) / 2
        key = (band, focus_bin)
        mag = item["mags"][band]
        airmass = item["airmass"]
        exptime = item["exptime"]
        peak_adu = item["peak_adu"]

        # Airmass correction
        k = EXTINCTION.get(band, 0.10)
        mag_eff = mag + k * (airmass - 1.1)

        # Solve: peak_ADU = 10^coef * 10^(-0.4*mag_eff) * exp / 60
        # → coef = log10(peak_ADU) + 0.4*mag_eff - log10(exp/60)
        c = math.log10(peak_adu) + 0.4 * mag_eff - math.log10(exptime / 60.0)
        agg[key].append(c)
        mag_used[key].append(mag)

        if item["fwhm_pix"] is not None:
            fwhm_data[key].append(item["fwhm_pix"])

    bands_calibrated = 0
    total_calib_frames = 0

    for (band, focus_bin), coefs in agg.items():
        coef_mean = float(np.mean(coefs))
        n = len(coefs)
        measured_fwhms = fwhm_data.get((band, focus_bin))
        if measured_fwhms:
            avg_fwhm = float(np.mean(measured_fwhms))
        elif instrument == "muscat3":
            avg_fwhm = _muscat3_coef(band, focus_bin)[1]
        else:
            avg_fwhm = _DEFAULT_FWHM
        save_coeff(instrument, band, focus_bin, coef_mean, avg_fwhm, n)
        bands_calibrated += 1
        total_calib_frames += n

    return {
        "ok": True,
        "instrument": instrument,
        "bands_calibrated": bands_calibrated,
        "total_frames": total_calib_frames,
    }


# ---------------------------------------------------------------------------
# Prediction functions
# ---------------------------------------------------------------------------


def calc_peak(
    instrument: str,
    band: str,
    mag: float,
    focus_mm: float,
    exptime: float,
    airmass: float = 1.1,
) -> dict:
    """Estimate peak pixel count (ADU and electrons) and FWHM.

    Returns:
        {
            "band": str,
            "mag": float,
            "exptime": float,
            "focus_mm": float,
            "airmass": float,
            "peak_adu": float,
            "peak_electrons": float,
            "fwhm_pix": float,
            "fwhm_arcsec": float,
        }
    """
    coeffs = load_coeffs(instrument)
    coef, fwhm_pix = get_coeff(instrument, band, focus_mm, coeffs)
    k = EXTINCTION.get(band, 0.10)
    mag_eff = mag + k * (airmass - 1.1)
    logpeak = coef - 0.4 * mag_eff + math.log10(exptime / 60.0)
    peak_adu = 10.0 ** logpeak
    params = INSTRUMENT_PARAMS.get(instrument, {})
    pixel_scale = params.get("pixel_scale", 0.4)
    full_well, gain_val = _full_well_gain(instrument, band)
    peak_electrons = peak_adu * gain_val
    pct_full_well = (peak_electrons / full_well) * 100.0 if full_well > 0 else 0.0
    return {
        "band": band,
        "mag": mag,
        "exptime": exptime,
        "focus_mm": focus_mm,
        "airmass": airmass,
        "peak_adu": round(peak_adu, 0),
        "peak_electrons": round(peak_electrons, 0),
        "fwhm_pix": round(fwhm_pix, 2),
        "fwhm_arcsec": round(fwhm_pix * pixel_scale, 2),
        "pct_full_well": round(pct_full_well, 1),
        "is_saturated": peak_electrons >= full_well,
    }


def calc_exptime(
    instrument: str,
    band: str,
    mag: float,
    focus_mm: float,
    target_adu: float,
    airmass: float = 1.1,
) -> dict:
    """Estimate exposure time needed to reach a target peak ADU.

    Returns same dict as calc_peak but with exptime as the derived value.
    """
    coeffs = load_coeffs(instrument)
    coef, fwhm_pix = get_coeff(instrument, band, focus_mm, coeffs)
    k = EXTINCTION.get(band, 0.10)
    mag_eff = mag + k * (airmass - 1.1)
    # peak_ADU = 10^coef * 10^(-0.4*mag_eff) * exp / 60
    # → exp = target_ADU * 60 / (10^coef * 10^(-0.4*mag_eff))
    exptime = target_adu * 60.0 / (10.0 ** (coef - 0.4 * mag_eff))
    params = INSTRUMENT_PARAMS.get(instrument, {})
    pixel_scale = params.get("pixel_scale", 0.4)
    full_well, gain_val = _full_well_gain(instrument, band)
    peak_electrons = target_adu * gain_val
    pct_full_well = (peak_electrons / full_well) * 100.0 if full_well > 0 else 0.0
    return {
        "band": band,
        "mag": mag,
        "exptime": round(exptime, 1),
        "focus_mm": focus_mm,
        "airmass": airmass,
        "peak_adu": round(target_adu, 0),
        "peak_electrons": round(peak_electrons, 0),
        "fwhm_pix": round(fwhm_pix, 2),
        "fwhm_arcsec": round(fwhm_pix * pixel_scale, 2),
        "pct_full_well": round(pct_full_well, 1),
        "is_saturated": peak_electrons >= full_well,
    }


def calc_all_bands(
    instrument: str,
    mags: dict[str, float],
    focus_mm: float,
    airmass: float = 1.1,
    sat_frac: float = 0.5,
    mode: str = "exptime",
    exptime: float | None = None,
    target_adu: float | None = None,
    confmode: str | None = None,
    extra_sources: list[dict] | None = None,
) -> dict:
    """Calculate for all bands, optionally across the target plus extra sources.

    mode="exptime": returns exposure time to reach sat_frac of full well.
    mode="peak": returns peak count for a given exptime.

    Full well is looked up per band (see ``_full_well_gain``): muscat3/muscat4
    saturate at meaningfully different levels per band, so the sat_frac-derived
    target ADU is computed separately for each band rather than once for the
    whole instrument. When ``target_adu`` is provided (custom ADU mode), it
    overrides the sat_frac-derived target uniformly across all bands.

    confmode: Sinistro readout mode ("central_2k_2x2" or "full_frame"), currently
    not used for calculation but may affect full well in future versions.

    ``extra_sources`` is an optional list of ``{"label": str, "mags": {band: mag}}``
    dicts (e.g. bright comparison stars from the FOV optimizer). MuSCAT
    instruments expose every star in the field simultaneously, so a
    comparison star that saturates before the target caps the usable
    exposure just as much as a limiting band does. Each source's bands are
    calculated the same way as the target's and tagged with ``source_label``
    / ``is_target`` in the returned per-band dict; ``recommended_exptime`` is
    the minimum across every (source, band) pair, not just the target's.
    """
    sources = [{"label": "Target", "mags": mags}, *(extra_sources or [])]

    # Sort by band order: gp, rp, ip, zs, then narrow; ties broken by source
    # order (target first) so same-band rows from different sources stay
    # grouped together in the output.
    band_order = {"gp": 0, "rp": 1, "ip": 2, "zs": 3,
                  "g_narrow": 4, "r_narrow": 5, "Na_D": 6, "i_narrow": 7, "z_narrow": 8}

    results: list[dict] = []
    for source_idx, source in enumerate(sources):
        label = source.get("label") or ("Target" if source_idx == 0 else f"Comp {source_idx}")
        for band, mag in source["mags"].items():
            if mode == "exptime":
                if target_adu is not None:
                    band_target_adu = target_adu
                else:
                    full_well_b, gain_b = _full_well_gain(instrument, band)
                    band_target_adu = (full_well_b * sat_frac) / gain_b
                r = calc_exptime(instrument, band, mag, focus_mm, band_target_adu, airmass)
            else:
                r = calc_peak(instrument, band, mag, focus_mm, exptime or 30.0, airmass)
            r["source_label"] = label
            r["is_target"] = source_idx == 0
            r["_sort_key"] = (band_order.get(band, 99), source_idx)
            results.append(r)

    results.sort(key=lambda r: r["_sort_key"])
    for r in results:
        del r["_sort_key"]

    # Recommended exposure time: minimum among all (source, band) pairs that
    # keeps everyone below saturation.
    if mode == "exptime":
        rec = min(r["exptime"] for r in results) if results else 0
    else:
        rec = None

    return {
        "instrument": instrument,
        "focus_mm": focus_mm,
        "airmass": airmass,
        "sat_frac": sat_frac,
        "mode": mode,
        "results": results,
        "recommended_exptime": round(rec, 1) if rec else None,
    }
