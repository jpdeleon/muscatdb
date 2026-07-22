"""Transit observability across the LCO network (self-contained, astropy-only).

Given a target and a list of predicted transit windows, classify each transit as
``full`` / ``split_gap`` / ``split_overlap`` / ``partial`` / ``none`` from the
relevant LCO sites, and produce the time-series a frontend needs to draw a
visibility plot (target + moon altitude, twilight, the airmass limit, and the
shaded transit interval). ``split_gap``/``split_overlap`` mean no single site
covers the whole transit, but two sites in relay (one covering ingress through
a handoff, the other the handoff through egress) do -- ``split_gap`` when a
real gap remains near the handoff, ``split_overlap`` when the two sites' own
coverage meets or overlaps with no gap. ``partial`` is reserved for the
single-site case where one site sees a transit *contact* (ingress or egress)
but not the whole transit, with no second site to help; a lone site that sees
only the mid-transit sliver (neither contact) is rated ``none``.

No new dependency: astropy is already required. The LCO site coordinates are
frozen below (resolved once from astropy's site registry, sourced against
prose2's ``.telescope`` files) so there is no site-registry lookup at request
time. The NASA TransitView tool is deliberately *not* used here.
"""

from __future__ import annotations

import datetime
import itertools
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


def resolve_site_list(
    sites: list[str] | None = None, kind: str | None = None
) -> list[str]:
    """Sites to evaluate, in priority order: an explicit ``sites`` list when
    given, else the selected instrument ``kind``'s sites, else the full LCO
    network. Site codes are normalised and de-duplicated; unknown codes raise.
    """
    if sites:
        out: list[str] = []
        for s in sites:
            key = (s or "").strip().lower()
            if key not in LCO_SITES:
                raise TransitObsError(f"unknown site: {key!r}", 400)
            if key not in out:
                out.append(key)
        return out
    if kind:
        return sites_for_kind(kind)
    return list(LCO_SITES)


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


# Below this illuminated fraction the Moon is treated as dark: the
# min-lunar-distance cut is skipped, because a near-new Moon adds negligible sky
# background. The cut is likewise skipped whenever the Moon is below the horizon.
_MOON_DARK_FRACTION = 0.10


def _moon_illuminated_fraction(sun, moon):
    """Illuminated fraction of the Moon's disc, 0 (new) .. 1 (full).

    Uses the Sun–Moon elongation and their distances (the phase-angle formula
    behind ``astroplan.moon_illumination``). Both ``sun`` and ``moon`` must be
    geocentric (as ``get_sun`` / ``get_body("moon", times)`` return) so the
    elongation is frame-consistent and warning-free.
    """
    import numpy as np

    elongation = sun.separation(moon)
    phase_angle = np.arctan2(
        sun.distance * np.sin(elongation),
        moon.distance - sun.distance * np.cos(elongation),
    )
    return np.asarray((1.0 + np.cos(phase_angle)) / 2.0, dtype=float)


def _observable_mask(target, location, times, alt_min, sun_alt_max,
                     moon_sep_min, max_lunar_phase=1.0):
    """Boolean array: True where the target is observable at each time.

    Observable = target above ``alt_min`` AND Sun below ``sun_alt_max`` AND
    (Moon illuminated fraction <= ``max_lunar_phase``) AND -- only when the Moon
    is above the horizon and at least ``_MOON_DARK_FRACTION`` illuminated -- the
    Moon is farther than ``moon_sep_min`` from the target. A below-horizon or
    near-new (dark) Moon therefore never rejects a window on separation alone.
    Returns ``(mask, target_alt, moon_alt, sun_alt, moon_sep, moon_illum)`` as
    numpy arrays.
    """
    import numpy as np
    from astropy.coordinates import AltAz, get_body, get_sun

    altaz = AltAz(obstime=times, location=location)
    target_altaz = target.transform_to(altaz)
    target_alt = target_altaz.alt.deg
    sun = get_sun(times)
    sun_alt = sun.transform_to(altaz).alt.deg
    moon_altaz = get_body("moon", times, location).transform_to(altaz)
    moon_alt = moon_altaz.alt.deg
    # Topocentric on-sky separation (both in the same AltAz frame) — the correct
    # quantity for Moon-avoidance and free of frame-mismatch warnings.
    moon_sep = moon_altaz.separation(target_altaz).deg
    # Illuminated fraction uses geocentric Sun & Moon (frame-consistent, and the
    # standard phase definition); the sub-degree topocentric parallax is moot.
    moon_illum = _moon_illuminated_fraction(sun, get_body("moon", times))

    mask = (target_alt >= alt_min) & (sun_alt < sun_alt_max)
    if max_lunar_phase is not None and max_lunar_phase < 1.0:
        # Mirror LCO's max_lunar_phase: never observe under a brighter Moon.
        mask = mask & (moon_illum <= max_lunar_phase)
    if moon_sep_min and moon_sep_min > 0:
        # Moonlight only matters when the Moon is up and not near-new.
        enforce = (moon_alt >= 0.0) & (moon_illum >= _MOON_DARK_FRACTION)
        mask = mask & (~enforce | (moon_sep >= moon_sep_min))
    return (np.asarray(mask), np.asarray(target_alt), np.asarray(moon_alt),
            np.asarray(sun_alt), np.asarray(moon_sep), np.asarray(moon_illum))


def _jd_to_iso_z(jd: float) -> str:
    """Format a JD as an ISO-8601 UTC string with a ``Z`` suffix (matches the
    ``mid``/``start``/``end`` formatting already used by ``lco.generate_windows``)."""
    from astropy.time import Time

    return Time(jd, format="jd", scale="utc").isot + "Z"


def _site_coverage_span(mask, offsets, start_jd: float, end_jd: float):
    """First/last ``True`` sample of a per-window boolean mask, as JD, plus
    whether the ``True`` run is contiguous (``fragmented=False``) or has gaps
    of its own (e.g. a moon-separation dip splitting one site's own coverage
    into two runs). Assumes ``mask`` has at least one ``True`` (only called for
    sites already known to have some coverage).
    """
    import numpy as np

    idx = np.flatnonzero(mask)
    first_idx, last_idx = int(idx[0]), int(idx[-1])
    fragmented = (last_idx - first_idx + 1) != idx.size
    span = end_jd - start_jd
    return (
        start_jd + span * offsets[first_idx],
        start_jd + span * offsets[last_idx],
        fragmented,
    )


def _contact_flags(mask_row, start_jd: float, end_jd: float, mid_jd, half: float):
    """Return ``(ingress_observed, egress_observed)`` for the bare-transit
    contacts (``mid ± half``) against one window's per-sample coverage over
    ``start_jd``..``end_jd``.

    The mid-transit is deliberately not consulted. Falls back to the grid edges
    when the midpoint is unknown (those edges are the bare-transit contacts by
    construction when padding is excluded).
    """
    n = len(mask_row)
    if mid_jd is None or end_jd <= start_jd:
        return bool(mask_row[0]), bool(mask_row[-1])

    def sample_at(jd: float) -> int:
        frac = (jd - start_jd) / (end_jd - start_jd)
        frac = min(1.0, max(0.0, frac))
        return int(round(frac * (n - 1)))

    return bool(mask_row[sample_at(mid_jd - half)]), bool(mask_row[sample_at(mid_jd + half)])


def _observes_contact(mask_row, start_jd: float, end_jd: float, mid_jd, half: float) -> bool:
    """True if a transit contact (ingress or egress) lands on an observable
    sample; the mid-transit is excluded (see :func:`_contact_flags`)."""
    ingress_obs, egress_obs = _contact_flags(mask_row, start_jd, end_jd, mid_jd, half)
    return ingress_obs or egress_obs


def classify_transits(
    ra_deg: float,
    dec_deg: float,
    windows: list[dict],
    kind: str,
    duration_hours: float,
    max_airmass: float = 2.0,
    twilight: str = DEFAULT_TWILIGHT,
    moon_sep_min: float = 30.0,
    max_lunar_phase: float = 1.0,
    include_padding: bool = False,
    sites: list[str] | None = None,
    pad_before_min: float = 0.0,
    pad_after_min: float = 0.0,
) -> list[dict]:
    """Classify each window's transit as full / split_gap / split_overlap /
    partial / none across a set of LCO sites. The sites are resolved by
    :func:`resolve_site_list`: an explicit ``sites`` list takes priority, else
    the instrument ``kind``'s sites, else the full LCO network. Returns a list
    aligned with ``windows``; each entry is at least ``{"rating", "sites",
    "best_site"}`` where ``sites`` lists sites with at least partial coverage
    and ``best_site`` is a full site if any, else the most-covered site.

    - ``full``: one site alone covers the whole transit (``frac >= 0.999``).
    - ``partial``: exactly one site has coverage, that coverage includes a
      transit contact (ingress or egress) but not the whole transit, and there
      is no second site to relay with. A lone site that observes only the
      middle -- neither contact -- is rated ``none`` instead (not useful).
    - ``split_gap`` / ``split_overlap``: two or more sites have coverage AND the
      best pair's union spans both contacts (ingress and egress) -- a genuine
      ingress→egress relay. ``split_gap`` means a real gap remains near the
      handoff (``split_gap_min`` minutes uncovered by either site);
      ``split_overlap`` means the pair's coverage meets or overlaps with no gap
      (``split_overlap_min`` minutes covered by both). Both also carry
      ``split_sites`` (``[early, late]``, chronological) and ``split_windows``
      (each site's own observable start/end + a ``fragmented`` flag). If the
      best pair's union misses a contact (the extra site only contributes a
      sliver away from the contacts), the transit falls back to the single-site
      rating: ``partial`` when the best site covers a contact, else ``none``.
    - ``none``: no site observes a transit contact -- either no site sees any
      part of the transit, or the only coverage is a single site's mid-transit
      sliver with neither ingress nor egress.

    When ``include_padding=True`` and ``pad_before_min``/``pad_after_min`` are
    given, split entries also get ``ingress_bracket_ok`` / ``egress_bracket_ok``:
    whether the *early* site's own coverage extends at least ``pad_before_min``
    minutes past ingress (not just up to it), and the *late* site's coverage
    extends at least ``pad_after_min`` minutes before egress. This catches a
    handoff landing right on a contact point, leaving one leg with no
    single-site baseline immediately around its own transit contact.

    By default the observability check spans only the transit itself
    (``mid ± duration/2``); the padded ``start``/``end`` are used purely for the
    table/plot span. Set ``include_padding=True`` to require the padded baseline
    (``start``..``end``) to be observable too, which makes ``full``/``split_*``
    stricter.
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
    site_list = resolve_site_list(sites, kind)
    target = SkyCoord(ra=ra_deg * u.deg, dec=dec_deg * u.deg)
    half = (duration_hours / 24.0) / 2.0  # days

    # Per-transit time grids, concatenated for one transform per site. Unless
    # ``include_padding`` is set, the grid covers only the bare transit
    # (mid ± duration/2); the padded start/end are span metadata, not part of
    # the observability test.
    starts = []
    ends = []
    mids = []  # bare-transit midpoint JD per window, for the ingress/egress bracket check
    for w in windows:
        if include_padding and "start" in w and "end" in w:
            starts.append(Time(_parse_iso_utc(w["start"])).jd)
            ends.append(Time(_parse_iso_utc(w["end"])).jd)
        elif "mid" in w:
            mid_jd = Time(_parse_iso_utc(w["mid"])).jd
            starts.append(mid_jd - half)
            ends.append(mid_jd + half)
        elif "start" in w and "end" in w:
            starts.append(Time(_parse_iso_utc(w["start"])).jd)
            ends.append(Time(_parse_iso_utc(w["end"])).jd)
        else:
            raise TransitObsError("window needs 'mid' or 'start'/'end'", 400)
        mids.append(Time(_parse_iso_utc(w["mid"])).jd if "mid" in w else None)
    starts = np.asarray(starts)
    ends = np.asarray(ends)

    max_duration = float(np.max((ends - starts) * 24.0))
    n_samp = max(5, int(round(max_duration * 60.0 / _CLASSIFY_STEP_MIN)) + 1)
    offsets = np.linspace(0.0, 1.0, n_samp)
    all_jd = (starts[:, None] + (ends[:, None] - starts[:, None]) * offsets[None, :]).ravel()
    grid = Time(all_jd, format="jd", scale="utc")

    # site -> per-transit observable mask (per window, per sample) and fraction
    mask2d = {}
    frac = {}
    for site in site_list:
        mask, *_ = _observable_mask(
            target, _earth_location(site), grid, alt_min, sun_alt_max,
            moon_sep_min, max_lunar_phase,
        )
        mask2d[site] = mask.reshape(len(windows), n_samp)
        frac[site] = mask2d[site].mean(axis=1)

    results = []
    for i in range(len(windows)):
        full_sites = [s for s in site_list if frac[s][i] >= 0.999]
        partial_sites = [s for s in site_list if 0.0 < frac[s][i] < 0.999]

        if full_sites:
            # A single site already covers the whole transit: this is always
            # preferred over a two-site relay, so no pair search is needed.
            results.append({
                "rating": "full",
                "sites": full_sites + partial_sites,
                "best_site": full_sites[0],
            })
            continue

        if len(partial_sites) < 2:
            # At most one site sees anything at all: no second site to relay
            # with. "partial" is reserved for coverage that includes a transit
            # contact (ingress or egress); a lone site that sees only the middle
            # -- neither contact -- is not scientifically useful and is rated
            # "none".
            best = partial_sites[0] if partial_sites else None
            if best and _observes_contact(mask2d[best][i], starts[i], ends[i], mids[i], half):
                results.append({"rating": "partial", "sites": partial_sites, "best_site": best})
            else:
                results.append({"rating": "none", "sites": [], "best_site": None})
            continue

        # 2+ sites each have some coverage: always resolve to a two-site relay
        # -- there is no ambiguous "still partial" outcome here. Whether it's
        # actually useful is communicated by split_gap_min/split_overlap_min,
        # not by falling back to a vaguer "partial" label.
        best_pair = None
        best_union_frac = -1.0
        for s1, s2 in itertools.combinations(partial_sites, 2):
            union_frac = float((mask2d[s1][i] | mask2d[s2][i]).mean())
            if union_frac > best_union_frac:
                best_union_frac = union_frac
                best_pair = (s1, s2)
        s1, s2 = best_pair
        m1, m2 = mask2d[s1][i], mask2d[s2][i]

        # A genuine relay must span both contacts: the pair's union has to see
        # ingress AND egress. If it misses one, the "extra" site is only adding
        # a sliver away from the contacts (e.g. a few minutes in the padding),
        # so fall back to the single-site rating -- partial when the best site
        # covers a contact, else none -- matching the one-site branch above.
        ingress_obs, egress_obs = _contact_flags(m1 | m2, starts[i], ends[i], mids[i], half)
        if not (ingress_obs and egress_obs):
            best_single = max(partial_sites, key=lambda s: frac[s][i])
            if _observes_contact(mask2d[best_single][i], starts[i], ends[i], mids[i], half):
                results.append({"rating": "partial", "sites": partial_sites, "best_site": best_single})
            else:
                results.append({"rating": "none", "sites": [], "best_site": None})
            continue

        span1 = _site_coverage_span(m1, offsets, starts[i], ends[i])
        span2 = _site_coverage_span(m2, offsets, starts[i], ends[i])
        if span1[0] <= span2[0]:
            early, early_span, late, late_span = s1, span1, s2, span2
        else:
            early, early_span, late, late_span = s2, span2, s1, span1
        span_min = (ends[i] - starts[i]) * 24.0 * 60.0
        overlap_min = float((m1 & m2).mean()) * span_min
        gap_min = max(0.0, (1.0 - best_union_frac) * span_min)
        rating = "split_gap" if gap_min > 1e-6 else "split_overlap"

        entry = {
            "rating": rating,
            "sites": partial_sites,
            "best_site": max(partial_sites, key=lambda s: frac[s][i]),
            "split_sites": [early, late],
            "split_windows": [
                {
                    "site": early,
                    "start": _jd_to_iso_z(early_span[0]),
                    "end": _jd_to_iso_z(early_span[1]),
                    "fragmented": early_span[2],
                },
                {
                    "site": late,
                    "start": _jd_to_iso_z(late_span[0]),
                    "end": _jd_to_iso_z(late_span[1]),
                    "fragmented": late_span[2],
                },
            ],
            "split_gap_min": round(gap_min, 1),
            "split_overlap_min": round(overlap_min, 1),
        }

        # Ingress/egress "bracket" check: does the early site's own coverage
        # still extend pad_before_min *past* ingress (not just up to it), and
        # does the late site's coverage extend pad_after_min *before* egress?
        # Only meaningful when the padded baseline is actually part of the
        # observability test (include_padding) and a pad value was given --
        # otherwise the bracket falls outside the sampled grid entirely.
        if include_padding and mids[i] is not None:
            ingress_jd = mids[i] - half
            egress_jd = mids[i] + half
            tol = (_CLASSIFY_STEP_MIN / 2.0) / 1440.0  # half a sampling step, in days
            if pad_before_min:
                pad_before_days = pad_before_min / 1440.0
                entry["ingress_bracket_ok"] = bool(
                    early_span[0] <= ingress_jd - pad_before_days + tol
                    and early_span[1] >= ingress_jd + pad_before_days - tol
                )
            if pad_after_min:
                pad_after_days = pad_after_min / 1440.0
                entry["egress_bracket_ok"] = bool(
                    late_span[0] <= egress_jd - pad_after_days + tol
                    and late_span[1] >= egress_jd + pad_after_days - tol
                )

        results.append(entry)
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
    max_lunar_phase: float = 1.0,
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

    mask, target_alt, moon_alt, sun_alt, moon_sep, moon_illum = _observable_mask(
        target, location, times, alt_min, sun_alt_max, moon_sep_min, max_lunar_phase
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
        "moon_illum": [round(float(v), 3) for v in moon_illum],
        "ingress": ingress,
        "egress": egress,
        "alt_limit": round(alt_min, 2),
        "sun_alt_limit": sun_alt_max,
        "moon_sep_min": float(moon_sep_min),
        "moon_sep_mid": round(moon_sep_mid, 1),
        "max_lunar_phase": float(max_lunar_phase),
        "moon_dark_floor": _MOON_DARK_FRACTION,
        "observable_fraction": round(float(np.asarray(mask).mean()), 3),
    }
