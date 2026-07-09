# Database Health Audit

Date: 2026-06-30
Database: `muscat.db`
Audited from: repository root

## Overall Result

`muscat.db` is structurally healthy, but it has several data-health issues that should be addressed.

## Structural Health

- `PRAGMA quick_check` returned `ok`.
- The first integrity attempt hit `database is locked`, but it passed after waiting, consistent with an active writer rather than corruption.
- `journal_mode` is `wal`.
- `freelist_count` is `0`.
- No orphan rows were found in `target_notes` or `target_overrides`.
- No `(instrument, obsdate)` pairs exist in `frames` without corresponding rows in `summaries`.
- The materialized `targets` table is internally consistent with `summaries`:
  - `target_rows = 2224`
  - `recomputed_target_rows = 2224`
  - `orphan_target_rows = 0`
  - `missing_target_rows = 0`

## Runtime State

- DB file size: `1,626,746,880` bytes
- DB mtime: `2026-06-30 14:01:35 +0900`
- `db_meta.last_build_at`: `2026-06-29T17:34:23.970401`
- One live job was present during the audit:
  - `photometry`
  - instrument: `muscat4`
  - obsdate: `250416`
  - target: `TOI-6715`
  - run name: `default`

## Findings

### 1. Duplicate frame rows in `muscat3 / 231111`

`frames` contains `2,086` duplicate `(instrument, obsdate, ccd, filename)` keys, all on `muscat3` `231111`.

Per CCD:

- `ccd 0`: `1298` rows, `649` distinct filenames, duplicate excess `649`
- `ccd 1`: `996` rows, `498` distinct filenames, duplicate excess `498`
- `ccd 2`: `1134` rows, `567` distinct filenames, duplicate excess `567`
- `ccd 3`: `744` rows, `372` distinct filenames, duplicate excess `372`

This inflates `summaries` for that date because the summary counts are derived from `frames`.

### 2. Large number of blank `frames.object` values

Total blank-object frame rows: `542,047 / 10,030,979`

By instrument:

- `muscat`: `1,443` (`0.08%`)
- `muscat2`: `517,491` (`7.03%`)
- `muscat3`: `21,144` (`3.39%`)
- `muscat4`: `1,969` (`1.57%`)
- `sinistro`: `0`

Blank-object rows also appear in `summaries`, reducing target-quality for downstream target discovery and reporting.

### 3. Noncanonical `obsdate` tokens in `summaries`

Total non-`YYMMDD` summary rows: `3,372`

By instrument:

- `muscat`: `1,985`
- `muscat2`: `10`
- `muscat3`: `22`
- `muscat4`: `4`
- `sinistro`: `1,351`

Examples observed:

- `csv_old_220914`
- `csv_old`
- `M_monitoring`
- `Hyades`
- `K2-25_z_2022`
- `K2-25_g_2022`
- `TOI1201_g`
- `TOI2015_r`
- `V1298Tau_monitor_Sinistro`

The application filters many of these in some read paths, but they remain in the underlying database and can affect raw queries and audits.

## Table Counts Snapshot

- `frames`: `10,030,979`
- `summaries`: `48,591`
- `targets`: `2,224`
- `jobs`: `168`
- `target_notes`: `0`
- `target_overrides`: `0`
- `db_meta`: `1`
- `exposure_coeffs`: `0`
- `exposure_jobs`: `0`
- `ephemeris_views`: `53`

## Instrument Summary Snapshot

- `muscat`: `431` dates, `1,775,547` frames, `9,276` summary rows
- `muscat2`: `1,247` dates, `7,357,521` frames, `30,219` summary rows
- `muscat3`: `402` dates, `624,331` frames, `3,077` summary rows
- `muscat4`: `69` dates, `125,404` frames, `354` summary rows
- `sinistro`: `503` dates, `148,176` frames, `5,665` summary rows

## Recommended Next Steps

1. Root-cause the duplicate ingest for `muscat3 / 231111`.
2. Trace where blank `OBJECT` values enter the obslog-to-DB pipeline, especially for `muscat2`.
3. Decide whether noncanonical `obsdate` tokens should be excluded during ingest or normalized into metadata rather than primary date fields.
