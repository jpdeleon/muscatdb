import sqlite3
import re
import os
import sys
import time

sys.path.insert(0, os.path.abspath("src"))

from muscat_db.instruments import INSTRUMENTS
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
print(f"Found {len(sorted_pairs)} unique instrument-date pairs to rescan.")

total_start = time.time()
scanned = 0
failed = 0

for inst_name, obsdate in sorted_pairs:
    start = time.time()
    try:
        result = scan_date(inst_name, obsdate, max_workers=8)
        if result:
            elapsed = time.time() - start
            scanned += 1
            nfiles = result.get("total", 0)
            print(f"  OK  {inst_name} {obsdate}  ({nfiles} files, {elapsed:.1f}s)  [{scanned}/{len(sorted_pairs)}]")
        else:
            elapsed = time.time() - start
            scanned += 1
            print(f"  --  {inst_name} {obsdate}  (no files, {elapsed:.1f}s)  [{scanned}/{len(sorted_pairs)}]")
    except Exception as e:
        elapsed = time.time() - start
        failed += 1
        print(f"  FAIL {inst_name} {obsdate}  ({elapsed:.1f}s): {e}")

total_elapsed = time.time() - total_start
print(f"\nDone: {scanned} OK, {failed} failed in {total_elapsed:.1f}s")
