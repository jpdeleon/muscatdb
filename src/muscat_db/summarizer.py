from __future__ import annotations

import csv
import os
from dataclasses import dataclass

from muscat_db.instruments import INSTRUMENTS, OBSLOG_BASE


def _ep_name_for(inst_name: str, ccd: int) -> str:
    if inst_name == "muscat3":
        return ["ep02", "ep03", "ep04", "ep05"][ccd]
    if inst_name == "muscat4":
        return ["ep06", "ep07", "ep08", "ep09"][ccd]
    return ""


def _delim_for(inst_name: str, ccd: int, obsdate: str) -> str:
    inst = INSTRUMENTS[inst_name]
    if inst_name == "muscat":
        return f"MSCT{ccd}_{obsdate}"
    if inst_name == "muscat2":
        return f"MCT2{ccd}_{obsdate}"
    if inst_name in ("muscat3", "muscat4"):
        ep = _ep_name_for(inst_name, ccd)
        return f"{inst.prefix}{ep}-20{obsdate}-"
    return ""


@dataclass
class SummaryRow:
    object: str
    exptime: str
    read_mode: str
    frame_start: str
    frame_end: str
    ut_start: str
    ut_end: str
    nframes: int


def summarize_csv(inst_name: str, obsdate: str, ccd: int) -> list[SummaryRow]:
    csv_path = f"{OBSLOG_BASE}/{inst_name}/{obsdate}/obslog-{inst_name}-{obsdate}-ccd{ccd}.csv"
    if not os.path.isfile(csv_path):
        return []
    delim = _delim_for(inst_name, ccd, obsdate)
    with open(csv_path) as f:
        reader = csv.DictReader(f)
        colnames = reader.fieldnames or []
        keys_in_order: list[str] = []
        seen_keys: dict[str, int] = {}
        key_fnum_prev: dict[str, int] = {}
        key_id: dict[str, int] = {}
        key_data: dict[str, SummaryRow] = {}
        n: dict[str, int] = {}
        fnum_start: dict[str, str] = {}
        ut_start: dict[str, str] = {}
        ut_end: dict[str, str] = {}
        for row in reader:
            obj = row.get("OBJECT", "")
            exptime = row.get("EXPTIME (s)", "")
            read_mode = row.get("READ_MODE", "")
            key1 = f"{obj}-{exptime}-{read_mode}"
            frame = row.get("FRAME", "")
            fnum = ""
            if delim:
                parts = frame.split(delim, 1)
                if len(parts) > 1:
                    fnum = parts[1]
                    if inst_name in ("muscat3", "muscat4"):
                        fnum = fnum.split("-e91", 1)[0] if "-e91" in fnum else fnum
            else:
                fnum = frame
            if key1 not in seen_keys:
                seen_keys[key1] = 1
                key_fnum_prev[key1] = 0
            try:
                fnum_int = int(fnum) if fnum else 0
            except ValueError:
                fnum_int = 0
            if fnum_int > key_fnum_prev[key1] + 1:
                key_id[key1] = key_id.get(key1, 0) + 1
            key_fnum_prev[key1] = fnum_int
            key2 = f"{key1}-{key_id.get(key1, 0)}"
            if key2 not in key_data:
                key_data[key2] = SummaryRow(
                    object=obj,
                    exptime=exptime,
                    read_mode=read_mode,
                    frame_start=fnum,
                    frame_end=fnum,
                    ut_start=row.get("UT-STRT", ""),
                    ut_end=row.get("UT-STRT", ""),
                    nframes=0,
                )
                keys_in_order.append(key2)
                fnum_start[key2] = fnum
                ut_start[key2] = row.get("UT-STRT", "")
            key_data[key2].frame_end = fnum
            key_data[key2].ut_end = row.get("UT-STRT", "")
            key_data[key2].nframes += 1
    return [key_data[k] for k in keys_in_order]


def print_summary(inst_name: str, obsdate: str, ccd: int) -> None:
    rows = summarize_csv(inst_name, obsdate, ccd)
    if not rows:
        print(f"No obslog: {OBSLOG_BASE}/{inst_name}/{obsdate}/obslog-{inst_name}-{obsdate}-ccd{ccd}.csv")
        return
    print("# OBJECT EXPTIME(s) READ_MODE FRAME#1 FRAME#2 UT-STRT1 UT-STRT2 NFRAMES")
    for r in rows:
        print(f"{r.object:<14} {r.exptime:>3} {r.read_mode:>4} {r.frame_start:>7} {r.frame_end:>7} {r.ut_start:>8} {r.ut_end:>8} {r.nframes:>4}")
