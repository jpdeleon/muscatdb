"""Heavyweight, on-demand runtime-profiling integration tests.

These exercise the *full* MuSCAT pipelines end-to-end against real observation
data and the external conda tools (``prose`` for photometry, ``timer`` for the
transit fit). They are marked ``@pytest.mark.slow`` so the default suite (and
CI) deselect them via ``addopts = -m 'not slow'``; opt in with ``pytest -m slow``.

They serve as runtime-regression signals, not exact-time assertions: each run is
capped under a generous ceiling well above its observed baseline so a large
slowdown trips the test while normal variance does not.

Both tests **skip cleanly** (never error/fail) when the real raw data, the
photometry CSV lightcurves, or the external conda envs are absent — so they are
safe to collect anywhere, and only do real work on the production host.
"""

from __future__ import annotations

import time
from pathlib import Path

import pytest

from muscat_db import photometry as phot
from muscat_db import transit_fit as fit

# Shared dataset: muscat3 / 241222 / WASP-104.
INST = "muscat3"
DATE = "241222"
TARGET = "WASP-104"

# States that mean the job is still in flight (see photometry/transit_fit
# job_status). Anything else is terminal.
_NON_TERMINAL = {"running", "finalizing", "cancelling"}

# Overall wall-clock cap for polling a run to completion.
_POLL_TIMEOUT_S = 1800
_POLL_INTERVAL_S = 5

# Runtime-regression ceilings. Observed photometry baseline ~230s; keep a wide
# margin so this flags a real regression, not normal variance.
_PHOT_ELAPSED_CEILING_S = 600


def _poll_to_terminal(status_fn, *, timeout=_POLL_TIMEOUT_S, interval=_POLL_INTERVAL_S):
    """Poll ``status_fn()`` until it reports a terminal state or *timeout*.

    Returns the final status dict. The caller asserts on its ``state``.
    """
    deadline = time.time() + timeout
    status = status_fn()
    while status.get("state") in _NON_TERMINAL:
        if time.time() >= deadline:
            break
        time.sleep(interval)
        status = status_fn()
    return status


@pytest.mark.slow
def test_full_photometry_run_completes_and_is_fast(record_property):
    """Full photometry reduction for muscat3/241222/WASP-104 finishes ``done``
    within the runtime-regression ceiling."""
    res = phot.start_run(INST, DATE, TARGET, test_run=False)
    if not res.get("ok"):
        # Raw data dir or conda env missing on this host -> nothing to profile.
        pytest.skip(f"photometry run could not start: {res.get('error')}")

    status = _poll_to_terminal(lambda: phot.job_status(INST, DATE, TARGET))

    state = status.get("state")
    assert state not in _NON_TERMINAL, f"run did not reach a terminal state: {state}"
    assert state == "done", (
        f"photometry run ended in {state!r} "
        f"(error_desc={status.get('error_desc')!r})\n{status.get('log', '')[-2000:]}"
    )

    elapsed = status.get("elapsed") or 0
    record_property("photometry_elapsed_s", elapsed)
    print(f"\n[slow] photometry full run elapsed: {elapsed}s "
          f"(ceiling {_PHOT_ELAPSED_CEILING_S}s)")
    assert elapsed < _PHOT_ELAPSED_CEILING_S, (
        f"photometry runtime regression: {elapsed}s >= {_PHOT_ELAPSED_CEILING_S}s ceiling"
    )


@pytest.mark.slow
def test_full_transit_fit_run_completes(record_property):
    """Full transit fit for muscat3/241222/WASP-104 writes ``tune: 1000`` into
    the generated fit.yaml and finishes ``done``."""
    # Guard: the fit consumes photometry CSV lightcurves. Without them there is
    # nothing to fit, so skip rather than fail.
    csvs = fit.get_csv_lightcurves(INST, DATE, TARGET)
    if not csvs:
        pytest.skip("no photometry CSV lightcurves found for muscat3/241222/WASP-104")

    res = fit.start_fit(INST, DATE, TARGET, options={"tune": 1000}, test_run=False)
    if not res.get("ok"):
        # timer conda env missing (or inputs unavailable) on this host.
        pytest.skip(f"transit fit could not start: {res.get('error')}")

    # The requested tune MUST land in the generated fit.yaml; draws/chains/cores
    # stay at their timer defaults.
    import yaml

    fit_yaml = fit.fit_output_dir(INST, DATE, TARGET) / "fit.yaml"
    assert fit_yaml.is_file(), f"fit.yaml not written at {fit_yaml}"
    with open(fit_yaml) as f:
        fit_data = yaml.safe_load(f)
    assert fit_data.get("tune") == 1000, f"expected tune=1000 in fit.yaml, got {fit_data.get('tune')!r}"

    status = _poll_to_terminal(lambda: fit.job_status(INST, DATE, TARGET))

    state = status.get("state")
    assert state not in _NON_TERMINAL, f"fit did not reach a terminal state: {state}"
    assert state == "done", (
        f"transit fit ended in {state!r}\n{status.get('log', '')[-2000:]}"
    )

    elapsed = status.get("elapsed") or 0
    record_property("transit_fit_elapsed_s", elapsed)
    print(f"\n[slow] transit fit full run elapsed: {elapsed}s")
