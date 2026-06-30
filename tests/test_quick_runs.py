"""Quick ``test_run=True`` photometry smoke-tests on real data from muscat.db.

Each test starts a limited-frame reduction (default 10 frames per band) for one
representative observation per instrument. They verify the pipeline launches,
runs to completion, and reports ``done`` without errors.

Because they depend on real FITS data on disk and the external ``prose`` conda
environment, they **skip cleanly** when either is absent — safe to collect
anywhere, real work only on the production host.
"""

from __future__ import annotations

import time

import pytest

from muscat_db import photometry as phot


# States that mean the job is still in flight.
_NON_TERMINAL = {"running", "finalizing", "cancelling"}

# test_run=True should complete in << 60 s per instrument; 300 s is a generous
# safety net for heavily loaded hosts.
_POLL_TIMEOUT_S = 300
_POLL_INTERVAL_S = 2


# One representative real-data combination per instrument, discovered from
# muscat.db and verified to have raw FITS on disk.
_INST_DATES_TARGETS = [
    pytest.param("muscat",   "260126", "TOI-1730c",   id="muscat"),
    pytest.param("muscat2",  "260613", "HD143317",    id="muscat2"),
    pytest.param("muscat3",  "260613", "TOI-1252",    id="muscat3"),
    pytest.param("muscat4",  "260512", "TOI-6557",    id="muscat4"),
    pytest.param("sinistro", "260624", "TIC88297141", id="sinistro"),
]


def _poll_to_terminal(status_fn, *, timeout=_POLL_TIMEOUT_S, interval=_POLL_INTERVAL_S):
    deadline = time.time() + timeout
    status = status_fn()
    while status.get("state") in _NON_TERMINAL:
        if time.time() >= deadline:
            break
        time.sleep(interval)
        status = status_fn()
    return status


@pytest.mark.slow
@pytest.mark.parametrize("inst,date,target", _INST_DATES_TARGETS)
def test_photometry_test_run_completes(inst, date, target):
    res = phot.start_run(inst, date, target, test_run=True)
    if not res.get("ok"):
        pytest.skip(f"{inst}/{date}/{target} could not start: {res.get('error')}")

    run_id = res.get("run_id") or ""
    status = _poll_to_terminal(lambda: phot.job_status(inst, date, target, run_id))

    state = status.get("state")
    assert state not in _NON_TERMINAL, (
        f"{inst}/{date}/{target} did not reach terminal: {state}"
    )
    assert state == "done", (
        f"{inst}/{date}/{target} ended in {state!r} "
        f"(error_desc={status.get('error_desc')!r})\n{status.get('log', '')[-2000:]}"
    )
