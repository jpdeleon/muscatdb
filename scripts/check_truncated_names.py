#!/usr/bin/env python3
"""Check whether suspicious target names (digits + a single trailing letter,
e.g. ``138971B``) in the muscat-db SQLite database are actually truncated
values produced by the legacy Perl scanner.

The hypothesis: a real FITS ``OBJECT`` header like ``'HD 138971 B'`` or
``'TIC 138971 b'`` may have been whitespace-split by the old Perl scripts,
leaving only ``138971`` (or ``138971B`` if columns shifted) in the obslog CSV.

For each suspicious target this script:
  1. Finds every (instrument, obsdate) pair where the name appears.
  2. Re-scans those dates with the modern Python scanner (overwrites the CSV).
  3. Re-reads the freshly-written CSV and reports the OBJECT names found there.
  4. Flags a discrepancy when the rescanned OBJECT differs from the DB value.

This script does NOT modify the database. It DOES overwrite obslog CSVs on disk
for any dates it rescans (those CSVs were going to be regenerated anyway).

Usage (from the project root):

    python scripts/check_truncated_names.py              # rescan every suspect
    python scripts/check_truncated_names.py --dry-run    # list only, no rescan
    python scripts/check_truncated_names.py --limit 5    # check first 5 only
    python scripts/check_truncated_names.py --pattern '^\\d+[A-Za-z]$'
"""
from __future__ import annotations

import argparse
import csv
import re
import sqlite3
import sys
from collections import defaultdict
from pathlib import Path

# Allow running directly from project root without installing.
HERE = Path(__file__).resolve().parent
SRC = HERE.parent / "src"
if SRC.is_dir() and str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from muscat_db.scanner import scan_date, OBSLOG_BASE  # noqa: E402


DEFAULT_PATTERN = r"^\d+[A-Za-z]$"


def find_suspects(conn: sqlite3.Connection, pattern: re.Pattern) -> list[str]:
    """Return every object in the targets table matching the pattern."""
    rows = conn.execute("SELECT object FROM targets").fetchall()
    return sorted(o for (o,) in rows if pattern.match(o))


def find_appearances(
    conn: sqlite3.Connection, obj: str
) -> list[tuple[str, str, int]]:
    """Return [(instrument, obsdate, n_frames)] tuples for one OBJECT name."""
    return conn.execute(
        """SELECT instrument, obsdate, COUNT(*) AS n
           FROM frames
           WHERE object = ?
           GROUP BY instrument, obsdate
           ORDER BY instrument, obsdate""",
        (obj,),
    ).fetchall()


def read_objects_from_csvs(inst: str, obsdate: str) -> dict[str, int]:
    """Tally OBJECT values across all CCD CSVs for one (inst, date)."""
    counts: dict[str, int] = defaultdict(int)
    logdir = Path(OBSLOG_BASE) / inst / obsdate
    for csv_path in sorted(logdir.glob(f"obslog-{inst}-{obsdate}-ccd*.csv")):
        try:
            with csv_path.open() as f:
                reader = csv.DictReader(f)
                for row in reader:
                    name = (row.get("OBJECT") or "").strip()
                    if name:
                        counts[name] += 1
        except (OSError, csv.Error) as e:
            print(f"    [warn] cannot read {csv_path.name}: {e}")
    return dict(counts)


def likely_originals(suspect: str, candidates: list[str]) -> list[str]:
    """Pick rescanned names that probably correspond to the suspect.

    Looks for names containing either the digit prefix or the letter suffix.
    """
    digits = suspect.rstrip("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz")
    return [c for c in candidates if digits and digits in c]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", default="muscat.db", help="SQLite DB path")
    parser.add_argument(
        "--pattern", default=DEFAULT_PATTERN,
        help=f"Regex for suspicious OBJECT names (default: {DEFAULT_PATTERN!r})",
    )
    parser.add_argument(
        "--limit", type=int, default=None,
        help="Stop after checking this many suspicious targets",
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="List suspects only; do not rescan or write any CSVs",
    )
    parser.add_argument(
        "--workers", type=int, default=None,
        help="Worker processes per scan_date call (default: cpu_count)",
    )
    args = parser.parse_args()

    pattern = re.compile(args.pattern)
    conn = sqlite3.connect(args.db)

    suspects = find_suspects(conn, pattern)
    print(f"Found {len(suspects)} suspicious target names matching {args.pattern!r}")
    if not suspects:
        return 0

    if args.dry_run:
        for obj in suspects:
            combos = find_appearances(conn, obj)
            spread = ", ".join(f"{i}/{d} ({n})" for i, d, n in combos)
            print(f"  {obj:15s}  -> {spread}")
        return 0

    truncated = []
    unchanged = []
    missing_data = []

    for i, obj in enumerate(suspects):
        if args.limit and i >= args.limit:
            print(f"\n[stopped after --limit {args.limit}]")
            break
        combos = find_appearances(conn, obj)
        print(f"\n=== {obj!r}  ({sum(n for _,_,n in combos)} frames "
              f"across {len(combos)} (inst,date) combos) ===")
        for inst, date, n in combos:
            print(f"  rescanning {inst} {date}  (was {n} frames as OBJECT={obj!r})")
            try:
                result = scan_date(inst, date, max_workers=args.workers)
            except Exception as e:
                print(f"    [warn] scan_date failed: {type(e).__name__}: {e}")
                continue
            if not result:
                print("    SKIP: no FITS data found on disk")
                missing_data.append((obj, inst, date))
                continue
            new_counts = read_objects_from_csvs(inst, date)
            if obj in new_counts:
                print(f"    UNCHANGED: OBJECT={obj!r} still present ({new_counts[obj]} frames)")
                unchanged.append((obj, inst, date))
            else:
                originals = likely_originals(obj, list(new_counts))
                if originals:
                    print(f"    TRUNCATED?  was {obj!r}; rescan shows: "
                          + ", ".join(f"{o!r}={new_counts[o]}" for o in originals))
                    truncated.append((obj, inst, date, originals))
                else:
                    sample = sorted(new_counts.items(), key=lambda x: -x[1])[:5]
                    print("    DIFFERENT: rescan top OBJECTs: "
                          + ", ".join(f"{o!r}={c}" for o, c in sample))
                    truncated.append((obj, inst, date, [o for o, _ in sample]))

    # Summary
    print("\n" + "=" * 60)
    print("SUMMARY")
    print(f"  unchanged    : {len(unchanged)}  (DB OBJECT matches FITS — not truncated)")
    print(f"  truncated    : {len(truncated)}  (DB OBJECT differs from FITS)")
    print(f"  missing data : {len(missing_data)}  (no FITS files on disk)")
    if truncated:
        print("\nLikely Perl-era truncations:")
        for obj, inst, date, originals in truncated:
            print(f"  {obj:15s}  {inst}/{date}  -> {originals}")
        print("\nAfter reviewing, run `muscat-db build-db` to refresh the DB "
              "from the rescanned CSVs.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
