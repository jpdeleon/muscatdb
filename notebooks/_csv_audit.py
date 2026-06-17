"""Read-only audit: does the homepage CSV export summarize all muscatdb data?

Compares three things:
  1. rows the CSV endpoint would emit  == get_targets() length
  2. rows materialized in the `targets` table
  3. distinct science objects in `frames` using the SAME exclusion filter
     that _populate_targets() applied at build time (detects stale build).
"""
import sqlite3
import sys

sys.path.insert(0, "../src")
from muscat_db.database import get_targets, _populate_targets, _TARGET_EXCLUDE_EXACT  # noqa
import muscat_db.database as dbmod  # noqa

DB = "/raid_ut2/home/jerome/github/research/project/muscat-db/muscat.db"

# 1. What the CSV/homepage actually serve
targets = get_targets(DB)
print(f"get_targets() rows (== CSV data rows == homepage rows): {len(targets)}")

conn = sqlite3.connect(DB)

# 2. Materialized targets table (read object set + counts up front, from the
#    real table, BEFORE any temp table can shadow the name `targets`).
n_tbl = conn.execute("SELECT COUNT(*) FROM targets").fetchone()[0]
print(f"targets table rows: {n_tbl}")
tbl_objs = {r[0] for r in conn.execute("SELECT object FROM targets")}
tbl_nf = dict(conn.execute("SELECT object, n_frames FROM targets"))

# 3. Re-aggregate frames live with the build-time filter in a SEPARATE in-memory
#    DB and compare object sets + per-target frame counts.
live = sqlite3.connect(":memory:")
live.executescript(dbmod.SCHEMA)
src = sqlite3.connect(DB)

# copy frames into memory db and run the real _populate_targets
live.execute("DROP TABLE IF EXISTS frames")
cols = [r[1] for r in src.execute("PRAGMA table_info(frames)").fetchall()]
live.execute(f"CREATE TABLE frames ({', '.join(c + ' TEXT' for c in cols)})")
rows = src.execute("SELECT * FROM frames").fetchall()
live.executemany(
    f"INSERT INTO frames VALUES ({','.join('?' * len(cols))})", rows
)
live.execute("DELETE FROM targets")
_populate_targets(live)
live_objs = {r[0] for r in live.execute("SELECT object FROM targets")}

print(f"live re-aggregated objects: {len(live_objs)}")
print(f"objects in table but NOT in live rebuild (stale extras): {len(tbl_objs - live_objs)}")
print(f"objects in live rebuild but NOT in table (MISSING from CSV): {len(live_objs - tbl_objs)}")
missing = sorted(live_objs - tbl_objs)[:20]
if missing:
    print("  examples missing:", missing)

# per-target n_frames mismatch
live_nf = dict(live.execute("SELECT object, n_frames FROM targets"))
mismatch = [o for o in tbl_objs & live_objs if tbl_nf[o] != live_nf.get(o)]
print(f"objects with n_frames mismatch (stale counts): {len(mismatch)}")
if mismatch[:10]:
    print("  examples:", [(o, tbl_nf[o], live_nf[o]) for o in mismatch[:10]])

# total science frames covered vs total non-null frames in DB
total_frames = conn.execute("SELECT COUNT(*) FROM frames").fetchone()[0]
science_frames = sum(t["n_frames"] for t in targets)
print(f"total frames in DB: {total_frames}")
print(f"science frames covered by targets/CSV: {science_frames} "
      f"({100*science_frames/total_frames:.1f}% — rest are calib/junk by design)")
