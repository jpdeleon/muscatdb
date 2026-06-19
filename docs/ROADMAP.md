# muscat-db Roadmap

A prioritized plan for turning the observation-log browser into a research tool
for MuSCAT multi-band transit follow-up. Each item notes the value, the effort,
and the data it depends on (all of which already exists in the schema unless
flagged otherwise).

Schema recap (see `src/muscat_db/database.py`):

- `frames` — per-FITS metadata: `instrument, obsdate, ccd, filename, object,
  jd_start, ut_start, exptime, read_mode, filter, ra, declination, airmass,
  focus, pa` (~9.6 M rows).
- `summaries` — per `(instrument, obsdate, ccd, object, exptime, read_mode)`
  with `frame_start/end, ut_start/end, nframes`.
- `targets` — materialized per-object aggregate (dates, instruments, filters,
  RA/Dec, airmass range, total exposure, `is_identified`).
- `target_notes` — free-text notes, edited inline in the UI.

---

## Status of recent work (done)

- gzip middleware on responses (2.85 MB targets page → ~177 KB on the wire).
- `get_dates()` reads `summaries` instead of scanning `frames` (7.4 s → 0.06 s).
- Homepage render cached on DB mtime (cold ~7 s → cached ~0.007 s,
  auto-invalidated on note edits / `build-db`).
- Instrument-column filter uses whole-token matching (`muscat` no longer
  matches `muscat3/4`).
- Daily scan cron line documented.
- Per-target/night photometry and transit-fitting pages run the external
  pipelines, display their products, and persist status in a unified Jobs page.
- Workflow page documents ingestion, photometry, and transit fitting with
  Mermaid diagrams.

---

## Phase 1 — Consolidate the science view (highest value)

**Goal:** make a single target the unit of navigation across observing nights.

### 1.1 Per-target detail page  `/target/{name}`
- **Why:** clicking a target currently does nothing; its observations are
  scattered across date pages. This is the most glaring navigation gap.
- **Contents:** all observations across instruments/nights, per-band coverage,
  total exposure, airmass range, RA/Dec, a chronological timeline, and the
  target's note.
- **Data:** all present (`frames` / `summaries` / `targets`). No schema change.
- **Effort:** S–M. New route + template + one aggregation query (prefer
  `summaries`).

## Phase 2 — Planning & discovery

### 2.1 Observability tools
- Airmass/altitude-vs-time curve, moon separation, "observable tonight from
  Maunakea/Haleakalā?" Uses RA/Dec + `astroplan`/`astropy`.
- **Effort:** M. New dependency (`astroplan`).

### 2.2 Expand external catalog integration + name resolution
- Transit Fit already queries the NASA Exoplanet Archive and links its target
  overview. Add automatic links from other target views to SIMBAD and
  ExoFOP-TESS.
- Resolve `TIC`/`TOI` names → canonical coordinates & magnitudes.
- Optional embedded Aladin Lite sky cutout per target.
- **Effort:** M. External HTTP calls — cache results in a new `target_xmatch`
  table to avoid per-request latency.

### 2.3 Cone / coordinate search
- "All targets within N arcmin of RA/Dec." Essential for cross-matching and
  spotting duplicate target entries.
- **Effort:** S–M. Needs decimal RA/Dec columns (currently sexagesimal text) —
  add `ra_deg`, `dec_deg` to `targets` at build time.

---

## Phase 3 — Data quality & integrity

### 3.1 Surface the column-shift detector
- `scripts/check_bad_columns.py` already finds legacy column-shift artifacts
  (the `MAX(ra)` pollution, e.g. the TOI1453 `|` case). Surface a data-quality
  flag in the UI instead of failing silently.
- **Effort:** S.

### 3.2 Target-name aliasing
- `TOI1453` / `TOI-1453` / `TOI 1453` likely fragment into separate targets.
  Add a normalization/alias map and consolidate in the `targets` aggregation.
- **Effort:** M. Touches `_populate_targets`.

### 3.3 Calibration coverage
- Per night, show whether darks/flats/bias exist.
- **Effort:** S. Data is in `frames` (object names) but currently filtered out
  of `targets`; query `frames` directly.

---

## Phase 4 — Access, sharing, scale

### 4.1 Shareable URL state
- Encode search/filter/sort in query params so a filtered view is linkable.
- **Effort:** S (client-side).

### 4.2 Server-side pagination / filtering for the targets table
- Complements gzip + cache; lets the table scale past a few thousand targets.
- **Effort:** M. Needs a JSON endpoint + client rewrite of the table.

### 4.3 JSON API + export
- `/api/...` read endpoints for notebooks/scripts; CSV/VOTable export; direct
  FITS-path links.
- **Effort:** M.

### 4.4 Filters-column token matching
- Give the Filters column the same whole-token matching as Instruments
  (`g` currently also matches `gp`).
- **Effort:** XS.

### 4.5 Optional auth
- Only if notes become multi-observer.
- **Effort:** M.

---

## Phase 5 — Architecture / performance

### 5.1 Incremental `build-db`
- Rebuild only changed dates rather than the whole DB.
- **Effort:** M. Changes ingestion + aggregation to be delta-aware.

### 5.2 WAL mode
- Let the site stay readable while a scan/build writes.
- **Effort:** XS (`PRAGMA journal_mode=WAL`), with testing.

### 5.3 Extend the mtime cache
- Apply the homepage cache pattern to instrument/date pages if they grow.
- **Effort:** S.

---

## Suggested sequencing

1. **Phase 1** (cross-night target page) — connects the existing per-night
   photometry and transit-fit workflows through one target-centric view.
2. **Phase 3.1 + 3.2** (data-quality flags + aliasing) — correctness/trust;
   cheap and improves every other view.
3. **Phase 2** (planning + catalog links) — turns review into planning.
4. **Phase 4** (sharing/scale) and **Phase 5** (perf) as the dataset and user
   base grow.

Quick wins worth doing anytime: **4.4** (XS), **3.1** (S), **5.2** (XS),
**4.1** (S).
