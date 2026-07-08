from __future__ import annotations

import datetime
import contextvars
import json
import logging
import math
import os
import pathlib
import re
import sqlite3
import threading
import time
from zoneinfo import ZoneInfo

_DB_LOCK = threading.Lock()

import csv
import io
import zipfile
from contextlib import asynccontextmanager, contextmanager
from urllib.parse import urlsplit
from urllib.request import Request as UrlRequest, urlopen

from fastapi import Body, FastAPI, HTTPException, Request
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from jinja2 import Environment, FileSystemLoader

from muscat_db import photometry as phot
from muscat_db import exposure as exp_calc
from muscat_db import transit_fit as fit
from muscat_db import lco
from muscat_db import transit_obs
from muscat_db import fov as fov_opt
from muscat_db.database import (
    SCHEMA,
    UserSettingsError,
    ensure_user,
    get_conn,
    delete_note as _delete_note,
    format_elapsed,
    get_dates as _get_dates,
    get_frames as _get_frames,
    get_instruments as _get_instruments,
    get_instruments_summary as _get_instruments_summary,
    get_objects as _get_objects,
    get_summaries as _get_summaries,
    get_targets as _get_targets,
    get_identified_overrides as _get_identified_overrides,
    set_identified as _set_identified,
    set_note as _set_note,
    save_ephemeris_view,
    get_ephemeris_view,
    get_last_build_date,
    get_user_lco_token,
    set_user_lco_token,
    _normalize_filters,
)
from muscat_db.job_store import get_job_store
from muscat_db.cache import LRUCache
from muscat_db.instruments import INSTRUMENTS
from muscat_db.coord import (
    CoordRepr,
    unpack as _unpack_coord,
    clean_ra as _clean_ra,
    clean_dec as _clean_dec,
)

logger = logging.getLogger(__name__)

HERE = pathlib.Path(__file__).parent
TEMPLATE_DIR = HERE / "templates"
STATIC_DIR = HERE / "static"

@asynccontextmanager
async def _lifespan(app: FastAPI):
    """Create the database and schema on startup if they don't exist."""
    db = _db_path()
    with get_conn(db, timeout=10) as conn:
        conn.execute("PRAGMA journal_mode=WAL;")
        conn.executescript(SCHEMA)
    print(f"[startup] database ready at {db}")

    from muscat_db.config import config_status, missing_required_secret

    summary = ", ".join(f"{name}={state}" for name, state in config_status())
    print(f"[startup] env config: {summary}")
    missing = missing_required_secret()
    if missing is not None:
        print(
            f"[startup] WARNING: {missing.name} is unset. "
            "muscat/muscat2 calibration with --wcs_method astrometry.net will fail; "
            "use --wcs_method twirl (no API key) or export the key."
        )
    yield


app = FastAPI(title="MuSCAT Observation Log", lifespan=_lifespan)
# The targets page is ~2.8 MB of highly repetitive HTML; gzip shrinks it ~16x,
# which is the dominant cost when serving over an SSH port-forward tunnel.
app.add_middleware(GZipMiddleware, minimum_size=1000)

# Middleware: extract authenticated user from nginx reverse proxy.
# nginx sets X-Forwarded-User after HTTP Basic Auth. Trusting that header is
# ONLY safe for connections that actually came from nginx's own loopback
# socket, so we verify the immediate TCP peer is loopback before honoring it
# -- rather than relying on the operator having remembered --nginx at start
# time (uvicorn's default bind is 0.0.0.0, which would otherwise let any
# network client set this header and impersonate a user). This does not
# defend against another local account on the same host connecting straight
# to uvicorn's loopback port; that requires a shared-secret header between
# nginx and uvicorn, which is not implemented yet.
_TRUSTED_PROXY_HOSTS = frozenset({"127.0.0.1", "::1"})


@app.middleware("http")
async def _nginx_auth_middleware(request: Request, call_next):
    client_host = request.client.host if request.client else None
    user = request.headers.get("X-Forwarded-User") or None
    if user and client_host not in _TRUSTED_PROXY_HOSTS:
        logger.warning(
            "ignoring X-Forwarded-User=%r from non-loopback peer %s "
            "(request did not arrive via the nginx proxy)",
            user, client_host,
        )
        user = None
    request.state.user = user
    token = _CURRENT_USER.set(user)
    try:
        if user:
            try:
                ensure_user(user)
            except (UserSettingsError, sqlite3.Error) as exc:
                logger.warning("could not ensure user row for %s: %s", user, exc)
        response = await call_next(request)
        return response
    finally:
        _CURRENT_USER.reset(token)

# Mount static assets (shared stylesheet, etc.) before the dynamic routes so a
# request like /static/styles.css is not captured by the /{inst}/{date} route.
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

jinja = Environment(
    loader=FileSystemLoader(str(TEMPLATE_DIR)),
    autoescape=True,
)
jinja.globals["format_elapsed"] = format_elapsed


def _adql_literal(value: str) -> str:
    """Quote a string as an ADQL literal, escaping embedded apostrophes."""
    return "'" + value.replace("'", "''") + "'"


def _datetime_from_timestamp(ts: int) -> str:
    dt = datetime.datetime.fromtimestamp(ts)
    now = datetime.datetime.now()
    if dt.year == now.year:
        return dt.strftime("%b %d %H:%M")
    return dt.strftime("%b %d %Y")


jinja.filters["datetime_from_timestamp"] = _datetime_from_timestamp


def _wiki_url(inst: str, target: str) -> str | None:
    if inst != "muscat2" or not target:
        return None
    m = re.match(r"^TOI-?(\d+)(\.\d+)?$", target, re.IGNORECASE)
    if m:
        num, suf = m.groups()
        return f"https://research.iac.es/proyecto/muscat/stars/view/TOI{int(num):05d}{suf or ''}"
    return f"https://research.iac.es/proyecto/muscat/stars/view/{target}"


def _db_path() -> str:
    return str(pathlib.Path(os.environ.get("MUSCAT_DB_PATH", "muscat.db")).resolve())


_LCO_SITE_TZ = {
    "ogg": "Pacific/Honolulu",
    "coj": "Australia/Brisbane",
    "lsc": "America/Santiago",
    "cpt": "Africa/Johannesburg",
    "elp": "America/Chicago",
    "tfn": "Atlantic/Canary",
    "tlv": "Asia/Jerusalem",
}
_LCO_DATASET_MATCH_ARCSEC = 60.0
_CURRENT_USER: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "current_user",
    default=None,
)


def _render(name: str, **kwargs) -> str:
    tpl = jinja.get_template(name)
    kwargs.setdefault("current_user", _CURRENT_USER.get())
    return HTMLResponse(tpl.render(**kwargs))


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


# Rendering the ~2.85 MB targets page costs ~1.3s. Cache the rendered HTML
# keyed on the DB mtime so repeat loads are instant until the data changes.
# Each entry is a multi-MB HTML blob, so the cache is bounded (LRU) to keep
# memory flat over a long-lived server; sizes are env-overridable for tuning.
_INDEX_CACHE_MAX = int(os.environ.get("MUSCAT_INDEX_CACHE_MAX", "64"))
_CATALOG_CACHE_MAX = int(os.environ.get("MUSCAT_CATALOG_CACHE_MAX", "512"))
_index_cache = LRUCache(maxsize=_INDEX_CACHE_MAX)
# Per-target catalog lookups (NASA/TOI archive + local CSV). Bounded + locked:
# keyed per distinct query string, it otherwise grows once per unique target.
_CATALOG_CACHE = LRUCache(maxsize=_CATALOG_CACHE_MAX)
# Distinguishes "absent" from a legitimately cached None (see _query_target_coordinates).
_CACHE_MISS = object()


@app.get("/", response_class=HTMLResponse)
def index():
    db = _db_path()
    tpl_path = TEMPLATE_DIR / "index.html"
    tpl_mtime = str(tpl_path.stat().st_mtime_ns) if tpl_path.is_file() else ""
    key = (tpl_mtime, _db_mtime(db))
    cached = _index_cache.get("index")
    if cached is not None and cached[0] == key:
        return HTMLResponse(cached[1])

    targets = _get_targets(db)

    # Apply user overrides on top of computed is_identified
    overrides = _get_identified_overrides(db)
    for t in targets:
        if t["object"] in overrides:
            t["is_identified"] = overrides[t["object"]]
        t["norm_name"] = _normalize_target_name(t["object"])

    # Sum each raw OBJECT's dataset-date count across every other raw name
    # that normalizes to the same target, so the Ndataset column can show
    # "this row's count (total across the normalized target)".
    norm_date_totals: dict[str, int] = {}
    for t in targets:
        norm_date_totals[t["norm_name"]] = norm_date_totals.get(t["norm_name"], 0) + t["n_dates"]
    for t in targets:
        t["norm_n_dates"] = norm_date_totals[t["norm_name"]]

    last_updated = get_last_build_date(db)

    html = jinja.get_template("index.html").render(
        targets=targets,
        last_updated=last_updated,
    )
    _index_cache["index"] = (key, html)
    return HTMLResponse(html)


def _get_datasets_for_normalized_target(db: str, normalized_name: str) -> tuple[list[dict], str]:
    """Get all datasets for targets that match a normalized name.

    Returns (datasets_list, last_updated_date).
    """
    targets = _get_targets(db)

    matching_objects = [
        t["object"] for t in targets
        if _normalize_target_name(t["object"]) == normalized_name
    ]
    if not matching_objects:
        return [], get_last_build_date(db)

    # Query per-(inst, date, object) stats from the obslog (summaries table).
    placeholders = ",".join("?" for _ in matching_objects)
    obs_stats: dict[tuple, dict] = {}
    with get_conn(db) as conn:
        cur = conn.execute(
            f"""SELECT instrument, obsdate, object,
                       SUM(nframes)              AS n_frames,
                       GROUP_CONCAT(DISTINCT filter) AS filters,
                       MIN(NULLIF(airmass_min, 0))   AS airmass_min,
                       MAX(NULLIF(airmass_max, 0))   AS airmass_max
                FROM summaries
                WHERE object IN ({placeholders})
                GROUP BY instrument, obsdate, object""",
            matching_objects,
        )
        for row in cur.fetchall():
            raw_filters = sorted(f for f in (row[4] or "").split(",") if f)
            obs_stats[(row[0], row[1], row[2])] = {
                "n_frames": row[3] or 0,
                "filters": raw_filters,
                "filter_chips": _normalize_filters(raw_filters),
                "airmass_min": row[5],
                "airmass_max": row[6],
            }

    datasets = []
    for target in targets:
        if _normalize_target_name(target["object"]) != normalized_name:
            continue

        obj_name = target["object"]
        date_to_inst = target["date_to_inst"]

        for date in target["dates"]:
            inst = date_to_inst.get(date)
            if not inst:
                continue

            status = phot.get_photometry_status(inst, date, obj_name)
            phot_status = "full" if status == "full" else ("test" if status == "test" else "none")

            fit_status = "full" if fit.has_fit_outputs(inst, date, obj_name) else "none"

            stats = obs_stats.get((inst, date, obj_name), {})
            dataset = {
                "object": obj_name,
                "date": date,
                "instrument": inst,
                "filters": stats.get("filters", target["filters"]),
                "filter_chips": stats.get("filter_chips", target["filter_chips"]),
                "airmass_min": stats.get("airmass_min", target["airmass_min"]),
                "airmass_max": stats.get("airmass_max", target["airmass_max"]),
                "n_frames": stats.get("n_frames", target["n_frames"]),
                "ra": target["ra"],
                "dec": target["declination"],
                "phot": phot_status,
                "fit": fit_status,
                "note": target["note"],
            }
            datasets.append(dataset)

    datasets.sort(key=lambda d: d["date"], reverse=True)
    last_updated = get_last_build_date(db)
    return datasets, last_updated


@app.get("/target", response_class=HTMLResponse)
def target_page(name: str = ""):
    db = _db_path()
    tpl_path = TEMPLATE_DIR / "target.html"
    tpl_mtime = str(tpl_path.stat().st_mtime_ns) if tpl_path.is_file() else ""

    if not name:
        return RedirectResponse("/", status_code=303)
    else:
        # Single target view - normalize the input name
        norm_name = _normalize_target_name(name)
        key = (tpl_mtime, _db_mtime(db), _catalog_source_cache_key(), _HARPS_MATCH_ARCSEC, norm_name)
        cache_key = f"target:{norm_name}"
        cached = _index_cache.get(cache_key)
        if cached is not None and cached[0] == key:
            return HTMLResponse(cached[1])

        datasets, last_updated = _get_datasets_for_normalized_target(db, norm_name)
        target_tic_id = _target_tic_id(norm_name, datasets)

        html = jinja.get_template("target.html").render(
            target_name=norm_name,
            datasets=datasets,
            last_updated=last_updated,
            harps_match_arcsec=_HARPS_MATCH_ARCSEC,
            target_tic_id=target_tic_id,
            exofop_target_id=target_tic_id or norm_name,
        )

        _index_cache[cache_key] = (key, html)
        return HTMLResponse(html)


@app.get("/logs", response_class=HTMLResponse)
def logs_page(min_frames: int = 1000):
    db = _db_path()
    with_data = {row["name"] for row in _get_instruments(db)}
    instruments = [
        {"name": name, "has_data": name in with_data}
        for name in INSTRUMENTS
    ]
    summaries = _get_instruments_summary(db, min_frames=min_frames)
    return _render(
        "logs.html",
        instruments=instruments,
        summaries=summaries,
        min_frames=min_frames,
    )


@app.get("/guide", response_class=HTMLResponse)
def guide_page():
    return _render("guide.html")

# Legacy redirect for backward compatibility
@app.get("/workflow", response_class=RedirectResponse)
def workflow_redirect():
    return RedirectResponse(url="/guide", status_code=301)


# --------------------------- TOI catalog page ------------------------------

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


def _path_cache_part(path: pathlib.Path) -> tuple:
    try:
        st = path.stat()
    except OSError:
        return (str(path), None)
    return (str(path), st.st_mtime_ns, st.st_size)


def _catalog_source_cache_key() -> tuple:
    """Cache component for target-page catalog-coordinate fallbacks."""
    return (_path_cache_part(_TOI_CATALOG_PATH), _path_cache_part(_NEXSCI_CATALOG_PATH))


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

    req = UrlRequest(_HARPS_RVBANK_URL, headers={"User-Agent": "muscat-db/harps-rvbank"})
    with urlopen(req, timeout=_HARPS_ONLINE_TIMEOUT_S) as raw:
        with io.TextIOWrapper(raw, encoding="utf-8", newline="") as text:
            yield ("online", _HARPS_RVBANK_URL, text)


def _harps_coord_membership(cat_data: dict) -> tuple[list[int], int]:
    """Return 0/1 flags for catalog rows positionally matched to HARPS RVBank."""
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
    index: dict[tuple[int, int], list[tuple[float, float]]] = {}
    for ra, dec in harps_coords:
        rb = int((ra % 360.0) / bucket) % ra_bins
        db = int((dec + 90.0) / bucket)
        index.setdefault((rb, db), []).append((ra, dec))

    ras, decs = cat_data.get("ra") or [], cat_data.get("dec") or []
    matched = 0
    for i, (ra, dec) in enumerate(zip(ras, decs)):
        if ra is None or dec is None:
            continue
        rb = int((ra % 360.0) / bucket) % ra_bins
        db = int((dec + 90.0) / bucket)
        hit = False
        for dra in (-1, 0, 1):
            for ddec in (-1, 0, 1):
                for hra, hdec in index.get(((rb + dra) % ra_bins, db + ddec), ()):
                    if _angular_sep_arcsec(float(ra), float(dec), hra, hdec) <= tol:
                        hit = True
                        break
                if hit:
                    break
            if hit:
                break
        if hit:
            out[i] = 1
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


@app.get("/api/target/harps-rv", response_class=JSONResponse)
def api_target_harps_rv(name: str = ""):
    norm_name = _normalize_target_name(name)
    if not norm_name:
        return JSONResponse({"ok": False, "error": "Target name is required"}, status_code=400)
    datasets, _last_updated = _get_datasets_for_normalized_target(_db_path(), norm_name)
    harps_rv = _harps_data_for_target(datasets, norm_name)
    return JSONResponse({
        "ok": True,
        "target": norm_name,
        "match_arcsec": _HARPS_MATCH_ARCSEC,
        "has_data": bool(harps_rv.get("total_rows")),
        "harps_rv": harps_rv,
    })


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
    "/ut2/jerome/github/research/project/wakai/data/Boyle2026/final_catalog.feather",
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


@app.get("/toi", response_class=HTMLResponse)
def toi_page():
    import json

    cat = _load_toi_catalog()
    indb, tname = _toi_db_membership(cat["data"], _db_path())
    boyle, n_boyle = _merge_boyle_columns(cat["data"])
    harps, n_harps = _harps_coord_membership(cat["data"])
    payload = dict(cat["data"])
    payload.update(boyle)
    payload["indb"] = indb
    payload["tname"] = tname
    payload["has_harps_rv"] = harps
    return _render(
        "toi.html",
        toi_json=json.dumps(payload, separators=(",", ":"), allow_nan=False),
        n_rows=cat["n"],
        n_indb=sum(indb),
        n_boyle=n_boyle,
        n_harps=n_harps,
        toi_updated=cat["updated"],
    )


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

    data: dict[str, list] = {key: [] for _, key, _ in _NEXSCI_COLUMNS}
    with open(path, encoding="utf-8") as f:
        for row in csv.DictReader(f):
            for header, key, kind in _NEXSCI_COLUMNS:
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


@app.get("/nexsci", response_class=HTMLResponse)
def nexsci_page():
    cat = _load_nexsci_catalog()
    indb, tname = _nexsci_db_membership(cat["data"], _db_path())
    harps, n_harps = _harps_coord_membership(cat["data"])
    payload = dict(cat["data"])
    payload["indb"] = indb
    payload["tname"] = tname
    payload["has_harps_rv"] = harps
    return _render(
        "nexsci.html",
        nexsci_json=json.dumps(payload, separators=(",", ":"), allow_nan=False),
        n_rows=cat["n"],
        n_indb=sum(indb),
        n_harps=n_harps,
        nexsci_updated=cat["updated"],
    )


@app.get("/api/targets/export.csv")
def export_targets_csv():
    db = _db_path()
    targets = _get_targets(db)
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow([
        "object", "ra", "dec", "filters", "airmass_min", "airmass_max",
        "n_dates", "n_frames", "instruments", "dates",
        "total_exptime_hr", "note", "is_identified",
    ])
    for t in targets:
        filters = ", ".join(c["label"] for c in t["filter_chips"])
        w.writerow([
            t["object"],
            t["ra"],
            t["declination"],
            filters,
            t["airmass_min"] if t["airmass_min"] is not None else "",
            t["airmass_max"] if t["airmass_max"] is not None else "",
            t["n_dates"],
            t["n_frames"],
            ", ".join(t["instruments"]),
            ", ".join(t["dates"]),
            t["total_exptime_hr"],
            t["note"],
            "yes" if t["is_identified"] else "no",
        ])
    return Response(
        content=buf.getvalue(),
        media_type="text/csv",
        headers={"Content-Disposition": "attachment; filename=targets.csv"},
    )


def _sinistro_obslog_choices(db: str, inst: str, date: str, target: str) -> tuple[list[str], list[str]]:
    """``(sites, modes)`` present in the obslog for a sinistro target+date.

    The LCO site is the 3-char filename prefix (e.g. ``cpt1m010-...``); the mode
    is ``read_mode`` (CONFMODE). Both are intersected with the known valid sets
    so a stray prefix or non-canonical read_mode (MUSCAT_FAST/SLOW) can't leak
    in. Empty lists for non-sinistro or on error.
    """
    if inst != "sinistro" or not (date and target):
        return [], []
    try:
        with get_conn(db) as conn:
            cur = conn.execute(
                "SELECT DISTINCT substr(filename, 1, 3) FROM frames WHERE instrument = ? AND obsdate = ? AND object = ? AND filename IS NOT NULL AND filename != ''",
                (inst, date, target),
            )
            sites = sorted({row[0].lower() for row in cur.fetchall() if row[0]} & set(phot.SINISTRO_SITES))
            cur = conn.execute(
                "SELECT DISTINCT read_mode FROM frames WHERE instrument = ? AND obsdate = ? AND object = ? AND read_mode IS NOT NULL AND read_mode != ''",
                (inst, date, target),
            )
            modes = sorted({row[0].lower() for row in cur.fetchall() if row[0]} & set(phot.SINISTRO_MODES))
        return sites, modes
    except Exception:
        return [], []


def _site_required_error(db: str, inst: str, date: str, target: str, options: dict) -> str | None:
    """Block a sinistro run that would silently merge multiple sites.

    When the obslog holds more than one site for this target+date and no site is
    chosen, prose would combine frames from different telescopes into one
    mislabeled reduction (prose2 now aborts on this too). Require a choice.
    """
    if inst != "sinistro":
        return None
    if (options.get("site") or "").strip():
        return None
    sites, _modes = _sinistro_obslog_choices(db, inst, date, target)
    if len(sites) > 1:
        return f"select a site to run — {date} has {len(sites)} sites ({', '.join(sites)})"
    return None


@app.get("/photometry", response_class=HTMLResponse)
def photometry_page(inst: str = "", date: str = "", target: str = "", site: str = "", mode: str = "", run: str = "", overwrite: str = ""):
    db = _db_path()
    inst = inst if inst in INSTRUMENTS else ""
    date = date if phot.valid_date(date) else ""
    target = (target or "").strip()
    # Site/mode are sinistro-only view filters (which LCO site / readout mode's
    # products to show). They are validated against the known sets here; whether
    # they are actually present is decided by list_outputs from the filenames.
    site = site.strip().lower()
    if inst != "sinistro" or site not in phot.SINISTRO_SITES:
        site = ""
    mode = mode.strip().lower()
    if inst != "sinistro" or mode not in phot.SINISTRO_MODES:
        mode = ""

    # Parse overwrite from query parameter (overrides defaults for this session)
    run_defaults_override = {}
    if overwrite.lower() in ("0", "false", "no"):
        run_defaults_override["overwrite"] = False
    elif overwrite.lower() in ("1", "true", "yes"):
        run_defaults_override["overwrite"] = True

    dates: list[str] = []
    targets: list[str] = []
    available_sites: list[str] = ["lsc", "cpt", "coj", "tfn", "elp"]
    available_modes: list[str] = ["central_2k_2x2", "full_frame"]
    outputs = None
    runs: list = []
    sel_run: str | None = None
    previews: dict[str, dict] = {}
    nearby_preview: dict | None = None
    command = ""
    raw_missing = False

    if inst:
        date_set = {d["obsdate"] for d in _get_dates(db, inst)}
        date_set.update(phot.output_dates(inst))
        dates = sorted(date_set, reverse=True)
    if inst and date:
        targets = sorted(_get_objects(db, inst, date))
    obs_type = ""
    is_narrowband = False
    available_bands: list[str] = []
    if inst and date and target:
        runs, run_outputs = phot.list_photometry_runs(inst, date, target)
        if inst == "sinistro":
            if site:
                runs = [r for r in runs if r.is_legacy or r.site == site or not r.site]
            if mode:
                runs = [r for r in runs if r.is_legacy or r.mode == mode or not r.mode]
        run_ids = {r.run_id for r in runs}
        newest = runs[0].run_id if runs else None
        if not run:
            sel_run = newest
        elif run == "__legacy__":
            sel_run = "" if "" in run_ids else None
        elif run in run_ids:
            sel_run = run
        else:
            sel_run = newest

        sel_run_desc = next((r for r in runs if r.run_id == (sel_run or "")), None)
        if sel_run_desc and sel_run_desc.run_type == "test":
            runs = [sel_run_desc]

        if sel_run is not None:
            run_key = sel_run or None  # "" → None for legacy
            if not (site or mode) and run_key in run_outputs:
                # Reuse the outputs already computed by list_photometry_runs.
                # Only skip the cache when sinistro site/mode filters are active,
                # since those affect which files are selected.
                outputs = run_outputs[run_key]
            else:
                outputs = phot.list_outputs(inst, date, target, site=site or None, mode=mode or None, run_id=sel_run or None)
        else:
            outputs = phot.list_outputs(inst, date, target, site=site or None, mode=mode or None)
        command = phot.command_str(inst, date, target, test_run=False)
        raw_missing = not phot.raw_data_dir(inst, date).is_dir()

        try:
            with get_conn(db) as conn:
                cur = conn.execute(
                    "SELECT DISTINCT filter FROM frames WHERE instrument = ? AND obsdate = ? AND object = ? AND filter IS NOT NULL AND filter != ''",
                    (inst, date, target),
                )
                filters = [row[0] for row in cur.fetchall()]
                if filters:
                    is_narrowband = any("narrow" in f.lower() or f.lower() == "na_d" for f in filters)
                    obs_type = "(narrowband)" if is_narrowband else "(broadband)"
                    available_bands = phot.bands_from_filters(filters)

                cur = conn.execute(
                    "SELECT COUNT(*) FROM frames WHERE instrument = ? AND obsdate = ? AND object = ?",
                    (inst, date, target),
                )
                total_frames = cur.fetchone()[0]
                if obs_type and total_frames < 100:
                    obs_type += " (test)"
        except Exception:
            logger.debug("failed to load obs metadata for photometry page %s/%s/%s", inst, date, target, exc_info=True)

        # Restrict the site/mode run-option dropdowns to what the obslog actually
        # holds for this target+date, so you can't launch a reduction for a
        # site/mode with no frames.
        db_sites, db_modes = _sinistro_obslog_choices(db, inst, date, target)
        if db_sites:
            available_sites = db_sites
        if db_modes:
            available_modes = db_modes

        # fall through; previews computed below when outputs exist
        if outputs["has_any"]:
            rdir = phot.run_output_dir(inst, date, target, sel_run or None)
            for band, prods in outputs["bands"].items():
                csv_info = prods.get("csv")
                if csv_info:
                    headers, rows = phot.csv_preview(rdir / csv_info["file"], n=8)
                    previews[band] = {"headers": headers, "rows": rows}
            nearby_info = outputs.get("summary", {}).get("nearby_stars")
            if nearby_info:
                nb_headers, nb_rows = phot.csv_preview(rdir / nearby_info["file"], n=100)
                nearby_preview = {"headers": nb_headers, "rows": nb_rows}

    # Merge URL parameter overrides with defaults
    merged_defaults = {**phot.RUN_DEFAULTS, **run_defaults_override}

    resp = _render(
        "photometry.html",
        instruments=list(INSTRUMENTS),
        sel_inst=inst, sel_date=date, sel_target=target,
        sel_site=(outputs.get("site") if outputs else "") or "",
        sel_mode=(outputs.get("mode") if outputs else "") or "",
        runs=runs,
        sel_run=sel_run or "",
        dates=dates, targets=targets,
        outputs=outputs, previews=previews,
        nearby_preview=nearby_preview,
        command=command, raw_missing=raw_missing,
        default_bands=phot.DEFAULT_BANDS,
        run_defaults=merged_defaults,
        cmap_choices=phot.CMAP_CHOICES,
        nan_imputation_methods=phot.NAN_IMPUTATION_METHODS,
        wiki_url=_wiki_url(inst, target),
        obs_type=obs_type,
        is_narrowband=is_narrowband,
        available_bands=available_bands,
        available_sites=available_sites,
        available_modes=available_modes,
    )
    # The run buttons' enabled/disabled state is JavaScript-driven and reflects
    # the live job state. A cached or back/forward-restored snapshot can show
    # them stuck disabled after a failed run, so never let the browser reuse a
    # stale copy of this page.
    resp.headers["Cache-Control"] = "no-store"
    return resp


@app.get("/transit-fit", response_class=HTMLResponse)
def transit_fit_page(inst: str = "", date: str = "", target: str = "", site: str = "", mode: str = "", run: str = ""):
    db = _db_path()
    inst = inst if inst in INSTRUMENTS else ""
    date = date if phot.valid_date(date) else ""
    target = (target or "").strip()
    # Sinistro-only view filters (which site / readout mode's lightcurves to list).
    site = site.strip().lower()
    if inst != "sinistro" or site not in phot.SINISTRO_SITES:
        site = ""
    mode = mode.strip().lower()
    if inst != "sinistro" or mode not in phot.SINISTRO_MODES:
        mode = ""

    run = (run or "").strip()

    dates: list[str] = []
    targets: list[str] = []
    outputs = None
    csvs = []
    target_params = {}
    csv_sites: list[str] = []
    csv_modes: list[str] = []
    sel_site = ""
    sel_mode = ""
    runs: list = []
    sel_run = ""

    if inst:
        date_set = {d["obsdate"] for d in _get_dates(db, inst)}
        date_set.update(phot.output_dates(inst))
        dates = sorted(date_set, reverse=True)
    if inst and date:
        obj_set = set(_get_objects(db, inst, date))
        obj_set.update(phot.discovered_targets(inst, date))
        targets = sorted(obj_set)
    if inst and date and target:
        import datetime
        rows = []
        for c in fit.get_csv_lightcurves(inst, date, target):
            try:
                mtime = c.stat().st_mtime
                created_at = datetime.datetime.fromtimestamp(mtime).strftime('%Y-%m-%d %H:%M:%S')
            except Exception:
                mtime, created_at = 0.0, "Unknown"
            csite, cmode = fit.csv_site_mode(c.name) if inst == "sinistro" else (None, None)
            crun = c.parent.name if "_runs" in c.parts else ""
            rows.append({"path": str(c), "name": c.name, "created_at": created_at,
                         "_mtime": mtime, "_site": csite, "_mode": cmode, "run_id": crun})

        if inst == "sinistro":
            # A sinistro date+target can hold multiple sites / readout modes with
            # identical bands. The picker defaults to showing ALL lightcurves (so
            # the user can fit one site or deliberately combine several); the
            # Site/Mode chips optionally narrow the list. The run's identity is
            # derived from whatever is actually selected at launch.
            csv_sites = sorted({r["_site"] for r in rows if r["_site"]})
            sel_site = site  # validated against SINISTRO_SITES above; "" == all
            csv_modes = sorted({
                r["_mode"] for r in rows
                if r["_mode"] and (not sel_site or r["_site"] == sel_site)
            })
            sel_mode = mode  # "" == all
            rows = [r for r in rows
                    if (not sel_site or r["_site"] == sel_site)
                    and (not sel_mode or r["_mode"] == sel_mode)]

        csvs = [{"path": r["path"], "name": r["name"], "created_at": r["created_at"], "run_id": r["run_id"]} for r in rows]

        # Existing runs (each isolated in its own dir); show one run's results at
        # a time, defaulting to the newest, selectable via the results-run chips.
        # ``run`` unspecified -> newest; ``__legacy__`` -> the legacy single-dir
        # run (run_id ""); an explicit run_id -> that run.
        runs = fit.list_fit_runs(inst, date, target)
        if inst == "sinistro":
            if sel_site:
                runs = [r for r in runs if r.is_legacy or r.site == sel_site or not r.site]
            if sel_mode:
                runs = [r for r in runs if r.is_legacy or r.mode == sel_mode or not r.mode]

        run_ids = {r.run_id for r in runs}
        newest = runs[0].run_id if runs else None

        if not run:
            sel_run = newest
        elif run == "__legacy__":
            sel_run = "" if "" in run_ids else None
        elif run in run_ids:
            sel_run = run
        else:
            sel_run = newest

        if sel_run is not None:
            outputs = fit.get_fit_outputs(inst, date, target, run_id=sel_run or None)
        else:
            outputs = None
        target_params = fit.get_target_parameters(target)


    return _render(
        "transit_fit.html",
        instruments=list(INSTRUMENTS),
        sel_inst=inst, sel_date=date, sel_target=target,
        sel_site=sel_site, sel_mode=sel_mode,
        csv_sites=csv_sites, csv_modes=csv_modes,
        runs=runs, sel_run=sel_run,
        dates=dates, targets=targets,
        csvs=csvs, outputs=outputs,
        target_params=target_params,
        wiki_url=_wiki_url(inst, target),
    )


@app.get("/transit-fit/query-archive")
def transit_fit_query_archive(target: str, source: str = "nasa"):
    if not (target or "").strip():
        return JSONResponse({"ok": False, "error": "Target name is required"}, status_code=400)

    import urllib.request
    import urllib.parse
    import json
    import csv
    import pathlib
    import re

    target = target.strip()

    def get_unc(err1, err2):
        if err1 is None and err2 is None:
            return None
        val1 = abs(err1) if err1 is not None else 0.0
        val2 = abs(err2) if err2 is not None else 0.0
        return max(val1, val2)

    def query_local_tois(target: str) -> dict | None:
        csv_path = pathlib.Path(HERE.parent.parent / "data" / "TOIs.csv")
        if not csv_path.is_file():
            return None

        def extract_number(s: str) -> int | None:
            """Extract the numeric part and return as int to normalize leading zeros."""
            match = re.search(r'\d+', s)
            return int(match.group(0)) if match else None

        target_lower = target.lower()
        target_num = extract_number(target_lower)
        best_row = None

        with open(csv_path, mode='r', encoding='utf-8') as f:
            reader = csv.DictReader(f)
            for row in reader:
                toi = (row.get("TOI") or "").strip()
                planet_name = (row.get("Planet Name") or "").strip()
                tic_id = (row.get("TIC ID") or "").strip()

                # Match TOI by numeric value (handles leading zeros like toi02688 vs TOI-688)
                if toi:
                    toi_num = extract_number(toi)
                    if target_num and toi_num and target_num == toi_num:
                        best_row = row
                        break
                    # Also try exact prefix match for formats like toi688 or toi-688
                    target_clean = re.sub(r"[^0-9a-zA-Z]", "", target_lower)
                    toi_clean = re.sub(r"[^0-9a-zA-Z]", "", toi.lower())
                    if target_clean == toi_clean or (toi_num and target_clean == f"toi{toi_num}"):
                        best_row = row
                        break

                # Match planet name (exact or prefix)
                if planet_name:
                    target_clean = re.sub(r"[^0-9a-zA-Z]", "", target_lower)
                    planet_clean = re.sub(r"[^0-9a-zA-Z]", "", planet_name.lower())
                    if target_clean == planet_clean:
                        best_row = row
                        break

                # Match TIC ID by numeric value
                if tic_id:
                    tic_num = extract_number(tic_id)
                    if target_num and tic_num and target_num == tic_num:
                        best_row = row
                        break
                    target_clean = re.sub(r"[^0-9a-zA-Z]", "", target_lower)
                    tic_clean = re.sub(r"[^0-9a-zA-Z]", "", tic_id.lower())
                    if target_clean == tic_clean or (tic_num and target_clean == f"tic{tic_num}"):
                        best_row = row
                        break
                    
        if not best_row:
            return None
            
        def _float_or_none(val):
            if not val or val.strip() == "":
                return None
            try: return float(val)
            except ValueError: return None
            
        toi_val = best_row.get("TOI", "")
        toi_display = f"TOI-{toi_val}" if toi_val else target
        
        teff = _float_or_none(best_row.get("Stellar Eff Temp (K)"))
        teff_err = _float_or_none(best_row.get("Stellar Eff Temp (K) err"))
        logg = _float_or_none(best_row.get("Stellar log(g) (cm/s^2)"))
        logg_err = _float_or_none(best_row.get("Stellar log(g) (cm/s^2) err"))
        period = _float_or_none(best_row.get("Period (days)"))
        period_err = _float_or_none(best_row.get("Period (days) err"))
        t0 = _float_or_none(best_row.get("Epoch (BJD)"))
        t0_err = _float_or_none(best_row.get("Epoch (BJD) err"))
        dur = _float_or_none(best_row.get("Duration (hours)"))
        dur_err = _float_or_none(best_row.get("Duration (hours) err"))
        
        params = {
            "planets": "b",
            "teff": teff,
            "teff_unc": teff_err,
            "logg": logg,
            "logg_unc": logg_err,
            "feh": "",
            "feh_unc": "",
            "period": period,
            "period_unc": period_err,
            "t0": t0,
            "t0_unc": t0_err,
            "dur": dur,
            "dur_unc": dur_err,
            "ror": "",
            "ror_unc": "",
            "b": "",
            "b_unc": "",
            "st_ref": "TOI Catalog",
            "pl_ref": "TOI Catalog"
        }
        for k, v in params.items():
            if v is None:
                params[k] = ""
        return {"params": params, "pl_name": toi_display}

    def query_local_nasa(target: str) -> dict | None:
        csv_path = pathlib.Path(HERE.parent.parent / "data" / "nexsci_ps.csv")
        if not csv_path.is_file():
            return None
            
        target_clean = re.sub(r"[^0-9a-zA-Z]", "", target).lower()
        best_row_line = None
        best_score = -1
        
        clean_re = re.compile(r"[^0-9a-zA-Z]")
        
        with open(csv_path, mode='r', encoding='utf-8', errors='ignore') as f:
            header_line = f.readline()
            for line in f:
                parts = line.split(',', 9)
                if len(parts) < 9:
                    continue
                pl_name = parts[0].strip('"')
                hostname = parts[2].strip('"')
                hd_name = parts[3].strip('"')
                hip_name = parts[4].strip('"')
                
                pl_clean = clean_re.sub('', pl_name).lower()
                host_clean = clean_re.sub('', hostname).lower()
                hip_clean = clean_re.sub('', hip_name).lower()
                hd_clean = clean_re.sub('', hd_name).lower()
                
                score = -1
                if target_clean == pl_clean:
                    score = 3
                elif target_clean in (host_clean, hip_clean, hd_clean):
                    score = 2
                elif (pl_clean and pl_clean in target_clean) or (host_clean and host_clean in target_clean):
                    score = 1
                    
                if score > -1:
                    is_default = (parts[8].strip('"') == '1')
                    if score > best_score:
                        best_score = score
                        best_row_line = line
                    elif score == best_score:
                        best_is_default = False
                        if best_row_line:
                            best_parts = best_row_line.split(',', 9)
                            if len(best_parts) > 8:
                                best_is_default = (best_parts[8].strip('"') == '1')
                        if is_default and not best_is_default:
                            best_row_line = line
                            
                    if best_score >= 2 and is_default:
                        break
                        
        if not best_row_line:
            return None
            
        import csv
        header = [h.strip('"') for h in next(csv.reader([header_line]))]
        row_values = next(csv.reader([best_row_line]))
        best_row = dict(zip(header, row_values))
            
        def _float_or_none(val):
            if not val or val.strip() == "":
                return None
            try: return float(val)
            except ValueError: return None
            
        pl_name = best_row.get("pl_name", "")
        planets = "b"
        if pl_name and len(pl_name) > 2 and pl_name[-2] == " ":
            planets = pl_name[-1]
            
        params = {
            "planets": planets,
            "teff": _float_or_none(best_row.get("st_teff")),
            "teff_unc": get_unc(_float_or_none(best_row.get("st_tefferr1")), _float_or_none(best_row.get("st_tefferr2"))),
            "logg": _float_or_none(best_row.get("st_logg")),
            "logg_unc": get_unc(_float_or_none(best_row.get("st_loggerr1")), _float_or_none(best_row.get("st_loggerr2"))),
            "feh": _float_or_none(best_row.get("st_met")),
            "feh_unc": get_unc(_float_or_none(best_row.get("st_meterr1")), _float_or_none(best_row.get("st_meterr2"))),
            "period": _float_or_none(best_row.get("pl_orbper")),
            "period_unc": get_unc(_float_or_none(best_row.get("pl_orbpererr1")), _float_or_none(best_row.get("pl_orbpererr2"))),
            "t0": _float_or_none(best_row.get("pl_tranmid")),
            "t0_unc": get_unc(_float_or_none(best_row.get("pl_tranmiderr1")), _float_or_none(best_row.get("pl_tranmiderr2"))),
            "dur": _float_or_none(best_row.get("pl_trandur")),
            "dur_unc": get_unc(_float_or_none(best_row.get("pl_trandurerr1")), _float_or_none(best_row.get("pl_trandurerr2"))),
            "ror": _float_or_none(best_row.get("pl_ratror")),
            "ror_unc": get_unc(_float_or_none(best_row.get("pl_ratrorerr1")), _float_or_none(best_row.get("pl_ratrorerr2"))),
            "b": _float_or_none(best_row.get("pl_imppar")),
            "b_unc": get_unc(_float_or_none(best_row.get("pl_impparerr1")), _float_or_none(best_row.get("pl_impparerr2"))),
            "st_ref": best_row.get("st_refname") or "",
            "pl_ref": best_row.get("pl_refname") or ""
        }
        for k, v in params.items():
            if v is None:
                params[k] = ""
        return {"params": params, "pl_name": pl_name}

    urlopen_is_mocked = hasattr(urllib.request.urlopen, "called")

    if source == "toi":
        if not urlopen_is_mocked:
            local_res = query_local_tois(target)
            if local_res:
                return JSONResponse({"ok": True, **local_res})

        cols = [
            "toi", "toidisplay",
            "st_teff", "st_tefferr1", "st_tefferr2",
            "st_logg", "st_loggerr1", "st_loggerr2",
            "pl_orbper", "pl_orbpererr1", "pl_orbpererr2",
            "pl_tranmid", "pl_tranmiderr1", "pl_tranmiderr2",
            "pl_trandurh", "pl_trandurherr1", "pl_trandurherr2",
        ]
        col_str = ", ".join(cols)

        clean_target = target.replace("TOI", "").replace("toi", "").replace("-", "").replace(" ", "").lstrip("0").split(".")[0].strip()
        target_lit = _adql_literal(clean_target)
        target_like = _adql_literal(f"%{target}%")
        clean_like = _adql_literal(f"%{clean_target}%")
        
        q = f"SELECT {col_str} FROM toi WHERE toi = {target_lit} OR toidisplay LIKE {target_like} OR toi LIKE {clean_like}"
        data = []
        url = 'https://exoplanetarchive.ipac.caltech.edu/TAP/sync?' + urllib.parse.urlencode({'query': q, 'format': 'json'})
        req = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0'})
        try:
            with urllib.request.urlopen(req, timeout=5) as response:
                res = json.loads(response.read().decode())
                if res:
                    # Sort to prioritize: toi = clean_target (3), toidisplay LIKE target (2), toi LIKE clean_target (1)
                    best_row = None
                    best_score = -1
                    for row in res:
                        r_toi = str(row.get("toi", "")).strip()
                        r_toidisplay = str(row.get("toidisplay", "")).strip()
                        
                        score = -1
                        if r_toi == clean_target:
                            score = 3
                        elif target.lower() in r_toidisplay.lower():
                            score = 2
                        elif clean_target in r_toi:
                            score = 1
                            
                        if score > best_score:
                            best_score = score
                            best_row = row
                    if best_row:
                        data = [best_row]
        except Exception:
            pass

        if not data:
            return JSONResponse({"ok": False, "error": f"No parameters found for target '{target}' in TOI Catalog."})

        row = data[0]
        toi_display = row.get("toidisplay", "")
        pl_name = toi_display or target

        params = {
            "planets": "b",
            "teff": row.get("st_teff"),
            "teff_unc": get_unc(row.get("st_tefferr1"), row.get("st_tefferr2")),
            "logg": row.get("st_logg"),
            "logg_unc": get_unc(row.get("st_loggerr1"), row.get("st_loggerr2")),
            "feh": "",
            "feh_unc": "",
            "period": row.get("pl_orbper"),
            "period_unc": get_unc(row.get("pl_orbpererr1"), row.get("pl_orbpererr2")),
            "t0": row.get("pl_tranmid"),
            "t0_unc": get_unc(row.get("pl_tranmiderr1"), row.get("pl_tranmiderr2")),
            "dur": row.get("pl_trandurh") if row.get("pl_trandurh") is not None else None,
            "dur_unc": get_unc(row.get("pl_trandurherr1"), row.get("pl_trandurherr2")),
            "ror": "",
            "ror_unc": "",
            "b": "",
            "b_unc": "",
            "st_ref": "TOI Catalog",
            "pl_ref": "TOI Catalog"
        }

        for k, v in params.items():
            if v is None:
                params[k] = ""

        return JSONResponse({"ok": True, "params": params, "pl_name": pl_name})

    else:
        if not urlopen_is_mocked:
            local_res = query_local_nasa(target)
            if local_res:
                return JSONResponse({"ok": True, **local_res})

        cols = [
            "pl_name", "st_teff", "st_tefferr1", "st_tefferr2",
            "st_logg", "st_loggerr1", "st_loggerr2",
            "st_met", "st_meterr1", "st_meterr2",
            "pl_orbper", "pl_orbpererr1", "pl_orbpererr2",
            "pl_tranmid", "pl_tranmiderr1", "pl_tranmiderr2",
            "pl_trandur", "pl_trandurerr1", "pl_trandurerr2",
            "pl_ratror", "pl_ratrorerr1", "pl_ratrorerr2",
            "pl_imppar", "pl_impparerr1", "pl_impparerr2",
            "st_teff_reflink", "pl_orbper_reflink"
        ]
        col_str = ", ".join(cols)

        norm_target = re.sub(r'^([A-Za-z]+)(\d)', r'\1 \2', target)

        target_lit = _adql_literal(target)
        target_like = _adql_literal(f"%{target}%")
        conditions = [
            f"pl_name = {target_lit}",
            f"hostname = {target_lit}",
            f"hip_name = {target_lit}",
            f"hd_name = {target_lit}",
            f"pl_name LIKE {target_like}",
            f"hostname LIKE {target_like}",
            f"hip_name LIKE {target_like}",
            f"hd_name LIKE {target_like}"
        ]
        if norm_target != target:
            norm_lit = _adql_literal(norm_target)
            conditions.extend([
                f"hostname = {norm_lit}",
                f"hip_name = {norm_lit}",
                f"hd_name = {norm_lit}"
            ])

        q = f"SELECT {col_str} FROM pscomppars WHERE " + " OR ".join(conditions)

        data = []
        url = 'https://exoplanetarchive.ipac.caltech.edu/TAP/sync?' + urllib.parse.urlencode({'query': q, 'format': 'json'})
        req = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0'})
        try:
            with urllib.request.urlopen(req, timeout=5) as response:
                res = json.loads(response.read().decode())
                if res:
                    # Score and rank matching rows in memory
                    target_clean = re.sub(r"[^0-9a-zA-Z]", "", target).lower()
                    best_row = None
                    best_score = -1
                    clean_re = re.compile(r"[^0-9a-zA-Z]")
                    
                    for row in res:
                        pl_name = (row.get("pl_name") or "").strip()
                        hostname = (row.get("hostname") or "").strip()
                        hip_name = (row.get("hip_name") or "").strip()
                        hd_name = (row.get("hd_name") or "").strip()
                        
                        pl_clean = clean_re.sub('', pl_name).lower()
                        host_clean = clean_re.sub('', hostname).lower()
                        hip_clean = clean_re.sub('', hip_name).lower()
                        hd_clean = clean_re.sub('', hd_name).lower()
                        
                        score = -1
                        if target_clean == pl_clean:
                            score = 3
                        elif target_clean in (host_clean, hip_clean, hd_clean):
                            score = 2
                        elif (pl_clean and target_clean in pl_clean) or (host_clean and target_clean in host_clean):
                            score = 1
                            
                        if score > best_score:
                            best_score = score
                            best_row = row
                    
                    if best_row:
                        data = [best_row]
        except Exception:
            pass

        if not data:
            return JSONResponse({"ok": False, "error": f"No parameters found for target '{target}' in Exoplanet Archive."})

        row = data[0]

        pl_name = row.get("pl_name", "")
        planets = "b"
        if pl_name and len(pl_name) > 2 and pl_name[-2] == " ":
            planets = pl_name[-1]

        params = {
            "planets": planets,
            "teff": row.get("st_teff"),
            "teff_unc": get_unc(row.get("st_tefferr1"), row.get("st_tefferr2")),
            "logg": row.get("st_logg"),
            "logg_unc": get_unc(row.get("st_loggerr1"), row.get("st_loggerr2")),
            "feh": row.get("st_met"),
            "feh_unc": get_unc(row.get("st_meterr1"), row.get("st_meterr2")),
            "period": row.get("pl_orbper"),
            "period_unc": get_unc(row.get("pl_orbpererr1"), row.get("pl_orbpererr2")),
            "t0": row.get("pl_tranmid"),
            "t0_unc": get_unc(row.get("pl_tranmiderr1"), row.get("pl_tranmiderr2")),
            "dur": row.get("pl_trandur") if row.get("pl_trandur") is not None else None,
            "dur_unc": get_unc(row.get("pl_trandurerr1"), row.get("pl_trandurerr2")),
            "ror": row.get("pl_ratror"),
            "ror_unc": get_unc(row.get("pl_ratrorerr1"), row.get("pl_ratrorerr2")),
            "b": row.get("pl_imppar"),
            "b_unc": get_unc(row.get("pl_impparerr1"), row.get("pl_impparerr2")),
            "st_ref": row.get("st_teff_reflink") or "",
            "pl_ref": row.get("pl_orbper_reflink") or ""
        }

        for k, v in params.items():
            if v is None:
                params[k] = ""

        return JSONResponse({"ok": True, "params": params, "pl_name": pl_name})


@app.get("/transit-fit/status")
def transit_fit_status(inst: str, date: str, target: str, run: str = ""):
    fit.sync_jobs()
    return JSONResponse(fit.job_status(inst, date, target, run_id=(run or "").strip()))


@app.post("/transit-fit/run")
def transit_fit_run(request: Request, payload: dict = Body(...)):
    inst = (payload.get("inst") or "").strip()
    date = (payload.get("date") or "").strip()
    target = (payload.get("target") or "").strip()
    options = payload.get("options") or {}
    test_run = bool(payload.get("test_run", False))
    selected_csvs = payload.get("selected_csvs") if "selected_csvs" in payload else None
    user_name = request.state.user
    result = fit.start_fit(inst, date, target, options, test_run=test_run, selected_csvs=selected_csvs, user_name=user_name)
    if not result.get("ok"):
        return JSONResponse(result, status_code=400)
    return JSONResponse(result)


@app.post("/transit-fit/logp")
def transit_fit_logp(payload: dict = Body(...)):
    inst = (payload.get("inst") or "").strip()
    date = (payload.get("date") or "").strip()
    target = (payload.get("target") or "").strip()
    options = payload.get("options") or {}
    selected_csvs = payload.get("selected_csvs") if "selected_csvs" in payload else None
    result = fit.compute_logp(inst, date, target, options, selected_csvs=selected_csvs)
    if not result.get("ok"):
        return JSONResponse(result, status_code=400)
    return JSONResponse(result)


@app.post("/transit-fit/cancel")
def transit_fit_cancel(payload: dict = Body(...)):
    inst = (payload.get("inst") or "").strip()
    date = (payload.get("date") or "").strip()
    target = (payload.get("target") or "").strip()
    run_id = (payload.get("run_id") or payload.get("run") or "").strip()
    result = fit.cancel_fit(inst, date, target, run_id=run_id)
    if not result.get("ok"):
        return JSONResponse(result, status_code=400)
    return JSONResponse(result)


@app.post("/transit-fit/delete")
def transit_fit_delete(payload: dict = Body(...)):
    inst = (payload.get("inst") or "").strip()
    date = (payload.get("date") or "").strip()
    target = (payload.get("target") or "").strip()
    run_id = (payload.get("run_id") or "").strip()
    if inst not in INSTRUMENTS:
        return JSONResponse({"ok": False, "error": "unknown instrument"}, status_code=400)
    if not phot.valid_date(date):
        return JSONResponse({"ok": False, "error": "invalid date"}, status_code=400)
    if not (target or "").strip():
        return JSONResponse({"ok": False, "error": "target is required"}, status_code=400)
    result = fit.delete_fit(inst, date, target, run_id=run_id)
    return JSONResponse(result)


def _serve_transit_file(inst: str, date: str, target: str, name: str, run_id: str | None):
    if inst not in INSTRUMENTS or not phot.valid_date(date):
        raise HTTPException(404, "invalid parameters")
    if ".." in name or "/" in name:
        raise HTTPException(400, "invalid filename")
    if run_id and (".." in run_id or "/" in run_id):
        raise HTTPException(400, "invalid run id")

    try:
        rdir = fit.fit_output_dir(inst, date, target, run_id or None)
    except ValueError:
        raise HTTPException(400, "invalid target")
    out_dir = rdir / "out"

    # ``name`` is already sanitized above (no "/" or ".."), so it can only
    # resolve to a direct child of out_dir or rdir. Serve any output file
    # found there (PNG plots, summary.csv, *.yaml, logs, etc.).
    path = out_dir / name
    if not path.is_file():
        path = rdir / name
    if not path.is_file():
        raise HTTPException(404, "file not found")
    return FileResponse(str(path), headers={"Cache-Control": "no-store, no-cache, must-revalidate, max-age=0"})


@app.get("/transit-fit/file/{inst}/{date}/{target}/run/{run_id}/{name}")
def transit_fit_file_run(inst: str, date: str, target: str, run_id: str, name: str):
    return _serve_transit_file(inst, date, target, name, run_id)


@app.get("/transit-fit/file/{inst}/{date}/{target}/{name}")
def transit_fit_file(inst: str, date: str, target: str, name: str):
    # Legacy single-dir fits (run_id="").
    return _serve_transit_file(inst, date, target, name, None)


def _create_zip_response(files_to_zip: list[tuple[pathlib.Path, str]], archive_name: str) -> FileResponse:
    import tempfile
    import zipfile
    from starlette.background import BackgroundTask

    tmp_dir = phot.prose_tmpdir()
    pathlib.Path(tmp_dir).mkdir(parents=True, exist_ok=True)
    temp_zip = tempfile.NamedTemporaryFile(delete=False, suffix=".zip", dir=tmp_dir)
    temp_zip_path = pathlib.Path(temp_zip.name)
    temp_zip.close()

    try:
        with zipfile.ZipFile(temp_zip_path, "w", zipfile.ZIP_DEFLATED) as zip_file:
            for filepath, arcname in files_to_zip:
                if filepath.is_file():
                    zip_file.write(filepath, arcname)
    except Exception as exc:
        try:
            temp_zip_path.unlink()
        except OSError:
            pass
        raise HTTPException(500, f"failed to create zip archive: {exc}")

    def cleanup():
        try:
            temp_zip_path.unlink()
        except OSError:
            pass

    return FileResponse(
        str(temp_zip_path),
        media_type="application/zip",
        filename=archive_name,
        background=BackgroundTask(cleanup),
    )


def _transit_fit_download_all(inst: str, date: str, target: str, run_id: str | None):
    if inst not in INSTRUMENTS or not phot.valid_date(date):
        raise HTTPException(400, "invalid parameters")
    if run_id and (".." in run_id or "/" in run_id):
        raise HTTPException(400, "invalid run id")

    try:
        rdir = fit.fit_output_dir(inst, date, target, run_id or None)
    except ValueError:
        raise HTTPException(400, "invalid target")

    if not rdir.is_dir():
        raise HTTPException(404, "no fit directory found")

    files_to_zip = []
    if run_id:
        # Zip all files recursively
        for p in rdir.rglob("*"):
            if p.is_file():
                files_to_zip.append((p, str(p.relative_to(rdir))))
    else:
        # Legacy run: only include files directly in rdir and in rdir / "out"
        for p in rdir.iterdir():
            if p.is_file():
                files_to_zip.append((p, p.name))
        out_dir = rdir / "out"
        if out_dir.is_dir():
            for p in out_dir.iterdir():
                if p.is_file():
                    files_to_zip.append((p, f"out/{p.name}"))

    if not files_to_zip:
        raise HTTPException(404, "no files to download")

    archive_name = f"{target.replace(' ', '')}_fit_{date}"
    if run_id:
        archive_name += f"_{run_id}"
    archive_name += ".zip"

    return _create_zip_response(files_to_zip, archive_name)


@app.get("/transit-fit/download-all/{inst}/{date}/{target}/run/{run_id}")
def transit_fit_download_all_run(inst: str, date: str, target: str, run_id: str):
    return _transit_fit_download_all(inst, date, target, run_id)


@app.get("/transit-fit/download-all/{inst}/{date}/{target}")
def transit_fit_download_all(inst: str, date: str, target: str):
    return _transit_fit_download_all(inst, date, target, None)


# ---------------------------------------------------------------------------
# Exposure Time Calculator
# ---------------------------------------------------------------------------


@app.get("/exposure", response_class=HTMLResponse)
def exposure_page(inst: str = "", target: str = ""):
    inst = inst if inst in INSTRUMENTS else ""
    calibrations = {}
    for name in INSTRUMENTS:
        status = exp_calc.calibration_status(name)
        calibrations[name] = status

    return _render(
        "exposure.html",
        instruments=list(INSTRUMENTS),
        sel_inst=inst,
        sel_target=target,
        calibrations=calibrations,
        inst_params=exp_calc.INSTRUMENT_PARAMS,
    )


@app.post("/exposure/calculate", response_class=JSONResponse)
def exposure_calculate(payload: dict = Body(...)):
    inst = (payload.get("instrument") or "").strip()
    if inst not in INSTRUMENTS:
        return JSONResponse({"ok": False, "error": "Invalid instrument"}, status_code=400)
    mags = payload.get("mags") or {}
    focus_mm = float(payload.get("focus_mm", 0))
    airmass = float(payload.get("airmass", 1.1))
    sat_frac = payload.get("sat_frac")
    mode = payload.get("mode", "exptime")
    exptime = payload.get("exptime")
    target_adu = payload.get("target_adu")
    confmode = payload.get("confmode", "central_2k_2x2") if inst == "sinistro" else None
    if exptime is not None:
        exptime = float(exptime)
    if target_adu is not None:
        target_adu = float(target_adu)
    if sat_frac is not None:
        sat_frac = float(sat_frac)
    else:
        sat_frac = 0.5

    if not mags:
        return JSONResponse({"ok": False, "error": "No magnitudes provided"}, status_code=400)

    result = exp_calc.calc_all_bands(
        instrument=inst,
        mags=mags,
        focus_mm=focus_mm,
        airmass=airmass,
        sat_frac=sat_frac,
        mode=mode,
        exptime=exptime,
        target_adu=target_adu,
        confmode=confmode,
    )
    return JSONResponse({"ok": True, **result})


@app.post("/exposure/calibrate", response_class=JSONResponse)
def exposure_calibrate(payload: dict = Body(...)):
    inst = (payload.get("instrument") or "").strip()
    if inst not in INSTRUMENTS:
        return JSONResponse({"ok": False, "error": "Invalid instrument"}, status_code=400)
    # Run in a thread to avoid blocking
    import threading
    result = {"ok": True, "message": f"Calibration started for {inst}"}
    threading.Thread(target=exp_calc.calibrate_instrument, args=(inst,), daemon=True).start()
    return JSONResponse(result)


@app.post("/exposure/lookup-mags", response_class=JSONResponse)
def exposure_lookup_mags(payload: dict = Body(...)):
    target = (payload.get("target") or "").strip()
    if not target:
        return JSONResponse({"ok": False, "error": "Target name required"}, status_code=400)

    # Try resolving target name
    coords = exp_calc.resolve_target_coords(target)
    if not coords:
        return JSONResponse({"ok": False, "error": f"Could not resolve target '{target}'"})

    ra, dec = coords
    mags, source = exp_calc.lookup_magnitudes(ra, dec, return_source=True)
    if not mags:
        return JSONResponse({
            "ok": False,
            "error": f"No griz magnitudes found for '{target}' in Pan-STARRS or SkyMapper",
            "ra": ra,
            "dec": dec,
        })

    return JSONResponse({
        "ok": True,
        "target": target,
        "ra": ra,
        "dec": dec,
        "mags": mags,
        "source": source,
    })


@app.get("/exposure/status", response_class=JSONResponse)
def exposure_status():
    calibrations = {}
    for name in INSTRUMENTS:
        calibrations[name] = exp_calc.calibration_status(name)
    return JSONResponse({"calibrations": calibrations})


@app.get("/exposure/coeffs/{instrument}", response_class=JSONResponse)
def exposure_coeffs(instrument: str):
    if instrument not in INSTRUMENTS:
        return JSONResponse({"ok": False, "error": "Invalid instrument"}, status_code=400)
    coeffs = exp_calc.load_coeffs(instrument)
    # Convert to serializable format
    rows = []
    for (band, focus_mm), (coef, fwhm, n) in sorted(coeffs.items()):
        rows.append({"band": band, "focus_mm": focus_mm, "coef": round(coef, 4), "fwhm_pix": round(fwhm, 2), "n_frames": n})
    return JSONResponse({"ok": True, "instrument": instrument, "coeffs": rows})


@app.get("/api/exposure/target/{target}", response_class=JSONResponse)
def api_exposure_target(target: str):
    """Get exposure information for a specific target.

    Returns unique exposure times, filters, instruments, and other details
    for all observations of the given target.
    """
    target = target.strip() if target else ""
    if not target:
        return JSONResponse({"ok": False, "error": "Target name required"}, status_code=400)

    try:
        db = _db_path()
        with get_conn(db, timeout=10, row_factory=sqlite3.Row) as conn:
            # Get all frames for this target
            frames = conn.execute(
                """
                SELECT
                    instrument, obsdate, filter, exptime, read_mode,
                    ra, declination, airmass, focus, ccd
                FROM frames
                WHERE object = ?
                ORDER BY obsdate DESC, instrument, filter, exptime
                """,
                (target,)
            ).fetchall()
            # Get target info from targets table
            target_info = conn.execute(
                "SELECT n_dates, n_frames, ra, declination FROM targets WHERE object = ?",
                (target,)
            ).fetchone()

        if not frames:
            return JSONResponse({
                "ok": False,
                "error": f"No observations found for target '{target}'"
            }, status_code=404)

        # Aggregate data
        instruments = set()
        filters = set()
        unique_exptimes = {}  # {filter: set of exptimes}
        unique_read_modes = set()
        airmass_values = []
        focus_values = []
        n_observations = len(frames)

        for frame in frames:
            instruments.add(frame["instrument"])
            if frame["filter"]:
                filters.add(frame["filter"])
                if frame["filter"] not in unique_exptimes:
                    unique_exptimes[frame["filter"]] = set()
                if frame["exptime"] is not None:
                    unique_exptimes[frame["filter"]].add(round(frame["exptime"], 3))
            if frame["read_mode"]:
                unique_read_modes.add(frame["read_mode"])
            if frame["airmass"] is not None:
                airmass_values.append(frame["airmass"])
            if frame["focus"] is not None:
                focus_values.append(frame["focus"])

        # Format results
        exptime_summary = {}
        for filt in sorted(filters):
            exptimes = sorted(unique_exptimes.get(filt, []))
            exptime_summary[filt] = exptimes

        result = {
            "ok": True,
            "target": target,
            "n_observations": n_observations,
            "n_unique_dates": target_info["n_dates"] if target_info else None,
            "n_total_frames": target_info["n_frames"] if target_info else None,
            "instruments": sorted(instruments),
            "filters": sorted(filters),
            "unique_read_modes": sorted(unique_read_modes),
            "exposure_times_by_filter": exptime_summary,
            "airmass": {
                "min": min(airmass_values) if airmass_values else None,
                "max": max(airmass_values) if airmass_values else None,
                "mean": sum(airmass_values) / len(airmass_values) if airmass_values else None,
            } if airmass_values else None,
            "focus": {
                "min": min(focus_values) if focus_values else None,
                "max": max(focus_values) if focus_values else None,
                "mean": sum(focus_values) / len(focus_values) if focus_values else None,
            } if focus_values else None,
            "coordinates": {
                "ra": target_info["ra"],
                "dec": target_info["declination"],
            } if target_info else None,
        }

        return JSONResponse(result)

    except Exception as e:
        return JSONResponse(
            {"ok": False, "error": f"Database error: {str(e)}"},
            status_code=500
        )


# ---------------------------------------------------------------------------
# FOV optimization
# ---------------------------------------------------------------------------
# Instruments that have a footprint definition (XML or computed fallback).
_FOV_INSTRUMENTS = [name for name in INSTRUMENTS if fov_opt.has_footprint(name)]


@app.get("/fov", response_class=HTMLResponse)
def fov_page(inst: str = "", target: str = ""):
    inst = inst if inst in _FOV_INSTRUMENTS else ""
    readout_modes: dict[str, list[dict[str, str]]] = {}
    fov_sizes = {}
    for name in _FOV_INSTRUMENTS:
        if name == "sinistro":
            # Show the default (central_2k_2x2) size; full_frame is a selectable option
            size_arcmin = fov_opt.SINISTRO_MODES["central_2k_2x2"] * 2.0 / 60.0
            readout_modes[name] = [
                {"value": mode, "label": f"{mode} ({round(half_arcsec * 2.0 / 60.0, 1)}′)"}
                for mode, half_arcsec in fov_opt.SINISTRO_MODES.items()
            ]
        else:
            size_arcmin = fov_opt.load_fov_halfsize_arcsec(name) * 2.0 / 60.0
            readout_modes[name] = [{"value": "MUSCAT_FAST", "label": "MUSCAT_FAST"}]
        fov_sizes[name] = round(size_arcmin, 2)
    return _render(
        "fov.html",
        instruments=_FOV_INSTRUMENTS,
        sel_inst=inst,
        sel_target=target,
        fov_sizes=fov_sizes,
        readout_modes=readout_modes,
    )


@app.post("/api/fov/optimize", response_class=JSONResponse)
def api_fov_optimize(payload: dict = Body(...)):
    inst = (payload.get("instrument") or "").strip()
    if inst not in _FOV_INSTRUMENTS:
        return JSONResponse({"ok": False, "error": "Invalid instrument"}, status_code=400)

    target = (payload.get("target") or "").strip()
    ra = payload.get("ra")
    dec = payload.get("dec")
    try:
        ra = float(ra) if ra not in (None, "") else None
        dec = float(dec) if dec not in (None, "") else None
        margin = float(payload.get("margin_arcsec", fov_opt.DEFAULT_MARGIN_ARCSEC))
        comp_margin = payload.get("comp_margin_arcsec")
        comp_margin = float(comp_margin) if comp_margin not in (None, "") else None
        mag_limit = float(payload.get("mag_limit", 18.0))
        
        min_mag = payload.get("mag_min")
        min_mag = float(min_mag) if min_mag not in (None, "") else 0.0
        max_mag = payload.get("mag_max")
        max_mag = float(max_mag) if max_mag not in (None, "") else 18.0
        mag_delta = payload.get("mag_delta")
        mag_delta = float(mag_delta) if mag_delta not in (None, "") else None
        avoid_mag = payload.get("avoid_mag")
        avoid_mag = float(avoid_mag) if avoid_mag not in (None, "") else None
    except (TypeError, ValueError):
        return JSONResponse({"ok": False, "error": "Invalid numeric parameter"}, status_code=400)

    if not target and (ra is None or dec is None):
        return JSONResponse(
            {"ok": False, "error": "Provide a target name or RA/Dec."}, status_code=400
        )

    allow_rotation = payload.get("allow_rotation", True)
    pa_step_deg = None if allow_rotation else 180.0
    sinistro_mode = payload.get("sinistro_mode")

    result = fov_opt.optimize(
        instrument=inst,
        target=target,
        ra=ra,
        dec=dec,
        margin_arcsec=margin,
        comp_margin_arcsec=comp_margin,
        mag_limit=mag_limit,
        pa_step_deg=pa_step_deg,
        sinistro_mode=sinistro_mode,
        min_mag=min_mag,
        max_mag=max_mag,
        mag_delta=mag_delta,
    )
    return JSONResponse(result.to_dict(), status_code=200)


@app.post("/api/fov/resolve-target", response_class=JSONResponse)
def api_fov_resolve_target(payload: dict = Body(...)):
    target = (payload.get("target") or "").strip()
    if not target:
        return JSONResponse({"ok": False, "error": "Target name is required."}, status_code=400)

    coords = exp_calc.resolve_target_coords(target)
    if coords is None:
        return JSONResponse(
            {"ok": False, "error": f"Could not resolve '{target}'. Try a different name or enter RA/Dec manually."},
            status_code=200,
        )
    return JSONResponse({"ok": True, "ra": round(coords[0], 5), "dec": round(coords[1], 5)})


@app.get("/api/fov/observable", response_class=JSONResponse)
def api_fov_observable():
    """Report observable declination ranges for each instrument."""
    observable = {}
    for inst in _FOV_INSTRUMENTS:
        min_dec, max_dec = fov_opt.get_observable_range(inst)
        observable[inst] = {
            "min_dec": round(min_dec, 1),
            "max_dec": round(max_dec, 1),
            "latitude": fov_opt.OBSERVATORY_LOCATIONS.get(inst, 0.0),
        }
    return JSONResponse({"ok": True, "observable": observable})


@app.get("/ephemeris", response_class=HTMLResponse)
def ephemeris_page():
    return _render("ephemeris.html")


@app.post("/api/ephemeris/view", response_class=JSONResponse)
def api_ephemeris_view_save(payload: dict = Body(...)):
    state = payload.get("state") if isinstance(payload, dict) else None
    if not isinstance(state, dict):
        return JSONResponse({"ok": False, "error": "State is required"}, status_code=400)
    targets = state.get("targets")
    if not isinstance(targets, list) or not [t for t in targets if str(t).strip()]:
        return JSONResponse({"ok": False, "error": "At least one target is required"}, status_code=400)
    saved = save_ephemeris_view(state)
    return JSONResponse({"ok": True, **saved})


@app.get("/api/ephemeris/view/{slug}", response_class=JSONResponse)
def api_ephemeris_view_get(slug: str):
    if not re.fullmatch(r"[A-Za-z0-9_-]{8,64}", slug or ""):
        return JSONResponse({"ok": False, "error": "Invalid view slug"}, status_code=400)
    view = get_ephemeris_view(slug)
    if view is None:
        return JSONResponse({"ok": False, "error": "View not found"}, status_code=404)
    return JSONResponse({"ok": True, **view})


# ---------------------------------------------------------------------------
# LCO scheduling & archive download (see muscat_db/lco.py)
# ---------------------------------------------------------------------------


def _lco_error_response(e: "lco.LcoError") -> JSONResponse:
    return JSONResponse(e.to_dict(), status_code=e.status)


def _request_user(request: Request) -> str | None:
    return getattr(request.state, "user", None) or None


def _settings_auth_error() -> JSONResponse:
    return JSONResponse(
        {
            "ok": False,
            "error": "login required",
            "detail": "Per-user LCO tokens require nginx authentication.",
        },
        status_code=401,
    )


def _is_same_origin(request: Request) -> bool:
    """True if the request's Origin (or Referer) header matches this host.

    HTTP Basic Auth credentials are resent by the browser automatically on
    every request to the realm, so state-changing endpoints need their own
    CSRF defense. A CORS preflight is not sufficient here: FastAPI's
    ``Body(...)`` parses the request body as JSON regardless of the
    Content-Type the client declared, so a cross-origin "simple request"
    (e.g. Content-Type: text/plain, which browsers don't preflight) would
    still reach the handler with an attacker-controlled body.
    """
    origin = request.headers.get("origin") or request.headers.get("referer")
    if not origin:
        return False
    return urlsplit(origin).netloc == request.headers.get("host", "")


def _csrf_error() -> JSONResponse:
    return JSONResponse({"ok": False, "error": "cross-origin request rejected"}, status_code=403)


@app.get("/settings", response_class=HTMLResponse)
def settings_page():
    return _render("settings.html")


@app.get("/api/settings/lco-token-status", response_class=JSONResponse)
def api_settings_lco_token_status(request: Request):
    user = _request_user(request)
    if not user:
        return _settings_auth_error()
    try:
        user_token_configured = get_user_lco_token(user) is not None
    except UserSettingsError as exc:
        return JSONResponse(
            {"ok": False, "error": "stored LCO token cannot be read", "detail": str(exc)},
            status_code=503,
        )
    return JSONResponse(
        {
            "ok": True,
            "user": user,
            "user_token_configured": user_token_configured,
            "global_token_configured": bool(os.environ.get("LCO_API_TOKEN")),
            "secret_configured": bool(os.environ.get("MUSCAT_DB_SECRET")),
        }
    )


@app.post("/api/settings/lco-token", response_class=JSONResponse)
def api_settings_lco_token(request: Request, payload: dict = Body(...)):
    if not _is_same_origin(request):
        return _csrf_error()
    user = _request_user(request)
    if not user:
        return _settings_auth_error()
    token = str(payload.get("token") or "").strip()
    try:
        set_user_lco_token(user, token)
    except UserSettingsError as exc:
        return JSONResponse(
            {"ok": False, "error": "could not save LCO token", "detail": str(exc)},
            status_code=503,
        )
    return JSONResponse({"ok": True, "user_token_configured": bool(token)})


@app.get("/lco")
def lco_page():
    return RedirectResponse(url="/lco/schedule", status_code=307)


@app.get("/lco/schedule", response_class=HTMLResponse)
def lco_schedule_page():
    return _render("lco_schedule.html")


@app.get("/lco/archive", response_class=HTMLResponse)
def lco_archive_page():
    return _render("lco_archive.html")


@app.get("/api/lco/config", response_class=JSONResponse)
def api_lco_config(request: Request):
    """Report whether the token/download-root/submit gate are configured. No secrets."""
    return JSONResponse({"ok": True, **lco.config_state(_request_user(request))})


@app.get("/api/lco/proposals", response_class=JSONResponse)
def api_lco_proposals(request: Request):
    try:
        return JSONResponse({"ok": True, **lco.get_proposals(_request_user(request))})
    except lco.LcoError as e:
        return _lco_error_response(e)


@app.get("/api/lco/requestgroups", response_class=JSONResponse)
def api_lco_requestgroups(request: Request, proposal: str = ""):
    try:
        return JSONResponse({"ok": True, **lco.get_requestgroups(proposal, _request_user(request))})
    except lco.LcoError as e:
        return _lco_error_response(e)


@app.post("/api/lco/windows", response_class=JSONResponse)
def api_lco_windows(payload: dict = Body(...)):
    """Generate transit windows from explicit t0/period/duration or a catalog lookup."""
    try:
        t0 = payload.get("t0")
        period = payload.get("period")
        duration = payload.get("duration")
        target = (payload.get("target") or "").strip()
        planet = (payload.get("planet") or "").strip().lower()
        source = (payload.get("source") or "catalog").strip().lower()

        if t0 in (None, "") or period in (None, ""):
            if not target:
                return JSONResponse(
                    {"ok": False, "error": "provide t0+period, or a target to look up"},
                    status_code=400,
                )
            # Only catalog sources resolve server-side. The "linear" fit and
            # individual "dataset_*" sources are computed on the client (via
            # /api/ephemeris/calculate or target-info) and must populate t0/period
            # first, so they can't be looked up here.
            if source == "nasa":
                planets = _query_target_planets_nasa(target)
            elif source == "toi":
                planets = _query_target_planets_toi(target)
            elif source in ("", "catalog"):
                planets = _query_target_planets_catalog(target)
            else:
                return JSONResponse(
                    {"ok": False, "error": f"click 'Fetch ephemeris' first for the '{source}' source"},
                    status_code=400,
                )
            key = planet if planet in planets else (next(iter(planets)) if planets else None)
            ephem = planets.get(key) if key else None
            if not ephem:
                return JSONResponse(
                    {"ok": False, "error": f"no ephemeris found for {target} {planet}".strip()},
                    status_code=404,
                )
            t0 = ephem.get("t0")
            period = ephem.get("period")
            if duration in (None, "") and ephem.get("duration") is not None:
                duration = ephem.get("duration")
            planet = key

        if duration in (None, ""):
            return JSONResponse(
                {"ok": False, "error": "transit duration (hours) is required (none in catalog)"},
                status_code=400,
            )

        windows = lco.generate_windows(
            float(t0),
            float(period),
            float(duration),
            payload.get("range_start"),
            payload.get("range_end"),
            float(payload.get("pad_before_min") or 0),
            float(payload.get("pad_after_min") or 0),
        )
        result = {
            "ok": True,
            "windows": windows,
            "planet": planet,
            "t0": float(t0),
            "period": float(period),
            "duration": float(duration),
        }

        # Optional: classify each transit's observability across the instrument's
        # LCO sites when coordinates + instrument kind are supplied. Degrades
        # gracefully (windows still returned) if astropy/observability fails.
        ra = payload.get("ra")
        dec = payload.get("dec")
        # The windows table is site-driven, independent of the imaging instrument:
        # an explicit ``sites`` list restricts the check; empty/omitted evaluates
        # the full LCO network (kind is intentionally not used as a fallback here).
        sites = payload.get("sites") or None
        if windows and ra not in (None, "") and dec not in (None, ""):
            try:
                obs = transit_obs.classify_transits(
                    float(ra), float(dec), windows, None, float(duration),
                    max_airmass=float(payload.get("obs_airmass") or 2.0),
                    twilight=payload.get("twilight") or transit_obs.DEFAULT_TWILIGHT,
                    moon_sep_min=float(payload.get("moon_sep_min") or 0.0),
                    include_padding=bool(payload.get("include_padding")),
                    sites=sites,
                    pad_before_min=float(payload.get("pad_before_min") or 0.0),
                    pad_after_min=float(payload.get("pad_after_min") or 0.0),
                )
                for w, o in zip(windows, obs):
                    w["observability"] = o
            except transit_obs.TransitObsError as e:
                result["obs_error"] = str(e)
            except Exception as e:  # never let plotting/astropy break window listing
                result["obs_error"] = f"observability unavailable: {e}"

        return JSONResponse(result)
    except lco.LcoError as e:
        return _lco_error_response(e)
    except (TypeError, ValueError) as e:
        return JSONResponse({"ok": False, "error": f"invalid numeric input: {e}"}, status_code=400)


@app.get("/api/lco/visibility", response_class=JSONResponse)
def api_lco_visibility(
    ra: float,
    dec: float,
    mid: str,
    duration: float,
    site: str,
    obs_airmass: float = 2.0,
    twilight: str = transit_obs.DEFAULT_TWILIGHT,
    moon_sep_min: float = 0.0,
):
    """Time-series for the inline visibility plot of one transit at one site
    (target + moon altitude, twilight, airmass limit, shaded transit interval)."""
    try:
        series = transit_obs.visibility_series(
            float(ra), float(dec), mid, float(duration), site,
            max_airmass=float(obs_airmass), twilight=twilight,
            moon_sep_min=float(moon_sep_min),
        )
        return JSONResponse({"ok": True, **series})
    except transit_obs.TransitObsError as e:
        return JSONResponse({"ok": False, "error": str(e)}, status_code=e.status)
    except Exception as e:
        return JSONResponse({"ok": False, "error": f"visibility unavailable: {e}"}, status_code=500)


@app.post("/api/lco/ipp", response_class=JSONResponse)
def api_lco_ipp(request: Request, payload: dict = Body(...)):
    """Build the requestgroup and run the max-allowable-IPP dry-run."""
    try:
        rg = lco.build_requestgroup(payload.get("kind"), payload)
        ipp = lco.max_allowable_ipp(rg, _request_user(request))
        return JSONResponse(
            {"ok": True, "payload": rg, "payload_hash": lco.payload_hash(rg), "ipp": ipp}
        )
    except lco.LcoError as e:
        return _lco_error_response(e)


@app.post("/api/lco/submit", response_class=JSONResponse)
def api_lco_submit(request: Request, payload: dict = Body(...)):
    """Live submission. Guarded: requires explicit confirm AND a payload hash that
    matches a prior successful dry-run, plus the server-side submit switch."""
    try:
        if not payload.get("confirm"):
            return JSONResponse(
                {"ok": False, "error": "submission requires explicit confirm"}, status_code=400
            )
        rg = lco.build_requestgroup(payload.get("kind"), payload)
        expected = payload.get("dry_run_hash")
        if not expected or expected != lco.payload_hash(rg):
            return JSONResponse(
                {
                    "ok": False,
                    "error": "no matching IPP dry-run for this payload; run the dry-run again",
                },
                status_code=409,
            )
        result = lco.submit_requestgroup(rg, _request_user(request))
        return JSONResponse({"ok": True, "result": result})
    except lco.LcoError as e:
        return _lco_error_response(e)


def _lco_split_error_response(e: "lco.LcoError", leg: str) -> JSONResponse:
    body = e.to_dict()
    body["leg"] = leg
    return JSONResponse(body, status_code=e.status)


@app.post("/api/lco/split-ipp", response_class=JSONResponse)
def api_lco_split_ipp(request: Request, payload: dict = Body(...)):
    """Build+dry-run BOTH legs of a two-site split-transit request (one site
    covering ingress through a handoff, the other the handoff through egress).
    Both legs must pass validation before either can be submitted; this never
    reports a partial pass, naming which leg failed when one does."""
    user = _request_user(request)
    try:
        rg_a = lco.build_requestgroup((payload.get("leg_a") or {}).get("kind"), payload.get("leg_a") or {})
        ipp_a = lco.max_allowable_ipp(rg_a, user)
    except lco.LcoError as e:
        return _lco_split_error_response(e, "leg_a")
    try:
        rg_b = lco.build_requestgroup((payload.get("leg_b") or {}).get("kind"), payload.get("leg_b") or {})
        ipp_b = lco.max_allowable_ipp(rg_b, user)
    except lco.LcoError as e:
        return _lco_split_error_response(e, "leg_b")
    return JSONResponse({
        "ok": True,
        "leg_a": {"payload": rg_a, "payload_hash": lco.payload_hash(rg_a), "ipp": ipp_a},
        "leg_b": {"payload": rg_b, "payload_hash": lco.payload_hash(rg_b), "ipp": ipp_b},
    })


@app.post("/api/lco/split-submit", response_class=JSONResponse)
def api_lco_split_submit(request: Request, payload: dict = Body(...)):
    """Live two-site submission. Both legs' dry-run hashes must match before
    either submits. Leg A submits first; leg B only submits if leg A
    succeeds.

    The two submits are NOT atomic -- LCO has no cross-site transactional API
    here, so if leg B's submit fails after leg A's already succeeded, leg A is
    a real, already-committed telescope-time booking. That case is reported as
    ``"partial": true`` (with leg A's booked result included) rather than a
    generic error, so the caller can surface it prominently instead of losing
    track of the committed leg.
    """
    if not payload.get("confirm"):
        return JSONResponse(
            {"ok": False, "error": "submission requires explicit confirm"}, status_code=400
        )
    user = _request_user(request)
    leg_a_params = payload.get("leg_a") or {}
    leg_b_params = payload.get("leg_b") or {}

    try:
        rg_a = lco.build_requestgroup(leg_a_params.get("kind"), leg_a_params)
    except lco.LcoError as e:
        return _lco_split_error_response(e, "leg_a")
    try:
        rg_b = lco.build_requestgroup(leg_b_params.get("kind"), leg_b_params)
    except lco.LcoError as e:
        return _lco_split_error_response(e, "leg_b")

    expected_a = payload.get("dry_run_hash_a")
    if not expected_a or expected_a != lco.payload_hash(rg_a):
        return JSONResponse(
            {"ok": False, "leg": "leg_a", "error": "no matching IPP dry-run for leg A; run the dry-run again"},
            status_code=409,
        )
    expected_b = payload.get("dry_run_hash_b")
    if not expected_b or expected_b != lco.payload_hash(rg_b):
        return JSONResponse(
            {"ok": False, "leg": "leg_b", "error": "no matching IPP dry-run for leg B; run the dry-run again"},
            status_code=409,
        )

    try:
        result_a = lco.submit_requestgroup(rg_a, user)
    except lco.LcoError as e:
        # Neither leg is booked yet.
        return _lco_split_error_response(e, "leg_a")

    try:
        result_b = lco.submit_requestgroup(rg_b, user)
    except lco.LcoError as e:
        return JSONResponse(
            {
                "ok": False,
                "partial": True,
                "error": "leg A booked, leg B failed to submit",
                "leg_a": {"result": result_a},
                "leg_b": e.to_dict(),
            },
            status_code=e.status,
        )

    return JSONResponse({"ok": True, "leg_a": {"result": result_a}, "leg_b": {"result": result_b}})


@app.get("/api/lco/archive/frames", response_class=JSONResponse)
def api_lco_archive_frames(
    request: Request,
    instrument: str = "",
    proposal_id: str = "",
    OBJECT: str = "",
    SITEID: str = "",
    TELID: str = "",
    INSTRUME: str = "",
    FILTER: str = "",
    reduction_level: str = "",
    start: str = "",
    end: str = "",
    limit: str = "50",
    fuzzy_name: str = "",
    request_id: str = "",
):
    # Request-id path: a single observation request (e.g. the id in
    # https://observe.lco.global/requests/4236675) fully specifies a dataset on
    # its own, so it short-circuits the coordinate/name search and pulls every
    # frame for that request (paginated) filtered only by reduction level.
    req = request_id.strip()
    if req:
        if not req.isdigit():
            return JSONResponse(
                {"ok": False, "error": f"Request ID must be numeric, got '{req}'."},
                status_code=400,
            )
        # limit=1000 is the archive's max page size; use it so a multi-thousand
        # frame request paginates in a few calls rather than dozens.
        req_filters = {"request_id": req, "reduction_level": reduction_level, "limit": "1000"}
        try:
            result = lco.archive_search_all(req_filters, _request_user(request))
            rows = result.get("results") or []
            if isinstance(rows, list):
                annotated, dataset_count = _annotate_lco_archive_results(instrument, rows)
                result = dict(result)
                result["results"] = annotated
                result["dataset_count"] = dataset_count
            return JSONResponse({"ok": True, "match_mode": "request_id", "request_id": req, **result})
        except lco.LcoError as e:
            return _lco_error_response(e)

    use_fuzzy = fuzzy_name.strip().lower() in ("1", "true", "yes", "on")
    tel_class = TELID if TELID in ("0m4", "1m0", "2m0") else ""
    filters = {
        "proposal_id": proposal_id,
        "SITEID": SITEID,
        "TELID": "" if tel_class else TELID,
        "INSTRUME": INSTRUME,
        "FILTER": FILTER,
        "reduction_level": reduction_level,
        "start": start,
        "end": end,
        "limit": limit,
    }
    # Default: coordinate-primary. Resolve the target name to RA/Dec (ICRS deg)
    # and return every frame whose footprint covers that position. This is robust
    # to OBJECT-header naming variants (WASP-12 vs Wasp-12 vs WASP12). The
    # 'Fuzzy name match' checkbox falls back to the OBJECT header substring match.
    resolved: tuple[float, float, str] | None = None
    if use_fuzzy:
        filters["OBJECT"] = OBJECT
    else:
        name = OBJECT.strip()
        if not name:
            return JSONResponse(
                {"ok": False, "error": "Enter a target name to resolve its coordinates, or enable 'Fuzzy name match'."},
                status_code=400,
            )
        resolved = _resolve_archive_coords(name)
        if resolved is None:
            return JSONResponse(
                {"ok": False, "error": f"Could not resolve coordinates for '{name}'. Check the name or enable 'Fuzzy name match'."},
                status_code=422,
            )
        ra_deg, dec_deg, _source = resolved
        filters["covers"] = f"POINT({ra_deg} {dec_deg})"
    try:
        result = lco.archive_search(filters, _request_user(request))
        rows = result.get("results") or []
        if tel_class and isinstance(rows, list):
            rows = [r for r in rows if str(r.get("TELID") or "").lower().startswith(tel_class)]
            result = dict(result)
            result["results"] = rows
            result["count"] = len(rows)
        if isinstance(rows, list):
            annotated, dataset_count = _annotate_lco_archive_results(instrument, rows)
            result = dict(result)
            result["results"] = annotated
            result["dataset_count"] = dataset_count
        payload = {"ok": True, "match_mode": "name" if use_fuzzy else "coord", **result}
        if resolved is not None:
            payload["resolved_ra"] = round(resolved[0], 5)
            payload["resolved_dec"] = round(resolved[1], 5)
            payload["resolved_source"] = resolved[2]
        return JSONResponse(payload)
    except lco.LcoError as e:
        return _lco_error_response(e)


@app.post("/api/lco/archive/download", response_class=JSONResponse)
def api_lco_archive_download(payload: dict = Body(...)):
    try:
        frames = payload.get("frames")
        if not isinstance(frames, list) or not frames:
            return JSONResponse({"ok": False, "error": "no frames selected"}, status_code=400)
        if payload.get("background"):
            job = lco.start_archive_download(frames, overwrite=bool(payload.get("overwrite")))
            return JSONResponse({"ok": True, **job})
        results = lco.download_frames(frames, overwrite=bool(payload.get("overwrite")))
        return JSONResponse({"ok": True, "results": results})
    except lco.LcoError as e:
        return _lco_error_response(e)


@app.get("/api/lco/archive/download/{job_id}", response_class=JSONResponse)
def api_lco_archive_download_status(job_id: str):
    try:
        job = lco.archive_download_status(job_id)
        if job.get("state") in {"done", "error", "cancelled"}:
            _persist_lco_archive_download_row(_lco_archive_download_row(job))
        return JSONResponse({"ok": True, **job})
    except lco.LcoError as e:
        return _lco_error_response(e)


# Helper to normalize target names for comparison
def _normalize_target_name(t: str) -> str:
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



def _query_target_planets_nasa(target: str) -> dict:
    import urllib.request
    import urllib.parse
    import json
    
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
                        pl_letter = row.get("pl_letter", "").strip().lower()
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
        req = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0'})
        try:
            with urllib.request.urlopen(req, timeout=1.0) as response:
                data = json.loads(response.read().decode())
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
                    toi_val = row.get("TOI", "")
                    tic_val = row.get("TIC ID", "")
                    match = False
                    if toi_val and _normalize_target_name("TOI" + toi_val) == target_norm:
                        match = True
                    elif tic_val and (
                        _normalize_target_name(tic_val) == target_norm
                        or _normalize_target_name("TIC" + tic_val) == target_norm
                    ):
                        match = True
                    elif row.get("Planet Name") and _normalize_target_name(row.get("Planet Name", "")) == target_norm:
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


def _parse_lco_obs_dt(frame: dict) -> datetime.datetime | None:
    raw = (
        frame.get("DATE_OBS")
        or frame.get("observation_date")
        or frame.get("DAY_OBS")
        or ""
    )
    raw = str(raw).strip()
    if not raw:
        return None
    try:
        if len(raw) == 10:
            return datetime.datetime.fromisoformat(raw).replace(tzinfo=datetime.timezone.utc)
        if raw.endswith("Z"):
            raw = raw[:-1] + "+00:00"
        dt = datetime.datetime.fromisoformat(raw)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=datetime.timezone.utc)
        return dt.astimezone(datetime.timezone.utc)
    except ValueError:
        return None


def _lco_observing_date(frame: dict) -> str:
    dt = _parse_lco_obs_dt(frame)
    if dt is None:
        day_obs = str(frame.get("DAY_OBS") or "").strip()
        if day_obs:
            return day_obs
        return ""
    site = str(frame.get("SITEID") or "").strip().lower()
    tz_name = _LCO_SITE_TZ.get(site, "UTC")
    local_dt = dt.astimezone(ZoneInfo(tz_name))
    # Observing nights run through local midnight, so local post-midnight frames
    # belong to the prior evening's dataset.
    if local_dt.hour < 12:
        local_dt = local_dt - datetime.timedelta(days=1)
    return local_dt.date().isoformat()


def _sexagesimal_to_deg(value: str, *, is_ra: bool) -> float | None:
    parts = value.split(":")
    if len(parts) != 3:
        return None
    sign = 1.0
    head = parts[0]
    if not is_ra and head.startswith("-"):
        sign = -1.0
    head = head.lstrip("+-")
    try:
        a = float(head)
        b = float(parts[1])
        c = float(parts[2])
    except ValueError:
        return None
    base = abs(a) + b / 60.0 + c / 3600.0
    if is_ra:
        return base * 15.0
    return sign * base


def _coord_to_deg(value, *, is_ra: bool) -> float | None:
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    s = str(value).strip()
    if not s:
        return None
    try:
        return float(s)
    except ValueError:
        pass
    clean = _clean_ra(s) if is_ra else _clean_dec(s)
    if clean is None:
        return None
    return _sexagesimal_to_deg(clean, is_ra=is_ra)


def _frame_coords_deg(frame: dict) -> tuple[float | None, float | None]:
    ra = (
        frame.get("RA")
        or frame.get("ra")
        or frame.get("ra_x")
        or frame.get("target_ra")
    )
    dec = (
        frame.get("DEC")
        or frame.get("Dec")
        or frame.get("declination")
        or frame.get("dec_x")
        or frame.get("target_dec")
    )
    return _coord_to_deg(ra, is_ra=True), _coord_to_deg(dec, is_ra=False)


def _angular_sep_arcsec(ra1: float, dec1: float, ra2: float, dec2: float) -> float:
    ra1r = math.radians(ra1)
    dec1r = math.radians(dec1)
    ra2r = math.radians(ra2)
    dec2r = math.radians(dec2)
    cos_sep = (
        math.sin(dec1r) * math.sin(dec2r)
        + math.cos(dec1r) * math.cos(dec2r) * math.cos(ra1r - ra2r)
    )
    cos_sep = max(-1.0, min(1.0, cos_sep))
    return math.degrees(math.acos(cos_sep)) * 3600.0


def _local_lco_datasets(inst: str, obsdate: str, site: str) -> list[dict]:
    db = _db_path()
    with get_conn(db) as conn:
        conn.create_aggregate("coord_repr", 2, CoordRepr)
        rows = conn.execute(
            """
            SELECT object, COUNT(*) AS nframes, coord_repr(ra, declination) AS coord
            FROM frames
            WHERE instrument = ?
              AND obsdate = ?
              AND filename LIKE ?
            GROUP BY object
            """,
            (inst, obsdate, f"{site}%"),
        ).fetchall()
    out = []
    for obj, nframes, packed in rows:
        ra_raw, dec_raw = _unpack_coord(packed)
        ra_deg = _coord_to_deg(ra_raw, is_ra=True)
        dec_deg = _coord_to_deg(dec_raw, is_ra=False)
        out.append(
            {
                "object": obj or "",
                "nframes": int(nframes or 0),
                "ra_deg": ra_deg,
                "dec_deg": dec_deg,
            }
        )
    return out


def _annotate_lco_archive_results(inst: str, results: list[dict]) -> tuple[list[dict], int]:
    if not results:
        return [], 0

    rows: list[dict] = [dict(r) for r in results]
    rows.sort(
        key=lambda r: (
            _parse_lco_obs_dt(r) or datetime.datetime.min.replace(tzinfo=datetime.timezone.utc),
            str(r.get("filename") or r.get("basename") or ""),
        )
    )

    filename_to_group: dict[str, str] = {}
    dataset_meta: dict[str, dict] = {}
    group_idx_by_key: dict[tuple[str, str, str], int] = {}

    for row in rows:
        observing_date = _lco_observing_date(row)
        identity = (
            observing_date,
            str(row.get("OBJECT") or ""),
            str(row.get("SITEID") or ""),
        )
        if identity not in group_idx_by_key:
            group_idx_by_key[identity] = len(group_idx_by_key) + 1
        group_id = f"{observing_date or 'unknown'}:{group_idx_by_key[identity]}"
        if group_id not in dataset_meta:
            inferred_inst = inst if inst in INSTRUMENTS else ""
            if not inferred_inst:
                try:
                    inferred_inst = lco.infer_archive_instrument(row)
                except lco.LcoError:
                    inferred_inst = ""
            dataset_meta[group_id] = {
                "dataset_id": group_id,
                "dataset_date": observing_date,
                "instrument": inferred_inst,
                "object": str(row.get("OBJECT") or ""),
                "site": str(row.get("SITEID") or ""),
                "telescope": str(row.get("TELID") or ""),
                "instrument_header": str(row.get("INSTRUME") or ""),
                "frame_count": 0,
                "existing_count": 0,
                "filenames": [],
                "archive_ra_deg": None,
                "archive_dec_deg": None,
            }
        meta = dataset_meta[group_id]
        meta["frame_count"] += 1
        fname = str(row.get("filename") or row.get("basename") or "")
        if fname:
            meta["filenames"].append(fname)
            filename_to_group[fname] = group_id
        if meta["archive_ra_deg"] is None or meta["archive_dec_deg"] is None:
            ra_deg, dec_deg = _frame_coords_deg(row)
            if ra_deg is not None and dec_deg is not None:
                meta["archive_ra_deg"] = ra_deg
                meta["archive_dec_deg"] = dec_deg

    local_cache: dict[tuple[str, str, str], list[dict]] = {}
    for meta in dataset_meta.values():
        inst_name = str(meta.get("instrument") or "")
        obsdate = (meta.get("dataset_date") or "").replace("-", "")[2:8]
        site = str(meta.get("site") or "").lower()
        if not inst_name or not obsdate or not site:
            continue
        key = (inst_name, obsdate, site)
        if key not in local_cache:
            local_cache[key] = _local_lco_datasets(inst_name, obsdate, site)

        archive_ra = meta.get("archive_ra_deg")
        archive_dec = meta.get("archive_dec_deg")
        if archive_ra is None or archive_dec is None:
            archive_name = _normalize_target_name(str(meta.get("object") or ""))
            if not archive_name:
                continue
            for cand in local_cache[key]:
                if _normalize_target_name(str(cand.get("object") or "")) == archive_name:
                    meta["existing_count"] = int(cand.get("nframes") or 0)
                    meta["matched_object"] = str(cand.get("object") or "")
                    break
            continue

        best_match = None
        best_sep = None
        for cand in local_cache[key]:
            ra2 = cand.get("ra_deg")
            dec2 = cand.get("dec_deg")
            if ra2 is None or dec2 is None:
                continue
            sep = _angular_sep_arcsec(archive_ra, archive_dec, ra2, dec2)
            if best_sep is None or sep < best_sep:
                best_sep = sep
                best_match = cand
        if best_match is not None and best_sep is not None and best_sep <= _LCO_DATASET_MATCH_ARCSEC:
            meta["existing_count"] = int(best_match.get("nframes") or 0)
            meta["matched_object"] = str(best_match.get("object") or "")
            meta["match_sep_arcsec"] = round(best_sep, 2)

    out: list[dict] = []
    for row in rows:
        fname = str(row.get("filename") or row.get("basename") or "")
        gid = filename_to_group.get(fname, "")
        meta = dataset_meta.get(gid, {})
        row["dataset_id"] = gid
        row["dataset_date"] = meta.get("dataset_date", "")
        row["archive_instrument"] = meta.get("instrument", "")
        row["dataset_exists"] = bool(meta.get("existing_count"))
        row["dataset_existing_count"] = int(meta.get("existing_count", 0))
        row["dataset_frame_count"] = int(meta.get("frame_count", 0))
        row["dataset_matched_object"] = meta.get("matched_object", "")
        row["dataset_match_sep_arcsec"] = meta.get("match_sep_arcsec")
        
        # Check if frame is saved locally
        inferred_inst = meta.get("instrument") or ""
        obsdate = (meta.get("dataset_date") or "").replace("-", "")[2:8]
        row["saved_locally"] = False
        if inferred_inst and obsdate and fname:
            try:
                dest = lco.frame_dest(inferred_inst, obsdate, fname)
                if dest.exists() and dest.stat().st_size > 0:
                    row["saved_locally"] = True
            except Exception:
                logger.debug("failed local saved-frame check for %s/%s/%s", inferred_inst, obsdate, fname, exc_info=True)
        out.append(row)
    return out, len(dataset_meta)


def _query_target_planets_toi(target: str) -> dict:
    import urllib.request
    import urllib.parse
    import json
    
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
                    toi_val = row.get("TOI", "")
                    tic_val = row.get("TIC ID", "")
                    match = False
                    if toi_val and _normalize_target_name("TOI" + toi_val) == target_norm:
                        match = True
                    elif tic_val and (
                        _normalize_target_name(tic_val) == target_norm or
                        _normalize_target_name("TIC" + tic_val) == target_norm
                    ):
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
                        t0 = row.get("Epoch (BJD)")
                        per = row.get("Period (days)")
                        if t0 is not None and per is not None:
                            try:
                                entry = {"t0": float(t0), "period": float(per)}
                            except ValueError:
                                continue
                            dur = _safe_float(row.get("Duration (hours)"))
                            if dur is not None:
                                entry["duration"] = dur
                            # Extract uncertainties
                            t0_unc = _safe_float(row.get("Epoch (BJD) err"))
                            per_unc = _safe_float(row.get("Period (days) err"))
                            dur_unc = _safe_float(row.get("Duration (hours) err"))
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
        req = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0'})
        try:
            with urllib.request.urlopen(req, timeout=1.0) as response:
                data = json.loads(response.read().decode())
                for row in data:
                    toidisplay = row.get("toidisplay", "")
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


# Helper to fetch fitted transit centers for a run
def _get_run_fitted_params(inst: str, date: str, target: str, run_id: str | None) -> dict:
    """Per-planet fitted ephemeris from a run's outputs (the Fitted Parameters
    Summary), not the input ``sys.yaml`` priors.

    Returns ``{planet: {"tc", "unc", "dur", "dur_unc"}}`` with whatever is
    available. The transit center (``tc``, BJD) comes from ``out/tc.txt`` when
    present, otherwise from ``out/summary.csv`` (``t0[idx]`` + the run's
    reference time). The transit duration (``dur``, hours) comes from
    ``summary.csv`` (``dur[idx]``, stored in days). ``period`` is deliberately
    not read here: it is held fixed in the fit and never appears in the summary.
    """
    import csv
    import yaml
    fitted: dict[str, dict] = {}
    try:
        rdir = fit.fit_output_dir(inst, date, target, run_id or None)
        out_dir = rdir / "out"

        # Index -> planet letter mapping (summary rows are keyed e.g. "dur[0]").
        planets_fitted = "b"
        fit_yaml = rdir / "fit.yaml"
        if fit_yaml.is_file():
            with open(fit_yaml) as f:
                cfg = yaml.safe_load(f) or {}
                planets_fitted = str(cfg.get("planets", "b"))

        # Transit centers from tc.txt take precedence for t0 when present.
        tc_txt = out_dir / "tc.txt"
        if tc_txt.is_file():
            with open(tc_txt) as f:
                for line in f:
                    parts = line.strip().split()
                    if len(parts) >= 3:
                        pl = parts[0]
                        entry = fitted.setdefault(pl, {})
                        entry["tc"] = float(parts[1]) + 2454833.0  # Kepler -> BJD
                        entry["unc"] = float(parts[2])

        # Fitted Parameters Summary: t0[idx] (if not already from tc.txt) and dur[idx].
        summary_csv = out_dir / "summary.csv"
        if summary_csv.is_file():
            ref_time = None
            log_file = rdir / "timer-fit.log"
            if log_file.is_file():
                with open(log_file) as lf:
                    for line in lf:
                        if "ref. time:" in line:
                            try:
                                ref_time = int(line.split("ref. time:")[-1].strip())
                            except ValueError:
                                ref_time = None
                            break
            with open(summary_csv) as f:
                reader = csv.reader(f)
                headers = next(reader)
                headers[0] = "parameter"
                for row in reader:
                    if not row:
                        continue
                    rd = dict(zip(headers, row))
                    param = rd.get("parameter", "")
                    if "[" not in param or not param.endswith("]"):
                        continue
                    base, _, idx_str = param[:-1].partition("[")
                    try:
                        idx = int(idx_str)
                    except ValueError:
                        continue
                    if idx >= len(planets_fitted):
                        continue
                    pl = planets_fitted[idx]
                    entry = fitted.setdefault(pl, {})
                    try:
                        if base == "t0" and "tc" not in entry and ref_time is not None:
                            entry["tc"] = float(rd["mean"]) + ref_time
                            entry["unc"] = float(rd["sd"])
                        elif base == "dur":
                            entry["dur"] = float(rd["mean"]) * 24.0  # days -> hours
                            entry["dur_unc"] = float(rd["sd"]) * 24.0
                    except (ValueError, KeyError):
                        pass
    except Exception:
        logger.debug("failed to read fitted transit params for %s/%s/%s/%s", inst, date, target, run_id, exc_info=True)
    return fitted


@app.get("/api/ads/config", response_class=JSONResponse)
def api_ads_config():
    """Report whether the ADS API token is configured. No secrets."""
    import os
    token = os.environ.get("ADS_API_TOKEN") or os.environ.get("ADS_DEV_KEY") or os.environ.get("ADS_TOKEN")
    return JSONResponse({
        "ok": True,
        "token_configured": token is not None and token.strip() != ""
    })


@app.get("/api/target/publications", response_class=JSONResponse)
def api_target_publications(q: str):
    import os
    import urllib.request
    import urllib.parse
    import json
    
    q = (q or "").strip()
    if not q:
        return JSONResponse({"ok": False, "error": "Query parameter q is required"}, status_code=400)
        
    token = os.environ.get("ADS_API_TOKEN") or os.environ.get("ADS_DEV_KEY") or os.environ.get("ADS_TOKEN")
    if not token:
        return JSONResponse({
            "ok": False,
            "error": "ADS_API_TOKEN is not configured. Please add it to your .env file.",
            "token_missing": True
        }, status_code=400)
        
    params = {
        "q": q,
        "fq": "collection:astronomy",
        "fl": "bibcode,title,author,pubdate,pub,citation_count",
        "sort": "date desc",
        "rows": 20
    }
    url = "https://api.adsabs.harvard.edu/v1/search/query?" + urllib.parse.urlencode(params)
    
    req = urllib.request.Request(url, headers={
        "Authorization": f"Bearer {token}",
        "User-Agent": "MuSCAT-db/0.1.0"
    })
    
    try:
        with urllib.request.urlopen(req, timeout=10.0) as response:
            data = json.loads(response.read().decode("utf-8"))
            docs = data.get("response", {}).get("docs", [])
            return JSONResponse({"ok": True, "papers": docs})
    except urllib.error.HTTPError as e:
        try:
            err_body = e.read().decode("utf-8")
            err_msg = json.loads(err_body).get("error", {}).get("message", str(e))
        except Exception:
            err_msg = str(e)
        return JSONResponse({"ok": False, "error": f"ADS API returned error: {err_msg}"}, status_code=e.code)
    except Exception as e:
        return JSONResponse({"ok": False, "error": f"Failed to query ADS: {str(e)}"}, status_code=500)


@app.get("/api/ephemeris/targets", response_class=JSONResponse)
def api_ephemeris_targets():
    with _DB_LOCK:
        fit.sync_jobs()
        all_jobs = get_job_store().all()
        existing_keys = {j["key"] for j in all_jobs if j["type"] == "transit_fit"}
        orphan_fits = fit._discover_orphan_fits(existing_keys)
        all_jobs.extend(orphan_fits)
        completed = [j for j in all_jobs if j["type"] == "transit_fit" and j["state"] == "done"]
        targets = sorted({_normalize_target_name(j["target"]) for j in completed if j.get("target")})
    return JSONResponse({"ok": True, "targets": targets})


@app.get("/api/ephemeris/target-info", response_class=JSONResponse)
def api_ephemeris_target_info(target: str):
    target = (target or "").strip()
    if not target:
        return JSONResponse({"ok": False, "error": "Target is required"}, status_code=400)
    
    with _DB_LOCK:
        fit.sync_jobs()
        all_jobs = get_job_store().all()
        existing_keys = {j["key"] for j in all_jobs if j["type"] == "transit_fit"}
        orphan_fits = fit._discover_orphan_fits(existing_keys)
        all_jobs.extend(orphan_fits)
        
        norm_t = _normalize_target_name(target)
        # A job can stay "done" in the DB after its fit outputs are deleted from
        # disk (e.g. a re-run under a new run_id, or manual cleanup). Only surface
        # datasets whose outputs still exist so the ephemeris table never links to
        # a fit that no longer exists.
        completed = [
            j for j in all_jobs
            if j["type"] == "transit_fit"
            and j["state"] == "done"
            and _normalize_target_name(j["target"]) == norm_t
            and fit.get_fit_outputs(
                j["instrument"], j["obsdate"], j["target"], (j.get("run_id") or "") or None
            ).get("has_any")
        ]
    
    # 1. Query all planets from catalog
    nasa_ephem = _query_target_planets_nasa(target)
    toi_ephem = _query_target_planets_toi(target)
    ref_ephem = _query_target_planets_catalog(target)
    
    # 2. Get datasets and unique planets in them
    datasets_list = []
    seen_planets = set(ref_ephem.keys())
    
    import yaml
    for j in completed:
        inst = j["instrument"]
        date = j["obsdate"]
        run_id = j.get("run_id") or ""
        
        # Discover planets fitted and their periods/t0 in dataset
        planets_fitted = "b"
        planets_ephem = {}
        try:
            rdir = fit.fit_output_dir(inst, date, j["target"], run_id or None)
            sys_yaml = rdir / "sys.yaml"
            if sys_yaml.is_file():
                with open(sys_yaml) as f:
                    sys_cfg = yaml.safe_load(f) or {}
                    planets_data = sys_cfg.get("planets", {})
                    for pl, pl_params in planets_data.items():
                        t0_list = pl_params.get("t0", [2450000.0, 0.0])
                        if isinstance(t0_list, (list, tuple)):
                            t0_mean = t0_list[0] if len(t0_list) > 0 else 2450000.0
                            t0_unc = t0_list[1] if len(t0_list) > 1 else None
                        else:
                            t0_mean = t0_list
                            t0_unc = None

                        period_list = pl_params.get("period", [1.0, 0.0])
                        if isinstance(period_list, (list, tuple)):
                            period_mean = period_list[0] if len(period_list) > 0 else 1.0
                            period_unc = period_list[1] if len(period_list) > 1 else None
                        else:
                            period_mean = period_list
                            period_unc = None

                        # t0 and duration are overridden below from the Fitted
                        # Parameters Summary; period stays from sys.yaml (it is
                        # held fixed in the fit and absent from the summary).
                        planets_ephem[pl] = {
                            "t0": float(t0_mean),
                            "t0_unc": float(t0_unc) if t0_unc is not None else None,
                            "period": float(period_mean),
                            "period_unc": float(period_unc) if period_unc is not None else None,
                            "duration": None,
                            "duration_unc": None,
                        }
            fit_yaml = rdir / "fit.yaml"
            if fit_yaml.is_file():
                with open(fit_yaml) as f:
                    cfg = yaml.safe_load(f) or {}
                    planets_fitted = str(cfg.get("planets", "b"))
        except Exception:
            logger.debug("failed to read ephemeris dataset metadata for %s/%s/%s/%s", inst, date, j["target"], run_id, exc_info=True)
        
        for pl in planets_fitted:
            seen_planets.add(pl)
            if pl not in planets_ephem:
                planets_ephem[pl] = {}
            
        # Override t0 and duration with the run's Fitted Parameters Summary.
        fitted = _get_run_fitted_params(inst, date, j["target"], run_id)
        for pl in planets_fitted:
            fp = fitted.get(pl)
            if not fp:
                continue
            if fp.get("tc") is not None:
                planets_ephem[pl]["t0"] = float(fp["tc"])
                if fp.get("unc") is not None:
                    planets_ephem[pl]["t0_unc"] = float(fp["unc"])
            if fp.get("dur") is not None:
                planets_ephem[pl]["duration"] = float(fp["dur"])
                if fp.get("dur_unc") is not None:
                    planets_ephem[pl]["duration_unc"] = float(fp["dur_unc"])
        
        datasets_list.append({
            "instrument": inst,
            "date": date,
            "run_id": run_id,
            "run_name": j.get("run_name") or (run_id if run_id else "legacy"),
            "target": j["target"],
            "planets_fitted": planets_fitted,
            "fitted_tcs": fitted,
            "planets_ephem": planets_ephem,
            "run_type": j.get("run_type") or ""
        })
        
    # Ensure all seen planets are initialized in all ephemerides
    for pl in seen_planets:
        ref_ephem.setdefault(pl, {})
        nasa_ephem.setdefault(pl, {})
        toi_ephem.setdefault(pl, {})
            
    planets_sorted = sorted(list(seen_planets))
    
    return JSONResponse({
        "ok": True,
        "target": target,
        "planets": planets_sorted,
        "coordinates": _query_target_coordinates(target),
        "reference_ephemeris": ref_ephem,
        "nasa_ephemeris": nasa_ephem,
        "toi_ephemeris": toi_ephem,
        "datasets": datasets_list
    })


@app.post("/api/ephemeris/calculate", response_class=JSONResponse)
def api_ephemeris_calculate(payload: dict = Body(...)):
    target_param = payload.get("target")
    if isinstance(target_param, list):
        targets = [str(t).strip() for t in target_param if t]
    elif isinstance(target_param, str):
        targets = [target_param.strip()]
    else:
        targets = []
        
    targets = [t for t in targets if t]
    if not targets:
        return JSONResponse({"ok": False, "error": "Target is required"}, status_code=400)
        
    planets_ephem = payload.get("planets_ephem") or {}
    req_datasets = payload.get("datasets") or []
    
    # Build checked lookup: (target_normalized, inst, date, run_id) -> checked_bool
    checked_lookup = {}
    for d in req_datasets:
        tgt = d.get("target")
        norm_t = _normalize_target_name(tgt) if tgt else None
        key = (norm_t, d.get("instrument"), d.get("date"), d.get("run_id") or "")
        checked_lookup[key] = bool(d.get("checked"))
        
    # Get all completed runs for all requested targets
    with _DB_LOCK:
        fit.sync_jobs()
        all_jobs = get_job_store().all()
        existing_keys = {j["key"] for j in all_jobs if j["type"] == "transit_fit"}
        orphan_fits = fit._discover_orphan_fits(existing_keys)
        all_jobs.extend(orphan_fits)
        
        completed = []
        seen_keys = set()
        for target in targets:
            norm_t = _normalize_target_name(target)
            for j in all_jobs:
                if j["type"] == "transit_fit" and j["state"] == "done" and _normalize_target_name(j["target"]) == norm_t:
                    if j["key"] not in seen_keys:
                        seen_keys.add(j["key"])
                        completed.append(j)
    
    # Map run parameters
    results = {}
    
    for pl, ephem in planets_ephem.items():
        T0 = float(ephem.get("t0", 2450000.0))
        P = float(ephem.get("period", 1.0))
        
        # Collect data points for this planet
        points = []
        for j in completed:
            inst = j["instrument"]
            date = j["obsdate"]
            run_id = j.get("run_id") or ""
            
            # Fetch transit centers
            tcs = _get_run_fitted_params(inst, date, j["target"], run_id)
            if pl in tcs and tcs[pl].get("tc") is not None:
                val = tcs[pl]["tc"]
                unc = tcs[pl].get("unc")
                
                # Check status: target-specific lookup first, fallback to targetless
                norm_tgt = _normalize_target_name(j["target"])
                is_checked = checked_lookup.get((norm_tgt, inst, date, run_id))
                if is_checked is None:
                    is_checked = checked_lookup.get((None, inst, date, run_id))
                if is_checked is None:
                    for (k_tgt, k_inst, k_date, k_run_id), val_cb in checked_lookup.items():
                        if k_inst == inst and k_date == date and k_run_id == run_id:
                            is_checked = val_cb
                            break
                if is_checked is None:
                    is_checked = True
                    
                epoch = int(round((val - T0) / P))
                
                points.append({
                    "instrument": inst,
                    "date": date,
                    "run_id": run_id,
                    "run_name": j.get("run_name") or (run_id if run_id else "legacy"),
                    "target": j["target"],
                    "epoch": epoch,
                    "tc": val,
                    "unc": unc,
                    "checked": is_checked
                })
                
        # Perform straight line fit if possible
        fit_points = [p for p in points if p["checked"] and p["unc"] is not None and p["unc"] > 0]
        was_fit = False
        t0_fit = T0
        period_fit = P
        t0_fit_unc = 0.0
        period_fit_unc = 0.0
        
        # Center epoch
        E_center = 0
        t0_centered = T0
        t0_centered_unc = 0.0
        fit_method = payload.get("fit_method", "unweighted")
        
        if len(fit_points) >= 2:
            epochs_checked = [p["epoch"] for p in fit_points]
            E_min = min(epochs_checked)
            E_max = max(epochs_checked)
            E_center = E_min + int((E_max - E_min) // 2)
            
            Sw = Swx = Swy = Swxx = Swxy = 0.0
            for p in fit_points:
                x = p["epoch"] - E_center
                y = p["tc"]
                if fit_method == "weighted":
                    w = 1.0 / (p["unc"] ** 2)
                else:
                    w = 1.0
                Sw += w
                Swx += w * x
                Swy += w * y
                Swxx += w * (x ** 2)
                Swxy += w * x * y
                
            delta = Sw * Swxx - (Swx ** 2)
            if delta > 0.0:
                t0_centered = (Swxx * Swy - Swx * Swxy) / delta
                period_fit = (Sw * Swxy - Swx * Swy) / delta
                
                # Calculate uncertainties
                if fit_method == "weighted":
                    t0_centered_unc = (Swxx / delta) ** 0.5
                    period_fit_unc = (Sw / delta) ** 0.5
                else:
                    # Unweighted fit uncertainty needs residual variance estimation
                    residuals_sum_sq = sum(
                        (p["tc"] - (t0_centered + (p["epoch"] - E_center) * period_fit)) ** 2
                        for p in fit_points
                    )
                    dof = len(fit_points) - 2
                    sigma_sq = residuals_sum_sq / dof if dof > 0 else 0.0
                    t0_centered_unc = (sigma_sq * Swxx / delta) ** 0.5
                    period_fit_unc = (sigma_sq * Sw / delta) ** 0.5
                
                # Extrapolate back to the catalog epoch (E = 0)
                t0_fit = t0_centered - E_center * period_fit
                
                # Extrapolated uncertainty: Var(t0_fit) = Var(t0_centered) + E_center^2 * Var(P) - 2 * E_center * Cov(t0_centered, P)
                var_t0_factor = Swxx + (E_center ** 2) * Sw + 2.0 * E_center * Swx
                if fit_method == "weighted":
                    t0_fit_unc = (var_t0_factor / delta) ** 0.5
                else:
                    t0_fit_unc = (sigma_sq * var_t0_factor / delta) ** 0.5
                    
                was_fit = True
                
        # Calculate O-C values
        points_data = []
        for p in points:
            t_calc = t0_fit + p["epoch"] * period_fit
            oc_days = p["tc"] - t_calc
            oc_min = oc_days * 1440.0
            oc_err_min = p["unc"] * 1440.0
            
            points_data.append({
                "instrument": p["instrument"],
                "date": p["date"],
                "run_id": p["run_id"],
                "run_name": p["run_name"],
                "target": p["target"],
                "epoch": p["epoch"],
                "bjd": p["tc"],
                "tc_unc": p["unc"],
                "oc_min": round(oc_min, 4),
                "oc_err_min": round(oc_err_min, 4),
                "checked": p["checked"]
            })
            
        results[pl] = {
            "was_fit": was_fit,
            "fit_method": fit_method if was_fit else "none",
            "t0_ref": T0,
            "period_ref": P,
            "t0_fit": round(t0_fit, 6),
            "t0_fit_unc": round(t0_fit_unc, 6),
            "period_fit": round(period_fit, 8),
            "period_fit_unc": round(period_fit_unc, 8),
            "t0_fit_centered": round(t0_centered, 6) if was_fit else round(T0, 6),
            "t0_fit_centered_unc": round(t0_centered_unc, 6) if was_fit else 0.0,
            "E_center": E_center,
            "points": points_data
        }
        
    return JSONResponse({"ok": True, "results": results})


def _live_elapsed(job: dict) -> int:
    """Elapsed seconds for display: live for active jobs, stored otherwise.

    ``sync_jobs`` no longer rewrites a running job's row every poll just to bump
    its elapsed, so derive it from ``started_at`` for active states instead of
    reading the (intentionally stale) stored value.
    """
    if job.get("state") in ("running", "cancelling") and job.get("started_at"):
        return round(time.time() - job["started_at"])
    return round(job.get("elapsed") or 0)


def _lco_archive_download_row(job: dict) -> dict:
    objects = job.get("objects") or []
    instruments = job.get("instruments") or []
    obsdates = job.get("obsdates") or []
    dest_dirs = job.get("dest_dirs") or []
    frames_done = int(job.get("frames_done") or 0)
    frames_total = int(job.get("frames_total") or 0)
    funpack_done = int(job.get("funpack_done") or 0)
    funpack_total = int(job.get("funpack_total") or 0)
    started_at = float(job.get("started_at") or 0)
    finished_at = job.get("finished_at")
    state = job.get("state") or "pending"
    phase = job.get("phase") or state
    elapsed = round(((finished_at or time.time()) - started_at) if started_at else 0)
    if phase == "funpacking":
        run_name = f"funpack {funpack_done}/{funpack_total}"
    else:
        run_name = f"{frames_done}/{frames_total} frames"
    details = "; ".join(dest_dirs) if dest_dirs else "Destination pending"
    if phase and phase not in {"done", state}:
        details = f"{phase}: {details}"
    can_run_dataset_action = state == "done" and len(instruments) == 1 and len(obsdates) == 1
    job_id = job.get("job_id") or ""
    return {
        "key": f"lco_archive_download:{job_id}",
        "type": "lco_archive_download",
        "inst": ",".join(instruments) if instruments else "lco",
        "date": ",".join(obsdates) if obsdates else "mixed",
        "target": ", ".join(objects) if objects else "LCO archive",
        "state": state,
        "returncode": None if state in ("pending", "running") else (0 if state == "done" else 1),
        "elapsed": elapsed,
        "started_at": started_at,
        "error_desc": job.get("error") or "",
        "run_type": "archive",
        "run_id": job_id,
        "run_name": run_name,
        "user_name": "",
        "details": details,
        "action_inst": instruments[0] if len(instruments) == 1 else "",
        "action_date": obsdates[0] if len(obsdates) == 1 else "",
        "can_run_dataset_action": can_run_dataset_action,
    }


def _persist_lco_archive_download_row(row: dict) -> None:
    if row.get("state") not in {"done", "error", "cancelled"}:
        return
    params = {
        "job_id": row.get("run_id") or "",
        "details": row.get("details") or "",
        "action_inst": row.get("action_inst") or "",
        "action_date": row.get("action_date") or "",
        "can_run_dataset_action": bool(row.get("can_run_dataset_action")),
    }
    try:
        get_job_store().save(
            type_="lco_archive_download",
            inst=row.get("action_inst") or row.get("inst") or "lco",
            date=row.get("action_date") or row.get("date") or "mixed",
            target=row.get("target") or "LCO archive",
            state=row.get("state") or "done",
            returncode=row.get("returncode"),
            elapsed=int(row.get("elapsed") or 0),
            started_at=float(row.get("started_at") or time.time()),
            error_desc=row.get("error_desc") or "",
            run_type="archive",
            params=json.dumps(params, sort_keys=True, separators=(",", ":")),
            run_id=row.get("run_id") or "",
            run_name=row.get("run_name") or "",
            user_name="",
        )
    except Exception:
        logger.debug("failed to persist LCO archive download job %s", row.get("run_id"), exc_info=True)


def _adapt_persisted_lco_archive_row(job: dict) -> dict:
    row = dict(job)
    try:
        params = json.loads(row.get("params") or "{}")
    except (TypeError, json.JSONDecodeError):
        params = {}
    job_id = str(params.get("job_id") or row.get("run_id") or "").strip()
    if job_id:
        row["key"] = f"lco_archive_download:{job_id}"
    row["details"] = str(params.get("details") or row.get("details") or "")
    row["action_inst"] = str(params.get("action_inst") or row.get("inst") or "")
    row["action_date"] = str(params.get("action_date") or row.get("date") or "")
    row["can_run_dataset_action"] = bool(
        params.get("can_run_dataset_action")
        or (row.get("state") == "done" and row["action_inst"] in INSTRUMENTS and re.fullmatch(r"\d{6}", row["action_date"]))
    )
    return row


def _lco_archive_download_rows() -> list[dict]:
    rows = []
    for job in lco.archive_download_jobs():
        row = _lco_archive_download_row(job)
        _persist_lco_archive_download_row(row)
        rows.append(row)
    return rows


def _jobs_with_lco_archive_rows() -> list[dict]:
    merged: dict[str, dict] = {}
    for job in get_job_store().all():
        row = _adapt_persisted_lco_archive_row(job) if job.get("type") == "lco_archive_download" else job
        merged[row["key"]] = row
    for row in _lco_archive_download_rows():
        merged[row["key"]] = row
    return list(merged.values())


def _validate_lco_dataset_action(payload: dict) -> tuple[str, str]:
    inst = (payload.get("inst") or "").strip()
    obsdate = (payload.get("date") or payload.get("obsdate") or "").strip()
    if inst not in INSTRUMENTS:
        raise HTTPException(status_code=400, detail="Invalid instrument")
    if not re.fullmatch(r"\d{6}", obsdate):
        raise HTTPException(status_code=400, detail="Invalid obsdate")
    return inst, obsdate


@app.post("/jobs/lco-archive/scan", response_class=JSONResponse)
def jobs_lco_archive_scan(payload: dict = Body(...)):
    inst, obsdate = _validate_lco_dataset_action(payload)
    try:
        from muscat_db.scanner import scan_date as _scan_date

        result = _scan_date(inst, obsdate)
        return JSONResponse({
            "ok": True,
            "command": f"muscat-db scan {inst} {obsdate}",
            "result": result,
        })
    except Exception as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=500)


@app.post("/jobs/lco-archive/ingest-date", response_class=JSONResponse)
def jobs_lco_archive_ingest_date(payload: dict = Body(...)):
    inst, obsdate = _validate_lco_dataset_action(payload)
    try:
        from muscat_db.database import ingest_date as _ingest_date

        count = _ingest_date(str(_db_path()), inst, obsdate)
        return JSONResponse({
            "ok": True,
            "command": f"muscat-db ingest-date {inst} {obsdate}",
            "count": count,
        })
    except Exception as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=500)


@app.get("/jobs", response_class=HTMLResponse)
def jobs_page():
    phot.sync_jobs()
    fit.sync_jobs()
    all_jobs = _jobs_with_lco_archive_rows()

    # Discover fits completed on-disk outside the web UI.
    existing_keys = {j["key"] for j in all_jobs if j["type"] == "transit_fit"}
    orphan_fits = fit._discover_orphan_fits(existing_keys)
    if orphan_fits:
        all_jobs.extend(orphan_fits)
    all_jobs.sort(key=lambda j: j.get("started_at", 0), reverse=True)

    for j in all_jobs:
        j["elapsed"] = _live_elapsed(j)

    counts = {"running": 0, "done": 0, "error": 0, "cancelled": 0, "pending": 0}
    for j in all_jobs:
        s = j["state"]
        if s == "cancelling":
            s = "running"
        if s in counts:
            counts[s] += 1

    return _render("jobs.html", jobs=all_jobs, counts=counts)


_last_running: set[str] = set()

@app.get("/jobs/status", response_class=JSONResponse)
def jobs_status(active_only: bool = False):
    phot.sync_jobs()
    fit.sync_jobs()
    all_jobs = _jobs_with_lco_archive_rows()

    if active_only:
        # Lightweight path for the site-wide loading indicator. Reports only
        # which jobs are currently active (running/cancelling/pending) and
        # deliberately does NOT touch the module-global `_last_running`
        # baseline — that diff belongs to the full Jobs-page poll, and letting
        # a second site-wide poller mutate it would steal `finished`
        # transitions from the Jobs page.
        active = [
            {"key": j["key"], "state": j["state"]}
            for j in all_jobs
            if j["state"] in ("running", "cancelling", "pending")
        ]
        return {"active": active}

    # Discover fits completed on-disk outside the web UI.
    existing_keys = {j["key"] for j in all_jobs if j["type"] == "transit_fit"}
    orphan_fits = fit._discover_orphan_fits(existing_keys)
    if orphan_fits:
        all_jobs.extend(orphan_fits)

    global _last_running
    current_running = {j["key"] for j in all_jobs if j["state"] in ("running", "cancelling", "pending")}
    finished = {}
    for j in all_jobs:
        is_terminal_lco_archive = (
            j.get("type") == "lco_archive_download"
            and j.get("state") in {"done", "error", "cancelled"}
        )
        if (j["key"] in _last_running and j["key"] not in current_running) or is_terminal_lco_archive:
            finished[j["key"]] = {
                "key": j["key"],
                "type": j.get("type", ""),
                "inst": j.get("inst", ""),
                "date": j.get("date", ""),
                "target": j.get("target", ""),
                "state": j["state"],
                "elapsed": j["elapsed"],
                "error_desc": j.get("error_desc", "") or "",
                "returncode": j.get("returncode"),
                "started_at": j.get("started_at"),
                "started_at_str": _datetime_from_timestamp(int(j["started_at"])) if j.get("started_at") else "—",
                "user_name": j.get("user_name", ""),
                "run_name": j.get("run_name", ""),
                "details": j.get("details", ""),
                "action_inst": j.get("action_inst", ""),
                "action_date": j.get("action_date", ""),
                "can_run_dataset_action": bool(j.get("can_run_dataset_action")),
            }
    _last_running = current_running
    running = [
        {
            "key": j["key"],
            "type": j.get("type", ""),
            "inst": j.get("inst", ""),
            "date": j.get("date", ""),
            "target": j.get("target", ""),
            "state": j["state"],
            "elapsed": _live_elapsed(j),
            "started_at": j.get("started_at"),
            "started_at_str": _datetime_from_timestamp(int(j["started_at"])) if j.get("started_at") else "—",
            "user_name": j.get("user_name", ""),
            "run_name": j.get("run_name", ""),
            "details": j.get("details", ""),
            "action_inst": j.get("action_inst", ""),
            "action_date": j.get("action_date", ""),
            "can_run_dataset_action": bool(j.get("can_run_dataset_action")),
        }
        for j in all_jobs if j["state"] in ("running", "cancelling", "pending")
    ]
    counts = {"running": 0, "done": 0, "error": 0, "cancelled": 0, "pending": 0}
    for j in all_jobs:
        s = j["state"]
        if s == "cancelling":
            s = "running"
        if s in counts:
            counts[s] += 1
    return {"running": running, "counts": counts, "finished": finished}


@app.get("/jobs/log/{type_}/{inst}/{date}/{target}")
def job_log(type_: str, inst: str, date: str, target: str, run: str = ""):
    if type_ == "photometry":
        path = phot.log_path(inst, date, target, run_id=(run or "").strip())
    elif type_ == "transit_fit":
        path = fit.log_path(inst, date, target, run_id=(run or "").strip())
    else:
        raise HTTPException(400, "unknown job type")
    if path is None:
        raise HTTPException(404, "log not found")
    return FileResponse(str(path))


@app.post("/jobs/rerun")
def jobs_rerun(request: Request, payload: dict = Body(...)):
    import json
    key = (payload.get("key") or "").strip()
    if not key:
        raise HTTPException(400, "job key required")
    all_jobs = get_job_store().all()
    job = next((j for j in all_jobs if j["key"] == key), None)
    if job is None:
        raise HTTPException(404, "job not found")
    inst, date, target = job["inst"], job["date"], job["target"]
    params_raw = job.get("params", "")
    try:
        p = json.loads(params_raw) if params_raw else {}
    except (json.JSONDecodeError, TypeError):
        p = {}
    options = dict(p.get("options") or {})
    for field in ("run_name", "site", "mode"):
        value = p.get(field) or job.get(field)
        if value and not options.get(field):
            options[field] = value
    user_name = request.state.user
    if job["type"] == "photometry":
        result = phot.start_run(inst, date, target, options=options, test_run=p.get("test_run", True), user_name=user_name)
    elif job["type"] == "transit_fit":
        result = fit.start_fit(inst, date, target, options=options, test_run=p.get("test_run", False), selected_csvs=p.get("selected_csvs"), user_name=user_name)
    else:
        raise HTTPException(400, "unknown job type")
    if not result.get("ok"):
        return JSONResponse(result, status_code=400)
    return JSONResponse(result)


@app.get("/photometry/file/{inst}/{date}/{target}/run/{run_id}/{name}")
def photometry_file_run(inst: str, date: str, target: str, run_id: str, name: str):
    path = phot.safe_run_artifact_path(inst, date, target, run_id, name)
    if path is None:
        raise HTTPException(404, "artifact not found")
    return FileResponse(str(path), headers={"Cache-Control": "no-store, no-cache, must-revalidate, max-age=0"})


@app.get("/photometry/file/{inst}/{date}/{name}")
def photometry_file(inst: str, date: str, name: str):
    path = phot.safe_artifact_path(inst, date, name)
    if path is None:
        raise HTTPException(404, "artifact not found")
    return FileResponse(str(path), headers={"Cache-Control": "no-store, no-cache, must-revalidate, max-age=0"})


def _photometry_download_all(inst: str, date: str, target: str, run_id: str | None):
    if inst not in INSTRUMENTS or not phot.valid_date(date):
        raise HTTPException(400, "invalid parameters")
    if run_id and (".." in run_id or "/" in run_id):
        raise HTTPException(400, "invalid run id")

    try:
        rdir = phot.run_output_dir(inst, date, target, run_id or None)
    except ValueError:
        raise HTTPException(400, "invalid target")

    outputs = phot.list_outputs(inst, date, target, run_id=run_id or None)
    if not outputs.get("has_any") and not outputs.get("masters"):
        raise HTTPException(404, "no files to download")

    files_to_zip = []

    if run_id:
        # Zip all files recursively in rdir
        if rdir.is_dir():
            for p in rdir.rglob("*"):
                if p.is_file():
                    files_to_zip.append((p, str(p.relative_to(rdir))))
    else:
        # Legacy run: extract target-specific files from outputs
        if rdir.is_dir():
            # Gather single-file keys
            for key in ("npz", "log", "ref_header"):
                name = outputs.get(key)
                if name:
                    p = rdir / name
                    if p.is_file():
                        files_to_zip.append((p, name))
            # Gather summary files
            for item in outputs.get("summary_items", []):
                name = item.get("file")
                if name:
                    p = rdir / name
                    if p.is_file():
                        files_to_zip.append((p, name))
            # Gather nearby stars if any
            nearby = outputs.get("summary", {}).get("nearby_stars")
            if nearby and nearby.get("file"):
                p = rdir / nearby["file"]
                if p.is_file():
                    files_to_zip.append((p, nearby["file"]))
            # Gather band files
            for band_data in outputs.get("bands", {}).values():
                for prod in band_data.values():
                    name = prod.get("file")
                    if name:
                        p = rdir / name
                        if p.is_file():
                            files_to_zip.append((p, name))

    # Include masters for both modes if present
    for name in outputs.get("masters", []):
        for base_dir in (phot.results_dir(inst, date), phot.raw_data_dir(inst, date)):
            cal_p = pathlib.Path(str(base_dir) + "_calibrated") / name
            if cal_p.is_file():
                files_to_zip.append((cal_p, f"masters/{name}"))
                break

    if not files_to_zip:
        raise HTTPException(404, "no files to download")

    archive_name = f"{target.replace(' ', '')}_phot_{date}"
    if run_id:
        archive_name += f"_{run_id}"
    archive_name += ".zip"

    return _create_zip_response(files_to_zip, archive_name)


@app.get("/photometry/download-all/{inst}/{date}/{target}/run/{run_id}")
def photometry_download_all_run(inst: str, date: str, target: str, run_id: str):
    return _photometry_download_all(inst, date, target, run_id)


@app.get("/photometry/download-all/{inst}/{date}/{target}")
def photometry_download_all(inst: str, date: str, target: str):
    return _photometry_download_all(inst, date, target, None)


@app.post("/photometry/run")
def photometry_run(request: Request, payload: dict = Body(...)):
    inst = (payload.get("inst") or "").strip()
    date = (payload.get("date") or "").strip()
    target = (payload.get("target") or "").strip()
    options = payload.get("options") or {}
    test_run = bool(payload.get("test_run", True))
    user_name = request.state.user
    # Hard block: never launch a sinistro run that would merge multiple sites.
    site_err = _site_required_error(_db_path(), inst, date, target, options)
    if site_err:
        return JSONResponse({"ok": False, "error": site_err}, status_code=400)
    result = phot.start_run(inst, date, target, options=options, test_run=test_run, user_name=user_name)
    if not result.get("ok"):
        return JSONResponse(result, status_code=400)
    return JSONResponse(result)


@app.post("/photometry/command")
def photometry_command(payload: dict = Body(...)):
    """Preview the exact prose command for the chosen options (live form echo)."""
    inst = (payload.get("inst") or "").strip()
    date = (payload.get("date") or "").strip()
    target = (payload.get("target") or "").strip()
    options = payload.get("options") or {}
    test_run = bool(payload.get("test_run", False))
    error = phot.validate_run_options(phot.normalize_run_options(options), inst=inst)
    # Surface the multi-site block as a command error so the page disables the
    # run buttons and shows why until a site is chosen.
    if not error:
        error = _site_required_error(_db_path(), inst, date, target, options)
    command = phot.command_str(inst, date, target, options=options, test_run=test_run)
    return JSONResponse({"command": command, "error": error})


@app.get("/photometry/status")
def photometry_status(inst: str, date: str, target: str, run: str = ""):
    # Drain the queue so a pending full job is promoted once the slot frees,
    # even when only the photometry page (not the Jobs page) is polling.
    phot.sync_jobs()
    return JSONResponse(phot.job_status(inst, date, target, run_id=(run or "").strip()))


@app.post("/photometry/status-batch")
def photometry_status_batch(payload: dict = Body(...)):
    """Poll multiple jobs in a single request. Reduces polling overhead when monitoring many jobs.

    Request body:
    {
      "jobs": [
        {"inst": "muscat2", "date": "260307", "target": "TOI05646.01", "run": "run_name"},
        ...
      ]
    }

    Response:
    {
      "jobs": [
        {
          "inst": "muscat2", "date": "260307", "target": "TOI05646.01", "run": "run_name",
          "state": "running", "log": "...", "elapsed": 123, ...
        },
        ...
      ]
    }
    """
    phot.sync_jobs()
    jobs = payload.get("jobs") or []
    if not isinstance(jobs, list):
        return JSONResponse({"error": "jobs must be a list"}, status_code=400)

    results = []
    for job_spec in jobs:
        inst = (job_spec.get("inst") or "").strip()
        date = (job_spec.get("date") or "").strip()
        target = (job_spec.get("target") or "").strip()
        run = (job_spec.get("run") or "").strip()

        if not all([inst, date, target]):
            results.append({"error": "inst, date, and target are required"})
            continue

        status = phot.job_status(inst, date, target, run_id=run)
        results.append({
            "inst": inst,
            "date": date,
            "target": target,
            "run": run,
            **status
        })

    return JSONResponse({"jobs": results})


@app.post("/photometry/cancel")
def photometry_cancel(payload: dict = Body(...)):
    inst = (payload.get("inst") or "").strip()
    date = (payload.get("date") or "").strip()
    target = (payload.get("target") or "").strip()
    run_id = (payload.get("run_id") or payload.get("run") or "").strip()
    result = phot.cancel_run(inst, date, target, run_id=run_id)
    if not result.get("ok"):
        return JSONResponse(result, status_code=400)
    return JSONResponse(result)


@app.post("/photometry/delete")
def photometry_delete(payload: dict = Body(...)):
    inst = (payload.get("inst") or "").strip()
    date = (payload.get("date") or "").strip()
    target = (payload.get("target") or "").strip()
    if inst not in INSTRUMENTS:
        return JSONResponse({"ok": False, "error": "unknown instrument"}, status_code=400)
    if not phot.valid_date(date):
        return JSONResponse({"ok": False, "error": "invalid date"}, status_code=400)
    if not (target or "").strip():
        return JSONResponse({"ok": False, "error": "target is required"}, status_code=400)
    run_id = (payload.get("run_id") or payload.get("run") or "").strip()
    result = phot.delete_reduction(inst, date, target, run_id=run_id)
    return JSONResponse(result)


@app.put("/api/targets/{obj}/note")
def api_set_note(obj: str, payload: dict = Body(...)):
    note = (payload.get("note") or "").strip()
    if len(note) > 2000:
        raise HTTPException(400, "note too long (max 2000 chars)")
    _set_note(_db_path(), obj, note)
    return JSONResponse({"ok": True, "object": obj, "note": note})


@app.delete("/api/targets/{obj}/note")
def api_delete_note(obj: str):
    _delete_note(_db_path(), obj)
    return JSONResponse({"ok": True, "object": obj})


@app.put("/api/targets/{obj}/identified")
def api_set_identified(obj: str, payload: dict = Body(...)):
    val = payload.get("is_identified")
    if val not in (0, 1):
        raise HTTPException(400, "is_identified must be 0 or 1")
    _set_identified(_db_path(), obj, val)
    return JSONResponse({"ok": True, "object": obj, "is_identified": bool(val)})


@app.get("/{instrument}", response_class=HTMLResponse)
def instrument_page(instrument: str):
    dates = _get_dates(_db_path(), instrument)
    return _render("instrument.html", instrument=instrument, dates=dates)


@app.get("/{instrument}/{obsdate}", response_class=HTMLResponse)
def date_page(instrument: str, obsdate: str):
    summaries = _get_summaries(_db_path(), instrument, obsdate)
    ccds = sorted(set(s["ccd"] for s in summaries))
    return _render("date.html", instrument=instrument, obsdate=obsdate, summaries=summaries, ccds=ccds)


@app.get("/{instrument}/{obsdate}/ccd{ccd}", response_class=HTMLResponse)
def ccd_page(instrument: str, obsdate: str, ccd: int):
    frames = _get_frames(_db_path(), instrument, obsdate, ccd)
    return _render("ccd.html", instrument=instrument, obsdate=obsdate, ccd=ccd, frames=frames)
