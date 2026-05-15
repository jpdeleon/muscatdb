from __future__ import annotations

import csv
import os
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
"""


def build_db(db_path: str) -> int:
    conn = sqlite3.connect(db_path)
    conn.executescript(SCHEMA)
    conn.executescript("DELETE FROM frames; DELETE FROM summaries;")
    count = 0
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
                csv_path = f"{obsdir}/{fname}"
                try:
                    ccd = int(fname.rstrip(".csv").rsplit("-ccd", 1)[1])
                except (IndexError, ValueError):
                    continue
                with open(csv_path) as f:
                    reader = csv.DictReader(f)
                    for row in reader:
                        inst_cfg = INSTRUMENTS[inst_name]
                        airmass_key = inst_cfg.airmass_key
                        focus_key = inst_cfg.focus_label
                        pa_key = "PA (deg)" if inst_cfg.has_pa else None
                        conn.execute(
                            """INSERT INTO frames
                               (instrument, obsdate, ccd, filename, object, jd_start, ut_start,
                                exptime, read_mode, filter, ra, declination, airmass, focus, pa)
                               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                            (
                                inst_name, entry, ccd,
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
                            ),
                        )
                        count += 1
    conn.commit()

    rows = conn.execute(
        """SELECT instrument, obsdate, ccd, object, exptime, read_mode,
                  MIN(filename), MAX(filename),
                  MIN(ut_start), MAX(ut_start), COUNT(*)
           FROM frames
           GROUP BY instrument, obsdate, ccd, object, exptime, read_mode"""
    ).fetchall()
    for r in rows:
        conn.execute(
            """INSERT INTO summaries
               (instrument, obsdate, ccd, object, exptime, read_mode,
                frame_start, frame_end, ut_start, ut_end, nframes)
               VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
            r,
        )
    conn.commit()
    conn.close()
    return count


def _safe_float(v: str) -> float | None:
    try:
        return float(v)
    except (ValueError, TypeError):
        return None


def get_instruments(db_path: str) -> list[dict]:
    conn = sqlite3.connect(db_path)
    cur = conn.execute("SELECT DISTINCT instrument FROM frames ORDER BY instrument")
    result = [{"name": r[0]} for r in cur.fetchall()]
    conn.close()
    return result


def get_dates(db_path: str, instrument: str) -> list[dict]:
    conn = sqlite3.connect(db_path)
    cur = conn.execute(
        """SELECT obsdate, COUNT(DISTINCT ccd), COUNT(*)
           FROM frames WHERE instrument = ?
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


def get_targets(db_path: str) -> list[dict]:
    """Return a per-target summary: dates observed, instruments, filters, exposure."""
    conn = sqlite3.connect(db_path)
    cur = conn.execute(
        """SELECT
              object,
              COUNT(DISTINCT obsdate)            AS n_dates,
              COUNT(*)                           AS n_frames,
              GROUP_CONCAT(DISTINCT instrument)  AS instruments,
              GROUP_CONCAT(DISTINCT obsdate)     AS dates,
              GROUP_CONCAT(DISTINCT filter)      AS filters,
              SUM(COALESCE(exptime, 0))          AS total_exptime,
              MAX(ra)                            AS ra,
              MAX(declination)                   AS declination,
              MIN(NULLIF(airmass, 0))            AS airmass_min,
              MAX(NULLIF(airmass, 0))            AS airmass_max
           FROM frames
           WHERE object IS NOT NULL
             AND TRIM(object) <> ''
             AND LOWER(TRIM(object)) NOT IN ('muscat', 'muscat_fast', 'test', 'tic', 'dark', 'bias', 'movie', 'misc', 'misc.', 'focus_adjust', 'fov')
             AND LOWER(TRIM(object)) NOT LIKE 'flat%'
             AND LOWER(TRIM(object)) NOT LIKE 'dark%'
             AND LOWER(TRIM(object)) NOT LIKE 'bias%'
             AND LOWER(TRIM(object)) NOT LIKE 'muscat commission%'
           GROUP BY object
           ORDER BY object COLLATE NOCASE"""
    )
    result = []
    for r in cur.fetchall():
        dates = sorted(set((r[4] or "").split(","))) if r[4] else []
        filters = sorted(f for f in set((r[5] or "").split(",")) if f) if r[5] else []
        total_s = r[6] or 0.0
        result.append({
            "object": r[0],
            "n_dates": r[1],
            "n_frames": r[2],
            "instruments": sorted(set((r[3] or "").split(","))) if r[3] else [],
            "dates": dates,
            "filters": filters,
            "total_exptime_hr": round(total_s / 3600.0, 2),
            "ra": r[7] or "",
            "declination": r[8] or "",
            "airmass_min": r[9],
            "airmass_max": r[10],
        })
    conn.close()
    return result


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
