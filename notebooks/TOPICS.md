# Notebooks

Short, self-contained tutorials for the `muscat-db` pipeline. Each one reads
real project data where practical (the `muscat.db` observing log, `prose2`
lightcurve CSVs, and the `muscat_db` calculators) and falls back to a clear
message or a small synthetic example when that data is not present, so every
notebook opens and reads cleanly off-host.

## Environment

Run these with the **`prose` conda kernel**, which provides the scientific
stack (numpy, scipy, pandas, matplotlib, astropy, photutils):

```bash
conda activate prose
jupyter lab   # from this directory
```

Data locations (overridable by environment variable):

| Source | Default | Used by |
|--------|---------|---------|
| `muscat.db` | repo root (`../muscat.db`) or `$MUSCAT_DB_PATH` | 01, exposure, crossmatch |
| prose2 lightcurve CSVs | `$MUSCAT_PROSE_DIR` (`~/ql/prose`) | 01, 02 |
| bundled catalogs | `../data/` | crossmatch |

The `muscat_db` package is imported from `../src` (the exposure and FOV
notebooks add a small `dotenv` fallback so the import works under the `prose`
kernel, which does not ship `python-dotenv`).

## Tutorials

### 01. Target Lightcurve Analysis — `01_Target_Lightcurve_Analysis.ipynb`
Look up a target's observing history in `muscat.db`, load its per-band `prose2`
differential lightcurves, and plot flux, time-binning, and diagnostics (airmass,
FWHM). Uses real data for a four-band MuSCAT3 observation of TOI-5191.

### 02. Mini Photometry Walkthrough — `02_Mini_Photometry_Walkthrough.ipynb`
The photometry steps on one synthetic frame: source detection, aperture
photometry with sky annuli, and a differential target/comparison ratio. Ends by
tying the result to a real `prose2` lightcurve. Uses `photutils`.

### 03. Ephemeris and O-C Analysis — `03_Ephemeris_OC_Analysis.ipynb`
Fit a linear transit ephemeris with the project's own fitter
(`muscat_db.ephemeris_math`, the routine behind the web O-C tool) and build the
Observed-minus-Calculated diagram. Shows how a slope means a wrong period and a
wave means a transit-timing variation. Synthetic transit centers.

### 04. FOV Optimization Concepts — `04_FOV_Optimization_Concepts.ipynb`
The geometry of pointing and position-angle optimization: place a rotated camera
footprint over a star field and grid-search for the pointing that captures the
most comparison stars while keeping the target framed. Self-contained synthetic
field; see `fov_optimization.ipynb` for the production version.

## Observation planning

### Exposure Time Calculator — `exposure_time_calculator.ipynb`
Estimate exposure times with `muscat_db.exposure` (the observation-planning
calculator): per-band exposure to a target peak ADU, the airmass/extinction and
defocus trade-offs, why a bright comparison star caps the exposure, and the
per-instrument calibration status read from `muscat.db`.

### FOV Pointing & Orientation Optimization — `fov_optimization.ipynb`
The production `muscat_db.fov` pipeline end to end: instrument footprints from
the VOTable XML, tangent-plane geometry, comparison-star weighting, and a
pointing/PA optimizer over real Gaia DR3 stars (with a synthetic fallback when
the network is unavailable). This is what the `/fov` web page calls.

## Catalog work

### Crossmatch: MuSCAT-db vs. TESS TOIs — `extras_crossmatch_toi_muscatdb.ipynb`
Spatially crossmatch the observed targets in the live `muscat.db` against the
TESS Objects of Interest catalog (`data/TOIs.csv`) with `astropy`, to see which
observed fields are TOIs and their vetting disposition. The same workflow reuses
any catalog with RA/Dec (e.g. `data/nexsci_pscomppars.csv`).

## Utilities

- `_csv_audit.py` — checks that a `muscatdb_targets.csv` export matches the live
  `targets` table in `muscat.db`.
