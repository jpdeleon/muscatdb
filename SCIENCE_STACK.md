# Science Stack — muscat-db

Date: 2026-07-10

## Overview

A comprehensive scientific platform for managing and analyzing multi-band astronomical observation data, primarily focused on exoplanet transit observations. It integrates sophisticated pipelines for photometry, transit fitting, and ephemeris analysis, alongside tools for observation planning and catalog browsing. Designed for active research, enabling rapid data reduction, robust model fitting, and scientific discovery across a fleet of robotic telescopes.

---

## Core Scientific Disciplines & Problem Domains

| Discipline | Focus |
|---|---|
| **Exoplanet Transit Photometry** | Extraction of high-precision lightcurves from FITS images, differential photometry, optimal aperture selection, and systematic noise reduction. |
| **Exoplanet Transit Fitting** | Bayesian inference of exoplanet and stellar parameters from lightcurves using MCMC sampling, including limb darkening and astrophysical noise modeling. |
| **Ephemeris Analysis (O-C / TTVs)** | Characterization of transit timing variations (TTVs) and observed-minus-calculated (O-C) diagrams to detect perturbing bodies or orbital dynamics. |
| **Field-of-View (FOV) Optimization** | Maximization of comparison star coverage for differential photometry through intelligent telescope pointing and position angle optimization. |
| **Exposure Time Calculation** | Prediction of signal-to-noise ratios (S/N) and optimal exposure durations based on instrument characteristics and target properties. |
| **Astronomical Catalog Integration** | Cross-matching observations with professional catalogs (Gaia, NASA Exoplanet Archive, TOI) for target identification, stellar parameters, and contamination assessment. |
| **Time-Series Analysis** | Techniques for handling and analyzing time-domain astronomical data, including barycentric corrections and periodogram analysis. |

---

## Key Scientific Algorithms & Models

| Algorithm/Model | Purpose | Details & Methodology |
|---|---|---|
| **Photometry Reduction (prose2)** | Transforms raw FITS frames into calibrated lightcurves. | - **Band Grouping**: Sorting frames by filter (gp, rp, ip, zs, narrowbands).<br>- **Reference Frame Selection**: Positional or quality-based selection of a high-quality reference frame for alignment (e.g., sharpest FWHM, low airmass).<br>- **Source Detection**: Identifying point sources on reference frames (e.g., using Gaussian PSF fitting).<br>- **Target Identification**: WCS matching against MAST/SIMBAD coordinates.<br>- **Gaia-based Aperture Sizing**: Dynamic adjustment of aperture and annulus radii to avoid contaminating sources based on Gaia DR3.<br>- **Parallel Reduction**: Efficient processing of frames (trimming, Gaussian modeling, Twirl alignment, centroid tuning, flux extraction).<br>- **Differential Photometry (Broeg et al. 2005)**: Iterative, optimal selection and weighting of comparison stars using robust scale estimators (MAD) to minimize lightcurve scatter.<br>- **BJD Time Correction**: Conversion from JD-UTC to BJD-TDB using `astropy.time` or `barycorrpy` for high-precision timing. |
| **Transit Fitting (timer)** | Infers exoplanet and host star parameters from transit lightcurves. | - **Bayesian Inference**: Utilizes `PyMC` with the NUTS (No-U-Turn Sampler) algorithm for efficient MCMC sampling.<br>- **Transit Models**: Supports `batman` or `starry` for flexible transit lightcurve modeling.<br>- **Limb Darkening**: Employs quadratic or non-linear limb darkening laws, with coefficients either fixed or sampled with priors.<br>- **Systematics Models**: Linear regression or Gaussian Processes (GP) to model instrumental or atmospheric systematics.<br>- **Pre-optimization**: L-BFGS algorithm for Maximum A Posteriori (MAP) estimation to initialize MCMC chains.<br>- **Parameter Priors**: Incorporates stellar parameters (Teff, logg, metallicity) and planetary priors (period, T0, duration, impact parameter) from catalogs or user input. |
| **TTV Fitting (harmonic)** | Analyzes transit timing variations for perturbing bodies. | - **Multi-harmonic Model**: Implements the Lithwick, Xie & Wu (2012) near-resonant TTV model.<br>- **MCMC Sampling**: Employs `emcee` (ensemble MCMC sampler) for posterior sampling.<br>- **Configuration**: Uses `config.ini` to define TTV amplitudes, super-periods, phase references, and options for non-transiting outer planets or phase offsets.<br>- **Input Assembly**: Transit centers (`Tc`, `tc_unc`, epoch) derived from O-C analysis are used as input. |
| **FOV Optimization (fov.py)** | Identifies optimal telescope pointing for photometry. | - **Grid-Search**: Systematically varies pointing offsets (east, north) and position angles (PA) to maximize comparison star coverage.<br>- **Star Scoring Heuristics**: Ranks potential comparison stars based on magnitude offset, color similarity, and blending from Gaia DR3.<br>- **Observability Check**: Validates target visibility based on site latitude limits and altitude constraints. |
| **Exposure Time Calculation (exposure.py)** | Determines optimal exposure settings for desired S/N. | - **Empirical Coefficients**: Relies on instrument-specific, band-dependent, and focus-dependent calibration coefficients.<br>- **S/N Estimation**: Calculates expected S/N for a given exposure time or vice-versa.<br>- **Saturation Check**: Flags exposures risking detector saturation based on CCD full-well limits. |

---

## Data Sources & Scientific Input

| Source | Type | Purpose |
|---|---|---|
| **FITS Files (raw/calibrated)** | Image data | Primary input for photometry, containing raw astronomical observations. |
| **Observation Logs (CSV)** | Metadata | Extracted FITS header information (object, instrument, date, filter, coordinates, exposure time). Used for database indexing and job scheduling. |
| **Gaia DR3 Catalog** | Stellar catalog | Used for FOV optimization (comparison star selection, contamination assessment) and WCS matching. |
| **NASA Exoplanet Archive** | Exoplanet catalog | Provides ephemeris, stellar, and planetary parameters for transit fitting priors, and transit/visibility verification. |
| **TESS Objects of Interest (TOI) Catalog** | Exoplanet candidate catalog | Provides candidate ephemeris and stellar parameters for transit fitting priors and target browsing. |
| **LCO Observation Portal API** | Operational data | Real-time telescope scheduling, observation request validation (IPP), and archive download manifest. |
| **nova.astrometry.net API** | WCS solution service | External plate-solving for FITS images lacking WCS headers (e.g., MuSCAT, MuSCAT2). |

---

## Scientific Validation & Reproducibility

| Aspect | Method / Tool | Description |
|---|---|---|
| **Code Testing** | `pytest` | Comprehensive unit and integration tests (including `@pytest.mark.slow` for full-pipeline runtime tests with real data) ensure algorithmic correctness and robustness. |
| **Diagnostic Plots** | `matplotlib`, `Plotly` | Generated automatically by science pipelines (prose2, timer, harmonic) for visual inspection of reduction quality, fit convergence, and residual analysis. Includes corner plots, trace plots, lightcurves, FWHM/airmass/centroid systematics. |
| **Provenance Tracking** | `meta.yaml` | Each pipeline run generates a `meta.yaml` file recording `muscat-db` and external pipeline (prose2, timer, harmonic) versions, full configuration options, and run parameters for full reproducibility. |
| **External Comparison** | Manual/Automated | Verification of transit and visibility predictions against established sources like the NASA Exoplanet Archive. Comparison of `timer` pipeline results with other transit fitting codes. |
| **Data Integrity** | Database schema constraints | Ensures consistency of observation metadata and analysis results. |

---

## Scientific Computing Environment

| Component | Technology |
|---|---|
| **Main Language** | **Python >=3.12** |
| **Core Scientific Libraries** | `astropy` (FITS, WCS, coordinates, time), `numpy`, `scipy` |
| **Astronomical Catalogs** | `astroquery` |
| **Bayesian Inference** | `PyMC` (for `timer`), `emcee` (for `harmonic`) |
| **Parallel Processing** | `prose.SequenceParallel` (multiprocessing workers for photometry) |
| **FITS File Handling** | `astropy.io.fits` |
| **Database** | SQLite3 (for metadata and job persistence) |
| **Environment Management** | `uv`, `conda` (for external science pipelines) |

---

## Scientific Architecture Characteristics

- **Modularity**: Core scientific algorithms are encapsulated in external, version-controlled repositories (`prose2`, `timer`, `harmonic`), enabling independent development and robust versioning.
- **Data-driven**: Workflow is driven by observational data (FITS files, CSV logs) and integrated astronomical catalogs.
- **Pipeline Orchestration**: `muscat-db` acts as an orchestration layer, invoking external science pipelines as subprocesses in isolated environments, capturing and tracking their outputs.
- **Reproducibility Focus**: Extensive provenance tracking and diagnostic outputs support scientific reproducibility.
- **Interactive Analysis**: Web GUI provides interactive tools for ephemeris analysis, FOV optimization, and catalog browsing, facilitating rapid scientific exploration.

---

## Planned / Future Scientific Enhancements

| Enhancement | Status | Potential Impact |
|---|---|---|
| **GPU Acceleration** | Investigating | Significant speedup for computationally intensive tasks (e.g., photometry, MCMC sampling) using `Numba` with CUDA or `CuPy`. |
| **Advanced Systematics Modeling** | Planned | Integration of more sophisticated Gaussian Process (GP) models or machine learning techniques for robust detrending of lightcurves. |
| **Automated Planet Search** | Research | Implementation of algorithms for automated detection of transiting exoplanets directly from observation logs. |
| **Multi-instrument Calibration** | Planned | Improved cross-instrument calibration procedures for more consistent multi-wavelength photometry. |
| **Expanded Catalog Integration** | Planned | Integration with additional exoplanet and stellar catalogs (e.g., TESS Input Catalog, Kepler, K2) to enrich target information. |
| **Advanced TTV Analysis** | Research | Incorporation of N-body simulations for more complex TTV modeling, especially for systems with strong mutual interactions. |
