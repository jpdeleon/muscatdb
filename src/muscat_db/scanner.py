from __future__ import annotations

import csv
import os
import pathlib
import subprocess
from concurrent.futures import ProcessPoolExecutor, as_completed
from datetime import date, timedelta

from astropy.io import fits

from muscat_db.instruments import INSTRUMENTS, OBSLOG_BASE, InstrumentConfig


def _read_fits_header_keys(filepath: str, keys: list[str]) -> dict[str, str]:
    result: dict[str, str] = {}
    try:
        with fits.open(filepath, memmap=True) as hdul:
            header = hdul[0].header
            for key in keys:
                try:
                    val = header[key]
                    result[key] = str(val).strip() if val is not None else ""
                except (KeyError, ValueError):
                    result[key] = ""
    except Exception:
        try:
            keys_arg = " ".join(f"{k} 2" for k in keys)
            info = subprocess.run(
                ["fitsheader_list", "-frame", filepath, "-keys", keys_arg],
                capture_output=True, text=True, timeout=30,
            )
            if info.returncode == 0:
                words = info.stdout.strip().split()
                for i, key in enumerate(keys):
                    result[key] = words[i] if i < len(words) else ""
        except Exception:
            pass
    return result


def _process_single_file(filepath: str, inst: InstrumentConfig) -> dict[str, str] | None:
    fname = os.path.basename(filepath).removesuffix(".fits")
    kv = _read_fits_header_keys(filepath, inst.keys)
    mjd_key = "MJD-OBS" if inst.use_alt_ut_key else "MJD-STRT"
    ut_key = "UTSTART" if inst.use_alt_ut_key else "EXP-STRT"
    try:
        mjd = float(kv.get(mjd_key, "0"))
    except ValueError:
        mjd = 0.0
    jd = mjd - 49999.5
    ut_raw = kv.get(ut_key, "")
    ut_parts = ut_raw.split(":")
    ut = f"{ut_parts[0]}:{ut_parts[1]}:{int(float(ut_parts[2])):02d}" if len(ut_parts) >= 3 else ut_raw
    read_mode = kv.get("SPDTAB" if not inst.use_alt_ut_key else "CONFMODE", "")
    read_mode = "high" if read_mode == "1" else ("low" if read_mode == "0" else read_mode)
    airmass_key = inst.airmass_key
    try:
        focus_val = float(kv.get("FOC-VAL" if not inst.use_alt_ut_key else "FOCPOSN", "0"))
    except ValueError:
        focus_val = 0.0
    row = {
        "FRAME": fname,
        "OBJECT": kv.get("OBJECT", ""),
        "JD-STRT": f"{jd:.6f}",
        "UT-STRT": ut,
        "EXPTIME (s)": kv.get("EXPTIME", ""),
        "READ_MODE": read_mode,
        "FILTER": kv.get("FILTER", ""),
        "RA": kv.get("RA", ""),
        "DEC": kv.get("DEC", ""),
        airmass_key: kv.get(airmass_key, ""),
        inst.focus_label: f"{focus_val:.3f}" if focus_val else kv.get("FOC-VAL" if not inst.use_alt_ut_key else "FOCPOSN", ""),
    }
    if inst.has_pa:
        row["PA (deg)"] = kv.get("INST-PA", "")
    return row


def _find_fits_files(inst: InstrumentConfig, obsdate: str, ccd: int) -> list[str]:
    datadir = f"{inst.data_dir}/{obsdate}"
    if not os.path.isdir(datadir):
        return []
    if inst.ep_names:
        ep = inst.ep_names[ccd]
        pattern = f"{inst.prefix}{ep}*e91.fits"
    else:
        pattern = f"{inst.prefix}{ccd}*.fits"
    matches = sorted(pathlib.Path(datadir).glob(pattern))
    return [str(p) for p in matches]


def scan_date(
    inst_name: str,
    obsdate: str,
    max_workers: int | None = None,
    progress=None,
) -> dict:
    """Scan all CCDs for a date.

    Returns {"total": int, "per_ccd": {ccd: count}} — falsy if no files found.
    """
    inst = INSTRUMENTS[inst_name]
    logdir = f"{OBSLOG_BASE}/{inst_name}/{obsdate}"
    os.makedirs(logdir, exist_ok=True)

    file_ccd_pairs: list[tuple[str, int]] = []
    for ccd in range(inst.nccd):
        for fp in _find_fits_files(inst, obsdate, ccd):
            file_ccd_pairs.append((fp, ccd))

    if not file_ccd_pairs:
        return {}

    total = len(file_ccd_pairs)
    max_workers = max_workers or min(16, os.cpu_count() or 4)
    rows_by_ccd: dict[int, list[dict[str, str]]] = {}

    task_id = None
    ccd_label = f"CCD0-{inst.nccd - 1}"
    if progress is not None:
        task_id = progress.add_task(
            f"[cyan]{inst_name} {obsdate} {ccd_label}[/]", total=total, filename=""
        )

    with ProcessPoolExecutor(max_workers=max_workers) as executor:
        fut_map = {
            executor.submit(_process_single_file, fp, inst): (fp, ccd)
            for fp, ccd in file_ccd_pairs
        }
        for fut in as_completed(fut_map):
            fp, ccd = fut_map[fut]
            try:
                row = fut.result()
                if row:
                    rows_by_ccd.setdefault(ccd, []).append(row)
            except Exception:
                pass
            if progress is not None:
                progress.update(task_id, advance=1, filename=os.path.basename(fp))

    for ccd in sorted(rows_by_ccd):
        csv_path = f"{logdir}/obslog-{inst_name}-{obsdate}-ccd{ccd}.csv"
        fieldnames = inst.csv_header.split(",")
        with open(csv_path, "w", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=fieldnames)
            writer.writeheader()
            for row in sorted(rows_by_ccd[ccd], key=lambda r: r["FRAME"]):
                writer.writerow({k: row.get(k, "") for k in fieldnames})

    return {
        "total": total,
        "per_ccd": {ccd: len(rows) for ccd, rows in rows_by_ccd.items()},
    }


def scan_missing_dates(
    inst_name: str,
    year_prefix: str,
    max_workers: int | None = None,
    progress=None,
) -> list[str]:
    inst = INSTRUMENTS[inst_name]
    data_dir = inst.data_dir
    obslog_dir = f"{OBSLOG_BASE}/{inst_name}"
    existing = set()
    if os.path.isdir(obslog_dir):
        for d in os.listdir(obslog_dir):
            if os.path.isdir(f"{obslog_dir}/{d}") and d.startswith(year_prefix):
                existing.add(d)
    scanned: list[str] = []
    if not os.path.isdir(data_dir):
        return scanned
    missing = [
        d for d in sorted(os.listdir(data_dir))
        if os.path.isdir(f"{data_dir}/{d}") and d.startswith(year_prefix) and d not in existing
    ]
    if not missing:
        return scanned
    task_id = None
    if progress is not None:
        task_id = progress.add_task(
            f"[cyan]{inst_name} {year_prefix}xx[/]", total=len(missing), filename=""
        )
    for d in missing:
        scan_date(inst_name, d, max_workers=max_workers, progress=None)
        scanned.append(d)
        if progress is not None:
            progress.update(task_id, advance=1, filename=d)
    return scanned


def scan_all_instruments(year_prefix: str, max_workers: int | None = None) -> dict[str, list[str]]:
    from rich.progress import (
        BarColumn,
        Progress,
        TextColumn,
        TimeRemainingColumn,
    )
    result: dict[str, list[str]] = {}
    with Progress(
        TextColumn("[progress.description]{task.description}"),
        BarColumn(),
        TextColumn("[bold]{task.fields[filename]}"),
        TimeRemainingColumn(),
    ) as progress:
        for name in INSTRUMENTS:
            dates = scan_missing_dates(name, year_prefix, max_workers=max_workers, progress=progress)
            if dates:
                result[name] = dates
    return result


def scan_date_for_all_inst(obsdate: str, max_workers: int | None = None) -> list[str]:
    from rich.progress import (
        BarColumn,
        Progress,
        TextColumn,
        TimeRemainingColumn,
    )
    scanned: list[str] = []
    with Progress(
        TextColumn("[progress.description]{task.description}"),
        BarColumn(),
        TextColumn("[bold]{task.fields[filename]}"),
        TimeRemainingColumn(),
    ) as progress:
        for name in INSTRUMENTS:
            try:
                result = scan_date(name, obsdate, max_workers=max_workers, progress=progress)
                if result:
                    scanned.append(name)
            except Exception:
                pass
    return scanned


def scan_yesterday(max_workers: int | None = None) -> list[str]:
    yesterday = date.today() - timedelta(days=1)
    obsdate = yesterday.strftime("%y%m%d")
    return scan_date_for_all_inst(obsdate, max_workers=max_workers)
