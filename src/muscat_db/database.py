from __future__ import annotations

import csv
import base64
import hashlib
import json
import os
import re
import datetime
import sqlite3
import threading
from collections.abc import Iterator
from contextlib import contextmanager

from muscat_db.instruments import INSTRUMENTS, OBSLOG_BASE
from muscat_db.cache import clear_all_caches
from muscat_db.coord import CoordRepr, unpack as _unpack_coord


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
    is_identified INTEGER NOT NULL DEFAULT 1
);

CREATE TABLE IF NOT EXISTS target_notes (
    object     TEXT PRIMARY KEY,
    note       TEXT NOT NULL,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS target_overrides (
    object        TEXT PRIMARY KEY,
    is_identified INTEGER NOT NULL,
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

CREATE TABLE IF NOT EXISTS ephemeris_views (
    slug         TEXT PRIMARY KEY,
    state_hash   TEXT NOT NULL,
    state_json   TEXT NOT NULL,
    targets_json TEXT NOT NULL,
    created_at   TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at   TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
"""


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
    raw = conn.execute(
        f"""SELECT instrument, obsdate, ccd, object, exptime, read_mode,
                  MIN(filename), MAX(filename),
                  MIN(ut_start), MAX(ut_start), COUNT(*),
                  MAX(filter), coord_repr(ra, declination),
                  MIN(NULLIF(airmass, 0)), MAX(NULLIF(airmass, 0))
           FROM frames
           {where_sql}
           GROUP BY instrument, obsdate, ccd, object, exptime, read_mode""",
        params,
    ).fetchall()
    return [(*r[:12], *_unpack_coord(r[12]), r[13], r[14]) for r in raw]


def _insert_summary_rows(conn: sqlite3.Connection, rows: list[tuple]) -> None:
    if not rows:
        return
    conn.executemany(
        """INSERT INTO summaries
           (instrument, obsdate, ccd, object, exptime, read_mode,
            frame_start, frame_end, ut_start, ut_end, nframes,
            filter, ra, declination, airmass_min, airmass_max)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
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
    return [(*r[:8], *_unpack_coord(r[8]), r[9], r[10], r[11]) for r in cur.fetchall()]


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
                is_identified)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
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
# job history, and saved ephemeris views on every successful build.
_APP_OWNED_TABLES = (
    "jobs",
    "ephemeris_views",
    "target_notes",
    "target_overrides",
    "exposure_coeffs",
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
                old_conn.executescript(SCHEMA)
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
        conn.executescript(SCHEMA)
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
    except BaseException:
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
                is_identified)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
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
        conn.executescript(SCHEMA)

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
                      frame_start, frame_end, ut_start, ut_end, nframes
               FROM summaries
               WHERE instrument = ? AND obsdate = ?
               ORDER BY ccd, object, ut_start""",
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
        conn.executescript(SCHEMA)  # ensure target_notes exists on first read
        return _targets_from_conn(conn)


def _targets_from_conn(conn: sqlite3.Connection) -> list[dict]:
    cur = conn.execute(
        """SELECT t.object, t.n_dates, t.n_frames, t.instruments, t.dates, t.filters,
                  t.total_exptime, t.ra, t.declination, t.airmass_min, t.airmass_max,
                  t.is_identified, COALESCE(n.note, ''), COALESCE(t.inst_dates, '')
           FROM targets t
           LEFT JOIN target_notes n ON n.object = t.object
           ORDER BY t.object COLLATE NOCASE"""
    )
    result = []
    from muscat_db import photometry as phot
    for r in cur.fetchall():
        # Keep only canonical YYMMDD obsdates; drop junk like '240129.org'.
        dates = sorted(d for d in set((r[4] or "").split(",")) if _is_obsdate(d)) if r[4] else []
        filters = sorted(f for f in set((r[5] or "").split(",")) if f) if r[5] else []
        total_s = r[6] or 0.0
        date_to_inst = {d: i for d, i in _parse_inst_dates(r[13]).items() if _is_obsdate(d)}
        
        phot_status = "none"
        fit_status = "none"
        from muscat_db import transit_fit as fit_mod
        for d, inst in date_to_inst.items():
            status = phot.get_photometry_status(inst, d, r[0])
            if status == "full":
                phot_status = "full"
            elif status == "test" and phot_status != "full":
                phot_status = "test"
                
            fit_out = fit_mod.get_fit_outputs(inst, d, r[0])
            if fit_out.get("has_any"):
                fit_status = "full"

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
            "phot": phot_status,
            "fit": fit_status,
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
    of the same color and keeps the suffix in the label. Anything else falls
    through to the neutral 'other' colour with the original label.
    Deduplicates so a target with both 'g' and 'gp' shows a single 'g' chip.
    """
    chips: list[dict] = []
    seen: set[tuple[str, str, bool]] = set()
    for f in filters or []:
        if not f:
            continue
        narrow = f.endswith("_narrow")
        base = f[:-7] if narrow else f
        color = _FILTER_COLOR_ALIAS.get(base) or _FILTER_COLOR_ALIAS.get(base.lower(), "other")
        label = (color if color != "other" else base) + ("_narrow" if narrow else "")
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
        conn.executescript(SCHEMA)
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
        conn.executescript(SCHEMA)
        conn.execute("DELETE FROM target_notes WHERE object = ?", (obj,))
        conn.commit()
    clear_all_caches()


def set_identified(db_path: str, obj: str, is_identified: int) -> None:
    with get_conn(db_path) as conn:
        conn.executescript(SCHEMA)
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
        conn.executescript(SCHEMA)
        cur = conn.execute("SELECT object, is_identified FROM target_overrides")
        return {row[0]: bool(row[1]) for row in cur.fetchall()}


def get_frames(db_path: str, instrument: str, obsdate: str, ccd: int) -> list[dict]:
    with get_conn(db_path) as conn:
        cur = conn.execute(
            """SELECT * FROM frames
               WHERE instrument = ? AND obsdate = ? AND ccd = ?
               ORDER BY filename""",
            (instrument, obsdate, ccd),
        )
        columns = [d[0] for d in cur.description]
        return [dict(zip(columns, r)) for r in cur.fetchall()]


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


def _ensure_jobs_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)
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
    import getpass
    if user_name is None:
        user_name = getpass.getuser()
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


def save_ephemeris_view(state: dict) -> dict:
    """Persist a deterministic ephemeris page view and return its slug."""
    path = db_path()
    slug, state_hash, state_json = ephemeris_view_slug(state)
    targets = state.get("targets") if isinstance(state, dict) else []
    if not isinstance(targets, list):
        targets = []
    targets_json = _canonical_json([str(t) for t in targets])

    with get_conn(path) as conn:
        conn.executescript(SCHEMA)
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
        conn.executescript(SCHEMA)
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
