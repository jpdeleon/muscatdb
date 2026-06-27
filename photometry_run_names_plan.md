# Add Separate Photometry Run Names

## Summary

Add transit-fit-style named photometry runs to `photometry.html`: users can enter a run name, launch separate reductions, switch between stored runs, and view/delete/cancel/status-check the selected run without overwriting other runs. Existing flat photometry outputs remain visible as a `legacy` run.

## Key Changes

- Add shared run-id helpers for `run_name -> slug` and `site/mode/run_name -> run_id`; reuse the same behavior as transit-fit:
  - blank run name becomes `default`
  - sinistro run IDs include site and only non-default mode, e.g. `lsc-default` for `central_2k_2x2` and `lsc-full_frame-default` for `full_frame`
  - non-sinistro run IDs use just the run-name slug, e.g. `default`
- Store new photometry runs under an isolated directory inside the existing result tree:
  - legacy outputs remain in `results_dir(inst, date)`
  - new outputs go to `results_dir(inst, date)/_runs/{target_clean}/{run_id}`
  - prose still receives only `--results_dir`; no prose2 photometry logic is duplicated.
- Extend photometry backend APIs to accept `run_id`:
  - `/photometry?run=...`
  - `/photometry/status?run=...`
  - `/photometry/run` response includes `run_id`
  - `/photometry/cancel`, `/photometry/delete`, `/photometry/command`, and job rerun preserve run identity
  - add run-scoped file route like `/photometry/file/{inst}/{date}/{target}/run/{run_id}/{name}` while keeping the existing legacy file route.
- Update job tracking:
  - include `run_id` in photometry `job_key`, DB `save_job`, pending queue, persisted status, sync, cancel, delete, log path, and Jobs page links/log buttons
  - use a run-scoped `_webrun_<digest>.log` inside the selected run directory.
- Update `photometry.html`:
  - add `Run name` text input in the run panel, defaulting to `default`
  - register it in `collectOptions`, `restoreOptions`, and default-settings reset so localStorage works
  - show run selector chips when more than one run exists, including `legacy`
  - show selected run in the results header, similar to `Transit Fit Results - {sel_run}`
  - make all artifact, CSV, log, ref-header, and NPZ links use the selected run's file base.
- Preserve current site/mode behavior:
  - site/mode filters still select products inside a run
  - sinistro still requires an explicit site when obslog has multiple sites
  - legacy flat outputs remain filtered by current `site`/`mode` query params.

## Test Plan

- Unit tests for run-id generation, run output directory resolution, traversal rejection, and legacy-vs-run-scoped artifact serving.
- `list_outputs` tests covering:
  - legacy flat outputs still render as `legacy`
  - named run outputs are discovered from `_runs/{target}/{run_id}`
  - multiple runs sort newest-first and selected run controls displayed artifacts
  - sinistro site/mode filtering still works inside a named run.
- Route/template tests for:
  - `/photometry?...&run=...` renders selected run name in results header
  - run chips appear for multiple runs
  - artifact URLs include `/run/{run_id}/...`
  - run-name input persists through JS option arrays.
- Job lifecycle tests for start/status/cancel/delete/pending/sync with distinct `run_id`s.
- Run `uv run pytest`.

## Assumptions

- Default blank run name is `default`.
- Existing flat files are not moved; they appear as `legacy`.
- Delete removes only the selected run; deleting `legacy` removes only the existing flat target products.
- New named photometry runs are isolated by output directory, not by changing prose output filenames.
