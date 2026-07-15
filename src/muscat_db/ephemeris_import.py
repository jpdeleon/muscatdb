"""Parse external transit-center CSV files for the ephemeris page."""

from __future__ import annotations

import csv
import io
import math
import re
from collections import Counter


MAX_CSV_BYTES = 1_000_000
MAX_CSV_ROWS = 5_000


class EphemerisCSVError(ValueError):
    """Raised when a CSV cannot be interpreted as transit-center data."""


_ALIASES = {
    "planet": {"planet", "pl", "planetletter"},
    "epoch": {"epoch", "e", "transitepoch"},
    "tc": {"tc", "bjd", "transitcenter", "transitcentertime", "midtransittime"},
    "tc_unc": {
        "tcunc",
        "unc",
        "uncertainty",
        "tcuncertainty",
        "transitcenteruncertainty",
        "sigmatc",
    },
}


def _header_key(value: str) -> str:
    # Units belong to the field metadata, not its identity: tc(BJD), tc [BJD]
    # and tc_bjd should all be recognized as transit-center columns.
    value = value.lstrip("\ufeff").strip().casefold()
    value = re.sub(r"\([^)]*\)|\[[^]]*\]", "", value)
    compact = re.sub(r"[^a-z0-9]+", "", value)
    for unit in ("bjdtbd", "bjdtdb", "bjdutc", "bjd", "jd", "days", "day", "d"):
        if compact.endswith(unit) and compact != unit:
            compact = compact[: -len(unit)]
            break
    return compact


def _map_headers(fieldnames: list[str]) -> tuple[dict[str, str], dict[str, str]]:
    mapped: dict[str, str] = {}
    original: dict[str, str] = {}
    for raw in fieldnames:
        key = _header_key(raw)
        for canonical, aliases in _ALIASES.items():
            if key in aliases and canonical not in mapped:
                mapped[canonical] = raw
                original[canonical] = raw.strip()
                break
    missing = [name for name in ("planet", "tc", "tc_unc") if name not in mapped]
    if missing:
        raise EphemerisCSVError(
            "Missing required column(s): " + ", ".join(missing)
            + ". Expected planet, tc and tc_unc; epoch is optional."
        )
    return mapped, original


def _time_system(text: str, headers: list[str]) -> dict[str, str | bool]:
    haystack = " ".join(headers + [text[:2000]]).casefold().replace("-", "_")
    if "bjd_tdb" in haystack or "bjdtdb" in re.sub(r"[^a-z0-9]", "", haystack):
        return {"value": "BJD_TDB", "confirmed": True, "supported": True}
    if "bjd_utc" in haystack or "bjdutc" in re.sub(r"[^a-z0-9]", "", haystack):
        return {"value": "BJD_UTC", "confirmed": True, "supported": False}
    if "bjd" in haystack:
        return {"value": "BJD (time scale unspecified)", "confirmed": False, "supported": True}
    return {"value": "Unspecified", "confirmed": False, "supported": True}


def parse_transit_csv(text: str) -> dict:
    """Return normalized rows, row errors, header mapping and time metadata."""
    if not isinstance(text, str) or not text.strip():
        raise EphemerisCSVError("The selected CSV is empty.")
    if len(text.encode("utf-8")) > MAX_CSV_BYTES:
        raise EphemerisCSVError("CSV is larger than the 1 MB import limit.")

    # O-C exports contain descriptive comment lines. Keep physical line
    # numbers so validation messages still point to what the user sees.
    retained = [
        (line_no, line)
        for line_no, line in enumerate(text.splitlines(), start=1)
        if line.strip() and not line.lstrip().startswith("#")
    ]
    if not retained:
        raise EphemerisCSVError("The CSV contains no header or data rows.")

    sample = "\n".join(line for _, line in retained[:20])
    try:
        dialect = csv.Sniffer().sniff(sample, delimiters=",\t;")
    except csv.Error:
        dialect = csv.excel

    reader = csv.DictReader(io.StringIO("\n".join(line for _, line in retained)), dialect=dialect)
    if not reader.fieldnames:
        raise EphemerisCSVError("The CSV header could not be read.")
    mapped, original = _map_headers(reader.fieldnames)

    rows: list[dict] = []
    errors: list[dict] = []
    seen: Counter[tuple[str, float]] = Counter()
    data_lines = retained[1:]
    for index, raw in enumerate(reader):
        if index >= MAX_CSV_ROWS:
            raise EphemerisCSVError(f"CSV exceeds the {MAX_CSV_ROWS:,}-row import limit.")
        line_no = data_lines[index][0] if index < len(data_lines) else index + 2
        row_errors: list[str] = []
        planet = str(raw.get(mapped["planet"], "") or "").strip().casefold()
        if not planet:
            row_errors.append("planet is blank")

        try:
            tc = float(str(raw.get(mapped["tc"], "") or "").strip())
            if not math.isfinite(tc):
                raise ValueError
        except ValueError:
            tc = None
            row_errors.append("tc is not a finite number")

        try:
            tc_unc = float(str(raw.get(mapped["tc_unc"], "") or "").strip())
            if not math.isfinite(tc_unc) or tc_unc <= 0:
                raise ValueError
        except ValueError:
            tc_unc = None
            row_errors.append("tc_unc must be a positive finite number")

        source_epoch = None
        if "epoch" in mapped:
            epoch_text = str(raw.get(mapped["epoch"], "") or "").strip()
            try:
                epoch_number = float(epoch_text)
                if not math.isfinite(epoch_number) or not epoch_number.is_integer():
                    raise ValueError
                source_epoch = int(epoch_number)
            except ValueError:
                row_errors.append("epoch must be an integer")

        if row_errors:
            errors.append({"line": line_no, "errors": row_errors})
            continue
        assert tc is not None and tc_unc is not None
        seen[(planet, tc)] += 1
        rows.append(
            {
                "line": line_no,
                "planet": planet,
                "source_epoch": source_epoch,
                "tc": tc,
                "tc_unc": tc_unc,
            }
        )

    duplicates = sum(count - 1 for count in seen.values() if count > 1)
    warnings = []
    if duplicates:
        warnings.append(f"{duplicates} duplicate planet/transit-center row(s) detected.")
    time_system = _time_system(text, reader.fieldnames)
    if not time_system["confirmed"]:
        warnings.append(
            "The CSV does not identify its time scale as BJD_TDB; confirmation is required before import."
        )
    elif not time_system["supported"]:
        warnings.append(
            f"{time_system['value']} cannot be imported as BJD_TDB without an explicit time-scale conversion."
        )
    return {
        "rows": rows,
        "errors": errors,
        "warnings": warnings,
        "columns": original,
        "time_system": time_system,
        "delimiter": "tab" if dialect.delimiter == "\t" else dialect.delimiter,
    }
