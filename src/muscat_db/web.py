from __future__ import annotations

import os
import pathlib
import sqlite3

from fastapi import Body, FastAPI, HTTPException
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from jinja2 import Environment, FileSystemLoader

from muscat_db import photometry as phot
from muscat_db.database import (
    SCHEMA,
    delete_note as _delete_note,
    get_dates as _get_dates,
    get_frames as _get_frames,
    get_instruments as _get_instruments,
    get_objects as _get_objects,
    get_summaries as _get_summaries,
    get_targets as _get_targets,
    set_note as _set_note,
)
from muscat_db.instruments import INSTRUMENTS

HERE = pathlib.Path(__file__).parent
TEMPLATE_DIR = HERE / "templates"

app = FastAPI(title="MuSCAT Observation Log")
# The targets page is ~2.8 MB of highly repetitive HTML; gzip shrinks it ~16x,
# which is the dominant cost when serving over an SSH port-forward tunnel.
app.add_middleware(GZipMiddleware, minimum_size=1000)

jinja = Environment(
    loader=FileSystemLoader(str(TEMPLATE_DIR)),
    autoescape=True,
)


@app.on_event("startup")
def _ensure_db():
    """Create the database and schema if they don't exist."""
    db = _db_path()
    conn = sqlite3.connect(db)
    conn.executescript(SCHEMA)
    conn.close()
    print(f"[startup] database ready at {db}")


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
    key = _db_mtime(db)
    cached = _index_cache.get("index")
    if cached is not None and cached[0] == key:
        return HTMLResponse(cached[1])

    with_data = {row["name"] for row in _get_instruments(db)}
    instruments = [
        {"name": name, "has_data": name in with_data}
        for name in INSTRUMENTS
    ]
    targets = _get_targets(db)
    html = jinja.get_template("index.html").render(
        instruments=instruments, targets=targets,
    )
    _index_cache["index"] = (key, html)
    return HTMLResponse(html)


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
    if inst and date and target:
        outputs = phot.list_outputs(inst, date, target)
        command = phot.command_str(inst, date, target, test_run=False)
        raw_missing = not phot.raw_data_dir(inst, date).is_dir()
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
    )


@app.get("/photometry/file/{inst}/{date}/{name}")
def photometry_file(inst: str, date: str, name: str):
    path = phot.safe_artifact_path(inst, date, name)
    if path is None:
        raise HTTPException(404, "artifact not found")
    return FileResponse(str(path))


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
