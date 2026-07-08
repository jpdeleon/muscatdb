from __future__ import annotations

import csv
import logging
import os
import pathlib
from concurrent.futures import ProcessPoolExecutor
from datetime import date, timedelta

from muscat_db.instruments import INSTRUMENTS, OBSLOG_BASE, InstrumentConfig

logger = logging.getLogger(__name__)

# FITS header blocks are 2880 bytes; almost all real headers fit in <=8 blocks.
_FITS_HEADER_MAX_BYTES = 2880 * 16


def _normalize_numeric(val: str) -> str:
    """Round-trip a numeric FITS value to match astropy formatting.

    FITS cards without a decimal point or exponent are typed as integers by
    astropy; preserve that distinction so downstream comparisons (e.g.
    ``read_mode == "1"``) keep working.
    """
    try:
        f = float(val)
    except ValueError:
        return val
    if "." not in val and "e" not in val and "E" not in val:
        return str(int(f))
    return str(f)


def _parse_fits_cards(text: str, wanted: set[str]) -> tuple[dict[str, str], bool]:
    """Parse 80-char FITS header cards. Returns (values, end_found)."""
    result: dict[str, str] = {}
    end_found = False
    for i in range(0, len(text) - 79, 80):
        card = text[i:i + 80]
        key = card[:8].strip()
        if key == "END":
            end_found = True
            break
        if key not in wanted or card[8:10] != "= ":
            continue
        val_part = card[10:]
        if val_part.lstrip().startswith("'"):
            stripped = val_part.lstrip()
            end_quote = stripped.find("'", 1)
            val = stripped[1:end_quote] if end_quote > 0 else stripped[1:]
            result[key] = val.strip()
        else:
            slash = val_part.find("/")
            val = (val_part[:slash] if slash >= 0 else val_part).strip()
            result[key] = _normalize_numeric(val)
    return result, end_found


def _read_fits_header_raw(filepath: str, keys: list[str]) -> dict[str, str] | None:
    """Fast path: read FITS primary-HDU header cards directly from disk.

    Returns ``None`` to signal that astropy should be tried (well-formed primary
    HDU with no requested keys — typical of MEF files). Returns an empty-values
    dict for corrupt files (no ``END`` card within the first ~46 KB) so the
    caller skips the file instead of feeding it to astropy, which can hang on
    pathological headers.
    """
    try:
        with open(filepath, "rb") as f:
            data = f.read(_FITS_HEADER_MAX_BYTES)
    except OSError:
        return None
    if len(data) < 80 or not data.startswith(b"SIMPLE  ="):
        return None
    text = data.decode("ascii", errors="replace")
    values, end_found = _parse_fits_cards(text, set(keys))
    if values:
        # Got at least one requested key — use whatever we parsed.
        return {k: values.get(k, "") for k in keys}
    if end_found:
        # Well-formed primary HDU with none of our keys → MEF, try astropy.
        return None
    # No END card and no values → corrupt/truncated header. Return empties so
    # the caller skips it; astropy is liable to hang on these.
    return {k: "" for k in keys}


def _read_fits_header_astropy(filepath: str, keys: list[str]) -> dict[str, str]:
    """Fallback: full astropy parse, including MEF extension scan."""
    from astropy.io import fits  # imported lazily so workers don't pay for it
    result: dict[str, str] = {k: "" for k in keys}
    try:
        with fits.open(filepath, memmap=False) as hdul:
            for hdu in hdul:
                header = hdu.header
                for key in keys:
                    if not result[key]:
                        try:
                            val = header[key]
                            if val is not None and str(val).strip():
                                result[key] = str(val).strip()
                        except (KeyError, ValueError):
                            pass
                if all(result.values()):
                    break
    except Exception:
        logger.debug("astropy fallback failed reading FITS header %s", filepath, exc_info=True)
    return result


def _read_fits_header_keys(filepath: str, keys: list[str]) -> dict[str, str]:
    raw = _read_fits_header_raw(filepath, keys)
    if raw is not None:
        return raw
    # Only MEF-like files (well-formed primary HDU, no requested keys) reach
    # this path; corrupt files are skipped by the raw parser above.
    return _read_fits_header_astropy(filepath, keys)


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
    try:
        matches = sorted(pathlib.Path(datadir).glob(pattern))
    except (PermissionError, OSError):
        return []
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
    try:
        os.makedirs(logdir, exist_ok=True)
    except (PermissionError, OSError) as e:
        print(f"[warn] cannot create {logdir}: {e}")
        return {}

    file_ccd_pairs: list[tuple[str, int]] = []
    for ccd in range(inst.nccd):
        for fp in _find_fits_files(inst, obsdate, ccd):
            file_ccd_pairs.append((fp, ccd))

    if not file_ccd_pairs:
        return {}

    total = len(file_ccd_pairs)
    max_workers = max_workers or (os.cpu_count() or 4)
    rows_by_ccd: dict[int, list[dict[str, str]]] = {}

    task_id = None
    ccd_label = f"CCD0-{inst.nccd - 1}"
    if progress is not None:
        task_id = progress.add_task(
            f"[cyan]{inst_name} {obsdate} {ccd_label}[/]", total=total, filename=""
        )

    # CPU-bound header parsing dominates per-file cost, so processes scale where
    # threads can't (the GIL serialises the parse loop). Chunked map keeps
    # dispatch overhead low.
    paths = [fp for fp, _ in file_ccd_pairs]
    ccds  = [ccd for _, ccd in file_ccd_pairs]
    chunksize = max(1, total // (max_workers * 4))
    with ProcessPoolExecutor(max_workers=max_workers) as executor:
        for (fp, ccd, row) in zip(
            paths, ccds,
            executor.map(_process_single_file, paths, [inst] * total, chunksize=chunksize),
        ):
            if row:
                rows_by_ccd.setdefault(ccd, []).append(row)
            if progress is not None:
                progress.update(task_id, advance=1, filename=os.path.basename(fp))

    for ccd in sorted(rows_by_ccd):
        csv_path = f"{logdir}/obslog-{inst_name}-{obsdate}-ccd{ccd}.csv"
        fieldnames = inst.csv_header.split(",")
        try:
            with open(csv_path, "w", newline="") as f:
                writer = csv.DictWriter(f, fieldnames=fieldnames)
                writer.writeheader()
                for row in sorted(rows_by_ccd[ccd], key=lambda r: r["FRAME"]):
                    writer.writerow({k: row.get(k, "") for k in fieldnames})
        except (PermissionError, OSError) as e:
            print(f"[warn] cannot write {csv_path}: {e}")

    return {
        "total": total,
        "per_ccd": {ccd: len(rows) for ccd, rows in rows_by_ccd.items()},
    }


def scan_missing_dates(
    inst_name: str,
    year_prefix: str,
    max_workers: int | None = None,
    progress=None,
    force: bool = False,
) -> list[str]:
    """Scan dates for an instrument.

    ``year_prefix`` filters date directories by leading characters (e.g. ``"25"``).
    Pass ``"all"`` (case-insensitive) to scan every date directory under the
    instrument's data dir.

    By default, only dates without an existing obslog CSV are scanned.
    With ``force=True``, every date with FITS data is rescanned, overwriting
    any existing CSVs — useful for fixing legacy malformed obslogs.
    """
    prefix = "" if year_prefix.lower() == "all" else year_prefix
    inst = INSTRUMENTS[inst_name]
    data_dir = inst.data_dir
    obslog_dir = f"{OBSLOG_BASE}/{inst_name}"
    existing = set()
    if not force and os.path.isdir(obslog_dir):
        try:
            entries = os.listdir(obslog_dir)
        except (PermissionError, OSError) as e:
            print(f"[warn] cannot list {obslog_dir}: {e}")
            entries = []
        for d in entries:
            if os.path.isdir(f"{obslog_dir}/{d}") and d.startswith(prefix):
                existing.add(d)
    scanned: list[str] = []
    if not os.path.isdir(data_dir):
        return scanned
    try:
        data_entries = sorted(os.listdir(data_dir))
    except (PermissionError, OSError) as e:
        print(f"[warn] cannot list {data_dir}: {e}")
        return scanned
    missing = [
        d for d in data_entries
        if os.path.isdir(f"{data_dir}/{d}") and d.startswith(prefix) and d not in existing
    ]
    if not missing:
        return scanned
    task_id = None
    if progress is not None:
        label = "all" if prefix == "" else f"{prefix}xx"
        task_id = progress.add_task(
            f"[cyan]{inst_name} {label}[/]", total=len(missing), filename=""
        )
    for d in missing:
        try:
            scan_date(inst_name, d, max_workers=max_workers, progress=None)
            scanned.append(d)
        except (PermissionError, OSError) as e:
            print(f"[warn] skipping {inst_name} {d}: {e}")
        except Exception as e:
            print(f"[warn] {inst_name} {d} failed: {type(e).__name__}: {e}")
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
                logger.debug("scan_date failed for %s %s", name, obsdate, exc_info=True)
    return scanned


def scan_yesterday(max_workers: int | None = None) -> list[str]:
    yesterday = date.today() - timedelta(days=1)
    obsdate = yesterday.strftime("%y%m%d")
    return scan_date_for_all_inst(obsdate, max_workers=max_workers)
