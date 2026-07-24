from __future__ import annotations

import csv
import base64
import hashlib
import json
import logging
import os
import re
import datetime
import sqlite3
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager

from cryptography.fernet import Fernet, InvalidToken

from muscat_db.instruments import INSTRUMENTS, OBSLOG_BASE
from muscat_db.cache import clear_all_caches
from muscat_db.coord import CoordRepr, unpack as _unpack_coord

logger = logging.getLogger(__name__)


def format_elapsed(seconds: int) -> str:
    if seconds < 60:
        return f"{seconds}s"
    minutes = seconds // 60
    secs = seconds % 60
    if minutes < 60:
        return f"{minutes}m {secs}s"
    hours = minutes // 60
    mins = minutes % 60
    if hours < 24:
        return f"{hours}h {mins}m"
    days = hours // 24
    hrs = hours % 24
    return f"{days}d {hrs}h"



SCHEMA = """
CREATE TABLE IF NOT EXISTS frames (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    instrument  TEXT NOT NULL,
    obsdate     TEXT NOT NULL,
    ccd         INTEGER NOT NULL,
    filename    TEXT NOT NULL,
    object      TEXT,
    jd_start    REAL,
    ut_start    TEXT,
    exptime     REAL,
    read_mode   TEXT,
    filter      TEXT,
    ra          TEXT,
    declination TEXT,
    airmass     REAL,
    focus       REAL,
    pa          REAL
);

CREATE INDEX IF NOT EXISTS idx_frames_inst_date ON frames(instrument, obsdate);
CREATE INDEX IF NOT EXISTS idx_frames_object   ON frames(object);

CREATE TABLE IF NOT EXISTS summaries (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    instrument  TEXT NOT NULL,
    obsdate     TEXT NOT NULL,
    ccd         INTEGER NOT NULL,
    object      TEXT,
    exptime     REAL,
    read_mode   TEXT,
    telescope   TEXT,
    frame_start TEXT,
    frame_end   TEXT,
    ut_start    TEXT,
    ut_end      TEXT,
    nframes     INTEGER,
    filter      TEXT,
    ra          TEXT,
    declination TEXT,
    airmass_min REAL,
    airmass_max REAL
);

CREATE INDEX IF NOT EXISTS idx_summaries_inst_date ON summaries(instrument, obsdate);

CREATE TABLE IF NOT EXISTS targets (
    object        TEXT PRIMARY KEY,
    n_dates       INTEGER NOT NULL,
    n_frames      INTEGER NOT NULL,
    instruments   TEXT,
    dates         TEXT,
    inst_dates    TEXT,
    filters       TEXT,
    total_exptime REAL,
    ra            TEXT,
    declination   TEXT,
    airmass_min   REAL,
    airmass_max   REAL,
    is_identified INTEGER NOT NULL DEFAULT 1,
    phot_status   TEXT NOT NULL DEFAULT 'none',
    fit_status    TEXT NOT NULL DEFAULT 'none'
);

CREATE TABLE IF NOT EXISTS target_notes (
    object     TEXT PRIMARY KEY,
    note       TEXT NOT NULL,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS target_overrides (
    object        TEXT PRIMARY KEY,
    is_identified INTEGER NOT NULL,
    norm_name     TEXT,
    updated_at    TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS jobs (
    key          TEXT PRIMARY KEY,
    type         TEXT NOT NULL,
    instrument   TEXT NOT NULL,
    obsdate      TEXT NOT NULL,
    target       TEXT NOT NULL,
    state        TEXT NOT NULL,
    returncode   INTEGER,
    elapsed      INTEGER NOT NULL,
    started_at   REAL NOT NULL,
    error_desc   TEXT,
    run_type     TEXT NOT NULL DEFAULT '',
    params       TEXT NOT NULL DEFAULT '',
    run_id       TEXT NOT NULL DEFAULT '',
    run_name     TEXT NOT NULL DEFAULT '',
    user_name    TEXT NOT NULL DEFAULT ''
);

CREATE INDEX IF NOT EXISTS idx_jobs_state_started
ON jobs(state, started_at DESC);

CREATE TABLE IF NOT EXISTS users (
    username      TEXT PRIMARY KEY,
    password_hash TEXT NOT NULL DEFAULT '',
    display_name  TEXT NOT NULL DEFAULT '',
    is_admin      INTEGER NOT NULL DEFAULT 0,
    created_at    TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    last_login    TEXT,
    settings      TEXT NOT NULL DEFAULT '{}'
);

CREATE TABLE IF NOT EXISTS db_meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS exposure_coeffs (
    instrument  TEXT NOT NULL,
    band        TEXT NOT NULL,
    focus_mm    REAL NOT NULL,
    coef        REAL NOT NULL,
    fwhm_pix    REAL NOT NULL,
    n_frames    INTEGER NOT NULL DEFAULT 0,
    updated_at  TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (instrument, band, focus_mm)
);

CREATE TABLE IF NOT EXISTS exposure_jobs (
    id          TEXT PRIMARY KEY,
    instrument  TEXT NOT NULL,
    state       TEXT NOT NULL DEFAULT 'pending',
    progress    TEXT NOT NULL DEFAULT '',
    started_at  REAL NOT NULL,
    updated_at  REAL NOT NULL
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_exposure_jobs_active_instrument
ON exposure_jobs(instrument)
WHERE state IN ('pending', 'running', 'cancelling');

-- App-owned closed-loop observation records are intentionally outside the
-- frames/summaries/targets rebuild set and therefore survive daily rescans.
CREATE TABLE IF NOT EXISTS test_observations (
    id                  TEXT PRIMARY KEY,
    target              TEXT NOT NULL,
    instrument          TEXT NOT NULL,
    site                TEXT NOT NULL,
    transit_json        TEXT NOT NULL DEFAULT '{}',
    plan_json           TEXT NOT NULL,
    result_json         TEXT NOT NULL DEFAULT '{}',
    request_ids_json    TEXT NOT NULL DEFAULT '[]',
    payload_hash        TEXT NOT NULL DEFAULT '',
    state               TEXT NOT NULL DEFAULT 'draft',
    analysis_version    TEXT NOT NULL,
    recommendation_json TEXT NOT NULL DEFAULT '{}',
    failure_detail      TEXT NOT NULL DEFAULT '',
    created_at          TEXT NOT NULL,
    updated_at          TEXT NOT NULL
);

-- Every child Request accepted by LCO through the scheduler is monitored until
-- its final BANZAI products have been downloaded, scanned, and ingested.  Keep
-- this app-owned state outside the daily frames/summaries rebuild set.
CREATE TABLE IF NOT EXISTS lco_observation_requests (
    request_id          INTEGER PRIMARY KEY,
    requestgroup_id     INTEGER NOT NULL,
    name                TEXT NOT NULL DEFAULT '',
    proposal            TEXT NOT NULL DEFAULT '',
    target              TEXT NOT NULL DEFAULT '',
    instrument          TEXT NOT NULL DEFAULT '',
    user_name           TEXT NOT NULL DEFAULT '',
    request_state       TEXT NOT NULL DEFAULT 'PENDING',
    monitor_state       TEXT NOT NULL DEFAULT 'monitoring',
    window_start        TEXT NOT NULL DEFAULT '',
    window_end          TEXT NOT NULL DEFAULT '',
    payload_json        TEXT NOT NULL DEFAULT '{}',
    result_json         TEXT NOT NULL DEFAULT '{}',
    raw_frame_count     INTEGER NOT NULL DEFAULT 0,
    reduced_frame_count INTEGER NOT NULL DEFAULT 0,
    downloaded_count    INTEGER NOT NULL DEFAULT 0,
    download_job_id     TEXT NOT NULL DEFAULT '',
    next_poll_at        REAL NOT NULL DEFAULT 0,
    unchanged_polls     INTEGER NOT NULL DEFAULT 0,
    error_count         INTEGER NOT NULL DEFAULT 0,
    last_error          TEXT NOT NULL DEFAULT '',
    last_polled_at      REAL,
    terminal_seen_at    REAL,
    created_at          REAL NOT NULL,
    updated_at          REAL NOT NULL,
    completed_at        REAL
);

CREATE INDEX IF NOT EXISTS idx_lco_observation_due
    ON lco_observation_requests(monitor_state, next_poll_at);

CREATE TABLE IF NOT EXISTS lco_observation_frames (
    request_id      INTEGER NOT NULL,
    frame_id        TEXT NOT NULL,
    observation_id  TEXT NOT NULL DEFAULT '',
    filename        TEXT NOT NULL,
    instrument      TEXT NOT NULL,
    obsdate         TEXT NOT NULL,
    state           TEXT NOT NULL DEFAULT 'pending',
    error           TEXT NOT NULL DEFAULT '',
    metadata_json   TEXT NOT NULL,
    updated_at      REAL NOT NULL,
    PRIMARY KEY (request_id, frame_id),
    FOREIGN KEY (request_id) REFERENCES lco_observation_requests(request_id)
        ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS lco_monitor_leases (
    name        TEXT PRIMARY KEY,
    owner       TEXT NOT NULL,
    expires_at  REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS ephemeris_views (
    slug         TEXT PRIMARY KEY,
    state_hash   TEXT NOT NULL,
    state_json   TEXT NOT NULL,
    targets_json TEXT NOT NULL,
    created_at   TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at   TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS exofop_cache (
    tic_id                 TEXT PRIMARY KEY,
    has_confirmed_planets  INTEGER NOT NULL,
    confirmed_planets      TEXT NOT NULL DEFAULT '',
    updated_at             TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

-- Cross-process/cross-server concurrency gate for each pipeline's full-job
-- cap (architecture audit finding: _MAX_FULL_JOBS was an in-memory-only
-- per-process dict, already wrong under --workers N>1). One row per
-- currently-claimed slot; a pipeline holds at most max_slots rows at once,
-- enforced by job_store.DatabaseJobStore.claim_slot's atomic INSERT.
CREATE TABLE IF NOT EXISTS job_concurrency_slots (
    pipeline    TEXT NOT NULL,
    holder_key  TEXT NOT NULL,
    claimed_at  REAL NOT NULL,
    PRIMARY KEY (pipeline, holder_key)
);

-- App-owned team chat. Kept outside the daily frames/summaries/targets rebuild
-- set (see _APP_OWNED_TABLES) so conversation history survives daily rescans.
-- `mentions` is a JSON array of @-mentioned usernames so recipients can be
-- re-highlighted when history is backfilled. Ephemeral (@test) messages are
-- never written here.
CREATE TABLE IF NOT EXISTS chat_messages (
    id          INTEGER PRIMARY KEY,
    user_name   TEXT NOT NULL DEFAULT '',
    text        TEXT NOT NULL,
    kind        TEXT NOT NULL DEFAULT 'user',
    created_at  REAL NOT NULL,
    edited_at   REAL,
    mentions    TEXT NOT NULL DEFAULT '[]'
);
CREATE INDEX IF NOT EXISTS idx_chat_messages_created ON chat_messages(created_at);

CREATE TABLE IF NOT EXISTS chat_reactions (
    message_id  INTEGER NOT NULL,
    user_name   TEXT NOT NULL,
    emoji       TEXT NOT NULL,
    created_at  REAL NOT NULL,
    PRIMARY KEY (message_id, user_name, emoji)
);
"""

# Idempotent schema migrations for columns added after initial deployment.
# Each migration is attempted unconditionally; "duplicate column" errors are
# silently swallowed so the function is safe to call multiple times.
_MIGRATIONS = [
    # 2026-07-12: group simultaneous multi-telescope sinistro nights separately
    "ALTER TABLE summaries ADD COLUMN telescope TEXT",
    # 2026-07-24: user override for normalized target names
    "ALTER TABLE target_overrides ADD COLUMN norm_name TEXT",
]


def _migrate_schema(conn: sqlite3.Connection) -> None:
    """Apply any pending schema migrations idempotently."""
    for sql in _MIGRATIONS:
        try:
            conn.execute(sql)
        except sqlite3.OperationalError:
            pass  # column already exists


def _apply_schema(conn: sqlite3.Connection) -> None:
    """Create all tables (idempotent) then apply any pending column migrations."""
    conn.executescript(SCHEMA)
    _migrate_schema(conn)


def _canonical_json(value) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def ephemeris_view_slug(state: dict) -> tuple[str, str, str]:
    """Return deterministic slug, hex hash, and canonical JSON for a view state."""
    state_json = _canonical_json(state)
    digest = hashlib.sha256(state_json.encode("utf-8")).digest()
    slug = base64.urlsafe_b64encode(digest).decode("ascii").rstrip("=")[:16]
    state_hash = hashlib.sha256(state_json.encode("utf-8")).hexdigest()
    return slug, state_hash, state_json


def _discover_csv_jobs(instrument: str | None = None, obsdate: str | None = None) -> list[tuple[str, str, str, int]]:
    """Return obslog CSV jobs as ``(inst, obsdate, path, ccd)`` tuples."""
    csv_jobs: list[tuple[str, str, str, int]] = []
    instruments = [instrument] if instrument else list(INSTRUMENTS)
    for inst_name in instruments:
        inst_dir = f"{OBSLOG_BASE}/{inst_name}"
        if not os.path.isdir(inst_dir):
            continue
        date_entries = [obsdate] if obsdate else sorted(os.listdir(inst_dir))
        for entry in date_entries:
            obsdir = f"{inst_dir}/{entry}"
            if not os.path.isdir(obsdir):
                continue
            # Only canonical YYMMDD obslog directories should ever be ingested
            # into the database. Legacy folders like ``csv_old_220914`` or
            # free-text labels like ``Hyades`` must remain on disk for
            # provenance/debugging, but they are not valid observation dates.
            if not _is_obsdate(entry):
                continue
            for fname in sorted(os.listdir(obsdir)):
                if not fname.endswith(".csv") or not fname.startswith("obslog-"):
                    continue
                try:
                    ccd = int(fname.rstrip(".csv").rsplit("-ccd", 1)[1])
                except (IndexError, ValueError):
                    continue
                csv_jobs.append((inst_name, entry, f"{obsdir}/{fname}", ccd))
    return csv_jobs


def _read_frame_rows(inst_name: str, obsdate: str, csv_path: str, ccd: int) -> list[tuple]:
    inst_cfg = INSTRUMENTS[inst_name]
    airmass_key = inst_cfg.airmass_key
    focus_key = inst_cfg.focus_label
    pa_key = "PA (deg)" if inst_cfg.has_pa else None

    rows_to_insert = []
    with open(csv_path) as f:
        reader = csv.DictReader(f)
        for row in reader:
            rows_to_insert.append((
                inst_name, obsdate, ccd,
                row.get("FRAME", ""),
                row.get("OBJECT", ""),
                _safe_float(row.get("JD-STRT", "0")),
                row.get("UT-STRT", ""),
                _safe_float(row.get("EXPTIME (s)", "0")),
                row.get("READ_MODE", ""),
                row.get("FILTER", ""),
                row.get("RA", ""),
                row.get("DEC", ""),
                _safe_float(row.get(airmass_key, "0")),
                _safe_float(row.get(focus_key, "0")),
                _safe_float(row.get(pa_key, "0")) if pa_key else None,
            ))
    return rows_to_insert


def _ingest_csv_jobs(conn: sqlite3.Connection, csv_jobs: list[tuple[str, str, str, int]], progress=None) -> int:
    ingest_task = None
    if progress is not None:
        ingest_task = progress.add_task(
            "[cyan]Ingesting CSVs[/]", total=len(csv_jobs), filename="",
        )

    count = 0
    for inst_name, obsdate, csv_path, ccd in csv_jobs:
        rows_to_insert = _read_frame_rows(inst_name, obsdate, csv_path, ccd)
        if rows_to_insert:
            conn.executemany(
                """INSERT INTO frames
                   (instrument, obsdate, ccd, filename, object, jd_start, ut_start,
                    exptime, read_mode, filter, ra, declination, airmass, focus, pa)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                rows_to_insert,
            )
            count += len(rows_to_insert)
        if progress is not None:
            progress.update(ingest_task, advance=1, filename=os.path.basename(csv_path))
    return count


def _summary_rows(conn: sqlite3.Connection, *, instrument: str | None = None, obsdate: str | None = None) -> list[tuple]:
    where = []
    params: list[str] = []
    if instrument is not None:
        where.append("instrument = ?")
        params.append(instrument)
    if obsdate is not None:
        where.append("obsdate = ?")
        params.append(obsdate)
    where_sql = f" WHERE {' AND '.join(where)}" if where else ""
    # Telescope is a Sinistro-only grouping dimension.  Extract its physical
    # telescope/camera prefix from an LCO filename (e.g. ``lsc1m005`` from
    # ``lsc1m005-fa15-20250806-0058-e91``).  A malformed Sinistro filename is
    # kept as its own group rather than silently combining different physical
    # telescopes.  MuSCAT filenames must use one shared empty key: using the
    # whole filename here makes every frame a separate "summary" row.
    raw = conn.execute(
        f"""WITH keyed AS (
               SELECT *,
                      CASE
                          WHEN instrument = 'sinistro' AND INSTR(filename, '-') > 0
                          THEN SUBSTR(filename, 1, INSTR(filename, '-') - 1)
                          WHEN instrument = 'sinistro' THEN filename
                          ELSE ''
                      END AS telescope
               FROM frames
               {where_sql}
           ),
           ranked AS (
               SELECT *,
                      ROW_NUMBER() OVER (
                          PARTITION BY instrument, obsdate, ccd, object,
                                       ROUND(exptime, 1), read_mode, telescope
                          ORDER BY jd_start ASC
                      ) as rn_asc,
                      ROW_NUMBER() OVER (
                          PARTITION BY instrument, obsdate, ccd, object,
                                       ROUND(exptime, 1), read_mode, telescope
                          ORDER BY jd_start DESC
                      ) as rn_desc
               FROM keyed
           )
           SELECT
               f1.instrument, f1.obsdate, f1.ccd, f1.object, ROUND(f1.exptime, 1) AS exptime, f1.read_mode,
               f1.telescope,
               f1.filename AS frame_start,
               f2.filename AS frame_end,
               f1.ut_start AS ut_start,
               f2.ut_start AS ut_end,
               stats.nframes,
               stats.filter,
               stats.coord,
               stats.airmass_min,
               stats.airmass_max
           FROM ranked f1
           JOIN ranked f2 ON
               f1.instrument = f2.instrument AND
               f1.obsdate    = f2.obsdate    AND
               f1.ccd        = f2.ccd        AND
               f1.object     = f2.object     AND
               ROUND(f1.exptime, 1) = ROUND(f2.exptime, 1) AND
               f1.read_mode  = f2.read_mode  AND
               f1.telescope  = f2.telescope  AND
               f1.rn_asc = 1 AND f2.rn_desc = 1
           JOIN (
               SELECT instrument, obsdate, ccd, object,
                      ROUND(exptime, 1) AS exptime_grp, read_mode,
                      telescope,
                      COUNT(*) AS nframes,
                      MAX(filter) AS filter,
                      coord_repr(ra, declination) AS coord,
                      MIN(NULLIF(airmass, 0)) AS airmass_min,
                      MAX(NULLIF(airmass, 0)) AS airmass_max
               FROM keyed
               GROUP BY instrument, obsdate, ccd, object,
                        ROUND(exptime, 1), read_mode, telescope
           ) stats ON
               f1.instrument = stats.instrument AND
               f1.obsdate    = stats.obsdate    AND
               f1.ccd        = stats.ccd        AND
               f1.object     = stats.object     AND
               ROUND(f1.exptime, 1) = stats.exptime_grp AND
               f1.read_mode  = stats.read_mode  AND
               f1.telescope  = stats.telescope""",
        params,
    ).fetchall()
    # columns: instrument, obsdate, ccd, object, exptime, read_mode,
    #          telescope, frame_start, frame_end, ut_start, ut_end,
    #          nframes, filter, coord, airmass_min, airmass_max  (16 cols)
    return [(*r[:13], *_unpack_coord(r[13]), r[14], r[15]) for r in raw]


def _insert_summary_rows(conn: sqlite3.Connection, rows: list[tuple]) -> None:
    if not rows:
        return
    conn.executemany(
        """INSERT INTO summaries
           (instrument, obsdate, ccd, object, exptime, read_mode,
            telescope, frame_start, frame_end, ut_start, ut_end, nframes,
            filter, ra, declination, airmass_min, airmass_max)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        rows,
    )


def _target_rows(conn: sqlite3.Connection, objects: set[str] | None = None) -> list[tuple]:
    where = [
        "object IS NOT NULL",
        "TRIM(object) <> ''",
        f"LOWER(TRIM(object)) NOT IN ({_TARGET_EXACT_CLAUSE})",
        "LOWER(TRIM(object)) NOT LIKE '%flat%'",
        "LOWER(TRIM(object)) NOT LIKE 'dark%'",
        "LOWER(TRIM(object)) NOT LIKE 'bias%'",
        "LOWER(TRIM(object)) NOT LIKE 'muscat commission%'",
        "LOWER(TRIM(object)) NOT LIKE '%test%'",
        "LOWER(TRIM(object)) NOT LIKE '%pinhole%'",
        "LOWER(TRIM(object)) NOT LIKE '%pointing%'",
        "LOWER(TRIM(object)) NOT LIKE '%dust_spot%'",
        "LOWER(TRIM(object)) NOT LIKE '%dust spot%'",
        "LOWER(TRIM(object)) NOT LIKE '%domeflat%'",
        "LOWER(TRIM(object)) NOT LIKE '%dome flat%'",
        "TRIM(object) NOT GLOB '*:*:*'",
        "TRIM(object) NOT GLOB '[0-9]*.[0-9]*'",
        "TRIM(object) NOT GLOB '[Pp][0-9]'",
        "TRIM(object) NOT GLOB '[Pp][0-9][0-9]'",
        "TRIM(object) NOT GLOB '[Pp][0-9][0-9][0-9]'",
        "TRIM(object) NOT GLOB '[Pp][0-9][0-9][0-9][0-9]'",
    ]
    params: list[str] = []
    if objects:
        where.append(f"object IN ({', '.join('?' for _ in objects)})")
        params.extend(sorted(objects))
    cur = conn.execute(
        f"""SELECT
              object,
              COUNT(DISTINCT obsdate)            AS n_dates,
              SUM(nframes)                       AS n_frames,
              GROUP_CONCAT(DISTINCT instrument)  AS instruments,
              GROUP_CONCAT(DISTINCT obsdate)     AS dates,
              GROUP_CONCAT(DISTINCT instrument || ':' || obsdate) AS inst_dates,
              GROUP_CONCAT(DISTINCT filter)      AS filters,
              SUM(COALESCE(exptime * nframes, 0)) AS total_exptime,
              coord_repr(ra, declination)        AS coord,
              MIN(NULLIF(airmass_min, 0))        AS airmass_min,
              MAX(NULLIF(airmass_max, 0))        AS airmass_max,
              CASE WHEN object GLOB '*[A-Za-z]*' THEN 1 ELSE 0 END
                                                 AS is_identified
           FROM summaries
           WHERE {' AND '.join(where)}
           GROUP BY object""",
        params,
    )

    result = []
    for r in cur.fetchall():
        base_row = (*r[:8], *_unpack_coord(r[8]), r[9], r[10], r[11])
        phot_status, fit_status = _aggregate_target_status(r[0], r[5])
        result.append((*base_row, phot_status, fit_status))

    return result


def _aggregate_target_status(obj: str, inst_dates_str: str) -> tuple[str, str]:
    """Aggregate (phot_status, fit_status) across all of a target's observation
    dates.

    A target is observed on many (instrument, date) pairs; its status is the
    strongest reduction found on *any* of them — ``"full"`` if any date has a
    full photometry reduction / fit output, else ``"test"`` (phot only), else
    ``"none"``. This is the single source of truth shared by the daily build
    (:func:`_target_rows`) and the per-job live refresh
    (:func:`refresh_target_status`), so both agree on what a status means.
    """
    from muscat_db import photometry as phot
    from muscat_db import transit_fit as fit_mod

    date_to_inst = {d: i for d, i in _parse_inst_dates(inst_dates_str).items() if _is_obsdate(d)}

    phot_status = "none"
    fit_status = "none"
    for d, inst in date_to_inst.items():
        status = phot.get_photometry_status(inst, d, obj)
        if status == "full":
            phot_status = "full"
        elif status == "test" and phot_status != "full":
            phot_status = "test"

        if fit_mod.has_fit_outputs(inst, d, obj):
            fit_status = "full"

    return phot_status, fit_status


def _replace_target_rows(conn: sqlite3.Connection, objects: set[str]) -> None:
    if not objects:
        return
    conn.executemany("DELETE FROM targets WHERE object = ?", [(obj,) for obj in sorted(objects)])
    rows = _target_rows(conn, objects)
    if rows:
        conn.executemany(
            """INSERT INTO targets
               (object, n_dates, n_frames, instruments, dates, inst_dates,
                filters, total_exptime, ra, declination, airmass_min, airmass_max,
                is_identified, phot_status, fit_status)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            rows,
        )


def _remove_sqlite_tmp(path: str) -> None:
    """Remove a SQLite file and its WAL/SHM/journal sidecars, ignoring absent
    ones. A WAL-mode build writes ``<path>-wal`` / ``<path>-shm`` next to the
    main file, so removing only the main file (the previous cleanup) leaked a
    multi-GB WAL on every failed build and could leave a stale sidecar that
    corrupts the next build.
    """
    for suffix in ("", "-wal", "-shm", "-journal"):
        try:
            os.remove(path + suffix)
        except OSError:
            pass


def _set_temp_store_dir(conn: sqlite3.Connection, db_file: str) -> None:
    """Direct SQLite's on-disk scratch files (sort / GROUP BY spills) to the
    database's own directory instead of the default ``/tmp``.

    The build spills the large ``summaries`` GROUP BY to a temp file; on a host
    whose root volume (holding ``/tmp``) is small or full, that aborts the build
    with "database or disk is full" even though the DB's own volume has ample
    space. The bundled SQLite ignores the ``SQLITE_TMPDIR`` env var, so use the
    per-connection ``temp_store_directory`` pragma (deprecated but honored),
    pointing scratch at *db_file*'s directory. Single quotes are escaped for the
    inlined literal because PRAGMA does not accept bound parameters.
    """
    tmp_dir = os.path.dirname(os.path.abspath(db_file)) or "."
    conn.execute("PRAGMA temp_store_directory = '%s'" % tmp_dir.replace("'", "''"))


# Tables owned by the app rather than derived from the obslog CSVs. build_db
# rebuilds the observation tables (frames/summaries/targets) from scratch, so
# these must be copied across the atomic swap or the daily cron silently wipes
# user notes, manual identification overrides, exposure calibration coefficients,
# job history, saved ephemeris views, closed-loop test observations, and LCO
# request-monitoring state on every successful build.  Monitor leases are
# deliberately excluded because they are process-local and must be reacquired.
_APP_OWNED_TABLES = (
    "jobs",
    "users",
    "ephemeris_views",
    "target_notes",
    "target_overrides",
    "exposure_coeffs",
    "test_observations",
    "lco_observation_requests",
    "lco_observation_frames",
    "chat_messages",
    "chat_reactions",
)


def _restore_table(conn: sqlite3.Connection, table: str, rows: list[dict]) -> None:
    """Re-insert preserved app-owned rows into the freshly rebuilt database.

    Copies whatever columns each row carries (``INSERT OR REPLACE``), intersected
    with the columns the new table actually has, so nothing is silently dropped
    and a schema that has gained/lost columns since the row was written still
    round-trips. *table* is a trusted module constant, never user input.
    """
    if not rows:
        return
    valid_cols = {r[1] for r in conn.execute(f"PRAGMA table_info({table})").fetchall()}
    for row in rows:
        cols = [c for c in row.keys() if c in valid_cols]
        if not cols:
            continue
        placeholders = ",".join("?" for _ in cols)
        conn.execute(
            f"INSERT OR REPLACE INTO {table} ({','.join(cols)}) VALUES ({placeholders})",
            tuple(row[c] for c in cols),
        )


def build_db(db_path: str, progress=None) -> int:
    """Rebuild the SQLite database from obslog CSVs.

    If ``progress`` is a ``rich.progress.Progress`` instance, three tasks are
    reported: CSV ingestion, summary aggregation, and targets aggregation.

    Builds to a temporary file first, then atomically replaces the target
    so a concurrently-running web server is never blocked by ``DROP TABLE``.
    """
    tmp_path = db_path + ".tmp"

    # A previously crashed build can leave <tmp>-wal / <tmp>-shm next to the
    # (already-removed) main tmp file. If SQLite replays that stale WAL against
    # the fresh tmp DB the build aborts with a malformed-image error, so clear
    # any leftover sidecars before opening the new connection.
    _remove_sqlite_tmp(tmp_path)

    # Preserve every app-owned table (user notes, manual identification
    # overrides, exposure calibration, job history, saved ephemeris views) from
    # the existing database so the temp-file rebuild of the observation-derived
    # tables doesn't wipe them. Rows are copied verbatim (all columns) so nothing
    # is silently dropped; missing tables/columns are tolerated for older DBs.
    preserved: dict[str, list[dict]] = {t: [] for t in _APP_OWNED_TABLES}
    if os.path.exists(db_path):
        try:
            with get_conn(db_path, row_factory=sqlite3.Row) as old_conn:
                _apply_schema(old_conn)
                for table in _APP_OWNED_TABLES:
                    try:
                        rows = old_conn.execute(f"SELECT * FROM {table}").fetchall()
                        preserved[table] = [dict(r) for r in rows]
                    except sqlite3.OperationalError:
                        pass
        except sqlite3.OperationalError:
            pass

    # Phase 1: discover all CSVs (cheap walk so we can size the progress bar).
    csv_jobs = _discover_csv_jobs()

    try:
        conn = sqlite3.connect(tmp_path)
        # Robust coordinate picker (filters malformed strings, keeps RA/Dec
        # paired, takes the median) — replaces the old MAX() string aggregation.
        conn.create_aggregate("coord_repr", 2, CoordRepr)
        conn.execute("PRAGMA journal_mode=WAL;")
        conn.execute("PRAGMA synchronous=OFF;")
        conn.execute("PRAGMA cache_size=100000;")
        # Keep GROUP BY / sort spills on the DB's own (roomy) volume, not /tmp.
        _set_temp_store_dir(conn, tmp_path)
        conn.executescript("DROP TABLE IF EXISTS frames; DROP TABLE IF EXISTS summaries; DROP TABLE IF EXISTS targets;")
        _apply_schema(conn)
        conn.execute("DROP INDEX IF EXISTS idx_frames_inst_date;")
        conn.execute("DROP INDEX IF EXISTS idx_frames_object;")
        conn.execute("DROP INDEX IF EXISTS idx_summaries_inst_date;")

        # Phase 2: ingest frames.
        if progress is not None:
            progress.add_task(
                "[cyan]Ingesting CSVs[/]", total=len(csv_jobs), filename="",
            )

        count = _ingest_csv_jobs(conn, csv_jobs, progress=progress)
        conn.commit()

        # Phase 3: build summaries.
        summary_task = None
        if progress is not None:
            summary_task = progress.add_task(
                "[cyan]Building summaries[/]", total=None, filename="",
            )
        rows = _summary_rows(conn)
        if progress is not None:
            progress.update(summary_task, total=len(rows))
        _insert_summary_rows(conn, rows)
        if progress is not None:
            progress.update(summary_task, completed=len(rows))
        conn.commit()

        # Phase 4: build targets (single aggregation query).
        targets_task = None
        if progress is not None:
            targets_task = progress.add_task(
                "[cyan]Building targets[/]", total=1, filename="",
            )
        _populate_targets(conn)
        if progress is not None:
            progress.update(targets_task, advance=1)

        # Create indexes at the very end to speed up insertions
        conn.execute("CREATE INDEX IF NOT EXISTS idx_frames_inst_date ON frames(instrument, obsdate);")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_frames_object ON frames(object);")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_summaries_inst_date ON summaries(instrument, obsdate);")

        conn.execute(
            "INSERT OR REPLACE INTO db_meta (key, value) VALUES ('last_build_at', ?)",
            (datetime.datetime.now().isoformat(),)
        )

        # Restore every preserved app-owned table verbatim so build-db never
        # wipes user notes, identification overrides, exposure calibration, job
        # history, or saved ephemeris views.
        for table in _APP_OWNED_TABLES:
            _restore_table(conn, table, preserved.get(table) or [])

        conn.commit()
        conn.close()
    except Exception:
        _remove_sqlite_tmp(tmp_path)
        raise

    os.replace(tmp_path, db_path)
    clear_all_caches()
    return count


_TARGET_EXCLUDE_EXACT = (
    "muscat", "muscat_fast", "test", "tic", "dark", "bias",
    "movie", "misc", "misc.", "focus_adjust", "fov",
)
_TARGET_EXACT_CLAUSE = ", ".join(f"'{s}'" for s in _TARGET_EXCLUDE_EXACT)


def _populate_targets(conn: sqlite3.Connection) -> None:
    """Aggregate per-target summary into the targets table."""
    rows = _target_rows(conn)
    if rows:
        conn.executemany(
            """INSERT INTO targets
               (object, n_dates, n_frames, instruments, dates, inst_dates,
                filters, total_exptime, ra, declination, airmass_min, airmass_max,
                is_identified, phot_status, fit_status)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            rows,
        )


def ingest_date(db_path: str, instrument: str, obsdate: str, progress=None) -> int:
    """Ingest one instrument/date from obslog CSVs into an existing database."""
    csv_jobs = _discover_csv_jobs(instrument, obsdate)
    if not csv_jobs:
        raise FileNotFoundError(f"No obslog CSVs found for {instrument} {obsdate}")

    conn = sqlite3.connect(db_path, timeout=30)
    try:
        conn.create_aggregate("coord_repr", 2, CoordRepr)
        conn.execute("PRAGMA journal_mode=WAL;")
        conn.execute("PRAGMA synchronous=OFF;")
        conn.execute("PRAGMA cache_size=100000;")
        # Keep GROUP BY / sort spills on the DB's own (roomy) volume, not /tmp.
        _set_temp_store_dir(conn, db_path)
        _apply_schema(conn)

        old_objects = {
            row[0] for row in conn.execute(
                "SELECT DISTINCT object FROM summaries WHERE instrument = ? AND obsdate = ?",
                (instrument, obsdate),
            ).fetchall()
            if row[0] is not None
        }

        conn.execute("DELETE FROM frames WHERE instrument = ? AND obsdate = ?", (instrument, obsdate))
        conn.execute("DELETE FROM summaries WHERE instrument = ? AND obsdate = ?", (instrument, obsdate))

        count = _ingest_csv_jobs(conn, csv_jobs, progress=progress)
        summary_rows = _summary_rows(conn, instrument=instrument, obsdate=obsdate)

        summary_task = None
        if progress is not None:
            summary_task = progress.add_task(
                "[cyan]Building summaries[/]", total=len(summary_rows), filename=f"{instrument} {obsdate}",
            )
        _insert_summary_rows(conn, summary_rows)
        if progress is not None:
            progress.update(summary_task, completed=len(summary_rows))

        new_objects = {
            row[0] for row in conn.execute(
                "SELECT DISTINCT object FROM summaries WHERE instrument = ? AND obsdate = ?",
                (instrument, obsdate),
            ).fetchall()
            if row[0] is not None
        }
        _replace_target_rows(conn, old_objects | new_objects)

        now = datetime.datetime.now().isoformat()
        conn.execute(
            "INSERT OR REPLACE INTO db_meta (key, value) VALUES ('last_build_at', ?)",
            (now,),
        )
        conn.execute(
            "INSERT OR REPLACE INTO db_meta (key, value) VALUES ('last_ingest_at', ?)",
            (now,),
        )
        conn.commit()
    finally:
        conn.close()

    clear_all_caches()
    return count


def _safe_float(v: str) -> float | None:
    try:
        return float(v)
    except (ValueError, TypeError):
        return None


def get_instruments(db_path: str) -> list[dict]:
    with get_conn(db_path) as conn:
        cur = conn.execute("SELECT DISTINCT instrument FROM summaries ORDER BY instrument")
        return [{"name": r[0]} for r in cur.fetchall()]


def get_instruments_summary(db_path: str, min_frames: int = 1000) -> list[dict]:
    """Return count of dates, frames, and targets for all instruments.

    Filters science targets to only count those with at least min_frames frames.
    """
    with get_conn(db_path) as conn:
        return _instruments_summary(conn, min_frames)


def _instruments_summary(conn: sqlite3.Connection, min_frames: int) -> list[dict]:
    # Get total dates and frames per instrument
    base_stats = conn.execute(
        """SELECT instrument, COUNT(DISTINCT obsdate), SUM(nframes)
           FROM summaries
           GROUP BY instrument"""
    ).fetchall()
    stats_map = {r[0]: (r[1] or 0, r[2] or 0) for r in base_stats}
    
    # Get science targets with at least min_frames frames
    target_stats = conn.execute(
        f"""SELECT instrument, COUNT(DISTINCT object)
            FROM (
                SELECT instrument, object, SUM(nframes) AS target_frames
                FROM summaries
                WHERE object IS NOT NULL
                  AND TRIM(object) <> ''
                  AND LOWER(TRIM(object)) NOT IN ({_TARGET_EXACT_CLAUSE})
                  AND LOWER(TRIM(object)) NOT LIKE '%flat%'
                  AND LOWER(TRIM(object)) NOT LIKE 'dark%'
                  AND LOWER(TRIM(object)) NOT LIKE 'bias%'
                  AND LOWER(TRIM(object)) NOT LIKE '%test%'
                  AND TRIM(object) NOT GLOB '*:*:*'
                GROUP BY instrument, object
                HAVING target_frames >= ?
            )
            GROUP BY instrument""",
        (min_frames,)
    ).fetchall()
    target_map = {r[0]: r[1] for r in target_stats}

    # Ensure all instruments from INSTRUMENTS or base_stats are returned
    names = sorted(list(set(INSTRUMENTS) | set(stats_map.keys())))
    return [
        {
            "name": name,
            "n_dates": stats_map.get(name, (0, 0))[0],
            "n_frames": stats_map.get(name, (0, 0))[1],
            "n_targets": target_map.get(name, 0)
        }
        for name in names
    ]


def get_dates(db_path: str, instrument: str) -> list[dict]:
    """Return one row per obsdate. Only YYMMDD-formatted dates are returned;
    legacy/test directories like ``200722_2`` or ``csv_old_220914`` are skipped.
    """
    # Read from the pre-aggregated `summaries` table rather than `frames`: it is
    # ~1000x smaller per instrument and SUM(nframes) reproduces COUNT(*) over
    # frames exactly, turning a multi-second scan into a sub-second query.
    with get_conn(db_path) as conn:
        cur = conn.execute(
            """SELECT obsdate, COUNT(DISTINCT ccd), SUM(nframes)
               FROM summaries
               WHERE instrument = ?
                 AND length(obsdate) = 6
                 AND obsdate GLOB '[0-9][0-9][0-9][0-9][0-9][0-9]'
               GROUP BY obsdate ORDER BY obsdate DESC""",
            (instrument,),
        )
        return [{"obsdate": r[0], "nccd": r[1], "nframes": r[2]} for r in cur.fetchall()]


def get_summaries(db_path: str, instrument: str, obsdate: str) -> list[dict]:
    with get_conn(db_path, row_factory=sqlite3.Row) as conn:
        cur = conn.execute(
            """SELECT ccd, object, exptime, read_mode,
                      telescope, frame_start, frame_end, ut_start, ut_end, nframes
               FROM summaries
               WHERE instrument = ? AND obsdate = ?
               ORDER BY ccd, object, telescope, ut_start""",
            (instrument, obsdate),
        )
        return [dict(r) for r in cur.fetchall()]


def get_objects(db_path: str, instrument: str, obsdate: str) -> list[str]:
    """Distinct real-target object names observed on one instrument/date.

    Reuses the same calibration/junk exclusions as the materialized targets
    table so the photometry picker only offers genuine science targets.
    """
    with get_conn(db_path) as conn:
        cur = conn.execute(
            """SELECT DISTINCT object FROM summaries
               WHERE instrument = ? AND obsdate = ?
                 AND object IS NOT NULL AND TRIM(object) <> ''
                 AND LOWER(TRIM(object)) NOT IN ({exact})
                 AND LOWER(TRIM(object)) NOT LIKE '%flat%'
                 AND LOWER(TRIM(object)) NOT LIKE 'dark%'
                 AND LOWER(TRIM(object)) NOT LIKE 'bias%'
                 AND LOWER(TRIM(object)) NOT LIKE '%test%'
                 AND TRIM(object) NOT GLOB '*:*:*'
               ORDER BY object COLLATE NOCASE""".format(
                exact=", ".join(f"'{s}'" for s in _TARGET_EXCLUDE_EXACT),
            ),
            (instrument, obsdate),
        )
        return [r[0] for r in cur.fetchall()]


_YYMMDD = re.compile(r"\d{6}")


def _is_obsdate(token: str) -> bool:
    """True only for canonical 6-digit YYMMDD obsdates.

    Excludes legacy/junk date tokens such as ``240129.org``, ``240722_1``,
    ``250512_bkup`` or free-text labels like ``Hyades`` that appear in the raw
    OBJECT-derived obsdate list. Matches the filter used by ``get_dates``.
    """
    return bool(_YYMMDD.fullmatch(token.strip()))


def get_targets(db_path: str) -> list[dict]:
    """Return the per-target summary materialized at build_db time."""
    with get_conn(db_path) as conn:
        return _targets_from_conn(conn)


def _targets_from_conn(conn: sqlite3.Connection) -> list[dict]:
    cur = conn.execute(
        """SELECT t.object, t.n_dates, t.n_frames, t.instruments, t.dates, t.filters,
                  t.total_exptime, t.ra, t.declination, t.airmass_min, t.airmass_max,
                  t.is_identified, COALESCE(n.note, ''), COALESCE(t.inst_dates, ''),
                  COALESCE(t.phot_status, 'none'), COALESCE(t.fit_status, 'none')
           FROM targets t
           LEFT JOIN target_notes n ON n.object = t.object
           ORDER BY t.object COLLATE NOCASE"""
    )
    result = []
    for r in cur.fetchall():
        dates = sorted(d for d in set((r[4] or "").split(",")) if _is_obsdate(d)) if r[4] else []
        filters = sorted(f for f in set((r[5] or "").split(",")) if f) if r[5] else []
        total_s = r[6] or 0.0
        date_to_inst = {d: i for d, i in _parse_inst_dates(r[13]).items() if _is_obsdate(d)}

        result.append({
            "object": r[0],
            "n_dates": len(dates),
            "n_frames": r[2],
            "instruments": sorted(set((r[3] or "").split(","))) if r[3] else [],
            "dates": dates,
            "filters": filters,
            "total_exptime_hr": round(total_s / 3600.0, 2),
            "ra": r[7] or "",
            "declination": r[8] or "",
            "airmass_min": r[9],
            "airmass_max": r[10],
            "is_identified": bool(r[11]),
            "note": r[12] or "",
            "date_to_inst": date_to_inst,
            "filter_chips": _normalize_filters(filters),
            "phot": r[14],
            "fit": r[15],
        })
    return result


_FILTER_COLOR_ALIAS = {
    "g":  "g",  "gp": "g",
    "r":  "r",  "rp": "r",  "R": "r",
    "i":  "i",  "ip": "i",  "I": "i",
    "z":  "z",  "zp": "z",  "zs": "z",  "z_s": "z",
}


def _normalize_filters(filters: list[str]) -> list[dict]:
    """Map raw filter names to display chips with band-color + narrow flag.

    g/gp -> 'g' (blue), r/rp/R -> 'r' (green), i/ip/I -> 'i' (yellow),
    z/zs/z_s/zp -> 'z' (red). Any *_narrow suffix renders as a darker chip
    of the same color and keeps the suffix in the label. 'Na_D' (sodium
    doublet) is also flagged narrow, matching the narrowband detection used
    elsewhere (see is_narrowband in web.py), even though it has no _narrow
    suffix. Anything else falls through to the neutral 'other' colour with
    the original label. Deduplicates so a target with both 'g' and 'gp'
    shows a single 'g' chip.
    """
    chips: list[dict] = []
    seen: set[tuple[str, str, bool]] = set()
    for f in filters or []:
        if not f:
            continue
        has_narrow_suffix = f.endswith("_narrow")
        base = f[:-7] if has_narrow_suffix else f
        narrow = has_narrow_suffix or base.lower() == "na_d"
        color = _FILTER_COLOR_ALIAS.get(base) or _FILTER_COLOR_ALIAS.get(base.lower(), "other")
        label = (color if color != "other" else base) + ("_narrow" if has_narrow_suffix else "")
        key = (label, color, narrow)
        if key in seen:
            continue
        seen.add(key)
        chips.append({"label": label, "color": color, "narrow": narrow})
    return chips


def _parse_inst_dates(s: str) -> dict[str, str]:
    """Parse 'inst1:date1,inst2:date2,...' into {date: inst}.

    When the same date appears for multiple instruments, the lexicographically
    first instrument wins (deterministic, matches sorted iteration order).
    """
    out: dict[str, str] = {}
    if not s:
        return out
    for pair in s.split(","):
        if ":" not in pair:
            continue
        inst, date = pair.split(":", 1)
        if date not in out or inst < out[date]:
            out[date] = inst
    return out


def set_note(db_path: str, obj: str, note: str) -> None:
    """Upsert a per-target note. Empty/whitespace `note` deletes the row."""
    note = (note or "").strip()
    with get_conn(db_path) as conn:
        _apply_schema(conn)
        if not note:
            conn.execute("DELETE FROM target_notes WHERE object = ?", (obj,))
        else:
            conn.execute(
                """INSERT INTO target_notes(object, note, updated_at)
                   VALUES (?, ?, CURRENT_TIMESTAMP)
                   ON CONFLICT(object) DO UPDATE
                     SET note = excluded.note, updated_at = CURRENT_TIMESTAMP""",
                (obj, note),
            )
        conn.commit()
    clear_all_caches()


def delete_note(db_path: str, obj: str) -> None:
    with get_conn(db_path) as conn:
        _apply_schema(conn)
        conn.execute("DELETE FROM target_notes WHERE object = ?", (obj,))
        conn.commit()
    clear_all_caches()


def set_identified(db_path: str, obj: str, is_identified: int) -> None:
    with get_conn(db_path) as conn:
        _apply_schema(conn)
        conn.execute(
            """INSERT INTO target_overrides(object, is_identified, updated_at)
               VALUES (?, ?, CURRENT_TIMESTAMP)
               ON CONFLICT(object) DO UPDATE
                 SET is_identified = excluded.is_identified, updated_at = CURRENT_TIMESTAMP""",
            (obj, is_identified),
        )
        conn.commit()
    clear_all_caches()


def get_identified_overrides(db_path: str) -> dict[str, bool]:
    with get_conn(db_path) as conn:
        _apply_schema(conn)
        cur = conn.execute("SELECT object, is_identified FROM target_overrides")
        return {row[0]: bool(row[1]) for row in cur.fetchall()}


def set_norm_name_override(db_path: str, obj: str, norm_name: str) -> None:
    """Set a user override for the normalized target name. Empty/None clears."""
    norm_name = (norm_name or "").strip() or None
    with get_conn(db_path) as conn:
        _apply_schema(conn)
        if norm_name is None:
            conn.execute(
                """UPDATE target_overrides SET norm_name = NULL, updated_at = CURRENT_TIMESTAMP
                   WHERE object = ?""",
                (obj,),
            )
        else:
            conn.execute(
                """INSERT INTO target_overrides(object, is_identified, norm_name, updated_at)
                   VALUES (?, 1, ?, CURRENT_TIMESTAMP)
                   ON CONFLICT(object) DO UPDATE
                     SET norm_name = excluded.norm_name, updated_at = CURRENT_TIMESTAMP""",
                (obj, norm_name),
            )
        conn.commit()
    clear_all_caches()


def get_norm_name_overrides(db_path: str) -> dict[str, str]:
    with get_conn(db_path) as conn:
        _apply_schema(conn)
        cur = conn.execute(
            "SELECT object, norm_name FROM target_overrides WHERE norm_name IS NOT NULL"
        )
        return {row[0]: row[1] for row in cur.fetchall()}


def get_frames(db_path: str, instrument: str, obsdate: str, ccd: int) -> list[dict]:
    with get_conn(db_path) as conn:
        cur = conn.execute(
            """SELECT * FROM frames
               WHERE instrument = ? AND obsdate = ? AND ccd = ?
               ORDER BY jd_start, filename""",
            (instrument, obsdate, ccd),
        )
        columns = [d[0] for d in cur.description]
        return [dict(zip(columns, r)) for r in cur.fetchall()]


def get_frame_objects(db_path: str) -> list[str]:
    """Distinct non-empty OBJECT values present in the frames obslog."""
    with get_conn(db_path) as conn:
        cur = conn.execute(
            "SELECT DISTINCT object FROM frames WHERE object IS NOT NULL AND TRIM(object) != ''"
        )
        return [r[0] for r in cur.fetchall()]


def get_exposure_log_for_objects(db_path: str, objects: list[str]) -> list[dict]:
    """Distinct past exposure configurations for the given OBJECT values.

    Groups the frames obslog by (instrument, filter, read_mode, focus, exptime)
    so each row is one recurring setup with its frame count and the obsdate
    range it was used, newest first. Feeds the schedule page's "Show ObsLog"
    lookup so a recurring observation can reuse a prior exposure time.
    """
    objects = [o for o in objects if o]
    if not objects:
        return []
    placeholders = ",".join("?" for _ in objects)
    with get_conn(db_path, row_factory=sqlite3.Row) as conn:
        cur = conn.execute(
            f"""SELECT instrument,
                       COALESCE(filter, '')    AS filter,
                       COALESCE(read_mode, '') AS read_mode,
                       ROUND(focus * 2) / 2    AS focus,
                       ROUND(exptime)          AS exptime,
                       COUNT(*)                AS nframes,
                       MAX(obsdate)            AS last_date,
                       MIN(obsdate)            AS first_date
                  FROM frames
                 WHERE object IN ({placeholders}) AND exptime IS NOT NULL
              GROUP BY instrument, COALESCE(filter, ''), COALESCE(read_mode, ''),
                       ROUND(focus * 2) / 2, ROUND(exptime)
              ORDER BY last_date DESC, instrument, filter""",
            objects,
        )
        return [dict(r) for r in cur.fetchall()]


def db_path() -> str:
    import pathlib
    return str(pathlib.Path(os.environ.get("MUSCAT_DB_PATH", "muscat.db")).resolve())


@contextmanager
def get_conn(
    path: str | None = None,
    *,
    timeout: float = 30.0,
    row_factory=None,
) -> Iterator[sqlite3.Connection]:
    """Single entry point for SQLite connections.

    Guarantees the connection is closed even if the body raises — the previous
    open-coded ``connect(...) ... close()`` helpers leaked the handle on any
    exception between the two — and standardizes the busy ``timeout`` (default
    30s) so writers don't fail fast under WAL contention. Schema-ensure and
    migration calls stay at the call site because they vary per table.
    """
    if path is None:
        path = db_path()
    conn = sqlite3.connect(path, timeout=timeout)
    try:
        if row_factory is not None:
            conn.row_factory = row_factory
        yield conn
    finally:
        conn.close()


class UserSettingsError(RuntimeError):
    """Raised when per-user settings cannot be read or written safely."""


def _settings_fernet() -> Fernet:
    secret = os.environ.get("MUSCAT_DB_SECRET", "").strip()
    if not secret:
        raise UserSettingsError("MUSCAT_DB_SECRET is required to encrypt per-user tokens")
    key = base64.urlsafe_b64encode(
        hashlib.sha256(("muscat-db-user-settings:" + secret).encode("utf-8")).digest()
    )
    return Fernet(key)


def _encrypt_token(token: str) -> str:
    return _settings_fernet().encrypt(token.encode("utf-8")).decode("ascii")


def _decrypt_token(ciphertext: str) -> str:
    try:
        return _settings_fernet().decrypt(ciphertext.encode("ascii")).decode("utf-8")
    except (InvalidToken, UnicodeError) as exc:
        raise UserSettingsError("stored token cannot be decrypted with MUSCAT_DB_SECRET") from exc


def _ensure_users_schema(conn: sqlite3.Connection) -> None:
    _apply_schema(conn)


def _clean_username(username: str | None) -> str:
    username = (username or "").strip()
    if not username:
        raise UserSettingsError("username is required")
    return username


def ensure_user(username: str | None, *, display_name: str | None = None) -> None:
    username = _clean_username(username)
    display_name = (display_name or username).strip() or username
    with get_conn() as conn:
        _ensure_users_schema(conn)
        conn.execute(
            "INSERT OR IGNORE INTO users (username, display_name) VALUES (?, ?)",
            (username, display_name),
        )
        conn.commit()


def get_user_settings(username: str | None) -> dict:
    username = _clean_username(username)
    with get_conn(row_factory=sqlite3.Row) as conn:
        _ensure_users_schema(conn)
        row = conn.execute("SELECT settings FROM users WHERE username = ?", (username,)).fetchone()
    if row is None:
        return {}
    try:
        settings = json.loads(row["settings"] or "{}")
    except (TypeError, json.JSONDecodeError):
        settings = {}
    return settings if isinstance(settings, dict) else {}


def update_user_settings(username: str | None, updates: dict, *, remove: list[str] | None = None) -> dict:
    username = _clean_username(username)
    ensure_user(username)
    settings = get_user_settings(username)
    for key in remove or []:
        settings.pop(key, None)
    settings.update(updates)
    payload = json.dumps(settings, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    with get_conn() as conn:
        _ensure_users_schema(conn)
        conn.execute(
            "UPDATE users SET settings = ? WHERE username = ?",
            (payload, username),
        )
        conn.commit()
    return settings


def set_user_lco_token(username: str | None, token: str | None) -> None:
    token = (token or "").strip()
    if not token:
        update_user_settings(username, {}, remove=["lco_token_enc"])
        return
    update_user_settings(username, {"lco_token_enc": _encrypt_token(token)})


def get_user_lco_token(username: str | None) -> str | None:
    if not (username or "").strip():
        return None
    settings = get_user_settings(username)
    ciphertext = settings.get("lco_token_enc")
    if not ciphertext:
        return None
    return _decrypt_token(str(ciphertext))


def user_lco_token_configured(username: str | None) -> bool:
    try:
        return get_user_lco_token(username) is not None
    except UserSettingsError:
        return False


def set_user_ads_token(username: str | None, token: str | None) -> None:
    token = (token or "").strip()
    if not token:
        update_user_settings(username, {}, remove=["ads_token_enc"])
        return
    update_user_settings(username, {"ads_token_enc": _encrypt_token(token)})


def get_user_ads_token(username: str | None) -> str | None:
    if not (username or "").strip():
        return None
    settings = get_user_settings(username)
    ciphertext = settings.get("ads_token_enc")
    if not ciphertext:
        return None
    return _decrypt_token(str(ciphertext))


def user_ads_token_configured(username: str | None) -> bool:
    try:
        return get_user_ads_token(username) is not None
    except UserSettingsError:
        return False


_EPHEM_SHEET_KEYS = (
    "ephem_sheet_url_enc",
    "ephem_sheet_ephem_tab",
    "ephem_sheet_tc_tab",
    "ephem_sheet_ephem_cols",
    "ephem_sheet_tc_cols",
)


def _clean_col_map(cols: dict | None) -> dict:
    """Normalize a {field: header} column mapping to non-empty string pairs."""
    if not isinstance(cols, dict):
        return {}
    cleaned: dict[str, str] = {}
    for key, value in cols.items():
        key_s = str(key).strip()
        value_s = str(value or "").strip()
        if key_s and value_s:
            cleaned[key_s] = value_s
    return cleaned


def set_user_ephem_sheet(
    username: str | None,
    url: str | None,
    ephem_tab: str | None = None,
    tc_tab: str | None = None,
    ephem_cols: dict | None = None,
    tc_cols: dict | None = None,
) -> None:
    """Store a per-user ephemeris Google Sheet (URL encrypted, tabs/columns plain).

    A blank ``url`` clears the sheet configuration entirely. Blank tab names and
    empty column maps are removed so the resolver falls back to its own defaults
    (alias auto-detection for columns).
    """
    url = (url or "").strip()
    if not url:
        update_user_settings(username, {}, remove=list(_EPHEM_SHEET_KEYS))
        return
    updates: dict = {"ephem_sheet_url_enc": _encrypt_token(url)}
    removes: list[str] = []
    for key, tab in (
        ("ephem_sheet_ephem_tab", ephem_tab),
        ("ephem_sheet_tc_tab", tc_tab),
    ):
        tab = (tab or "").strip()
        if tab:
            updates[key] = tab
        else:
            removes.append(key)
    for key, cols in (
        ("ephem_sheet_ephem_cols", ephem_cols),
        ("ephem_sheet_tc_cols", tc_cols),
    ):
        cleaned = _clean_col_map(cols)
        if cleaned:
            updates[key] = cleaned
        else:
            removes.append(key)
    update_user_settings(username, updates, remove=removes or None)


def get_user_ephem_sheet(username: str | None) -> dict | None:
    """Return the sheet config or None when not configured.

    Keys: ``url``, ``ephem_tab``, ``tc_tab`` (tab names as stored, possibly
    empty; callers apply defaults) and ``ephem_cols``/``tc_cols`` column maps
    (possibly empty dicts). Raises :class:`UserSettingsError` if the URL cannot
    be decrypted.
    """
    if not (username or "").strip():
        return None
    settings = get_user_settings(username)
    ciphertext = settings.get("ephem_sheet_url_enc")
    if not ciphertext:
        return None
    ephem_cols = settings.get("ephem_sheet_ephem_cols")
    tc_cols = settings.get("ephem_sheet_tc_cols")
    return {
        "url": _decrypt_token(str(ciphertext)),
        "ephem_tab": str(settings.get("ephem_sheet_ephem_tab") or "").strip(),
        "tc_tab": str(settings.get("ephem_sheet_tc_tab") or "").strip(),
        "ephem_cols": ephem_cols if isinstance(ephem_cols, dict) else {},
        "tc_cols": tc_cols if isinstance(tc_cols, dict) else {},
    }


def user_ephem_sheet_configured(username: str | None) -> bool:
    try:
        return get_user_ephem_sheet(username) is not None
    except UserSettingsError:
        return False


def set_user_eso_credentials(
    username: str | None,
    eso_username: str | None,
    eso_password: str | None,
) -> None:
    """Store ESO archive username and password (each encrypted separately)."""
    eso_username = (eso_username or "").strip()
    eso_password = (eso_password or "").strip()
    updates: dict = {}
    removes: list[str] = []
    if eso_username:
        updates["eso_username_enc"] = _encrypt_token(eso_username)
    else:
        removes.append("eso_username_enc")
    if eso_password:
        updates["eso_password_enc"] = _encrypt_token(eso_password)
    else:
        removes.append("eso_password_enc")
    update_user_settings(username, updates, remove=removes if removes else None)


def get_user_eso_credentials(username: str | None) -> tuple[str | None, str | None]:
    """Return (eso_username, eso_password) or (None, None) if not configured."""
    if not (username or "").strip():
        return None, None
    settings = get_user_settings(username)
    eso_u_enc = settings.get("eso_username_enc")
    eso_p_enc = settings.get("eso_password_enc")
    eso_u = _decrypt_token(str(eso_u_enc)) if eso_u_enc else None
    eso_p = _decrypt_token(str(eso_p_enc)) if eso_p_enc else None
    return eso_u, eso_p


def user_eso_credentials_configured(username: str | None) -> bool:
    """Return True if both ESO username and password are stored for this user."""
    try:
        eso_u, eso_p = get_user_eso_credentials(username)
        return eso_u is not None and eso_p is not None
    except UserSettingsError:
        return False


def _ensure_jobs_schema(conn: sqlite3.Connection) -> None:
    _apply_schema(conn)
    # Migrations for databases created before these columns existed.
    for col, col_type in [
        ("run_type", "TEXT NOT NULL DEFAULT ''"),
        ("params", "TEXT NOT NULL DEFAULT ''"),
        ("run_id", "TEXT NOT NULL DEFAULT ''"),
        ("run_name", "TEXT NOT NULL DEFAULT ''"),
        ("user_name", "TEXT NOT NULL DEFAULT ''"),
    ]:
        try:
            conn.execute(f"ALTER TABLE jobs ADD COLUMN {col} {col_type}")
        except sqlite3.OperationalError:
            pass


def _backfill_job_run_names(conn: sqlite3.Connection) -> bool:
    rows = conn.execute(
        "SELECT key, params, run_id, run_name FROM jobs WHERE COALESCE(run_name, '') = ''"
    ).fetchall()
    updates: list[tuple[str, str]] = []
    for key, params_raw, run_id, _run_name in rows:
        parsed_name = ""
        if params_raw:
            try:
                payload = json.loads(params_raw)
            except (TypeError, json.JSONDecodeError):
                payload = {}
            if isinstance(payload, dict):
                parsed_name = str(payload.get("run_name") or "").strip()
                if not parsed_name:
                    options = payload.get("options")
                    if isinstance(options, dict):
                        parsed_name = str(options.get("run_name") or "").strip()
        if not parsed_name and run_id:
            parsed_name = str(run_id).strip()
        if parsed_name:
            updates.append((parsed_name, key))
    if not updates:
        return False
    conn.executemany("UPDATE jobs SET run_name = ? WHERE key = ?", updates)
    return True


# The jobs schema-ensure (executescript + ALTER probes) and the run_name backfill
# (a full-table scan) are one-time migrations, but they sat in the hot path of
# every save_job / get_persisted_jobs call -- i.e. on every 2s status poll. Run
# them once per (process, db path) instead. build_db always rewrites the full
# SCHEMA when it swaps the file in, so a DB seen after the first call can never be
# older than the current schema, making the skip safe.
_migrated_paths: set[str] = set()
_migrate_lock = threading.Lock()


def _ensure_jobs_migrated(conn: sqlite3.Connection, path: str) -> None:
    if path in _migrated_paths:
        return
    with _migrate_lock:
        if path in _migrated_paths:
            return
        _ensure_jobs_schema(conn)
        if _backfill_job_run_names(conn):
            conn.commit()
        _migrated_paths.add(path)


def save_job(
    type_: str,
    inst: str,
    date: str,
    target: str,
    state: str,
    returncode: int | None,
    elapsed: int,
    started_at: float,
    error_desc: str = "",
    run_type: str = "",
    params: str = "",
    run_id: str = "",
    run_name: str = "",
    user_name: str | None = None,
) -> None:
    # The "user" is the nginx-authenticated account (X-Forwarded-User), set at
    # job creation from request.state.user. State-transition callers (sync_jobs,
    # watchdog, cancel, queue-drain) legitimately omit it, so default to "" and
    # let the ON CONFLICT clause below preserve the existing user_name instead
    # of clobbering it with the OS account (getpass.getuser()), which is never
    # who launched the job through the web UI.
    if user_name is None:
        user_name = ""
    path = db_path()
    # Run-scoped key so distinct runs of the same target are separate job rows;
    # an empty run_id reproduces the legacy key.
    key = f"{type_}:{inst}/{date}/{target.replace(' ', '')}"
    if run_id:
        key = f"{key}/{run_id}"
    with get_conn(path) as conn:
        _ensure_jobs_migrated(conn, path)
        conn.execute(
            """INSERT INTO jobs(key, type, instrument, obsdate, target, state, returncode, elapsed, started_at, error_desc, run_type, params, run_id, run_name, user_name)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(key) DO UPDATE SET
                 state      = excluded.state,
                 returncode = excluded.returncode,
                 elapsed    = excluded.elapsed,
                 started_at = excluded.started_at,
                 error_desc = excluded.error_desc,
                 run_type   = CASE WHEN excluded.run_type != '' THEN excluded.run_type ELSE run_type END,
                 params     = CASE WHEN excluded.params != '' THEN excluded.params ELSE params END,
                 run_id     = excluded.run_id,
                 run_name   = CASE WHEN excluded.run_name != '' THEN excluded.run_name ELSE run_name END,
                 user_name  = CASE WHEN excluded.user_name != '' THEN excluded.user_name ELSE user_name END""",
            (key, type_, inst, date, target, state, returncode, elapsed, started_at, error_desc, run_type, params, run_id, run_name, user_name)
        )
        conn.commit()
    clear_all_caches()


def get_persisted_jobs() -> list[dict]:
    path = db_path()
    with get_conn(path) as conn:
        _ensure_jobs_migrated(conn, path)
        cur = conn.execute("SELECT * FROM jobs ORDER BY started_at DESC")
        columns = [d[0] for d in cur.description]
        result = []
        for r in cur.fetchall():
            d = dict(zip(columns, r))
            d["inst"] = d["instrument"]
            d["date"] = d["obsdate"]
            if not str(d.get("run_name") or "").strip():
                d["run_name"] = str(d.get("run_id") or "").strip()
            result.append(d)
        return result


def get_active_persisted_jobs() -> list[dict]:
    """Return only live job rows using the active-state index.

    This is the read path used by the site-wide progress poller; unlike
    :func:`get_persisted_jobs`, its work does not grow with job history.
    """
    path = db_path()
    with get_conn(path) as conn:
        _ensure_jobs_migrated(conn, path)
        cur = conn.execute(
            """SELECT * FROM jobs
               WHERE state IN ('running', 'cancelling', 'pending')
               ORDER BY started_at DESC"""
        )
        columns = [d[0] for d in cur.description]
        result = []
        for row in cur.fetchall():
            item = dict(zip(columns, row))
            item["inst"] = item["instrument"]
            item["date"] = item["obsdate"]
            if not str(item.get("run_name") or "").strip():
                item["run_name"] = str(item.get("run_id") or "").strip()
            result.append(item)
        return result


def save_ephemeris_view(state: dict) -> dict:
    """Persist a deterministic ephemeris page view and return its slug."""
    path = db_path()
    slug, state_hash, state_json = ephemeris_view_slug(state)
    targets = state.get("targets") if isinstance(state, dict) else []
    if not isinstance(targets, list):
        targets = []
    targets_json = _canonical_json([str(t) for t in targets])

    with get_conn(path) as conn:
        _apply_schema(conn)
        conn.execute(
            """INSERT INTO ephemeris_views
               (slug, state_hash, state_json, targets_json, created_at, updated_at)
               VALUES (?, ?, ?, ?, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
               ON CONFLICT(slug) DO UPDATE SET
                 updated_at = CURRENT_TIMESTAMP""",
            (slug, state_hash, state_json, targets_json),
        )
        conn.commit()
    return {"slug": slug, "state_hash": state_hash}


def get_ephemeris_view(slug: str) -> dict | None:
    path = db_path()
    with get_conn(path, row_factory=sqlite3.Row) as conn:
        _apply_schema(conn)
        row = conn.execute(
            "SELECT slug, state_hash, state_json, targets_json, created_at, updated_at FROM ephemeris_views WHERE slug = ?",
            (slug,),
        ).fetchone()
    if row is None:
        return None
    state = json.loads(row["state_json"])
    return {
        "slug": row["slug"],
        "state_hash": row["state_hash"],
        "state": state,
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
    }


def get_last_build_date(db_path: str) -> str:
    """Get the date when muscat-db build was run, or the date when the database file was generated."""
    try:
        with get_conn(db_path) as conn:
            row = conn.execute(
                "SELECT value FROM db_meta WHERE key = 'last_build_at'"
            ).fetchone()
        if row:
            return row[0][:10]
    except sqlite3.Error:
        pass

    try:
        mtime = os.stat(db_path).st_mtime
        return datetime.datetime.fromtimestamp(mtime).strftime("%Y-%m-%d")
    except OSError:
        return datetime.date.today().strftime("%Y-%m-%d")


def refresh_target_status(obj: str) -> None:
    """Recompute and persist a single target's phot_status/fit_status immediately.

    Called by photometry.py / transit_fit.py after a job reaches a terminal state
    so the Targets page reflects new outputs without waiting for the daily
    ``build_db`` cron. Aggregates across *all* of the target's observation dates
    (via :func:`_aggregate_target_status`), so a reduction finishing on one date
    never clobbers a "full" status earned on another.

    Best-effort: a failed refresh (e.g. read-only DB, unknown target) is logged
    but never propagated, so it cannot break the surrounding job sync.
    """
    try:
        with get_conn() as conn:
            row = conn.execute(
                "SELECT COALESCE(inst_dates, '') FROM targets WHERE object = ?",
                (obj,),
            ).fetchone()
            if row is None:
                # No target row yet (e.g. unidentified object, or built after the
                # last daily run). Nothing to update until the next build_db.
                return
            phot_status, fit_status = _aggregate_target_status(obj, row[0])
            conn.execute(
                "UPDATE targets SET phot_status = ?, fit_status = ? WHERE object = ?",
                (phot_status, fit_status, obj),
            )
            conn.commit()
        clear_all_caches()
    except sqlite3.Error:
        logger.debug("failed to refresh target status for %s", obj, exc_info=True)


# ---------------------------------------------------------------------------
# Team chat persistence
#
# Chat rows are app-owned (see _APP_OWNED_TABLES) so they survive the daily
# frames/summaries rebuild. Message rendering/escaping happens client-side
# (DOM text nodes), so text is stored verbatim. Ephemeral @test messages are
# never persisted and never reach these functions.
# ---------------------------------------------------------------------------

# Backfill window (days) for the initial history load. Kept configurable so a
# busy room can be tuned without a code change.
CHAT_BACKFILL_DAYS = int(os.environ.get("MUSCAT_CHAT_BACKFILL_DAYS", "7"))

_chat_migrated_paths: set[str] = set()


def _ensure_chat_schema(conn: sqlite3.Connection, path: str) -> None:
    """Create the chat tables once per (process, db path). ``_apply_schema``
    runs every ``CREATE TABLE IF NOT EXISTS`` in SCHEMA, which includes the chat
    tables, so this doubles as the migration for pre-existing databases."""
    if path in _chat_migrated_paths:
        return
    with _migrate_lock:
        if path in _chat_migrated_paths:
            return
        _apply_schema(conn)
        _chat_migrated_paths.add(path)


def _reactions_by_message(conn: sqlite3.Connection, message_ids: list[int]) -> dict[int, list[dict]]:
    """Return ``{message_id: [{emoji, count, users:[...]}, ...]}`` for the given ids."""
    if not message_ids:
        return {}
    placeholders = ",".join("?" for _ in message_ids)
    rows = conn.execute(
        f"SELECT message_id, emoji, user_name FROM chat_reactions "
        f"WHERE message_id IN ({placeholders}) ORDER BY created_at",
        tuple(message_ids),
    ).fetchall()
    grouped: dict[int, dict[str, list[str]]] = {}
    for mid, emoji, user in rows:
        grouped.setdefault(mid, {}).setdefault(emoji, []).append(user)
    result: dict[int, list[dict]] = {}
    for mid, emojis in grouped.items():
        result[mid] = [
            {"emoji": e, "count": len(users), "users": users}
            for e, users in emojis.items()
        ]
    return result


def _chat_row_to_dict(row: sqlite3.Row, reactions: dict[int, list[dict]]) -> dict:
    try:
        mentions = json.loads(row["mentions"] or "[]")
    except (TypeError, json.JSONDecodeError):
        mentions = []
    return {
        "id": row["id"],
        "user": row["user_name"],
        "text": row["text"],
        "kind": row["kind"],
        "ts": row["created_at"],
        "edited": row["edited_at"] is not None,
        "edited_at": row["edited_at"],
        "mentions": mentions if isinstance(mentions, list) else [],
        "reactions": reactions.get(row["id"], []),
    }


def save_chat_message(
    user_name: str,
    text: str,
    mentions: list[str] | None = None,
    kind: str = "user",
    created_at: float | None = None,
) -> dict:
    """Persist a chat message and return its stored representation (incl. id)."""
    path = db_path()
    ts = time.time() if created_at is None else created_at
    mentions_json = json.dumps(sorted(set(mentions or [])), ensure_ascii=True)
    with get_conn(path, row_factory=sqlite3.Row) as conn:
        _ensure_chat_schema(conn, path)
        cur = conn.execute(
            "INSERT INTO chat_messages(user_name, text, kind, created_at, mentions) "
            "VALUES (?, ?, ?, ?, ?)",
            (user_name, text, kind, ts, mentions_json),
        )
        conn.commit()
        msg_id = cur.lastrowid
    return {
        "id": msg_id,
        "user": user_name,
        "text": text,
        "kind": kind,
        "ts": ts,
        "edited": False,
        "edited_at": None,
        "mentions": sorted(set(mentions or [])),
        "reactions": [],
    }


def get_recent_chat_messages(days: int | None = None, limit: int = 500) -> list[dict]:
    """Return persisted messages from the last ``days`` (oldest first), with
    reactions attached. ``limit`` caps the payload for a very busy window."""
    path = db_path()
    window_days = CHAT_BACKFILL_DAYS if days is None else days
    since = time.time() - window_days * 86400
    with get_conn(path, row_factory=sqlite3.Row) as conn:
        _ensure_chat_schema(conn, path)
        rows = conn.execute(
            "SELECT * FROM chat_messages WHERE created_at >= ? "
            "ORDER BY created_at DESC LIMIT ?",
            (since, limit),
        ).fetchall()
        rows = list(reversed(rows))
        reactions = _reactions_by_message(conn, [r["id"] for r in rows])
        return [_chat_row_to_dict(r, reactions) for r in rows]


def edit_chat_message(msg_id: int, user_name: str, new_text: str) -> dict | None:
    """Edit a message in place. Author-only: returns the updated message, or
    ``None`` if the message does not exist or ``user_name`` is not its author."""
    path = db_path()
    edited_at = time.time()
    with get_conn(path, row_factory=sqlite3.Row) as conn:
        _ensure_chat_schema(conn, path)
        cur = conn.execute(
            "UPDATE chat_messages SET text = ?, edited_at = ? "
            "WHERE id = ? AND user_name = ? AND kind = 'user'",
            (new_text, edited_at, msg_id, user_name),
        )
        conn.commit()
        if cur.rowcount == 0:
            return None
        row = conn.execute("SELECT * FROM chat_messages WHERE id = ?", (msg_id,)).fetchone()
        reactions = _reactions_by_message(conn, [msg_id])
        return _chat_row_to_dict(row, reactions) if row else None


def delete_chat_message(msg_id: int, user_name: str) -> bool:
    """Hard-delete a message and its reactions. Author-only: returns True on
    success, False if the message is missing or not owned by ``user_name``."""
    path = db_path()
    with get_conn(path) as conn:
        _ensure_chat_schema(conn, path)
        cur = conn.execute(
            "DELETE FROM chat_messages WHERE id = ? AND user_name = ? AND kind = 'user'",
            (msg_id, user_name),
        )
        if cur.rowcount:
            conn.execute("DELETE FROM chat_reactions WHERE message_id = ?", (msg_id,))
        conn.commit()
        return cur.rowcount > 0


def toggle_chat_reaction(msg_id: int, user_name: str, emoji: str) -> dict | None:
    """Toggle a user's emoji reaction on a message. Returns the message id with
    its refreshed reaction summary, or ``None`` if the message no longer exists."""
    path = db_path()
    with get_conn(path, row_factory=sqlite3.Row) as conn:
        _ensure_chat_schema(conn, path)
        exists = conn.execute("SELECT 1 FROM chat_messages WHERE id = ?", (msg_id,)).fetchone()
        if not exists:
            return None
        present = conn.execute(
            "SELECT 1 FROM chat_reactions WHERE message_id = ? AND user_name = ? AND emoji = ?",
            (msg_id, user_name, emoji),
        ).fetchone()
        if present:
            conn.execute(
                "DELETE FROM chat_reactions WHERE message_id = ? AND user_name = ? AND emoji = ?",
                (msg_id, user_name, emoji),
            )
        else:
            conn.execute(
                "INSERT OR IGNORE INTO chat_reactions(message_id, user_name, emoji, created_at) "
                "VALUES (?, ?, ?, ?)",
                (msg_id, user_name, emoji, time.time()),
            )
        conn.commit()
        reactions = _reactions_by_message(conn, [msg_id])
        return {"id": msg_id, "reactions": reactions.get(msg_id, [])}


# Auth accounts that are test/demo/system identities rather than real
# teammates. They are kept out of the chat @-mention autocomplete and out of
# mention resolution (so they never appear as suggestions and never get nudged).
# Override with MUSCAT_CHAT_EXCLUDE_USERS (comma-separated, case-insensitive);
# an empty value disables the filter entirely. A user who has actually posted a
# chat message is always kept, even if listed here.
_DEFAULT_EXCLUDED_CHAT_USERS = frozenset(
    {"testuser", "trusted-user", "diagnostic", "observer", "alice", "john"}
)


def _excluded_chat_users() -> set[str]:
    raw = os.environ.get("MUSCAT_CHAT_EXCLUDE_USERS")
    if raw is None:
        return set(_DEFAULT_EXCLUDED_CHAT_USERS)
    return {n.strip().lower() for n in raw.split(",") if n.strip()}


def get_known_chat_usernames(limit: int = 200) -> list[str]:
    """Usernames for @-mention autocomplete: everyone who has authenticated
    (users table) plus any historical chat authors, de-duplicated and sorted.

    Test/demo/system accounts (see ``_excluded_chat_users``) are dropped unless
    they have actually posted a chat message."""
    path = db_path()
    excluded = _excluded_chat_users()
    with get_conn(path) as conn:
        _ensure_chat_schema(conn, path)
        # Real chat authors are always kept, so exclusion never hides someone
        # who has demonstrably participated.
        chatted: set[str] = set()
        for (u,) in conn.execute(
            "SELECT DISTINCT user_name FROM chat_messages WHERE kind = 'user'"
        ):
            if u and u.strip():
                chatted.add(u.strip())
        names = set(chatted)
        try:
            for (u,) in conn.execute("SELECT username FROM users"):
                if u and u.strip() and (
                    u.strip().lower() not in excluded or u.strip() in chatted
                ):
                    names.add(u.strip())
        except sqlite3.OperationalError:
            pass
    return sorted(names, key=str.lower)[:limit]
