### I. Data Exploration & Visualization

1.  **Interactive Target Lightcurve Analysis**:
    *   **Purpose**: Showcase raw and differential lightcurve data for a specific exoplanet target.
    *   **What it showcases**: Data loading, basic visualization (plotting different bands), interactive binning, and visual identification of transit events. This highlights the immediate scientific output of the photometry pipeline.

2.  **Astronomical Catalog Browsing and Filtering**:
    *   **Purpose**: Demonstrate how to query and filter exoplanet and stellar catalogs.
    *   **What it showcases**: Integration with TOI, NASA Exoplanet Archive, and Gaia DR3. Users can visualize population properties (e.g., period-radius diagrams, stellar parameters) and identify targets of interest.

3.  **FITS Header Exploration and Metadata Visualization**:
    *   **Purpose**: Illustrate the richness of metadata extracted from raw FITS files.
    *   **What it showcases**: Extracting and visualizing key FITS header keywords (e.g., observing conditions, instrument settings, WCS information) across multiple frames, demonstrating the initial data ingestion step.

### II. Core Scientific Pipeline Demonstrations (Simplified)

1.  **Mini-Photometry Workflow**:
    *   **Purpose**: Provide a simplified, step-by-step walkthrough of the photometry process.
    *   **What it showcases**: Core `prose2` concepts like source detection, aperture placement (manual vs. Gaia-based), and differential photometry on a small subset of frames. This can explain the logic behind the automated pipeline.

2.  **Conceptual Transit Model Visualization**:
    *   **Purpose**: Explain how transit lightcurve models are constructed and parameterized.
    *   **What it showcases**: Interactive plots of `batman` or `starry` models, allowing users to adjust parameters (e.g., planet radius, period, impact parameter, limb darkening) and immediately see their effect on the lightcurve shape.

3.  **O-C Diagram Generation and Linear Ephemeris Fitting**:
    *   **Purpose**: Demonstrate the process of creating Observed-minus-Calculated (O-C) diagrams.
    *   **What it showcases**: Loading transit midpoints, fitting a linear ephemeris, calculating O-C residuals, and plotting the results to visually identify potential TTVs.

### III. Observation Planning & Optimization

1.  **Field-of-View (FOV) Optimization Scenario**:
    *   **Purpose**: Illustrate how the system optimizes telescope pointing.
    *   **What it showcases**: Visualizing the stellar field (from Gaia), instrument footprint, and how adjusting pointing offsets and position angles can maximize comparison star coverage while keeping the target within the FOV.

2.  **Exposure Time Calculation & S/N Estimation**:
    *   **Purpose**: Demonstrate how to calculate optimal exposure times or predict S/N for observations.
    *   **What it showcases**: Using instrument characteristics and target magnitudes to determine exposure durations for desired S/N, and checking for potential detector saturation.

### IV. Integration & Reproducibility

1.  **Accessing `muscat-db` Data Programmatically**:
    *   **Purpose**: Show how to query the internal SQLite database directly from Python.
    *   **What it showcases**: Retrieving observation metadata, job results, or configured parameters, demonstrating the power of the `muscat.db` as a scientific data backend.

2.  **Reproducing Pipeline Diagnostic Plots**:
    *   **Purpose**: Verify the scientific output of the automated pipelines.
    *   **What it showcases**: Loading the results of a `prose2` or `timer` run and regenerating key diagnostic plots (e.g., corner plots from MCMC, detailed lightcurve fits, FWHM/airmass trends) to highlight reproducibility and quality control.
