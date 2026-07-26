"""MUSCAT_MAX_FULL_JOBS gates full runs across all three pipelines.

A secondary instance (staging) sets 0 so it can never compete with production for
the shared host. The interesting case is 0: no slot can ever be claimed, so a
pipeline that fell through to its queue would leave the job pending forever with
nothing able to drain it. Every pipeline must refuse outright instead.

photometry's behavioural case lives in test_photometry.py next to the test-run cap.
"""

from __future__ import annotations

import importlib
import pathlib

import pytest

from muscat_db import photometry, transit_fit, ttv_fit


@pytest.mark.parametrize("module", [photometry, transit_fit, ttv_fit])
def test_cap_is_env_tunable(module, monkeypatch):
    """Previously hardcoded to 1 in all three, so staging could not be limited."""
    monkeypatch.setenv("MUSCAT_MAX_FULL_JOBS", "0")
    reloaded = importlib.reload(module)
    try:
        assert reloaded._MAX_FULL_JOBS == 0
    finally:
        monkeypatch.delenv("MUSCAT_MAX_FULL_JOBS", raising=False)
        importlib.reload(module)  # restore the default for later tests


@pytest.mark.parametrize("module", [photometry, transit_fit, ttv_fit])
def test_cap_defaults_to_one(module, monkeypatch):
    monkeypatch.delenv("MUSCAT_MAX_FULL_JOBS", raising=False)
    assert importlib.reload(module)._MAX_FULL_JOBS == 1


def test_transit_fit_refuses_full_run_when_disabled(monkeypatch):
    def must_not_reach_store():
        pytest.fail("a disabled full fit must be refused before touching the job store")

    monkeypatch.setattr(transit_fit, "_MAX_FULL_JOBS", 0)
    monkeypatch.setattr(transit_fit, "get_job_store", must_not_reach_store)
    monkeypatch.setattr(transit_fit, "_fit_reduction_exists", lambda *a, **k: False)
    # Get past the input checks that run before the capacity gate.
    monkeypatch.setattr(transit_fit, "validate_fit_options", lambda options: None)
    monkeypatch.setattr(
        transit_fit, "get_csv_lightcurves",
        lambda *a, **k: [pathlib.Path("TOI-123_muscat3_g.csv")],
    )
    monkeypatch.setattr(transit_fit, "validate_no_duplicate_datasets", lambda *a, **k: None)

    result = transit_fit.start_fit(
        "muscat3", "260720", "TOI-123",
        options={"run_mode": "new"}, selected_csvs=None, test_run=False,
    )
    assert result["ok"] is False
    assert "MUSCAT_MAX_FULL_JOBS=0" in result["error"]


def test_ttv_fit_refuses_when_disabled(monkeypatch):
    """Every ttv_fit job is 'full', so a zero cap disables the pipeline outright."""

    def must_not_reach_store():
        pytest.fail("a disabled ttv fit must be refused before touching the job store")

    monkeypatch.setattr(ttv_fit, "_MAX_FULL_JOBS", 0)
    monkeypatch.setattr(ttv_fit, "get_job_store", must_not_reach_store)
    monkeypatch.setattr(ttv_fit, "validate_ttv_options", lambda options: None)

    result = ttv_fit.start_ttv_fit("TOI-123", options={"run_name": "r1"})
    assert result["ok"] is False
    assert "MUSCAT_MAX_FULL_JOBS=0" in result["error"]
