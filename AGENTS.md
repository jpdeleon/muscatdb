## use uv for muscat-db
uv run

## Do not delete muscat.db, data/

## use conda env prose when running run_photometry
path: /ut2/jerome/miniconda3/envs/prose

## make sure GUI settings are consistent with the arguments in run_photometry 

## update readme if needed before comitting

## check background process
report similar background processes related to muscat-db before running a new one
report any idle processes

## GUI Persistence
Ensure all new inputs and checkboxes added to templates (e.g. photometry.html) are registered in the corresponding JavaScript helper arrays (collectOptions, restoreOptions, and the default settings listener) so they persist in localStorage across page navigation.

## Photometry job lifecycle
The pipeline is launched with `start_new_session=True` and prose spawns multiprocessing workers (SequenceParallel) that keep appending to the per-target log (`_webrun_<digest>.log`) **after** the tracked parent process has exited. Do not declare a job terminal the instant `job.proc.poll()` returns: `_resolve_job_state` keeps it in a non-terminal `finalizing` state until the log mtime has been quiescent for `_FINALIZE_GRACE_S` (env `MUSCAT_PHOT_FINALIZE_GRACE_S`), so the photometry page's live log keeps streaming the trailing output instead of freezing at parent-exit. `finalizing` is a live-view-only state; `sync_jobs` persists it to the DB as `running` so the Jobs page (which reads state from the DB) stays consistent. Cancelled jobs bypass the grace window and go terminal immediately.

