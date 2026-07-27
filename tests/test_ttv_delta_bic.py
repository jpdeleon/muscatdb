"""ΔBIC surfacing for saved harmonic TTV runs.

harmonic compares the fitted TTV model against a linear ephemeris and persists
the result to ``fit_stats.json``. muscat-db reads it to tell the observer whether
a TTV model is justified at all, which is advisory input to scheduling.

Two properties matter and are easy to get wrong:

* the file is **optional** -- harmonic writes it only when it actually samples,
  so a run resumed without ``--clobber``, or one predating the feature, has none;
* it can contain a bare ``NaN`` (harmonic uses ``json.dump`` defaults and
  ``tau_max`` is not always finite). Python parses that, but it is not valid
  JSON, so passing it through unsanitised breaks ``JSON.parse`` in the browser
  for the *entire* response, not just that field.
"""

from __future__ import annotations

import json

import pytest

from muscat_db import ttv_fit as ttv

_COMPLETE_RUN = ("samples.csv.gz", "data.csv", "config.ini", "fit_config.json")

_STATS = {
    "delta_bic": 680.48,
    "evidence": "very strong",
    "chi2_lin": 846.41,
    "chi2_harm": 134.47,
    "k_lin": 6,
    "k_harm": 14,
    "n_data": 51,
}


def _run_dir(tmp_path, target="HIP67522", run_name="default", complete=True):
    d = tmp_path / target / "_runs" / run_name
    d.mkdir(parents=True)
    if complete:
        for name in _COMPLETE_RUN:
            (d / name).write_text("x")
    return d


# ---------------------------------------------------------------------------
# read_fit_stats
# ---------------------------------------------------------------------------
def test_absent_fit_stats_is_not_an_error(tmp_path):
    assert ttv.read_fit_stats(_run_dir(tmp_path)) is None


def test_stats_are_read_back(tmp_path):
    d = _run_dir(tmp_path)
    (d / "fit_stats.json").write_text(json.dumps(_STATS))
    assert ttv.read_fit_stats(d) == _STATS


def test_non_finite_values_are_sanitised_to_none(tmp_path):
    """A bare NaN must not reach the browser.

    Asserted with allow_nan=False, which is what a strict parser does: the
    default json.dumps would happily re-emit NaN and hide the bug.
    """
    d = _run_dir(tmp_path)
    (d / "fit_stats.json").write_text(
        '{"delta_bic": 680.48, "evidence": "very strong", "chi2_lin": 846.41,'
        ' "chi2_harm": 134.47, "k_lin": 6, "k_harm": 14, "n_data": 51,'
        ' "tau_max": NaN, "converged": false}'
    )
    stats = ttv.read_fit_stats(d)
    assert stats["tau_max"] is None
    assert stats["delta_bic"] == 680.48        # the real values survive
    json.dumps(stats, allow_nan=False)          # raises if any NaN leaked


def test_partial_file_reads_as_absent(tmp_path):
    """Mirrors harmonic, which treats a file missing any ΔBIC key as absent."""
    d = _run_dir(tmp_path)
    (d / "fit_stats.json").write_text('{"delta_bic": 1.0}')
    assert ttv.read_fit_stats(d) is None


def test_corrupt_file_reads_as_absent(tmp_path):
    d = _run_dir(tmp_path)
    (d / "fit_stats.json").write_text("not json at all")
    assert ttv.read_fit_stats(d) is None


# ---------------------------------------------------------------------------
# outputs integration
# ---------------------------------------------------------------------------
def test_outputs_expose_stats_and_list_the_file(tmp_path, monkeypatch):
    monkeypatch.setenv("MUSCAT_TTV_DIR", str(tmp_path))
    d = _run_dir(tmp_path)
    (d / "fit_stats.json").write_text(json.dumps(_STATS))

    outputs = ttv.get_ttv_outputs("HIP 67522", "default")
    assert outputs["fit_stats"]["evidence"] == "very strong"
    assert "fit_stats.json" in outputs["extra_files"]


def test_outputs_report_none_when_absent(tmp_path, monkeypatch):
    monkeypatch.setenv("MUSCAT_TTV_DIR", str(tmp_path))
    _run_dir(tmp_path)
    assert ttv.get_ttv_outputs("HIP 67522", "default")["fit_stats"] is None


# ---------------------------------------------------------------------------
# compute_delta_bic
# ---------------------------------------------------------------------------
def test_stored_stats_are_returned_without_running_harmonic(tmp_path, monkeypatch):
    """The common case must not pay for a subprocess."""
    monkeypatch.setenv("MUSCAT_TTV_DIR", str(tmp_path))
    d = _run_dir(tmp_path)
    (d / "fit_stats.json").write_text(json.dumps(_STATS))
    monkeypatch.setattr(
        ttv.subprocess, "run",
        lambda *a, **k: pytest.fail("harmonic must not run when stats are stored"),
    )

    result = ttv.compute_delta_bic("HIP 67522", "default")
    assert result["ok"] is True
    assert result["recomputed"] is False
    assert result["stats"]["delta_bic"] == 680.48


def test_incomplete_run_refuses_before_spawning_harmonic(tmp_path, monkeypatch):
    monkeypatch.setenv("MUSCAT_TTV_DIR", str(tmp_path))
    _run_dir(tmp_path, complete=False)
    monkeypatch.setattr(
        ttv.subprocess, "run",
        lambda *a, **k: pytest.fail("must not run harmonic for an incomplete run"),
    )
    result = ttv.compute_delta_bic("HIP 67522", "default")
    assert result["ok"] is False
    assert "complete TTV model output" in result["error"]


def test_invalid_target_is_rejected(tmp_path, monkeypatch):
    monkeypatch.setenv("MUSCAT_TTV_DIR", str(tmp_path))
    assert ttv.compute_delta_bic("../../etc")["error"] == "invalid target"


def test_recompute_parses_helper_output(tmp_path, monkeypatch):
    """No stored stats: harmonic is invoked and its JSON is returned."""
    monkeypatch.setenv("MUSCAT_TTV_DIR", str(tmp_path))
    _run_dir(tmp_path)
    monkeypatch.setattr(ttv, "_conda_env_python", lambda env: "/fake/python")

    class _Done:
        returncode = 0
        stdout = json.dumps(_STATS)
        stderr = ""

    monkeypatch.setattr(ttv.subprocess, "run", lambda *a, **k: _Done())
    ttv._ttv_model_cache.clear()

    result = ttv.compute_delta_bic("HIP 67522", "default")
    assert result["ok"] is True
    assert result["recomputed"] is True
    assert result["stats"]["evidence"] == "very strong"


def test_recompute_failure_does_not_leak_details(tmp_path, monkeypatch):
    """A harmonic traceback must stay in the log, not reach the browser."""
    monkeypatch.setenv("MUSCAT_TTV_DIR", str(tmp_path))
    _run_dir(tmp_path)
    monkeypatch.setattr(ttv, "_conda_env_python", lambda env: "/fake/python")

    class _Failed:
        returncode = 1
        stdout = ""
        stderr = 'Traceback...\nPredictionError: /secret/path/samples.csv.gz missing'

    monkeypatch.setattr(ttv.subprocess, "run", lambda *a, **k: _Failed())
    ttv._ttv_model_cache.clear()

    result = ttv.compute_delta_bic("HIP 67522", "default")
    assert result["ok"] is False
    assert result["error"] == "delta-BIC computation failed"
    assert "secret" not in json.dumps(result)


def test_recompute_rejects_malformed_helper_output(tmp_path, monkeypatch):
    monkeypatch.setenv("MUSCAT_TTV_DIR", str(tmp_path))
    _run_dir(tmp_path)
    monkeypatch.setattr(ttv, "_conda_env_python", lambda env: "/fake/python")

    class _Done:
        returncode = 0
        stdout = json.dumps({"delta_bic": 1.0})   # missing the other keys
        stderr = ""

    monkeypatch.setattr(ttv.subprocess, "run", lambda *a, **k: _Done())
    ttv._ttv_model_cache.clear()

    assert ttv.compute_delta_bic("HIP 67522", "default")["ok"] is False
