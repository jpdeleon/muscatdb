## TODO
* File a celerite2 upstream issue/PR: `celerite2/pymc/ops.py` (v0.3.2, latest) uses `import pkg_resources` / `pkg_resources.resource_filename("celerite2", "definitions.json")`, which breaks under setuptools>=81 (pkg_resources removed) with "No module named 'pkg_resources'". Proposed fix: replace with `importlib.resources.files("celerite2").joinpath("definitions.json")`. Repo: https://github.com/exoplanet-dev/celerite2 — also check `celerite2/jax/ops.py` and `celerite2/pymc3/ops.py`. Workaround currently in place: pinned `setuptools<81` in ext_tools/timer/pyproject.toml.

## Later

Normalize target names without destroying originals: instead of the in-place `UPDATE frames SET object` in `scripts/fix_malformed_names.py` (Stage 3), add a separate normalized-name column and keep the raw FITS `object` as the source of truth. Decide whether the normalized name is (a) canonical identity used for grouping/joins so duplicate spellings merge into one target (requires migrating `target_notes`/`target_overrides` keys), or (b) display/search-only alias (no migration, but `55Cnc` and `55 Cnc` stay separate target rows). Make normalization a pure function applied during `build_db()` ingestion (not a post-hoc UPDATE) so derived `summaries`/`targets` tables can't go stale.

## Done
* In GUI Transit Fit page, add an url to https://exoplanetarchive.ipac.caltech.edu/overview/<target_name> that opens a new browser tab when clicked.
* In GUI Jobs page, persist a table of jobs whether finished or still at queue. Just update the status but keep the history record.
* If job failed, add a short description on the jobs page.
* In muscat-db homepage, add a suffix i.e. YYMMDD(Mn) where n is 1 is the date is under muscat1, 2 if muscat2, etc. or YYMMDD(S) if observed with Sinistro. Add the (Mn), (S) suffix to all the dates in the Date column
* Add a new favicon for transit fit in dates column beside the telescope favicon that leads to the transit page for that target.
* Add GUI photometry page, add a button named "To Transit Fit" to go to Transit Fit page for this target. The url is identical to the favicon in the muscat-db table.
* Add persistence to GUI Transit Fit page, similar to GUI photometry page.
* In GUI Jobs page, Create two tables to summarize jobs: one is for photometry and another for transit fit.
* In GUI transit fit, change the output dir from /ut2/jerome/ql/prose/<inst>/<date>/transit_fit_<target> to /ut2/jerome/ql/timer/<isnt>/<date>/<target>
* In timer, add a new argument called --test_run similar to run_photometry scripts' to run preliminary fits given short run time.
* Add a test to check solve wcs worked on a target that is observed with either muscat or msucat2 and compare with muscat3 with correct headers.
* In GUI photometry page, add question mark icon to show useful help or tips when mouse hover.
* For muscat and muscat2 inst, show also the master_*.png in GUI photometry page.
* Add progress bar in calibrate_muscat*.py
* In GUI photometry page, add a "use defaults" button pipeline options section.
* In muscat-db table in the home page, add new table column called Phot placed after Dates column which should indicate a check or X mark if full photometry outputs exists or no output exists (or only ran using test_run).
* The muscat-db and Logs page are identical. Separate them into two different page. The muscat-db table should only be in the MuSCAT-db homepage. Move the link for the five Instruments i.e. muscat, muscat2, muscat4, muscat4, and sinistro to the Logs Page.
* In Logs page, add a summary of data for each instruments below the Instruments section.
* Add a new boilerplate page called "Transit Fit" for that will host transit fitting code in the future. Add a link in the navbar after "Photometry".
* Add a new page called Jobs. Add a table that tallies the job queue with deep links and their status e.g. Done, Failed, Pending, etc. Add a link in the navbar after "Transit Fit".
* Fix the status bar in Photometry page. It sometimes show up and sometimes disappears when navigating to different pages.
