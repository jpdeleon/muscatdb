import sqlite3
import re
import os
import sys
from concurrent.futures import ProcessPoolExecutor
import csv
import time

sys.path.insert(0, os.path.abspath("src"))

from muscat_db.instruments import INSTRUMENTS
from muscat_db.scanner import _process_single_file
from muscat_db.database import build_db

db_path = "muscat.db"
conn = sqlite3.connect(db_path)

truncated_pattern = re.compile(r"^(WASP-?|TOI-?|HD-?|HAT-P-?|K2-?|KOI-?|TESS-?|TIC-?)$", re.IGNORECASE)
valid_chars = re.compile(r"^[0-9:+\-.\s]*$")

affected_pairs = set()

print("[1/5] Finding truncated target names...")
cur = conn.execute("SELECT DISTINCT instrument, obsdate, object FROM summaries")
for inst, obsdate, obj in cur.fetchall():
    if not obj:
        continue
    obj_clean = obj.strip()
    if truncated_pattern.match(obj_clean) or obj_clean.endswith("-"):
        affected_pairs.add((inst, obsdate))

print("[2/5] Finding invalid/shifted coordinate formats...")
cur = conn.execute("SELECT object, ra, declination, inst_dates FROM targets")
for obj, ra, dec, inst_dates in cur.fetchall():
    ra_clean = ra.strip() if ra else ""
    dec_clean = dec.strip() if dec else ""
    is_ra_invalid = ra_clean and (not valid_chars.match(ra_clean) or ":" not in ra_clean)
    is_dec_invalid = dec_clean and (not valid_chars.match(dec_clean) or ":" not in dec_clean)
    if is_ra_invalid or is_dec_invalid:
        if inst_dates:
            for pair in inst_dates.split(","):
                if ":" in pair:
                    inst, date = pair.split(":", 1)
                    affected_pairs.add((inst, date))

conn.close()

sorted_pairs = sorted(affected_pairs)
print(f"  Found {len(sorted_pairs)} unique instrument-date pairs that require rescanning.")

print("[3/5] Gathering FITS file paths (optimized)...")
tasks = []
for inst_name, obsdate in sorted_pairs:
    inst = INSTRUMENTS[inst_name]
    datadir = f"{inst.data_dir}/{obsdate}"
    if not os.path.isdir(datadir):
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

print(f"  Total FITS files to process: {len(tasks)}")

if not tasks:
    print("No FITS files found to process.")
    sys.exit(0)

print("[4/5] Processing FITS headers in parallel (8 workers)...")
max_workers = 8
chunksize = max(1, len(tasks) // (max_workers * 4))

paths = [t[0] for t in tasks]
configs = [t[1] for t in tasks]

rows_by_group = {}
last_report = 0
report_interval = max(1, len(tasks) // 40)
start = time.time()

with ProcessPoolExecutor(max_workers=max_workers) as executor:
    for idx, (t, row) in enumerate(zip(
        tasks,
        executor.map(_process_single_file, paths, configs, chunksize=chunksize)
    )):
        if row:
            fp, inst, ccd, obsdate, inst_name = t
            rows_by_group.setdefault((inst_name, obsdate, ccd), []).append(row)
        if idx >= last_report + report_interval:
            pct = (idx + 1) / len(tasks) * 100
            elapsed = time.time() - start
            rate = (idx + 1) / elapsed if elapsed > 0 else 0
            eta = (len(tasks) - idx - 1) / rate if rate > 0 else 0
            print(f"  {pct:.0f}% ({idx+1}/{len(tasks)})  {rate:.0f} f/s  ETA:{eta:.0f}s")
            last_report = idx

elapsed = time.time() - start
print(f"  100% ({len(tasks)}/{len(tasks)}) in {elapsed:.0f}s — {len(rows_by_group)} groups")

print("[5/5] Writing updated CSV log files...")
written_csvs = 0
total_rows = 0
for (inst_name, obsdate, ccd), rows in rows_by_group.items():
    inst = INSTRUMENTS[inst_name]
    logdir = f"/ut3/muscat/obslog/{inst_name}/{obsdate}"
    os.makedirs(logdir, exist_ok=True)
    csv_path = f"{logdir}/obslog-{inst_name}-{obsdate}-ccd{ccd}.csv"
    fieldnames = inst.csv_header.split(",")
    try:
        with open(csv_path, "w", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=fieldnames)
            writer.writeheader()
            for row in sorted(rows, key=lambda r: r["FRAME"]):
                writer.writerow({k: row.get(k, "") for k in fieldnames})
        written_csvs += 1
        total_rows += len(rows)
    except Exception as e:
        print(f"  [warn] cannot write {csv_path}: {e}")

print(f"  Wrote {written_csvs} CSV files ({total_rows} total rows)")

print("\nRebuilding the database from the updated CSV files...")
try:
    total_frames = build_db(db_path)
    print(f"  Successfully rebuilt database: {total_frames} frames indexed in {db_path}.")
except Exception as e:
    print(f"  Error rebuilding database: {e}")
