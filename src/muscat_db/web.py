from __future__ import annotations

import datetime
import os
import pathlib
import re
import sqlite3

import csv
import io

from fastapi import Body, FastAPI, HTTPException
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from jinja2 import Environment, FileSystemLoader
from contextlib import asynccontextmanager

from muscat_db import photometry as phot
from muscat_db import transit_fit as fit
from muscat_db.database import (
    SCHEMA,
    delete_note as _delete_note,
    get_dates as _get_dates,
    get_frames as _get_frames,
    get_instruments as _get_instruments,
    get_instruments_summary as _get_instruments_summary,
    get_objects as _get_objects,
    get_summaries as _get_summaries,
    get_targets as _get_targets,
    set_note as _set_note,
    get_persisted_jobs,
    get_last_build_date,
)
from muscat_db.instruments import INSTRUMENTS

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
    last_updated = get_last_build_date(db)

    html = jinja.get_template("index.html").render(
        targets=targets,
        last_updated=last_updated,
    )
    _index_cache["index"] = (key, html)
    return HTMLResponse(html)


@app.get("/logs", response_class=HTMLResponse)
async def logs_page():
    db = _db_path()
    with_data = {row["name"] for row in _get_instruments(db)}
    instruments = [
        {"name": name, "has_data": name in with_data}
        for name in INSTRUMENTS
    ]
    summaries = _get_instruments_summary(db)
    return _render(
        "logs.html",
        instruments=instruments,
        summaries=summaries,
    )


@app.get("/workflow", response_class=HTMLResponse)
async def workflow_page():
    return _render("workflow.html")


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


@app.get("/photometry", response_class=HTMLResponse)
def photometry_page(inst: str = "", date: str = "", target: str = ""):
    db = _db_path()
    inst = inst if inst in INSTRUMENTS else ""
    date = date if phot.valid_date(date) else ""
    target = (target or "").strip()

    dates: list[str] = []
    targets: list[str] = []
    outputs = None
    previews: dict[str, dict] = {}
    command = ""
    raw_missing = False

    if inst:
        date_set = {d["obsdate"] for d in _get_dates(db, inst)}
        date_set.update(phot.output_dates(inst))
        dates = sorted(date_set, reverse=True)
    if inst and date:
        obj_set = set(_get_objects(db, inst, date))
        obj_set.update(phot.discovered_targets(inst, date))
        targets = sorted(obj_set)
    obs_type = ""
    is_narrowband = False
    if inst and date and target:
        outputs = phot.list_outputs(inst, date, target)
        command = phot.command_str(inst, date, target, test_run=False)
        raw_missing = not phot.raw_data_dir(inst, date).is_dir()

        try:
            conn = sqlite3.connect(db)
            cur = conn.execute(
                "SELECT DISTINCT filter FROM frames WHERE instrument = ? AND obsdate = ? AND object = ? AND filter IS NOT NULL AND filter != ''",
                (inst, date, target),
            )
            filters = [row[0] for row in cur.fetchall()]
            conn.close()
            if filters:
                is_narrowband = any("narrow" in f.lower() or f.lower() == "na_d" for f in filters)
                obs_type = "(narrowband)" if is_narrowband else "(broadband)"
        except Exception:
            pass

        # fall through; previews computed below when outputs exist
        if outputs["has_any"]:
            rdir = phot.results_dir(inst, date)
            for band, prods in outputs["bands"].items():
                if prods.get("csv"):
                    headers, rows = phot.csv_preview(rdir / prods["csv"], n=8)
                    previews[band] = {"headers": headers, "rows": rows}

    return _render(
        "photometry.html",
        instruments=list(INSTRUMENTS),
        sel_inst=inst, sel_date=date, sel_target=target,
        dates=dates, targets=targets,
        outputs=outputs, previews=previews,
        command=command, raw_missing=raw_missing,
        default_bands=phot.DEFAULT_BANDS,
        run_defaults=phot.RUN_DEFAULTS,
        wiki_url=_wiki_url(inst, target),
        obs_type=obs_type,
        is_narrowband=is_narrowband,
    )


@app.get("/transit-fit", response_class=HTMLResponse)
def transit_fit_page(inst: str = "", date: str = "", target: str = ""):
    db = _db_path()
    inst = inst if inst in INSTRUMENTS else ""
    date = date if phot.valid_date(date) else ""
    target = (target or "").strip()

    dates: list[str] = []
    targets: list[str] = []
    outputs = None
    csvs = []
    target_params = {}

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
        csvs = []
        for c in fit.get_csv_lightcurves(inst, date, target):
            try:
                mtime = c.stat().st_mtime
                dt = datetime.datetime.fromtimestamp(mtime)
                created_at = dt.strftime('%Y-%m-%d %H:%M:%S')
            except Exception:
                created_at = "Unknown"
            csvs.append({
                "name": c.name,
                "created_at": created_at
            })
        outputs = fit.get_fit_outputs(inst, date, target)
        target_params = fit.get_target_parameters(target)

    return _render(
        "transit_fit.html",
        instruments=list(INSTRUMENTS),
        sel_inst=inst, sel_date=date, sel_target=target,
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

    target = target.strip()

    def get_unc(err1, err2):
        if err1 is None and err2 is None:
            return None
        val1 = abs(err1) if err1 is not None else 0.0
        val2 = abs(err2) if err2 is not None else 0.0
        return max(val1, val2)

    if source == "toi":
        cols = [
            "toi", "toidisplay",
            "st_teff", "st_tefferr1", "st_tefferr2",
            "st_logg", "st_loggerr1", "st_loggerr2",
            "pl_orbper", "pl_orbpererr1", "pl_orbpererr2",
            "pl_tranmid", "pl_tranmiderr1", "pl_tranmiderr2",
            "pl_trandurh", "pl_trandurherr1", "pl_trandurherr2",
        ]
        col_str = ", ".join(cols)

        clean_target = target.replace("TOI", "").replace("toi", "").replace("-", "").replace(" ", "").strip()
        queries = [
            f"SELECT {col_str} FROM toi WHERE toi = '{clean_target}'",
            f"SELECT {col_str} FROM toi WHERE toidisplay LIKE '%{target}%'",
            f"SELECT {col_str} FROM toi WHERE toi LIKE '%{clean_target}%'",
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

        queries = [
            f"SELECT {col_str} FROM pscomppars WHERE pl_name = '{target}'",
            f"SELECT {col_str} FROM pscomppars WHERE hostname = '{target}'",
            f"SELECT {col_str} FROM pscomppars WHERE pl_name LIKE '%{target}%'",
            f"SELECT {col_str} FROM pscomppars WHERE hostname LIKE '%{target}%'"
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
def transit_fit_status(inst: str, date: str, target: str):
    return JSONResponse(fit.job_status(inst, date, target))


@app.post("/transit-fit/run")
def transit_fit_run(payload: dict = Body(...)):
    inst = (payload.get("inst") or "").strip()
    date = (payload.get("date") or "").strip()
    target = (payload.get("target") or "").strip()
    options = payload.get("options") or {}
    test_run = bool(payload.get("test_run", False))
    selected_csvs = payload.get("selected_csvs") or None
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
    selected_csvs = payload.get("selected_csvs") or None
    result = fit.compute_logp(inst, date, target, options, selected_csvs=selected_csvs)
    if not result.get("ok"):
        return JSONResponse(result, status_code=400)
    return JSONResponse(result)


@app.post("/transit-fit/cancel")
def transit_fit_cancel(payload: dict = Body(...)):
    inst = (payload.get("inst") or "").strip()
    date = (payload.get("date") or "").strip()
    target = (payload.get("target") or "").strip()
    result = fit.cancel_fit(inst, date, target)
    if not result.get("ok"):
        return JSONResponse(result, status_code=400)
    return JSONResponse(result)


@app.get("/transit-fit/file/{inst}/{date}/{target}/{name}")
def transit_fit_file(inst: str, date: str, target: str, name: str):
    if inst not in INSTRUMENTS or not phot.valid_date(date):
        raise HTTPException(404, "invalid parameters")
    if ".." in name or "/" in name:
        raise HTTPException(400, "invalid filename")

    rdir = fit.fit_output_dir(inst, date, target)
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


@app.get("/jobs", response_class=HTMLResponse)
def jobs_page():
    phot.sync_jobs()
    fit.sync_jobs()
    all_jobs = get_persisted_jobs()
    phot_jobs = [j for j in all_jobs if j["type"] == "photometry"]
    fit_jobs = [j for j in all_jobs if j["type"] == "transit_fit"]

    # Discover fits completed on-disk outside the web UI.
    existing_keys = {j["key"] for j in fit_jobs}
    orphan_fits = fit._discover_orphan_fits(existing_keys)
    if orphan_fits:
        fit_jobs.extend(orphan_fits)
        fit_jobs.sort(key=lambda j: j.get("started_at", 0), reverse=True)

    return _render("jobs.html", phot_jobs=phot_jobs, fit_jobs=fit_jobs)


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
    error = phot.validate_run_options(phot.normalize_run_options(options))
    command = phot.command_str(inst, date, target, options=options, test_run=test_run)
    return JSONResponse({"command": command, "error": error})


@app.get("/photometry/status")
def photometry_status(inst: str, date: str, target: str):
    return JSONResponse(phot.job_status(inst, date, target))


@app.post("/photometry/cancel")
def photometry_cancel(payload: dict = Body(...)):
    inst = (payload.get("inst") or "").strip()
    date = (payload.get("date") or "").strip()
    target = (payload.get("target") or "").strip()
    result = phot.cancel_run(inst, date, target)
    if not result.get("ok"):
        return JSONResponse(result, status_code=400)
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
