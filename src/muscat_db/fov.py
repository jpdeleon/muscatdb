"""Field-of-view pointing & orientation optimization.

Given a science target and an instrument with a (square) footprint, choose the
telescope **pointing** (field center, which need not sit on the target) and
**orientation** (position angle of the field) that keep the target inside the
field while capturing as many *useful comparison stars* as possible.

Why this matters: the optimal exposure time depends not only on the target but
on the comparison stars available in the field. A comparison much brighter than
the target forces a shorter exposure (to avoid saturating it), which lowers the
target's own SNR; one much fainter than the target carries little weight for
differential photometry. The "best" field is therefore the one whose footprint,
once shifted and rotated, contains the richest set of similarly-bright, well
isolated comparison stars while still holding the target.

The geometry is done in a tangent plane (gnomonic projection) centered on the
target, so offsets and rotations are simple Cartesian operations in arcsec. The
search is a coarse grid over field-center offset ``(east, north)`` and position
angle, which is cheap (a few hundred evaluations) and avoids local-minima
trouble that a gradient method would hit with the non-smooth in/out membership.

This module is intentionally dependency-light at import time: ``astropy`` is
required for the coordinate transforms, but the network query (Gaia DR3, via
the official ESA archive with a VizieR cone-search fallback) and
``astroquery`` are imported lazily so the pure-geometry helpers stay testable
offline.
"""

from __future__ import annotations

import logging
import math
import os
import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np

from muscat_db.cache import LRUCache

logger = logging.getLogger(__name__)

# Repo root: .../muscat-db (src/muscat_db/fov.py -> parents[2]).
_REPO_ROOT = Path(__file__).resolve().parents[2]
_DATA_DIR = _REPO_ROOT / "data"

# Which footprint XML describes each instrument. MuSCAT4 shares the MuSCAT3
# 2m optical design, so it reuses that footprint.
INSTRUMENT_FOV_FILES: dict[str, str] = {
    "muscat": "FOV_MuSCAT.vot.xml",
    "muscat2": "FOV_MuSCAT2.vot.xml",
    "muscat3": "FOV_MuSCAT3.vot.xml",
    "muscat4": "FOV_MuSCAT3.vot.xml",
}

# Instruments without a footprint XML: half-width of the square field in arcsec,
# derived from pixel_scale x detector size (see exposure.INSTRUMENT_PARAMS).
_FALLBACK_HALF_ARCSEC: dict[str, float] = {}

# Sinistro readout modes (half-width in arcsec).
# full_frame: 4096 pix @ 0.389 "/pix = 26' on a side
# central_2k_2x2: 1024 pix @ 0.778 "/pix = 13' on a side (2x2 binning of detector)
SINISTRO_MODES: dict[str, float] = {
    "full_frame": 0.389 * 4096 / 2.0,          # 26'x26'
    "central_2k_2x2": 0.778 * 1024 / 2.0,      # 13'x13'
}
_FALLBACK_HALF_ARCSEC["sinistro"] = SINISTRO_MODES["central_2k_2x2"]  # default

# Observatory locations (latitude in degrees). LCO sites where these instruments are deployed.
OBSERVATORY_LOCATIONS: dict[str, float] = {
    "muscat": -32.38,        # Sutherland, South Africa (CTIO/SAAO)
    "muscat2": -32.38,       # Sutherland, South Africa
    "muscat3": -32.38,       # Sutherland, South Africa (ogg - OGG 2m)
    "muscat4": -30.24,       # Cerro Tololo, Chile (coj - COJ 2m)
    "sinistro": -30.24,      # Multiple LCO 1m sites, using Cerro Tololo as reference
}

# Minimum altitude (degrees above horizon) for observable targets.
MIN_ALTITUDE_DEG = 20.0


def is_observable(dec: float, instrument: str, min_altitude: float = MIN_ALTITUDE_DEG) -> bool:
    """Check if a target (declination in degrees) is observable with an instrument.

    Based on the observatory's latitude and a minimum altitude constraint.
    Returns True if the target can reach min_altitude above the horizon at the site.
    """
    obs_lat = OBSERVATORY_LOCATIONS.get(instrument)
    if obs_lat is None:
        return True  # Unknown instrument, assume observable

    # At transit (target on meridian), altitude = 90 - |obs_lat - dec|
    # For observation, we need: altitude >= min_altitude
    # So: 90 - |obs_lat - dec| >= min_altitude
    # Therefore: |obs_lat - dec| <= 90 - min_altitude
    # Which means: obs_lat - (90 - min_altitude) <= dec <= obs_lat + (90 - min_altitude)

    max_zenith = 90.0 - min_altitude
    min_observable_dec = obs_lat - max_zenith
    max_observable_dec = obs_lat + max_zenith

    return min_observable_dec <= dec <= max_observable_dec


def get_observable_range(instrument: str, min_altitude: float = MIN_ALTITUDE_DEG) -> tuple[float, float]:
    """Return (min_dec, max_dec) observable range for an instrument."""
    obs_lat = OBSERVATORY_LOCATIONS.get(instrument, 0.0)
    max_zenith = 90.0 - min_altitude
    return (obs_lat - max_zenith, obs_lat + max_zenith)

# --- Comparison-star scoring knobs -----------------------------------------
# Magnitude offset (comp - target) where a comparison is "ideal". Slightly
# fainter than the target is preferred: bright comps shorten the usable
# exposure, faint comps are photon-starved.
IDEAL_DMAG = 0.3
DMAG_SIGMA = 1.2
# Comparisons brighter than the target beyond this many mag are penalised hard
# because they cap the exposure time (saturation risk).
BRIGHT_DMAG_LIMIT = -1.0
BRIGHT_PENALTY = 0.3
# Color (BP-RP) similarity improves differential-photometry quality.
COLOR_SIGMA = 0.6
# Below this weight a star is not worth counting as a comparison.
WEIGHT_FLOOR = 0.05
# A comp blended with a comparably-bright neighbour within this radius is
# downweighted (crowding hurts aperture photometry).
BLEND_ARCSEC = 12.0
BLEND_DMAG = 2.0

# --- Search defaults --------------------------------------------------------
# Keep the target at least this far from any field edge.
DEFAULT_MARGIN_ARCSEC = 30.0
# Grid resolution for the field-center offset search (per axis).
DEFAULT_OFFSET_STEPS = 13
# Position-angle samples. A square has 90-deg symmetry, so 0..90 suffices.
DEFAULT_PA_STEP_DEG = 15.0
# How far (in units of the field half-width) past the field to pull comparison
# candidates, so off-center pointings still see their neighbourhood.
QUERY_RADIUS_FACTOR = 1.6


# ===========================================================================
# Footprint geometry
# ===========================================================================
def has_footprint(instrument: str) -> bool:
    """True if a footprint (XML or computed fallback) is defined for ``instrument``."""
    return instrument in INSTRUMENT_FOV_FILES or instrument in _FALLBACK_HALF_ARCSEC


def fov_xml_path(instrument: str) -> Path | None:
    """Absolute path to an instrument's footprint XML, or None if it has none."""
    fname = INSTRUMENT_FOV_FILES.get(instrument)
    return (_DATA_DIR / fname) if fname else None


def load_fov_halfsize_arcsec(instrument: str) -> float:
    """Half-width (arcsec) of the square footprint for ``instrument``.

    Parses the VOTable polygon vertices and returns the largest |coordinate|
    (the half-side of the square). Falls back to a computed detector size for
    instruments without an XML footprint.
    """
    path = fov_xml_path(instrument)
    if path is None or not path.exists():
        half = _FALLBACK_HALF_ARCSEC.get(instrument)
        if half is None:
            msg = f"No footprint definition for instrument {instrument!r}"
            raise ValueError(msg)
        return float(half)

    tree = ET.parse(path)
    # VOTable is namespaced; match on the local tag name to stay version-agnostic.
    coords: list[float] = []
    for td in tree.iter():
        if td.tag.split("}")[-1] == "TD" and td.text is not None:
            try:
                coords.append(abs(float(td.text)))
            except ValueError:
                continue
    if not coords:
        msg = f"No polygon vertices found in {path}"
        raise ValueError(msg)
    return max(coords)


# ===========================================================================
# Tangent-plane transforms (gnomonic / TAN projection)
# ===========================================================================
def radec_to_tangent(
    ra: np.ndarray, dec: np.ndarray, ra0: float, dec0: float
) -> tuple[np.ndarray, np.ndarray]:
    """Project (ra, dec) deg onto a tangent plane at (ra0, dec0).

    Returns ``(east, north)`` standard coordinates in arcsec, with East along
    +x (increasing toward larger RA on the sky) and North along +y.
    """
    rad = math.pi / 180.0
    ra = np.atleast_1d(np.asarray(ra, dtype=float)) * rad
    dec = np.atleast_1d(np.asarray(dec, dtype=float)) * rad
    ra0r, dec0r = ra0 * rad, dec0 * rad
    cos_c = np.sin(dec0r) * np.sin(dec) + np.cos(dec0r) * np.cos(dec) * np.cos(ra - ra0r)
    # Standard coordinates (radians), East-positive.
    xi = np.cos(dec) * np.sin(ra - ra0r) / cos_c
    eta = (np.cos(dec0r) * np.sin(dec) - np.sin(dec0r) * np.cos(dec) * np.cos(ra - ra0r)) / cos_c
    arcsec = 180.0 / math.pi * 3600.0
    return xi * arcsec, eta * arcsec


def tangent_to_radec(
    east: float, north: float, ra0: float, dec0: float
) -> tuple[float, float]:
    """Inverse gnomonic projection: tangent-plane arcsec -> (ra, dec) deg."""
    rad = math.pi / 180.0
    arcsec = 180.0 / math.pi * 3600.0
    xi = east / arcsec
    eta = north / arcsec
    dec0r = dec0 * rad
    rho = math.hypot(xi, eta)
    if rho == 0.0:
        return ra0 % 360.0, dec0
    c = math.atan(rho)
    sin_c, cos_c = math.sin(c), math.cos(c)
    dec = math.asin(cos_c * math.sin(dec0r) + eta * sin_c * math.cos(dec0r) / rho)
    ra = ra0 * rad + math.atan2(
        xi * sin_c, rho * math.cos(dec0r) * cos_c - eta * math.sin(dec0r) * sin_c
    )
    return (ra / rad) % 360.0, dec / rad


def inside_square(
    east: np.ndarray,
    north: np.ndarray,
    cx: float,
    cy: float,
    half: float,
    pa_deg: float,
) -> np.ndarray:
    """Boolean mask: which tangent-plane points fall in the rotated square.

    The field is a square of half-width ``half`` centered at ``(cx, cy)`` and
    rotated by ``pa_deg`` (position angle, North through East).
    """
    pa = math.radians(pa_deg)
    cos_p, sin_p = math.cos(pa), math.sin(pa)
    rx = east - cx
    ry = north - cy
    xr = rx * cos_p + ry * sin_p
    yr = -rx * sin_p + ry * cos_p
    return (np.abs(xr) <= half) & (np.abs(yr) <= half)


def footprint_corners_radec(
    cx: float, cy: float, half: float, pa_deg: float, ra0: float, dec0: float
) -> list[list[float]]:
    """Four corners (closed is left to the caller) of the field as [ra, dec]."""
    pa = math.radians(pa_deg)
    cos_p, sin_p = math.cos(pa), math.sin(pa)
    corners = []
    for sx, sy in ((-1, -1), (1, -1), (1, 1), (-1, 1)):
        lx, ly = sx * half, sy * half
        # Rotate the local square corner into the tangent plane, then offset.
        east = cx + lx * cos_p - ly * sin_p
        north = cy + lx * sin_p + ly * cos_p
        ra, dec = tangent_to_radec(east, north, ra0, dec0)
        corners.append([ra, dec])
    return corners


# ===========================================================================
# Comparison-star scoring
# ===========================================================================
def comparison_weights(
    gmag: np.ndarray,
    bp_rp: np.ndarray | None,
    target_g: float,
    target_bp_rp: float | None,
) -> np.ndarray:
    """Weight each star by how useful it is as a comparison for the target.

    Combines a magnitude term (Gaussian around a slightly-fainter-than-target
    optimum, with an extra penalty for comps bright enough to cap the exposure)
    and an optional color-similarity term.
    """
    gmag = np.asarray(gmag, dtype=float)
    dmag = gmag - target_g
    w = np.exp(-0.5 * ((dmag - IDEAL_DMAG) / DMAG_SIGMA) ** 2)
    # Hard penalty on comps brighter than the target past the saturation margin.
    too_bright = dmag < BRIGHT_DMAG_LIMIT
    w = np.where(too_bright, w * BRIGHT_PENALTY, w)

    if bp_rp is not None and target_bp_rp is not None:
        bp_rp = np.asarray(bp_rp, dtype=float)
        cw = np.exp(-0.5 * ((bp_rp - target_bp_rp) / COLOR_SIGMA) ** 2)
        # Stars with missing color (NaN) keep a neutral factor.
        cw = np.where(np.isfinite(bp_rp), cw, 1.0)
        w = w * cw
    return w


def _blend_penalty(
    east: np.ndarray, north: np.ndarray, gmag: np.ndarray, weights: np.ndarray
) -> np.ndarray:
    """Downweight stars crowded by a comparably-bright neighbour."""
    n = len(weights)
    if n < 2:
        return weights
    out = weights.copy()
    for i in range(n):
        dx = east - east[i]
        dy = north - north[i]
        near = (dx * dx + dy * dy) <= BLEND_ARCSEC * BLEND_ARCSEC
        near[i] = False
        if np.any(near & (np.abs(gmag - gmag[i]) <= BLEND_DMAG)):
            out[i] *= 0.4
    return out


# ===========================================================================
# Pointing + orientation search
# ===========================================================================
@dataclass
class FovSolution:
    center_east: float
    center_north: float
    pa_deg: float
    score: float
    in_field: np.ndarray  # boolean mask over the candidate stars
    half_arcsec: float
    margin_arcsec: float


def optimize_pointing(
    east: np.ndarray,
    north: np.ndarray,
    weights: np.ndarray,
    half: float,
    margin: float = DEFAULT_MARGIN_ARCSEC,
    offset_steps: int = DEFAULT_OFFSET_STEPS,
    pa_step_deg: float = DEFAULT_PA_STEP_DEG,
    comp_margin: float | None = None,
    avoid_east: np.ndarray | None = None,
    avoid_north: np.ndarray | None = None,
) -> FovSolution:
    """Grid-search the field center offset and PA maximizing in-field weight.

    The target is at the tangent-plane origin and must stay inside the field by
    at least ``margin``. Comparisons are counted if they fall inside the field
    by at least ``comp_margin`` (defaults to ``margin`` if not specified).

    If ``avoid_east``/``avoid_north`` are given (tangent-plane positions of stars
    too bright to tolerate in the field), any pointing whose footprint contains
    one of them is rejected. When every candidate pointing is rejected the
    returned solution keeps its sentinel ``score`` of ``-1.0`` so the caller can
    report infeasibility.
    """
    if half <= margin:
        msg = f"margin ({margin}) must be smaller than the field half-width ({half})"
        raise ValueError(msg)

    comp_margin = comp_margin if comp_margin is not None else margin

    has_avoid = avoid_east is not None and len(avoid_east) > 0

    reach = half - margin
    offs = np.linspace(-reach, reach, offset_steps)
    pas = np.arange(0.0, 90.0, pa_step_deg)

    best = FovSolution(0.0, 0.0, 0.0, -1.0, np.zeros(len(weights), bool), half, margin)
    for pa in pas:
        for cx in offs:
            for cy in offs:
                # Require the target (origin) to be inside with the edge margin.
                if not inside_square(
                    np.array([0.0]), np.array([0.0]), cx, cy, half - margin, pa
                )[0]:
                    continue
                # Reject pointings that admit a too-bright star anywhere in the
                # (full) footprint.
                if has_avoid and inside_square(
                    avoid_east, avoid_north, cx, cy, half, pa
                ).any():
                    continue
                # Comparisons inside by at least comp_margin.
                mask = inside_square(east, north, cx, cy, half - comp_margin, pa)
                score = float(weights[mask].sum())
                if score > best.score:
                    best = FovSolution(
                        float(cx), float(cy), float(pa), score, mask, half, margin
                    )
    return best


# ===========================================================================
# Catalog query (Gaia DR3 via Vizier)
# ===========================================================================
# VizieR signals a server-side failure (e.g. its own database backend being
# unreachable) as an HTTP 200 VOTable with QUERY_STATUS=ERROR and one or more
# <INFO name="Error" value="..."/> nodes, rather than a non-2xx response or a
# raised exception. Left undetected, this looks identical to "no sources in
# this field" to astroquery (it parses to an empty TableList either way).
_VIZIER_QUERY_STATUS_ERROR_RE = re.compile(r'name="QUERY_STATUS"\s+value="ERROR"')
_VIZIER_ERROR_INFO_RE = re.compile(r'<INFO\b[^>]*\bname="Error"[^>]*\bvalue="([^"]*)"')
_VIZIER_ERROR_NOISE = {"", "--", "-- no connection"}


def _vizier_server_error(response_text: str) -> str | None:
    """Human-readable message if a VizieR response reports a server error.

    Returns ``None`` for a normal response (which may still contain zero
    matching sources for the query).
    """
    if not _VIZIER_QUERY_STATUS_ERROR_RE.search(response_text):
        return None
    for match in _VIZIER_ERROR_INFO_RE.finditer(response_text):
        detail = match.group(1).strip()
        if detail not in _VIZIER_ERROR_NOISE:
            return detail
    return "VizieR reported an unspecified server error"


@dataclass
class StarField:
    ra: np.ndarray
    dec: np.ndarray
    gmag: np.ndarray
    bp_rp: np.ndarray
    source: str = ""
    error: str | None = None
    # Proper motion (mas/yr). ``pmra`` is Gaia's convention: mu_alpha* = mu_alpha
    # * cos(dec), i.e. already a true angular rate in the eastward direction.
    # Appended after ``error`` (rather than grouped with gmag/bp_rp) so every
    # existing positional ``StarField(...)`` call site stays valid.
    pmra: np.ndarray = field(default_factory=lambda: np.array([]))
    pmdec: np.ndarray = field(default_factory=lambda: np.array([]))

    def __len__(self) -> int:
        return len(self.ra)


def _query_gaia_esa(
    ra: float, dec: float, radius_arcsec: float, min_mag: float, max_mag: float
) -> StarField:
    """Cone-search Gaia DR3 via the official ESA archive (TAP/ADQL).

    This is independent of CDS/VizieR, so it stays available during a VizieR
    outage (and vice versa). Slower than the VizieR cone-search (a TAP job
    round trip typically takes several seconds to tens of seconds).
    """
    empty = StarField(
        np.array([]), np.array([]), np.array([]), np.array([]), "Gaia DR3 (ESA)"
    )
    try:
        from astroquery.gaia import Gaia
    except Exception as exc:  # pragma: no cover - import guard
        empty.error = f"astroquery.gaia unavailable: {exc}"
        return empty

    radius_deg = radius_arcsec / 3600.0
    mag_clause = (
        f"phot_g_mean_mag BETWEEN {min_mag} AND {max_mag}"
        if min_mag > 0
        else f"phot_g_mean_mag < {max_mag}"
    )
    query = (
        "SELECT TOP 10000 ra, dec, phot_g_mean_mag, bp_rp, pmra, pmdec "
        "FROM gaiadr3.gaia_source WHERE "
        f"1=CONTAINS(POINT('ICRS', ra, dec), CIRCLE('ICRS', {ra}, {dec}, {radius_deg})) "
        f"AND {mag_clause} "
        "ORDER BY phot_g_mean_mag"
    )
    try:
        tab = Gaia.launch_job_async(query).get_results()
    except Exception as exc:
        empty.error = f"ESA Gaia query failed: {exc}"
        logger.warning("ESA Gaia query failed for (%.4f, %.4f): %s", ra, dec, exc)
        return empty

    return StarField(
        ra=np.asarray(tab["ra"], dtype=float),
        dec=np.asarray(tab["dec"], dtype=float),
        gmag=np.asarray(tab["phot_g_mean_mag"], dtype=float),
        bp_rp=np.asarray(tab["bp_rp"], dtype=float),
        pmra=np.asarray(tab["pmra"], dtype=float),
        pmdec=np.asarray(tab["pmdec"], dtype=float),
        source="Gaia DR3 (ESA)",
    )


def _query_gaia_vizier(
    ra: float, dec: float, radius_arcsec: float, min_mag: float, max_mag: float
) -> StarField:
    """Cone-search Gaia DR3 via the VizieR mirror (catalog I/355/gaiadr3)."""
    empty = StarField(
        np.array([]), np.array([]), np.array([]), np.array([]), "Gaia DR3 (VizieR)"
    )
    try:
        import astropy.units as u
        from astropy.coordinates import SkyCoord
        from astroquery.vizier import Vizier
    except Exception as exc:  # pragma: no cover - import guard
        empty.error = f"astroquery/astropy unavailable: {exc}"
        return empty

    try:
        coord = SkyCoord(ra=ra, dec=dec, unit=(u.deg, u.deg), frame="icrs")
        gmag_filter = f"{min_mag}..{max_mag}" if min_mag > 0 else f"<{max_mag}"
        viz = Vizier(
            columns=["RA_ICRS", "DE_ICRS", "Gmag", "BP-RP", "pmRA", "pmDE", "_r"],
            column_filters={"Gmag": gmag_filter},
            row_limit=-1,
        )
        response = viz.query_region_async(
            coord, radius=radius_arcsec * u.arcsec, catalog="I/355/gaiadr3"
        )
    except Exception as exc:
        empty.error = f"Gaia query failed: {exc}"
        logger.warning("Gaia query failed for (%.4f, %.4f): %s", ra, dec, exc)
        return empty

    server_error = _vizier_server_error(response.text)
    if server_error:
        empty.error = f"VizieR server error: {server_error}"
        logger.warning(
            "VizieR server error for (%.4f, %.4f): %s", ra, dec, server_error
        )
        return empty

    try:
        result = viz._parse_result(response)
    except Exception as exc:
        empty.error = f"Gaia response parse failed: {exc}"
        logger.warning("Gaia response parse failed for (%.4f, %.4f): %s", ra, dec, exc)
        return empty

    if not result or "I/355/gaiadr3" not in [t.meta.get("name") for t in result]:
        empty.error = "No Gaia sources returned"
        return empty

    tab = result["I/355/gaiadr3"]
    return StarField(
        ra=np.asarray(tab["RA_ICRS"], dtype=float),
        dec=np.asarray(tab["DE_ICRS"], dtype=float),
        gmag=np.asarray(tab["Gmag"], dtype=float),
        bp_rp=np.asarray(tab["BP-RP"], dtype=float),
        pmra=np.asarray(tab["pmRA"], dtype=float),
        pmdec=np.asarray(tab["pmDE"], dtype=float),
        source="Gaia DR3 (VizieR)",
    )


def query_gaia_field(
    ra: float,
    dec: float,
    radius_arcsec: float,
    min_mag: float = 0.0,
    max_mag: float = 18.0,
    mag_limit: float | None = None,
) -> StarField:
    """Cone-search Gaia DR3 around (ra, dec).

    Tries the official ESA archive first (authoritative, but a TAP job round
    trip can take several seconds); falls back to the VizieR mirror
    (I/355/gaiadr3) if the ESA archive is unavailable. Returns a
    :class:`StarField`; on failure the arrays are empty and ``error``
    explains why (callers should surface it rather than crash).
    """
    if mag_limit is not None:
        max_mag = mag_limit

    esa_stars = _query_gaia_esa(ra, dec, radius_arcsec, min_mag, max_mag)
    if esa_stars.error is None:
        return esa_stars

    logger.warning(
        "Falling back to VizieR for (%.4f, %.4f) after ESA archive error: %s",
        ra, dec, esa_stars.error,
    )
    vizier_stars = _query_gaia_vizier(ra, dec, radius_arcsec, min_mag, max_mag)
    if vizier_stars.error is None:
        return vizier_stars

    vizier_stars.error = (
        f"ESA Gaia archive failed ({esa_stars.error}); "
        f"VizieR fallback also failed ({vizier_stars.error})"
    )
    return vizier_stars


# Gaia DR3 astrometry/photometry at a fixed sky position is effectively static
# (proper motion is negligible at cone-search radii over the timescales this
# cache lives for), so successful cone-searches are cached with no expiry,
# just an LRU size cap. Repeated re-optimizations of the same target/instrument
# (different margin, PA, or magnitude filter) issue the identical cone-search,
# since none of those knobs affect the query radius or mag range.
_GAIA_CACHE_MAX = int(os.environ.get("MUSCAT_GAIA_CACHE_MAX", "512"))
_gaia_cache = LRUCache(maxsize=_GAIA_CACHE_MAX)


def _gaia_cache_key(
    ra: float, dec: float, radius_arcsec: float, min_mag: float, max_mag: float
) -> tuple:
    # Round to bucket near-duplicate requests together: 1e-4 deg (~0.36") is
    # far finer than any FOV footprint, so this never conflates distinct
    # pointings while still absorbing float noise between repeated calls.
    return (
        round(ra, 4), round(dec, 4), round(radius_arcsec, 1),
        round(min_mag, 2), round(max_mag, 2),
    )


def cached_query_gaia_field(
    ra: float,
    dec: float,
    radius_arcsec: float,
    min_mag: float = 0.0,
    max_mag: float = 18.0,
    mag_limit: float | None = None,
) -> StarField:
    """Memoizing wrapper around :func:`query_gaia_field`.

    Only successful lookups are cached; a failed query (ESA and VizieR both
    down, or a transient network error) is never stored, so it doesn't stay
    "stuck" for other requests hitting the same field until the process
    restarts.
    """
    if mag_limit is not None:
        max_mag = mag_limit
    key = _gaia_cache_key(ra, dec, radius_arcsec, min_mag, max_mag)
    cached = _gaia_cache.get(key)
    if cached is not None:
        return cached
    result = query_gaia_field(ra, dec, radius_arcsec, min_mag=min_mag, max_mag=max_mag)
    if result.error is None:
        _gaia_cache[key] = result
    return result


# ===========================================================================
# Top-level orchestration
# ===========================================================================
@dataclass
class FovResult:
    ok: bool
    instrument: str
    error: str | None = None
    target: str = ""
    ra: float = math.nan
    dec: float = math.nan
    target_gmag: float = math.nan
    target_bp_rp: float | None = None
    target_pmra: float | None = None
    target_pmdec: float | None = None
    fov_arcsec: float = math.nan
    fov_half_arcsec: float = math.nan
    margin_arcsec: float = DEFAULT_MARGIN_ARCSEC
    center_ra: float = math.nan
    center_dec: float = math.nan
    pa_deg: float = 0.0
    offset_east_arcsec: float = 0.0
    offset_north_arcsec: float = 0.0
    footprint: list[list[float]] = field(default_factory=list)
    comps: list[dict] = field(default_factory=list)
    n_comps: int = 0
    total_weight: float = 0.0
    catalog: str = "Gaia DR3"
    avoid_mag: float | None = None
    n_avoided: int = 0
    avoided: list[dict] = field(default_factory=list)

    def to_dict(self) -> dict:
        d = self.__dict__.copy()
        # Replace NaNs with None so the payload is valid JSON.
        for k, v in d.items():
            if isinstance(v, float) and math.isnan(v):
                d[k] = None
        return d


def _pm_at(arr: np.ndarray, i: int, n: int) -> float | None:
    """Proper motion component at index ``i``, or ``None`` if unavailable.

    Guards against :class:`StarField` instances built without ``pmra``/
    ``pmdec`` (e.g. older callers or test doubles), where the array is empty
    rather than length-``n``, and against Gaia sources lacking a 5-parameter
    astrometric solution (stored as NaN).
    """
    if len(arr) != n:
        return None
    v = float(arr[i])
    return v if math.isfinite(v) else None


def _identify_target_star(
    field_stars: StarField, ra: float, dec: float, match_arcsec: float = 3.0
) -> int | None:
    """Index of the Gaia source nearest (ra, dec) within match_arcsec, else None."""
    if len(field_stars) == 0:
        return None
    east, north = radec_to_tangent(field_stars.ra, field_stars.dec, ra, dec)
    d2 = east * east + north * north
    i = int(np.argmin(d2))
    return i if d2[i] <= match_arcsec * match_arcsec else None


def optimize(
    instrument: str,
    target: str = "",
    ra: float | None = None,
    dec: float | None = None,
    target_gmag: float | None = None,
    margin_arcsec: float = DEFAULT_MARGIN_ARCSEC,
    comp_margin_arcsec: float | None = None,
    mag_limit: float = 18.0,
    max_comps: int = 60,
    pa_step_deg: float | None = None,
    sinistro_mode: str | None = None,
    min_mag: float = 0.0,
    max_mag: float = 18.0,
    mag_delta: float | None = None,
    avoid_mag: float | None = None,
) -> FovResult:
    """Resolve a target, pull Gaia neighbours, and optimize pointing + PA.

    Either ``target`` (resolvable name) or explicit ``ra``/``dec`` (deg) must be
    given. Returns a :class:`FovResult` suitable for JSON serialization and for
    overplotting in Aladin Lite.

    The Gaia cone-search itself goes through :func:`cached_query_gaia_field`,
    so re-optimizing the same target/instrument (e.g. after tweaking the
    margin or magnitude filter, which don't change the query) skips the
    network round trip.

    If ``pa_step_deg`` is None, uses :const:`DEFAULT_PA_STEP_DEG`. Set to a
    large value (e.g. 180) to fix PA at 0° (no rotation).

    If ``comp_margin_arcsec`` is None, defaults to ``margin_arcsec``.

    For Sinistro, ``sinistro_mode`` selects "full_frame" (26'x26') or
    "central_2k_2x2" (13'x13', default).

    If ``avoid_mag`` is given, any pointing whose footprint contains a star
    brighter than ``avoid_mag`` (Gmag) is rejected; the science target itself is
    exempt. If no pointing can avoid every such star, the result carries an
    error explaining the infeasibility.

    If the resolved target declination never reaches
    :const:`MIN_ALTITUDE_DEG` above the horizon at ``instrument``'s site, the
    result carries an error instead of querying Gaia for an unreachable field.
    """
    if mag_limit != 18.0 and max_mag == 18.0:
        max_mag = mag_limit

    res = FovResult(ok=False, instrument=instrument, target=target)
    res.avoid_mag = avoid_mag

    # --- field size ---
    try:
        if instrument == "sinistro" and sinistro_mode in SINISTRO_MODES:
            half = SINISTRO_MODES[sinistro_mode]
        else:
            half = load_fov_halfsize_arcsec(instrument)
    except ValueError as exc:
        res.error = str(exc)
        return res
    res.fov_half_arcsec = half
    res.fov_arcsec = 2.0 * half
    res.margin_arcsec = margin_arcsec

    # --- resolve coordinates ---
    if ra is None or dec is None:
        if not target:
            res.error = "Provide a target name or ra/dec."
            return res
        from . import exposure  # lazy: pulls astroquery

        coords = exposure.resolve_target_coords(target)
        if coords is None:
            res.error = f"Could not resolve target {target!r}."
            return res
        ra, dec = coords
    res.ra, res.dec = float(ra), float(dec)

    # --- visibility from the instrument's site ---
    if not is_observable(res.dec, instrument):
        min_dec, max_dec = get_observable_range(instrument)
        res.error = (
            f"Target at dec={res.dec:.2f}\N{DEGREE SIGN} is not observable with "
            f"{instrument} (observable dec range: {min_dec:.1f}\N{DEGREE SIGN} to "
            f"{max_dec:.1f}\N{DEGREE SIGN})."
        )
        return res

    # --- candidate stars ---
    radius = half * math.sqrt(2.0) * QUERY_RADIUS_FACTOR

    query_min_mag = min_mag
    query_max_mag = max_mag
    if mag_delta is not None:
        query_min_mag = 0.0
        query_max_mag = max(max_mag, 18.0)
    if avoid_mag is not None:
        # Pull the bright stars we must steer around, even when they sit above
        # the comparison magnitude range.
        query_min_mag = 0.0
        query_max_mag = max(query_max_mag, avoid_mag)

    stars = cached_query_gaia_field(ra, dec, radius, min_mag=query_min_mag, max_mag=query_max_mag)
    if stars.error:
        res.error = stars.error
        return res
    res.catalog = stars.source or res.catalog
    if len(stars) == 0:
        res.error = "No catalog stars found near the target."
        return res

    # --- target photometry (from catalog match, or caller override) ---
    t_idx = _identify_target_star(stars, ra, dec)
    if target_gmag is not None:
        res.target_gmag = float(target_gmag)
    elif t_idx is not None:
        res.target_gmag = float(stars.gmag[t_idx])
    else:
        res.target_gmag = float(np.nanmedian(stars.gmag))
    if t_idx is not None and np.isfinite(stars.bp_rp[t_idx]):
        res.target_bp_rp = float(stars.bp_rp[t_idx])
    if t_idx is not None:
        n_stars = len(stars)
        res.target_pmra = _pm_at(stars.pmra, t_idx, n_stars)
        res.target_pmdec = _pm_at(stars.pmdec, t_idx, n_stars)

    # --- tangent plane + scoring (exclude the target itself) ---
    east, north = radec_to_tangent(stars.ra, stars.dec, ra, dec)
    weights = comparison_weights(
        stars.gmag, stars.bp_rp, res.target_gmag, res.target_bp_rp
    )
    if t_idx is not None:
        weights[t_idx] = 0.0
    weights = np.where(np.isfinite(weights), weights, 0.0)

    # Magnitude filtering mask
    mag_mask = np.ones(len(stars), dtype=bool)
    if mag_delta is not None:
        mag_mask &= (stars.gmag >= res.target_gmag - mag_delta) & (stars.gmag <= res.target_gmag + mag_delta)
    mag_mask &= (stars.gmag >= min_mag) & (stars.gmag <= max_mag)
    if t_idx is not None:
        mag_mask[t_idx] = True

    weights = np.where(mag_mask, weights, 0.0)
    weights = np.where(weights >= WEIGHT_FLOOR, weights, 0.0)
    weights = _blend_penalty(east, north, stars.gmag, weights)

    # --- too-bright stars to steer the field away from (target exempt) ---
    avoid_east = avoid_north = None
    avoid_idx = np.array([], dtype=int)
    if avoid_mag is not None:
        avoid_mask = np.isfinite(stars.gmag) & (stars.gmag < avoid_mag)
        if t_idx is not None:
            avoid_mask[t_idx] = False
        avoid_idx = np.where(avoid_mask)[0]
        avoid_east = east[avoid_mask]
        avoid_north = north[avoid_mask]

    # --- search ---
    try:
        sol = optimize_pointing(
            east, north, weights, half,
            margin=margin_arcsec,
            pa_step_deg=pa_step_deg or DEFAULT_PA_STEP_DEG,
            comp_margin=comp_margin_arcsec,
            avoid_east=avoid_east,
            avoid_north=avoid_north,
        )
    except ValueError as exc:
        res.error = str(exc)
        return res

    # A negative score means no candidate pointing satisfied the constraints.
    if sol.score < 0:
        if avoid_mag is not None and len(avoid_idx):
            res.error = (
                f"No pointing keeps the target in the field while avoiding all "
                f"{len(avoid_idx)} star(s) brighter than Gmag {avoid_mag:g}. "
                f"Try a fainter 'avoid brighter than' limit."
            )
        else:
            res.error = "No valid pointing found for the given constraints."
        return res

    res.pa_deg = sol.pa_deg
    res.offset_east_arcsec = sol.center_east
    res.offset_north_arcsec = sol.center_north
    res.center_ra, res.center_dec = tangent_to_radec(
        sol.center_east, sol.center_north, ra, dec
    )
    res.footprint = footprint_corners_radec(
        sol.center_east, sol.center_north, half, sol.pa_deg, ra, dec
    )

    # --- comparison list (in-field, weighted, brightest-useful first) ---
    in_field = sol.in_field & (weights > 0)
    idx = np.where(in_field)[0]
    idx = idx[np.argsort(-weights[idx])][:max_comps]
    n_stars = len(stars)
    comps = []
    for i in idx:
        comps.append(
            {
                "ra": float(stars.ra[i]),
                "dec": float(stars.dec[i]),
                "gmag": float(stars.gmag[i]),
                "bp_rp": (float(stars.bp_rp[i]) if np.isfinite(stars.bp_rp[i]) else None),
                "weight": round(float(weights[i]), 3),
                "dmag": round(float(stars.gmag[i] - res.target_gmag), 2),
                "sep_arcsec": round(float(math.hypot(east[i], north[i])), 1),
                "pmra": _pm_at(stars.pmra, i, n_stars),
                "pmdec": _pm_at(stars.pmdec, i, n_stars),
            }
        )
    res.comps = comps
    res.n_comps = len(comps)
    res.total_weight = round(float(weights[in_field].sum()), 2)

    # --- too-bright stars the field was steered around (for display) ---
    avoided = []
    for i in avoid_idx:
        avoided.append(
            {
                "ra": float(stars.ra[i]),
                "dec": float(stars.dec[i]),
                "gmag": float(stars.gmag[i]),
                "sep_arcsec": round(float(math.hypot(east[i], north[i])), 1),
            }
        )
    avoided.sort(key=lambda s: s["gmag"])
    res.avoided = avoided
    res.n_avoided = len(avoided)

    res.ok = True
    return res
