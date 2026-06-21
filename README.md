# muscat-db

MuSCAT observation log pipeline — scan FITS files, build a fast SQLite database, and browse results in a web UI.

Converted from the original Perl scripts (`mkobslog*.pl`, `auto_mkobslog.pl`, `show_obslog_summary.pl`) into a single Python project with a modern backend + frontend.

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

The navigation bar links the observation log, photometry, transit fitting, job
history, exposure calculator, and workflow diagram. Observation-log navigation
is **Logs** → **Dates** → **CCD summaries** → **Per-frame table**.

The home page shows:

- **Instrument cards** for every configured instrument (cards mark which ones already have data ingested).
- **Targets table** aggregated from all frames, with one row per OBJECT and columns:
  `Target · RA · Dec · Filters · Airmass · # Frames · Instruments · Dates`.
- **Search bar** that filters the targets table in real time. Plain-text substring by default, with optional regex and case-sensitive toggles. Invalid regex is reported inline.
- **Light / dark theme toggle** in the navbar (sun/moon icon, persisted via `localStorage`).
- **Loading status bar** at the top of the page plus a bottom status line showing `Rendering N targets…` while the table lays out.
- Inline SVG **favicon** (no extra HTTP request).

Photometry and transit-fit runs execute in the background and remain recorded
on the **Jobs** page. A photometry process that exits successfully but reports
`photometry PARTIAL FAILURE` is shown as failed, because one or more requested
bands did not complete. Hiding a job is local to the browser; starting that job
again makes its row visible.

Photometry run logs are isolated by instrument, date, and target. Transit-fit
outputs are stored under `$MUSCAT_TIMER_DIR/<instrument>/<date>/<target>/`
(default `$MUSCAT_TIMER_DIR` is `/ut2/jerome/ql/timer`). Spaces are removed from
the target directory name; empty names and names containing `..`, `/`, or `\`
are rejected.

Calibration and engineering frames (`DARK*`, `FLAT*`, `BIAS*`, `MOVIE`, `FOCUS_ADJUST`, `FoV`, `Muscat commissioning *`, etc.) are excluded from the targets aggregation so the table only shows real science targets.

Fonts, icons, theme, and search are local or inlined. The **Workflow** page loads
Mermaid from jsDelivr to render its pipeline diagrams.

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

The exposure calculator uses these instrument references when scaling its
MuSCAT3 calibration. Full well is in electrons, gain in electrons/ADU, pixel
scale in arcsec/pixel, and aperture in metres.

| Instrument | Full well | Gain | Pixel scale | Aperture |
|---|---:|---:|---:|---:|
| muscat | 55,000 | 1.0 | 0.358 | 1.88 |
| muscat2 | 62,000 | 1.0 | 0.44 | 1.52 |
| muscat3 | 99,000 | 1.8 | 0.267 | 2.0 |
| muscat4 | 99,000 | 1.8 | 0.267 | 2.0 |
| sinistro | 100,000 | 1.5 | 0.39 | 1.0 |

### Migration from Perl

| Perl script | Python equivalent |
|---|---|
| `mkobslog.pl muscat 240101` | `muscat-db scan muscat 240101` |
| `mkobslog_muscat3.pl 240101` | `muscat-db scan muscat3 240101` |
| `auto_mkobslog.pl muscat3 24` | `muscat-db scan-missing muscat3 24` |
| `show_obslog_summary.pl muscat3 240101 0` | `muscat-db summary muscat3 240101 0` |
| `auto_mkobslog_muscat2.sh` (cron) | `muscat-db scan-yesterday` |
