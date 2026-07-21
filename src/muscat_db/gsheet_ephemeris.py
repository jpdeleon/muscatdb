"""Read per-user linear ephemerides and transit centers from a Google Sheet.

Two published-CSV tabs feed the LCO schedule page:

* an **ephemeris** tab (``target`` / ``t0`` / ``period`` / ``duration`` plus
  optional uncertainties), parsed into the same
  ``{planet: {t0, period, duration, *_unc}}`` dict the NASA and TOI catalog
  resolvers already return (:mod:`muscat_db.catalog`); and
* a **transit-centers** tab (``target`` / ``planet`` / ``epoch`` / ``tc`` /
  ``tc_unc``), reused through :func:`muscat_db.ephemeris_import.parse_transit_csv`
  so a linear ephemeris can be refit from observed mid-transit times.

Only Google's own published-CSV export endpoint is ever fetched: the caller
supplies a sheet URL or ID which is validated and reduced to an opaque ID
*before* any request URL is built (SSRF guard -- see :func:`sheet_id_from`).
Fetches degrade to empty on any error, mirroring the catalog resolvers, so a
misconfigured or unpublished sheet never hard-fails the schedule page.
"""

from __future__ import annotations

import csv
import io
import logging
import math
import os
import re
import urllib.parse

from . import ephemeris_import
from .cache import register_cache
from .catalog import _normalize_target_name, _safe_float, _sync_get

logger = logging.getLogger(__name__)

GOOGLE_SHEETS_HOST = "docs.google.com"
DEFAULT_EPHEM_TAB = "ephemeris"
DEFAULT_TC_TAB = "tc"

# Logical fields the user can map to a sheet column (canonical order used by the
# settings UI). An empty/absent mapping for a field falls back to alias
# auto-detection. t0+period are required for the ephemeris tab; planet+tc+tc_unc
# for the transit-centers tab.
EPHEM_FIELDS = ("target", "planet", "t0", "period", "duration", "t0_unc", "period_unc", "duration_unc")
TC_FIELDS = ("target", "planet", "epoch", "tc", "tc_unc")

# A Google spreadsheet ID is a long opaque base64url-ish token. Requiring >= 20
# chars keeps a stray word (e.g. a tab name pasted by mistake) from being
# mistaken for an ID.
_SHEET_ID_RE = re.compile(r"^[A-Za-z0-9_-]{20,}$")
_URL_ID_RE = re.compile(r"/spreadsheets/d/([A-Za-z0-9_-]+)")

# On-demand fetch cache so repeated target lookups within a session (Fetch
# ephemeris, then Generate windows) don't re-hit Google. TTL keeps the sheet
# reasonably fresh without a manual refresh.
_SHEET_TTL_S = float(os.environ.get("MUSCAT_EPHEMERIS_SHEET_TTL_S", "300"))
_fetch_cache = register_cache(ttl=_SHEET_TTL_S, maxsize=256)

# Column header aliases for the ephemeris tab, matched after normalization by
# ephemeris_import._header_key (which strips unit annotations like "(BJD)",
# "[days]" and trailing bjd/jd/day units). The transit-centers tab reuses
# ephemeris_import._ALIASES via parse_transit_csv.
_EPHEM_ALIASES: dict[str, set[str]] = {
    "target": {
        "target", "targetname", "name", "star", "starname",
        "host", "hostname", "object", "objectname", "toi", "tic", "ticid",
    },
    "planet": set(ephemeris_import._ALIASES["planet"]),
    "t0": {
        "t0", "tzero", "tc", "epoch", "transitepoch",
        "midtransit", "midtransittime", "tmid", "t0bjd",
    },
    "period": {"period", "per", "p", "orbitalperiod", "porb"},
    "duration": {
        "duration", "dur", "transitduration", "t14",
        "durationhours", "durhours", "durationhr", "durhr",
    },
    "t0_unc": {
        "t0unc", "t0err", "t0error", "t0uncertainty",
        "epochunc", "epocherr", "sigmat0",
    },
    "period_unc": {
        "periodunc", "perioderr", "perioderror", "perianduncertainty",
        "perunc", "pererr", "sigmaperiod",
    },
    "duration_unc": {
        "durationunc", "durunc", "durerr", "durationerr",
        "durationuncertainty", "sigmadur",
    },
}


class GsheetError(ValueError):
    """Raised when a spreadsheet reference is missing or not a Google Sheet."""


def sheet_id_from(url_or_id: str) -> str:
    """Return the spreadsheet ID from a full Google Sheets URL or a bare ID.

    The returned value is an opaque token that we later interpolate into a
    ``docs.google.com`` export URL we build ourselves; a caller-supplied URL is
    never fetched verbatim. Any host other than ``docs.google.com`` is rejected
    (SSRF guard), as is anything that does not carry a spreadsheet ID.
    """
    ref = (url_or_id or "").strip()
    if not ref:
        raise GsheetError("A Google Sheet URL or ID is required.")

    # Bare ID: no path and no scheme, just the token itself.
    if "/" not in ref and "://" not in ref:
        if _SHEET_ID_RE.match(ref):
            return ref
        raise GsheetError("That does not look like a Google Sheet ID.")

    parsed = urllib.parse.urlparse(ref if "://" in ref else "https://" + ref)
    host = (parsed.hostname or "").lower()
    if host != GOOGLE_SHEETS_HOST:
        raise GsheetError(
            f"Only {GOOGLE_SHEETS_HOST} spreadsheet links are allowed "
            f"(got {host or 'no host'})."
        )
    match = _URL_ID_RE.search(parsed.path)
    if not match:
        raise GsheetError("Could not find a spreadsheet ID in that URL.")
    return match.group(1)


def _gviz_csv_url(sheet_id: str, tab_name: str) -> str:
    """Build the gviz CSV-export URL for a worksheet tab addressed by name."""
    query = urllib.parse.urlencode({"tqx": "out:csv", "sheet": tab_name})
    return f"https://{GOOGLE_SHEETS_HOST}/spreadsheets/d/{sheet_id}/gviz/tq?{query}"


@_fetch_cache
def _fetch_tab_csv(sheet_id: str, tab_name: str) -> str:
    """Fetch one worksheet tab as CSV text; ``''`` on any error (TTL-cached).

    Degrades to empty rather than raising so a private/unpublished sheet or a
    transient network error surfaces as "no ephemeris" instead of a 500.
    """
    try:
        url = _gviz_csv_url(sheet_id, tab_name)
        text = _sync_get(url, headers={"User-Agent": "Mozilla/5.0"}).text
        if len(text.encode("utf-8")) > ephemeris_import.MAX_CSV_BYTES:
            logger.warning(
                "gsheet tab %r exceeds the %d-byte import cap; ignoring",
                tab_name, ephemeris_import.MAX_CSV_BYTES,
            )
            return ""
        return text
    except Exception:
        logger.debug("failed to fetch gsheet tab %s/%r", sheet_id, tab_name, exc_info=True)
        return ""


def _map_ephem_headers(
    fieldnames: list[str] | None, overrides: dict | None = None
) -> dict[str, str]:
    """Map canonical ephemeris fields to their raw header names.

    Explicit ``overrides`` (canonical field -> exact header name) win when the
    named header exists in the sheet; every field left unmapped falls back to
    alias auto-detection. Passing no overrides reproduces pure auto-detection.
    """
    overrides = overrides or {}
    fieldset = list(fieldnames or [])
    mapped: dict[str, str] = {}
    used: set[str] = set()
    for canonical, header in overrides.items():
        header = str(header or "").strip()
        if header and canonical in _EPHEM_ALIASES and header in fieldset:
            mapped[canonical] = header
            used.add(header)
    for raw in fieldset:
        if raw in used:
            continue
        key = ephemeris_import._header_key(raw)
        for canonical, aliases in _EPHEM_ALIASES.items():
            if canonical in mapped:
                continue
            if key in aliases:
                mapped[canonical] = raw
                used.add(raw)
                break
    return mapped


def _finite_float(value) -> float | None:
    parsed = _safe_float(value)
    if parsed is None or not math.isfinite(parsed):
        return None
    return parsed


def _planet_from_name(cell) -> str:
    """Extract a trailing planet marker from a target/name cell.

    Mirrors the trailing-letter stripping in
    :func:`catalog._normalize_target_name`, so a sheet that encodes the planet
    in the name column (no dedicated ``planet`` column) still yields one letter
    per planet: ``"HIP 67522 c" -> "c"`` and a TOI candidate suffix
    ``".02" -> "c"``. A bare host name with no planet marker yields ``""`` (the
    caller then defaults to ``"b"``).
    """
    s = str(cell or "").strip().upper().replace(" ", "").replace("-", "").replace("_", "")
    candidate = re.search(r"\.(\d+)$", s)
    if candidate:
        try:
            number = int(candidate.group(1))
        except ValueError:
            return ""
        return chr(ord("b") + number - 1) if 1 <= number <= 25 else ""
    if len(s) > 2 and s[-1] in "BCDEFGH":
        return s[-1].lower()
    return ""


def _planet_label(value) -> str:
    """Planet letter from a sheet ``planet`` cell.

    Accepts, in order: a plain letter (``b``-``z``); a TOI/TFOP candidate number
    written with a leading dot or zero padding (``".01"``/``"01"`` -> ``b``,
    ``".02"``/``"02"`` -> ``c``; 1-based, matching the TOI catalog); or a bare
    zero-based index (``0`` -> ``b``, ``1`` -> ``c``) via
    :func:`ephemeris_import._planet_letter`. Returns ``""`` when unrecognized.
    """
    text = str(value or "").strip()
    if not text:
        return ""
    number = None
    dotted = re.fullmatch(r"\.(\d+)", text)  # ".01", ".2"
    if dotted:
        number = int(dotted.group(1))
    elif re.fullmatch(r"0\d+", text):  # zero-padded "01", "002"
        number = int(text)
    if number is not None:
        return chr(ord("b") + number - 1) if 1 <= number <= 25 else ""
    return ephemeris_import._planet_letter(text)


def query_target_ephemeris(
    target: str,
    url_or_id: str,
    ephem_tab: str = DEFAULT_EPHEM_TAB,
    col_map: dict | None = None,
) -> dict:
    """Return ``{planet: {t0, period, duration, *_unc}}`` for *target*.

    Reads the ephemeris tab, filters to rows whose ``target`` column matches
    *target* (normalized), and returns the same dict shape the NASA/TOI
    resolvers return. ``col_map`` (canonical field -> exact header) overrides
    alias auto-detection per field. Rows without a finite ``t0``+``period`` are
    skipped; ``duration`` and uncertainties are included only when present and
    finite. Returns ``{}`` when the sheet is unreachable or has no matching rows.
    """
    sheet_id = sheet_id_from(url_or_id)
    text = _fetch_tab_csv(sheet_id, ephem_tab or DEFAULT_EPHEM_TAB)
    if not text.strip():
        return {}

    target_norm = _normalize_target_name(target)
    results: dict = {}
    try:
        reader = csv.DictReader(io.StringIO(text))
        mapped = _map_ephem_headers(reader.fieldnames, col_map)
        if "t0" not in mapped or "period" not in mapped:
            logger.debug(
                "gsheet ephemeris tab missing t0/period columns: %s", reader.fieldnames
            )
            return {}
        has_target_col = "target" in mapped
        for row in reader:
            if has_target_col and (
                _normalize_target_name(str(row.get(mapped["target"]) or "")) != target_norm
            ):
                continue
            t0 = _finite_float(row.get(mapped["t0"]))
            period = _finite_float(row.get(mapped["period"]))
            if t0 is None or period is None:
                continue
            letter = ""
            if "planet" in mapped:
                letter = _planet_label(row.get(mapped["planet"]))
            if not letter and "target" in mapped:
                # No dedicated planet column: derive the letter from the
                # target/name cell (e.g. "HIP 67522 c" -> "c"). Without this,
                # every row of a planet-in-name sheet collapses onto "b" and all
                # but the last planet is silently dropped.
                letter = _planet_from_name(row.get(mapped["target"]))
            if not letter:
                letter = "b"
            entry: dict = {"t0": t0, "period": period}
            if "duration" in mapped:
                dur = _finite_float(row.get(mapped["duration"]))
                if dur is not None:
                    entry["duration"] = dur
            for canonical in ("t0_unc", "period_unc", "duration_unc"):
                if canonical in mapped:
                    unc = _finite_float(row.get(mapped[canonical]))
                    if unc is not None and unc > 0:
                        entry[canonical] = unc
            results[letter] = entry
    except Exception:
        logger.debug("failed to parse gsheet ephemeris tab for %s", target, exc_info=True)
        return {}
    return results


_TC_ALIASES: dict[str, set[str]] = {
    "target": _EPHEM_ALIASES["target"],
    "planet": set(ephemeris_import._ALIASES["planet"]),
    "epoch": set(ephemeris_import._ALIASES["epoch"]),
    "tc": set(ephemeris_import._ALIASES["tc"]),
    "tc_unc": set(ephemeris_import._ALIASES["tc_unc"]),
}


def _resolve_tc_headers(
    fieldnames: list[str], overrides: dict | None = None
) -> dict[str, str]:
    """Resolve transit-center canonical fields to raw header names.

    Explicit ``overrides`` win when the named header exists; the rest fall back
    to alias auto-detection. Covers target/planet/epoch/tc/tc_unc.
    """
    overrides = overrides or {}
    resolved: dict[str, str] = {}
    used: set[str] = set()
    for canonical in TC_FIELDS:
        header = str(overrides.get(canonical) or "").strip()
        if header and header in fieldnames:
            resolved[canonical] = header
            used.add(header)
    for canonical in TC_FIELDS:
        if canonical in resolved:
            continue
        for raw in fieldnames:
            if raw in used:
                continue
            if ephemeris_import._header_key(raw) in _TC_ALIASES[canonical]:
                resolved[canonical] = raw
                used.add(raw)
                break
    return resolved


def _canonicalize_tc(text: str, target_norm: str, col_map: dict | None) -> str:
    """Project the transit-centers tab to the columns parse_transit_csv needs,
    filtered to *target*.

    Emits only the resolved planet/epoch/tc/tc_unc columns (dropping everything
    else, which removes header ambiguity), so an explicitly mapped column with a
    non-standard name is still understood. The ``tc`` column keeps its original
    header when that name already aliases to ``tc`` (so a BJD_TDB annotation
    survives for time-scale detection); otherwise it is renamed to ``tc``.
    """
    try:
        reader = csv.DictReader(io.StringIO(text))
        fieldnames = reader.fieldnames or []
        if not fieldnames:
            return ""
        resolved = _resolve_tc_headers(fieldnames, col_map)
        if "tc" not in resolved or "tc_unc" not in resolved:
            return ""
        planet_col = resolved.get("planet")
        target_col = resolved.get("target")
        # Planet attribution: a dedicated planet column, else derive it per-row
        # from the target/name cell (e.g. "HIP 67522 c" -> "c"). With neither
        # there is no way to tell the planets apart.
        if not planet_col and not target_col:
            return ""
        tc_out = (
            resolved["tc"]
            if ephemeris_import._header_key(resolved["tc"]) in _TC_ALIASES["tc"]
            else "tc"
        )
        header = ["planet"]
        if "epoch" in resolved:
            header.append("epoch")
        header += [tc_out, "tc_unc"]
        out = io.StringIO()
        writer = csv.DictWriter(out, fieldnames=header)
        writer.writeheader()
        wrote = False
        for row in reader:
            target_cell = str(row.get(target_col) or "") if target_col else ""
            if target_col and _normalize_target_name(target_cell) != target_norm:
                continue
            planet = (
                _planet_label(row.get(planet_col)) if planet_col
                else _planet_from_name(target_cell)
            )
            record = {"planet": planet, tc_out: row.get(resolved["tc"], ""),
                      "tc_unc": row.get(resolved["tc_unc"], "")}
            if "epoch" in resolved:
                record["epoch"] = row.get(resolved["epoch"], "")
            writer.writerow(record)
            wrote = True
        return out.getvalue() if wrote else ""
    except Exception:
        logger.debug("failed to canonicalize gsheet transit-centers tab", exc_info=True)
        return ""


def _empty_tc_result() -> dict:
    return {"rows": [], "errors": [], "warnings": [], "time_system": None}


def query_target_transit_centers(
    target: str,
    url_or_id: str,
    tc_tab: str = DEFAULT_TC_TAB,
    col_map: dict | None = None,
) -> dict:
    """Return parse_transit_csv output for *target*'s transit-center rows.

    Fetches the transit-centers tab, projects it to the resolved
    planet/epoch/tc/tc_unc columns (``col_map`` overrides alias auto-detection),
    filters to *target*, and delegates to
    :func:`muscat_db.ephemeris_import.parse_transit_csv`. Returns an empty result
    when the sheet is unreachable, has no matching rows, or fails to parse.
    """
    sheet_id = sheet_id_from(url_or_id)
    text = _fetch_tab_csv(sheet_id, tc_tab or DEFAULT_TC_TAB)
    if not text.strip():
        return _empty_tc_result()
    target_norm = _normalize_target_name(target)
    canonical = _canonicalize_tc(text, target_norm, col_map)
    if not canonical.strip():
        return _empty_tc_result()
    try:
        return ephemeris_import.parse_transit_csv(canonical)
    except ephemeris_import.EphemerisCSVError:
        logger.debug("failed to parse gsheet transit-centers tab for %s", target, exc_info=True)
        return _empty_tc_result()


def tab_columns(url_or_id: str, tab_name: str) -> list[str]:
    """Return the header-row column names of one worksheet tab ([] on error)."""
    sheet_id = sheet_id_from(url_or_id)
    text = _fetch_tab_csv(sheet_id, tab_name or DEFAULT_EPHEM_TAB)
    if not text.strip():
        return []
    try:
        reader = csv.DictReader(io.StringIO(text))
        return [name for name in (reader.fieldnames or []) if name and name.strip()]
    except Exception:
        logger.debug("failed to read gsheet columns for %s/%r", sheet_id, tab_name, exc_info=True)
        return []


def suggest_ephem_columns(columns: list[str]) -> dict[str, str]:
    """Best-guess ephemeris field -> column mapping via alias auto-detection."""
    return _map_ephem_headers(columns)


def suggest_tc_columns(columns: list[str]) -> dict[str, str]:
    """Best-guess transit-center field -> column mapping via alias detection."""
    return _resolve_tc_headers(columns)
