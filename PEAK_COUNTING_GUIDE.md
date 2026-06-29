# Peak Counting & Exposure Prediction Testing Guide

## How Peak Count is Computed

### Single Frame Measurement

**Peak count is measured from a SINGLE FITS frame**, not aggregated across multiple frames.

The measurement process:
1. **Read FITS data** from the primary HDU (or first science extension if primary is empty)
2. **Subtract baseline**: Measure median of entire frame (accounts for bias/dark current)
3. **Compute robust peak**: Use 99.9th percentile of pixel values
4. **Final peak** = 99.9th percentile - median

```python
peak_adu = np.percentile(data, 99.9) - np.median(data)
```

### Why 99.9th Percentile?

- **Robust to cosmic rays**: Max pixel can be artificially high from cosmic ray hit
- **Robust to outliers**: 99.9th percentile is more stable than max()
- **Captures stellar peak**: Still captures the true PSF peak within the core
- **Consistent across frames**: More reproducible than raw maximum

### Why Not Multiple Frames?

Each FITS frame is independent:
- Different airmass
- Different focus position
- Different seeing conditions
- Different exposure time (sometimes)

Averaging would conflate these variables, making calibration unreliable.

---

## Expected vs Observed Peak

### Formula

The exposure calculator predicts peak ADU based on:

```
log10(peak_ADU) = coef - 0.4 * (mag + k * (airmass - 1.1)) + log10(exp / 60)
```

Where:
- `coef`: Calibrated zero-point coefficient (per band, per focus)
- `mag`: Target magnitude (from Pan-STARRS or SkyMapper)
- `k`: Atmospheric extinction coefficient (~0.05-0.15)
- `airmass`: Observed airmass
- `exp`: Exposure time in seconds

### Prediction Process

1. **Look up target magnitude** from catalog (Pan-STARRS DR1 or SkyMapper DR2)
2. **Get calibration coefficient** (from DB if available, else empirical MuSCAT3)
3. **Apply airmass correction** for atmospheric extinction
4. **Compute expected peak** from formula above

### Error Sources

Prediction errors can come from:

| Source | Typical Effect | Mitigation |
|--------|---|---|
| Magnitude uncertainty | ±0.05 mag → ±2% peak | Use bright, well-measured stars |
| Focus variation | ±0.5mm → ±5-10% peak | Use calibrated focus coefficients |
| Airmass accuracy | ±0.02 AM → ±1-2% peak | Use accurate airmass from FITS header |
| Atmospheric seeing | Variable PSF → ±5% peak | Expected natural variation |
| Scattered light | Adds baseline offset | Inherent in calibration |
| Cosmic rays | Biases 99.9th percentile | Low probability at 99.9th |

---

## Testing Script

The script `scripts/test_exposure_predictions.py` validates predictions against real data.

### What It Does

1. **Reads all MuSCAT3 observations** from the database
2. **For each frame**:
   - Measures actual peak from FITS file
   - Looks up target magnitude from Pan-STARRS DR1 or SkyMapper DR2 catalogs
   - Predicts peak using the exposure calculator
   - Computes prediction error
3. **Generates statistics**:
   - Per-band accuracy
   - Per-airmass accuracy
   - Outlier detection
   - Error distributions

### Important Limitations

**Catalog Coverage**: Magnitude lookups only work for targets in Pan-STARRS DR1 (Dec > -30°) or SkyMapper DR2 (Dec < +10°). Many MuSCAT3 targets fall outside these coverage areas or are too faint for catalog detection.

**Coefficient Calibration Range**: Empirical coefficients are calibrated on observations of brighter targets (typically mag 10-15). Very faint targets (mag 18+) and high airmass (>2) regimes have larger prediction errors due to extrapolation outside the calibration range.

### Usage

```bash
# Test 100 frames (default)
python scripts/test_exposure_predictions.py

# Test specific instrument
python scripts/test_exposure_predictions.py --instrument muscat3

# Test more frames
python scripts/test_exposure_predictions.py --max-frames 1000

# Filter by target name
python scripts/test_exposure_predictions.py --object "HAT-P"

# Save results to CSV
python scripts/test_exposure_predictions.py --output results.csv
```

### Output Example

```
================================================================================
ANALYSIS SUMMARY
================================================================================

✓ Valid predictions: 85/100
  Mean error: +2.3%
  Std dev:    4.1%
  Min error:  -8.5%
  Max error:  +11.2%
  Median:     +1.8%

Band        N      Mean Error     Std Dev      Range
────────────────────────────────────────────────────────────────
gp          24     +1.5%          3.2%         -5.2% to +7.8%
rp          22     +2.1%          4.5%         -8.5% to +11.2%
ip          20     +3.2%          4.8%         -4.1% to +9.5%
zs          19     +2.8%          3.9%         -6.3% to +8.7%

Airmass    N      Mean Error     Range
──────────────────────────────────────
 1.0 ±0.1  8      +1.2%          -2.1% to +3.5%
 1.2 ±0.1  18     +2.0%          -3.2% to +6.1%
 1.5 ±0.1  25     +2.5%          -5.1% to +8.2%
 1.8 ±0.1  20     +2.8%          -4.5% to +9.1%
 2.0 ±0.1  14     +3.1%          -2.3% to +11.2%

Status Breakdown:
  ok               : 85 (85.0%)
  saturated        :  8 (8.0%)
  no_magnitude     :  5 (5.0%)
  no_peak          :  2 (2.0%)

⚠️  Outliers (>30% error): 0
```

---

## Understanding Results

### Good Accuracy

**Mean error < 5%** indicates the calibration is working well:
- Predictions are unbiased
- Standard deviation is reasonable
- Can be used for exposure planning with confidence

### Per-Band Differences

Different bands may have different accuracy due to:
- Different optical properties
- Different CCD sensitivity
- Different extinction corrections
- Different number of calibration frames

### Airmass Dependence

If error increases with airmass, suggests:
- Extinction coefficient needs refinement
- More calibration data needed at high airmass
- Or natural atmospheric variability

### Systematic Offsets

If all predictions are consistently high/low:
- Calibration coefficients may need recalibration
- Systematic issue with magnitude lookup
- Possible gain/sensitivity drift over time

---

## Frame Data Used

The database tracks per-frame information:

```
Frame Information:
- instrument: MuSCAT, MuSCAT2, MuSCAT3, MuSCAT4, Sinistro
- obsdate: Observation date
- filename: FITS filename
- object: Target name
- filter: g, r, i, z, or narrowband
- exptime: Exposure time (seconds)
- airmass: Observed airmass
- focus: Focus position (mm)
- ra, dec: Target coordinates
```

All frames with:
- Valid filter name
- Valid target name
- FITS file accessible
- Magnitude lookup successful

are included in the test.

---

## Calibration Database

The script uses two sources for peak coefficients (in priority order):

1. **Database-calibrated coefficients** (`exposure_coeffs` table)
   - Measured from actual MuSCAT3 observations
   - Per-band, per-focus
   - Updated as new data is processed

2. **Empirical MuSCAT3 coefficients** (fallback)
   - From peak_count_estimator project
   - Scaled for other instruments
   - Always available

---

## Next Steps

After running the test:

1. **Analyze accuracy**
   - Is mean error < 5%?
   - Are there systematic issues?

2. **Identify outliers**
   - Which targets/bands have poor predictions?
   - Why? (saturation, bad magnitude, etc.)

3. **Refine calibration**
   - Recalibrate if error is large
   - Focus on underperforming bands
   - Add more calibration frames

4. **Use for planning**
   - Plan observations with known accuracy
   - Account for typical ±5% error in planning
   - Use airmass-corrected predictions for better accuracy

---

## Related Documentation

- `scripts/test_exposure_predictions.py` - Test script
- `src/muscat_db/exposure.py` - Exposure calculator implementation
- `notebooks/exposure_calculator_comparison.ipynb` - Interactive comparison notebook
