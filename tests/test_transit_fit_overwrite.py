from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from muscat_db import transit_fit as fit


class _RunningProcess:
    pid = 12345

    def poll(self):
        return None


class _NoQueueStore:
    def enqueue(self, **_kwargs):
        pytest.fail("same-key running fit must be reused, not queued")


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


def test_start_fit_reuses_same_key_running_job_before_capacity_queue(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    source_csv = tmp_path / "Target_muscat3_gp_250101.csv"
    source_csv.write_text("time,flux\n")
    run_dir = tmp_path / "fit-run"
    log_path = tmp_path / "running.log"
    logf = log_path.open("w")

    run_id = fit.build_run_id("", "", "")
    key = fit.fit_job_key("muscat3", "250101", "Target", run_id)
    monkeypatch.setattr(fit, "fit_output_dir", lambda *_args: run_dir)
    monkeypatch.setattr(fit, "get_csv_lightcurves", lambda *_args: [source_csv])
    monkeypatch.setattr(fit, "_count_running_full", lambda: 1)
    monkeypatch.setattr(fit, "get_job_store", lambda: _NoQueueStore())
    monkeypatch.setattr(fit, "_FIT_JOBS", {
        key: fit.TransitFitJob(
            key=key,
            inst="muscat3",
            date="250101",
            target="Target",
            cmd=["timer-fit"],
            proc=_RunningProcess(),
            logf=logf,
            log_path=log_path,
            run_type="full",
            run_id=run_id,
            run_name="default",
        )
    })

    try:
        result = fit.start_fit("muscat3", "250101", "Target", {}, test_run=False)
        assert result == {
            "ok": True,
            "key": key,
            "already_running": True,
            "run_id": run_id,
        }
        assert not run_dir.exists()
    finally:
        logf.close()
        fit._FIT_JOBS.clear()
