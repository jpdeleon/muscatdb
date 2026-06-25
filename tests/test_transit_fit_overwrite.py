from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from muscat_db import transit_fit as fit


class _RunningProcess:
    pid = 12345

    def poll(self):
        return None


@pytest.mark.parametrize("overwrite", ["false", "true"])
def test_start_fit_delegates_overwrite_to_timer_without_deleting_outputs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, overwrite: str
):
    source_csv = tmp_path / "source.csv"
    source_csv.write_text("time,flux\n")

    run_dir = tmp_path / "run"
    output_dir = run_dir / "out"
    output_dir.mkdir(parents=True)
    cached_result = output_dir / "result.pkl"
    cached_result.write_text("cached")
    existing_plot = run_dir / "fit.png"
    existing_plot.write_text("plot")

    monkeypatch.setattr(fit, "fit_output_dir", lambda *_args: run_dir)
    monkeypatch.setattr(fit, "get_csv_lightcurves", lambda *_args: [source_csv])
    monkeypatch.setattr(fit, "_timer_prefix", lambda: ["timer-fit"])
    monkeypatch.setattr(fit.subprocess, "Popen", lambda *_args, **_kwargs: _RunningProcess())
    monkeypatch.setattr(fit, "_FIT_JOBS", {})
    monkeypatch.setattr("muscat_db.database.save_job", lambda **_kwargs: None)

    result = fit.start_fit(
        "muscat3", "250101", "Target", {"overwrite": overwrite}, test_run=True
    )

    try:
        assert result["ok"] is True
        assert cached_result.read_text() == "cached"
        assert existing_plot.read_text() == "plot"
        fit_yaml = yaml.safe_load((run_dir / "fit.yaml").read_text())
        assert fit_yaml["clobber"] is (overwrite == "true")
    finally:
        for job in fit._FIT_JOBS.values():
            job.logf.close()

