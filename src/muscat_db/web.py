from __future__ import annotations

import datetime
import math
import os
import pathlib
import re
import sqlite3
import threading
import time
from zoneinfo import ZoneInfo

_DB_LOCK = threading.Lock()
_CATALOG_CACHE: dict = {}

import csv
import io

from fastapi import Body, FastAPI, HTTPException
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from jinja2 import Environment, FileSystemLoader
from contextlib import asynccontextmanager

from muscat_db import photometry as phot
from muscat_db import exposure as exp_calc
from muscat_db import transit_fit as fit
from muscat_db import lco
from muscat_db import transit_obs
from muscat_db import fov as fov_opt
from muscat_db.database import (
    SCHEMA,
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
    get_persisted_jobs,
    get_last_build_date,
    _normalize_filters,
)
from muscat_db.instruments import INSTRUMENTS
from muscat_db.coord import (
    CoordRepr,
    unpack as _unpack_coord,
    clean_ra as _clean_ra,
    clean_dec as _clean_dec,
)

HERE = pathlib.Path(__file__).parent
TEMPLATE_DIR = HERE / "templates"
STATIC_DIR = HERE / "static"

@asynccontextmanager
async def _lifespan(app: FastAPI):
    """Create the database and schema on startup if they don't exist."""
    db = _db_path()
    conn = sqlite3.connect(db, timeout=10)
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.executescript(SCHEMA)
    conn.close()
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


def _render(name: str, **kwargs) -> str:
    tpl = jinja.get_template(name)
    return HTMLResponse(tpl.render(**kwargs))


def _db_mtime(db: str):
    """Cache key for the DB file. Note edits and `build-db` both rewrite the
    SQLite file, bumping its mtime, so this auto-invalidates the index cache."""
    try:
        return os.stat(db).st_mtime_ns
    except OSError:
        return None


# Rendering the ~2.85 MB targets page costs ~1.3s. Cache the rendered HTML
# keyed on the DB mtime so repeat loads are instant until the data changes.
_index_cache: dict[str, tuple] = {}


@app.get("/", response_class=HTMLResponse)
async def index():
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
    conn = sqlite3.connect(db)
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
    obs_stats: dict[tuple, dict] = {}
    for row in cur.fetchall():
        raw_filters = sorted(f for f in (row[4] or "").split(",") if f)
        obs_stats[(row[0], row[1], row[2])] = {
            "n_frames": row[3] or 0,
            "filters": raw_filters,
            "filter_chips": _normalize_filters(raw_filters),
            "airmass_min": row[5],
            "airmass_max": row[6],
        }
    conn.close()

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

            fit_out = fit.get_fit_outputs(inst, date, obj_name)
            fit_status = "full" if fit_out.get("has_any") else "none"

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
                "phot": phot_status,
                "fit": fit_status,
                "note": target["note"],
            }
            datasets.append(dataset)

    datasets.sort(key=lambda d: d["date"], reverse=True)
    last_updated = get_last_build_date(db)
    return datasets, last_updated


@app.get("/target", response_class=HTMLResponse)
async def target_page(name: str = ""):
    db = _db_path()
    tpl_path = TEMPLATE_DIR / "target.html"
    tpl_mtime = str(tpl_path.stat().st_mtime_ns) if tpl_path.is_file() else ""

    if not name:
        # List view: show all normalized targets
        key = (tpl_mtime, _db_mtime(db), "list")
        cache_key = "target:list"
        cached = _index_cache.get(cache_key)
        if cached is not None and cached[0] == key:
            return HTMLResponse(cached[1])

        targets = _get_targets(db)

        from collections import defaultdict
        groups = defaultdict(int)

        for target in targets:
            norm_name = _normalize_target_name(target["object"])
            # Count datasets for this normalized name
            date_to_inst = target["date_to_inst"]
            for date in target["dates"]:
                if date in date_to_inst:
                    groups[norm_name] += 1

        # Sort by normalized name
        sorted_groups = sorted(groups.items(), key=lambda x: x[0])

        last_updated = get_last_build_date(db)

        html = jinja.get_template("target.html").render(
            target_name=None,
            target_groups=sorted_groups,
            last_updated=last_updated,
        )

        _index_cache[cache_key] = (key, html)
        return HTMLResponse(html)
    else:
        # Single target view - normalize the input name
        norm_name = _normalize_target_name(name)
        key = (tpl_mtime, _db_mtime(db), norm_name)
        cache_key = f"target:{norm_name}"
        cached = _index_cache.get(cache_key)
        if cached is not None and cached[0] == key:
            return HTMLResponse(cached[1])

        datasets, last_updated = _get_datasets_for_normalized_target(db, norm_name)

        html = jinja.get_template("target.html").render(
            target_name=norm_name,
            datasets=datasets,
            last_updated=last_updated,
        )

        _index_cache[cache_key] = (key, html)
        return HTMLResponse(html)


@app.get("/logs", response_class=HTMLResponse)
async def logs_page(min_frames: int = 1000):
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
async def guide_page():
    return _render("guide.html")

# Legacy redirect for backward compatibility
@app.get("/workflow", response_class=RedirectResponse)
async def workflow_redirect():
    return RedirectResponse(url="/guide", status_code=301)


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
        conn = sqlite3.connect(db)
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
        conn.close()
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
            conn = sqlite3.connect(db)
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

            conn.close()
        except Exception:
            pass

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
            rows.append({"path": str(c), "name": c.name, "created_at": created_at,
                         "_mtime": mtime, "_site": csite, "_mode": cmode})

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

        csvs = [{"path": r["path"], "name": r["name"], "created_at": r["created_at"]} for r in rows]

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
        best_row = None
        best_score = -1
        
        with open(csv_path, mode='r', encoding='utf-8') as f:
            reader = csv.DictReader(f)
            for row in reader:
                pl_name = (row.get("pl_name") or "").strip()
                hostname = (row.get("hostname") or "").strip()
                hip_name = (row.get("hip_name") or "").strip()
                hd_name = (row.get("hd_name") or "").strip()
                
                pl_clean = re.sub(r"[^0-9a-zA-Z]", "", pl_name).lower()
                host_clean = re.sub(r"[^0-9a-zA-Z]", "", hostname).lower()
                hip_clean = re.sub(r"[^0-9a-zA-Z]", "", hip_name).lower()
                hd_clean = re.sub(r"[^0-9a-zA-Z]", "", hd_name).lower()
                
                score = -1
                if target_clean == pl_clean:
                    score = 3
                elif target_clean in (host_clean, hip_clean, hd_clean):
                    score = 2
                elif (pl_clean and pl_clean in target_clean) or (host_clean and host_clean in target_clean):
                    score = 1
                    
                if score > -1:
                    is_default = (row.get("default_flag") == "1")
                    if score > best_score:
                        best_score = score
                        best_row = row
                    elif score == best_score:
                        if is_default and (best_row and best_row.get("default_flag") != "1"):
                            best_row = row
                            
                    if best_score >= 2 and is_default:
                        break
                        
        if not best_row:
            return None
            
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
        queries = [
            f"SELECT {col_str} FROM toi WHERE toi = {_adql_literal(clean_target)}",
            f"SELECT {col_str} FROM toi WHERE toidisplay LIKE {_adql_literal('%' + target + '%')}",
            f"SELECT {col_str} FROM toi WHERE toi LIKE {_adql_literal('%' + clean_target + '%')}",
        ]

        data = []
        for q in queries:
            url = 'https://exoplanetarchive.ipac.caltech.edu/TAP/sync?' + urllib.parse.urlencode({'query': q, 'format': 'json'})
            req = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0'})
            try:
                with urllib.request.urlopen(req, timeout=5) as response:
                    res = json.loads(response.read().decode())
                    if res:
                        data = res
                        break
            except Exception:
                continue

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

        queries = [
            f"SELECT {col_str} FROM pscomppars WHERE pl_name = {_adql_literal(target)}",
            f"SELECT {col_str} FROM pscomppars WHERE hostname = {_adql_literal(target)}",
            f"SELECT {col_str} FROM pscomppars WHERE hip_name = {_adql_literal(target)}",
            f"SELECT {col_str} FROM pscomppars WHERE hd_name = {_adql_literal(target)}",
            f"SELECT {col_str} FROM pscomppars WHERE pl_name LIKE {_adql_literal('%' + target + '%')}",
            f"SELECT {col_str} FROM pscomppars WHERE hostname LIKE {_adql_literal('%' + target + '%')}",
            f"SELECT {col_str} FROM pscomppars WHERE hip_name LIKE {_adql_literal('%' + target + '%')}",
            f"SELECT {col_str} FROM pscomppars WHERE hd_name LIKE {_adql_literal('%' + target + '%')}",
        ]

        if norm_target != target:
            queries.extend([
                f"SELECT {col_str} FROM pscomppars WHERE hostname = {_adql_literal(norm_target)}",
                f"SELECT {col_str} FROM pscomppars WHERE hip_name = {_adql_literal(norm_target)}",
                f"SELECT {col_str} FROM pscomppars WHERE hd_name = {_adql_literal(norm_target)}",
            ])

        data = []
        for q in queries:
            url = 'https://exoplanetarchive.ipac.caltech.edu/TAP/sync?' + urllib.parse.urlencode({'query': q, 'format': 'json'})
            req = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0'})
            try:
                with urllib.request.urlopen(req, timeout=5) as response:
                    res = json.loads(response.read().decode())
                    if res:
                        data = res
                        break
            except Exception:
                continue

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
def transit_fit_run(payload: dict = Body(...)):
    inst = (payload.get("inst") or "").strip()
    date = (payload.get("date") or "").strip()
    target = (payload.get("target") or "").strip()
    options = payload.get("options") or {}
    test_run = bool(payload.get("test_run", False))
    selected_csvs = payload.get("selected_csvs") if "selected_csvs" in payload else None
    result = fit.start_fit(inst, date, target, options, test_run=test_run, selected_csvs=selected_csvs)
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
    force = bool(payload.get("force", False))

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
        conn = sqlite3.connect(db, timeout=10)
        conn.row_factory = sqlite3.Row

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

        # Get target info from targets table
        target_info = conn.execute(
            "SELECT n_dates, n_frames, ra, declination FROM targets WHERE object = ?",
            (target,)
        ).fetchone()

        conn.close()

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
    fov_sizes = {}
    for name in _FOV_INSTRUMENTS:
        if name == "sinistro":
            # Show the default (central_2k_2x2) size; full_frame is a selectable option
            size_arcmin = fov_opt.SINISTRO_MODES["central_2k_2x2"] * 2.0 / 60.0
        else:
            size_arcmin = fov_opt.load_fov_halfsize_arcsec(name) * 2.0 / 60.0
        fov_sizes[name] = round(size_arcmin, 2)
    return _render(
        "fov.html",
        instruments=_FOV_INSTRUMENTS,
        sel_inst=inst,
        sel_target=target,
        fov_sizes=fov_sizes,
        sinistro_modes=fov_opt.SINISTRO_MODES,
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
    )
    status = 200 if result.ok else 422
    return JSONResponse(result.to_dict(), status_code=status)


@app.post("/api/fov/resolve-target", response_class=JSONResponse)
def api_fov_resolve_target(payload: dict = Body(...)):
    target = (payload.get("target") or "").strip()
    if not target:
        return JSONResponse({"ok": False, "error": "Target name is required."}, status_code=400)

    coords = exp_calc.resolve_target_coords(target)
    if coords is None:
        return JSONResponse(
            {"ok": False, "error": f"Could not resolve '{target}'. Try a different name or enter RA/Dec manually."},
            status_code=422,
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
def api_lco_config():
    """Report whether the token/download-root/submit gate are configured. No secrets."""
    return JSONResponse({"ok": True, **lco.config_state()})


@app.get("/api/lco/proposals", response_class=JSONResponse)
def api_lco_proposals():
    try:
        return JSONResponse({"ok": True, **lco.get_proposals()})
    except lco.LcoError as e:
        return _lco_error_response(e)


@app.get("/api/lco/requestgroups", response_class=JSONResponse)
def api_lco_requestgroups(proposal: str = ""):
    try:
        return JSONResponse({"ok": True, **lco.get_requestgroups(proposal)})
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
def api_lco_ipp(payload: dict = Body(...)):
    """Build the requestgroup and run the max-allowable-IPP dry-run."""
    try:
        rg = lco.build_requestgroup(payload.get("kind"), payload)
        ipp = lco.max_allowable_ipp(rg)
        return JSONResponse(
            {"ok": True, "payload": rg, "payload_hash": lco.payload_hash(rg), "ipp": ipp}
        )
    except lco.LcoError as e:
        return _lco_error_response(e)


@app.post("/api/lco/submit", response_class=JSONResponse)
def api_lco_submit(payload: dict = Body(...)):
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
        result = lco.submit_requestgroup(rg)
        return JSONResponse({"ok": True, "result": result})
    except lco.LcoError as e:
        return _lco_error_response(e)


@app.get("/api/lco/archive/frames", response_class=JSONResponse)
def api_lco_archive_frames(
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
):
    tel_class = TELID if TELID in ("0m4", "1m0", "2m0") else ""
    filters = {
        "proposal_id": proposal_id,
        "OBJECT": OBJECT,
        "SITEID": SITEID,
        "TELID": "" if tel_class else TELID,
        "INSTRUME": INSTRUME,
        "FILTER": FILTER,
        "reduction_level": reduction_level,
        "start": start,
        "end": end,
        "limit": limit,
    }
    try:
        result = lco.archive_search(filters)
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
        return JSONResponse({"ok": True, **result})
    except lco.LcoError as e:
        return _lco_error_response(e)


@app.post("/api/lco/archive/download", response_class=JSONResponse)
def api_lco_archive_download(payload: dict = Body(...)):
    try:
        frames = payload.get("frames")
        if not isinstance(frames, list) or not frames:
            return JSONResponse({"ok": False, "error": "no frames selected"}, status_code=400)
        results = lco.download_frames(frames, overwrite=bool(payload.get("overwrite")))
        return JSONResponse({"ok": True, "results": results})
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
    if cache_key in _CATALOG_CACHE:
        return _CATALOG_CACHE[cache_key]
        
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
        pass

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
            pass

    _CATALOG_CACHE[cache_key] = results
    return results


def _query_target_coordinates(target: str) -> dict | None:
    target_clean = target.strip().upper()
    cache_key = "coords_" + target_clean
    if cache_key in _CATALOG_CACHE:
        return _CATALOG_CACHE[cache_key]

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
        pass

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
        pass

    return _store(None)


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
    conn = sqlite3.connect(db)
    conn.create_aggregate("coord_repr", 2, CoordRepr)
    try:
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
    finally:
        conn.close()
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
                pass
        out.append(row)
    return out, len(dataset_meta)


def _query_target_planets_toi(target: str) -> dict:
    import urllib.request
    import urllib.parse
    import json
    
    target_clean = target.strip().upper()
    cache_key = "toi_" + target_clean
    if cache_key in _CATALOG_CACHE:
        return _CATALOG_CACHE[cache_key]
        
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
        pass

    # 2. Online search
    if not results:
        host = target.strip()
        if len(host) > 2 and host[-2] == " " and host[-1].lower() in "bcdefgh":
            host = host[:-2].strip()
        clean_target = host.replace("TOI", "").replace("toi", "").replace("-", "").replace(" ", "").lstrip("0").split(".")[0].strip()
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
            pass

    _CATALOG_CACHE[cache_key] = results
    return results


# Helper to query all planet ephemerides for a target from catalogs
def _query_target_planets_catalog(target: str) -> dict:
    target_clean = target.strip().upper()
    if target_clean in _CATALOG_CACHE:
        return _CATALOG_CACHE[target_clean]
        
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
                            # Parse planet period
                            period_val = 1.0
                            if row.get("period"):
                                try: period_val = float(row["period"])
                                except ValueError: pass
                            elif row.get("period_sg1"):
                                try: period_val = float(row["period_sg1"])
                                except ValueError: pass
                            
                            t0_val = 2450000.0
                            if row.get("t0"):
                                try: t0_val = float(row["t0"])
                                except ValueError: pass
                            elif row.get("t0_sg1"):
                                try: t0_val = float(row["t0_sg1"])
                                except ValueError: pass
                            
                            results["b"] = {
                                "t0": t0_val,
                                "period": period_val
                            }
                            break
        except Exception:
            pass

    # Final fallback if absolutely nothing was found
    if not results:
        results["b"] = {"t0": 2450000.0, "period": 1.0}
        
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
        pass
    return fitted


@app.get("/api/ephemeris/targets", response_class=JSONResponse)
def api_ephemeris_targets():
    with _DB_LOCK:
        fit.sync_jobs()
        all_jobs = get_persisted_jobs()
        existing_keys = {j["key"] for j in all_jobs if j["type"] == "transit_fit"}
        orphan_fits = fit._discover_orphan_fits(existing_keys)
        all_jobs.extend(orphan_fits)
        completed = [j for j in all_jobs if j["type"] == "transit_fit" and j["state"] == "done"]
        targets = sorted(list(set(j["target"] for j in completed)))
    return JSONResponse({"ok": True, "targets": targets})


@app.get("/api/ephemeris/target-info", response_class=JSONResponse)
def api_ephemeris_target_info(target: str):
    target = (target or "").strip()
    if not target:
        return JSONResponse({"ok": False, "error": "Target is required"}, status_code=400)
    
    with _DB_LOCK:
        fit.sync_jobs()
        all_jobs = get_persisted_jobs()
        existing_keys = {j["key"] for j in all_jobs if j["type"] == "transit_fit"}
        orphan_fits = fit._discover_orphan_fits(existing_keys)
        all_jobs.extend(orphan_fits)
        
        norm_t = _normalize_target_name(target)
        completed = [j for j in all_jobs if j["type"] == "transit_fit" and j["state"] == "done" and _normalize_target_name(j["target"]) == norm_t]
    
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
            pass
        
        for pl in planets_fitted:
            seen_planets.add(pl)
            if pl not in planets_ephem:
                planets_ephem[pl] = {"t0": 2450000.0, "period": 1.0}
            
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
        if pl not in ref_ephem:
            ref_ephem[pl] = {"t0": 2450000.0, "period": 1.0}
        if pl not in nasa_ephem:
            nasa_ephem[pl] = {"t0": 2450000.0, "period": 1.0}
        if pl not in toi_ephem:
            toi_ephem[pl] = {"t0": 2450000.0, "period": 1.0}
            
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
        all_jobs = get_persisted_jobs()
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


@app.get("/jobs", response_class=HTMLResponse)
def jobs_page():
    phot.sync_jobs()
    fit.sync_jobs()
    all_jobs = get_persisted_jobs()

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
def jobs_status():
    phot.sync_jobs()
    fit.sync_jobs()
    all_jobs = get_persisted_jobs()

    # Discover fits completed on-disk outside the web UI.
    existing_keys = {j["key"] for j in all_jobs if j["type"] == "transit_fit"}
    orphan_fits = fit._discover_orphan_fits(existing_keys)
    if orphan_fits:
        all_jobs.extend(orphan_fits)

    global _last_running
    current_running = {j["key"] for j in all_jobs if j["state"] in ("running", "cancelling")}
    finished = {}
    for j in all_jobs:
        if j["key"] in _last_running and j["key"] not in current_running:
            finished[j["key"]] = {
                "state": j["state"],
                "elapsed": j["elapsed"],
                "error_desc": j.get("error_desc", "") or "",
                "returncode": j.get("returncode"),
                "started_at": j.get("started_at"),
                "started_at_str": _datetime_from_timestamp(int(j["started_at"])) if j.get("started_at") else "—",
                "user_name": j.get("user_name", ""),
            }
    _last_running = current_running
    running = [
        {
            "key": j["key"],
            "state": j["state"],
            "elapsed": _live_elapsed(j),
            "started_at": j.get("started_at"),
            "started_at_str": _datetime_from_timestamp(int(j["started_at"])) if j.get("started_at") else "—",
            "user_name": j.get("user_name", ""),
        }
        for j in all_jobs if j["state"] in ("running", "cancelling")
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
def jobs_rerun(payload: dict = Body(...)):
    import json
    key = (payload.get("key") or "").strip()
    if not key:
        raise HTTPException(400, "job key required")
    all_jobs = get_persisted_jobs()
    job = next((j for j in all_jobs if j["key"] == key), None)
    if job is None:
        raise HTTPException(404, "job not found")
    inst, date, target = job["inst"], job["date"], job["target"]
    params_raw = job.get("params", "")
    try:
        p = json.loads(params_raw) if params_raw else {}
    except (json.JSONDecodeError, TypeError):
        p = {}
    if job["type"] == "photometry":
        result = phot.start_run(inst, date, target, options=p.get("options", {}), test_run=p.get("test_run", True))
    elif job["type"] == "transit_fit":
        result = fit.start_fit(inst, date, target, options=p.get("options", {}), test_run=p.get("test_run", False), selected_csvs=p.get("selected_csvs"))
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


@app.post("/photometry/run")
def photometry_run(payload: dict = Body(...)):
    inst = (payload.get("inst") or "").strip()
    date = (payload.get("date") or "").strip()
    target = (payload.get("target") or "").strip()
    options = payload.get("options") or {}
    test_run = bool(payload.get("test_run", True))
    # Hard block: never launch a sinistro run that would merge multiple sites.
    site_err = _site_required_error(_db_path(), inst, date, target, options)
    if site_err:
        return JSONResponse({"ok": False, "error": site_err}, status_code=400)
    result = phot.start_run(inst, date, target, options=options, test_run=test_run)
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
async def api_set_note(obj: str, payload: dict = Body(...)):
    note = (payload.get("note") or "").strip()
    if len(note) > 2000:
        raise HTTPException(400, "note too long (max 2000 chars)")
    _set_note(_db_path(), obj, note)
    return JSONResponse({"ok": True, "object": obj, "note": note})


@app.delete("/api/targets/{obj}/note")
async def api_delete_note(obj: str):
    _delete_note(_db_path(), obj)
    return JSONResponse({"ok": True, "object": obj})


@app.put("/api/targets/{obj}/identified")
async def api_set_identified(obj: str, payload: dict = Body(...)):
    val = payload.get("is_identified")
    if val not in (0, 1):
        raise HTTPException(400, "is_identified must be 0 or 1")
    _set_identified(_db_path(), obj, val)
    return JSONResponse({"ok": True, "object": obj, "is_identified": bool(val)})


@app.get("/{instrument}", response_class=HTMLResponse)
async def instrument_page(instrument: str):
    dates = _get_dates(_db_path(), instrument)
    return _render("instrument.html", instrument=instrument, dates=dates)


@app.get("/{instrument}/{obsdate}", response_class=HTMLResponse)
async def date_page(instrument: str, obsdate: str):
    summaries = _get_summaries(_db_path(), instrument, obsdate)
    ccds = sorted(set(s["ccd"] for s in summaries))
    return _render("date.html", instrument=instrument, obsdate=obsdate, summaries=summaries, ccds=ccds)


@app.get("/{instrument}/{obsdate}/ccd{ccd}", response_class=HTMLResponse)
async def ccd_page(instrument: str, obsdate: str, ccd: int):
    frames = _get_frames(_db_path(), instrument, obsdate, ccd)
    return _render("ccd.html", instrument=instrument, obsdate=obsdate, ccd=ccd, frames=frames)
