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

