"""Canonical band/filter constants for the web layer.

Why this file
-------------
The prose pipeline defines the authoritative band ordering and filter alias
mapping in ``prose/utils.py`` (``_FILTER_ALIASES``, ``DEFAULT_BROAD_BANDS``,
``DEFAULT_NARROW_BANDS``).  However, the web process runs in muscat-db's own
Python environment and **cannot** import the ``prose`` package (which lives
in a separate conda env).

This module therefore maintains a **vendor copy** of those constants plus
``bands_from_filters()`` — the same logic prose exposes, but available without
a prose import.  The prose copy is the source of truth; update this file
whenever a new filter or band is added there.

The comment above each definition records which prose symbol it mirrors.
"""

from __future__ import annotations

# Mirrors prose.utils.DEFAULT_BROAD_BANDS
DEFAULT_BANDS: list[str] = ["gp", "rp", "ip", "zs"]

# Mirrors prose.utils.DEFAULT_NARROW_BANDS
NARROW_BANDS: list[str] = ["g_narrow", "Na_D", "i_narrow", "z_narrow"]

# Mirrors prose.utils._FILTER_ALIASES
# Raw obslog FILTER value -> prose ``--bands`` token.  Unknown filters
# (e.g. Sinistro R/V/B) are not listed and pass through unchanged —
# run_photometry's ``_resolve_band`` falls back to the raw value, so
# ``--bands R V`` works for those frames.
_FILTER_BAND_ALIAS: dict[str, str] = {
    "gp": "gp", "g": "gp",
    "rp": "rp", "r": "rp", "rp*diffuser": "rp",
    "ip": "ip", "i": "ip",
    "zs": "zs", "z": "zs", "zp": "zs", "z_s": "zs", "zp*diffuser": "zs",
    "g_narrow": "g_narrow", "r_narrow": "r_narrow",
    "i_narrow": "i_narrow", "z_narrow": "z_narrow",
    "g_wide": "g_wide", "Na_D": "Na_D",
}


def bands_from_filters(filters: list[str]) -> list[str]:
    """Map raw obslog FILTER values to ordered, de-duplicated ``--bands`` tokens.

    Each raw filter is normalized via :data:`_FILTER_BAND_ALIAS`; unknown values
    (e.g. Sinistro ``R``/``V``/``B``) pass through unchanged.  The result is
    ordered canonically — broadband (gp, rp, ip, zs), then narrowbands, then any
    extras in first-seen order — so the UI shows a stable, familiar layout.
    Returns ``[]`` for empty input.

    Mirrors ``prose.utils.bands_from_filters()``.
    """
    seen: set[str] = set()
    tokens: list[str] = []
    for f in filters or []:
        if not f:
            continue
        token = _FILTER_BAND_ALIAS.get(f, f)
        if token not in seen:
            seen.add(token)
            tokens.append(token)
    order = {b: i for i, b in enumerate([*DEFAULT_BANDS, *NARROW_BANDS])}
    return sorted(tokens, key=lambda b: (order.get(b, len(order)), tokens.index(b)))
