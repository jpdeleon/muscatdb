"""Unit tests for the shared job-lifecycle runner (``muscat_db.jobs``).

This module is the single source of truth that photometry and transit-fit both
delegate to (architecture audit C1), so the finalizing state machine and the
run-id / path-segment helpers are exercised here directly.
"""

import os
import time

import pytest

from muscat_db import jobs


# --------------------------- run-id / path helpers ---------------------------


class TestRunIdHelpers:
    def test_slugify_run_name(self):
        assert jobs.slugify_run_name("") == "default"
        assert jobs.slugify_run_name("   ") == "default"
        assert jobs.slugify_run_name("Gaussian Priors!") == "gaussian_priors"
        assert jobs.slugify_run_name("a-b.c") == "a_b_c"  # never yields '-'
        assert "-" not in jobs.slugify_run_name("x-y-z")

    def test_build_run_id_components(self):
        # central_2k_2x2 is the default mode and is omitted from the id.
        assert jobs.build_run_id("lsc", "central_2k_2x2", "gaussian priors") == "lsc-gaussian_priors"
        assert jobs.build_run_id("mixed", "central_2k_2x2", "") == "mixed-default"
        assert jobs.build_run_id("lsc", "full_frame", "gaussian priors") == "lsc-full_frame-gaussian_priors"
        assert jobs.build_run_id("", "", "uniform") == "uniform"
        assert jobs.build_run_id("", "", "") == "default"

    def test_slugify_telescope(self):
        assert jobs.slugify_telescope("1m0-05") == "tel05"
        assert jobs.slugify_telescope("1M0-09") == "tel09"
        assert jobs.slugify_telescope("mixed") == "mixed"
        assert jobs.slugify_telescope("tel05") == "tel05"  # idempotent
        assert jobs.slugify_telescope("") == ""
        assert jobs.slugify_telescope(None) == ""

    def test_build_run_id_includes_telescope_between_site_and_mode(self):
        assert (
            jobs.build_run_id("lsc", "central_2k_2x2", "gaussian priors", telescope="1m0-05")
            == "lsc-tel05-gaussian_priors"
        )
        assert (
            jobs.build_run_id("lsc", "full_frame", "gaussian priors", telescope="1m0-05")
            == "lsc-tel05-full_frame-gaussian_priors"
        )
        assert jobs.build_run_id("", "", "uniform", telescope="1m0-05") == "tel05-uniform"
        assert jobs.build_run_id("lsc", "", "", telescope="mixed") == "lsc-mixed-default"

    def test_target_dir_name_rejects_traversal(self):
        assert jobs.target_dir_name("TOI 1234") == "TOI1234"
        for bad in ("", "   ", "..", ".", "a/b", "a\\b", "../x"):
            with pytest.raises(ValueError):
                jobs.target_dir_name(bad)

    def test_run_dir_name_rejects_traversal(self):
        assert jobs.run_dir_name("lsc-default") == "lsc-default"
        for bad in ("", "   ", "..", ".", "a/b", "a\\b"):
            with pytest.raises(ValueError):
                jobs.run_dir_name(bad)


# --------------------------- finalizing state machine ---------------------------


class _FakeProc:
    """Minimal subprocess.Popen stand-in: ``poll()`` returns the set returncode."""

    def __init__(self, rc=None):
        self._rc = rc
        self.pid = os.getpid()

    def poll(self):
        return self._rc

    def wait(self, timeout=None):
        return self._rc


def _cfg(grace_s=8, grace_terminal_s=2, markers=("DONE",), partial=None, success=None):
    return jobs.FinalizeConfig(
        grace_s=grace_s,
        grace_terminal_s=grace_terminal_s,
        terminal_markers=markers,
        partial_failure_marker=partial,
        success_marker=success,
    )


def _make_job(tmp_path, rc=None, cancelled=False):
    log = tmp_path / "run.log"
    log.write_text("INFO: started\n")
    job = jobs.PipelineJob(
        key="k", inst="muscat4", date="260101", target="T",
        cmd=["x"], proc=_FakeProc(rc=rc), logf=open(log, "a"), log_path=log,
    )
    job.cancelled = cancelled
    return job, log


class TestResolveJobState:
    def test_running_while_parent_alive(self, tmp_path):
        job, _ = _make_job(tmp_path, rc=None)
        state, rc, terminal = jobs.resolve_job_state(job, _cfg())
        assert (state, rc, terminal) == ("running", None, False)

    def test_cancelling_while_parent_alive(self, tmp_path):
        job, _ = _make_job(tmp_path, rc=None, cancelled=True)
        state, _rc, terminal = jobs.resolve_job_state(job, _cfg())
        assert state == "cancelling" and terminal is False

    def test_finalizing_while_log_fresh_then_terminal(self, tmp_path):
        job, log = _make_job(tmp_path, rc=0)
        # Worker just appended -> within grace window -> finalizing (non-terminal).
        with open(log, "a") as f:
            f.write("INFO: trailing worker output\n")
        state, rc, terminal = jobs.resolve_job_state(job, _cfg(grace_s=1))
        assert (state, rc, terminal) == ("finalizing", 0, False)
        # Once the log goes quiescent past the window -> terminal done.
        time.sleep(1.2)
        state, rc, terminal = jobs.resolve_job_state(job, _cfg(grace_s=1))
        assert (state, rc, terminal) == ("done", 0, True)

    def test_terminal_marker_shortens_window(self, tmp_path):
        job, log = _make_job(tmp_path, rc=0)
        with open(log, "a") as f:
            f.write("result: DONE\n")  # terminal marker present
        # Huge default but short terminal window: past terminal window -> done.
        time.sleep(1.2)
        state, _rc, terminal = jobs.resolve_job_state(
            job, _cfg(grace_s=600, grace_terminal_s=1, markers=("DONE",))
        )
        assert state == "done" and terminal is True

    def test_nonzero_exit_is_error(self, tmp_path):
        job, _ = _make_job(tmp_path, rc=3)
        time.sleep(0)  # log already quiescent (mtime in the past)
        state, rc, terminal = jobs.resolve_job_state(job, _cfg(grace_s=0))
        assert (state, rc, terminal) == ("error", 3, True)

    def test_partial_failure_marker_is_error(self, tmp_path):
        job, log = _make_job(tmp_path, rc=0)
        with open(log, "a") as f:
            f.write("WARNING: PARTIAL FAILURE: 1/2 bands\n")
        state, _rc, terminal = jobs.resolve_job_state(
            job, _cfg(grace_s=0, partial="PARTIAL FAILURE")
        )
        assert state == "error" and terminal is True

    def test_success_marker_overrides_lost_parent(self, tmp_path):
        """A non-zero/lost parent (e.g. server --reload, SIGHUP -> rc -1) must be
        reported 'done' when the log shows the reduction succeeded, because the
        real work runs in detached workers independent of the tracked parent."""
        job, log = _make_job(tmp_path, rc=-1)
        with open(log, "a") as f:
            f.write("INFO: photometry SUCCEEDED: 4/4 bands (1004s elapsed)\n")
        state, rc, terminal = jobs.resolve_job_state(
            job, _cfg(grace_s=0, success="photometry SUCCEEDED")
        )
        assert (state, rc, terminal) == ("done", -1, True)

    def test_nonzero_exit_without_success_marker_still_error(self, tmp_path):
        """Regression: with a success marker configured but absent from the log,
        a non-zero exit stays an error (the marker only *upgrades* real successes)."""
        job, log = _make_job(tmp_path, rc=-1)
        with open(log, "a") as f:
            f.write("INFO: crashed before finishing\n")
        state, _rc, terminal = jobs.resolve_job_state(
            job, _cfg(grace_s=0, success="photometry SUCCEEDED")
        )
        assert state == "error" and terminal is True

    def test_partial_failure_beats_success_marker(self, tmp_path):
        """If both markers appear, partial failure wins (do not report success)."""
        job, log = _make_job(tmp_path, rc=-1)
        with open(log, "a") as f:
            f.write("INFO: photometry SUCCEEDED: 3/4 bands\n")
            f.write("ERROR: photometry PARTIAL FAILURE: 3/4 bands\n")
        state, _rc, terminal = jobs.resolve_job_state(
            job,
            _cfg(grace_s=0, partial="photometry PARTIAL FAILURE", success="photometry SUCCEEDED"),
        )
        assert state == "error" and terminal is True

    def test_success_marker_ignored_when_not_configured(self, tmp_path):
        """Pipelines without a success_marker (e.g. transit-fit) keep the parent
        return code authoritative even if the success text happens to be logged."""
        job, log = _make_job(tmp_path, rc=-1)
        with open(log, "a") as f:
            f.write("INFO: photometry SUCCEEDED: 4/4 bands\n")
        state, _rc, terminal = jobs.resolve_job_state(job, _cfg(grace_s=0))  # success=None
        assert state == "error" and terminal is True

    def test_cancelled_finalizes_immediately(self, tmp_path):
        job, log = _make_job(tmp_path, rc=-15, cancelled=True)
        # Large window + fresh log proves cancel bypasses the finalize gate.
        with open(log, "a") as f:
            f.write("INFO: still writing during cancel\n")
        state, _rc, terminal = jobs.resolve_job_state(job, _cfg(grace_s=600))
        assert state == "cancelled" and terminal is True


class TestCountRunningFull:
    def test_counts_only_running_full_jobs(self, tmp_path):
        running_full, _ = _make_job(tmp_path, rc=None)
        running_full.run_type = "full"
        done_full, _ = _make_job(tmp_path, rc=0)
        done_full.run_type = "full"
        running_test, _ = _make_job(tmp_path, rc=None)
        running_test.run_type = "test"
        registry = {"a": running_full, "b": done_full, "c": running_test}
        assert jobs.count_running_full(registry) == 1
