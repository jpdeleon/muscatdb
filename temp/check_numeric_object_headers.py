#!/usr/bin/env python3
"""Read real FITS OBJECT cards for pure-numeric target names to check for spaces."""
import sqlite3
import sys
from pathlib import Path

SRC = Path(__file__).resolve().parent.parent / "src"
sys.path.insert(0, str(SRC))
from muscat_db.instruments import INSTRUMENTS
from muscat_db.database import db_path
from astropy.io import fits

NUMERIC = ["98943", "18916", "3104", "65803", "145627", "88264", "52768"]

conn = sqlite3.connect(db_path())
conn.row_factory = sqlite3.Row

for obj in NUMERIC:
    row = conn.execute(
        """SELECT instrument, obsdate, filename FROM frames
           WHERE object = ? AND obsdate NOT LIKE 'csv_old%' LIMIT 1""",
        (obj,),
    ).fetchone()
    if not row:
        print(f"{obj!r}: no frame row found")
        continue
    inst = INSTRUMENTS[row["instrument"]]
    fp = f"{inst.data_dir}/{row['obsdate']}/{row['filename']}"
    if not Path(fp).exists() and Path(fp + ".fits").exists():
        fp = fp + ".fits"
    if not Path(fp).exists():
        print(f"{obj!r}: file missing {fp}")
        continue
    try:
        with fits.open(fp, memmap=False) as hdul:
            hdr_obj = None
            for hdu in hdul:
                if "OBJECT" in hdu.header:
                    hdr_obj = hdu.header["OBJECT"]
                    break
        print(f"db={obj!r:12} header OBJECT={hdr_obj!r:30} "
              f"has_space={' ' in str(hdr_obj)}  ({row['instrument']}/{row['obsdate']})")
    except Exception as e:
        print(f"{obj!r}: read error {e}")

conn.close()
