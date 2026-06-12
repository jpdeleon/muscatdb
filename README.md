# muscat-db

MuSCAT observation log pipeline — scan FITS files, build a fast SQLite database, and browse results in a web UI.

Converted from the original Perl scripts (`mkobslog*.pl`, `auto_mkobslog.pl`, `show_obslog_summary.pl`) into a single Python project with a modern backend + frontend.

## TODO
1. In GUI photometry page, add question mark icon to show useful help or tips when mouse hover.
2. For muscat and muscat2 inst, show also the master_*.png in GUI photometry page.
3. Add progress bar in calibrate_muscat*.py
4. In GUI photometry page, add a "use defaults" button pipeline options section.
5. In muscat-db table in the home page, add new table column called Phot placed after Dates column which should indicate a check or X mark if full photometry outputs exists or no output exists (or only ran using test-run).
6. The muscat-db and Logs page are identical. Separate them into two different page. The muscat-db table should only be in the MuSCAT-db homepage. 
Move the link for the five Instruments i.e. muscat, muscat2, muscat4, muscat4, and sinistro to the Logs Page.
7. In Logs page, add a summary of data for each instruments below the Instruments section.
8. Add a new boilerplate page called "Transit Fit" for that will host transit fitting code in the future. Add a link in the navbar after "Photometry".
9. Add a new page called Jobs. Add a table that tallies the job queue with deep links and their status e.g. Done, Failed, Pending, etc. Add a link in the navbar after "Transit Fit".
10. Fix the status bar in Photometry page. It sometimes show up and sometimes disappears when navigating to different pages.

## Requirements

- Python ≥ 3.12
- FITS files on the standard data directories (`/data/MuSCAT*`, `/data/Sinistro`)
- Obslog output directories (`/ut3/muscat/obslog/`)

## Install

```bash
pip install -e .
```

## CLI Usage

### Scan a single date

```bash
muscat-db scan muscat3 260423
```

### Scan all dates in a year that don't have CSVs yet

```bash
muscat-db scan-missing muscat4 26
muscat-db scan-all 26          # all instruments
```

### Scan yesterday (cron target)

```bash
muscat-db scan-yesterday
```

### Print a summary (like the original `show_obslog_summary.pl`)

```bash
muscat-db summary muscat3 260423 0
```

### Build the SQLite database from all CSVs

```bash
muscat-db build-db
```

### Start the web frontend

```bash
muscat-db serve              # http://0.0.0.0:8000
muscat-db serve --port 8080  # custom port
```

## Web Frontend

Navigation: **Logs** → **Dates** → **CCD summaries** → **Per-frame table**

The home page shows:

- **Instrument cards** for every configured instrument (cards mark which ones already have data ingested).
- **Targets table** aggregated from all frames, with one row per OBJECT and columns:
  `Target · RA · Dec · Filters · Airmass · # Frames · Instruments · Dates`.
- **Search bar** that filters the targets table in real time. Plain-text substring by default, with optional regex and case-sensitive toggles. Invalid regex is reported inline.
- **Light / dark theme toggle** in the navbar (sun/moon icon, persisted via `localStorage`).
- **Loading status bar** at the top of the page plus a bottom status line showing `Rendering N targets…` while the table lays out.
- Inline SVG **favicon** (no extra HTTP request).

Calibration and engineering frames (`DARK*`, `FLAT*`, `BIAS*`, `MOVIE`, `FOCUS_ADJUST`, `FoV`, `Muscat commissioning *`, etc.) are excluded from the targets aggregation so the table only shows real science targets.

All pages still render with zero external dependencies — fonts, icons, theme, and search are inlined.

## Cron (daily)

```cron
0 6 * * * cd /path/to/muscat-db && .venv/bin/muscat-db scan-yesterday && .venv/bin/muscat-db build-db
```

## Architecture

```
CLI (typer)
├── scan         → scanner.py    → reads FITS headers (astropy), writes CSV
├── summary      → summarizer.py → groups frames by OBJECT/EXPTIME/READ_MODE
├── build-db     → database.py   → walks CSVs → SQLite (frames + summaries)
└── serve        → web.py        → FastAPI + Jinja2 → browser
```

### Instrument support

| Instrument | CCDs | FITS prefix | Data dir |
|---|---|---|---|
| muscat  | 3 | `MSCT`   | `/data/MuSCAT`   |
| muscat2 | 4 | `MCT2`   | `/data/MuSCAT2`  |
| muscat3 | 4 | `ogg2m001-` | `/data/MuSCAT3` |
| muscat4 | 4 | `coj2m002-` | `/data/MuSCAT4` |
| sinistro | 1 | `*` (any LCO 1m site) | `/data/Sinistro`  |

Sinistro scans the reduced `*e91.fits` frames produced by LCO BANZAI, regardless of site prefix (`elp1m008-`, `coj1m003-`, `cpt1m013-`, …).

### Migration from Perl

| Perl script | Python equivalent |
|---|---|
| `mkobslog.pl muscat 240101` | `muscat-db scan muscat 240101` |
| `mkobslog_muscat3.pl 240101` | `muscat-db scan muscat3 240101` |
| `auto_mkobslog.pl muscat3 24` | `muscat-db scan-missing muscat3 24` |
| `show_obslog_summary.pl muscat3 240101 0` | `muscat-db summary muscat3 240101 0` |
| `auto_mkobslog_muscat2.sh` (cron) | `muscat-db scan-yesterday` |
