"""Transit observability across the LCO network (self-contained, astropy-only).

Given a target and a list of predicted transit windows, classify each transit as
``full`` / ``partial`` / ``none`` from the relevant LCO sites, and produce the
time-series a frontend needs to draw a visibility plot (target + moon altitude,
twilight, the airmass limit, and the shaded transit interval).

No new dependency: astropy is already required. The LCO site coordinates are
frozen below (resolved once from astropy's site registry, sourced against
prose2's ``.telescope`` files) so there is no site-registry lookup at request
time. The NASA TransitView tool is deliberately *not* used here.
"""

from __future__ import annotations

import datetime
import math

# Frozen LCO site coordinates (lat_deg, lon_deg, height_m). 2 m values match
# prose2/data/muscat{3,4}_*.telescope; 1 m values from astropy's site registry.
LCO_SITES: dict[str, tuple[float, float, float]] = {
    "ogg": (20.71552, -156.16900, 3048),    # Haleakala, Maui (FTN / MuSCAT3, 2m)
    "coj": (-31.27336, 149.06119, 1149),    # Siding Spring, AU (FTS / MuSCAT4, 2m + 1m)
    "lsc": (-30.16528, -70.81500, 2215),    # Cerro Tololo, Chile (1m)
    "cpt": (-32.37582, 20.81081, 1798),     # Sutherland, South Africa (1m)
    "tfn": (28.30000, -16.50972, 2390),     # Teide, Tenerife (1m)
    "elp": (30.67167, -104.02167, 2075),    # McDonald, Texas (1m)
}

# Which sites host each schedulable instrument kind.
SITES_FOR_KIND: dict[str, list[str]] = {
    "muscat": ["ogg", "coj"],                       # 2M0-SCICAM-MUSCAT (MuSCAT3/4)
    "muscat3": ["ogg"],
    "muscat4": ["coj"],
    "sinistro": ["lsc", "cpt", "coj", "tfn", "elp"],  # 1m network
}

# Named twilight options -> sun altitude limit (deg). A sample counts as "night"
# when the Sun is below this altitude.
TWILIGHT_LIMITS: dict[str, float] = {
    "civil": -6.0,
    "nautical": -12.0,
    "astronomical": -18.0,
}
DEFAULT_TWILIGHT = "nautical"

_PLOT_HALF_WINDOW_H = 8.0   # hours each side of mid-transit for the plot grid
_PLOT_STEP_MIN = 5.0        # plot sampling cadence (minutes)
_CLASSIFY_STEP_MIN = 3.0    # classification sampling cadence (minutes)


class TransitObsError(RuntimeError):
    """Boundary error for observability requests."""

    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.status = status


def alt_limit_from_airmass(max_airmass: float) -> float:
    """Minimum altitude (deg) implied by a max airmass via the plane-parallel
    relation airmass = sec(z): alt = 90 - arccos(1/airmass)."""
    if max_airmass is None or max_airmass < 1.0:
        raise TransitObsError("max_airmass must be >= 1.0", 400)
    return 90.0 - math.degrees(math.acos(min(1.0, 1.0 / float(max_airmass))))


def twilight_limit(twilight: str | None) -> float:
    key = (twilight or DEFAULT_TWILIGHT).strip().lower()
    if key not in TWILIGHT_LIMITS:
        raise TransitObsError(
            f"twilight must be one of {sorted(TWILIGHT_LIMITS)}", 400
        )
    return TWILIGHT_LIMITS[key]


def sites_for_kind(kind: str) -> list[str]:
    sites = SITES_FOR_KIND.get((kind or "").strip().lower())
    if not sites:
        raise TransitObsError(f"unknown instrument kind: {kind!r}", 400)
    return sites


def _earth_location(site: str):
    from astropy.coordinates import EarthLocation
    import astropy.units as u

    lat, lon, height = LCO_SITES[site]
    return EarthLocation(lat=lat * u.deg, lon=lon * u.deg, height=height * u.m)


def _parse_iso_utc(value: str):
    """Parse an ISO-8601 UTC string to an astropy Time (UTC scale)."""
    from astropy.time import Time

    s = ("" if value is None else str(value)).strip().replace("Z", "")
    try:
        # Validate/normalise via datetime, then hand a clean string to astropy.
        datetime.datetime.fromisoformat(s)
    except ValueError:
        raise TransitObsError(f"could not parse time: {value!r}", 400)
    return Time(s, format="isot", scale="utc")


def _observable_mask(target, location, times, alt_min, sun_alt_max, moon_sep_min):
    """Boolean array: True where the target is observable at each time.

    Observable = target above ``alt_min`` AND Sun below ``sun_alt_max`` AND
    (if ``moon_sep_min`` > 0) Moon farther than ``moon_sep_min`` from the target.
    Returns ``(mask, target_alt, moon_alt, sun_alt, moon_sep)`` as numpy arrays.
    """
    import numpy as np
    from astropy.coordinates import AltAz, get_body, get_sun

    altaz = AltAz(obstime=times, location=location)
    target_altaz = target.transform_to(altaz)
    target_alt = target_altaz.alt.deg
    sun_alt = get_sun(times).transform_to(altaz).alt.deg
    moon_altaz = get_body("moon", times, location).transform_to(altaz)
    moon_alt = moon_altaz.alt.deg
    # Topocentric on-sky separation (both in the same AltAz frame) — the correct
    # quantity for Moon-avoidance and free of frame-mismatch warnings.
    moon_sep = moon_altaz.separation(target_altaz).deg

    mask = (target_alt >= alt_min) & (sun_alt < sun_alt_max)
    if moon_sep_min and moon_sep_min > 0:
        mask = mask & (moon_sep >= moon_sep_min)
    return (np.asarray(mask), np.asarray(target_alt), np.asarray(moon_alt),
            np.asarray(sun_alt), np.asarray(moon_sep))


def classify_transits(
    ra_deg: float,
    dec_deg: float,
    windows: list[dict],
    kind: str,
    duration_hours: float,
    max_airmass: float = 2.0,
    twilight: str = DEFAULT_TWILIGHT,
    moon_sep_min: float = 30.0,
) -> list[dict]:
    """Classify each window's transit as full / partial / none across the kind's
    LCO sites. Returns a list aligned with ``windows``; each entry is
    ``{"rating", "sites", "best_site"}`` where ``sites`` lists sites with at
    least partial coverage and ``best_site`` is a full site if any, else the
    most-covered partial site.
    """
    import numpy as np
    import astropy.units as u
    from astropy.time import Time
    from astropy.coordinates import SkyCoord

    if not windows:
        return []
    duration_hours = float(duration_hours)
    if duration_hours <= 0:
        raise TransitObsError("duration must be positive", 400)
    alt_min = alt_limit_from_airmass(max_airmass)
    sun_alt_max = twilight_limit(twilight)
    sites = sites_for_kind(kind)
    target = SkyCoord(ra=ra_deg * u.deg, dec=dec_deg * u.deg)
    half = (duration_hours / 24.0) / 2.0  # days

    # Per-transit time grids (ingress..egress), concatenated for one transform
    # per site. n_samp samples per transit at ~_CLASSIFY_STEP_MIN cadence.
    n_samp = max(5, int(round(duration_hours * 60.0 / _CLASSIFY_STEP_MIN)) + 1)
    mids = np.array([Time(_parse_iso_utc(w["mid"])).jd for w in windows])
    offsets = np.linspace(-half, half, n_samp)
    all_jd = (mids[:, None] + offsets[None, :]).ravel()
    grid = Time(all_jd, format="jd", scale="utc")

    # site -> per-transit observable fraction
    frac = {}
    for site in sites:
        mask, *_ = _observable_mask(
            target, _earth_location(site), grid, alt_min, sun_alt_max, moon_sep_min
        )
        frac[site] = mask.reshape(len(windows), n_samp).mean(axis=1)

    results = []
    for i in range(len(windows)):
        full_sites = [s for s in sites if frac[s][i] >= 0.999]
        partial_sites = [s for s in sites if 0.0 < frac[s][i] < 0.999]
        if full_sites:
            rating, best = "full", full_sites[0]
        elif partial_sites:
            rating = "partial"
            best = max(partial_sites, key=lambda s: frac[s][i])
        else:
            rating, best = "none", None
        results.append({
            "rating": rating,
            "sites": full_sites + partial_sites,
            "best_site": best,
        })
    return results


def visibility_series(
    ra_deg: float,
    dec_deg: float,
    mid_iso: str,
    duration_hours: float,
    site: str,
    max_airmass: float = 2.0,
    twilight: str = DEFAULT_TWILIGHT,
    moon_sep_min: float = 30.0,
) -> dict:
    """Time-series for a Plotly visibility plot of one transit at one site.

    Returns ISO times plus target/moon/sun altitude arrays, the shaded transit
    interval (ingress/egress), the altitude limit, and the moon separation at
    mid-transit — everything the frontend needs to draw the plot.
    """
    import numpy as np
    import astropy.units as u
    from astropy.time import Time
    from astropy.coordinates import SkyCoord

    if site not in LCO_SITES:
        raise TransitObsError(f"unknown site: {site!r}", 400)
    duration_hours = float(duration_hours)
    if duration_hours <= 0:
        raise TransitObsError("duration must be positive", 400)
    alt_min = alt_limit_from_airmass(max_airmass)
    sun_alt_max = twilight_limit(twilight)

    target = SkyCoord(ra=ra_deg * u.deg, dec=dec_deg * u.deg)
    location = _earth_location(site)
    mid = Time(_parse_iso_utc(mid_iso))

    n = int(round(2 * _PLOT_HALF_WINDOW_H * 60.0 / _PLOT_STEP_MIN)) + 1
    offsets_h = np.linspace(-_PLOT_HALF_WINDOW_H, _PLOT_HALF_WINDOW_H, n)
    times = mid + offsets_h * u.hour

    mask, target_alt, moon_alt, sun_alt, moon_sep = _observable_mask(
        target, location, times, alt_min, sun_alt_max, moon_sep_min
    )
    half_h = duration_hours / 2.0
    ingress = (mid - half_h * u.hour).isot
    egress = (mid + half_h * u.hour).isot
    # Moon separation at mid-transit (topocentric, in the AltAz frame).
    from astropy.coordinates import get_body, AltAz
    mid_altaz = AltAz(obstime=mid, location=location)
    moon_sep_mid = float(
        get_body("moon", mid, location).transform_to(mid_altaz)
        .separation(target.transform_to(mid_altaz)).deg
    )

    def _round(arr):
        return [round(float(v), 2) for v in arr]

    return {
        "site": site,
        "times": [t.isot for t in times],
        "target_alt": _round(target_alt),
        "moon_alt": _round(moon_alt),
        "sun_alt": _round(sun_alt),
        "moon_sep": _round(moon_sep),
        "ingress": ingress,
        "egress": egress,
        "alt_limit": round(alt_min, 2),
        "sun_alt_limit": sun_alt_max,
        "moon_sep_min": float(moon_sep_min),
        "moon_sep_mid": round(moon_sep_mid, 1),
        "observable_fraction": round(float(np.asarray(mask).mean()), 3),
    }
