#!/usr/bin/env python3
"""Fix malformed target names in the muscat-db SQLite database.

Three fix stages:
  Stage 1 — RESCAN: re-read the real FITS headers for dates where the legacy
            Perl pipeline wrote empty or corrupt OBJECT fields.
  Stage 2 — NORMALIZE: fix known case / spacing inconsistencies directly
            in the DB (e.g. 55Cnc → 55 Cnc, WASP33 → WASP-33).
  Stage 3 — REBUILD: re-run build_db() so summaries and targets tables
            reflect the corrected frames.

Usage:
  python scripts/fix_malformed_names.py --dry-run       # preview only
  python scripts/fix_malformed_names.py rescan           # rescan + rebuild
  python scripts/fix_malformed_names.py normalize        # normalize + rebuild
  python scripts/fix_malformed_names.py all              # full pipeline
"""
from __future__ import annotations

import argparse
import csv
import os
import sqlite3
import sys
import time
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path

HERE = Path(__file__).resolve().parent
SRC = HERE.parent / "src"
if SRC.is_dir() and str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from muscat_db.instruments import INSTRUMENTS, OBSLOG_BASE
from muscat_db.scanner import _process_single_file
from muscat_db.database import build_db, db_path as _db_path


# ── Known normalization maps ────────────────────────────────────────────────
# (old_name -> corrected_name)

CASE_NORMALIZE: dict[str, str] = {
    "55CNC": "55 Cnc",
    "55CnC": "55 Cnc",
    "55Cnc": "55 Cnc",
    "WASP33": "WASP-33",
    "WASP43": "WASP-43",
    "WASP104": "WASP-104",
    "WASP12b": "WASP-12b",
    "wasp33": "WASP-33",
    "wasp43": "WASP-43",
    "wasp104": "WASP-104",
    "KELT-9": "KELT-9b",
}

# Targets missing catalog prefix (verified manually by coordinate cross-match)
KNOWN_PREFIX_FIXES: dict[str, str] = {
    "65803": "(65803) Didymos",
    "65803A": "(65803) Didymos A",
    "65803B": "(65803) Didymos B",
    "65803C": "(65803) Didymos C",
    "65803D": "(65803) Didymos D",
    "29P": "29P/Schwassmann-Wachmann",
}


# ── Helpers ─────────────────────────────────────────────────────────────────

def db() -> sqlite3.Connection:
    conn = sqlite3.connect(_db_path(), timeout=30)
    conn.row_factory = sqlite3.Row
    return conn


def find_empty_object_dates(conn: sqlite3.Connection) -> list[tuple[str, str]]:
    """Return (instrument, obsdate) for dates with empty OBJECT, excluding csv_old."""
    cur = conn.execute(
        """SELECT DISTINCT instrument, obsdate FROM frames
           WHERE object = '' AND obsdate NOT LIKE 'csv_old%'
           ORDER BY instrument, obsdate"""
    )
    return [(r["instrument"], r["obsdate"]) for r in cur.fetchall()]


# ── Stage 1: rescan ─────────────────────────────────────────────────────────

def rescan_affected_dates(
    pairs: list[tuple[str, str]],
    max_workers: int = 8,
    dry_run: bool = False,
) -> int:
    """Re-scan FITS headers for each (instrument, date) pair.

    Writes corrected obslog CSVs.  Returns number of pairs processed.
    """
    # Build flat task list: (filepath, instrument_config, ccd, obsdate, inst_name)
    tasks: list[tuple[str, object, int, str, str]] = []
    for inst_name, obsdate in pairs:
        inst = INSTRUMENTS[inst_name]
        datadir = f"{inst.data_dir}/{obsdate}"
        if not os.path.isdir(datadir):
            print(f"  SKIP {inst_name}/{obsdate} — data dir not found")
            continue
        try:
            all_files = os.listdir(datadir)
        except (PermissionError, OSError):
            continue
        for ccd in range(inst.nccd):
            if inst.ep_names:
                ep = inst.ep_names[ccd]
                prefix = f"{inst.prefix}{ep}"
            else:
                prefix = f"{inst.prefix}{ccd}"
            for fname in all_files:
                if fname.startswith(prefix) and fname.endswith(".fits"):
                    fp = f"{datadir}/{fname}"
                    tasks.append((fp, inst, ccd, obsdate, inst_name))

    if not tasks:
        print("  No FITS files found.")
        return 0

    if dry_run:
        print(f"  Would rescan {len(tasks)} files across {len(pairs)} dates")
        return 0

    print(f"  Processing {len(tasks)} FITS files across {len(pairs)} dates "
          f"({max_workers} workers)...")

    paths = [t[0] for t in tasks]
    configs = [t[1] for t in tasks]
    chunksize = max(1, len(tasks) // (max_workers * 4))

    # Group results by (inst, date, ccd)
    rows_by_group: dict[tuple[str, str, int], list[dict]] = {}
    start = time.time()
    last_report = 0
    report_interval = max(1, len(tasks) // 40)

    with ProcessPoolExecutor(max_workers=max_workers) as executor:
        for idx, row in enumerate(
            executor.map(_process_single_file, paths, configs, chunksize=chunksize)
        ):
            if row:
                fp, inst, ccd, obsdate, inst_name = tasks[idx]
                rows_by_group.setdefault((inst_name, obsdate, ccd), []).append(row)
            if idx >= last_report + report_interval:
                pct = (idx + 1) / len(tasks) * 100
                elapsed = time.time() - start
                rate = (idx + 1) / elapsed if elapsed else 0
                eta = (len(tasks) - idx - 1) / rate if rate else 0
                print(f"    {pct:.0f}% ({idx+1}/{len(tasks)})  "
                      f"{rate:.0f} f/s  ETA {eta:.0f}s")
                last_report = idx

    elapsed = time.time() - start
    print(f"    100% in {elapsed:.0f}s — {len(rows_by_group)} groups")

    # Write CSVs
    csv_count = 0
    row_count = 0
    for (inst_name, obsdate, ccd), rows in rows_by_group.items():
        inst = INSTRUMENTS[inst_name]
        logdir = f"{OBSLOG_BASE}/{inst_name}/{obsdate}"
        os.makedirs(logdir, exist_ok=True)
        csv_path = f"{logdir}/obslog-{inst_name}-{obsdate}-ccd{ccd}.csv"
        fieldnames = inst.csv_header.split(",")
        try:
            with open(csv_path, "w", newline="") as f:
                w = csv.DictWriter(f, fieldnames=fieldnames)
                w.writeheader()
                for row in sorted(rows, key=lambda r: r["FRAME"]):
                    w.writerow({k: row.get(k, "") for k in fieldnames})
            csv_count += 1
            row_count += len(rows)
        except (PermissionError, OSError) as e:
            print(f"    [warn] cannot write {csv_path}: {e}")

    print(f"  Wrote {csv_count} CSV files ({row_count} rows)")
    return len(pairs)


# ── Stage 2: normalize ─────────────────────────────────────────────────────

def normalize_names(conn: sqlite3.Connection, corrections: dict[str, str],
                    dry_run: bool = False) -> int:
    """Apply name corrections directly to frames table.  Returns rows updated."""
    total = 0
    for old, new in corrections.items():
        if old == new:
            continue
        cur = conn.execute("UPDATE frames SET object = ? WHERE object = ?", (new, old))
        if cur.rowcount:
            total += cur.rowcount
            action = "Would rename" if dry_run else "Renamed"
            print(f"  {action} {old!r} -> {new!r} ({cur.rowcount} rows)")
    return total


# ── Stage 3: rebuild ────────────────────────────────────────────────────────

def rebuild(db_path: str | None = None) -> float:
    t0 = time.time()
    build_db(db_path or _db_path())
    return time.time() - t0


# ── Summary ──────────────────────────────────────────────────────────────────

def print_summary(conn: sqlite3.Connection, label: str = "Current state"):
    total = conn.execute("SELECT COUNT(*) FROM frames").fetchone()[0]
    empty = conn.execute("SELECT COUNT(*) FROM frames WHERE object = ''").fetchone()[0]
    numeric = conn.execute(
        """SELECT COUNT(DISTINCT object) FROM frames
           WHERE object GLOB '[0-9][0-9][0-9][0-9]*'
           AND object NOT GLOB '*[A-Za-z]*' AND length(object) >= 4"""
    ).fetchone()[0]
    print(f"  {label}:")
    print(f"    total frames    {total:>8d}")
    print(f"    empty OBJECT    {empty:>8d}")
    print(f"    pure-numeric    {numeric:>8d}")


# ── CLI ─────────────────────────────────────────────────────────────────────

def main() -> int:
    parser = argparse.ArgumentParser(
        description="Fix malformed target names from legacy obslog pipeline",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument("command", nargs="?",
                        choices=["rescan", "normalize", "rebuild", "all", "summary"],
                        default="all")
    parser.add_argument("--db", default=None, help="SQLite DB path")
    parser.add_argument("--dry-run", action="store_true",
                        help="Show what would be done")
    parser.add_argument("--max-workers", type=int, default=8,
                        help="Parallel workers for FITS scan")
    args = parser.parse_args()

    db_path = args.db or _db_path()
    conn = db()
    cmd = args.command

    print("=" * 60)
    print("  MALFORMED TARGET NAME FIX")
    print("=" * 60)
    print_summary(conn, "Before")

    do_rescan = cmd in ("rescan", "all")
    do_normalize = cmd in ("normalize", "all")
    do_rebuild = cmd in ("rescan", "rebuild", "all")

    # Stage 1 — rescan (writes corrected CSVs from FITS headers)
    if do_rescan:
        print("\n--- Stage 1: Rescan dates with empty OBJECT ---")
        pairs = find_empty_object_dates(conn)
        if pairs:
            print(f"  Found {len(pairs)} dates with empty OBJECT frames")
            if args.dry_run:
                for inst, date in pairs[:10]:
                    print(f"    {inst}/{date}")
                if len(pairs) > 10:
                    print(f"    ... and {len(pairs)-10} more")
                print("  (remove --dry-run to rescan)")
            else:
                rescan_affected_dates(pairs, max_workers=args.max_workers)
        else:
            print("  No dates need rescanning.")
        conn.close()
        conn = db()

    # Stage 2 — rebuild (read corrected CSVs into DB)
    # Must run BEFORE normalize so DB-direct changes don't get wiped.
    if do_rebuild:
        if args.dry_run:
            print("\n--- Stage 2: Rebuild database (skipped in dry-run) ---")
        else:
            print("\n--- Stage 2: Rebuild database ---")
            elapsed = rebuild(db_path)
            print(f"  Rebuilt in {elapsed:.1f}s")
            conn.close()
            conn = db()

    # Stage 3 — normalize (apply name fixes directly in DB tables)
    # Must run AFTER rebuild so changes survive.
    if do_normalize:
        print("\n--- Stage 3: Apply name normalizations ---")
        all_fixes: dict[str, str] = {}
        all_fixes.update(CASE_NORMALIZE)
        all_fixes.update(KNOWN_PREFIX_FIXES)

        normalize_names(conn, all_fixes, dry_run=args.dry_run)

        if not args.dry_run:
            conn.commit()

    # Final summary
    print("\n" + "=" * 60)
    if args.dry_run:
        print_summary(conn, "Would be after")
    else:
        print_summary(conn, "After")
    print("=" * 60)

    conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
