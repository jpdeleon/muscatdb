# Avoiding the target source in photometry

## Incident

The Sinistro reduction for `TIC 89071445` on `250806` was launched with
`--avoid_cids 3 9 13`.  The WCS match subsequently identified the target as
source index `3`.

`avoid_cids` is intended to remove *comparison* sources.  In the automatic
comparison path, however, it was applied directly to the all-star mask.  This
also removed source `3`, the target.  `Fluxes.mask_stars(...,
keep_indexing=True)` replaces excluded fluxes and errors with `-1`; after
differential normalization, the target's error became approximately
`-1 / mean(-1) = 1`.  The exported light curve therefore contained
`Err ~= 1.000005` (100% relative uncertainty), and transit fitting correctly
returned a prior-dominated posterior with very large parameter error bars.

## Verified control

On the same data and with the same reduction settings:

| Avoided source IDs | Median target relative uncertainty |
| --- | ---: |
| `3, 9, 13` | `1.000004` |
| `9, 13` | `0.002893` |

The pre-differential aperture uncertainty is healthy (about `0.0022` on the
reference exposure).  The failure is therefore a target-mask validation bug,
not a detector-noise issue.

## Required invariant

After target identification, `target_index` must not occur in `avoid_cids`.
The photometry runner must raise a clear error before masking or exporting any
product.  This check is required for both automatic and explicit comparison
selection, including when the target is inferred from WCS rather than supplied
as `--tID`.
