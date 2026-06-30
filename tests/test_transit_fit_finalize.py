"""Regression test for the transit-fit live-log freeze (architecture audit C1).

Before C1, transit-fit declared a job terminal the instant ``proc.poll()``
returned, with none of photometry's finalizing semantics — so the transit-fit
page's live log could freeze mid-output while timer's workers were still writing.
After porting transit-fit onto the shared ``muscat_db.jobs`` runner, a finished
parent stays ``finalizing`` until its log goes quiescent, exactly like
photometry. These tests lock that behaviour in.
"""

import os
import time

from muscat_db import transit_fit as fit

INST = "muscat4"
DATE = "260101"
TARGET = "HIP67522"


class _FakeProc:
    def __init__(self, rc=None):
        self._rc = rc
        self.pid = os.getpid()

    def poll(self):
        return self._rc

    def wait(self, timeout=None):
        return self._rc


def _make_job(tmp_path):
    with fit._FIT_LOCK:
        fit._FIT_JOBS.clear()
    log = tmp_path / "timer-fit.log"
    log.write_text("$ timer-fit\nINFO: started\n")
    proc = _FakeProc(rc=None)
    key = fit.fit_job_key(INST, DATE, TARGET)
    job = fit.TransitFitJob(
        key=key, inst=INST, date=DATE, target=TARGET,
        cmd=["x"], proc=proc, logf=open(log, "a"),
        log_path=log, run_type="full",
    )
    with fit._FIT_LOCK:
        fit._FIT_JOBS[key] = job
    return job, proc, log


class TestTransitFitFinalizeGrace:
    def test_stays_finalizing_while_log_grows_then_terminal(self, monkeypatch, tmp_path):
        monkeypatch.setattr(fit, "_FINALIZE_GRACE_S", 1)
        _job, proc, log = _make_job(tmp_path)
        try:
            # Parent still alive -> running.
            assert fit.job_status(INST, DATE, TARGET)["state"] == "running"

            # Parent exits 0 but a worker just appended -> finalizing, not done,
            # and the freshly written line is visible in the live log.
            proc._rc = 0
            with open(log, "a") as f:
                f.write("INFO: writing corner plot\n")
            s = fit.job_status(INST, DATE, TARGET)
            assert s["state"] == "finalizing"
            assert "corner plot" in s["log"]

            # Log goes quiescent past the grace window -> terminal done.
            time.sleep(1.2)
            assert fit.job_status(INST, DATE, TARGET)["state"] == "done"
        finally:
            with fit._FIT_LOCK:
                fit._FIT_JOBS.clear()

    def test_terminal_marker_shortens_finalize_window(self, monkeypatch, tmp_path):
        monkeypatch.setattr(fit, "_FINALIZE_GRACE_S", 600)
        monkeypatch.setattr(fit, "_FINALIZE_GRACE_TERMINAL_S", 1)
        _job, proc, log = _make_job(tmp_path)
        try:
            proc._rc = 0
            with open(log, "a") as f:
                f.write("INFO: Timer-fit completed successfully\n")
            # Freshly written -> still finalizing.
            assert fit.job_status(INST, DATE, TARGET)["state"] == "finalizing"
            # Past the short terminal window -> done despite the 600s default.
            time.sleep(1.2)
            assert fit.job_status(INST, DATE, TARGET)["state"] == "done"
        finally:
            with fit._FIT_LOCK:
                fit._FIT_JOBS.clear()

    def test_cancelled_job_finalizes_immediately(self, monkeypatch, tmp_path):
        monkeypatch.setattr(fit, "_FINALIZE_GRACE_S", 600)
        job, proc, log = _make_job(tmp_path)
        try:
            job.cancelled = True
            proc._rc = -15
            with open(log, "a") as f:
                f.write("INFO: still writing during cancel\n")
            assert fit.job_status(INST, DATE, TARGET)["state"] == "cancelled"
        finally:
            with fit._FIT_LOCK:
                fit._FIT_JOBS.clear()

    def test_sync_jobs_persists_finalizing_as_running(self, monkeypatch, tmp_path):
        """While finalizing, sync_jobs must persist the DB row as 'running' so the
        Jobs page (which reads state from the DB) stays consistent with the
        transit-fit page instead of flipping to a terminal state early."""
        monkeypatch.setattr(fit, "_FINALIZE_GRACE_S", 600)
        _job, proc, log = _make_job(tmp_path)
        proc._rc = 0
        with open(log, "a") as f:
            f.write("INFO: wrote trace\n")  # fresh mtime -> finalizing
        saved: list[dict] = []
        monkeypatch.setattr("muscat_db.database.get_persisted_jobs", lambda: [])
        monkeypatch.setattr("muscat_db.database.save_job", lambda **kw: saved.append(kw))
        try:
            fit.sync_jobs()
            fit_saves = [s for s in saved if s.get("target") == TARGET]
            assert fit_saves, "expected the finalizing job to be persisted"
            assert fit_saves[-1]["state"] == "running"
            assert fit_saves[-1]["returncode"] is None
        finally:
            with fit._FIT_LOCK:
                fit._FIT_JOBS.clear()
