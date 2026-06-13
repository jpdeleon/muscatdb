import sqlite3
import re
import os
import sys
import time
from concurrent.futures import ProcessPoolExecutor, as_completed

sys.path.insert(0, os.path.abspath("src"))

from muscat_db.scanner import scan_date

db_path = "muscat.db"
conn = sqlite3.connect(db_path)

truncated_pattern = re.compile(r"^(WASP-?|TOI-?|HD-?|HAT-P-?|K2-?|KOI-?|TESS-?|TIC-?)$", re.IGNORECASE)
valid_chars = re.compile(r"^[0-9:+\-.\s]*$")

affected_pairs = set()

print("Finding truncated target names...")
cur = conn.execute("SELECT DISTINCT instrument, obsdate, object FROM summaries")
for inst, obsdate, obj in cur.fetchall():
    if not obj:
        continue
    obj_clean = obj.strip()
    if truncated_pattern.match(obj_clean) or obj_clean.endswith("-"):
        affected_pairs.add((inst, obsdate))

print("Finding invalid/shifted coordinate formats...")
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
total = len(sorted_pairs)
print(f"Found {total} unique instrument-date pairs to rescan.")

def process_one(pair):
    inst_name, obsdate = pair
    start = time.time()
    try:
        result = scan_date(inst_name, obsdate, max_workers=8)
        elapsed = time.time() - start
        if result:
            return (True, inst_name, obsdate, result.get("total", 0), elapsed, None)
        else:
            return (True, inst_name, obsdate, 0, elapsed, None)
    except Exception as e:
        elapsed = time.time() - start
        return (False, inst_name, obsdate, 0, elapsed, str(e))

total_start = time.time()
scanned = 0
failed = 0
done = 0

with ProcessPoolExecutor(max_workers=4) as pool:
    fut_map = {pool.submit(process_one, p): p for p in sorted_pairs}
    for fut in as_completed(fut_map):
        ok, inst_name, obsdate, nfiles, elapsed, err = fut.result()
        done += 1
        if ok:
            scanned += 1
            print(f"  OK  {inst_name} {obsdate}  ({nfiles} files, {elapsed:.1f}s)  [{done}/{total}]")
        else:
            failed += 1
            print(f"  FAIL {inst_name} {obsdate}  ({elapsed:.1f}s): {err}  [{done}/{total}]")

total_elapsed = time.time() - total_start
avg = total_elapsed / total if total else 0
print(f"\nDone: {scanned} OK, {failed} failed in {total_elapsed:.1f}s ({total_elapsed/60:.1f} min, {avg:.1f}s avg)")
