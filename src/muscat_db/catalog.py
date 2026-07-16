"""Catalog lookup and matching helpers shared by web.py's routes.

This module centralizes the local-CSV / online-archive lookups that back the
/toi, /nexsci, and /target pages: TOI catalog parsing, HARPS RVBank coordinate
matching, the Boyle2026 stellar-rotation merge, NASA Exoplanet Archive (NExScI)
composite-catalog parsing, JWST/spectra target membership, and per-target
ephemeris/coordinate resolution (local CSV first, then the online TAP/ADQL
archive, then SIMBAD via muscat_db.exposure).

Extracted from muscat_db.web (see docs/architecture_audit.md, findings H1/H2)
so this logic has one home instead of being interleaved with route handlers;
web.py's route handlers import these names back and call them unchanged.
"""

from __future__ import annotations

import csv
import datetime
import io
import logging
import math
import os
import pathlib
import re
import zipfile
from contextlib import contextmanager

from fastapi import Request

from muscat_db import exposure as exp_calc
from muscat_db import http_client
from muscat_db.auth import request_user as _request_user
from muscat_db.cache import LRUCache
from muscat_db.database import (
    UserSettingsError,
    get_targets as _get_targets,
    get_user_ads_token,
)

logger = logging.getLogger(__name__)

HERE = pathlib.Path(__file__).parent


def _sync_get(url: str, *, headers: dict | None = None, timeout: float | None = None):
    """GET via the shared sync httpx client, raising on non-2xx status
    (mirrors urllib.request.urlopen's implicit HTTPError-on-bad-status).

    Used by call sites embedded in routes that also do synchronous local
    DB/job-store work and must stay plain ``def`` (see http_client.py); tests
    monkeypatch this name directly."""
    response = http_client.get_sync_client().get(
        url,
        headers=headers,
        timeout=timeout if timeout is not None else http_client.DEFAULT_TIMEOUT_S,
    )
    response.raise_for_status()
    return response


def _db_mtime(db: str):
    """Cache key for the DB file. Note edits and `build-db` both rewrite the
    SQLite file, bumping its mtime, so this auto-invalidates the index cache.

    The DB runs in WAL mode, where a commit is durable once it lands in the
    `-wal` sidecar file; the main file's mtime only advances when SQLite
    happens to checkpoint the WAL back into it (e.g. on the last connection
    closing), which frequently does not happen while the server has
    concurrent requests open. Folding the `-wal` file's mtime/size into the
    key ensures every commit invalidates the cache, not just checkpoints."""
    try:
        stat = os.stat(db)
        key = (stat.st_mtime_ns, stat.st_size)
    except OSError:
        return None
    try:
        wal_stat = os.stat(db + "-wal")
        key = (*key, wal_stat.st_mtime_ns, wal_stat.st_size)
    except OSError:
        pass
    return key


# --------------------------- TOI catalog ------------------------------

# (csv header, json key, kind) — kind "s" keeps the raw string, "f" parses a
# float (or null). Only this subset of the 69 raw columns is surfaced on the
# /toi page; it drives both the preview table and the interactive plot.
_TOI_COLUMNS: list[tuple[str, str, str]] = [
    ("TOI", "toi", "s"),
    ("TIC ID", "tic", "s"),
    ("Planet Name", "name", "s"),
    ("TFOPWG Disposition", "disp", "s"),
    ("Period (days)", "period", "f"),
    ("Duration (hours)", "duration", "f"),
    ("Depth (ppm)", "depth", "f"),
    ("Planet Radius (R_Earth)", "radius", "f"),
    ("Planet Equil Temp (K)", "teq", "f"),
    ("Planet Insolation (Earth Flux)", "insol", "f"),
    ("TESS Mag", "tmag", "f"),
    ("Stellar Eff Temp (K)", "steff", "f"),
    ("Stellar Radius (R_Sun)", "srad", "f"),
    ("Stellar Distance (pc)", "dist", "f"),
    ("ra_deg", "ra", "f"),
    ("dec_deg", "dec", "f"),
    # 1-sigma uncertainties for axes that carry them (drive the plot error bars).
    ("Period (days) err", "period_err", "f"),
    ("Duration (hours) err", "duration_err", "f"),
    ("Depth (ppm) err", "depth_err", "f"),
    ("Planet Radius (R_Earth) err", "radius_err", "f"),
    ("TESS Mag err", "tmag_err", "f"),
    ("Stellar Eff Temp (K) err", "steff_err", "f"),
    ("Stellar Radius (R_Sun) err", "srad_err", "f"),
    ("Stellar Distance (pc) err", "dist_err", "f"),
]

_toi_cache: dict = {}


def _toi_float(v) -> float | None:
    """Parse a finite float from a raw CSV cell, or None."""
    if v is None:
        return None
    s = str(v).strip()
    if not s:
        return None
    try:
        x = float(s)
    except ValueError:
        return None
    # Reject NaN/inf so the JSON stays strict (allow_nan=False).
    if x != x or x in (float("inf"), float("-inf")):
        return None
    return x


_HARPS_TARGETS_PATH = pathlib.Path(os.environ.get(
    "MUSCAT_HARPS_TARGETS_CSV",
    str(HERE.parent.parent / "data" / "HARPS_RVBank_targets.csv"),
))
_HARPS_RVBANK_PATH = pathlib.Path(os.environ.get(
    "MUSCAT_HARPS_RVBANK_CSV",
    str(HERE.parent.parent / "data" / "HARPS_RVBank_ver02.csv"),
))
_HARPS_RVBANK_ZIP_PATH = pathlib.Path(os.environ.get(
    "MUSCAT_HARPS_RVBANK_ZIP",
    str(HERE.parent.parent / "data" / "HARPS_RVBank_ver02.csv.zip"),
))
_HARPS_RVBANK_URL = os.environ.get(
    "MUSCAT_HARPS_RVBANK_URL",
    "https://raw.githubusercontent.com/3fon3fonov/HARPS_RVBank/master/HARPS_RVBank_ver02.csv",
)
_HARPS_MATCH_ARCSEC = float(os.environ.get("MUSCAT_HARPS_MATCH_ARCSEC", "5.0"))
_HARPS_TARGET_TABLE_MAX_ROWS = int(os.environ.get("MUSCAT_HARPS_TARGET_TABLE_MAX_ROWS", "2000"))
_HARPS_ONLINE_TIMEOUT_S = float(os.environ.get("MUSCAT_HARPS_ONLINE_TIMEOUT_S", "60"))
_HARPS_BUCKET_DEG = 0.05
_harps_cache: dict = {}


def _coord_deg(value, *, is_ra: bool) -> float | None:
    """Parse decimal degrees or sexagesimal coordinates into degrees."""
    x = _toi_float(value)
    if x is not None:
        return x % 360.0 if is_ra else x
    if value is None:
        return None
    s = str(value).strip()
    if not s or ":" not in s:
        return None
    sign = 1.0
    if not is_ra and s[0] in "+-":
        sign = -1.0 if s[0] == "-" else 1.0
        s = s[1:]
    parts = s.split(":")
    if len(parts) != 3:
        return None
    try:
        a, b, c = int(parts[0]), int(parts[1]), float(parts[2])
    except ValueError:
        return None
    if b < 0 or b >= 60 or c < 0 or c >= 60:
        return None
    deg = a + b / 60.0 + c / 3600.0
    if is_ra:
        return (deg * 15.0) % 360.0
    return sign * deg


def _angular_sep_arcsec(ra1: float, dec1: float, ra2: float, dec2: float) -> float:
    r1, d1, r2, d2 = map(math.radians, (ra1, dec1, ra2, dec2))
    sd = math.sin((d2 - d1) / 2.0)
    sr = math.sin((r2 - r1) / 2.0)
    a = sd * sd + math.cos(d1) * math.cos(d2) * sr * sr
    a = min(1.0, max(0.0, a))
    return math.degrees(2.0 * math.asin(math.sqrt(a))) * 3600.0


def _load_harps_coords() -> tuple[list[tuple[float, float]], str]:
    """Load unique HARPS RVBank target coordinates.

    Prefer the compact per-target CSV produced from the RVBank, but accept the
    full observation-level RVBank CSV as a fallback. Both expose ``ra`` and
    ``dec`` columns in degrees; the HTML table uses sexagesimal coordinates, so
    the parser also accepts that form for hand-built target lists.
    """
    if _HARPS_TARGETS_PATH.is_file():
        path = _HARPS_TARGETS_PATH
    elif _HARPS_RVBANK_PATH.is_file():
        path = _HARPS_RVBANK_PATH
    else:
        path = _HARPS_RVBANK_ZIP_PATH
    empty: tuple[list[tuple[float, float]], str] = ([], "")
    try:
        mtime = path.stat().st_mtime_ns
    except OSError:
        return empty

    cached = _harps_cache.get("coords")
    cache_key = (str(path), mtime)
    if cached is not None and cached[0] == cache_key:
        return cached[1]

    seen: set[tuple[float, float]] = set()
    coords: list[tuple[float, float]] = []
    try:
        with _open_harps_csv_path(path) as f:
            reader = csv.DictReader(f)
            col_map = {h.strip().lower(): h for h in (reader.fieldnames or [])}
            ra_col = col_map.get("ra")
            dec_col = col_map.get("dec")
            if not ra_col or not dec_col:
                logger.warning("HARPS RVBank catalog %s lacks ra/dec columns", path)
                return empty
            for row in reader:
                ra = _coord_deg(row.get(ra_col), is_ra=True)
                dec = _coord_deg(row.get(dec_col), is_ra=False)
                if ra is None or dec is None or not (-90.0 <= dec <= 90.0):
                    continue
                key = (round(ra, 8), round(dec, 8))
                if key in seen:
                    continue
                seen.add(key)
                coords.append((ra, dec))
    except Exception:
        logger.warning("failed to read HARPS RVBank catalog %s", path, exc_info=True)
        return empty

    result = (coords, datetime.date.fromtimestamp(mtime / 1e9).isoformat())
    _harps_cache["coords"] = (cache_key, result)
    return result


def _harps_source_cache_key() -> tuple:
    """Cache component for HARPS data used by rendered target/catalog pages."""
    parts = [_HARPS_MATCH_ARCSEC]
    for path in (_HARPS_TARGETS_PATH, _HARPS_RVBANK_PATH, _HARPS_RVBANK_ZIP_PATH):
        try:
            st = path.stat()
        except OSError:
            parts.append((str(path), None))
        else:
            parts.append((str(path), st.st_mtime_ns, st.st_size))
    return tuple(parts)


_TOI_CATALOG_PATH = HERE.parent.parent / "data" / "TOIs.csv"
_NEXSCI_CATALOG_PATH = HERE.parent.parent / "data" / "nexsci_pscomppars.csv"
_JWST_TARGETS_PATH = HERE.parent.parent / "data" / "jwst_targets.csv"
_SPECTRA_TARGETS_PATH = HERE.parent.parent / "data" / "spectra_targets.csv"


def _path_cache_part(path: pathlib.Path) -> tuple:
    try:
        st = path.stat()
    except OSError:
        return (str(path), None)
    return (str(path), st.st_mtime_ns, st.st_size)


def _catalog_source_cache_key() -> tuple:
    """Cache component for target-page catalog-coordinate fallbacks."""
    return (_path_cache_part(_TOI_CATALOG_PATH), _path_cache_part(_NEXSCI_CATALOG_PATH), _path_cache_part(_JWST_TARGETS_PATH), _path_cache_part(_SPECTRA_TARGETS_PATH))


def _load_harps_targets() -> tuple[list[dict], str]:
    """Load unique HARPS targets with coordinates and optional RV counts."""
    path = _HARPS_TARGETS_PATH
    if not path.is_file():
        path = _HARPS_RVBANK_PATH if _HARPS_RVBANK_PATH.is_file() else _HARPS_RVBANK_ZIP_PATH
    empty: tuple[list[dict], str] = ([], "")
    try:
        mtime = path.stat().st_mtime_ns
    except OSError:
        return empty

    cache_key = ("targets", str(path), mtime)
    cached = _harps_cache.get("targets")
    if cached is not None and cached[0] == cache_key:
        return cached[1]

    targets: dict[tuple[str, float, float], dict] = {}
    try:
        with _open_harps_csv_path(path) as f:
            reader = csv.DictReader(f)
            col_map = {h.strip().lower(): h for h in (reader.fieldnames or [])}
            target_col = col_map.get("target")
            ra_col = col_map.get("ra")
            dec_col = col_map.get("dec")
            n_col = col_map.get("n_rv")
            if not target_col or not ra_col or not dec_col:
                logger.warning("HARPS target catalog %s lacks target/ra/dec columns", path)
                return empty
            for row in reader:
                target = (row.get(target_col) or "").strip()
                ra = _coord_deg(row.get(ra_col), is_ra=True)
                dec = _coord_deg(row.get(dec_col), is_ra=False)
                if not target or ra is None or dec is None or not (-90.0 <= dec <= 90.0):
                    continue
                key = (target, round(ra, 8), round(dec, 8))
                entry = targets.setdefault(key, {"target": target, "ra": ra, "dec": dec, "n_rv": 0})
                if n_col:
                    try:
                        entry["n_rv"] = max(entry["n_rv"], int(float(row.get(n_col) or 0)))
                    except ValueError:
                        pass
                else:
                    entry["n_rv"] += 1
    except Exception:
        logger.warning("failed to read HARPS target catalog %s", path, exc_info=True)
        return empty

    result = (list(targets.values()), datetime.date.fromtimestamp(mtime / 1e9).isoformat())
    _harps_cache["targets"] = (cache_key, result)
    return result


@contextmanager
def _open_harps_csv_path(path: pathlib.Path):
    if path.suffix.lower() == ".zip":
        with zipfile.ZipFile(path) as zf:
            names = [n for n in zf.namelist() if n.lower().endswith(".csv")]
            if not names:
                raise FileNotFoundError(f"{path} contains no CSV file")
            with zf.open(names[0]) as raw:
                with io.TextIOWrapper(raw, encoding="utf-8", newline="") as text:
                    yield text
    else:
        with open(path, encoding="utf-8", newline="") as f:
            yield f


@contextmanager
def _open_harps_rvbank_csv():
    if _HARPS_RVBANK_PATH.is_file():
        with _open_harps_csv_path(_HARPS_RVBANK_PATH) as f:
            yield ("local", str(_HARPS_RVBANK_PATH), f)
        return
    if _HARPS_RVBANK_ZIP_PATH.is_file():
        with _open_harps_csv_path(_HARPS_RVBANK_ZIP_PATH) as f:
            yield ("local", str(_HARPS_RVBANK_ZIP_PATH), f)
        return

    # csv.DictReader always consumes the whole file (it counts total_rows
    # past max_rows), so buffering the full response body loses no streaming
    # benefit that urlopen's chunk-by-chunk TextIOWrapper had here.
    response = _sync_get(
        _HARPS_RVBANK_URL,
        headers={"User-Agent": "muscat-db/harps-rvbank"},
        timeout=_HARPS_ONLINE_TIMEOUT_S,
    )
    yield ("online", _HARPS_RVBANK_URL, io.StringIO(response.text))


def _harps_coord_membership(cat_data: dict) -> tuple[list[int], int]:
    """Return RV counts (or 0) for catalog rows positionally matched to HARPS RVBank."""
    harps_coords, _updated = _load_harps_coords()
    n = len(cat_data.get("ra") or [])
    out = [0] * n
    if not harps_coords:
        return out, 0

    tol = max(0.0, _HARPS_MATCH_ARCSEC)
    if tol <= 0:
        return out, 0
    bucket = max(_HARPS_BUCKET_DEG, tol / 3600.0)
    ra_bins = max(1, int(math.ceil(360.0 / bucket)))

    # Attempt to load target-level counts to resolve coordinate-level RV counts.
    harps_targets, _ = _load_harps_targets()
    counts_map = {}
    for t in harps_targets:
        counts_map[(round(t["ra"], 8), round(t["dec"], 8))] = t.get("n_rv", 0)

    index: dict[tuple[int, int], list[tuple[float, float, int]]] = {}
    for ra, dec in harps_coords:
        key = (round(ra, 8), round(dec, 8))
        n_rv = counts_map.get(key, 1)
        rb = int((ra % 360.0) / bucket) % ra_bins
        db = int((dec + 90.0) / bucket)
        index.setdefault((rb, db), []).append((ra, dec, n_rv))

    ras, decs = cat_data.get("ra") or [], cat_data.get("dec") or []
    matched = 0
    for i, (ra, dec) in enumerate(zip(ras, decs)):
        if ra is None or dec is None:
            continue
        rb = int((ra % 360.0) / bucket) % ra_bins
        db = int((dec + 90.0) / bucket)
        hit_count = 0
        hit = False
        for dra in (-1, 0, 1):
            for ddec in (-1, 0, 1):
                for hra, hdec, hn_rv in index.get(((rb + dra) % ra_bins, db + ddec), ()):
                    if _angular_sep_arcsec(float(ra), float(dec), hra, hdec) <= tol:
                        hit = True
                        hit_count = max(hit_count, hn_rv)
        if hit:
            out[i] = hit_count if hit_count > 0 else 1
            matched += 1
    return out, matched


def _matching_harps_targets(coords: list[tuple[float, float]]) -> list[dict]:
    if not coords:
        return []
    harps_targets, _updated = _load_harps_targets()
    if not harps_targets:
        return []
    tol = max(0.0, _HARPS_MATCH_ARCSEC)
    matches = []
    seen = set()
    for entry in harps_targets:
        for ra, dec in coords:
            if _angular_sep_arcsec(ra, dec, entry["ra"], entry["dec"]) <= tol:
                key = (entry["target"], round(entry["ra"], 8), round(entry["dec"], 8))
                if key not in seen:
                    seen.add(key)
                    matches.append(entry)
                break
    return sorted(matches, key=lambda r: (r["target"].lower(), r["ra"], r["dec"]))


def _format_harps_cell(value: str | None) -> str:
    s = (value or "").strip()
    x = _toi_float(s)
    if x is None:
        return s
    return f"{x:.6f}".rstrip("0").rstrip(".")


def _row_matches_harps_query(
    row: dict,
    target_col: str,
    ra_col: str,
    dec_col: str,
    target_names: set[str],
    coords: list[tuple[float, float]],
) -> bool:
    if target_names and (row.get(target_col) or "").strip() in target_names:
        return True
    if not coords:
        return False
    ra = _coord_deg(row.get(ra_col), is_ra=True)
    dec = _coord_deg(row.get(dec_col), is_ra=False)
    if ra is None or dec is None:
        return False
    tol = max(0.0, _HARPS_MATCH_ARCSEC)
    return any(_angular_sep_arcsec(ra, dec, cra, cdec) <= tol for cra, cdec in coords)


def _query_harps_rvbank_rows(
    coords: list[tuple[float, float]],
    matching_targets: list[dict],
    max_rows: int | None = None,
) -> dict:
    """Return HARPS RVBank rows for matched target coordinates.

    Local CSV/ZIP is used first. If unavailable, the GitHub-hosted raw CSV is
    streamed and filtered online. ``total_rows`` is counted even when display
    rows are capped.
    """
    if max_rows is None:
        max_rows = _HARPS_TARGET_TABLE_MAX_ROWS
    target_names = {(m.get("target") or "").strip() for m in matching_targets if m.get("target")}
    cache_key = (
        "rows",
        tuple(sorted(target_names)),
        tuple((round(ra, 8), round(dec, 8)) for ra, dec in coords),
        max_rows,
        _harps_source_cache_key(),
    )
    cached = _harps_cache.get(cache_key)
    if cached is not None:
        return cached

    columns: list[str] = []
    rows: list[dict] = []
    total = 0
    source_kind = ""
    source_label = ""
    error = ""
    try:
        with _open_harps_rvbank_csv() as (source_kind, source_label, f):
            reader = csv.DictReader(f)
            columns = [c.strip() for c in (reader.fieldnames or []) if c is not None]
            col_map = {h.strip().lower(): h for h in (reader.fieldnames or [])}
            target_col = col_map.get("target")
            ra_col = col_map.get("ra")
            dec_col = col_map.get("dec")
            if not target_col or not ra_col or not dec_col:
                raise ValueError("HARPS RVBank CSV lacks target/ra/dec columns")
            for row in reader:
                if not _row_matches_harps_query(row, target_col, ra_col, dec_col, target_names, coords):
                    continue
                total += 1
                if len(rows) < max_rows:
                    rows.append({c: _format_harps_cell(row.get(c)) for c in columns})
    except Exception as exc:
        logger.warning("failed to query HARPS RVBank rows", exc_info=True)
        error = str(exc)

    result = {
        "columns": columns,
        "rows": rows,
        "total_rows": total,
        "display_rows": len(rows),
        "truncated": total > len(rows),
        "matched_targets": matching_targets,
        "source_kind": source_kind,
        "source": source_label,
        "error": error,
    }
    _harps_cache[cache_key] = result
    return result


def _target_lookup_aliases(name: str) -> set[str]:
    norm = _normalize_target_name(str(name or ""))
    aliases = {norm} if norm else set()
    m = re.fullmatch(r"TOI0*(\d+)(?:\.\d+)?", norm)
    if m:
        aliases.add(f"TOI{int(m.group(1))}")
    return aliases


def _target_tic_id(target_name: str, datasets: list[dict] | None = None) -> str:
    """Return a TIC identifier for target-page external links when available."""
    names = [target_name]
    names.extend(str(ds.get("object") or "") for ds in (datasets or []))
    for name in names:
        m = re.search(r"TIC[\s_-]*0*(\d+)", str(name or ""), flags=re.IGNORECASE)
        if m:
            return m.group(1)

    aliases: set[str] = set()
    for name in names:
        aliases.update(_target_lookup_aliases(name))
    if not aliases:
        return ""

    try:
        cat = _load_toi_catalog()["data"]
        n = len(cat.get("toi", []))
        for i in range(n):
            toi = str((cat.get("toi") or [""])[i] or "").strip()
            name = str((cat.get("name") or [""])[i] or "")
            row_aliases = _target_lookup_aliases(name)
            if toi:
                toi_num = _toi_float(toi)
                if toi_num is not None:
                    row_aliases.add(f"TOI{int(toi_num)}")
                row_aliases.add(_normalize_target_name(f"TOI-{toi}"))
            if not (aliases & row_aliases):
                continue
            tic = str((cat.get("tic") or [""])[i] or "")
            digits = re.sub(r"\D", "", tic)
            if digits:
                return digits
    except Exception:
        logger.warning("failed to resolve TIC ID from TOI catalog for %s", target_name, exc_info=True)

    try:
        cat = _load_nexsci_catalog()["data"]
        n = len(cat.get("name", []))
        for i in range(n):
            row_aliases = (
                _target_lookup_aliases((cat.get("name") or [""])[i])
                | _target_lookup_aliases((cat.get("host") or [""])[i])
            )
            if not (aliases & row_aliases):
                continue
            tic = str((cat.get("tic") or [""])[i] or "")
            digits = re.sub(r"\D", "", tic)
            if digits:
                return digits
    except Exception:
        logger.warning("failed to resolve TIC ID from NExScI catalog for %s", target_name, exc_info=True)

    return ""


def _target_catalog_coord_candidates(normalized_name: str) -> list[tuple[float, float]]:
    """Return TOI/NExScI catalog coordinates for a normalized target name.

    The target database can contain historical or header-derived coordinates.
    Catalog pages already use current catalog RA/Dec for HARPS matching, so the
    target page uses the same coordinates as a fallback when DB coordinates do
    not find a HARPS match.
    """
    aliases = _target_lookup_aliases(normalized_name)
    coords: list[tuple[float, float]] = []
    seen: set[tuple[float, float]] = set()

    def add_coord(ra_value, dec_value) -> None:
        ra = _coord_deg(ra_value, is_ra=True)
        dec = _coord_deg(dec_value, is_ra=False)
        if ra is None or dec is None or not (-90.0 <= dec <= 90.0):
            return
        key = (round(ra, 8), round(dec, 8))
        if key in seen:
            return
        seen.add(key)
        coords.append((ra, dec))

    def row_matches_toi_catalog(cat_data: dict, i: int) -> bool:
        toi = str((cat_data.get("toi") or [""])[i] or "").strip()
        if toi:
            toi_num = _toi_float(toi)
            if toi_num is not None and f"TOI{int(toi_num)}" in aliases:
                return True
            if _normalize_target_name(f"TOI-{toi}") in aliases:
                return True
        name = str((cat_data.get("name") or [""])[i] or "")
        return bool(_target_lookup_aliases(name) & aliases)

    try:
        cat = _load_toi_catalog()["data"]
        n = len(cat.get("toi", []))
        for i in range(n):
            if row_matches_toi_catalog(cat, i):
                add_coord(cat.get("ra", [None] * n)[i], cat.get("dec", [None] * n)[i])
    except Exception:
        logger.warning("failed to read TOI catalog coordinates for %s", normalized_name, exc_info=True)

    try:
        cat = _load_nexsci_catalog()["data"]
        n = len(cat.get("name", []))
        for i in range(n):
            name_aliases = _target_lookup_aliases((cat.get("name") or [""])[i])
            host_aliases = _target_lookup_aliases((cat.get("host") or [""])[i])
            if aliases & (name_aliases | host_aliases):
                add_coord(cat.get("ra", [None] * n)[i], cat.get("dec", [None] * n)[i])
    except Exception:
        logger.warning("failed to read NExScI catalog coordinates for %s", normalized_name, exc_info=True)

    return coords


def _harps_data_for_target(datasets: list[dict], target_name: str | None = None) -> dict:
    coords = []
    seen = set()
    def add_coord(ra_value, dec_value) -> None:
        ra = _coord_deg(ra_value, is_ra=True)
        dec = _coord_deg(dec_value, is_ra=False)
        if ra is None or dec is None:
            return
        key = (round(ra, 8), round(dec, 8))
        if key in seen:
            return
        seen.add(key)
        coords.append((ra, dec))

    for ds in datasets:
        add_coord(ds.get("ra"), ds.get("dec"))
    matches = _matching_harps_targets(coords)
    if target_name and not matches:
        for ra, dec in _target_catalog_coord_candidates(target_name):
            add_coord(ra, dec)
        matches = _matching_harps_targets(coords)
    if not matches and not coords:
        return {
            "columns": [],
            "rows": [],
            "total_rows": 0,
            "display_rows": 0,
            "truncated": False,
            "matched_targets": [],
            "source_kind": "",
            "source": "",
            "error": "",
        }
    return _query_harps_rvbank_rows(coords, matches)


def _load_toi_catalog() -> dict:
    """Read ``data/TOIs.csv`` into column-oriented arrays for the /toi page.
    All rows (every TFOPWG disposition, including FP/FA) are included so the
    candidate-type chips can filter them client-side. Cached by file mtime so
    the 8k-row CSV is parsed at most once per update."""
    path = _TOI_CATALOG_PATH
    empty = {"data": {k: [] for _, k, _ in _TOI_COLUMNS}, "n": 0, "updated": ""}
    try:
        mtime = path.stat().st_mtime_ns
    except OSError:
        return empty

    cached = _toi_cache.get("catalog")
    if cached is not None and cached[0] == mtime:
        return cached[1]

    data: dict[str, list] = {key: [] for _, key, _ in _TOI_COLUMNS}
    updated = ""
    with open(path, encoding="utf-8") as f:
        reader = csv.DictReader(f)
        # Build case-insensitive header lookup (TAP API folds identifiers to lowercase)
        col_map = {h.strip().lower(): h for h in (reader.fieldnames or [])}
        for row in reader:
            for header, key, kind in _TOI_COLUMNS:
                raw = row.get(col_map.get(header.strip().lower()))
                data[key].append(_toi_float(raw) if kind == "f" else (raw or "").strip())
            u = (row.get("Date TOI Updated (UTC)") or "").strip()
            if u > updated:
                updated = u

    result = {"data": data, "n": len(data["toi"]), "updated": updated}
    _toi_cache["catalog"] = (mtime, result)
    return result


# Boyle2026 stellar-rotation catalog (feather), merged onto TOIs by TIC ID.
# Path overridable so a refreshed/moved catalog doesn't require a code change.
_BOYLE_PATH = pathlib.Path(os.environ.get(
    "MUSCAT_BOYLE_CATALOG",
    str(HERE.parent.parent / "data" / "Boyle2026" / "final_catalog.feather"),
))

# (feather column == json key, kind) — kind "f" float, "i" int, "b" bool→0/1,
# "s" string. Only this subset is merged onto the /toi payload.
_BOYLE_COLUMNS: list[tuple[str, str]] = [
    ("ruwe", "f"),
    ("non_single_star", "i"),
    ("adopted_period", "f"),
    ("adopted_period_unc", "f"),
    ("flag_multiple_periods", "b"),
    ("flag_possible_binary", "b"),
    ("final_n_contams", "f"),
    ("flag_doubled_period", "b"),
    ("n_secs", "i"),
    ("n_sec_ratio", "f"),
    ("median_amplitude", "f"),
    ("sectors", "s"),
    ("sector_periods", "s"),
]

_boyle_cache: dict = {}
_TOI_CONFIRMED_PERIOD_REL_TOL = float(os.environ.get("MUSCAT_TOI_CONFIRMED_PERIOD_REL_TOL", "0.01"))
_TOI_CONFIRMED_PERIOD_ABS_TOL_D = float(os.environ.get("MUSCAT_TOI_CONFIRMED_PERIOD_ABS_TOL_D", "0.001"))


def _load_boyle_catalog() -> tuple[dict[str, list], dict[int, int]]:
    """Read the Boyle2026 catalog into ``(columns, tic_to_row)`` where
    ``columns`` holds JSON-safe per-column arrays (floats sanitized against
    NaN/inf, bools as 0/1) and ``tic_to_row`` maps TIC ID → row index.
    Cached by file mtime; returns empty structures when the file is absent
    or unreadable so the /toi page degrades gracefully."""
    empty: tuple[dict[str, list], dict[int, int]] = ({k: [] for k, _ in _BOYLE_COLUMNS}, {})
    try:
        mtime = _BOYLE_PATH.stat().st_mtime_ns
    except OSError:
        logger.warning("Boyle2026 catalog not found at %s; /toi merge columns will be empty", _BOYLE_PATH)
        return empty

    cached = _boyle_cache.get("catalog")
    if cached is not None and cached[0] == mtime:
        return cached[1]

    try:
        from pyarrow import feather

        table = feather.read_table(_BOYLE_PATH, columns=["TICID"] + [k for k, _ in _BOYLE_COLUMNS])
        raw = table.to_pydict()
    except Exception:
        logger.warning("failed to read Boyle2026 catalog %s", _BOYLE_PATH, exc_info=True)
        return empty

    cols: dict[str, list] = {}
    for key, kind in _BOYLE_COLUMNS:
        vals = raw[key]
        if kind == "f":
            cols[key] = [_toi_float(v) for v in vals]
        elif kind == "i":
            cols[key] = [None if v is None else int(v) for v in vals]
        elif kind == "b":
            cols[key] = [None if v is None else int(bool(v)) for v in vals]
        else:
            cols[key] = [(v or "").strip() if isinstance(v, str) else "" for v in vals]
    tic_to_row = {int(t): i for i, t in enumerate(raw["TICID"]) if t is not None}

    result = (cols, tic_to_row)
    _boyle_cache["catalog"] = (mtime, result)
    return result


def _merge_boyle_columns(cat_data: dict) -> tuple[dict[str, list], int]:
    """Left-join the Boyle2026 columns onto the TOI catalog rows by TIC ID.
    Returns ``(columns, n_matched)`` with one aligned array per Boyle column;
    unmatched rows get None (numeric) / "" (string)."""
    cols, tic_to_row = _load_boyle_catalog()
    tics = cat_data["tic"]
    n = len(tics)
    merged: dict[str, list] = {}
    for key, kind in _BOYLE_COLUMNS:
        merged[key] = ["" if kind == "s" else None] * n
    n_matched = 0
    for i in range(n):
        digits = re.sub(r"\D", "", tics[i]) if tics[i] else ""
        j = tic_to_row.get(int(digits)) if digits else None
        if j is None:
            continue
        n_matched += 1
        for key, _ in _BOYLE_COLUMNS:
            merged[key][i] = cols[key][j]
    return merged, n_matched


def _periods_match(a: float | None, b: float | None) -> bool:
    if a is None or b is None or a <= 0 or b <= 0:
        return False
    tol = max(_TOI_CONFIRMED_PERIOD_ABS_TOL_D, _TOI_CONFIRMED_PERIOD_REL_TOL * max(abs(a), abs(b)))
    return abs(a - b) <= tol


def _nasa_confirmed_toi_membership(cat_data: dict) -> tuple[list[int], list[str], int]:
    """Mark TOI rows that appear in the NASA confirmed-planet catalog.

    The TOI disposition remains the raw TFOPWG value. This overlay uses
    PSCompPars/NExScI as the confirmed-planet source and matches planet-level
    rows by TIC ID plus period. Exact normalized planet-name matches are kept as
    a fallback for rows where a confirmed planet carries a TOI-like name.
    """
    n = len(cat_data.get("toi", []))
    confirmed = [0] * n
    planet_names = [""] * n
    try:
        nx = _load_nexsci_catalog()["data"]
    except Exception:
        logger.warning("failed to read NExScI catalog for TOI confirmation overlay", exc_info=True)
        return confirmed, planet_names, 0

    by_tic: dict[int, list[tuple[float | None, str]]] = {}
    by_name: dict[str, str] = {}
    nx_names = nx.get("name", [])
    nx_periods = nx.get("period", [])
    nx_tics = nx.get("tic", [])
    for i, name in enumerate(nx_names):
        planet_name = str(name or "").strip()
        if planet_name:
            by_name.setdefault(_normalize_target_name(planet_name), planet_name)
        tic_digits = re.sub(r"\D", "", str(nx_tics[i] if i < len(nx_tics) else "") or "")
        if not tic_digits:
            continue
        period = nx_periods[i] if i < len(nx_periods) else None
        by_tic.setdefault(int(tic_digits), []).append((period, planet_name))

    n_matched = 0
    tois = cat_data.get("toi", [])
    names = cat_data.get("name", [])
    tics = cat_data.get("tic", [])
    periods = cat_data.get("period", [])
    for i in range(n):
        match_name = ""
        row_names = []
        if i < len(names) and names[i]:
            row_names.append(str(names[i]))
        if i < len(tois) and tois[i]:
            row_names.append(f"TOI-{tois[i]}")
        for row_name in row_names:
            match_name = by_name.get(_normalize_target_name(row_name), "")
            if match_name:
                break

        if not match_name:
            tic_digits = re.sub(r"\D", "", str(tics[i] if i < len(tics) else "") or "")
            period = periods[i] if i < len(periods) else None
            if tic_digits:
                for nx_period, nx_name in by_tic.get(int(tic_digits), []):
                    if _periods_match(period, nx_period):
                        match_name = nx_name
                        break

        if match_name:
            confirmed[i] = 1
            planet_names[i] = match_name
            n_matched += 1

    return confirmed, planet_names, n_matched


_toi_db_cache: dict = {}


def _db_target_identifiers(db: str) -> dict:
    """Index muscat-db target OBJECT names by the identifiers a TOI can be
    matched on — TIC id, TOI number (full and integer part), and normalized
    name — each mapped back to the DB target's normalized name (used as the
    target-page link). Cached by DB mtime."""
    key = _db_mtime(db)
    cached = _toi_db_cache.get("ids")
    if cached is not None and cached[0] == key:
        return cached[1]

    tic_to_norm: dict[int, str] = {}
    toi_to_norm: dict[str, str] = {}
    names: set[str] = set()
    for t in _get_targets(db):
        obj = t.get("object") or ""
        norm = _normalize_target_name(obj)
        names.add(norm)
        up = obj.upper()
        for m in re.finditer(r"TIC[\s_-]*0*(\d+)", up):
            tic_to_norm.setdefault(int(m.group(1)), norm)
        for m in re.finditer(r"TOI[\s_-]*0*(\d+(?:\.\d+)?)", up):
            num = m.group(1)
            toi_to_norm.setdefault(num, norm)
            toi_to_norm.setdefault(num.split(".")[0], norm)

    ids = {"tic": tic_to_norm, "toi": toi_to_norm, "names": names}
    _toi_db_cache["ids"] = (key, ids)
    return ids


def _toi_db_membership(cat_data: dict, db: str) -> tuple[list[int], list[str]]:
    """Return ``(indb, tname)`` per TOI row: ``indb`` is 1 when the object is in
    muscat-db, ``tname`` is the target-page link name (the matched DB target's
    normalized name, or a best-effort TOI/name fallback when not in the DB)."""
    ids = _db_target_identifiers(db)
    tic_map, toi_map, names = ids["tic"], ids["toi"], ids["names"]
    tics, tois, nms = cat_data["tic"], cat_data["toi"], cat_data["name"]
    n = len(tois)
    indb = [0] * n
    tname = [""] * n
    for i in range(n):
        link = None
        digits = re.sub(r"\D", "", tics[i]) if tics[i] else ""
        if digits:
            link = tic_map.get(int(digits))
        if link is None and tois[i]:
            link = toi_map.get(tois[i]) or toi_map.get(tois[i].split(".")[0])
        if link is None and nms[i]:
            nn = _normalize_target_name(nms[i])
            if nn in names:
                link = nn
        if link is not None:
            indb[i] = 1
            tname[i] = link
        elif tois[i]:
            tname[i] = _normalize_target_name(f"TOI-{tois[i]}")
        elif nms[i]:
            tname[i] = _normalize_target_name(nms[i])
    return indb, tname


_jwst_targets_cache: set[str] = set()


def _load_jwst_targets() -> set[str]:
    """Load unique JWST target names from data/jwst_targets.csv."""
    global _jwst_targets_cache
    if _jwst_targets_cache:
        return _jwst_targets_cache
    path = _JWST_TARGETS_PATH
    if not path.is_file():
        return set()
    try:
        with open(path, encoding="utf-8") as f:
            reader = csv.DictReader(f)
            _jwst_targets_cache = {row["pl_name"].strip() for row in reader if row.get("pl_name")}
    except Exception:
        logger.warning("failed to read JWST targets catalog %s", path, exc_info=True)
        return set()
    return _jwst_targets_cache


_spectra_targets_cache: set[str] = set()


def _load_spectra_targets() -> set[str]:
    """Load unique spectra target names from data/spectra_targets.csv or fetch from TAP if missing."""
    global _spectra_targets_cache
    if _spectra_targets_cache:
        return _spectra_targets_cache
    path = _SPECTRA_TARGETS_PATH
    if not path.is_file():
        logger.info("data/spectra_targets.csv not found, downloading from NASA Exoplanet Archive...")
        try:
            import urllib.parse
            query = "SELECT distinct pl_name FROM spectra"
            params = {"query": query, "format": "csv"}
            url = "https://exoplanetarchive.ipac.caltech.edu/TAP/sync?" + urllib.parse.urlencode(params)
            response = _sync_get(url, headers={"User-Agent": "MuSCAT-db/0.1.0"})
            content = response.text
            path.parent.mkdir(parents=True, exist_ok=True)
            with open(path, "w", encoding="utf-8") as f:
                f.write(content)
        except Exception as e:
            logger.warning("failed to download spectra targets from TAP: %s", e)
            return set()

    try:
        with open(path, encoding="utf-8") as f:
            reader = csv.DictReader(f)
            _spectra_targets_cache = {row["pl_name"].strip() for row in reader if row.get("pl_name")}
    except Exception:
        logger.warning("failed to read spectra targets catalog %s", path, exc_info=True)
        return set()
    return _spectra_targets_cache


_jwst_aliases_cache: set[str] = set()


def _load_jwst_targets_aliases() -> set[str]:
    """Load all aliases for the JWST targets."""
    global _jwst_aliases_cache
    if _jwst_aliases_cache:
        return _jwst_aliases_cache
    targets = _load_jwst_targets()
    aliases = set()
    for t in targets:
        aliases.update(_target_lookup_aliases(t))
    _jwst_aliases_cache = aliases
    return _jwst_aliases_cache


def _resolve_all_aliases(target_name: str, datasets: list[dict] | None = None) -> set[str]:
    """Resolve all possible aliases for a given target name, using TOI and NExScI catalogs."""
    names = [target_name]
    names.extend(str(ds.get("object") or "") for ds in (datasets or []))
    aliases: set[str] = set()
    for name in names:
        aliases.update(_target_lookup_aliases(name))

    try:
        cat = _load_toi_catalog()["data"]
        n = len(cat.get("toi", []))
        for i in range(n):
            toi = str((cat.get("toi") or [""])[i] or "").strip()
            name = str((cat.get("name") or [""])[i] or "")
            row_aliases = _target_lookup_aliases(name)
            if toi:
                toi_num = _toi_float(toi)
                if toi_num is not None:
                    row_aliases.add(f"TOI{int(toi_num)}")
                row_aliases.add(_normalize_target_name(f"TOI-{toi}"))
            if aliases & row_aliases:
                aliases.update(row_aliases)
                tic = str((cat.get("tic") or [""])[i] or "")
                digits = re.sub(r"\D", "", tic)
                if digits:
                    aliases.add(f"TIC{digits}")
                    aliases.add(f"TIC {digits}")
    except Exception:
        pass

    try:
        cat = _load_nexsci_catalog()["data"]
        n = len(cat.get("name", []))
        for i in range(n):
            name = str((cat.get("name") or [""])[i] or "")
            host = str((cat.get("host") or [""])[i] or "")
            tic = str((cat.get("tic") or [""])[i] or "")
            row_aliases = _target_lookup_aliases(name) | _target_lookup_aliases(host)
            if tic:
                digits = re.sub(r"\D", "", tic)
                if digits:
                    row_aliases.add(f"TIC{digits}")
                    row_aliases.add(f"TIC {digits}")
            if aliases & row_aliases:
                aliases.update(row_aliases)
    except Exception:
        pass

    return aliases


def _matched_jwst_targets(target_name: str, datasets: list[dict] | None = None) -> list[str]:
    """Return actual pl_name values from jwst_targets.csv matching target's aliases."""
    target_aliases = _resolve_all_aliases(target_name, datasets)
    jwst_targets = _load_jwst_targets()
    matched = []
    for t in jwst_targets:
        if _target_lookup_aliases(t) & target_aliases:
            matched.append(t)
    return matched


_spectra_aliases_cache: set[str] = set()


def _load_spectra_targets_aliases() -> set[str]:
    """Load all aliases for the spectra targets."""
    global _spectra_aliases_cache
    if _spectra_aliases_cache:
        return _spectra_aliases_cache
    targets = _load_spectra_targets()
    aliases = set()
    for t in targets:
        aliases.update(_target_lookup_aliases(t))
    _spectra_aliases_cache = aliases
    return _spectra_aliases_cache


def _matched_spectra_targets(target_name: str, datasets: list[dict] | None = None) -> list[str]:
    """Return actual pl_name values from spectra_targets.csv matching target's aliases."""
    target_aliases = _resolve_all_aliases(target_name, datasets)
    spectra_targets = _load_spectra_targets()
    matched = []
    for t in spectra_targets:
        if _target_lookup_aliases(t) & target_aliases:
            matched.append(t)
    return matched


# ── NASA Exoplanet Archive (NExScI) composite catalog ──────────────────────
# Column map for the /nexsci page: (csv header, json key, kind). "s" keeps the
# raw string, "f" parses a finite float (or null) via _toi_float. Header names
# verified against data/nexsci_pscomppars.csv — note the ra_x/dec_x suffixes.
_NEXSCI_COLUMNS: list[tuple[str, str, str]] = [
    ("pl_name", "name", "s"),
    ("hostname", "host", "s"),
    ("tic_id", "tic", "s"),
    ("discoverymethod", "method", "s"),
    ("disc_facility", "facility", "s"),
    ("st_spectype", "spectype", "s"),
    ("disc_year", "year", "f"),
    ("disc_pubdate", "pubdate", "s"),
    ("ra_x", "ra", "f"),
    ("dec_x", "dec", "f"),
    ("pl_orbper", "period", "f"),
    ("pl_orbsmax", "sma", "f"),
    ("pl_rade", "radius", "f"),
    ("pl_radj", "radj", "f"),
    ("pl_bmasse", "mass", "f"),
    ("pl_bmassj", "massj", "f"),
    ("pl_bmassprov", "bmassprov", "s"),
    ("pl_eqt", "teq", "f"),
    ("pl_insol", "insol", "f"),
    ("pl_ratror", "ratror", "f"),
    ("pl_trandep", "trandep", "f"),
    ("pl_trandur", "trandur", "f"),
    ("pl_imppar", "imppar", "f"),
    ("pl_orbincl", "incl", "f"),
    ("pl_orbeccen", "ecc", "f"),
    ("pl_dens", "pdens", "f"),
    ("st_teff", "steff", "f"),
    ("st_rad", "srad", "f"),
    ("st_mass", "smass", "f"),
    ("st_logg", "slogg", "f"),
    ("st_met", "smet", "f"),
    ("st_dens", "sdens", "f"),
    ("sy_dist", "dist", "f"),
    ("sy_vmag", "vmag", "f"),
    ("sy_tmag", "tmag", "f"),
    ("sy_gaiamag", "gmag", "f"),
    ("sy_kmag", "kmag", "f"),
    ("sy_snum", "snum", "f"),
    ("cb_flag", "cbflag", "f"),
    ("st_age", "age", "f"),
    ("st_ageerr1", "ageerr1", "f"),  # positive (upper) 1-sigma age uncertainty
    ("st_agelim", "agelim", "f"),    # archive limit flag: -1 lower, 0 value+error, 1 upper
    ("ttv_flag", "ttv", "f"),
    ("pl_projobliq", "projobliq", "f"),
    ("st_nrvc", "nrvc", "f"),
    ("st_nspec", "nspec", "f"),
    ("st_nphot", "nphot", "f"),
]

_nexsci_cache: dict = {}


_PUBDATES_PATH = HERE.parent.parent / "data" / "nexsci_pubdates.csv"
_pubdates_cache: dict[str, str] = {}


def _load_pubdates() -> dict[str, str]:
    """Load latest publication dates mapping from data/nexsci_pubdates.csv."""
    global _pubdates_cache
    if _pubdates_cache:
        return _pubdates_cache
    path = _PUBDATES_PATH
    if not path.is_file():
        return {}
    try:
        with open(path, encoding="utf-8") as f:
            reader = csv.DictReader(f)
            _pubdates_cache = {row["pl_name"].strip(): (row.get("pl_pubdate") or "").strip() for row in reader if row.get("pl_name")}
    except Exception:
        logger.warning("failed to read latest publication dates catalog %s", path, exc_info=True)
        return {}
    return _pubdates_cache


def _load_nexsci_catalog() -> dict:
    """Read ``data/nexsci_pscomppars.csv`` (NASA Exoplanet Archive Composite
    Planetary Systems — one row per confirmed planet) into column-oriented
    arrays for the /nexsci page. Cached by file mtime so the ~4.6k-row CSV is
    parsed at most once per update; degrades to empty when the (git-ignored)
    file is absent."""
    path = _NEXSCI_CATALOG_PATH
    empty = {"data": {k: [] for _, k, _ in _NEXSCI_COLUMNS}, "n": 0, "updated": ""}
    try:
        mtime = path.stat().st_mtime_ns
    except OSError:
        return empty

    cached = _nexsci_cache.get("catalog")
    if cached is not None and cached[0] == mtime:
        return cached[1]

    pubdates = _load_pubdates()

    data: dict[str, list] = {key: [] for _, key, _ in _NEXSCI_COLUMNS}
    with open(path, encoding="utf-8") as f:
        for row in csv.DictReader(f):
            for header, key, kind in _NEXSCI_COLUMNS:
                if key == "pubdate":
                    pl_name = row.get("pl_name", "").strip()
                    raw = pubdates.get(pl_name) or row.get(header)
                else:
                    raw = row.get(header)
                data[key].append(_toi_float(raw) if kind == "f" else (raw or "").strip())
            # Fallback for transit radius ratio if empty
            if data["ratror"][-1] is None:
                rade = data["radius"][-1]
                srad = data["srad"][-1]
                if rade is not None and srad is not None and srad > 0:
                    data["ratror"][-1] = (rade / srad) * (6378.1 / 695700.0)
            # Fallback for planet radius in Jupiter radii if empty
            if data["radj"][-1] is None:
                rade = data["radius"][-1]
                if rade is not None:
                    data["radj"][-1] = rade / 11.2089
            # Fallback for planet mass in Jupiter masses if empty
            if data["massj"][-1] is None:
                masse = data["mass"][-1]
                if masse is not None:
                    data["massj"][-1] = masse / 317.828

    # The composite table has no per-row date column, so surface the file's own
    # modification date as the catalog "last updated" stamp.
    updated = datetime.date.fromtimestamp(mtime / 1e9).isoformat()
    result = {"data": data, "n": len(data["name"]), "updated": updated}
    _nexsci_cache["catalog"] = (mtime, result)
    return result


def _nexsci_db_membership(cat_data: dict, db: str) -> tuple[list[int], list[str]]:
    """Return ``(indb, tname)`` per NExScI row: ``indb`` is 1 when the planet's
    host is in muscat-db (matched by TIC id, else by normalized host name), and
    ``tname`` is the matched DB target's normalized name (the /target link).
    Rows with no muscat-db match get ``indb=0`` and an empty ``tname`` — the
    page then falls back to the NASA Exoplanet Archive overview link, built
    client-side from the archive's canonically-hyphenated ``host`` name."""
    ids = _db_target_identifiers(db)
    tic_map, names = ids["tic"], ids["names"]
    tics, hosts = cat_data["tic"], cat_data["host"]
    n = len(hosts)
    indb = [0] * n
    tname = [""] * n
    for i in range(n):
        link = None
        digits = re.sub(r"\D", "", tics[i]) if tics[i] else ""
        if digits:
            link = tic_map.get(int(digits))
        if link is None and hosts[i]:
            nn = _normalize_target_name(hosts[i])
            if nn in names:
                link = nn
        if link is not None:
            indb[i] = 1
            tname[i] = link
    return indb, tname


# MuSCAT OBJECT values can carry an observing-date and coordinate decoration
# after an otherwise valid TOI name, e.g.
# ``TOI-179b_231015 J025710-560913``.  Match the complete known decoration so
# legitimate underscores such as the one in ``TOI_2457`` are never truncated.
_DECORATED_TOI_OBJECT_RE = re.compile(
    r"^(?P<target>TOI(?:[ _-]*)0*\d+(?:\.\d+|[B-H])?)_"
    r"\d{6}\s+J\d{6}[+-]\d{6}$",
    re.IGNORECASE,
)


# Helper to normalize target names for comparison
def _normalize_target_name(t: str) -> str:
    # Parse recognized TOI spellings before removing punctuation.  Otherwise a
    # malformed value such as ``TOI06209-01`` would be reinterpreted as TOI
    # 620901 after the hyphen is discarded.  TOI comparison keys represent the
    # host, so candidate-number and confirmed-planet suffixes are omitted.
    raw = t.strip().upper()
    decorated_toi = _DECORATED_TOI_OBJECT_RE.fullmatch(raw)
    if decorated_toi:
        raw = decorated_toi.group("target")
    toi_match = re.fullmatch(
        r"TOI(?:[ _-]*)0*(\d+)(?:\.\d+)?(?:\s*[B-H])?",
        raw,
    )
    if toi_match:
        return f"TOI{int(toi_match.group(1))}"

    s = t.strip().upper().replace(" ", "").replace("-", "").replace("_", "")
    s = re.sub(r"\.\d+$", "", s)
    if len(s) > 2 and s[-1] in "BCDEFGH":
        return s[:-1]
    return s


def _safe_float(value) -> float | None:
    """Parse a value to float, returning None for blanks/invalid input."""
    if value is None:
        return None
    try:
        s = str(value).strip()
        if not s:
            return None
        return float(s)
    except (TypeError, ValueError):
        return None


def _get_err(row: dict, key_base: str) -> float | None:
    """Extract and average positive and negative uncertainties if available, or return one."""
    err1 = _safe_float(row.get(key_base + "err1"))
    err2 = _safe_float(row.get(key_base + "err2"))
    if err1 is not None and err2 is not None:
        return (abs(err1) + abs(err2)) / 2.0
    if err1 is not None:
        return abs(err1)
    if err2 is not None:
        return abs(err2)
    return None


# Per-target catalog lookups (NASA/TOI archive + local CSV). Bounded + locked:
# keyed per distinct query string, it otherwise grows once per unique target.
_CATALOG_CACHE_MAX = int(os.environ.get("MUSCAT_CATALOG_CACHE_MAX", "512"))
_CATALOG_CACHE = LRUCache(maxsize=_CATALOG_CACHE_MAX)
# Distinguishes "absent" from a legitimately cached None (see _query_target_coordinates).
_CACHE_MISS = object()


def _adql_literal(value: str) -> str:
    """Quote a string as an ADQL literal, escaping embedded apostrophes."""
    return "'" + value.replace("'", "''") + "'"


def _query_target_planets_nasa(target: str) -> dict:
    import urllib.parse

    target_clean = target.strip().upper()
    cache_key = "nasa_" + target_clean
    cached = _CATALOG_CACHE.get(cache_key, _CACHE_MISS)
    if cached is not _CACHE_MISS:
        return cached

    results = {}
    target_norm = _normalize_target_name(target)

    # 1. Local database search (nexsci_pscomppars.csv)
    try:
        csv_path = pathlib.Path(HERE).parent.parent / "data" / "nexsci_pscomppars.csv"
        if csv_path.exists():
            with open(csv_path, errors="replace") as f:
                reader = csv.DictReader(f)
                for row in reader:
                    h_name = row.get("hostname", "")
                    p_name = row.get("pl_name", "")
                    tic = row.get("tic_id", "")
                    if (h_name and _normalize_target_name(h_name) == target_norm) or \
                       (p_name and _normalize_target_name(p_name) == target_norm) or \
                       (tic and _normalize_target_name(tic) == target_norm):
                        pl_letter = (row.get("pl_letter") or "").strip().lower()
                        if not pl_letter:
                            pn = (row.get("pl_name") or "").strip()
                            parts = pn.rsplit(None, 1)
                            if len(parts) == 2 and len(parts[1]) == 1 and parts[1].isalpha():
                                pl_letter = parts[1].lower()
                        t0 = row.get("pl_tranmid")
                        per = row.get("pl_orbper")
                        if pl_letter and t0 is not None and per is not None:
                            try:
                                entry = {"t0": float(t0), "period": float(per)}
                            except ValueError:
                                continue
                            dur = _safe_float(row.get("pl_trandur"))  # hours
                            if dur is not None:
                                entry["duration"] = dur
                            # Extract uncertainties
                            t0_unc = _get_err(row, "pl_tranmid")
                            per_unc = _get_err(row, "pl_orbper")
                            dur_unc = _get_err(row, "pl_trandur")
                            if t0_unc is not None:
                                entry["t0_unc"] = t0_unc
                            if per_unc is not None:
                                entry["period_unc"] = per_unc
                            if dur_unc is not None:
                                entry["duration_unc"] = dur_unc
                            results[pl_letter] = entry
    except Exception:
        logger.debug("failed local NASA ephemeris lookup for %s", target, exc_info=True)

    # 1b. Fallback local database search (nexsci_ps.csv)
    if not results:
        try:
            csv_path = pathlib.Path(HERE).parent.parent / "data" / "nexsci_ps.csv"
            if csv_path.exists():
                tokens = [t for t in re.split(r'[^0-9a-zA-Z]', target.lower()) if t and t not in ("toi", "tic", "hd", "hip")]
                if not tokens:
                    tokens = [target.lower()]
                with open(csv_path, errors="replace") as f:
                    header_line = f.readline()
                    header = [h.strip('"') for h in next(csv.reader([header_line]))]
                    for line in f:
                        line_lower = line.lower()
                        if not any(token in line_lower for token in tokens):
                            continue
                        row_values = next(csv.reader([line]))
                        row = dict(zip(header, row_values))
                        h_name = row.get("hostname", "")
                        p_name = row.get("pl_name", "")
                        tic = row.get("tic_id", "")
                        if (h_name and _normalize_target_name(h_name) == target_norm) or \
                           (p_name and _normalize_target_name(p_name) == target_norm) or \
                           (tic and _normalize_target_name(tic) == target_norm):
                            pl_letter = (row.get("pl_letter") or "").strip().lower()
                            if not pl_letter:
                                pn = (row.get("pl_name") or "").strip()
                                parts = pn.rsplit(None, 1)
                                if len(parts) == 2 and len(parts[1]) == 1 and parts[1].isalpha():
                                    pl_letter = parts[1].lower()
                            t0 = row.get("pl_tranmid")
                            per = row.get("pl_orbper")
                            if pl_letter and t0 and per:
                                try:
                                    entry = {"t0": float(t0), "period": float(per)}
                                except ValueError:
                                    continue
                                dur = _safe_float(row.get("pl_trandur"))
                                if dur is not None:
                                    entry["duration"] = dur
                                t0_unc = _get_err(row, "pl_tranmid")
                                per_unc = _get_err(row, "pl_orbper")
                                dur_unc = _get_err(row, "pl_trandur")
                                if t0_unc is not None:
                                    entry["t0_unc"] = t0_unc
                                if per_unc is not None:
                                    entry["period_unc"] = per_unc
                                if dur_unc is not None:
                                    entry["duration_unc"] = dur_unc

                                is_default = (row.get("default_flag") == "1")
                                if is_default or pl_letter not in results:
                                    results[pl_letter] = entry
        except Exception:
            logger.debug("failed local fallback NASA ephemeris lookup for %s", target, exc_info=True)

    # 2. Online search
    if not results:
        # Clean target to find host name. E.g. "TOI 4600 b" -> "TOI 4600"
        host = target.strip()
        if len(host) > 2 and host[-2] == " " and host[-1].lower() in "bcdefgh":
            host = host[:-2].strip()

        cols = [
            "pl_name", "pl_tranmid", "pl_tranmiderr1", "pl_tranmiderr2",
            "pl_orbper", "pl_orbpererr1", "pl_orbpererr2",
            "pl_trandur", "pl_trandurerr1", "pl_trandurerr2"
        ]
        col_str = ", ".join(cols)
        q = f"SELECT {col_str} FROM pscomppars WHERE hostname = {_adql_literal(host)} OR hostname LIKE {_adql_literal(host + '%')}"
        url = 'https://exoplanetarchive.ipac.caltech.edu/TAP/sync?' + urllib.parse.urlencode({'query': q, 'format': 'json'})
        try:
            data = _sync_get(url, headers={'User-Agent': 'Mozilla/5.0'}).json()
            for row in data:
                pl_name = row.get("pl_name", "")
                if pl_name and len(pl_name) > 2 and pl_name[-2] == " ":
                    letter = pl_name[-1].lower()
                    t0 = row.get("pl_tranmid")
                    per = row.get("pl_orbper")
                    if letter and t0 is not None and per is not None:
                        entry = {"t0": float(t0), "period": float(per)}
                        dur = _safe_float(row.get("pl_trandur"))  # hours
                        if dur is not None:
                            entry["duration"] = dur
                        # Extract uncertainties
                        t0_unc = _get_err(row, "pl_tranmid")
                        per_unc = _get_err(row, "pl_orbper")
                        dur_unc = _get_err(row, "pl_trandur")
                        if t0_unc is not None:
                            entry["t0_unc"] = t0_unc
                        if per_unc is not None:
                            entry["period_unc"] = per_unc
                        if dur_unc is not None:
                            entry["duration_unc"] = dur_unc
                        results[letter] = entry
        except Exception:
            logger.debug("failed online NASA ephemeris lookup for %s", target, exc_info=True)

    _CATALOG_CACHE[cache_key] = results
    return results


def _query_target_coordinates(target: str) -> dict | None:
    target_clean = target.strip().upper()
    cache_key = "coords_" + target_clean
    cached = _CATALOG_CACHE.get(cache_key, _CACHE_MISS)
    if cached is not _CACHE_MISS:
        return cached

    target_norm = _normalize_target_name(target)

    def _store(coords: dict | None) -> dict | None:
        _CATALOG_CACHE[cache_key] = coords
        return coords

    def _coords_from_nasa_row(row: dict) -> dict | None:
        ra = _safe_float(row.get("ra_x"))
        dec = _safe_float(row.get("dec_x"))
        if ra is None or dec is None:
            return None
        return {"ra": ra, "dec": dec, "source": "nasa"}

    def _coords_from_toi_row(row: dict) -> dict | None:
        ra = _safe_float(row.get("ra_deg"))
        dec = _safe_float(row.get("dec_deg"))
        if ra is None or dec is None:
            ra = _safe_float(row.get("RA"))
            dec = _safe_float(row.get("Dec"))
        if ra is None or dec is None:
            return None
        return {"ra": ra, "dec": dec, "source": "toi"}

    try:
        csv_path = pathlib.Path(HERE).parent.parent / "data" / "nexsci_pscomppars.csv"
        if csv_path.exists():
            with open(csv_path, errors="replace") as f:
                reader = csv.DictReader(f)
                for row in reader:
                    h_name = row.get("hostname", "")
                    p_name = row.get("pl_name", "")
                    tic = row.get("tic_id", "")
                    if (
                        h_name and _normalize_target_name(h_name) == target_norm
                    ) or (
                        p_name and _normalize_target_name(p_name) == target_norm
                    ) or (
                        tic and _normalize_target_name(tic) == target_norm
                    ):
                        coords = _coords_from_nasa_row(row)
                        if coords:
                            return _store(coords)
    except Exception:
        logger.debug("failed local coordinate lookup in NASA cache for %s", target, exc_info=True)

    try:
        csv_path = pathlib.Path(HERE).parent.parent / "data" / "TOIs.csv"
        if csv_path.exists():
            with open(csv_path, errors="replace") as f:
                reader = csv.DictReader(f)
                for row in reader:
                    row = {k.lower(): v for k, v in row.items()}
                    toi_val = row.get("toi", "")
                    tic_val = row.get("tic id", "")
                    match = False
                    if toi_val and _normalize_target_name("TOI" + toi_val) == target_norm:
                        match = True
                    elif tic_val and (
                        _normalize_target_name(tic_val) == target_norm
                        or _normalize_target_name("TIC" + tic_val) == target_norm
                    ):
                        match = True
                    elif row.get("planet name") and _normalize_target_name(row.get("planet name", "")) == target_norm:
                        match = True
                    if match:
                        coords = _coords_from_toi_row(row)
                        if coords:
                            return _store(coords)
    except Exception:
        logger.debug("failed local coordinate lookup in TOI cache for %s", target, exc_info=True)

    return _store(None)


def _resolve_archive_coords(target: str) -> tuple[float, float, str] | None:
    """Resolve a target name to (ra_deg, dec_deg, source) for archive searches.

    Tries the offline NASA/TOI catalogs first (fast, cached, no network), then
    falls back to SIMBAD name resolution. Returns None when the name cannot be
    resolved by any source. Both results are cached.
    """
    coords = _query_target_coordinates(target)
    if coords is not None:
        return float(coords["ra"]), float(coords["dec"]), str(coords.get("source") or "catalog")

    cache_key = "simbad_" + target.strip().upper()
    cached = _CATALOG_CACHE.get(cache_key, _CACHE_MISS)
    if cached is not _CACHE_MISS:
        return cached

    radec = exp_calc.resolve_target_coords(target)
    result = (float(radec[0]), float(radec[1]), "simbad") if radec else None
    _CATALOG_CACHE[cache_key] = result
    return result


def _query_target_planets_toi(target: str) -> dict:
    import urllib.parse

    target_clean = target.strip().upper()
    cache_key = "toi_" + target_clean
    cached = _CATALOG_CACHE.get(cache_key, _CACHE_MISS)
    if cached is not _CACHE_MISS:
        return cached

    results = {}
    target_norm = _normalize_target_name(target)

    # 1. Local database search (TOIs.csv)
    try:
        csv_path = pathlib.Path(HERE).parent.parent / "data" / "TOIs.csv"
        if csv_path.exists():
            with open(csv_path, errors="replace") as f:
                reader = csv.DictReader(f)
                for row in reader:
                    row = {k.lower(): v for k, v in row.items()}
                    toi_val = row.get("toi", "")
                    tic_val = row.get("tic id", "")
                    match = False
                    if toi_val and _normalize_target_name("TOI" + toi_val) == target_norm:
                        match = True
                    elif tic_val and (
                        _normalize_target_name(tic_val) == target_norm or
                        _normalize_target_name("TIC" + tic_val) == target_norm
                    ):
                        match = True
                    elif row.get("planet name") and _normalize_target_name(row.get("planet name", "")) == target_norm:
                        match = True

                    if match:
                        try:
                            parts = toi_val.split(".")
                            if len(parts) == 2:
                                candidate_num = int(parts[1])
                                letter = chr(ord('b') + candidate_num - 1)
                            else:
                                letter = "b"
                        except Exception:
                            letter = "b"
                        t0 = row.get("epoch (bjd)")
                        per = row.get("period (days)")
                        if t0 is not None and per is not None:
                            try:
                                entry = {"t0": float(t0), "period": float(per)}
                            except ValueError:
                                continue
                            dur = _safe_float(row.get("duration (hours)"))
                            if dur is not None:
                                entry["duration"] = dur
                            # Extract uncertainties
                            t0_unc = _safe_float(row.get("epoch (bjd) err"))
                            per_unc = _safe_float(row.get("period (days) err"))
                            dur_unc = _safe_float(row.get("duration (hours) err"))
                            if t0_unc is not None:
                                entry["t0_unc"] = t0_unc
                            if per_unc is not None:
                                entry["period_unc"] = per_unc
                            if dur_unc is not None:
                                entry["duration_unc"] = dur_unc
                            results[letter] = entry
    except Exception:
        logger.debug("failed local TOI ephemeris lookup for %s", target, exc_info=True)

    # 2. Online search
    if not results:
        host = target.strip()
        if len(host) > 2 and host[-2] == " " and host[-1].lower() in "bcdefgh":
            host = host[:-2].strip()
        q = f"SELECT toidisplay, pl_tranmid, pl_tranmiderr1, pl_tranmiderr2, pl_orbper, pl_orbpererr1, pl_orbpererr2, pl_trandurh, pl_trandurherr1, pl_trandurherr2 FROM toi WHERE toidisplay LIKE {_adql_literal(host + '%')}"
        url = 'https://exoplanetarchive.ipac.caltech.edu/TAP/sync?' + urllib.parse.urlencode({'query': q, 'format': 'json'})
        try:
            data = _sync_get(url, headers={'User-Agent': 'Mozilla/5.0'}).json()
            for row in data:
                toidisplay = row.get("toidisplay", "")
                if toidisplay:
                    base_name = toidisplay.split(".")[0].strip()
                    if _normalize_target_name(base_name) != target_norm:
                        continue
                t0 = row.get("pl_tranmid")
                per = row.get("pl_orbper")
                if toidisplay and t0 is not None and per is not None:
                    parts = toidisplay.split(".")
                    if len(parts) == 2:
                        try:
                            candidate_num = int(parts[1])
                            letter = chr(ord('b') + candidate_num - 1)
                            entry = {"t0": float(t0), "period": float(per)}
                        except Exception:
                            continue
                        dur = _safe_float(row.get("pl_trandurh"))  # hours
                        if dur is not None:
                            entry["duration"] = dur
                        # Extract uncertainties
                        t0_unc = _get_err(row, "pl_tranmid")
                        per_unc = _get_err(row, "pl_orbper")
                        dur_unc = _get_err(row, "pl_trandurh")
                        if t0_unc is not None:
                            entry["t0_unc"] = t0_unc
                        if per_unc is not None:
                            entry["period_unc"] = per_unc
                        if dur_unc is not None:
                            entry["duration_unc"] = dur_unc
                        results[letter] = entry
        except Exception:
            logger.debug("failed online TOI ephemeris lookup for %s", target, exc_info=True)

    _CATALOG_CACHE[cache_key] = results
    return results


# Helper to query all planet ephemerides for a target from catalogs
def _query_target_planets_catalog(target: str) -> dict:
    target_clean = target.strip().upper()
    cached = _CATALOG_CACHE.get(target_clean, _CACHE_MISS)
    if cached is not _CACHE_MISS:
        return cached

    results = dict(_query_target_planets_nasa(target))
    if not results:
        results = dict(_query_target_planets_toi(target))

    # Check local muscatdb_targets_old.csv if still empty and file exists
    if not results:
        target_norm = _normalize_target_name(target)
        try:
            csv_path = pathlib.Path(HERE).parent.parent / "data" / "muscatdb_targets_old.csv"
            if csv_path.exists():
                with open(csv_path, errors="replace") as f:
                    reader = csv.DictReader(f, delimiter=";")
                    for row in reader:
                        name_val = (row.get("name") or "").strip()
                        if name_val and _normalize_target_name(name_val) == target_norm:
                            period_raw = row.get("period") or row.get("period_sg1")
                            t0_raw = row.get("t0") or row.get("t0_sg1")
                            if not period_raw or not t0_raw:
                                break
                            try:
                                results["b"] = {
                                    "t0": float(t0_raw),
                                    "period": float(period_raw),
                                }
                            except ValueError:
                                pass
                            break
        except Exception:
            logger.debug("failed legacy catalog fallback for %s", target, exc_info=True)

    _CATALOG_CACHE[target_clean] = results
    return results


def _global_ads_token() -> str:
    return (
        os.environ.get("ADS_API_TOKEN")
        or os.environ.get("ADS_DEV_KEY")
        or os.environ.get("ADS_TOKEN")
        or ""
    ).strip()


def _ads_token_for_request(request: Request | None) -> tuple[str, str | None]:
    user = _request_user(request) if request is not None else None
    if user:
        try:
            token = get_user_ads_token(user)
        except UserSettingsError:
            token = None
        if token:
            return token, "user"
    token = _global_ads_token()
    return (token, "global") if token else ("", None)
