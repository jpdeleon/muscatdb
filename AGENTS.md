## philosophy
* adapt scientific software standards focused on managing extreme complexity while ensuring reproducibility, portability, and performance.
* maintain seamless integration between database, photometry, and transit fitting pipeline
* do not implement critical design choices from assumptions without discussing implications

## Data
* do not delete muscat.db and data/
* always make a daily backup of muscat.db in $HOME/temp. Delete if the backup is stale.
* there are currently five unique instruments: muscat, muscat2, muscat3, muscat4, sinistro
* each instrument has telescope and camera specifications defined in prose2/data/*.telescope files read by prose package
* header keyword should precede over hardcoded parameters keeping in mind that the header keyword may change over time
* muscat and muscat2 has no wcs in header. muscat3, muscat4, sinistro has wcs. muscat4 may have constant wcs offset.
* muscat and muscat2 fits require calibration first before photometry
* muscat3, muscat4, and sinistro has been reduced or calibrated with BANZAI-pipeline
* for BANZAI-reduced fits data, saturation unit is e- when gain is 1 in header
* muscat.db is updated daily via a cronjob
* do not modify fits files directly, store metadata if needed

## paths 
* use uv run for muscat-db
* use conda env prose when running run_photometry
* path: $HOME/miniconda3/envs/prose
* photometry.py depends on run_photometry.py in $HOME/github/research/project/ext_tools/prose2
* transit_fit.py depends on timer package in $HOME/github/research/project/ext_tools/timer
* ttv_fit.py depends on $HOME/github/research/project/ext_tools/harmonic
* do not duplicate functions between muscat-db and prose2. all photometry functions should live in prose2.
* do not use /tmp. use $HOME/temp

## frontend and GUI
* GUI settings should be consistent with the arguments in run_photometry.py
* maintian design consistency across all pages based on styles.css
* Print or display values up to 6 decimal only. The significant figures should depend on the precision of uncertainty if available.
* table column widths cannot be wider than the text length of their row values or column names (e.g. in jobs.html, instrument column should be narrow to fit the content).
* test all GUI elements the same way a user interacts in practice
* ensure all new inputs and checkboxes added to templates (e.g. photometry.html) are registered in the corresponding JavaScript helper arrays 
(collectOptions, restoreOptions, and the default settings listener) so they persist in localStorage across page navigation.
* the jobs are run in a 24-core remote server with 100 Gb memory so queuing heavy jobs should be handled safely
* in the future, the pipeline will use celery and redis across several servers with 48, 120, and 120 cores

## backend and scripts
* the output should be high-quality lightcurves from photometry, and robust inferences from transit fit
* when writing new code, choose correctness over simplicity
* check background process, report any idle or background processes related to muscat-db before running a new one
* all one-off scripts should live in $HOME/temp but useful scripts should be kept in repo
* the server lives inside tmux session named muscatdb-gui
*  The --reload flag only watches Python files, not Jinja2 templates. Remind the user if a restart is needed to see the HTML/JavaScript changes.
If agent restarts the muscat-db by itself, make sure do it inside tmux session muscatdbgui

## optimization
* consider CPU parallelization with a JIT compiler such as Numba, porting the inner loop into Cython, or implementing a CUDA GPU function with Numba or CuPy

## git branch
* keep only main and test branch. PR comes from test branch and only gets merged to main
* before creating a new branch, request permission to delete stale branches

## Photometry job lifecycle
The pipeline is launched with `start_new_session=True` and prose spawns multiprocessing workers (SequenceParallel) that keep appending to the per-target log (`_webrun_<digest>.log`) **after** the tracked parent process has exited. Do not declare a job terminal the instant `job.proc.poll()` returns: `_resolve_job_state` keeps it in a non-terminal `finalizing` state until the log mtime has been quiescent for `_FINALIZE_GRACE_S` (env `MUSCAT_PHOT_FINALIZE_GRACE_S`), so the photometry page's live log keeps streaming the trailing output instead of freezing at parent-exit. `finalizing` is a live-view-only state; `sync_jobs` persists it to the DB as `running` so the Jobs page (which reads state from the DB) stays consistent. Cancelled jobs bypass the grace window and go terminal immediately.

## Testing
* The default suite is fast: `pyproject.toml` sets `addopts = "-m 'not slow'"`, so anything marked `@pytest.mark.slow` is deselected unless you opt in with `pytest -m slow`.
* `tests/test_slow_runs.py` holds heavyweight full-pipeline runtime-profiling runs (real `prose`/`timer`/`harmonic` conda tools + real data on the production host). They `pytest.skip` cleanly when raw data, CSV lightcurves, or the external conda envs are absent, so they collect/skip safely anywhere and only do real work on the host. Run them on the host with `uv run pytest -m slow`.
* Verify transit and visibility from https://exoplanetarchive.ipac.caltech.edu/docs/transit/transit_API.html

## Prompt
* Ask questions for clarifications if prompt is vague or confusing.
* Verify non-obvious assumptions before implementing edit.
