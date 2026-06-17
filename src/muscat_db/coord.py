"""Robust selection of a representative sky coordinate for a target.

The legacy ``targets``/``summaries`` aggregation used ``MAX(ra)`` and
``MAX(declination)`` over raw header strings.  Because that is a *lexicographic*
string max and corrupt header values (``'q'``, ``'OQ'``, ``'} |'``) sort above
valid ``+DD:MM:SS`` coordinates, a single bad frame out of thousands hijacked the
reported coordinate — and RA/Dec were maxed independently, producing a mismatched
pair.  See database._populate_targets / _populate_summaries.

This module validates sexagesimal format and picks the pair with the *median*
declination, keeping RA and Dec from the same frame.
"""
from __future__ import annotations

import re

# Packs an (ra, dec) pair into a single aggregate result; ASCII Unit Separator.
_SEP = "\x1f"

# Sexagesimal patterns. RA hours may be 1-2 digits (legacy "4:06:46" form).
# Dec carries an optional sign. Seconds may include a decimal fraction. Minutes
# and seconds are bounded 00-59 so malformed 3-digit seconds ("+20:16:251") and
# out-of-range fields are rejected.
_RA_RE = re.compile(r"^\d{1,2}:[0-5]\d:[0-5]\d(?:\.\d+)?$")
_DEC_RE = re.compile(r"^[+-]?\d{1,2}:[0-5]\d:[0-5]\d(?:\.\d+)?$")


def is_valid_ra(value: str | None) -> bool:
    return bool(value) and _RA_RE.match(value.strip()) is not None


def is_valid_dec(value: str | None) -> bool:
    return bool(value) and _DEC_RE.match(value.strip()) is not None


# muscat2's TCS dropped the decimal point in the seconds field, writing the
# tenths digit inline: "+20:11:12.1" -> "+20:11:121". Recovered by re-inserting
# the point before the last digit. Range-checked via the strict pattern, so a
# bad recovery (e.g. "+20:60:001" -> "60.0" minutes>59) is rejected.
_DROPPED_DECIMAL_RE = re.compile(r"^([+-]?\d{1,2}:[0-5]\d:)(\d{3})$")


def _clean(value: str | None, full_re: re.Pattern[str]) -> str | None:
    """Return a well-formed sexagesimal string, recovering the dropped-decimal
    seconds bug where possible, else ``None``."""
    if not value:
        return None
    s = value.strip()
    if full_re.match(s):
        return s
    m = _DROPPED_DECIMAL_RE.match(s)
    if m:
        sec = m.group(2)
        recovered = f"{m.group(1)}{sec[:2]}.{sec[2]}"
        if full_re.match(recovered):
            return recovered
    return None


def clean_ra(value: str | None) -> str | None:
    return _clean(value, _RA_RE)


def clean_dec(value: str | None) -> str | None:
    return _clean(value, _DEC_RE)


def _dec_to_arcsec(dec: str) -> float:
    """Signed declination in arcseconds, for ordering."""
    s = dec.strip()
    sign = -1.0 if s[0] == "-" else 1.0
    d, m, sec = s.lstrip("+-").split(":")
    return sign * (int(d) * 3600 + int(m) * 60 + float(sec))


def _median_pair(valid: list[tuple[str, str]]) -> tuple[str, str]:
    """Median-by-declination pair from an already-validated list."""
    if not valid:
        return ("", "")
    valid = sorted(valid, key=lambda p: _dec_to_arcsec(p[1]))
    return valid[len(valid) // 2]


def pick_representative(pairs: list[tuple[str | None, str | None]]) -> tuple[str, str]:
    """Return the (ra, dec) pair with the median declination among well-formed
    pairs, keeping RA and Dec from the same frame.

    Returns ``("", "")`` when no pair has both coordinates well-formed.
    """
    valid = []
    for ra, dec in pairs:
        cra, cdec = clean_ra(ra), clean_dec(dec)
        if cra is not None and cdec is not None:
            valid.append((cra, cdec))
    return _median_pair(valid)


def unpack(packed: str | None) -> tuple[str, str]:
    """Split a ``coord_repr`` aggregate result back into (ra, dec)."""
    if not packed or _SEP not in packed:
        return ("", "")
    ra, dec = packed.split(_SEP, 1)
    return (ra, dec)


class CoordRepr:
    """SQLite aggregate: pick a representative (ra, dec) pair from a group.

    Register with ``conn.create_aggregate("coord_repr", 2, CoordRepr)`` and call
    as ``coord_repr(ra, declination)``.  The result packs the chosen pair as
    ``ra<US>dec``; use :func:`unpack` to split it.  Invalid coordinates are
    filtered in ``step`` so memory stays bounded for huge groups (e.g. flats).
    """

    def __init__(self) -> None:
        self._valid: list[tuple[str, str]] = []

    def step(self, ra: str | None, dec: str | None) -> None:
        cra, cdec = clean_ra(ra), clean_dec(dec)
        if cra is not None and cdec is not None:
            self._valid.append((cra, cdec))

    def finalize(self) -> str:
        ra, dec = _median_pair(self._valid)
        return f"{ra}{_SEP}{dec}"
