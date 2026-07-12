# Closed-Loop Test Observation Optimizer

## Summary

Add a **Test observation** button to the LCO Schedule page for MuSCAT3, MuSCAT4, and Sinistro. It will create a short adaptive experiment, constrained to 10 minutes of exposure time, to validate two candidate FOV pointings and bracket the predicted exposure/defocus settings under conditions similar to the upcoming transit.

The workflow remains observer-controlled:

1. Generate an experiment from the current target, transit, instrument, and site.
2. Review the sequence and estimated telescope time.
3. Run the normal LCO dry-run and explicitly confirm submission.
4. Track and download completed observations.
5. Analyze observed peaks, FWHM, background, cadence, WCS placement, and comparison-star quality.
6. Present evidence-backed recommendations that the observer may apply to the transit form.

## Implementation Changes

### Experiment planning and UI

- Place **Test observation** beside **Show FOV** and **Show Exp** on the LCO Schedule page.
- Open a review dialog populated from the current schedule state: target, coordinates, instrument, site, transit window, filters, expected airmass/lunar conditions, and proposal.
- Call the existing FOV optimizer and exposure calculator through shared backend functions rather than duplicating their logic.
- Compare the optimizer’s best and fallback pointings. Preserve center coordinates, PA, predicted comparison stars, edge margins, and saturation-limiting stars.
- Select test windows before the transit by ranking observable intervals for similarity to the transit’s site, airmass, lunar separation, and expected observing time. Show the mismatch when no close window exists.
- Fit the experiment within a default 10-minute exposure budget, excluding acquisition/readout overhead from that labeled exposure budget but displaying both estimated exposure and wall-clock totals.
- Persist all new wizard inputs and defaults in the LCO page’s local-storage helpers.

### Adaptive test sequence

- Start from the calculator’s exposure recommendation using target and in-field comparison-star magnitudes, expected test airmass, configured saturation fraction, filter/readout mode, and defocus.
- Generate exposure candidates around the nominal prediction, initially `0.67×`, `1.0×`, and `1.5×`, clipped to instrument-supported limits and rejected when any important target/comparison star is predicted to exceed the saturation threshold.
- Obtain the live LCO instrument schema/capabilities and cache it with a timestamp. Offer focus/defocus variation only when an official writable field and valid range are present.
- When focus control is supported, test the predicted optimum plus adjacent supported settings, normally `−1 mm`, nominal, and `+1 mm`, clipped and deduplicated. Never encode defocus in an unverified payload field.
- Allocate at least three repeated frames per retained exposure/focus/pointing combination. If the 10-minute budget is exceeded, retain both FOVs and the nominal exposure first, then retain exposure brackets, then adjacent focus settings; show every removed combination.
- For MuSCAT, treat the four simultaneous bands as one configuration and optimize each band’s exposure while respecting the driving longest exposure. For Sinistro, build filter-specific configurations.
- Extend the LCO request builder to support an ordered list of test configurations in one request while leaving normal transit payloads unchanged.
- Identify requests with structured application metadata and a human-readable `TEST` request name, without relying on the name for analysis linkage.

### Submission, persistence, and lifecycle

- Introduce an app-owned `test_observations` record containing a stable ID, target/instrument/site, transit association, planned settings, FOV candidates, LCO request IDs, payload hash, lifecycle state, analysis version, timestamps, and recommendation summary.
- Store individual planned configurations and measured results as versioned JSON associated with that record; preserve this app-owned data during the daily database rebuild.
- Use the existing dry-run/hash/confirmation protections. The new button must not bypass `MUSCAT_LCO_ALLOW_SUBMIT`, proposal validation, or explicit telescope-time confirmation.
- Poll request status through existing LCO facilities and use the archive downloader when frames become available. Make retries idempotent by LCO request ID and frame ID.
- Expose states such as draft, validated, submitted, pending, downloading, analyzing, complete, partial, and failed, with actionable failure details.
- Do not modify FITS files. Store derived measurements and provenance separately.

### Analysis and recommendation

- Refactor `compare_observed_peaks.py` into reusable analysis functions plus its existing CLI wrapper.
- Support MuSCAT3, MuSCAT4, and Sinistro BANZAI/WCS products. For MuSCAT4, estimate a robust field-wide WCS translation from Gaia matches before measuring stars, record the offset, and fail clearly if the match is ambiguous.
- Measure per frame and star:
  - local background-subtracted peak and saturation/non-linearity margin;
  - FWHM or equivalent PSF width and ellipticity;
  - sky/background level and scatter;
  - WCS residual and distance to detector edge;
  - usable target/comparison-star detections;
  - actual cadence from timestamps and exposure/readout timing.
- Compare observed peaks with `calc_peak`, retaining magnitude provenance and uncertainty/approximation flags. Use robust medians and dispersion across repeats; do not treat a single frame as confirmation.
- Rank settings by expected differential-photometry precision per unit time, including target and ensemble comparison-star photon noise, background/read noise, measured cadence, and comparison availability.
- Enforce hard acceptance checks: target and required comparisons remain in-frame, no selected star violates the configured saturation margin, enough unsaturated comparison stars are measurable, and WCS/PSF measurements are reliable.
- Report predicted-versus-observed peak residuals, transparency/seeing variability, rejected settings, and confidence. Mark recommendations provisional when conditions differ materially from the transit or repeats are insufficient.
- Present the recommended exposure times, FOV center/PA, readout/filter configuration, and supported defocus with a **Review and apply** action. Applying changes only populates the unsent transit form and invalidates any earlier dry-run hash.

## Interfaces

- Add APIs to:
  - generate and validate a test plan;
  - dry-run and submit the plan through the existing guarded LCO flow;
  - fetch lifecycle/status and measured results;
  - analyze or safely retry analysis;
  - apply an accepted recommendation to the current form state.
- Extend `build_requestgroup` with an optional ordered `configurations` input for test requests. Existing single-configuration callers and payloads remain compatible.
- Return optimizer provenance in every plan/result: calculator coefficients, catalog/magnitude sources, FOV optimizer settings, instrument capability response, assumed conditions, software analysis version, and FITS/frame identifiers.

## Test Plan

- Unit-test adaptive bracketing, instrument-limit clipping, saturation rejection, focus capability gating, budget pruning, repeat allocation, and deterministic plan generation.
- Verify normal MuSCAT and Sinistro scheduling payloads are unchanged.
- Test multi-configuration MuSCAT and Sinistro payloads against mocked LCO validation responses, including unsupported focus fields and stale capability data.
- Test dry-run hash invalidation whenever a test or applied transit setting changes.
- Test lifecycle idempotency, partial archive availability, download retry, duplicate callbacks/polls, analysis retry, and daily DB rebuild preservation.
- Create synthetic FITS/WCS tests for observed peaks, background, PSF metrics, edge detection, missing stars, saturation, cosmic-ray contamination, and MuSCAT4 constant WCS offsets.
- Test recommendation ranking where cadence, S/N, saturation, comparison ensemble, and seeing favor different candidates.
- Test degraded outcomes: unmatched catalog, approximate magnitudes, cloudy/variable repeats, no suitable pre-transit window, incomplete filters, and insufficient confidence.
- Exercise the complete GUI as an observer: generate, edit, dry-run, confirm, monitor, inspect evidence, apply recommendations, and re-run the transit dry-run.
- Run the default suite with `uv run pytest`; keep real LCO/API and full FITS workflow tests explicitly marked slow/integration and non-submitting unless live submission is deliberately enabled.

## Assumptions and Defaults

- Initial scope is MuSCAT3, MuSCAT4, and Sinistro; MuSCAT/MuSCAT2 remain out of scope because they require calibration and lack header WCS.
- The default test evaluates the best and fallback FOVs and uses a 10-minute exposure-time budget.
- Transit precision per unit wall-clock time is the optimization objective.
- Defocus is available only after verification through the live LCO instrument schema; unsupported instruments still receive exposure/FOV testing.
- Recommendations require explicit observer review and never modify or submit an existing transit request automatically.
- Header metadata takes precedence over planned or hardcoded values during analysis.
- HTML/JavaScript changes require restarting the server because the current reload mode watches only Python files.
