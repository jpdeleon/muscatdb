from __future__ import annotations

import os
import pathlib

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse
from jinja2 import Environment, FileSystemLoader

import sqlite3

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse
from jinja2 import Environment, FileSystemLoader

from muscat_db.database import (
    SCHEMA,
    get_dates as _get_dates,
    get_frames as _get_frames,
    get_instruments as _get_instruments,
    get_summaries as _get_summaries,
    get_targets as _get_targets,
)
from muscat_db.instruments import INSTRUMENTS

HERE = pathlib.Path(__file__).parent
TEMPLATE_DIR = HERE / "templates"

app = FastAPI(title="MuSCAT Observation Log")

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


@app.get("/", response_class=HTMLResponse)
async def index():
    db = _db_path()
    with_data = {row["name"] for row in _get_instruments(db)}
    instruments = [
        {"name": name, "has_data": name in with_data}
        for name in INSTRUMENTS
    ]
    targets = _get_targets(db)
    return _render("index.html", instruments=instruments, targets=targets)


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
