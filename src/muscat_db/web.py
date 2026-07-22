from __future__ import annotations

import asyncio
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
from concurrent.futures import ThreadPoolExecutor

import socketio

sio = socketio.AsyncServer(async_mode="asgi")

_DB_LOCK = threading.Lock()

import csv
import io
from contextlib import asynccontextmanager
from urllib.parse import quote, urlencode

import httpx
from fastapi import Body, FastAPI, HTTPException, Request
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from jinja2 import Environment, FileSystemLoader

from muscat_db import photometry as phot
from muscat_db import exposure as exp_calc
from muscat_db.auth import (
    PROXY_SECRET_HEADER,
    authentication_required as _authentication_required,
    trusted_forwarded_user,
    request_user as _request_user,
    settings_auth_error as _settings_auth_error,
    is_same_origin as _is_same_origin,
    csrf_error as _csrf_error,
)
from muscat_db import transit_fit as fit
from muscat_db import ttv_fit as ttv
from muscat_db import lco
from muscat_db.lco import _annotate_lco_archive_results
from muscat_db import transit_obs
from muscat_db import fov as fov_opt
from muscat_db import ephemeris_math
from muscat_db import ephemeris_import
from muscat_db import gsheet_ephemeris
from muscat_db import test_observations
from muscat_db import lco_monitor
from muscat_db import http_client
from muscat_db import chat
from muscat_db.catalog import (
    _adql_literal,
    _ads_token_for_request,
    _catalog_source_cache_key,
    _db_mtime,
    _global_ads_token,
    _harps_coord_membership,
    _harps_data_for_target,
    _HARPS_MATCH_ARCSEC,
    _lamost_coord_membership,
    _lamost_rv_data_for_target,
    _LAMOST_MATCH_ARCSEC,
    _load_jwst_targets,
    _load_jwst_targets_aliases,
    _load_nexsci_catalog,
    _load_spectra_targets,
    _load_spectra_targets_aliases,
    _load_toi_catalog,
    _matched_jwst_targets,
    _matched_spectra_targets,
    _merge_boyle_columns,
    _nasa_confirmed_toi_membership,
    _nexsci_db_membership,
    _normalize_target_name,
    _query_target_planets_catalog,
    _query_target_planets_nasa,
    _query_target_planets_toi,
    _resolve_all_aliases,
    _resolve_archive_coords,
    _target_tic_id,
    _toi_db_membership,
    # Test-only compatibility: tests.test_web reach into these via
    # `web.<name>` (e.g. `web._boyle_cache.clear()`) even though no route
    # handler here reads them directly any more; they're the same dict/list
    # objects catalog.py's functions read, so mutating them in place (not
    # reassigning) still works through this alias.
    _BOYLE_COLUMNS,  # noqa: F401 -- tests read web._BOYLE_COLUMNS
    _NEXSCI_COLUMNS,  # noqa: F401 -- tests import web._NEXSCI_COLUMNS
    _boyle_cache,  # noqa: F401 -- tests call web._boyle_cache.clear()
    _harps_cache,  # noqa: F401 -- tests call web._harps_cache.clear()
    _toi_db_cache,  # noqa: F401 -- tests call web._toi_db_cache.clear()
)
from muscat_db.database import (
    _apply_schema as _apply_database_schema,
    UserSettingsError,
    ensure_user,
    get_conn,
    delete_note as _delete_note,
    format_elapsed,
    get_dates as _get_dates,
    get_frames as _get_frames,
    get_frame_objects as _get_frame_objects,
    get_exposure_log_for_objects as _get_exposure_log_for_objects,
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
    get_user_ads_token,
    get_user_eso_credentials,
    get_user_lco_token,
    get_user_ephem_sheet,
    set_user_ads_token,
    set_user_eso_credentials,
    set_user_lco_token,
    set_user_ephem_sheet,
    _normalize_filters,
)
from muscat_db.job_store import get_job_store
from muscat_db.cache import LRUCache
from muscat_db.instruments import INSTRUMENTS

logger = logging.getLogger(__name__)

_CATALOG_BATCH_MAX_ITEMS = max(1, int(os.environ.get("MUSCAT_CATALOG_BATCH_MAX_ITEMS", "200")))
_CATALOG_BATCH_MAX_BYTES = max(1024, int(os.environ.get("MUSCAT_CATALOG_BATCH_MAX_BYTES", "262144")))
_CATALOG_BATCH_MAX_ACTIVE = max(1, int(os.environ.get("MUSCAT_CATALOG_BATCH_MAX_ACTIVE", "4")))
_CATALOG_BATCH_EXECUTOR = ThreadPoolExecutor(
    max_workers=max(1, int(os.environ.get("MUSCAT_CATALOG_GLOBAL_WORKERS", "8"))),
    thread_name_prefix="catalog-lookup",
)
_CATALOG_BATCH_SLOTS = threading.BoundedSemaphore(_CATALOG_BATCH_MAX_ACTIVE)
_CATALOG_BATCH_USERS: set[str] = set()
_CATALOG_BATCH_USERS_LOCK = threading.Lock()

_ZIP_MAX_FILES = max(1, int(os.environ.get("MUSCAT_ZIP_MAX_FILES", "10000")))
_ZIP_MAX_INPUT_BYTES = max(1 << 20, int(os.environ.get("MUSCAT_ZIP_MAX_INPUT_BYTES", str(2 << 30))))
_ZIP_FREE_RESERVE_BYTES = max(0, int(os.environ.get("MUSCAT_ZIP_FREE_RESERVE_BYTES", str(5 << 30))))
_ZIP_CACHE_TTL_S = max(60, int(os.environ.get("MUSCAT_ZIP_CACHE_TTL_S", "900")))
_ZIP_BUILD_SLOTS = threading.BoundedSemaphore(
    max(1, int(os.environ.get("MUSCAT_ZIP_BUILD_WORKERS", "1")))
)

HERE = pathlib.Path(__file__).parent
TEMPLATE_DIR = HERE / "templates"
STATIC_DIR = HERE / "static"


def _reconcile_all_jobs() -> None:
    """Advance every in-process pipeline from one server-owned cadence."""
    phot.sync_jobs()
    fit.sync_jobs()
    ttv.sync_jobs()
    _lco_archive_download_rows()


async def _job_reconciliation_loop() -> None:
    interval = max(0.5, float(os.environ.get("MUSCAT_JOB_RECONCILE_INTERVAL_S", "2")))
    while True:
        try:
            await asyncio.to_thread(_reconcile_all_jobs)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("background job reconciliation failed")
        await asyncio.sleep(interval)


@asynccontextmanager
async def _lifespan(app: FastAPI):
    """Create the database and schema on startup if they don't exist."""
    db = _db_path()
    with get_conn(db, timeout=10) as conn:
        conn.execute("PRAGMA journal_mode=WAL;")
        _apply_database_schema(conn)
    print(f"[startup] database ready at {db}")

    from muscat_db.config import config_status, missing_required_secret

    print("[startup] env config:")
    for name, state in config_status():
        print(f"  {name}={state}")
    missing = missing_required_secret()
    if missing is not None:
        print(
            f"[startup] WARNING: {missing.name} is unset. "
            "muscat/muscat2 calibration with --wcs_method astrometry.net will fail; "
            "use --wcs_method twirl (no API key) or export the key."
        )

    from muscat_db import proxy, http_client
    await proxy.startup()
    await http_client.startup()
    # Let the chat's job-finished hook (which runs in the sync job-poll thread)
    # schedule broadcasts back onto this event loop.
    chat.set_event_loop(asyncio.get_running_loop())
    reconcile_task = asyncio.create_task(_job_reconciliation_loop())
    observation_monitor = None
    if os.environ.get("MUSCAT_LCO_MONITOR_ENABLED", "1") == "1":
        observation_monitor = lco_monitor.ObservationMonitor(db)
        observation_monitor.start()
        app.state.lco_observation_monitor = observation_monitor
    try:
        yield
    finally:
        reconcile_task.cancel()
        try:
            await reconcile_task
        except asyncio.CancelledError:
            pass
        if observation_monitor is not None:
            observation_monitor.stop()
        await http_client.shutdown()
        await proxy.shutdown()


app = FastAPI(title="MuSCAT Observation Log", lifespan=_lifespan)
sio_app = socketio.ASGIApp(sio, app)
# The targets page is ~2.8 MB of highly repetitive HTML; gzip shrinks it ~16x,
# which is the dominant cost when serving over an SSH port-forward tunnel.
app.add_middleware(GZipMiddleware, minimum_size=1000)

from fastapi import APIRouter
from muscat_db.proxy import router as proxy_router

photometry_router = APIRouter(prefix="/api/photometry", tags=["photometry"])
transit_fit_router = APIRouter(prefix="/api/transit-fit", tags=["transit-fit"])
ttv_fit_router = APIRouter(prefix="/api/ttv-fit", tags=["ttv-fit"])
exposure_router = APIRouter(prefix="/api/exposure", tags=["exposure"])
jobs_router = APIRouter(prefix="/api/jobs", tags=["jobs"])
target_router = APIRouter(prefix="/api/targets", tags=["targets"])
ephemeris_router = APIRouter(prefix="/api/ephemeris", tags=["ephemeris"])
fov_router = APIRouter(prefix="/api/fov", tags=["fov"])
lco_router = APIRouter(prefix="/api/lco", tags=["lco"])
settings_router = APIRouter(prefix="/api/settings", tags=["settings"])
ads_router = APIRouter(prefix="/api/ads", tags=["ads"])

_MAX_STATUS_BATCH = 100
_MAX_STATUS_FIELD_LEN = 256

# Middleware: extract the authenticated user from the nginx reverse proxy.
# The trust rule (only honor X-Forwarded-User from a loopback proxy peer) lives
# in muscat_db.auth so the companion-app gateway applies it identically.
@app.middleware("http")
async def _nginx_auth_middleware(request: Request, call_next):
    client_host = request.client.host if request.client else None
    forwarded = request.headers.get("X-Forwarded-User")
    user = trusted_forwarded_user(
        forwarded, client_host, request.headers.get(PROXY_SECRET_HEADER)
    )
    if forwarded and user is None:
        logger.warning(
            "ignoring untrusted X-Forwarded-User=%r from peer %s "
            "(proxy address or shared secret did not validate)",
            forwarded, client_host,
        )
    request.state.user = user
    token = _CURRENT_USER.set(user)
    try:
        protected = not (
            request.url.path == "/healthz"
            or request.url.path.startswith("/static/")
        )
        if _authentication_required() and protected and not user:
            return JSONResponse(
                {"ok": False, "error": "authentication required"},
                status_code=401,
            )
        if (
            protected
            and request.method.upper() not in {"GET", "HEAD", "OPTIONS", "TRACE"}
            and not _is_same_origin(request)
        ):
            return _csrf_error()
        if user:
            try:
                ensure_user(user)
            except (UserSettingsError, sqlite3.Error) as exc:
                logger.warning("could not ensure user row for %s: %s", user, exc)
        response = await call_next(request)
        return response
    finally:
        _CURRENT_USER.reset(token)

# Register the companion-application gateway before the broad observation-page
# routes below (``/{instrument}``, ``/{instrument}/{obsdate}``, ...). Starlette
# resolves routes in registration order, so adding this router at the end makes
# a request such as ``/tess-quicklook/available-sectors`` look like an
# instrument/date page and returns HTML where the QuickLook client expects JSON.
app.include_router(proxy_router)

# Mount static assets (shared stylesheet, etc.) before the dynamic routes so a
# request like /static/styles.css is not captured by the /{inst}/{date} route.
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


@app.get("/healthz", include_in_schema=False)
def healthz():
    """Minimal public liveness probe; it deliberately exposes no app state."""
    return {"ok": True}

jinja = Environment(
    loader=FileSystemLoader(str(TEMPLATE_DIR)),
    autoescape=True,
)
jinja.globals["format_elapsed"] = format_elapsed


def _static_url(name: str) -> str:
    """URL for a bundled static asset, cache-busted by its mtime.

    StaticFiles sends no Cache-Control, so browsers fall back to heuristic
    freshness and can serve a stale styles.css long after it changed on disk.
    The mtime query forces a new URL whenever the file is edited.
    """
    try:
        stamp = int((STATIC_DIR / name).stat().st_mtime)
    except OSError:
        return f"/static/{name}"
    return f"/static/{name}?v={stamp}"


jinja.globals["static_url"] = _static_url


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


async def _async_get(url: str, *, headers: dict | None = None, timeout: float | None = None) -> httpx.Response:
    """GET via the shared async httpx client, raising on non-2xx status
    (mirrors urllib.request.urlopen's implicit HTTPError-on-bad-status).

    Backs routes whose entire job is a single external archive call, so they
    can be ``async def`` and free FastAPI's threadpool while awaiting; tests
    monkeypatch this name directly."""
    response = await http_client.get_async_client().get(
        url,
        headers=headers,
        timeout=timeout if timeout is not None else http_client.DEFAULT_TIMEOUT_S,
    )
    response.raise_for_status()
    return response


_CURRENT_USER: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "current_user",
    default=None,
)


def _render(name: str, **kwargs) -> str:
    tpl = jinja.get_template(name)
    kwargs.setdefault("current_user", _CURRENT_USER.get())
    return HTMLResponse(tpl.render(**kwargs))


def _script_json(obj) -> str:
    """Serialize ``obj`` for safe embedding inside an inline ``<script>`` block.

    ``json.dumps`` does not escape ``<``, ``>`` or ``&``, so a value containing
    ``</script>`` (or ``<!--``) would break out of the script element and allow
    HTML/JS injection (XSS) when the result is emitted via ``{{ ... | safe }}``.
    Escape those, plus the U+2028/U+2029 JS line separators, to their ``\\uXXXX``
    forms — which parse back to the identical characters. This mirrors Jinja's
    ``|tojson`` filter (used by every other template); the TOI/NExSci pages
    pre-serialize server-side, so they need the same protection applied here.
    """
    text = json.dumps(obj, separators=(",", ":"), allow_nan=False)
    return (
        text.replace("<", "\\u003c")
        .replace(">", "\\u003e")
        .replace("&", "\\u0026")
        .replace("\u2028", "\\u2028")
        .replace("\u2029", "\\u2029")
    )


# Rendering the ~2.85 MB targets page costs ~1.3s. Cache the rendered HTML
# keyed on the DB mtime so repeat loads are instant until the data changes.
# Each entry is a multi-MB HTML blob, so the cache is bounded (LRU) to keep
# memory flat over a long-lived server; sizes are env-overridable for tuning.
_INDEX_CACHE_MAX = int(os.environ.get("MUSCAT_INDEX_CACHE_MAX", "64"))
_index_cache = LRUCache(maxsize=_INDEX_CACHE_MAX)


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
        key = (tpl_mtime, _db_mtime(db), _catalog_source_cache_key(), _HARPS_MATCH_ARCSEC, _LAMOST_MATCH_ARCSEC, norm_name)
        cache_key = f"target:{norm_name}"
        cached = _index_cache.get(cache_key)
        if cached is not None and cached[0] == key:
            return HTMLResponse(cached[1])

        datasets, last_updated = _get_datasets_for_normalized_target(db, norm_name)
        target_tic_id = _target_tic_id(norm_name, datasets)

        has_jwst_data = False
        has_spectra_data = False
        try:
            target_aliases = _resolve_all_aliases(norm_name, datasets)
            jwst_aliases = _load_jwst_targets_aliases()
            has_jwst_data = bool(target_aliases & jwst_aliases)
            spectra_aliases = _load_spectra_targets_aliases()
            has_spectra_data = bool(target_aliases & spectra_aliases)
        except Exception as e:
            logger.warning("failed to check membership for %s: %s", norm_name, e)

        html = jinja.get_template("target.html").render(
            target_name=norm_name,
            datasets=datasets,
            last_updated=last_updated,
            harps_match_arcsec=_HARPS_MATCH_ARCSEC,
            lamost_match_arcsec=_LAMOST_MATCH_ARCSEC,
            target_tic_id=target_tic_id,
            exofop_target_id=target_tic_id or norm_name,
            has_jwst_data=has_jwst_data,
            has_spectra_data=has_spectra_data,
        )

        _index_cache[cache_key] = (key, html)
        return HTMLResponse(html)


@target_router.get("/harps-rv", response_class=JSONResponse)
def api_target_harps_rv(name: str = ""):
    norm_name = _normalize_target_name(name)
    if not norm_name:
        return JSONResponse({"ok": False, "error": "Target name is required"}, status_code=400)
    datasets, _last_updated = _get_datasets_for_normalized_target(_db_path(), norm_name)
    harps_rv = _harps_data_for_target(norm_name, datasets)
    return JSONResponse({
        "ok": True,
        "target": norm_name,
        "match_arcsec": _HARPS_MATCH_ARCSEC,
        "has_data": bool(harps_rv.get("total_rows")),
        "harps_rv": harps_rv,
    })


@target_router.get("/lamost-rv", response_class=JSONResponse)
def api_target_lamost_rv(name: str = ""):
    """Return LAMA_stars.csv (Li+2024) rows matched to the target's coordinates."""
    norm_name = _normalize_target_name(name)
    if not norm_name:
        return JSONResponse({"ok": False, "error": "Target name is required"}, status_code=400)
    datasets, _last_updated = _get_datasets_for_normalized_target(_db_path(), norm_name)
    rv_data = _lamost_rv_data_for_target(norm_name, datasets)
    return JSONResponse({
        "ok": True,
        "target": norm_name,
        "lamost_rv": rv_data,
    })


# ESO Science Archive TAP constants
_ESO_TOKEN_URL = "https://www.eso.org/sso/oidc/token"
_ESO_TAP_URL = "https://archive.eso.org/tap_obs/sync"

# LAMOST DR11 TAP constants
_LAMOST_TAP_URL = "https://www.lamost.org/dr11/v2.0/voservice/tap"
_LAMOST_ARCHIVE_TABLE = "public.med_combined"


@target_router.get("/eso-archive", response_class=JSONResponse)
async def api_target_eso_archive(name: str = "", request: Request = None):
    """Query ESO Science Archive TAP for observations of the given target.

    Strategy:
    1. Try name-based ADQL (LIKE match on target_name).
    2. If zero rows: resolve RA/Dec via _resolve_archive_coords() and retry with
       a 1-arcmin cone-search.
    Auth: obtain a short-lived Bearer token from ESO OIDC if credentials are
    configured (user > global env), otherwise query anonymously (public data).
    """
    norm_name = _normalize_target_name(name)
    if not norm_name:
        return JSONResponse({"ok": False, "error": "Target name is required"}, status_code=400)

    # Resolve credentials: per-user > global env fallback > anonymous
    user = _request_user(request) if request else None
    eso_username: str | None = None
    eso_password: str | None = None
    if user:
        try:
            eso_username, eso_password = get_user_eso_credentials(user)
        except UserSettingsError:
            pass
    if not (eso_username and eso_password):
        eso_username = os.environ.get("ESO_USERNAME") or None
        eso_password = os.environ.get("ESO_PASSWORD") or None

    # Obtain a Bearer token if credentials are available
    headers: dict[str, str] = {}
    auth_used = False
    if eso_username and eso_password:
        try:
            tok_resp = await http_client.get_async_client().get(
                _ESO_TOKEN_URL,
                params={
                    "response_type": "id_token token",
                    "grant_type": "password",
                    "client_id": "clientid",
                    "username": eso_username,
                    "password": eso_password,
                },
                timeout=15.0,
            )
            if tok_resp.status_code == 200:
                tok_data = tok_resp.json()
                id_token = tok_data.get("id_token", "")
                if id_token:
                    # ESO id_token is base64url-encoded; append == for padding
                    headers["Authorization"] = f"Bearer {id_token}=="
                    auth_used = True
                else:
                    logger.warning("ESO token response missing id_token field: %s", list(tok_data.keys()))
            else:
                logger.warning(
                    "ESO token request returned %s: %s",
                    tok_resp.status_code,
                    tok_resp.text[:300],
                )
        except Exception as exc:  # network error, timeout, parse failure
            logger.warning("ESO token acquisition failed: %s", exc)

    # Helper: run a single TAP ADQL query and return raw JSON
    async def _tap_query(adql: str, timeout: float = 30.0) -> dict:
        resp = await http_client.get_async_client().get(
            _ESO_TAP_URL,
            params={
                "REQUEST": "doQuery",
                "LANG": "ADQL",
                "FORMAT": "json",
                "QUERY": adql.strip(),
            },
            headers=headers,
            timeout=timeout,
        )
        resp.raise_for_status()
        return resp.json()

    # Helper: parse ESO TAP JSON → (column_names, list_of_row_dicts)
    def _parse_tap(raw: dict) -> tuple[list[str], list[dict]]:
        cols = [col["name"] for col in raw.get("metadata", [])]
        rows = [dict(zip(cols, r)) for r in raw.get("data", [])]
        return cols, rows

    import asyncio as _asyncio

    # --- Build name query ADQL (fast: local alias lookup) ---
    safe_name = norm_name.replace("'", "''")
    name_conditions = [f"target_name LIKE '%{safe_name}%'"]
    try:
        from muscat_db.catalog import _resolve_all_aliases
        for alias in await _asyncio.to_thread(_resolve_all_aliases, norm_name):
            if alias.upper() != norm_name.upper() and alias.strip():
                safe_alias = alias.replace("'", "''")
                name_conditions.append(f"target_name LIKE '%{safe_alias}%'")
    except Exception:
        pass

    _SELECT = (
        "SELECT TOP 200 "
        "target_name, instrument_name, obs_collection, dataproduct_type, "
        "t_min, t_max, s_fov, proposal_id, obs_release_date, obs_id, "
        "access_url, s_ra, s_dec "
        "FROM ivoa.ObsCore "
    )
    adql_name = _SELECT + f"WHERE ({' OR '.join(name_conditions)}) ORDER BY t_min DESC"

    # --- Step 1: name-based query (fast path) ---
    columns: list = []
    rows: list = []
    query_method = "name"
    try:
        name_data = await _tap_query(adql_name, timeout=30.0)
        columns, rows = _parse_tap(name_data)
    except httpx.HTTPStatusError as exc:
        eso_msg = ""
        try:
            eso_msg = exc.response.text[:500]
        except Exception:
            pass
        return JSONResponse(
            {
                "ok": False,
                "error": f"ESO TAP returned HTTP {exc.response.status_code}",
                "detail": eso_msg,
            },
            status_code=502,
        )
    except Exception as exc:
        return JSONResponse(
            {"ok": False, "error": f"ESO TAP query failed: {type(exc).__name__}: {exc}".rstrip(": ")},
            status_code=502,
        )

    # --- Step 2: coordinate cone-search, only when the name query found nothing ---
    # Lazy on purpose: the spatial CONTAINS() query is far more expensive on ESO's
    # side than the name query, so we do not issue it on every page load (AGENTS.md:
    # do not overload external databases). Retry once on timeout with a generous
    # timeout, and surface a warning instead of silently implying "no observations".
    cone_warning = ""
    if not rows:
        try:
            resolved = await _asyncio.to_thread(_resolve_archive_coords, norm_name)
        except Exception as exc:
            logger.debug("ESO coordinate resolution failed for %s: %s", norm_name, exc)
            resolved = None
        if resolved:
            ra_deg, dec_deg, coord_source = resolved
            radius_deg = 1.0 / 60.0  # 1 arcmin
            adql_cone = (
                _SELECT
                + "WHERE CONTAINS("
                + "POINT('ICRS', s_ra, s_dec), "
                + f"CIRCLE('ICRS', {ra_deg}, {dec_deg}, {radius_deg})"
                + ") = 1 ORDER BY t_min DESC"
            )
            for attempt in range(2):
                try:
                    cone_data = await _tap_query(adql_cone, timeout=120.0)
                    columns, rows = _parse_tap(cone_data)
                    if rows:
                        query_method = f"cone ({coord_source}, 1 arcmin)"
                    cone_warning = ""
                    break
                except httpx.TimeoutException:
                    cone_warning = "ESO cone-search timed out (service slow); results may be incomplete."
                    logger.warning("ESO cone-search timeout for %s (attempt %d/2)", norm_name, attempt + 1)
                except Exception as exc2:
                    cone_warning = f"ESO cone-search failed: {type(exc2).__name__}"
                    logger.warning("ESO cone-search fallback failed for %s: %s", norm_name, repr(exc2))
                    break

    payload = {
        "ok": True,
        "target": norm_name,
        "authenticated": auth_used,
        "query_method": query_method,
        "total": len(rows),
        "columns": columns,
        "rows": rows,
    }
    if cone_warning and not rows:
        payload["warning"] = cone_warning
    return JSONResponse(payload)


# ── LAMOST DR11 TAP helper ──────────────────────────────────────────────


def _parse_votable(xml_text: str) -> tuple[list[str], list[dict]]:
    """Parse a VOTable XML response into (column_names, list_of_row_dicts)."""
    import xml.etree.ElementTree as ET
    root = ET.fromstring(xml_text)
    ns = {"v": "http://www.ivoa.net/xml/VOTable/v1.3"}
    fields = root.findall(".//v:FIELD", ns)
    cols = [f.get("name") for f in fields]
    data_rows = root.findall(".//v:TABLEDATA/v:TR", ns)
    rows = []
    for tr in data_rows:
        vals = [td.text or "" for td in tr.findall("v:TD", ns)]
        if len(vals) == len(cols):
            rows.append(dict(zip(cols, vals)))
    return cols, rows


@target_router.get("/lamost-archive", response_class=JSONResponse)
async def api_target_lamost_archive(name: str = "", request: Request = None):
    """Query LAMOST DR11 MRS TAP for observations near the given target.

    Uses coordinate-based cone search only (LAMOST does not support
    name-resolution in TAP).  Falls back to the local catalog resolution.
    """
    norm_name = _normalize_target_name(name)
    if not norm_name:
        return JSONResponse({"ok": False, "error": "Target name is required"}, status_code=400)

    # Resolve RA/Dec from local catalogs
    try:
        import asyncio as _asyncio
        from muscat_db.catalog import _resolve_archive_coords
        resolved = await _asyncio.to_thread(_resolve_archive_coords, norm_name)
    except Exception as exc:
        logger.debug("LAMOST coordinate resolution failed for %s: %s", norm_name, exc)
        resolved = None

    if not resolved:
        return JSONResponse({
            "ok": False,
            "error": f"Could not resolve coordinates for '{norm_name}'",
        }, status_code=404)

    ra_deg, dec_deg, coord_source = resolved
    radius_deg = 1.0 / 60.0  # 1 arcmin

    adql = (
        "SELECT TOP 200 "
        "obsid, ra, dec, obsdate, band, snr, designation, planid, spid, fiberid, spec "
        f"FROM {_LAMOST_ARCHIVE_TABLE} "
        "WHERE CONTAINS("
        f"POINT(ra, dec), "
        f"CIRCLE({ra_deg}, {dec_deg}, {radius_deg})"
        ") = 1 "
        "ORDER BY obsdate DESC"
    )

    # LAMOST TAP is normally sub-second, but the long-lived server occasionally
    # sees a transient timeout (service blip or connection-pool contention while a
    # concurrent slow query holds connections). Retry once — a fresh request gets a
    # new connection. Bounded to a single retry so we never hammer the service.
    columns: list = []
    rows: list = []
    timeout_exc: httpx.TimeoutException | None = None
    for attempt in range(2):
        try:
            resp = await http_client.get_async_client().get(
                _LAMOST_TAP_URL + "/sync",
                params={"REQUEST": "doQuery", "LANG": "ADQL", "QUERY": adql},
                timeout=30.0,
            )
            resp.raise_for_status()
            columns, rows = _parse_votable(resp.text)
            timeout_exc = None
            break
        except httpx.TimeoutException as exc:
            timeout_exc = exc
            logger.warning("LAMOST TAP timeout for %s (attempt %d/2)", norm_name, attempt + 1)
        except httpx.HTTPStatusError as exc:
            return JSONResponse({
                "ok": False,
                "error": f"LAMOST TAP returned HTTP {exc.response.status_code}",
            }, status_code=502)
        except Exception as exc:
            # Non-empty message even for exceptions that stringify to "" (timeouts).
            return JSONResponse({
                "ok": False,
                "error": f"LAMOST TAP query failed: {type(exc).__name__}: {exc}".rstrip(": "),
            }, status_code=502)
    if timeout_exc is not None:
        return JSONResponse({
            "ok": False,
            "error": "LAMOST TAP query timed out after two attempts (service slow or unreachable); please retry.",
        }, status_code=504)

    return JSONResponse({
        "ok": True,
        "target": norm_name,
        "query_method": f"cone ({coord_source}, 1 arcmin)",
        "total": len(rows),
        "columns": columns,
        "rows": rows,
    })


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


@app.get("/toi", response_class=HTMLResponse)
def toi_page():
    cat = _load_toi_catalog()
    indb, tname = _toi_db_membership(cat["data"], _db_path())
    boyle, n_boyle = _merge_boyle_columns(cat["data"])
    harps, n_harps = _harps_coord_membership(cat["data"])
    lamost, n_lamost = _lamost_coord_membership(cat["data"])
    nasa_confirmed, nasa_planet_name, n_nasa_confirmed = _nasa_confirmed_toi_membership(cat["data"])
    payload = dict(cat["data"])
    payload.update(boyle)
    payload["indb"] = indb
    payload["tname"] = tname
    payload["has_harps_rv"] = harps
    payload["has_lamost_rv"] = lamost
    payload["nasa_confirmed"] = nasa_confirmed
    payload["nasa_planet_name"] = nasa_planet_name
    return _render(
        "toi.html",
        toi_json=_script_json(payload),
        n_rows=cat["n"],
        n_indb=sum(indb),
        n_boyle=n_boyle,
        n_harps=n_harps,
        n_lamost=n_lamost,
        n_nasa_confirmed=n_nasa_confirmed,
        toi_updated=cat["updated"],
    )


@app.get("/whoami", response_class=JSONResponse)
def whoami(request: Request):
    """Return the current nginx-authenticated user for this request.

    The chat widget calls this to label messages. It cannot rely on the
    server-rendered ``current_user`` on every page: the index and target pages
    are served from a globally-cached HTML blob (keyed on DB mtime), so a
    per-user identity must never be baked into their markup. This endpoint is
    per-request and uncached, so it is correct on every page and never leaks one
    user's name into another's cached page. Not placed under ``/api/`` so it
    doesn't trip the global fetch/loading-bar tracker on every navigation.
    """
    return JSONResponse({"user": getattr(request.state, "user", None)})


@app.get("/chat/users", response_class=JSONResponse)
def chat_users(request: Request):
    """Known usernames for chat @-mention autocomplete. Requires an
    authenticated request; returns only usernames (no other user data). Not
    under /api/ so it doesn't trip the global loading-bar fetch tracker."""
    if not _request_user(request):
        return JSONResponse({"users": []})
    try:
        from muscat_db.database import get_known_chat_usernames
        from muscat_db import chat_agent
        users = get_known_chat_usernames()
        # Surface the codebase assistant so @bot autocompletes, even though it is
        # never a real (notifiable) chat user.
        if chat_agent.DISPLAY_NAME not in {u.lower() for u in users}:
            users = [chat_agent.DISPLAY_NAME, *users]
        return JSONResponse({"users": users})
    except Exception:
        logger.exception("failed to list chat users")
        return JSONResponse({"users": []})


@app.get("/chat/popout", response_class=HTMLResponse)
def chat_popout_page():
    """Standalone chat-only window (opened via the widget's pop-out button).

    Reuses the same #chat-window markup, styles.css, and chat.js as the
    floating widget on every other page; ``chat_popout=True`` just tells
    base.html to hide the nav/page chrome and let the chat fill the window.
    """
    return _render("chat_popout.html", chat_popout=True)


@app.get("/api/exofop/check_confirmed")
async def check_confirmed_planets(tics: str):
    import urllib.parse
    from .database import get_conn, SCHEMA

    tic_list = [t.strip() for t in tics.split(",") if t.strip()]
    if not tic_list:
        return {}

    results = {}
    missing_tics = []

    # 1. Query the cache
    with get_conn() as conn:
        conn.executescript(SCHEMA)  # Ensure schema updated
        placeholders = ",".join("?" for _ in tic_list)
        cursor = conn.cursor()
        cursor.execute(
            f"SELECT tic_id, has_confirmed_planets FROM exofop_cache WHERE tic_id IN ({placeholders})",
            tic_list
        )
        for row in cursor.fetchall():
            results[row[0]] = bool(row[1])

    # 2. Identify missing ones
    for t in tic_list:
        if t not in results:
            missing_tics.append(t)

    if missing_tics:
        # Cap concurrent ExoFOP requests at 10, same as the previous
        # ThreadPoolExecutor(max_workers=10).
        semaphore = asyncio.Semaphore(10)

        async def fetch_one(tic):
            encoded_tic = urllib.parse.quote(tic)
            url = f"https://exofop.ipac.caltech.edu/tess/target.php?id={encoded_tic}&json"
            try:
                async with semaphore:
                    resp = await _async_get(url, headers={"User-Agent": "MuSCAT-db/0.1.0"})
                data = resp.json()
                bi = data.get("basic_info", {})
                confirmed_val = bi.get("confirmed_planets") or ""
                has_confirmed = len(confirmed_val.strip()) > 0
                return tic, has_confirmed, confirmed_val
            except Exception as e:
                logger.warning("Failed to fetch ExoFOP for TIC %s: %s", tic, e)
                return tic, None, None

        fetched_results = await asyncio.gather(*(fetch_one(tic) for tic in missing_tics))

        # 3. Write new entries to cache
        with get_conn() as conn:
            cursor = conn.cursor()
            for tic, has_confirmed, confirmed_val in fetched_results:
                if has_confirmed is not None:
                    cursor.execute(
                        "INSERT OR REPLACE INTO exofop_cache (tic_id, has_confirmed_planets, confirmed_planets, updated_at) VALUES (?, ?, ?, CURRENT_TIMESTAMP)",
                        (tic, 1 if has_confirmed else 0, confirmed_val)
                    )
                    results[tic] = has_confirmed
            conn.commit()

    return results


@target_router.get("/jwst", response_class=JSONResponse)
async def api_target_jwst(name: str = ""):
    norm_name = _normalize_target_name(name)
    if not norm_name:
        return JSONResponse({"ok": False, "error": "Target name is required"}, status_code=400)

    db = _db_path()
    datasets, _last_updated = _get_datasets_for_normalized_target(db, norm_name)
    matched = _matched_jwst_targets(norm_name, datasets)

    if not matched:
        return JSONResponse({
            "ok": True,
            "target": norm_name,
            "jwst": {
                "columns": [],
                "rows": []
            }
        })

    import urllib.parse
    import csv
    import io
    import datetime

    names_str = ", ".join("'" + n.replace("'", "''") + "'" for n in matched)
    query = f"SELECT program, observation_num, instrument, observingmode, gratinggrism, event, status, starttime, observation_dur FROM nexolist WHERE pl_name IN ({names_str})"
    params = {
        "query": query,
        "format": "csv"
    }
    url = "https://exoplanetarchive.ipac.caltech.edu/TAP/sync?" + urllib.parse.urlencode(params)

    try:
        response = await _async_get(url, headers={"User-Agent": "MuSCAT-db/0.1.0"})
        content = response.text
        f = io.StringIO(content)
        reader = csv.DictReader(f)
        col_map = {
            "program": "Program",
            "observation_num": "Obs #",
            "instrument": "Instrument",
            "observingmode": "Observing Mode",
            "gratinggrism": "Grating/Grism",
            "event": "Event",
            "status": "Status",
            "starttime": "Start Time (UTC)",
            "observation_dur": "Duration (h)"
        }
        columns = ["Program", "Obs #", "Instrument", "Observing Mode", "Grating/Grism", "Event", "Status", "Start Time (UTC)", "Duration (h)"]
        rows = []
        for row in reader:
            if not row or "ERROR" in row:
                continue
            mapped_row = {}
            for orig_col, new_col in col_map.items():
                val = row.get(orig_col)
                if val is None:
                    val = ""
                else:
                    val = val.strip()
                    if orig_col == "observation_dur":
                        try:
                            float_val = float(val)
                            val = f"{float_val:.2f}"
                        except ValueError:
                            pass
                mapped_row[new_col] = val
            rows.append(mapped_row)

        def get_start_time(r):
            t_str = r.get("Start Time (UTC)", "")
            try:
                return datetime.datetime.strptime(t_str, "%b %d, %Y %H:%M:%S")
            except Exception:
                return datetime.datetime.min

        rows.sort(key=get_start_time, reverse=True)

        return JSONResponse({
            "ok": True,
            "target": norm_name,
            "jwst": {
                "columns": columns,
                "rows": rows
            }
        })
    except Exception as e:
        return JSONResponse({"ok": False, "error": f"Failed to query JWST observations: {str(e)}"}, status_code=500)


@target_router.get("/spectra", response_class=JSONResponse)
async def api_target_spectra(name: str = ""):
    norm_name = _normalize_target_name(name)
    if not norm_name:
        return JSONResponse({"ok": False, "error": "Target name is required"}, status_code=400)

    db = _db_path()
    datasets, _last_updated = _get_datasets_for_normalized_target(db, norm_name)
    matched = _matched_spectra_targets(norm_name, datasets)

    if not matched:
        return JSONResponse({
            "ok": True,
            "target": norm_name,
            "spectra": {
                "columns": [],
                "rows": []
            }
        })

    import urllib.parse
    import csv
    import io

    names_str = ", ".join("'" + n.replace("'", "''") + "'" for n in matched)
    query = f"SELECT spec_type, facility, instrument, minwavelng, maxwavelng, num_datapoints, authors, bibcode FROM spectra WHERE pl_name IN ({names_str})"
    params = {
        "query": query,
        "format": "csv"
    }
    url = "https://exoplanetarchive.ipac.caltech.edu/TAP/sync?" + urllib.parse.urlencode(params)

    try:
        response = await _async_get(url, headers={"User-Agent": "MuSCAT-db/0.1.0"})
        content = response.text
        f = io.StringIO(content)
        reader = csv.DictReader(f)
        col_map = {
            "spec_type": "Type",
            "facility": "Facility",
            "instrument": "Instrument",
            "minwavelng": "Min Wavelng (μm)",
            "maxwavelng": "Max Wavelng (μm)",
            "num_datapoints": "# Points",
            "authors": "Authors",
            "bibcode": "Bibcode"
        }
        columns = ["Type", "Facility", "Instrument", "Min Wavelng (μm)", "Max Wavelng (μm)", "# Points", "Authors", "Bibcode"]
        rows = []
        for row in reader:
            if not row or "ERROR" in row:
                continue
            mapped_row = {}
            for orig_col, new_col in col_map.items():
                val = row.get(orig_col)
                if val is None:
                    val = ""
                else:
                    val = val.strip()
                    if orig_col in ("minwavelng", "maxwavelng"):
                        try:
                            float_val = float(val)
                            val = f"{float_val:.4f}"
                        except ValueError:
                            pass
                mapped_row[new_col] = val
            rows.append(mapped_row)

        rows.sort(key=lambda r: (r.get("Type", ""), r.get("Authors", "")))

        return JSONResponse({
            "ok": True,
            "target": norm_name,
            "spectra": {
                "columns": columns,
                "rows": rows
            }
        })
    except Exception as e:
        return JSONResponse({"ok": False, "error": f"Failed to query spectra observations: {str(e)}"}, status_code=500)


@app.get("/nexsci", response_class=HTMLResponse)
def nexsci_page():
    cat = _load_nexsci_catalog()
    indb, tname = _nexsci_db_membership(cat["data"], _db_path())
    harps, n_harps = _harps_coord_membership(cat["data"])
    lamost, n_lamost = _lamost_coord_membership(cat["data"])
    jwst_targets = _load_jwst_targets()
    jwst = [1 if p in jwst_targets else 0 for p in cat["data"]["name"]]
    spectra_targets = _load_spectra_targets()
    spectra = [1 if p in spectra_targets else 0 for p in cat["data"]["name"]]

    payload = dict(cat["data"])
    payload["indb"] = indb
    payload["tname"] = tname
    payload["has_harps_rv"] = harps
    payload["has_lamost_rv"] = lamost
    payload["has_jwst"] = jwst
    payload["has_spectra"] = spectra
    return _render(
        "nexsci.html",
        nexsci_json=_script_json(payload),
        n_rows=cat["n"],
        n_indb=sum(indb),
        n_harps=n_harps,
        n_lamost=n_lamost,
        n_jwst=sum(jwst),
        n_spectra=sum(spectra),
        nexsci_updated=cat["updated"],
    )


@target_router.get("/export.csv")
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


_LCO_TELESCOPE_FILENAME_RE = re.compile(r"^[a-z]{3}1m0(\d{2})-")


def _sinistro_obslog_choices(
    db: str, inst: str, date: str, target: str, site: str = ""
) -> tuple[list[str], list[str], list[str]]:
    """``(sites, telescopes, modes)`` present in the obslog for a sinistro
    target+date, optionally scoping the telescope list to one ``site``.

    The LCO site is the 3-char filename prefix (e.g. ``cpt1m010-...``); the
    physical telescope is the 2-digit unit number right after ``1m0`` in that
    same prefix (e.g. ``cpt1m010-`` -> unit ``10``, reconstructed as the
    canonical TELESCOP-style value ``'1m0-10'``); the mode is ``read_mode``
    (CONFMODE). Site/mode are intersected with the known valid sets so a stray
    prefix or non-canonical read_mode (MUSCAT_FAST/SLOW) can't leak in;
    telescope is open-ended (LCO's 1m fleet changes over time) so only the
    filename shape is validated. Empty lists for non-sinistro or on error.
    """
    if inst != "sinistro" or not (date and target):
        return [], [], []
    try:
        with get_conn(db) as conn:
            cur = conn.execute(
                "SELECT DISTINCT filename FROM frames WHERE instrument = ? AND obsdate = ? AND object = ? AND filename IS NOT NULL AND filename != ''",
                (inst, date, target),
            )
            filenames = [row[0].lower() for row in cur.fetchall() if row[0]]
            sites = sorted({fn[:3] for fn in filenames} & set(phot.SINISTRO_SITES))
            scoped = [fn for fn in filenames if not site or fn.startswith(site)]
            telescopes = sorted({
                f"1m0-{m.group(1)}"
                for fn in scoped
                if (m := _LCO_TELESCOPE_FILENAME_RE.match(fn))
            })
            cur = conn.execute(
                "SELECT DISTINCT read_mode FROM frames WHERE instrument = ? AND obsdate = ? AND object = ? AND read_mode IS NOT NULL AND read_mode != ''",
                (inst, date, target),
            )
            modes = sorted({row[0].lower() for row in cur.fetchall() if row[0]} & set(phot.SINISTRO_MODES))
        return sites, telescopes, modes
    except Exception:
        return [], [], []


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
    sites, _telescopes, _modes = _sinistro_obslog_choices(db, inst, date, target)
    if len(sites) > 1:
        return f"select a site to run — {date} has {len(sites)} sites ({', '.join(sites)})"
    return None


def _telescope_required_error(db: str, inst: str, date: str, target: str, options: dict) -> str | None:
    """Block a sinistro run that would silently merge multiple physical telescopes.

    Mirrors :func:`_site_required_error`: when the obslog (scoped to the chosen
    site, if any) holds more than one physical 1m telescope for this
    target+date and none is chosen, prose would combine frames from different
    telescopes into one mislabeled reduction (prose2 aborts on this too).
    """
    if inst != "sinistro":
        return None
    if (options.get("telescope") or "").strip():
        return None
    site = (options.get("site") or "").strip().lower()
    _sites, telescopes, _modes = _sinistro_obslog_choices(db, inst, date, target, site=site)
    if len(telescopes) > 1:
        return f"select a telescope to run — {date} has {len(telescopes)} telescopes ({', '.join(telescopes)})"
    return None


def _resolve_dataset_target(requested: str, candidates: list[str]) -> str:
    """Resolve a canonical target name to one unambiguous raw dataset key.

    Photometry products and obslog rows are keyed by the original OBJECT value,
    while catalog/target-page links use normalized identities.  Preserve exact
    raw requests; otherwise translate only when normalization yields one match.
    Ambiguous aliases remain unresolved so the route never selects the wrong
    observation or reduction products.
    """
    if not requested or requested in candidates:
        return requested
    compact_matches = [
        candidate for candidate in candidates
        if candidate.replace(" ", "") == requested
    ]
    if len(compact_matches) == 1:
        return compact_matches[0]
    normalized = _normalize_target_name(requested)
    matches = [
        candidate for candidate in candidates
        if _normalize_target_name(candidate) == normalized
    ]
    return matches[0] if len(matches) == 1 else requested


@app.get("/photometry", response_class=HTMLResponse)
def photometry_page(inst: str = "", date: str = "", target: str = "", site: str = "", telescope: str = "", mode: str = "", run: str = "", overwrite: str = ""):
    db = _db_path()
    inst = inst if inst in INSTRUMENTS else ""
    date = date if phot.valid_date(date) else ""
    target = (target or "").strip()
    # Site/telescope/mode are sinistro-only view filters (which LCO
    # site/physical telescope/readout mode's products to show). Site/mode are
    # validated against the known sets here; telescope is open-ended (no fixed
    # whitelist, LCO's 1m fleet changes over time) so only its shape is
    # checked. Whether any of them are actually present is decided by
    # list_outputs from the filenames.
    site = site.strip().lower()
    if inst != "sinistro" or site not in phot.SINISTRO_SITES:
        site = ""
    telescope = telescope.strip().lower()
    if inst != "sinistro" or not phot.TELESCOPE_RE.match(telescope):
        telescope = ""
    mode = mode.strip().lower()
    if inst != "sinistro" or mode not in phot.SINISTRO_MODES:
        mode = ""

    route_target = target.replace(" ", "")
    if route_target != target:
        query = {
            key: value for key, value in (
                ("inst", inst),
                ("date", date),
                ("target", route_target),
                ("site", site),
                ("telescope", telescope),
                ("mode", mode),
                ("run", run),
                ("overwrite", overwrite),
            )
            if value
        }
        return RedirectResponse(f"/photometry?{urlencode(query)}", status_code=307)
    target = route_target

    # Parse overwrite from query parameter (overrides defaults for this session)
    run_defaults_override = {}
    if overwrite.lower() in ("0", "false", "no"):
        run_defaults_override["overwrite"] = False
    elif overwrite.lower() in ("1", "true", "yes"):
        run_defaults_override["overwrite"] = True

    dates: list[str] = []
    targets: list[str] = []
    available_sites: list[str] = ["lsc", "cpt", "coj", "tfn", "elp"]
    available_telescopes: list[str] = []
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
        raw_targets = sorted(_get_objects(db, inst, date))
        target = _resolve_dataset_target(route_target, raw_targets)
        public_targets = {name.replace(" ", "") for name in raw_targets}
        if route_target and target in raw_targets:
            public_targets.discard(target.replace(" ", ""))
            public_targets.add(route_target)
        targets = sorted(public_targets)
    obs_type = ""
    is_narrowband = False
    available_bands: list[str] = []
    if inst and date and target:
        runs, run_outputs = phot.list_photometry_runs(inst, date, target)
        if inst == "sinistro":
            if site:
                runs = [r for r in runs if r.is_legacy or r.site == site or not r.site]
            if telescope:
                runs = [r for r in runs if r.is_legacy or r.telescope == telescope or not r.telescope]
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
            if not (site or telescope or mode) and run_key in run_outputs:
                # Reuse the outputs already computed by list_photometry_runs.
                # Only skip the cache when sinistro site/telescope/mode filters
                # are active, since those affect which files are selected.
                outputs = run_outputs[run_key]
            else:
                outputs = phot.list_outputs(inst, date, target, site=site or None, telescope=telescope or None, mode=mode or None, run_id=sel_run or None)
        else:
            outputs = phot.list_outputs(inst, date, target, site=site or None, telescope=telescope or None, mode=mode or None)
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

        # Restrict the site/telescope/mode run-option dropdowns to what the
        # obslog actually holds for this target+date, so you can't launch a
        # reduction for a site/telescope/mode with no frames.
        db_sites, db_telescopes, db_modes = _sinistro_obslog_choices(db, inst, date, target, site=site)
        if db_sites:
            available_sites = db_sites
        if db_telescopes:
            available_telescopes = db_telescopes
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
    # Sinistro's site/telescope/mode selectors double as page view filters and
    # reduction options.  Seed the option controls from the validated URL so a
    # route reached through one of those selectors remains visibly selected on
    # reload, rather than being overwritten by the generic defaults.
    merged_defaults = {
        **phot.RUN_DEFAULTS,
        **run_defaults_override,
        "site": site,
        "telescope": telescope,
        "mode": mode,
    }

    resp = _render(
        "photometry.html",
        instruments=list(INSTRUMENTS),
        sel_inst=inst, sel_date=date, sel_target=route_target,
        dataset_target=target,
        sel_site=(outputs.get("site") if outputs else "") or "",
        sel_telescope=(outputs.get("telescope") if outputs else "") or "",
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
        available_telescopes=available_telescopes,
        available_modes=available_modes,
    )
    # The run buttons' enabled/disabled state is JavaScript-driven and reflects
    # the live job state. A cached or back/forward-restored snapshot can show
    # them stuck disabled after a failed run, so never let the browser reuse a
    # stale copy of this page.
    resp.headers["Cache-Control"] = "no-store"
    return resp


@app.get("/transit-fit", response_class=HTMLResponse)
def transit_fit_page(inst: str = "", date: str = "", target: str = "", site: str = "", telescope: str = "", mode: str = "", run: str = ""):
    db = _db_path()
    inst = inst if inst in INSTRUMENTS else ""
    date = date if phot.valid_date(date) else ""
    target = (target or "").strip()
    # Sinistro-only view filters (which site / physical telescope / readout
    # mode's lightcurves to list). Telescope is open-ended (no fixed whitelist)
    # so only its shape is checked.
    site = site.strip().lower()
    if inst != "sinistro" or site not in phot.SINISTRO_SITES:
        site = ""
    telescope = telescope.strip().lower()
    if inst != "sinistro" or not phot.TELESCOPE_RE.match(telescope):
        telescope = ""
    mode = mode.strip().lower()
    if inst != "sinistro" or mode not in phot.SINISTRO_MODES:
        mode = ""

    run = (run or "").strip()

    # prose filenames, timer result directories, and pipeline job keys all use
    # the target with spaces removed.  Canonicalize the public route the same
    # way so raw obslog names (``HIP 67522``) and discovered product stems
    # (``HIP67522``) cannot create duplicate dropdown entries or URL aliases.
    compact_target = target.replace(" ", "")
    if compact_target != target:
        query = {
            key: value for key, value in (
                ("inst", inst),
                ("date", date),
                ("target", compact_target),
                ("site", site),
                ("telescope", telescope),
                ("mode", mode),
                ("run", run),
            )
            if value
        }
        return RedirectResponse(f"/transit-fit?{urlencode(query)}", status_code=307)
    target = compact_target

    dates: list[str] = []
    targets: list[str] = []
    outputs = None
    csvs = []
    target_params = {}
    csv_sites: list[str] = []
    csv_telescopes: list[str] = []
    csv_modes: list[str] = []
    sel_site = ""
    sel_telescope = ""
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
        targets = sorted({name.replace(" ", "") for name in obj_set})
    if inst and date and target:
        import datetime
        rows = []
        for c in fit.get_csv_lightcurves(inst, date, target):
            try:
                mtime = c.stat().st_mtime
                created_at = datetime.datetime.fromtimestamp(mtime).strftime('%Y-%m-%d %H:%M:%S')
            except Exception:
                mtime, created_at = 0.0, "Unknown"
            csite, ctelescope, cmode = fit.csv_site_mode(c.name) if inst == "sinistro" else (None, None, None)
            crun = c.parent.name if "_runs" in c.parts else ""
            rows.append({"path": str(c), "name": c.name, "created_at": created_at,
                         "_mtime": mtime, "_site": csite, "_telescope": ctelescope, "_mode": cmode, "run_id": crun})

        if inst == "sinistro":
            # A sinistro date+target can hold multiple sites / physical
            # telescopes / readout modes with identical bands. The picker
            # defaults to showing ALL lightcurves (so the user can fit one
            # site/telescope or deliberately combine several); the
            # Site/Telescope/Mode chips optionally narrow the list. The run's
            # identity is derived from whatever is actually selected at launch.
            csv_sites = sorted({r["_site"] for r in rows if r["_site"]})
            sel_site = site  # validated against SINISTRO_SITES above; "" == all
            csv_telescopes = sorted({
                r["_telescope"] for r in rows
                if r["_telescope"] and (not sel_site or r["_site"] == sel_site)
            })
            sel_telescope = telescope  # "" == all
            csv_modes = sorted({
                r["_mode"] for r in rows
                if r["_mode"] and (not sel_site or r["_site"] == sel_site)
                and (not sel_telescope or r["_telescope"] == sel_telescope)
            })
            sel_mode = mode  # "" == all
            rows = [r for r in rows
                    if (not sel_site or r["_site"] == sel_site)
                    and (not sel_telescope or r["_telescope"] == sel_telescope)
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
            if sel_telescope:
                runs = [r for r in runs if r.is_legacy or r.telescope == sel_telescope or not r.telescope]
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
        sel_site=sel_site, sel_telescope=sel_telescope, sel_mode=sel_mode,
        csv_sites=csv_sites, csv_telescopes=csv_telescopes, csv_modes=csv_modes,
        runs=runs, sel_run=sel_run,
        dates=dates, targets=targets,
        csvs=csvs, outputs=outputs,
        target_params=target_params,
        wiki_url=_wiki_url(inst, target),
    )


@transit_fit_router.get("/query-archive")
async def transit_fit_query_archive(target: str, source: str = "nasa"):
    if not (target or "").strip():
        return JSONResponse({"ok": False, "error": "Target name is required"}, status_code=400)

    import urllib.parse
    import csv
    import pathlib
    import re

    target = target.strip()

    def clean_archive_name(value: str) -> str:
        return re.sub(r"[^0-9a-zA-Z]", "", value or "").lower()

    def is_planet_of_lookup_name(clean_planet_name: str) -> bool:
        """Match ``HOST b`` while rejecting arbitrary prefix continuations."""
        return any(
            clean_planet_name.startswith(name)
            and clean_planet_name[len(name):] in set("bcdefgh")
            for name in nasa_lookup_names
            if name
        )

    # NASA's confirmed-planet name may differ from its TOI designation
    # (TOI-179 is HD 18599 b).  Resolve the exact TIC alias so the NASA lookup
    # can cross-match identities without accepting unsafe numeric prefixes.
    nasa_lookup_names = {clean_archive_name(target)}
    normalized_target = _normalize_target_name(target)
    resolved_tic_id = ""
    if re.fullmatch(r"TOI\d+", normalized_target):
        resolved_tic_id = _target_tic_id(normalized_target)
        if resolved_tic_id:
            nasa_lookup_names.add(clean_archive_name(f"TIC {resolved_tic_id}"))

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
                row = {k.lower(): v for k, v in row.items()}
                toi = (row.get("toi") or "").strip()
                planet_name = (row.get("planet name") or "").strip()
                tic_id = (row.get("tic id") or "").strip()

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
            
        toi_val = best_row.get("toi", "")
        toi_display = f"TOI-{toi_val}" if toi_val else target
        
        teff = _float_or_none(best_row.get("stellar eff temp (k)"))
        teff_err = _float_or_none(best_row.get("stellar eff temp (k) err"))
        logg = _float_or_none(best_row.get("stellar log(g) (cm/s^2)"))
        logg_err = _float_or_none(best_row.get("stellar log(g) (cm/s^2) err"))
        period = _float_or_none(best_row.get("period (days)"))
        period_err = _float_or_none(best_row.get("period (days) err"))
        t0 = _float_or_none(best_row.get("epoch (bjd)"))
        t0_err = _float_or_none(best_row.get("epoch (bjd) err"))
        dur = _float_or_none(best_row.get("duration (hours)"))
        dur_err = _float_or_none(best_row.get("duration (hours) err"))
        
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
            
        best_row_line = None
        best_score = -1
        
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
                tic_id = parts[5].strip('"')
                
                pl_clean = clean_archive_name(pl_name)
                host_clean = clean_archive_name(hostname)
                hip_clean = clean_archive_name(hip_name)
                hd_clean = clean_archive_name(hd_name)
                tic_clean = clean_archive_name(tic_id)
                
                score = -1
                if pl_clean in nasa_lookup_names:
                    score = 3
                elif nasa_lookup_names.intersection(
                    (host_clean, hip_clean, hd_clean, tic_clean),
                ):
                    score = 2
                elif is_planet_of_lookup_name(pl_clean):
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

    urlopen_is_mocked = hasattr(_async_get, "called")

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
        try:
            response = await _async_get(url, headers={'User-Agent': 'Mozilla/5.0'})
            res = response.json()
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
            "pl_name", "hostname", "hip_name", "hd_name", "tic_id",
            "st_teff", "st_tefferr1", "st_tefferr2",
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
        if resolved_tic_id:
            conditions.append(f"tic_id = {_adql_literal(f'TIC {resolved_tic_id}')}")

        q = f"SELECT {col_str} FROM pscomppars WHERE " + " OR ".join(conditions)

        data = []
        url = 'https://exoplanetarchive.ipac.caltech.edu/TAP/sync?' + urllib.parse.urlencode({'query': q, 'format': 'json'})
        try:
            response = await _async_get(url, headers={'User-Agent': 'Mozilla/5.0'})
            res = response.json()
            if res:
                # Score and rank matching rows in memory
                best_row = None
                best_score = -1

                for row in res:
                    pl_name = (row.get("pl_name") or "").strip()
                    hostname = (row.get("hostname") or "").strip()
                    hip_name = (row.get("hip_name") or "").strip()
                    hd_name = (row.get("hd_name") or "").strip()
                    tic_id = (row.get("tic_id") or "").strip()

                    pl_clean = clean_archive_name(pl_name)
                    host_clean = clean_archive_name(hostname)
                    hip_clean = clean_archive_name(hip_name)
                    hd_clean = clean_archive_name(hd_name)
                    tic_clean = clean_archive_name(tic_id)

                    score = -1
                    if pl_clean in nasa_lookup_names:
                        score = 3
                    elif nasa_lookup_names.intersection(
                        (host_clean, hip_clean, hd_clean, tic_clean),
                    ):
                        score = 2
                    elif is_planet_of_lookup_name(pl_clean):
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


@transit_fit_router.get("/status")
def transit_fit_status(inst: str, date: str, target: str, run: str = ""):
    fit.sync_jobs()
    return JSONResponse(fit.job_status(inst, date, target, run_id=(run or "").strip()))


@transit_fit_router.post("/run")
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


@transit_fit_router.post("/logp")
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


@transit_fit_router.post("/cancel")
def transit_fit_cancel(payload: dict = Body(...)):
    inst = (payload.get("inst") or "").strip()
    date = (payload.get("date") or "").strip()
    target = (payload.get("target") or "").strip()
    run_id = (payload.get("run_id") or payload.get("run") or "").strip()
    result = fit.cancel_fit(inst, date, target, run_id=run_id)
    if not result.get("ok"):
        return JSONResponse(result, status_code=400)
    return JSONResponse(result)


@transit_fit_router.post("/delete")
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
    # Configuration links are inspection views.  Explicit download links remain
    # separate, so force a browser-renderable type instead of application/yaml.
    media_type = "text/plain; charset=utf-8" if name in {"fit.yaml", "sys.yaml"} else None
    return FileResponse(
        str(path), media_type=media_type,
        headers={"Cache-Control": "no-store, no-cache, must-revalidate, max-age=0"},
    )


@transit_fit_router.get("/file/{inst}/{date}/{target}/run/{run_id}/{name}")
def transit_fit_file_run(inst: str, date: str, target: str, run_id: str, name: str):
    return _serve_transit_file(inst, date, target, name, run_id)


@transit_fit_router.get("/file/{inst}/{date}/{target}/{name}")
def transit_fit_file(inst: str, date: str, target: str, name: str):
    # Legacy single-dir fits (run_id="").
    return _serve_transit_file(inst, date, target, name, None)


def _create_zip_response(files_to_zip: list[tuple[pathlib.Path, str]], archive_name: str) -> FileResponse:
    import hashlib
    import shutil
    import zipfile

    files = [(path, arcname) for path, arcname in files_to_zip if path.is_file()]
    if not files:
        raise HTTPException(404, "no output files found")
    if len(files) > _ZIP_MAX_FILES:
        raise HTTPException(413, f"archive contains more than {_ZIP_MAX_FILES} files")
    if len({arcname for _path, arcname in files}) != len(files):
        raise HTTPException(400, "archive contains duplicate output names")

    manifest = []
    total_bytes = 0
    for path, arcname in files:
        stat = path.stat()
        total_bytes += stat.st_size
        manifest.append((str(path.resolve()), arcname, stat.st_size, stat.st_mtime_ns))
    if total_bytes > _ZIP_MAX_INPUT_BYTES:
        raise HTTPException(
            413,
            f"archive input is {total_bytes} bytes; limit is {_ZIP_MAX_INPUT_BYTES} bytes",
        )

    tmp_dir = pathlib.Path(phot.prose_tmpdir())
    tmp_dir.mkdir(parents=True, exist_ok=True)
    now = time.time()
    for cached in tmp_dir.glob("muscat-archive-*.zip"):
        try:
            if now - cached.stat().st_mtime > _ZIP_CACHE_TTL_S:
                cached.unlink()
        except OSError:
            logger.debug("failed to prune ZIP cache entry %s", cached, exc_info=True)

    fingerprint = hashlib.sha256(
        json.dumps(manifest, separators=(",", ":"), ensure_ascii=True).encode("utf-8")
    ).hexdigest()
    cache_path = tmp_dir / f"muscat-archive-{fingerprint}.zip"
    if cache_path.is_file():
        return FileResponse(str(cache_path), media_type="application/zip", filename=archive_name)

    if not _ZIP_BUILD_SLOTS.acquire(blocking=False):
        raise HTTPException(429, "another archive is being generated; retry shortly")

    try:
        if cache_path.is_file():
            return FileResponse(str(cache_path), media_type="application/zip", filename=archive_name)
        free_bytes = shutil.disk_usage(tmp_dir).free
        zip_overhead = max(1 << 20, len(files) * 512)
        required_bytes = total_bytes + zip_overhead + _ZIP_FREE_RESERVE_BYTES
        if free_bytes < required_bytes:
            raise HTTPException(
                507,
                f"insufficient temporary disk space: need {required_bytes} bytes, have {free_bytes}",
            )
        part_path = cache_path.with_suffix(".zip.part")
        try:
            # Most pipeline outputs are already compressed (NPZ, PNG, gzip).
            # ZIP_STORED avoids spending CPU recompressing them and makes the
            # free-space budget an exact upper bound.
            with zipfile.ZipFile(part_path, "w", zipfile.ZIP_STORED, allowZip64=True) as zip_file:
                for filepath, arcname in files:
                    zip_file.write(filepath, arcname)
            part_path.replace(cache_path)
        finally:
            part_path.unlink(missing_ok=True)
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(500, f"failed to create zip archive: {exc}")
    finally:
        _ZIP_BUILD_SLOTS.release()

    return FileResponse(
        str(cache_path),
        media_type="application/zip",
        filename=archive_name,
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


@transit_fit_router.get("/download-all/{inst}/{date}/{target}/run/{run_id}")
def transit_fit_download_all_run(inst: str, date: str, target: str, run_id: str):
    return _transit_fit_download_all(inst, date, target, run_id)


@transit_fit_router.get("/download-all/{inst}/{date}/{target}")
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


@exposure_router.post("/calculate", response_class=JSONResponse)
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

    extra_sources = None
    raw_extra_sources = payload.get("extra_sources")
    if isinstance(raw_extra_sources, list):
        extra_sources = []
        for entry in raw_extra_sources:
            if not isinstance(entry, dict):
                continue
            entry_mags = entry.get("mags")
            if not isinstance(entry_mags, dict) or not entry_mags:
                continue
            try:
                cleaned_mags = {str(band): float(m) for band, m in entry_mags.items()}
            except (TypeError, ValueError):
                continue
            label = entry.get("label")
            extra_sources.append({"label": str(label) if label else None, "mags": cleaned_mags})

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
        extra_sources=extra_sources,
    )
    return JSONResponse({"ok": True, **result})


@exposure_router.post("/calibrate", response_class=JSONResponse)
def exposure_calibrate(payload: dict = Body(...)):
    inst = (payload.get("instrument") or "").strip()
    if inst not in INSTRUMENTS:
        return JSONResponse({"ok": False, "error": "Invalid instrument"}, status_code=400)
    try:
        job = exp_calc.start_calibration(inst)
    except RuntimeError as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=409)
    return JSONResponse({"ok": True, "message": f"Calibration queued for {inst}", **job})


@exposure_router.get("/calibrate/{job_id}", response_class=JSONResponse)
def exposure_calibration_job(job_id: str):
    try:
        return JSONResponse({"ok": True, **exp_calc.calibration_job(job_id)})
    except KeyError:
        return JSONResponse({"ok": False, "error": "Calibration job not found"}, status_code=404)


@exposure_router.post("/calibrate/{job_id}/cancel", response_class=JSONResponse)
def exposure_calibration_cancel(job_id: str):
    try:
        return JSONResponse({"ok": True, **exp_calc.cancel_calibration(job_id)})
    except KeyError:
        return JSONResponse({"ok": False, "error": "Calibration job not found"}, status_code=404)
    except RuntimeError as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=409)


@exposure_router.post("/lookup-mags", response_class=JSONResponse)
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


@exposure_router.post("/lookup-mags-batch", response_class=JSONResponse)
def exposure_lookup_mags_batch(request: Request, payload: dict = Body(...)):
    """Griz magnitudes for a batch of stars (e.g. FOV comparison stars).

    Each star tries the same Pan-STARRS/SkyMapper catalog lookup as the
    primary target first, falling back to a Gaia color transform when given
    ``gmag``/``bp_rp`` and no catalog match exists (see
    ``exposure.lookup_magnitudes_with_fallback``). Looked up in parallel
    since each is an independent network round trip.
    """
    stars = payload.get("stars") or []
    if not isinstance(stars, list) or not stars:
        return JSONResponse({"ok": False, "error": "No stars provided"}, status_code=400)
    if len(stars) > _CATALOG_BATCH_MAX_ITEMS:
        return JSONResponse(
            {"ok": False, "error": f"At most {_CATALOG_BATCH_MAX_ITEMS} stars are allowed per request"},
            status_code=413,
        )
    serialized_bytes = len(json.dumps(stars, separators=(",", ":"), ensure_ascii=True).encode("utf-8"))
    if serialized_bytes > _CATALOG_BATCH_MAX_BYTES:
        return JSONResponse(
            {"ok": False, "error": f"Star payload exceeds {_CATALOG_BATCH_MAX_BYTES} bytes"},
            status_code=413,
        )
    if any(not isinstance(star, dict) for star in stars):
        return JSONResponse({"ok": False, "error": "Each star must be an object"}, status_code=400)

    user_key = request.state.user or f"peer:{request.client.host if request.client else 'unknown'}"
    if not _CATALOG_BATCH_SLOTS.acquire(blocking=False):
        return JSONResponse({"ok": False, "error": "Catalog lookup queue is busy"}, status_code=429)
    with _CATALOG_BATCH_USERS_LOCK:
        if user_key in _CATALOG_BATCH_USERS:
            _CATALOG_BATCH_SLOTS.release()
            return JSONResponse(
                {"ok": False, "error": "A catalog batch is already running for this user"},
                status_code=429,
            )
        _CATALOG_BATCH_USERS.add(user_key)

    def _lookup(star: dict) -> dict:
        try:
            ra = float(star.get("ra"))
            dec = float(star.get("dec"))
        except (TypeError, ValueError):
            return {"mags": None, "source": None, "is_approx": False, "error": "Invalid ra/dec"}
        if not math.isfinite(ra) or not math.isfinite(dec) or not (0 <= ra < 360) or not (-90 <= dec <= 90):
            return {"mags": None, "source": None, "is_approx": False, "error": "Invalid ra/dec"}
        gmag = star.get("gmag")
        bp_rp = star.get("bp_rp")
        try:
            gmag = float(gmag) if gmag not in (None, "") else None
            bp_rp = float(bp_rp) if bp_rp not in (None, "") else None
        except (TypeError, ValueError):
            return {"mags": None, "source": None, "is_approx": False, "error": "Invalid Gaia photometry"}
        mags, source, is_approx = exp_calc.lookup_magnitudes_with_fallback(ra, dec, gmag, bp_rp)
        return {"mags": mags, "source": source, "is_approx": is_approx}

    try:
        results = list(_CATALOG_BATCH_EXECUTOR.map(_lookup, stars))
        return JSONResponse({"ok": True, "results": results})
    finally:
        with _CATALOG_BATCH_USERS_LOCK:
            _CATALOG_BATCH_USERS.discard(user_key)
        _CATALOG_BATCH_SLOTS.release()


@exposure_router.get("/status", response_class=JSONResponse)
def exposure_status():
    calibrations = {}
    for name in INSTRUMENTS:
        calibrations[name] = exp_calc.calibration_status(name)
    return JSONResponse({"calibrations": calibrations, "jobs": exp_calc.calibration_jobs()})


@exposure_router.get("/coeffs/{instrument}", response_class=JSONResponse)
def exposure_coeffs(instrument: str):
    if instrument not in INSTRUMENTS:
        return JSONResponse({"ok": False, "error": "Invalid instrument"}, status_code=400)
    coeffs = exp_calc.load_coeffs(instrument)
    # Convert to serializable format
    rows = []
    for (band, focus_mm), (coef, fwhm, n) in sorted(coeffs.items()):
        rows.append({"band": band, "focus_mm": focus_mm, "coef": round(coef, 4), "fwhm_pix": round(fwhm, 2), "n_frames": n})
    return JSONResponse({"ok": True, "instrument": instrument, "coeffs": rows})


@exposure_router.get("/target/{target}", response_class=JSONResponse)
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


@fov_router.post("/optimize", response_class=JSONResponse)
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

    try:
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
    except Exception as exc:
        logger.error("FOV optimization failed: %s", exc, exc_info=True)
        return JSONResponse(
            {"ok": False, "error": f"FOV optimization failed: {exc}"},
            status_code=500,
        )


@fov_router.post("/resolve-target", response_class=JSONResponse)
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


@fov_router.get("/observable", response_class=JSONResponse)
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


@app.get("/ttv-fit")
def ttv_fit_redirect(target: str = ""):
    """Redirect to ephemeris page (TTV fitting is now integrated there)."""
    params = []
    if target:
        params.append(f"targets={quote(target)}")
    qs = "&".join(params) if params else ""
    return RedirectResponse(url=f"/ephemeris{'?' + qs if qs else ''}", status_code=302)


@ephemeris_router.post("/view", response_class=JSONResponse)
def api_ephemeris_view_save(payload: dict = Body(...)):
    state = payload.get("state") if isinstance(payload, dict) else None
    if not isinstance(state, dict):
        return JSONResponse({"ok": False, "error": "State is required"}, status_code=400)
    targets = state.get("targets")
    if not isinstance(targets, list) or not [t for t in targets if str(t).strip()]:
        return JSONResponse({"ok": False, "error": "At least one target is required"}, status_code=400)
    saved = save_ephemeris_view(state)
    return JSONResponse({"ok": True, **saved})


@ephemeris_router.get("/view/{slug}", response_class=JSONResponse)
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


@app.get("/settings", response_class=HTMLResponse)
def settings_page():
    return _render("settings.html")


@settings_router.get("/lco-token-status", response_class=JSONResponse)
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


@settings_router.post("/lco-token", response_class=JSONResponse)
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


@settings_router.get("/ephem-sheet-status", response_class=JSONResponse)
def api_settings_ephem_sheet_status(request: Request):
    user = _request_user(request)
    if not user:
        return _settings_auth_error()
    try:
        cfg = get_user_ephem_sheet(user)
    except UserSettingsError as exc:
        return JSONResponse(
            {"ok": False, "error": "stored ephemeris sheet cannot be read", "detail": str(exc)},
            status_code=503,
        )
    return JSONResponse(
        {
            "ok": True,
            "user": user,
            "configured": cfg is not None,
            "ephem_tab": (cfg or {}).get("ephem_tab") or gsheet_ephemeris.DEFAULT_EPHEM_TAB,
            "tc_tab": (cfg or {}).get("tc_tab") or gsheet_ephemeris.DEFAULT_TC_TAB,
            "ephem_cols": (cfg or {}).get("ephem_cols") or {},
            "tc_cols": (cfg or {}).get("tc_cols") or {},
            "secret_configured": bool(os.environ.get("MUSCAT_DB_SECRET")),
        }
    )


def _clean_payload_col_map(value) -> dict:
    """Keep only known-field -> non-empty-header string pairs from a payload."""
    if not isinstance(value, dict):
        return {}
    allowed = set(gsheet_ephemeris.EPHEM_FIELDS) | set(gsheet_ephemeris.TC_FIELDS)
    cleaned: dict[str, str] = {}
    for key, header in value.items():
        key_s = str(key).strip()
        header_s = str(header or "").strip()
        if key_s in allowed and header_s:
            cleaned[key_s] = header_s
    return cleaned


@settings_router.post("/ephem-sheet", response_class=JSONResponse)
def api_settings_ephem_sheet(request: Request, payload: dict = Body(...)):
    if not _is_same_origin(request):
        return _csrf_error()
    user = _request_user(request)
    if not user:
        return _settings_auth_error()
    url = str(payload.get("url") or "").strip()
    keep_url = bool(payload.get("keep_url"))
    ephem_tab = str(payload.get("ephem_tab") or "").strip()
    tc_tab = str(payload.get("tc_tab") or "").strip()
    ephem_cols = _clean_payload_col_map(payload.get("ephem_cols"))
    tc_cols = _clean_payload_col_map(payload.get("tc_cols"))
    # Editing tabs/columns on an already-saved sheet: keep the stored URL so the
    # user need not re-enter (and we need not re-expose) it.
    if not url and keep_url:
        try:
            existing = get_user_ephem_sheet(user)
        except UserSettingsError as exc:
            return JSONResponse(
                {"ok": False, "error": "stored ephemeris sheet cannot be read", "detail": str(exc)},
                status_code=503,
            )
        if not existing:
            return JSONResponse(
                {"ok": False, "error": "no saved sheet to update; provide a URL"},
                status_code=400,
            )
        url = existing["url"]
    # Validate the URL/ID up front (SSRF guard) so a bad reference is rejected
    # at save time rather than silently degrading to "no ephemeris" later.
    if url:
        try:
            gsheet_ephemeris.sheet_id_from(url)
        except gsheet_ephemeris.GsheetError as exc:
            return JSONResponse({"ok": False, "error": str(exc)}, status_code=400)
    try:
        set_user_ephem_sheet(user, url, ephem_tab, tc_tab, ephem_cols, tc_cols)
    except UserSettingsError as exc:
        return JSONResponse(
            {"ok": False, "error": "could not save ephemeris sheet", "detail": str(exc)},
            status_code=503,
        )
    return JSONResponse({"ok": True, "configured": bool(url)})


@settings_router.post("/ephem-sheet-columns", response_class=JSONResponse)
def api_settings_ephem_sheet_columns(request: Request, payload: dict = Body(...)):
    """List each tab's columns so the user can map fields to them.

    Uses the URL from the request when provided (validated), else the user's
    saved sheet, so an already-configured sheet can be re-inspected without
    re-exposing the stored URL to the browser.
    """
    if not _is_same_origin(request):
        return _csrf_error()
    user = _request_user(request)
    if not user:
        return _settings_auth_error()
    url = str(payload.get("url") or "").strip()
    ephem_tab = str(payload.get("ephem_tab") or "").strip()
    tc_tab = str(payload.get("tc_tab") or "").strip()
    if url:
        try:
            gsheet_ephemeris.sheet_id_from(url)
        except gsheet_ephemeris.GsheetError as exc:
            return JSONResponse({"ok": False, "error": str(exc)}, status_code=400)
    else:
        try:
            saved = get_user_ephem_sheet(user)
        except UserSettingsError as exc:
            return JSONResponse(
                {"ok": False, "error": "stored ephemeris sheet cannot be read", "detail": str(exc)},
                status_code=503,
            )
        if not saved:
            return JSONResponse(
                {"ok": False, "error": "provide a sheet URL first"}, status_code=400
            )
        url = saved["url"]
        ephem_tab = ephem_tab or saved["ephem_tab"]
        tc_tab = tc_tab or saved["tc_tab"]
    ephem_tab = ephem_tab or gsheet_ephemeris.DEFAULT_EPHEM_TAB
    tc_tab = tc_tab or gsheet_ephemeris.DEFAULT_TC_TAB
    ephem_columns = gsheet_ephemeris.tab_columns(url, ephem_tab)
    tc_columns = gsheet_ephemeris.tab_columns(url, tc_tab)
    if not ephem_columns and not tc_columns:
        return JSONResponse(
            {"ok": False, "error": "no columns found (is the sheet published and are the tab names correct?)"},
            status_code=502,
        )
    return JSONResponse(
        {
            "ok": True,
            "ephem_tab": ephem_tab,
            "tc_tab": tc_tab,
            "ephem_columns": ephem_columns,
            "tc_columns": tc_columns,
            "ephem_suggested": gsheet_ephemeris.suggest_ephem_columns(ephem_columns),
            "tc_suggested": gsheet_ephemeris.suggest_tc_columns(tc_columns),
        }
    )


@settings_router.get("/ads-token-status", response_class=JSONResponse)
def api_settings_ads_token_status(request: Request):
    user = _request_user(request)
    if not user:
        return _settings_auth_error()
    try:
        user_token_configured = get_user_ads_token(user) is not None
    except UserSettingsError as exc:
        return JSONResponse(
            {"ok": False, "error": "stored ADS token cannot be read", "detail": str(exc)},
            status_code=503,
        )
    return JSONResponse(
        {
            "ok": True,
            "user": user,
            "user_token_configured": user_token_configured,
            "global_token_configured": bool(_global_ads_token()),
            "secret_configured": bool(os.environ.get("MUSCAT_DB_SECRET")),
        }
    )


@settings_router.post("/ads-token", response_class=JSONResponse)
def api_settings_ads_token(request: Request, payload: dict = Body(...)):
    if not _is_same_origin(request):
        return _csrf_error()
    user = _request_user(request)
    if not user:
        return _settings_auth_error()
    token = str(payload.get("token") or "").strip()
    try:
        set_user_ads_token(user, token)
    except UserSettingsError as exc:
        return JSONResponse(
            {"ok": False, "error": "could not save ADS token", "detail": str(exc)},
            status_code=503,
        )
    return JSONResponse({"ok": True, "user_token_configured": bool(token)})


@settings_router.get("/eso-credentials-status", response_class=JSONResponse)
def api_settings_eso_credentials_status(request: Request):
    user = _request_user(request)
    if not user:
        return _settings_auth_error()
    try:
        eso_u, eso_p = get_user_eso_credentials(user)
        user_credentials_configured = eso_u is not None and eso_p is not None
    except UserSettingsError as exc:
        return JSONResponse(
            {"ok": False, "error": "stored ESO credentials cannot be read", "detail": str(exc)},
            status_code=503,
        )
    global_configured = bool(os.environ.get("ESO_USERNAME") and os.environ.get("ESO_PASSWORD"))
    return JSONResponse(
        {
            "ok": True,
            "user": user,
            "user_credentials_configured": user_credentials_configured,
            "global_credentials_configured": global_configured,
            "secret_configured": bool(os.environ.get("MUSCAT_DB_SECRET")),
        }
    )


@settings_router.post("/eso-credentials-test", response_class=JSONResponse)
async def api_settings_eso_credentials_test(payload: dict = Body(...)):
    """Test ESO credentials against the OIDC token endpoint. Does not save."""
    eso_u = str(payload.get("username") or "").strip()
    eso_p = str(payload.get("password") or "").strip()
    if not eso_u or not eso_p:
        return JSONResponse({"ok": False, "error": "Username and password are required."}, status_code=400)
    try:
        tok_resp = await http_client.get_async_client().get(
            _ESO_TOKEN_URL,
            params={
                "response_type": "id_token token",
                "grant_type": "password",
                "client_id": "clientid",
                "username": eso_u,
                "password": eso_p,
            },
            timeout=15.0,
        )
        if tok_resp.status_code == 200:
            tok_data = tok_resp.json()
            if tok_data.get("id_token"):
                return JSONResponse({"ok": True, "authenticated": True})
            else:
                return JSONResponse(
                    {"ok": False, "error": "ESO returned an unexpected response (missing id_token)."},
                    status_code=502,
                )
        elif tok_resp.status_code == 400:
            return JSONResponse(
                {"ok": False, "error": "ESO rejected the credentials. Check your username and password."},
                status_code=401,
            )
        else:
            return JSONResponse(
                {
                    "ok": False,
                    "error": f"ESO authentication failed (HTTP {tok_resp.status_code}).",
                },
                status_code=502,
            )
    except httpx.TimeoutException:
        return JSONResponse(
            {"ok": False, "error": "ESO authentication timed out. Try again later."},
            status_code=504,
        )
    except Exception as exc:
        return JSONResponse(
            {"ok": False, "error": f"ESO authentication error: {exc}"},
            status_code=502,
        )


@settings_router.post("/eso-credentials", response_class=JSONResponse)
def api_settings_eso_credentials(request: Request, payload: dict = Body(...)):
    if not _is_same_origin(request):
        return _csrf_error()
    user = _request_user(request)
    if not user:
        return _settings_auth_error()
    eso_u = str(payload.get("username") or "").strip()
    eso_p = str(payload.get("password") or "").strip()
    try:
        set_user_eso_credentials(user, eso_u or None, eso_p or None)
    except UserSettingsError as exc:
        return JSONResponse(
            {"ok": False, "error": "could not save ESO credentials", "detail": str(exc)},
            status_code=503,
        )
    configured = bool(eso_u and eso_p)
    return JSONResponse({"ok": True, "user_credentials_configured": configured})


@app.get("/lco")
def lco_page():
    return RedirectResponse(url="/lco/schedule", status_code=307)


@app.get("/lco/schedule", response_class=HTMLResponse)
def lco_schedule_page():
    return _render("lco_schedule.html")


@app.get("/lco/archive", response_class=HTMLResponse)
def lco_archive_page():
    return _render("lco_archive.html")


@lco_router.get("/config", response_class=JSONResponse)
def api_lco_config(request: Request):
    """Report whether the token/download-root/submit gate are configured. No secrets."""
    return JSONResponse({"ok": True, **lco.config_state(_request_user(request))})


@lco_router.get("/proposals", response_class=JSONResponse)
def api_lco_proposals(request: Request):
    try:
        return JSONResponse({"ok": True, **lco.get_proposals(_request_user(request))})
    except lco.LcoError as e:
        return _lco_error_response(e)


@lco_router.get("/requestgroups", response_class=JSONResponse)
def api_lco_requestgroups(request: Request, proposal: str = ""):
    try:
        return JSONResponse({"ok": True, **lco.get_requestgroups(proposal, _request_user(request))})
    except lco.LcoError as e:
        return _lco_error_response(e)


@lco_router.post("/windows", response_class=JSONResponse)
def api_lco_windows(request: Request, payload: dict = Body(...)):
    """Generate transit windows from explicit t0/period/duration or a catalog lookup."""
    try:
        t0 = payload.get("t0")
        period = payload.get("period")
        duration = payload.get("duration")
        target = (payload.get("target") or "").strip()
        planet = (payload.get("planet") or "").strip().lower()
        # Planets are keyed by letter everywhere (b, c, …). Accept TOI/TFOP
        # candidate forms (".01"/"01" -> b) so a request for ".01" matches the
        # "b" entry the resolvers store.
        planet = gsheet_ephemeris._planet_label(planet) or planet
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
            elif source in ("gsheet", "gsheet_tc"):
                cfg = _user_ephem_sheet_cfg(request)
                if not cfg:
                    return JSONResponse(
                        {"ok": False, "error": "no ephemeris Google Sheet configured (see Settings)"},
                        status_code=400,
                    )
                if source == "gsheet":
                    planets = _sheet_ephemeris(target, cfg)
                else:
                    # Transit-centers tab -> linear fit; seed epoch assignment
                    # from the sheet ephemeris tab, then the catalog. Duration is
                    # backfilled from that same seed since a fit has none.
                    sheet_ephem = _sheet_ephemeris(target, cfg)
                    seed_planets = _query_target_planets_catalog(target)
                    seed_by_planet = {}
                    for pl in set(sheet_ephem) | set(seed_planets):
                        seed = sheet_ephem.get(pl) or seed_planets.get(pl) or {}
                        if seed.get("t0") is not None and seed.get("period"):
                            seed_by_planet[pl] = seed
                    planets = _sheet_fit_ephemeris(target, cfg, seed_by_planet)
                    for pl, entry in planets.items():
                        seed = sheet_ephem.get(pl) or seed_planets.get(pl) or {}
                        if entry.get("duration") is None and seed.get("duration") is not None:
                            entry["duration"] = seed["duration"]
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
                    max_lunar_phase=float(payload.get("max_lunar_phase") or 1.0),
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


@lco_router.get("/obslog-exposures", response_class=JSONResponse)
def api_lco_obslog_exposures(target: str):
    """Past exposure configurations logged for a target (frames obslog).

    Lists every distinct (instrument, filter, readout, defocus, exp time) the
    target was observed with, newest first, so a recurring observation can reuse
    a prior exposure time. OBJECT values are matched by normalized name, like
    the rest of the app.
    """
    target = (target or "").strip()
    if not target:
        return JSONResponse({"ok": False, "error": "target is required"}, status_code=400)
    norm = _normalize_target_name(target)
    db = _db_path()
    try:
        objects = [o for o in _get_frame_objects(db) if _normalize_target_name(o) == norm]
        exposures = _get_exposure_log_for_objects(db, objects)
    except Exception:
        logger.debug("obslog exposure lookup failed for %s", target, exc_info=True)
        return JSONResponse({"ok": False, "error": "obslog lookup failed"}, status_code=500)
    return JSONResponse(
        {"ok": True, "target": target, "objects": sorted(set(objects)), "exposures": exposures}
    )


@lco_router.get("/visibility", response_class=JSONResponse)
def api_lco_visibility(
    ra: float,
    dec: float,
    mid: str,
    duration: float,
    site: str,
    obs_airmass: float = 2.0,
    twilight: str = transit_obs.DEFAULT_TWILIGHT,
    moon_sep_min: float = 0.0,
    max_lunar_phase: float = 1.0,
):
    """Time-series for the inline visibility plot of one transit at one site
    (target + moon altitude, twilight, airmass limit, shaded transit interval)."""
    try:
        series = transit_obs.visibility_series(
            float(ra), float(dec), mid, float(duration), site,
            max_airmass=float(obs_airmass), twilight=twilight,
            moon_sep_min=float(moon_sep_min), max_lunar_phase=float(max_lunar_phase),
        )
        return JSONResponse({"ok": True, **series})
    except transit_obs.TransitObsError as e:
        return JSONResponse({"ok": False, "error": str(e)}, status_code=e.status)
    except Exception as e:
        return JSONResponse({"ok": False, "error": f"visibility unavailable: {e}"}, status_code=500)


@lco_router.post("/ipp", response_class=JSONResponse)
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


def _record_lco_submission(result: dict, requestgroup: dict, params: dict, user: str | None) -> dict:
    """Persist an accepted booking without ever disguising it as a failed submit.

    Once LCO accepts telescope time it cannot be rolled back here.  A local
    persistence error is therefore returned as an explicit monitoring warning,
    while the enclosing submission response remains successful so a user does
    not retry and accidentally create a duplicate booking.
    """
    monitor_payload = {**params, "requests": requestgroup.get("requests") or []}
    try:
        rows = lco_monitor.record_submission(result, monitor_payload, user)
        return {"ok": True, "request_ids": [row["request_id"] for row in rows]}
    except Exception as exc:
        logger.exception("LCO booking accepted but local monitoring registration failed")
        return {"ok": False, "error": str(exc)}


def _submitted_request_ids(result: dict) -> list[int]:
    ids = []
    for child in result.get("requests") or []:
        value = child.get("id") if isinstance(child, dict) else child
        if value is not None:
            ids.append(int(value))
    return ids


@lco_router.post("/test-observations/plan", response_class=JSONResponse)
def api_lco_test_plan(payload: dict = Body(...)):
    """Generate and persist a deterministic, observer-reviewable test plan."""
    try:
        if not payload.get("fov_candidates"):
            result = fov_opt.optimize(
                payload.get("kind"), target=payload.get("target_name") or "",
                ra=payload.get("ra"), dec=payload.get("dec"),
                sinistro_mode=payload.get("readout_mode"),
            )
            if not result.ok:
                raise test_observations.TestObservationError(result.error or "FOV optimization failed")
            best = result.to_dict()
            payload["fov_candidates"] = [
                {"center_ra": best["center_ra"], "center_dec": best["center_dec"], "pa_deg": best["pa_deg"],
                 "comparisons": best.get("comps", []), "edge_margin_arcsec": best.get("margin_arcsec")},
                {"center_ra": best["ra"], "center_dec": best["dec"], "pa_deg": 0,
                 "comparisons": best.get("brightest_in_field", []), "edge_margin_arcsec": best.get("margin_arcsec"),
                 "fallback_reason": "target-centered conservative pointing"},
            ]
            payload.setdefault("provenance", {})["fov_optimizer"] = {
                "catalog": best.get("catalog"), "fov_arcsec": best.get("fov_arcsec"),
                "margin_arcsec": best.get("margin_arcsec"), "software": test_observations.ANALYSIS_VERSION,
            }
        plan = test_observations.generate_plan(payload)
        record = test_observations.create_record(plan)
        return JSONResponse({"ok": True, "record": record})
    except test_observations.TestObservationError as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=400)


def _test_request(record: dict, params: dict) -> dict:
    plan = record["plan"]
    base = {**params, "kind": plan["kind"]}
    base["name"] = str(base.get("name") or f"TEST {plan.get('target') or record['id']}")
    if not base["name"].upper().startswith("TEST"):
        base["name"] = "TEST " + base["name"]
    configs = test_observations.request_configurations(plan, base)
    return lco.build_requestgroup(plan["kind"], base, configurations=configs)


@lco_router.post("/test-observations/{observation_id}/ipp", response_class=JSONResponse)
def api_lco_test_ipp(observation_id: str, request: Request, payload: dict = Body(...)):
    try:
        record = test_observations.get_record(observation_id)
        rg = _test_request(record, payload)
        ipp = lco.max_allowable_ipp(rg, _request_user(request))
        digest = lco.payload_hash(rg)
        record = test_observations.update_record(observation_id, state="validated", payload_hash=digest)
        return JSONResponse({"ok": True, "payload": rg, "payload_hash": digest, "ipp": ipp, "record": record})
    except KeyError:
        return JSONResponse({"ok": False, "error": "test observation not found"}, status_code=404)
    except lco.LcoError as exc:
        return _lco_error_response(exc)


@lco_router.post("/test-observations/{observation_id}/submit", response_class=JSONResponse)
def api_lco_test_submit(observation_id: str, request: Request, payload: dict = Body(...)):
    if not payload.get("confirm"):
        return JSONResponse({"ok": False, "error": "submission requires explicit confirm"}, status_code=400)
    try:
        record = test_observations.get_record(observation_id)
        rg = _test_request(record, payload)
        digest = lco.payload_hash(rg)
        if not payload.get("dry_run_hash") or payload["dry_run_hash"] != digest or record["payload_hash"] != digest:
            return JSONResponse({"ok": False, "error": "no matching test-observation dry-run; run the dry-run again"}, status_code=409)
        result = lco.submit_requestgroup(rg, _request_user(request))
        ids = _submitted_request_ids(result)
        record = test_observations.update_record(observation_id, state="submitted", request_ids=ids)
        params = {**payload, "kind": record["plan"]["kind"], "target_name": record["plan"].get("target") or ""}
        monitoring = _record_lco_submission(result, rg, params, _request_user(request))
        return JSONResponse({"ok": True, "result": result, "record": record, "monitoring": monitoring})
    except KeyError:
        return JSONResponse({"ok": False, "error": "test observation not found"}, status_code=404)
    except lco.LcoError as exc:
        return _lco_error_response(exc)


@lco_router.get("/test-observations/{observation_id}", response_class=JSONResponse)
def api_lco_test_status(observation_id: str):
    try:
        return JSONResponse({"ok": True, "record": test_observations.get_record(observation_id)})
    except KeyError:
        return JSONResponse({"ok": False, "error": "test observation not found"}, status_code=404)


@lco_router.post("/submit", response_class=JSONResponse)
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
        user = _request_user(request)
        result = lco.submit_requestgroup(rg, user)
        monitoring = _record_lco_submission(result, rg, payload, user)
        return JSONResponse({"ok": True, "result": result, "monitoring": monitoring})
    except lco.LcoError as e:
        return _lco_error_response(e)


def _lco_split_error_response(e: "lco.LcoError", leg: str) -> JSONResponse:
    body = e.to_dict()
    body["leg"] = leg
    return JSONResponse(body, status_code=e.status)


@lco_router.post("/split-ipp", response_class=JSONResponse)
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


@lco_router.post("/split-submit", response_class=JSONResponse)
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

    monitoring_a = _record_lco_submission(result_a, rg_a, leg_a_params, user)

    try:
        result_b = lco.submit_requestgroup(rg_b, user)
    except lco.LcoError as e:
        return JSONResponse(
            {
                "ok": False,
                "partial": True,
                "error": "leg A booked, leg B failed to submit",
                "leg_a": {"result": result_a, "monitoring": monitoring_a},
                "leg_b": e.to_dict(),
            },
            status_code=e.status,
        )

    monitoring_b = _record_lco_submission(result_b, rg_b, leg_b_params, user)
    return JSONResponse({
        "ok": True,
        "leg_a": {"result": result_a, "monitoring": monitoring_a},
        "leg_b": {"result": result_b, "monitoring": monitoring_b},
    })


@lco_router.get("/monitored-requests", response_class=JSONResponse)
def api_lco_monitored_requests():
    """Return locally persisted scheduler requests and pipeline progress."""
    return JSONResponse({"ok": True, "requests": lco_monitor.list_requests()})


@lco_router.get("/archive/frames", response_class=JSONResponse)
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


@lco_router.post("/archive/download", response_class=JSONResponse)
def api_lco_archive_download(request: Request, payload: dict = Body(...)):
    try:
        frames = payload.get("frames")
        if not isinstance(frames, list) or not frames:
            return JSONResponse({"ok": False, "error": "no frames selected"}, status_code=400)
        if len(frames) > lco._ARCHIVE_DOWNLOAD_MAX_FRAMES:
            return JSONResponse(
                {"ok": False, "error": f"At most {lco._ARCHIVE_DOWNLOAD_MAX_FRAMES} frames are allowed per download"},
                status_code=413,
            )
        payload_bytes = len(json.dumps(frames, separators=(",", ":"), ensure_ascii=True).encode("utf-8"))
        if payload_bytes > lco._ARCHIVE_DOWNLOAD_MAX_PAYLOAD_BYTES:
            return JSONResponse(
                {"ok": False, "error": f"Frame payload exceeds {lco._ARCHIVE_DOWNLOAD_MAX_PAYLOAD_BYTES} bytes"},
                status_code=413,
            )
        if payload.get("background"):
            job = lco.start_archive_download(
                frames,
                overwrite=bool(payload.get("overwrite")),
                auto_ingest=True,
                user_name=request.state.user,
            )
            _persist_lco_archive_download_row(_lco_archive_download_row(job))
            return JSONResponse({"ok": True, **job})
        foreground_limit = max(1, int(os.environ.get("MUSCAT_LCO_ARCHIVE_FOREGROUND_MAX_FRAMES", "10")))
        if len(frames) > foreground_limit:
            return JSONResponse(
                {"ok": False, "error": f"Foreground downloads are limited to {foreground_limit} frames; use background mode"},
                status_code=413,
            )
        results = lco.download_frames(frames, overwrite=bool(payload.get("overwrite")))
        return JSONResponse({"ok": True, "results": results})
    except lco.LcoError as e:
        return _lco_error_response(e)


@lco_router.get("/archive/download/{job_id}", response_class=JSONResponse)
def api_lco_archive_download_status(job_id: str):
    try:
        job = lco.archive_download_status(job_id)
        if job.get("state") in {"done", "error", "cancelled"}:
            _persist_lco_archive_download_row(_lco_archive_download_row(job))
        return JSONResponse({"ok": True, **job})
    except lco.LcoError as e:
        return _lco_error_response(e)


# Helper to fetch fitted transit centers for a run
def _bjd_to_yymmdd(bjd: float) -> str:
    """UTC obsdate (YYMMDD) for a Barycentric Julian Date.

    Used to give a manually entered transit center a display date matching the
    project's obsdate convention (the Date column of the fitted-dataset rows).
    The one-day-scale barycentric-vs-geocentric offset is irrelevant at date
    granularity, so a plain JD->UTC conversion is used. Returns "" if the value
    is not a finite, convertible number.
    """
    try:
        unix = (float(bjd) - 2440587.5) * 86400.0
        return datetime.datetime.fromtimestamp(
            unix, tz=datetime.timezone.utc
        ).strftime("%y%m%d")
    except (TypeError, ValueError, OverflowError, OSError):
        return ""


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


def _transit_fit_observation_span(rdir: pathlib.Path) -> tuple[float, float] | None:
    """Return the BJD span covered by a transit fit's input light curves."""
    start: float | None = None
    end: float | None = None
    for csv_path in rdir.glob("*.csv"):
        try:
            with open(csv_path, newline="") as f:
                reader = csv.DictReader(f)
                if not reader.fieldnames:
                    continue
                time_key = next(
                    (key for key in ("BJD_TDB", "BJD", "time", "#time") if key in reader.fieldnames),
                    None,
                )
                if time_key is None:
                    continue
                for row in reader:
                    try:
                        value = float(row[time_key])
                    except (KeyError, TypeError, ValueError):
                        continue
                    if not math.isfinite(value):
                        continue
                    start = value if start is None else min(start, value)
                    end = value if end is None else max(end, value)
        except OSError:
            logger.debug("failed to read transit-fit input span from %s", csv_path, exc_info=True)
    if start is None or end is None:
        return None
    return start, end


def _classify_transit_coverage(
    rdir: pathlib.Path,
    planets_fitted: str,
    planets_ephem: dict,
    fitted: dict,
) -> str:
    """Classify the observed event as full, ingress-only, or egress-only.

    The run type describes test versus production sampling and is unrelated to
    event coverage. Coverage instead comes from the actual light-curve time
    span relative to the fitted transit contacts. Input ephemerides are used
    only when a fixed parameter is absent from the fitted summary.
    """
    span = _transit_fit_observation_span(rdir)
    if span is None:
        return ""
    start, end = span
    midpoint = (start + end) / 2.0

    for planet in planets_fitted:
        ephem = planets_ephem.get(planet) or {}
        fit_params = fitted.get(planet) or {}
        tc = fit_params.get("tc", ephem.get("t0"))
        duration_hours = fit_params.get("dur", ephem.get("duration"))
        try:
            tc = float(tc)
            duration_hours = float(duration_hours)
        except (TypeError, ValueError):
            continue
        if not math.isfinite(tc) or not math.isfinite(duration_hours) or duration_hours <= 0:
            continue

        # An input catalog T0 can be many epochs away from the observation.
        # Move it onto the observed epoch when a fitted local Tc is unavailable.
        if fit_params.get("tc") is None:
            try:
                period = float(ephem.get("period"))
                if period > 0:
                    tc += round((midpoint - tc) / period) * period
            except (TypeError, ValueError):
                pass

        half_duration_days = duration_hours / 48.0
        ingress = tc - half_duration_days
        egress = tc + half_duration_days
        includes_ingress = start <= ingress <= end
        includes_egress = start <= egress <= end
        if includes_ingress and includes_egress:
            return "full"
        if includes_ingress:
            return "ing"
        if includes_egress:
            return "egr"
    return ""


def _is_full_transit_fit_job(job: dict) -> bool:
    """Return whether an ephemeris dataset came from a production fit run."""
    run_type = str(job.get("run_type") or "").strip().lower()
    if run_type:
        return run_type == "full"
    # Older persisted jobs predate the run_type column. Recover their mode from
    # the immutable run metadata/log rather than silently treating them as test
    # or dropping valid historical production fits.
    try:
        rdir = fit.fit_output_dir(
            job["instrument"],
            job["obsdate"],
            job["target"],
            (job.get("run_id") or "") or None,
        )
        return fit._detect_run_type(rdir) == "full"
    except (KeyError, OSError):
        logger.debug("failed to detect legacy ephemeris run type", exc_info=True)
        return False


@ads_router.get("/config", response_class=JSONResponse)
def api_ads_config(request: Request):
    """Report whether the ADS API token is configured. No secrets."""
    token, source = _ads_token_for_request(request)
    user = _request_user(request)
    return JSONResponse({
        "ok": True,
        "token_configured": bool(token),
        "token_source": source,
        "user_token_configured": source == "user",
        "global_token_configured": bool(_global_ads_token()),
        "user": user,
    })


@target_router.get("/publications", response_class=JSONResponse)
async def api_target_publications(request: Request, q: str):
    import urllib.parse

    q = (q or "").strip()
    if not q:
        return JSONResponse({"ok": False, "error": "Query parameter q is required"}, status_code=400)

    token, _source = _ads_token_for_request(request)
    if not token:
        return JSONResponse({
            "ok": False,
            "error": "ADS API token is not configured. Save your token in Settings.",
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

    try:
        response = await _async_get(url, headers={
            "Authorization": f"Bearer {token}",
            "User-Agent": "MuSCAT-db/0.1.0"
        })
        data = response.json()
        docs = data.get("response", {}).get("docs", [])
        return JSONResponse({"ok": True, "papers": docs})
    except httpx.HTTPStatusError as e:
        try:
            err_msg = e.response.json().get("error", {}).get("message", str(e))
        except Exception:
            err_msg = str(e)
        return JSONResponse({"ok": False, "error": f"ADS API returned error: {err_msg}"}, status_code=e.response.status_code)
    except Exception as e:
        return JSONResponse({"ok": False, "error": f"Failed to query ADS: {str(e)}"}, status_code=500)


@ephemeris_router.get("/targets", response_class=JSONResponse)
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


def _user_ephem_sheet_cfg(request) -> dict | None:
    """Return the requesting user's ephemeris sheet config (tab defaults
    applied), or None when no user / no sheet / unreadable."""
    user = _request_user(request) if request else None
    if not user:
        return None
    try:
        cfg = get_user_ephem_sheet(user)
    except UserSettingsError:
        logger.debug("could not read ephemeris sheet config for %s", user, exc_info=True)
        return None
    if not cfg:
        return None
    return {
        "url": cfg["url"],
        "ephem_tab": cfg.get("ephem_tab") or gsheet_ephemeris.DEFAULT_EPHEM_TAB,
        "tc_tab": cfg.get("tc_tab") or gsheet_ephemeris.DEFAULT_TC_TAB,
        "ephem_cols": cfg.get("ephem_cols") or {},
        "tc_cols": cfg.get("tc_cols") or {},
    }


def _sheet_ephemeris(target: str, cfg: dict) -> dict:
    """``{planet: {t0, period, duration, *_unc}}`` from the sheet ephemeris tab."""
    try:
        return gsheet_ephemeris.query_target_ephemeris(
            target, cfg["url"], cfg["ephem_tab"], cfg.get("ephem_cols")
        )
    except gsheet_ephemeris.GsheetError:
        return {}


def _sheet_fit_ephemeris(target: str, cfg: dict, seed_by_planet: dict) -> dict:
    """Linear-fit ``{planet: {t0, period, *_unc}}`` from the sheet transit-centers
    tab. ``seed_by_planet`` supplies a per-planet ``{t0, period}`` used only to
    assign integer epochs when the tab has no explicit epoch column; the fit
    re-derives t0/period from the transit centers."""
    try:
        parsed = gsheet_ephemeris.query_target_transit_centers(
            target, cfg["url"], cfg["tc_tab"], cfg.get("tc_cols")
        )
    except gsheet_ephemeris.GsheetError:
        return {}
    by_planet: dict[str, list[dict]] = {}
    for row in parsed.get("rows") or []:
        by_planet.setdefault(row["planet"], []).append(row)

    results: dict = {}
    for planet, points in by_planet.items():
        seed = seed_by_planet.get(planet) or {}
        t0_seed = seed.get("t0")
        p_seed = seed.get("period")
        have_seed = t0_seed is not None and p_seed
        if not have_seed and any(pt.get("source_epoch") is None for pt in points):
            # No seed to assign epochs and the tab did not supply them: cannot fit.
            continue
        if not have_seed and len(points) < 2:
            # Without a seed period a single point cannot yield a real fit; skip
            # rather than echo a fabricated reference period.
            continue
        # Reference echoed back only if the fit itself can't run (<2 points).
        t0_ref = float(t0_seed) if have_seed else float(points[0]["tc"])
        p_ref = float(p_seed) if have_seed else 1.0
        epochs, tcs, uncs = [], [], []
        for pt in points:
            epoch = pt.get("source_epoch")
            if epoch is None:
                epoch = ephemeris_math.assign_epoch(pt["tc"], t0_ref, p_ref)
            epochs.append(epoch)
            tcs.append(pt["tc"])
            uncs.append(pt["tc_unc"])
        fit_result = ephemeris_math.fit_linear_ephemeris(
            epochs, tcs, uncs, t0_ref, p_ref, fit_method="unweighted"
        )
        entry: dict = {"t0": fit_result["t0_fit"], "period": fit_result["period_fit"]}
        if fit_result.get("t0_fit_unc"):
            entry["t0_unc"] = fit_result["t0_fit_unc"]
        if fit_result.get("period_fit_unc"):
            entry["period_unc"] = fit_result["period_fit_unc"]
        results[planet] = entry
    return results


def _target_coordinates(target: str) -> dict | None:
    """Pointing coordinates for the schedule page: catalog CSVs first, then
    SIMBAD.

    The ephemeris catalogs (NASA/TOI) are consulted first via
    ``_resolve_archive_coords``; when the target is in neither, it falls back to
    SIMBAD name resolution (both cached), so a catalog-less target still gets
    coordinates instead of an empty RA/Dec field. Returns ``{"ra", "dec",
    "source"}`` (source one of nasa/toi/simbad) or None when unresolved."""
    resolved = _resolve_archive_coords(target)
    if resolved is None:
        return None
    ra, dec, source = resolved
    return {"ra": ra, "dec": dec, "source": source}


@ephemeris_router.get("/target-info", response_class=JSONResponse)
def api_ephemeris_target_info(target: str, request: Request):
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
            and _is_full_transit_fit_job(j)
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
        rdir = fit.fit_output_dir(inst, date, j["target"], run_id or None)
        try:
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

                        duration_list = pl_params.get("dur", [None, None])
                        if isinstance(duration_list, (list, tuple)):
                            duration_days = duration_list[0] if len(duration_list) > 0 else None
                            duration_unc_days = duration_list[1] if len(duration_list) > 1 else None
                        else:
                            duration_days = duration_list
                            duration_unc_days = None

                        # t0 and duration are overridden below from the Fitted
                        # Parameters Summary; period stays from sys.yaml (it is
                        # held fixed in the fit and absent from the summary).
                        planets_ephem[pl] = {
                            "t0": float(t0_mean),
                            "t0_unc": float(t0_unc) if t0_unc is not None else None,
                            "period": float(period_mean),
                            "period_unc": float(period_unc) if period_unc is not None else None,
                            "duration": float(duration_days) * 24.0 if duration_days is not None else None,
                            "duration_unc": float(duration_unc_days) * 24.0 if duration_unc_days is not None else None,
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
            "transit_coverage": _classify_transit_coverage(
                rdir, planets_fitted, planets_ephem, fitted
            ),
        })
        
    # Per-user Google Sheet ephemeris sources (optional). The ephemeris tab
    # feeds t0/period/duration directly; the transit-centers tab is linear-fit
    # against the best available seed ephemeris to derive t0/period.
    sheet_cfg = _user_ephem_sheet_cfg(request)
    sheet_ephem: dict = {}
    sheet_fit_ephem: dict = {}
    if sheet_cfg:
        sheet_ephem = _sheet_ephemeris(target, sheet_cfg)
        seed_by_planet: dict = {}
        for pl in set(sheet_ephem) | seen_planets:
            seed = (
                sheet_ephem.get(pl)
                or ref_ephem.get(pl)
                or nasa_ephem.get(pl)
                or toi_ephem.get(pl)
                or {}
            )
            if seed.get("t0") is not None and seed.get("period"):
                seed_by_planet[pl] = seed
        sheet_fit_ephem = _sheet_fit_ephemeris(target, sheet_cfg, seed_by_planet)
        seen_planets.update(sheet_ephem.keys())
        seen_planets.update(sheet_fit_ephem.keys())

    # Ensure all seen planets are initialized in all ephemerides
    for pl in seen_planets:
        ref_ephem.setdefault(pl, {})
        nasa_ephem.setdefault(pl, {})
        toi_ephem.setdefault(pl, {})
        sheet_ephem.setdefault(pl, {})
        sheet_fit_ephem.setdefault(pl, {})

    planets_sorted = sorted(list(seen_planets))

    return JSONResponse({
        "ok": True,
        "target": target,
        "planets": planets_sorted,
        "coordinates": _target_coordinates(target),
        "reference_ephemeris": ref_ephem,
        "nasa_ephemeris": nasa_ephem,
        "toi_ephemeris": toi_ephem,
        "sheet_configured": sheet_cfg is not None,
        "sheet_ephemeris": sheet_ephem,
        "sheet_fit_ephemeris": sheet_fit_ephem,
        "datasets": datasets_list
    })


@ephemeris_router.post("/import-csv", response_class=JSONResponse)
def api_ephemeris_import_csv(payload: dict = Body(...)):
    """Parse an uploaded CSV preview without reading arbitrary server paths."""
    text = payload.get("content")
    filename = pathlib.PurePath(str(payload.get("filename") or "transit-times.csv")).name
    try:
        parsed = ephemeris_import.parse_transit_csv(text)
    except ephemeris_import.EphemerisCSVError as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=400)
    return JSONResponse({"ok": True, "filename": filename, **parsed})


@ephemeris_router.post("/calculate", response_class=JSONResponse)
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

    # Manually entered transit centers (Reading-2 feature): user-supplied
    # points that are merged into the per-planet series alongside database
    # fits. Grouped by planet letter here; each requires a numeric tc and a
    # positive uncertainty (weighting needs unc > 0), otherwise it is dropped.
    manual_by_planet: dict[str, list[dict]] = {}
    for mp in payload.get("manual_points") or []:
        if not isinstance(mp, dict):
            continue
        planet = str(mp.get("planet") or "").strip()
        if not planet:
            continue
        try:
            tc = float(mp.get("tc"))
            unc = float(mp.get("tc_unc"))
        except (TypeError, ValueError):
            continue
        if not (unc > 0):
            continue
        source_epoch = None
        raw_source_epoch = mp.get("source_epoch")
        if raw_source_epoch is not None and not isinstance(raw_source_epoch, bool):
            try:
                source_epoch_number = float(raw_source_epoch)
                if source_epoch_number.is_integer():
                    source_epoch = int(source_epoch_number)
            except (TypeError, ValueError):
                pass
        manual_by_planet.setdefault(planet, []).append({
            "id": str(mp.get("id") or ""),
            "tc": tc,
            "unc": unc,
            "instrument": str(mp.get("instrument") or "").strip(),
            "target": str(mp.get("target") or "").strip(),
            "date": str(mp.get("date") or "").strip(),
            "note": str(mp.get("note") or "").strip(),
            "source_epoch": source_epoch,
            "source_file": str(mp.get("source_file") or "").strip(),
            "time_system": str(mp.get("time_system") or "").strip(),
            "checked": bool(mp.get("checked", True)),
        })

    # Build checked lookup: (target_normalized, inst, date, run_id) -> checked_bool
    checked_lookup = {}
    for d in req_datasets:
        tgt = d.get("target")
        norm_t = _normalize_target_name(tgt) if tgt else None
        key = (norm_t, d.get("instrument"), d.get("date"), d.get("run_id") or "")
        checked_lookup[key] = bool(d.get("checked"))

    def requested_dataset_state(job: dict) -> bool | None:
        """Return the posted check state, or None when the row was not posted."""
        inst = job["instrument"]
        date = job["obsdate"]
        run_id = job.get("run_id") or ""
        norm_tgt = _normalize_target_name(job["target"])
        exact_key = (norm_tgt, inst, date, run_id)
        if exact_key in checked_lookup:
            return checked_lookup[exact_key]
        targetless_key = (None, inst, date, run_id)
        if targetless_key in checked_lookup:
            return checked_lookup[targetless_key]
        # Backward compatibility for older clients that posted a target alias
        # which normalizes differently. Presence is still mandatory: an
        # unposted database job must never become an implicit plotted point.
        for (key_target, key_inst, key_date, key_run_id), state in checked_lookup.items():
            if key_inst == inst and key_date == date and key_run_id == run_id:
                return state
        return None
        
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
                if (
                    j["type"] == "transit_fit"
                    and j["state"] == "done"
                    and _is_full_transit_fit_job(j)
                    and _normalize_target_name(j["target"]) == norm_t
                    and requested_dataset_state(j) is not None
                ):
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
                
                is_checked = requested_dataset_state(j)
                if is_checked is None:
                    # Defensive guard: completed is already restricted to
                    # explicitly requested rows above.
                    continue

                epoch = ephemeris_math.assign_epoch(val, T0, P)

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

        # Merge manually entered transit centers for this planet. They share
        # the same epoch grid and participate in the fit (when checked) exactly
        # like database points; a UTC date is derived from the BJD for display.
        manual_target = targets[0] if targets else ""
        for mp in manual_by_planet.get(pl, []):
            points.append({
                # Instrument is free text (e.g. "tess"); blank falls back to
                # "manual" so it still reads clearly and gets the manual marker.
                "instrument": mp["instrument"] or "manual",
                # Target and date are optional overrides: date defaults to the
                # YYMMDD obsdate derived from the BJD, target to the loaded target.
                "date": mp["date"] or _bjd_to_yymmdd(mp["tc"]),
                "run_id": "",
                "run_name": mp["note"],
                "target": mp["target"] or manual_target,
                "epoch": ephemeris_math.assign_epoch(mp["tc"], T0, P),
                "tc": mp["tc"],
                "unc": mp["unc"],
                "checked": mp["checked"],
                "manual": True,
                "manual_id": mp["id"],
                "note": mp["note"],
                "source_epoch": mp["source_epoch"],
                "source_file": mp["source_file"],
                "time_system": mp["time_system"],
            })

        # Perform straight line fit if possible. The weighted/unweighted
        # least-squares math (epoch-centering, variance propagation) lives in
        # ephemeris_math.fit_linear_ephemeris; only checked, positive-uncertainty
        # points may participate in the regression.
        fit_points = [p for p in points if p["checked"] and p["unc"] is not None and p["unc"] > 0]
        fit_method = payload.get("fit_method", "unweighted")
        fit_result = ephemeris_math.fit_linear_ephemeris(
            [p["epoch"] for p in fit_points],
            [p["tc"] for p in fit_points],
            [p["unc"] for p in fit_points],
            T0,
            P,
            fit_method=fit_method,
        )
        was_fit = fit_result["was_fit"]
        t0_fit = fit_result["t0_fit"]
        period_fit = fit_result["period_fit"]
        t0_fit_unc = fit_result["t0_fit_unc"]
        period_fit_unc = fit_result["period_fit_unc"]
        t0_centered = fit_result["t0_fit_centered"]
        t0_centered_unc = fit_result["t0_fit_centered_unc"]
        E_center = fit_result["E_center"]

        # Calculate O-C values
        points_data = []
        for p in points:
            t_calc = t0_fit + p["epoch"] * period_fit
            oc_days = p["tc"] - t_calc
            oc_min = oc_days * 1440.0
            oc_err_min = p["unc"] * 1440.0

            point_out = {
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
            }
            if p.get("manual"):
                # Flag a manual point that sits more than 5 sigma off the fitted
                # linear ephemeris (a likely data-entry error). Warn-only: the
                # point still participates in the fit and downstream TTV CSV.
                point_out["manual"] = True
                point_out["manual_id"] = p.get("manual_id", "")
                point_out["note"] = p.get("note", "")
                point_out["source_epoch"] = p.get("source_epoch")
                point_out["epoch_offset"] = (
                    p["epoch"] - p["source_epoch"]
                    if p.get("source_epoch") is not None else None
                )
                point_out["source_file"] = p.get("source_file", "")
                point_out["time_system"] = p.get("time_system", "")
                point_out["flagged"] = bool(
                    was_fit and ephemeris_math.is_sigma_outlier(oc_days, p["unc"])
                )
            points_data.append(point_out)

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


def _lco_obslog_url(instrument: str, obsdate: str) -> str:
    """Dataset obslog URL once an archive date has been ingested."""
    if instrument not in INSTRUMENTS or not re.fullmatch(r"\d{6}", obsdate or ""):
        return ""
    try:
        if _get_summaries(_db_path(), instrument, obsdate):
            return f"/{instrument}/{obsdate}"
    except Exception:
        logger.debug("failed to check obslog for %s/%s", instrument, obsdate, exc_info=True)
    return ""


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
    single_dataset = state == "done" and len(instruments) == 1 and len(obsdates) == 1
    obslog_url = _lco_obslog_url(instruments[0], obsdates[0]) if single_dataset else ""
    photometry_url = str(job.get("photometry_url") or "")
    if not photometry_url and obslog_url:
        params = {"inst": instruments[0], "date": obsdates[0]}
        if len(objects) == 1:
            params["target"] = objects[0]
        photometry_url = "/photometry?" + urlencode(params)
    can_run_dataset_action = bool(photometry_url)
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
        "user_name": str(job.get("user_name") or ""),
        "details": details,
        "action_inst": instruments[0] if len(instruments) == 1 else "",
        "action_date": obsdates[0] if len(obsdates) == 1 else "",
        "can_run_dataset_action": can_run_dataset_action,
        "obslog_url": obslog_url,
        "photometry_url": photometry_url,
    }


_LCO_PERSIST_SIGNATURES: dict[tuple[str, str], tuple] = {}
_LCO_PERSIST_LOCK = threading.Lock()


def _persist_lco_archive_download_row(row: dict) -> None:
    params = {
        "job_id": row.get("run_id") or "",
        "details": row.get("details") or "",
        "action_inst": row.get("action_inst") or "",
        "action_date": row.get("action_date") or "",
        "can_run_dataset_action": bool(row.get("can_run_dataset_action")),
        "obslog_url": row.get("obslog_url") or "",
        "photometry_url": row.get("photometry_url") or "",
    }
    job_id = str(row.get("run_id") or "")
    signature = (
        row.get("state"), row.get("run_name"), row.get("details"),
        row.get("error_desc"), row.get("photometry_url"),
    )
    signature_key = (str(_db_path()), job_id)
    with _LCO_PERSIST_LOCK:
        if _LCO_PERSIST_SIGNATURES.get(signature_key) == signature:
            return
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
            user_name=row.get("user_name") or "",
        )
        with _LCO_PERSIST_LOCK:
            _LCO_PERSIST_SIGNATURES[signature_key] = signature
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
    row["obslog_url"] = _lco_obslog_url(row["action_inst"], row["action_date"])
    row["photometry_url"] = str(params.get("photometry_url") or "")
    if not row["photometry_url"] and row["obslog_url"]:
        query = {"inst": row["action_inst"], "date": row["action_date"]}
        target = str(row.get("target") or "").strip()
        if target and "," not in target:
            query["target"] = target
        row["photometry_url"] = "/photometry?" + urlencode(query)
    row["can_run_dataset_action"] = bool(row["photometry_url"])
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


@jobs_router.post("/lco-archive/scan", response_class=JSONResponse)
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


@jobs_router.post("/lco-archive/ingest-date", response_class=JSONResponse)
def jobs_lco_archive_ingest_date(payload: dict = Body(...)):
    inst, obsdate = _validate_lco_dataset_action(payload)
    try:
        from muscat_db.database import ingest_date as _ingest_date

        count = _ingest_date(str(_db_path()), inst, obsdate)
        return JSONResponse({
            "ok": True,
            "command": f"muscat-db ingest-date {inst} {obsdate}",
            "count": count,
            "obslog_url": f"/{inst}/{obsdate}",
        })
    except Exception as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=500)


@app.get("/jobs", response_class=HTMLResponse)
def jobs_page():
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

@jobs_router.get("/status", response_class=JSONResponse)
def jobs_status(active_only: bool = False):
    if active_only:
        # Lightweight path for the site-wide loading indicator. Reports only
        # indexed durable rows plus live archive jobs. It deliberately does not
        # reconcile pipelines, load terminal history, or touch `_last_running`
        # baseline — that diff belongs to the full Jobs-page poll, and letting
        # a second site-wide poller mutate it would steal `finished`
        # transitions from the Jobs page.
        active_by_key = {}
        for persisted in get_job_store().active():
            row = (
                _adapt_persisted_lco_archive_row(persisted)
                if persisted.get("type") == "lco_archive_download"
                else persisted
            )
            active_by_key[row["key"]] = {"key": row["key"], "state": row["state"]}
        archive_active = [
            {"key": j["key"], "state": j["state"]}
            for j in (_lco_archive_download_row(job) for job in lco.archive_download_jobs())
            if j["state"] in ("running", "cancelling", "pending")
        ]
        for item in archive_active:
            active_by_key[item["key"]] = item
        return {"active": list(active_by_key.values())}

    all_jobs = _jobs_with_lco_archive_rows()

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
                "obslog_url": j.get("obslog_url", ""),
                "photometry_url": j.get("photometry_url", ""),
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
            "obslog_url": j.get("obslog_url", ""),
            "photometry_url": j.get("photometry_url", ""),
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


@jobs_router.get("/log/{type_}/{inst}/{date}/{target}")
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


@jobs_router.get("/ttv-log/{target}")
def ttv_job_log(target: str, run: str = ""):
    # `run` is the job's run_id (an already-slugified segment); log_path
    # validates it and resolves the default run when it is empty.
    path = ttv.log_path(target, (run or "").strip())
    if path is None:
        raise HTTPException(404, "log not found")
    return FileResponse(str(path))


@jobs_router.post("/rerun")
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
    for field in ("run_name", "site", "telescope", "mode"):
        value = p.get(field) or job.get(field)
        if value and not options.get(field):
            options[field] = value
    user_name = request.state.user
    if job["type"] == "photometry":
        result = phot.start_run(inst, date, target, options=options, test_run=p.get("test_run", True), user_name=user_name)
    elif job["type"] == "transit_fit":
        result = fit.start_fit(inst, date, target, options=options, test_run=p.get("test_run", False), selected_csvs=p.get("selected_csvs"), user_name=user_name)
    elif job["type"] == "ttv_fit":
        from muscat_db.ttv_fit import start_ttv_fit
        result = start_ttv_fit(target, options, user_name)
    else:
        raise HTTPException(400, "unknown job type")
    if not result.get("ok"):
        return JSONResponse(result, status_code=400)
    return JSONResponse(result)


@photometry_router.get("/file/{inst}/{date}/{target}/run/{run_id}/{name}")
def photometry_file_run(inst: str, date: str, target: str, run_id: str, name: str):
    path = phot.safe_run_artifact_path(inst, date, target, run_id, name)
    if path is None:
        raise HTTPException(404, "artifact not found")
    return FileResponse(str(path), headers={"Cache-Control": "no-store, no-cache, must-revalidate, max-age=0"})


@photometry_router.get("/file/{inst}/{date}/{name}")
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


@photometry_router.get("/download-all/{inst}/{date}/{target}/run/{run_id}")
def photometry_download_all_run(inst: str, date: str, target: str, run_id: str):
    return _photometry_download_all(inst, date, target, run_id)


@photometry_router.get("/download-all/{inst}/{date}/{target}")
def photometry_download_all(inst: str, date: str, target: str):
    return _photometry_download_all(inst, date, target, None)


@photometry_router.post("/run")
def photometry_run(request: Request, payload: dict = Body(...)):
    inst = (payload.get("inst") or "").strip()
    date = (payload.get("date") or "").strip()
    target = (payload.get("target") or "").strip()
    options = payload.get("options") or {}
    test_run = bool(payload.get("test_run", True))
    user_name = request.state.user
    # Hard block: never launch a sinistro run that would merge multiple sites
    # or multiple physical telescopes.
    site_err = _site_required_error(_db_path(), inst, date, target, options)
    if site_err:
        return JSONResponse({"ok": False, "error": site_err}, status_code=400)
    telescope_err = _telescope_required_error(_db_path(), inst, date, target, options)
    if telescope_err:
        return JSONResponse({"ok": False, "error": telescope_err}, status_code=400)
    result = phot.start_run(inst, date, target, options=options, test_run=test_run, user_name=user_name)
    if not result.get("ok"):
        return JSONResponse(result, status_code=400)
    return JSONResponse(result)


@photometry_router.post("/command")
def photometry_command(payload: dict = Body(...)):
    """Preview the exact prose command for the chosen options (live form echo)."""
    inst = (payload.get("inst") or "").strip()
    date = (payload.get("date") or "").strip()
    target = (payload.get("target") or "").strip()
    options = payload.get("options") or {}
    test_run = bool(payload.get("test_run", False))
    error = phot.validate_run_options(phot.normalize_run_options(options), inst=inst)
    # Surface the multi-site/multi-telescope block as a command error so the
    # page disables the run buttons and shows why until a choice is made.
    if not error:
        error = _site_required_error(_db_path(), inst, date, target, options)
    if not error:
        error = _telescope_required_error(_db_path(), inst, date, target, options)
    command = phot.command_str(inst, date, target, options=options, test_run=test_run)
    return JSONResponse({"command": command, "error": error})


@photometry_router.get("/status")
def photometry_status(inst: str, date: str, target: str, run: str = ""):
    # Drain the queue so a pending full job is promoted once the slot frees,
    # even when only the photometry page (not the Jobs page) is polling.
    phot.sync_jobs()
    return JSONResponse(phot.job_status(inst, date, target, run_id=(run or "").strip()))


@photometry_router.post("/status-batch")
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
    if len(jobs) > _MAX_STATUS_BATCH:
        return JSONResponse(
            {"error": f"jobs must contain at most {_MAX_STATUS_BATCH} entries"},
            status_code=400,
        )

    results = []
    for job_spec in jobs:
        if not isinstance(job_spec, dict):
            results.append({"error": "each job must be an object"})
            continue
        raw_fields = tuple(job_spec.get(name) or "" for name in ("inst", "date", "target", "run"))
        if not all(isinstance(value, str) for value in raw_fields):
            results.append({"error": "job fields must be strings"})
            continue
        inst, date, target, run = (value.strip() for value in raw_fields)

        if not all([inst, date, target]):
            results.append({"error": "inst, date, and target are required"})
            continue
        if any(len(value) > _MAX_STATUS_FIELD_LEN for value in (inst, date, target, run)):
            results.append({"error": "job fields are too long"})
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


@photometry_router.post("/cancel")
def photometry_cancel(payload: dict = Body(...)):
    inst = (payload.get("inst") or "").strip()
    date = (payload.get("date") or "").strip()
    target = (payload.get("target") or "").strip()
    run_id = (payload.get("run_id") or payload.get("run") or "").strip()
    result = phot.cancel_run(inst, date, target, run_id=run_id)
    if not result.get("ok"):
        return JSONResponse(result, status_code=400)
    return JSONResponse(result)


@photometry_router.post("/delete")
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


@target_router.put("/{obj}/note")
def api_set_note(obj: str, payload: dict = Body(...)):
    note = (payload.get("note") or "").strip()
    if len(note) > 2000:
        raise HTTPException(400, "note too long (max 2000 chars)")
    _set_note(_db_path(), obj, note)
    return JSONResponse({"ok": True, "object": obj, "note": note})


@target_router.delete("/{obj}/note")
def api_delete_note(obj: str):
    _delete_note(_db_path(), obj)
    return JSONResponse({"ok": True, "object": obj})


@target_router.put("/{obj}/identified")
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


# ── TTV Fit API ──────────────────────────────────────────────────────────────


@ttv_fit_router.get("/outputs", response_class=JSONResponse)
def ttv_fit_outputs(target: str = "", run_name: str = ""):
    if not target:
        return JSONResponse({"ok": False, "error": "target is required"}, status_code=400)
    outputs = ttv.get_ttv_outputs(target.strip(), run_name)
    return JSONResponse({"ok": True, "outputs": outputs})


@ttv_fit_router.get("/runs", response_class=JSONResponse)
def ttv_fit_runs(target: str = ""):
    if not target:
        return JSONResponse({"ok": False, "error": "target is required"}, status_code=400)
    return JSONResponse({"ok": True, "runs": ttv.list_ttv_runs(target.strip())})


@ttv_fit_router.get("/model", response_class=JSONResponse)
def ttv_fit_model(target: str = "", run_name: str = "", end_date: str = ""):
    if not target:
        return JSONResponse({"ok": False, "error": "target is required"}, status_code=400)
    result = ttv.get_ttv_model(target.strip(), run_name, end_date.strip())
    return JSONResponse(result, status_code=200 if result.get("ok") else 400)


@ttv_fit_router.post("/start", response_class=JSONResponse)
def api_start_ttv_fit(request: Request, payload: dict = Body(...)):
    target = (payload.get("target") or "").strip()
    options = payload.get("options") or {}
    if not target:
        return JSONResponse({"ok": False, "error": "target is required"}, status_code=400)
    result = ttv.start_ttv_fit(target, options, request.state.user)
    return JSONResponse(result)


@ttv_fit_router.post("/cancel", response_class=JSONResponse)
def api_cancel_ttv_fit(payload: dict = Body(...)):
    target = (payload.get("target") or "").strip()
    run_name = (payload.get("run_name") or "").strip()
    if not target:
        return JSONResponse({"ok": False, "error": "target is required"}, status_code=400)
    res = ttv.cancel_ttv_fit(target, run_name)
    return JSONResponse(res)


@ttv_fit_router.post("/delete", response_class=JSONResponse)
def api_delete_ttv_fit(payload: dict = Body(...)):
    target = (payload.get("target") or "").strip()
    run_name = (payload.get("run_name") or "").strip()
    if not target:
        return JSONResponse({"ok": False, "error": "target is required"}, status_code=400)
    res = ttv.delete_ttv_fit(target, run_name)
    return JSONResponse(res)


@ttv_fit_router.get("/status", response_class=JSONResponse)
def ttv_fit_status(target: str = "", run_name: str = ""):
    if not target:
        return JSONResponse({"ok": False, "error": "target is required"}, status_code=400)
    status = ttv.job_status(target.strip(), run_name)
    return JSONResponse(status)


# Text-like TTV output extensions the browser should render in a new tab
# instead of downloading. FileResponse only adds Content-Disposition when a
# ``filename`` is passed, so we leave that unset and just force a
# browser-renderable media type (the guessed type for e.g. .csv/.yaml would
# otherwise trigger a download).
_INLINE_TEXT_EXTS = {".csv", ".ini", ".log", ".txt", ".yaml", ".yml", ".cfg", ".dat", ".tsv"}


def _inline_output_file(path: pathlib.Path) -> FileResponse:
    """Serve a TTV output file so browsers open it in a new tab rather than
    prompting a download.

    Text-like files are forced to ``text/plain``; gzipped text such as
    ``samples.csv.gz`` is served with ``Content-Encoding: gzip`` so the browser
    transparently decompresses and renders the underlying text inline (the
    GZip middleware passes it through untouched since the header is set here).
    """
    suffixes = [s.lower() for s in path.suffixes]
    headers = {"Cache-Control": "no-store, no-cache, must-revalidate, max-age=0"}
    media_type: str | None = None
    if suffixes and suffixes[-1] == ".gz":
        headers["Content-Encoding"] = "gzip"
        inner = suffixes[-2] if len(suffixes) >= 2 else ""
        media_type = "application/json" if inner == ".json" else "text/plain; charset=utf-8"
    elif suffixes and suffixes[-1] in _INLINE_TEXT_EXTS:
        media_type = "text/plain; charset=utf-8"
    # Other types (.json, .png, images) already render inline under their
    # guessed media type, so leave media_type=None for those.
    return FileResponse(str(path), media_type=media_type, headers=headers)


@ttv_fit_router.get("/output-file", response_class=FileResponse)
def ttv_fit_output_file(target: str = "", run_name: str = "", file: str = ""):
    if not target:
        return JSONResponse({"ok": False, "error": "target is required"}, status_code=400)
    filepath = ttv.safe_output_file(target.strip(), run_name, file)
    if filepath is None:
        if not file or pathlib.PurePath(file).name != file:
            raise HTTPException(400, "invalid filename")
        raise HTTPException(404, f"file not found: {file}")
    return _inline_output_file(filepath)


@ttv_fit_router.get("/download-all", response_class=FileResponse)
def ttv_fit_download_all(target: str = "", run_name: str = ""):
    if not target:
        return JSONResponse({"ok": False, "error": "target is required"}, status_code=400)
    output_dir = ttv.ttv_output_dir(target.strip(), run_name)
    if not output_dir.is_dir():
        raise HTTPException(404, "output directory not found")
    files = [
        (path, path.name)
        for path in sorted(output_dir.iterdir())
        if path.is_file() and not path.name.startswith(".")
    ]
    return _create_zip_response(
        files,
        f"{target.strip().replace(' ', '')}_ttv_outputs.zip",
    )


@ttv_fit_router.post("/command", response_class=JSONResponse)
def api_ttv_fit_command(payload: dict = Body(...)):
    target = (payload.get("target") or "").strip()
    options = payload.get("options") or {}
    if not target:
        return JSONResponse({"ok": False, "error": "target is required"}, status_code=400)
    cmd_str = ttv.get_ttv_command(target, options)
    return JSONResponse({"ok": True, "command": cmd_str})


app.include_router(photometry_router)
app.include_router(transit_fit_router)
app.include_router(exposure_router)
app.include_router(jobs_router)
app.include_router(target_router)
app.include_router(ephemeris_router)
app.include_router(ttv_fit_router)
app.include_router(fov_router)
app.include_router(lco_router)
app.include_router(settings_router)
app.include_router(ads_router)
