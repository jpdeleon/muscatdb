#!/usr/bin/env python3
"""Remove rows from the muscat-db SQLite database whose ``obsdate`` does not
follow the canonical ``YYMMDD`` format (six digits).

Legacy obslog directories like ``csv_old_220914``, ``240905_org``, ``240722_1``,
``EKDra_2401``, ``HIP41378_2105`` etc. get picked up by ``build-db`` and
contaminate the materialized ``targets`` table even though the instrument page
already filters them out for display.

This script:
  1. Lists every non-YYMMDD obsdate currently in ``frames`` (with row counts).
  2. With confirmation, deletes those rows from ``frames`` and ``summaries``.
  3. Rebuilds the materialized ``targets`` table so the homepage reflects the
     cleanup.

Usage (from the project root):

    python scripts/clean_nonstandard_dates.py --dry-run   # list only
    python scripts/clean_nonstandard_dates.py             # prompt, then delete
    python scripts/clean_nonstandard_dates.py --yes       # non-interactive

This does NOT touch any files on disk. Only the SQLite database is modified.
"""
from __future__ import annotations

import argparse
import sqlite3
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
SRC = HERE.parent / "src"
if SRC.is_dir() and str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from muscat_db.database import _populate_targets  # noqa: E402


# Anything that isn't exactly six digits is junk.
WHERE_NONSTANDARD = """
    length(obsdate) <> 6
 OR obsdate NOT GLOB '[0-9][0-9][0-9][0-9][0-9][0-9]'
"""


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--db", default="muscat.db", help="SQLite DB path")
    p.add_argument("--dry-run", action="store_true",
                   help="List affected rows only; no DB changes")
    p.add_argument("--yes", action="store_true",
                   help="Skip the confirmation prompt")
    args = p.parse_args()

    conn = sqlite3.connect(args.db)
    bad = conn.execute(
        f"""SELECT instrument, obsdate, COUNT(*) AS n
            FROM frames
            WHERE {WHERE_NONSTANDARD}
            GROUP BY instrument, obsdate
            ORDER BY instrument, obsdate"""
    ).fetchall()

    if not bad:
        print("No non-YYMMDD obsdates found in frames. Database is already clean.")
        return 0

    total = sum(n for _, _, n in bad)
    print(f"Found {len(bad)} non-YYMMDD obsdate entries "
          f"covering {total} frames:\n")
    for inst, date, n in bad:
        print(f"  {inst:9s} {date:24s} {n:7d} frames")
    print()

    summaries_to_go = conn.execute(
        f"SELECT COUNT(*) FROM summaries WHERE {WHERE_NONSTANDARD}"
    ).fetchone()[0]
    print(f"Will also delete {summaries_to_go} matching rows from summaries.")

    if args.dry_run:
        print("\n[--dry-run set; no changes made]")
        return 0

    if not args.yes:
        ans = input("\nProceed with deletion? [y/N] ").strip().lower()
        if ans != "y":
            print("Aborted.")
            return 1

    print("\nDeleting…")
    conn.execute(f"DELETE FROM frames    WHERE {WHERE_NONSTANDARD}")
    conn.execute(f"DELETE FROM summaries WHERE {WHERE_NONSTANDARD}")
    conn.commit()

    print("Rebuilding targets table…")
    conn.execute("DELETE FROM targets")
    _populate_targets(conn)
    conn.commit()

    remaining = conn.execute(
        f"SELECT COUNT(*) FROM frames WHERE {WHERE_NONSTANDARD}"
    ).fetchone()[0]
    n_targets = conn.execute("SELECT COUNT(*) FROM targets").fetchone()[0]
    print(f"\nDone. Removed {total} frames; remaining non-YYMMDD rows: {remaining}.")
    print(f"Targets table now has {n_targets} rows.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
