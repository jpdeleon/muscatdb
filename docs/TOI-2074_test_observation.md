# TOI-2074 Test Observation — Exposure Assessment

Date: 2026-07-12
Target: TOI-2074 (Gaia/Pan-STARRS mags: gp=8.78, rp=9.04, ip=8.54, zs=8.93 — a bright target)
Instrument: MuSCAT3 (OGG, 2m0-01)
Related: commit `6999aa3` (guarded closed-loop test-observation planning flow)

## Background

A manual LCO test observation of TOI-2074 was taken and ingested into `muscat-db`
(`/target?name=TOI2074`) ahead of committing real telescope time. It did **not**
go through the new closed-loop test-observation planner from `6999aa3` — there is
no corresponding row in the `test_observations` table, and that planner's
post-observation analysis (`analyzing` state, `recommendation_json`) is
schema-only and not yet implemented. Assessment here was done manually against
the pipeline's existing diagnostics.

## 1. First test observation (muscat3, 260712, narrowband, 10s, ~0mm defocus)

Four narrowband filters (Na_D, g_narrow, i_narrow, z_narrow), 10 frames each,
10s exposure, essentially in-focus (FOCPOSN ≈ −0.01mm), airmass ≈ 1.09.
Photometry had already run automatically (`phot_status=full`); no transit fit
was attempted (`fit_status=none`), as expected for a short calibration snapshot.

Measured peak counts (`Peak(ADU)` from the photometry CSVs) against each
band's real per-CCD `SATURATE` header value:

| band | measured peak (10s) | SATURATE (header, e⁻) | % of full well | verdict |
|---|---|---|---|---|
| Na_D | 64,193 (max 84,636) | 120,320 | 53–70% | clean |
| g_narrow | 123,558 (max 123,742) | 121,600 | ~102% | marginal |
| i_narrow | 111,095 (max 116,825) | 82,800 | 134% | saturated |
| z_narrow | 133,385 (max 134,244) | 84,000 | 159% | saturated (worst case) |

Confirmed visually: the z_narrow cutout diagnostic shows a flat-topped
(clipped) PSF core matching the CSV's own baked-in peak annotation. Na_D's
cutout shows a normal stellar profile with real margin.

**Root cause:** all four bands used the same shared 10s exposure. MuSCAT3's
four cameras expose simultaneously but support independent per-band exposure
times; a single uniform value doesn't work once the target is bright enough
that Na_D's headroom is g_narrow/i_narrow/z_narrow's overexposure.

## 2. Fix: exposure calculator full-well constants (`src/muscat_db/exposure.py`)

Investigating the saturation surfaced a real bug in `INSTRUMENT_PARAMS`:
`full_well` was a single constant per instrument, but muscat3/muscat4
saturate at meaningfully different levels **per band** (confirmed both by
prose2's `.telescope` files and by live BANZAI headers — this is exactly
what let g_narrow look "fine" under the old flat 99,000 e⁻ constant while
actually saturating).

Applied fix:
- `muscat`/`muscat2`: unchanged — their `.telescope` files agree across all bands.
- `muscat3`/`muscat4`: `full_well` is now a per-band dict (`gp`/`rp`/`ip`/`zs`),
  derived from `.telescope` file `saturation[ADU] × gain[e⁻/ADU]`. New
  `_full_well_gain(instrument, band)` helper resolves it, with narrowband
  filters falling back to their broadband parent.
- `sinistro`: corrected from 100,000 e⁻/gain 1.5 to 246,400 e⁻/gain 1.0
  (median MAXLIN sampled across 9 real Sinistro telescopes at different LCO
  sites — Sinistro has no per-band `.telescope` value since it's
  site/camera-dependent; this remains a per-instrument approximation).
- `calc_peak`, `calc_exptime`, `calc_all_bands` all route through the new
  helper; `calc_all_bands`'s sat_frac-derived target ADU is now computed per
  band instead of once for the whole instrument.
- `templates/exposure.html`'s "Full well" chip now renders per-band for
  muscat3/muscat4.

Full test suite passed after the change (651 passed, 1 skipped, 9 slow
deselected, no regressions).

## 3. Recommended settings for the next test round

### Narrowband @ 5s

Direct linear scaling from the real (unsaturated-regime) measured peaks:

| band | full well (e⁻) | predicted @ 5s | verdict |
|---|---|---|---|
| Na_D | 114,894 | ~28% | safe |
| g_narrow | 113,684 | ~54% | safe |
| i_narrow | 82,001 | ~68–71% (likely underestimate — was clipped at 10s) | probably okay, verify |
| z_narrow | 90,000 | ~74–75% (likely underestimate — was clipped at 10s, worst offender) | watch closely |

i_narrow and z_narrow were already clipped at 10s, so halving their measured
peak is only a lower bound, not a clean extrapolation. **Check z_narrow's
`Peak(ADU)` and cutout as soon as the 5s run lands** — if still elevated,
drop z_narrow alone to ~3s (per-band exposure times don't require
compromising the other three bands).

### Broadband + defocus — retracted initial recommendation

An initial recommendation (0mm→2.6–11.7s, 1mm→6.7–20.8s per band) was
generated after running the exposure calculator's "Calibrate" step for
muscat3 for the first time (612k historical frames → 40 band/focus bins from
1,520 real frames). That recommendation was **wrong** and was withdrawn after
a physical-consistency check: broadband passes far more bandwidth than
narrowband, so it should saturate *faster*, not slower — yet the model
predicted broadband zs could tolerate a *longer* exposure (11.7s) than the
10s that already saturated narrowband z_narrow.

Diagnosis:
- `z_narrow` was never calibrated in the DB, so its prediction falls back to
  the old generic factory coefficient + a naive filter-width-ratio
  narrowband offset. That combination predicts only 38,985 ADU at 10s vs.
  the real measured 133,385 ADU — underestimates by 3.4×.
- Routing through the *newly calibrated* real `zs` coefficient instead (via
  the same offset) predicts an even dimmer result — off by >100×.
- The `zs` calibration coefficient itself is suspect: it averages frames
  from 2021–2026, spanning a physical camera-channel change on the zs filter
  (`ep01` in 2021 → `ep05` from 2022 on, different CCD/SATURATE), and is
  likely dominated by the DB's typically much fainter (mag 10–13) targets,
  which doesn't extrapolate safely to a mag~8.9 target like TOI-2074.

Corrected, evidence-anchored estimate: scaling the real z_narrow flux rate
(133,385 ADU / 10s) by the nominal ~20× bandwidth ratio to broadband zs
(same `ep05` channel) implies broadband zs would hit full well in roughly
**0.2s at zero defocus** — consistent with the *original*, pre-calibration
generic-model estimate (0.2–0.3s), not the "calibrated" 11.7s.

**Current recommendation:** don't trust either coefficient table's absolute
number for this target. Confirm empirically first — take one short, cheap
test frame per broadband filter at a decent defocus (start around **4–6mm**,
~1s exposure) and read the actual `Peak(ADU)` immediately, the same approach
that correctly diagnosed the narrowband problem, before committing to a full
multi-band/multi-repeat sequence.

## Open follow-ups (not yet implemented)

1. **`calibrate_instrument`'s `fwhm_pix` is a hardcoded placeholder** (always
   3.0px, never actually measured from frames) — silently wrong for any
   defocused calibration bin. Affects the calculator's on-page FWHM display
   for muscat3/muscat4 wherever a calibrated bin exists.
2. **Calibration coefficients can be badly wrong for targets far outside the
   sample's typical brightness range**, with no warning surfaced — muscat3's
   `zs` coefficient is off by roughly two orders of magnitude for TOI-2074
   once cross-checked against real data. Also mixes data across a known
   camera-channel change (`ep01`→`ep05`) without accounting for it.
3. Sinistro's full-well constant remains a single cross-site approximation
   (median of a small sample); no per-site source exists to do better without
   querying LCO's live capabilities per telescope.
