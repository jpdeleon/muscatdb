#!/usr/bin/env python3
"""Scan the muscat-db SQLite database for frames with column-shift artifacts
from the legacy Perl scanner.

Symptom: a single frame in an otherwise-good observation has a non-coordinate
value in ``ra`` (e.g. ``|``), a non-numeric ``declination`` (e.g. ``0``), or a
filter value full of junk characters. Because the homepage targets aggregation
uses ``MAX(ra)``, one bad frame can pollute the displayed RA/Dec for an
otherwise legitimate target (TOI1453 was the canonical example: 24 good
``17:12:39.xx`` rows plus a single ``|``, which sorts higher in SQLite and
wins ``MAX``).

For every (instrument, obsdate) pair that contains at least one bad frame this
script re-runs the modern Python scanner with force-overwrite semantics, so the
legacy CSV is replaced with a clean one. It does NOT modify the database;
re-run ``muscat-db build-db`` afterwards to fold the corrections in.

Bad-column heuristics:
  * ``ra='|'`` or ``len(ra) <= 1``
  * ``declination='|'`` or ``len(declination) <= 1``
  * ``filter`` contains any of ``| } { ~``  or is purely numeric

Usage (from the project root):

    python scripts/check_bad_columns.py                # show + rescan
    python scripts/check_bad_columns.py --dry-run      # show only
    python scripts/check_bad_columns.py --limit 5      # rescan first 5 dates
    python scripts/check_bad_columns.py --instrument muscat
"""
from __future__ import annotations

import argparse
import re
import sqlite3
import sys
from collections import defaultdict
from pathlib import Path

HERE = Path(__file__).resolve().parent
SRC = HERE.parent / "src"
if SRC.is_dir() and str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from muscat_db.scanner import scan_date  # noqa: E402


# A frame is suspect if ANY of these SQL conditions matches.
SUSPECT_FRAME_SQL = """
       length(ra) <= 1
    OR ra GLOB '[|/\\\\]'
    OR length(declination) <= 1
    OR declination GLOB '[|/\\\\]'
    OR filter LIKE '%|%'
    OR filter LIKE '%}%'
    OR filter LIKE '%{%'
    OR filter LIKE '%~%'
    OR filter LIKE '%:%'
"""


def find_bad_combos(
    conn: sqlite3.Connection, instrument: str | None
) -> list[tuple[str, str, int, list[str]]]:
    """Return [(instrument, obsdate, n_bad, sample_objects)] tuples."""
    inst_filter = ""
    params: tuple = ()
    if instrument:
        inst_filter = "instrument = ? AND"
        params = (instrument,)
    sql = f"""
        SELECT instrument, obsdate, COUNT(*) AS n,
               GROUP_CONCAT(DISTINCT object) AS sample_objects
        FROM frames
        WHERE {inst_filter} ({SUSPECT_FRAME_SQL})
        GROUP BY instrument, obsdate
        ORDER BY instrument, obsdate
    """
    rows = conn.execute(sql, params).fetchall()
    return [
        (inst, date, n, sorted(set((objs or "").split(",")))[:6])
        for (inst, date, n, objs) in rows
    ]


def sample_bad_frames(
    conn: sqlite3.Connection, inst: str, date: str, limit: int = 3
) -> list[tuple]:
    return conn.execute(
        f"""SELECT filename, object, ra, declination, filter, airmass
            FROM frames
            WHERE instrument = ? AND obsdate = ? AND ({SUSPECT_FRAME_SQL})
            LIMIT ?""",
        (inst, date, limit),
    ).fetchall()


# Only YYMMDD obsdates can be rescanned (legacy dirs like csv_old_220914 have
# no corresponding /data/<inst>/<date> FITS source).
YYMMDD = re.compile(r"^\d{6}$")


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--db", default="muscat.db", help="SQLite DB path")
    p.add_argument("--instrument", default=None,
                   help="Limit to one instrument (default: all)")
    p.add_argument("--limit", type=int, default=None,
                   help="Only rescan the first N (instrument, date) pairs")
    p.add_argument("--dry-run", action="store_true",
                   help="List affected combos only, do not rescan")
    p.add_argument("--workers", type=int, default=None,
                   help="Worker processes per scan_date call")
    args = p.parse_args()

    conn = sqlite3.connect(args.db)
    combos = find_bad_combos(conn, args.instrument)
    if not combos:
        print("No frames matching bad-column heuristics found.")
        return 0

    # Split into rescannable (YYMMDD) and non-rescannable (legacy dirs).
    rescannable = [c for c in combos if YYMMDD.match(c[1])]
    legacy = [c for c in combos if not YYMMDD.match(c[1])]

    total_bad = sum(c[2] for c in combos)
    print(f"Found {total_bad} suspect frames across {len(combos)} "
          f"(instrument, date) combos")
    print(f"  rescannable (YYMMDD): {len(rescannable)}")
    print(f"  legacy (skipped):     {len(legacy)}")
    print()

    print("Affected combos (top 30):")
    for inst, date, n, objects in combos[:30]:
        tag = "" if YYMMDD.match(date) else "  [legacy, will skip]"
        print(f"  {inst:9s} {date:18s} {n:4d} bad frames  objects={objects}{tag}")
    if len(combos) > 30:
        print(f"  ... and {len(combos) - 30} more")

    if args.dry_run:
        print("\n[--dry-run set; no rescans performed]")
        return 0

    print("\n=== rescanning ===")
    rescanned: list[tuple[str, str]] = []
    failed: list[tuple[str, str, str]] = []
    for i, (inst, date, n, objects) in enumerate(rescannable):
        if args.limit and i >= args.limit:
            print(f"[stopped after --limit {args.limit}]")
            break
        print(f"\n  {inst} {date}  ({n} bad frames; objects={objects})")
        before = sample_bad_frames(conn, inst, date, limit=2)
        for row in before:
            print(f"    BEFORE: {row}")
        try:
            result = scan_date(inst, date, max_workers=args.workers)
        except Exception as e:
            msg = f"{type(e).__name__}: {e}"
            print(f"    [warn] scan_date failed: {msg}")
            failed.append((inst, date, msg))
            continue
        if not result:
            print("    [skip] no FITS data on disk for this date")
            failed.append((inst, date, "no FITS data"))
            continue
        print(f"    rescanned: {result.get('total')} frames written to CSVs")
        rescanned.append((inst, date))

    print("\n" + "=" * 60)
    print("SUMMARY")
    print(f"  rescanned successfully: {len(rescanned)}")
    print(f"  failed / skipped      : {len(failed)}")
    print(f"  legacy dirs (untouched): {len(legacy)}")
    if rescanned:
        print("\nNext step: refresh the database with the corrected CSVs:")
        print("  muscat-db build-db")
    if legacy:
        print("\nLegacy dirs that cannot be rescanned (no corresponding "
              "/data/<inst>/<date> source):")
        for inst, date, n, _ in legacy:
            print(f"  {inst} {date}  ({n} bad frames)")
        print("These are usually leftover from the Perl-era workflow and can "
              "safely be excluded by deleting the obslog dir.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
