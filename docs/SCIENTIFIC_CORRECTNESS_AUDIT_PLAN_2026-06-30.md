# Scientific Correctness Audit Plan

Date: 2026-06-30
Scope: post-database-health audit priorities for scientific correctness

## Priority Order

### 1. FITS-header-to-obslog correctness

Audit whether the following values are extracted correctly for each instrument:

- `OBJECT`
- `RA/DEC`
- `FILTER`
- `EXPTIME`
- `AIRMASS`
- `FOCUS`
- `PA`
- time fields (`MJD/JD/UT`)

Reason:
Bad metadata here directly corrupts target selection, grouping, photometry options, and transit timing.

### 2. Per-instrument scan and calibration assumptions

Audit that `muscat`, `muscat2`, `muscat3`, `muscat4`, and `sinistro` each use the correct:

- header keys
- WCS assumptions
- calibration requirements
- gain / saturation interpretation

Reason:
Scientific bugs often hide at instrument-specific boundaries.

### 3. Time-system correctness

Audit `MJD/JD/UT` handling end-to-end.

Verify:

- no off-by-one-day errors
- no UTC / local-time confusion
- no precision loss
- no timestamp truncation affecting photometry or transit fits

Reason:
Transit science is highly sensitive to timing errors.

### 4. Target grouping and observation segmentation

Audit how frames are grouped into `summaries` and how dates / objects are segmented.

Verify:

- different targets are not merged
- a single run is not split incorrectly
- cadence and coverage are not inflated by grouping mistakes

### 5. Photometry input audit

For a sample of real nights per instrument, verify that the arguments sent to `run_photometry.py` match the actual obslog and FITS reality.

Check:

- bands
- apertures
- reference band
- site
- mode
- target coordinates
- calibration paths

### 6. WCS and coordinate audit

Especially for `muscat3`, `muscat4`, and `sinistro`, verify that source positions used by photometry match the true field.

Special case:

- explicitly audit the possible constant WCS offset in `muscat4`

### 7. Calibration audit for raw instruments

For `muscat` and `muscat2`, verify:

- photometry is never run on uncalibrated data
- calibration products match the correct night, instrument, and chip

### 8. Transit-fit scientific audit

Validate that the transit fitter consumes the correct:

- timestamps
- uncertainties
- priors
- detrending assumptions

Cross-check a small set of known systems against expected mid-transit times and depths.

### 9. Gold-dataset regression audit

Pick a small curated set of nights across all five instruments with trusted outcomes.

Re-run:

- scan
- database ingest
- photometry
- transit fit

Compare outputs against known-good reference results.

### 10. Archive consistency audit

Compare local metadata against:

- raw FITS headers
- LCO or archive metadata where relevant

Reason:
This catches silent local corruption and historical scan mistakes.

## Recommended Next Audit

Start with:

### FITS header -> obslog CSV -> frames / summaries validation

Reason:
This is the narrowest layer where metadata errors become scientific errors, and the current blank-object and duplicate-row findings already show this layer needs direct scrutiny.

## Suggested Execution Strategy

1. Choose a small representative night for each of the five instruments.
2. Compare raw FITS headers against obslog CSV rows field by field.
3. Compare obslog CSV rows against `frames` rows in `muscat.db`.
4. Compare `frames` grouping against `summaries`.
5. Use the validated nights as the seed of a permanent gold-dataset regression suite.
