# TOI-1410 sky-annulus experiment

## Summary

The automated sky annulus is an appropriate shared setting for the MuSCAT3
TOI-1410 dataset in observation directory `260716`. The automatic per-band
annuli were `20,31` or `20,32` pixels. Moving the annulus slightly outward did
not materially improve the aggregate 10-minute precision, while annuli closer
to the nearby star increased correlated noise. The automatic setting should be
retained for the production reduction.

This conclusion is specific to this dataset. It does not by itself justify a
global change to the Prose annulus heuristic.

## Dataset and baseline

- Instrument: MuSCAT3
- Observation directory: `260716`
- FITS header observation date used in product names: `260715`
- Target: TOI-1410
- Bands and frame counts:
  - `g_narrow`: 249
  - `Na_D`: 47
  - `i_narrow`: 195
  - `z_narrow`: 231
- Prose version: 3.3.4
- Reference selection: first frame, independently for each band
- Sigma clipping: disabled
- Nearby-star comparison exclusion: automatic

The initial ten-frame automatic test measured target FWHMs of 11.03--12.21
pixels. It selected annuli of `20,31` or `20,32` pixels. The aperture overlay
shows a nearby Gaia source approximately 55 pixels from the target with
Delta-G about 4.9 mag, corresponding to roughly 1.1% of the target flux. Prose
therefore did not classify it as a contaminant under the current 2.5-mag
(approximately 10%-flux) threshold. Its core is nevertheless well outside the
automatic annulus.

## Experiment design

Four complete reductions were run in separate named output directories. The
candidate aperture grid was held at `12,14,16,18` pixels while only the sky
annulus was changed:

| Run name | Inner radius (px) | Outer radius (px) | Purpose |
|---|---:|---:|---|
| `annulus-fixed-20-32` | 20 | 32 | Automatic-geometry control |
| `annulus-fixed-24-36` | 24 | 36 | Small outward shift |
| `annulus-fixed-32-44` | 32 | 44 | Outer-annulus test |
| `annulus-fixed-42-54` | 42 | 54 | Nearby-star negative control |

All 722 frames were retained in every run. GIF generation and Gaia overlays
were omitted because they do not affect the extracted photometry. Existing
`default` products were not overwritten.

The reductions used the following command form:

```console
$HOME/miniconda3/envs/prose/bin/python -m prose.scripts.run_photometry \
  --target_name TOI-1410 \
  --data_dir /data/MuSCAT3/260716 \
  --results_dir $HOME/ql/prose/muscat3/260716/_runs/TOI-1410/annulus-fixed-RIN-ROUT \
  --bands g_narrow Na_D i_narrow z_narrow \
  --aper_radii 12,18,2 \
  --annulus RIN,ROUT \
  --avoid_nearby_star \
  --overwrite \
  --verbose
```

## Metrics

Three metrics were calculated from each output CSV:

- **RMS:** standard deviation of the median-normalized differential flux.
- **Point-to-point:** robust white-noise estimate,
  `MAD(diff(flux)) * 1.4826 / sqrt(2)`.
- **10-minute:** standard deviation of ten-minute median bins after subtracting
  a quadratic time trend. This reduces sensitivity to the transit and broad
  airmass trend while retaining time-correlated noise.

All values below are parts per thousand (ppt); lower is better.

| Annulus | Band | RMS | Point-to-point | 10-minute | Selected aperture (px) | Comparisons |
|---|---|---:|---:|---:|---:|---|
| 20--32 | `g_narrow` | 1.318 | 0.968 | 0.448 | 18 | 2, 1 |
| 20--32 | `Na_D` | 2.670 | 1.588 | 0.773 | 18 | 2, 8, 7, 1 |
| 20--32 | `i_narrow` | 1.391 | 0.901 | 0.512 | 14 | 1, 2, 5 |
| 20--32 | `z_narrow` | 1.240 | 0.810 | 0.509 | 18 | 1, 2 |
| 24--36 | `g_narrow` | 1.333 | 0.885 | 0.394 | 14 | 1, 2, 3, 5 |
| 24--36 | `Na_D` | 2.801 | 1.978 | 0.909 | 18 | 2, 8, 7, 1 |
| 24--36 | `i_narrow` | 1.277 | 0.803 | 0.482 | 12 | 1, 2 |
| 24--36 | `z_narrow` | 1.240 | 0.704 | 0.529 | 18 | 1, 2, 3, 5, 4 |
| 32--44 | `g_narrow` | 1.321 | 0.880 | 0.438 | 16 | 1, 2, 3 |
| 32--44 | `Na_D` | 2.864 | 2.013 | 0.910 | 18 | 2, 8, 7, 6, 1 |
| 32--44 | `i_narrow` | 1.422 | 0.843 | 0.566 | 12 | 1, 5, 2 |
| 32--44 | `z_narrow` | 1.244 | 0.727 | 0.573 | 18 | 1, 2, 3, 5, 4 |
| 42--54 | `g_narrow` | 1.314 | 0.982 | 0.472 | 18 | 1, 2 |
| 42--54 | `Na_D` | 3.028 | 2.065 | 0.975 | 18 | 8, 2, 7 |
| 42--54 | `i_narrow` | 1.282 | 0.863 | 0.488 | 12 | 1, 2 |
| 42--54 | `z_narrow` | 1.267 | 0.769 | 0.572 | 18 | 1, 2, 3, 5 |

Median ratios across the four bands, relative to the `20,32` control, are:

| Annulus | RMS ratio | Point-to-point ratio | 10-minute ratio |
|---|---:|---:|---:|
| 20--32 | 1.000 | 1.000 | 1.000 |
| 24--36 | 1.006 | 0.903 | 0.991 |
| 32--44 | 1.013 | 0.923 | 1.115 |
| 42--54 | 1.009 | 0.987 | 1.089 |

## Interpretation

The `24,36` annulus improved point-to-point precision in `g_narrow`,
`i_narrow`, and `z_narrow`, but worsened `Na_D` point-to-point noise from 1.588
to 1.978 ppt and its 10-minute noise from 0.773 to 0.909 ppt. Across all bands,
it gave no meaningful improvement in either total or 10-minute scatter.

Moving the annulus to `32,44` or `42,54` pixels increased the median 10-minute
noise by about 12% and 9%, respectively. The negative control therefore
supports keeping the annulus away from the nearby source and its defocused
wings.

The GUI supplies one annulus setting for all bands. Consequently, a small
band-specific improvement in white noise is insufficient reason to replace the
automatic shared setting, especially when the low-cadence `Na_D` light curve
becomes worse.

## Recommendation

Use the automated sky annuli for the full production reduction of this
dataset. Do not move the outer radius toward the nearby source.

A possible future improvement is to replace the hard 2.5-mag neighbor cutoff
with a contamination estimate based on flux ratio, separation, measured FWHM,
and a PSF-wing model. That is a critical global design change and should be
validated across multiple crowded and defocused datasets before implementation.

## Limitations

- The aperture candidate grid was fixed, but Prose still selected the best
  aperture and comparison ensemble independently for each run. This experiment
  therefore evaluates end-to-end pipeline quality rather than isolating only
  the background estimator.
- `Na_D` contains only 47 frames, so its time-binned metric is less precise than
  those of the other bands.
- The conclusion is based on one target and one observing sequence.

## Products

The complete products are stored under:

```text
$HOME/ql/prose/muscat3/260716/_runs/TOI-1410/annulus-fixed-20-32/
$HOME/ql/prose/muscat3/260716/_runs/TOI-1410/annulus-fixed-24-36/
$HOME/ql/prose/muscat3/260716/_runs/TOI-1410/annulus-fixed-32-44/
$HOME/ql/prose/muscat3/260716/_runs/TOI-1410/annulus-fixed-42-54/
```

They can be selected by run name on the photometry page. For example:

```text
http://localhost:8000/photometry?inst=muscat3&date=260716&target=TOI-1410&run=annulus-fixed-20-32
```
