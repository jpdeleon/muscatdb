from __future__ import annotations

import csv
import os
import re
import datetime
import sqlite3

from muscat_db.instruments import INSTRUMENTS, OBSLOG_BASE


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
    nframes     INTEGER
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
    run_type     TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS db_meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""


def build_db(db_path: str, progress=None) -> int:
    """Rebuild the SQLite database from obslog CSVs.

    If ``progress`` is a ``rich.progress.Progress`` instance, three tasks are
    reported: CSV ingestion, summary aggregation, and targets aggregation.

    Builds to a temporary file first, then atomically replaces the target
    so a concurrently-running web server is never blocked by ``DROP TABLE``.
    """
    tmp_path = db_path + ".tmp"

    # Preserve jobs from existing database so they survive the rebuild.
    preserved_jobs: list[tuple] = []
    if os.path.exists(db_path):
        try:
            old_conn = sqlite3.connect(db_path)
            old_conn.row_factory = sqlite3.Row
            rows = old_conn.execute("SELECT * FROM jobs").fetchall()
            preserved_jobs = [tuple(r) for r in rows]
            old_conn.close()
        except sqlite3.OperationalError:
            pass

    # Phase 1: discover all CSVs (cheap walk so we can size the progress bar).
    csv_jobs: list[tuple[str, str, str, int]] = []  # (inst, obsdate, csv_path, ccd)
    for inst_name in INSTRUMENTS:
        inst_dir = f"{OBSLOG_BASE}/{inst_name}"
        if not os.path.isdir(inst_dir):
            continue
        for entry in sorted(os.listdir(inst_dir)):
            obsdir = f"{inst_dir}/{entry}"
            if not os.path.isdir(obsdir) or entry in ("csv", "html", "muscat", "muscat2", "muscat3", "muscat4", "sinistro"):
                continue
            for fname in sorted(os.listdir(obsdir)):
                if not fname.endswith(".csv") or not fname.startswith("obslog-"):
                    continue
                try:
                    ccd = int(fname.rstrip(".csv").rsplit("-ccd", 1)[1])
                except (IndexError, ValueError):
                    continue
                csv_jobs.append((inst_name, entry, f"{obsdir}/{fname}", ccd))

    try:
        conn = sqlite3.connect(tmp_path)
        conn.execute("PRAGMA journal_mode=WAL;")
        conn.execute("PRAGMA synchronous=OFF;")
        conn.execute("PRAGMA cache_size=100000;")
        conn.executescript("DROP TABLE IF EXISTS frames; DROP TABLE IF EXISTS summaries; DROP TABLE IF EXISTS targets;")
        conn.executescript(SCHEMA)
        conn.execute("DROP INDEX IF EXISTS idx_frames_inst_date;")
        conn.execute("DROP INDEX IF EXISTS idx_frames_object;")
        conn.execute("DROP INDEX IF EXISTS idx_summaries_inst_date;")

        # Phase 2: ingest frames.
        ingest_task = None
        if progress is not None:
            ingest_task = progress.add_task(
                "[cyan]Ingesting CSVs[/]", total=len(csv_jobs), filename="",
            )

        count = 0
        for inst_name, obsdate, csv_path, ccd in csv_jobs:
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
        conn.execute("CREATE INDEX IF NOT EXISTS idx_frames_inst_date ON frames(instrument, obsdate);")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_frames_object ON frames(object);")
        conn.commit()

        # Phase 3: build summaries.
        summary_task = None
        if progress is not None:
            summary_task = progress.add_task(
                "[cyan]Building summaries[/]", total=None, filename="",
            )
        rows = conn.execute(
            """SELECT instrument, obsdate, ccd, object, exptime, read_mode,
                      MIN(filename), MAX(filename),
                      MIN(ut_start), MAX(ut_start), COUNT(*)
               FROM frames
               GROUP BY instrument, obsdate, ccd, object, exptime, read_mode"""
        ).fetchall()
        if progress is not None:
            progress.update(summary_task, total=len(rows))
        if rows:
            conn.executemany(
                """INSERT INTO summaries
                   (instrument, obsdate, ccd, object, exptime, read_mode,
                    frame_start, frame_end, ut_start, ut_end, nframes)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
                rows,
            )
        if progress is not None:
            progress.update(summary_task, completed=len(rows))
        conn.execute("CREATE INDEX IF NOT EXISTS idx_summaries_inst_date ON summaries(instrument, obsdate);")
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
        conn.execute(
            "INSERT OR REPLACE INTO db_meta (key, value) VALUES ('last_build_at', ?)",
            (datetime.datetime.now().isoformat(),)
        )

        # Restore preserved jobs so build-db doesn't wipe job history.
        for job in preserved_jobs:
            conn.execute(
                "INSERT OR REPLACE INTO jobs VALUES (?,?,?,?,?,?,?,?,?,?)", job
            )

        conn.commit()
        conn.close()
    except BaseException:
        try:
            os.remove(tmp_path)
        except OSError:
            pass
        raise

    os.replace(tmp_path, db_path)
    return count


_TARGET_EXCLUDE_EXACT = (
    "muscat", "muscat_fast", "test", "tic", "dark", "bias",
    "movie", "misc", "misc.", "focus_adjust", "fov",
)


def _populate_targets(conn: sqlite3.Connection) -> None:
    """Aggregate per-target summary into the targets table."""
    cur = conn.execute(
        """SELECT
              object,
              COUNT(DISTINCT obsdate)            AS n_dates,
              COUNT(*)                           AS n_frames,
              GROUP_CONCAT(DISTINCT instrument)  AS instruments,
              GROUP_CONCAT(DISTINCT obsdate)     AS dates,
              GROUP_CONCAT(DISTINCT instrument || ':' || obsdate) AS inst_dates,
              GROUP_CONCAT(DISTINCT filter)      AS filters,
              SUM(COALESCE(exptime, 0))          AS total_exptime,
              MAX(ra)                            AS ra,
              MAX(declination)                   AS declination,
              MIN(NULLIF(airmass, 0))            AS airmass_min,
              MAX(NULLIF(airmass, 0))            AS airmass_max,
              CASE WHEN object GLOB '*[A-Za-z]*' THEN 1 ELSE 0 END
                                                 AS is_identified
           FROM frames
           WHERE object IS NOT NULL
             AND TRIM(object) <> ''
             AND LOWER(TRIM(object)) NOT IN ({exact})
             AND LOWER(TRIM(object)) NOT LIKE '%flat%'
             AND LOWER(TRIM(object)) NOT LIKE 'dark%'
             AND LOWER(TRIM(object)) NOT LIKE 'bias%'
             AND LOWER(TRIM(object)) NOT LIKE 'muscat commission%'
             AND LOWER(TRIM(object)) NOT LIKE '%test%'
             AND LOWER(TRIM(object)) NOT LIKE '%pinhole%'
             AND LOWER(TRIM(object)) NOT LIKE '%pointing%'
             AND LOWER(TRIM(object)) NOT LIKE '%dust_spot%'
             AND LOWER(TRIM(object)) NOT LIKE '%dust spot%'
             AND LOWER(TRIM(object)) NOT LIKE '%domeflat%'
             AND LOWER(TRIM(object)) NOT LIKE '%dome flat%'
             AND TRIM(object) NOT GLOB '*:*:*'
             AND TRIM(object) NOT GLOB '[0-9]*.[0-9]*'
             -- Exclude pointing-frame names: pure P<digits>, length 1-4 digits.
             AND TRIM(object) NOT GLOB '[Pp][0-9]'
             AND TRIM(object) NOT GLOB '[Pp][0-9][0-9]'
             AND TRIM(object) NOT GLOB '[Pp][0-9][0-9][0-9]'
             AND TRIM(object) NOT GLOB '[Pp][0-9][0-9][0-9][0-9]'
           GROUP BY object""".format(
            exact=", ".join(f"'{s}'" for s in _TARGET_EXCLUDE_EXACT),
        )
    )
    rows = cur.fetchall()
    conn.executemany(
        """INSERT INTO targets
           (object, n_dates, n_frames, instruments, dates, inst_dates,
            filters, total_exptime, ra, declination, airmass_min, airmass_max,
            is_identified)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        rows,
    )


def _safe_float(v: str) -> float | None:
    try:
        return float(v)
    except (ValueError, TypeError):
        return None


def get_instruments(db_path: str) -> list[dict]:
    conn = sqlite3.connect(db_path)
    cur = conn.execute("SELECT DISTINCT instrument FROM summaries ORDER BY instrument")
    result = [{"name": r[0]} for r in cur.fetchall()]
    conn.close()
    return result


def get_instruments_summary(db_path: str) -> list[dict]:
    """Return count of dates, frames, and targets for all instruments."""
    conn = sqlite3.connect(db_path)
    cur = conn.execute("SELECT DISTINCT instrument FROM summaries ORDER BY instrument")
    instruments = [r[0] for r in cur.fetchall()]
    
    result = []
    for inst in instruments:
        cur_stats = conn.execute(
            """SELECT COUNT(DISTINCT obsdate), SUM(nframes)
               FROM summaries
               WHERE instrument = ?""",
            (inst,)
        )
        n_dates, n_frames = cur_stats.fetchone()
        
        cur_targets = conn.execute(
            """SELECT COUNT(DISTINCT object) FROM summaries
               WHERE instrument = ?
                 AND object IS NOT NULL AND TRIM(object) <> ''
                 AND LOWER(TRIM(object)) NOT IN ({exact})
                 AND LOWER(TRIM(object)) NOT LIKE '%flat%'
                 AND LOWER(TRIM(object)) NOT LIKE 'dark%'
                 AND LOWER(TRIM(object)) NOT LIKE 'bias%'
                 AND LOWER(TRIM(object)) NOT LIKE '%test%'
                 AND TRIM(object) NOT GLOB '*:*:*'""".format(
                exact=", ".join(f"'{s}'" for s in _TARGET_EXCLUDE_EXACT)
            ),
            (inst,)
        )
        n_targets = cur_targets.fetchone()[0] or 0
        
        result.append({
            "name": inst,
            "n_dates": n_dates or 0,
            "n_frames": n_frames or 0,
            "n_targets": n_targets
        })
    conn.close()
    return result


def get_dates(db_path: str, instrument: str) -> list[dict]:
    """Return one row per obsdate. Only YYMMDD-formatted dates are returned;
    legacy/test directories like ``200722_2`` or ``csv_old_220914`` are skipped.
    """
    conn = sqlite3.connect(db_path)
    # Read from the pre-aggregated `summaries` table rather than `frames`: it is
    # ~1000x smaller per instrument and SUM(nframes) reproduces COUNT(*) over
    # frames exactly, turning a multi-second scan into a sub-second query.
    cur = conn.execute(
        """SELECT obsdate, COUNT(DISTINCT ccd), SUM(nframes)
           FROM summaries
           WHERE instrument = ?
             AND length(obsdate) = 6
             AND obsdate GLOB '[0-9][0-9][0-9][0-9][0-9][0-9]'
           GROUP BY obsdate ORDER BY obsdate DESC""",
        (instrument,),
    )
    result = [{"obsdate": r[0], "nccd": r[1], "nframes": r[2]} for r in cur.fetchall()]
    conn.close()
    return result


def get_summaries(db_path: str, instrument: str, obsdate: str) -> list[dict]:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    cur = conn.execute(
        """SELECT ccd, object, exptime, read_mode,
                  frame_start, frame_end, ut_start, ut_end, nframes
           FROM summaries
           WHERE instrument = ? AND obsdate = ?
           ORDER BY ccd, object, ut_start""",
        (instrument, obsdate),
    )
    result = [dict(r) for r in cur.fetchall()]
    conn.close()
    return result


def get_objects(db_path: str, instrument: str, obsdate: str) -> list[str]:
    """Distinct real-target object names observed on one instrument/date.

    Reuses the same calibration/junk exclusions as the materialized targets
    table so the photometry picker only offers genuine science targets.
    """
    conn = sqlite3.connect(db_path)
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
    result = [r[0] for r in cur.fetchall()]
    conn.close()
    return result


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
    conn = sqlite3.connect(db_path)
    conn.executescript(SCHEMA)  # ensure target_notes exists on first read
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
    conn.close()
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
    conn = sqlite3.connect(db_path, timeout=30)
    conn.executescript(SCHEMA)
    note = (note or "").strip()
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
    conn.close()


def delete_note(db_path: str, obj: str) -> None:
    conn = sqlite3.connect(db_path, timeout=30)
    conn.executescript(SCHEMA)
    conn.execute("DELETE FROM target_notes WHERE object = ?", (obj,))
    conn.commit()
    conn.close()


def get_frames(db_path: str, instrument: str, obsdate: str, ccd: int) -> list[dict]:
    conn = sqlite3.connect(db_path)
    cur = conn.execute(
        """SELECT * FROM frames
           WHERE instrument = ? AND obsdate = ? AND ccd = ?
           ORDER BY filename""",
        (instrument, obsdate, ccd),
    )
    columns = [d[0] for d in cur.description]
    result = [dict(zip(columns, r)) for r in cur.fetchall()]
    conn.close()
    return result


def db_path() -> str:
    import pathlib
    return str(pathlib.Path(os.environ.get("MUSCAT_DB_PATH", "muscat.db")).resolve())


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
    run_type: str = ""
) -> None:
    path = db_path()
    conn = sqlite3.connect(path, timeout=30)
    conn.executescript(SCHEMA)
    # Migration: add run_type column for databases created before this column existed.
    try:
        conn.execute("ALTER TABLE jobs ADD COLUMN run_type TEXT NOT NULL DEFAULT ''")
    except sqlite3.OperationalError:
        pass
    key = f"{type_}:{inst}/{date}/{target.replace(' ', '')}"
    conn.execute(
        """INSERT INTO jobs(key, type, instrument, obsdate, target, state, returncode, elapsed, started_at, error_desc, run_type)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
           ON CONFLICT(key) DO UPDATE SET
             state      = excluded.state,
             returncode = excluded.returncode,
             elapsed    = excluded.elapsed,
             error_desc = excluded.error_desc,
             run_type   = CASE WHEN excluded.run_type != '' THEN excluded.run_type ELSE run_type END""",
        (key, type_, inst, date, target, state, returncode, elapsed, started_at, error_desc, run_type)
    )
    conn.commit()
    conn.close()


def get_persisted_jobs() -> list[dict]:
    path = db_path()
    conn = sqlite3.connect(path)
    conn.executescript(SCHEMA)
    cur = conn.execute("SELECT * FROM jobs ORDER BY started_at DESC")
    columns = [d[0] for d in cur.description]
    result = []
    for r in cur.fetchall():
        d = dict(zip(columns, r))
        d["inst"] = d["instrument"]
        d["date"] = d["obsdate"]
        result.append(d)
    conn.close()
    return result


def get_last_build_date(db_path: str) -> str:
    """Get the date when muscat-db build was run, or the date when the database file was generated."""
    try:
        conn = sqlite3.connect(db_path)
        cur = conn.execute("SELECT value FROM db_meta WHERE key = 'last_build_at'")
        row = cur.fetchone()
        conn.close()
        if row:
            return row[0][:10]
    except sqlite3.Error:
        pass

    try:
        mtime = os.stat(db_path).st_mtime
        return datetime.datetime.fromtimestamp(mtime).strftime("%Y-%m-%d")
    except OSError:
        return datetime.date.today().strftime("%Y-%m-%d")

