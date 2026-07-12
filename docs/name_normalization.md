# Target name normalization

## Purpose and scope

muscat-db preserves the target name supplied by an observation (normally the
FITS `OBJECT` value) as the raw name. It also derives a normalized name for
comparisons. Normalization is an identity-matching operation; it must not
rename targets in `muscat.db`, rewrite FITS headers, rename data directories,
or replace the spelling shown as the source name.

The comparison key is currently produced by `_normalize_target_name` in
`src/muscat_db/web.py`. It is used to group raw target spellings in target and
dataset views and by several catalog, ephemeris, job, and archive lookups.

## Current rule

The current implementation applies these operations in order to every target
name:

1. Strip leading and trailing whitespace.
2. Convert letters to uppercase.
3. Remove every space, hyphen, and underscore.
4. Remove a trailing decimal component matching `.digits`.
5. If the remaining name ends in `B`, `C`, `D`, `E`, `F`, `G`, or `H`, remove
   that final letter.

Examples:

| Raw name | Current comparison key |
|---|---|
| `TOI-6109` | `TOI6109` |
| `TOI06109.01` | `TOI06109` |
| `TOI06109.02` | `TOI06109` |
| `TOI-2457.01` | `TOI2457` |
| `TOI-1730c` | `TOI1730` |
| `HIP 67522` | `HIP67522` |
| `V1298Tau_b` | `V1298TAU` |

Consequently, candidate suffixes such as `.01` and `.02` and confirmed-planet
letters such as `b` and `c` are intentionally grouped at host level. Leading
zeros in the integer part of a TOI number are not currently removed, so padded
and unpadded TOI spellings can remain in separate groups.

## Observed database implication

A read-only audit of `muscat.db` on 2026-07-11 found:

- 2,300 distinct raw target names;
- 1,190 names matching a recognizable TOI spelling;
- 959 raw names whose comparison key would change when TOI integer padding is
  removed with the strict planned rule; and
- 99 existing TOI host groups that would be consolidated across the current
  padded/unpadded boundary.

For TOI-6109, the current database contains:

| Raw name | Instruments | Dates | Frames |
|---|---|---:|---:|
| `TOI-6109` | muscat3, sinistro | 59 | 9,631 |
| `TOI06109.01` | muscat2 | 1 | 2,780 |
| `TOI06109.02` | muscat2 | 4 | 37,106 |

The current rule creates two comparison groups, `TOI6109` and `TOI06109`.
The planned rule creates the single host key `TOI6109`, covering 49,517 frames
from all three raw names. This affects grouping and lookup behavior only; the
raw records remain distinct and unchanged.

## TOI rule

For a syntactically recognized TOI name, the internal host comparison key will
use the integer value without leading zeros:

| Raw name | Planned comparison key |
|---|---|
| `TOI6109` | `TOI6109` |
| `TOI-6109` | `TOI6109` |
| `TOI06109.01` | `TOI6109` |
| `TOI06109.02` | `TOI6109` |
| `TOI 06109 b` | `TOI6109` |

The TOI rule must parse the structured raw spelling before the generic removal
of punctuation. The intended recognition is equivalent to:

```python
raw = target.strip().upper()
m = re.fullmatch(
    r"TOI(?:[ _-]*)0*(\d+)(?:\.\d+)?(?:\s*[B-H])?",
    raw,
)
if m:
    return f"TOI{int(m.group(1))}"
```

If the raw name does not match this grammar, normalization falls back to the
existing generic rule. This constraint is important: parsing after blindly
removing punctuation could misinterpret a malformed value such as
`TOI06209-01` as TOI 620901.

This rule is implemented in `_normalize_target_name` and covered by regression
tests in `tests/test_photometry.py`.

## Canonical display and integration spellings

- Internal host comparison key: `TOI6109`.
- Preferred human-facing/catalog spelling: `TOI-6109` (and a candidate such as
  `TOI-6109.01` when candidate identity matters).
- Raw database and FITS spelling: preserve exactly; do not rewrite it.
- MuSCAT2 wiki integration: continue producing a five-digit padded identifier,
  such as `TOI06109`, because `_wiki_url` explicitly requires that external
  convention.

Normalization therefore does not establish one filename format for every
instrument. Formatting required by a particular external system belongs at
that integration boundary.

## Validation requirements

The code update should include regression tests demonstrating that:

1. `TOI-6109`, `TOI6109`, `TOI06109.01`, and `TOI06109.02` share `TOI6109`.
2. Existing host-level candidate and planet-letter grouping remains intact.
3. Non-TOI normalization behavior is unchanged.
4. Malformed or annotated names such as `TOI06209-01`, `TOI2106.01--exp0`,
   and `TOI3915TRACK` are not reinterpreted as different TOI numbers.
5. Target pages, catalog matching, ephemeris lookup, and job filtering resolve
   padded and unpadded TOI observations consistently.
6. No database rows, FITS files, or data paths are renamed or deleted.
