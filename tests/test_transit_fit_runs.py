"""Tests for per-run transit-fit isolation (site / mode / run-name).

A transit fit is stored in its own ``{target}/{run_id}/`` directory so distinct
runs never overwrite each other; runs are discovered from disk and selectable on
the page. These tests cover the run-id helpers, directory isolation, discovery,
run-scoped outputs, the run-aware file route, and the DB run_id column.
"""

from __future__ import annotations

import os
import time
from pathlib import Path

import pytest
import yaml

from muscat_db import transit_fit as fit


# ── run-id helpers ─────────────────────────────────────────────────────────

class TestRunId:
    def test_slugify_defaults_and_sanitizes(self):
        assert fit.slugify_run_name("") == "default"
        assert fit.slugify_run_name("   ") == "default"
        assert fit.slugify_run_name("Gaussian Priors!") == "gaussian_priors"
        assert fit.slugify_run_name("a-b.c") == "a_b_c"   # never yields '-'
        assert "-" not in fit.slugify_run_name("x-y-z")

    def test_build_run_id_components(self):
        assert fit.build_run_id("lsc", "central_2k_2x2", "gaussian priors") == "lsc-central_2k_2x2-gaussian_priors"
        assert fit.build_run_id("mixed", "central_2k_2x2", "") == "mixed-central_2k_2x2-default"
        assert fit.build_run_id("", "", "uniform") == "uniform"
        assert fit.build_run_id("", "", "") == "default"

    def test_csv_site_mode(self):
        assert fit.csv_site_mode("HIP67522_sinistro_lsc_gp_250710_full.csv") == ("lsc", "full_frame")
        assert fit.csv_site_mode("HIP67522_sinistro_cpt_gp_250710.csv") == ("cpt", "central_2k_2x2")
        assert fit.csv_site_mode("TOI-6_muscat4_gp_250512.csv") == (None, "central_2k_2x2")

    def test_selected_site_mode_mixed_and_single(self):
        names = ["HIP_sinistro_lsc_gp_250710.csv", "HIP_sinistro_cpt_gp_250710.csv"]
        assert fit.selected_site_mode("sinistro", names) == ("mixed", "central_2k_2x2")
        assert fit.selected_site_mode("sinistro", names[:1]) == ("lsc", "central_2k_2x2")
        assert fit.selected_site_mode("muscat4", ["x_muscat4_gp_250512.csv"]) == ("", "")

    def test_fit_job_key_run_aware(self):
        assert fit.fit_job_key("sinistro", "250710", "HIP 67522", "lsc-x-g") == "sinistro/250710/HIP67522/lsc-x-g"
        assert fit.fit_job_key("sinistro", "250710", "HIP 67522") == "sinistro/250710/HIP67522"


# ── directory isolation ────────────────────────────────────────────────────

class TestFitOutputDir:
    def test_run_isolation_and_legacy(self, monkeypatch, tmp_path):
        monkeypatch.setenv("MUSCAT_TIMER_DIR", str(tmp_path))
        legacy = fit.fit_output_dir("sinistro", "250710", "HIP 67522")
        run_a = fit.fit_output_dir("sinistro", "250710", "HIP 67522", "lsc-central_2k_2x2-g")
        run_b = fit.fit_output_dir("sinistro", "250710", "HIP 67522", "cpt-central_2k_2x2-g")
        assert legacy.name == "HIP67522"
        assert run_a != run_b
        assert run_a.parent == legacy  # run dirs nest under the target dir

    def test_run_id_traversal_rejected(self, monkeypatch, tmp_path):
        monkeypatch.setenv("MUSCAT_TIMER_DIR", str(tmp_path))
        for bad in ("../evil", "a/b", "..", "a\\b"):
            with pytest.raises(ValueError):
                fit.fit_output_dir("sinistro", "250710", "HIP67522", bad)


# ── discovery + run-scoped outputs ─────────────────────────────────────────

def _make_run(tdir: Path, run_id: str, site: str, mode: str, name: str, mtime: int):
    rd = tdir / run_id
    (rd / "out").mkdir(parents=True)
    (rd / "out" / "fit.png").write_bytes(b"\x89PNG\r\n")
    (rd / "out" / "summary.csv").write_text("parameter,mean\nt0[0],1.0\n")
    (rd / "meta.yaml").write_text(yaml.safe_dump(
        {"site": site, "mode": mode, "run_name": name, "run_id": run_id}))
    os.utime(rd / "out", (mtime, mtime))


class TestRunDiscovery:
    def test_list_runs_newest_first_and_ignores_out(self, monkeypatch, tmp_path):
        monkeypatch.setenv("MUSCAT_TIMER_DIR", str(tmp_path))
        tdir = tmp_path / "sinistro" / "250710" / "HIP67522"
        _make_run(tdir, "lsc-central_2k_2x2-gaussian", "lsc", "central_2k_2x2", "gaussian", 1_000_100)
        _make_run(tdir, "cpt-central_2k_2x2-uniform", "cpt", "central_2k_2x2", "uniform", 1_000_200)
        runs = fit.list_fit_runs("sinistro", "250710", "HIP67522")
        assert [r.run_id for r in runs] == ["cpt-central_2k_2x2-uniform", "lsc-central_2k_2x2-gaussian"]
        assert runs[0].run_name == "uniform" and runs[0].site == "cpt"
        assert all(not r.is_legacy for r in runs)

    def test_list_runs_surfaces_legacy(self, monkeypatch, tmp_path):
        monkeypatch.setenv("MUSCAT_TIMER_DIR", str(tmp_path))
        tdir = tmp_path / "muscat4" / "250512" / "TOI-6715"
        (tdir / "out").mkdir(parents=True)
        (tdir / "out" / "fit.png").write_bytes(b"\x89PNG\r\n")
        runs = fit.list_fit_runs("muscat4", "250512", "TOI-6715")
        assert len(runs) == 1 and runs[0].is_legacy and runs[0].run_id == ""

    def test_get_fit_outputs_run_scoped(self, monkeypatch, tmp_path):
        monkeypatch.setenv("MUSCAT_TIMER_DIR", str(tmp_path))
        tdir = tmp_path / "sinistro" / "250710" / "HIP67522"
        _make_run(tdir, "lsc-central_2k_2x2-gaussian", "lsc", "central_2k_2x2", "gaussian", 1_000_100)
        fit._fit_outputs_cache.clear()
        out = fit.get_fit_outputs("sinistro", "250710", "HIP67522", run_id="lsc-central_2k_2x2-gaussian")
        assert out["has_any"] and any(p["file"] == "fit.png" for p in out["plots"])
        # A non-existent run yields nothing.
        fit._fit_outputs_cache.clear()
        empty = fit.get_fit_outputs("sinistro", "250710", "HIP67522", run_id="does-not-exist")
        assert empty["has_any"] is False


# ── DB run_id column ───────────────────────────────────────────────────────

class TestSaveJobRunId:
    def test_run_id_in_key_and_column(self, monkeypatch, tmp_path):
        monkeypatch.setenv("MUSCAT_DB_PATH", str(tmp_path / "muscat.db"))
        from muscat_db.database import save_job, get_persisted_jobs
        save_job(type_="transit_fit", inst="sinistro", date="250710", target="HIP 67522",
                 run_id="lsc-central_2k_2x2-g", state="running", returncode=None,
                 elapsed=0, started_at=time.time(), run_type="full")
        save_job(type_="transit_fit", inst="sinistro", date="250710", target="HIP 67522",
                 run_id="cpt-central_2k_2x2-g", state="done", returncode=0,
                 elapsed=5, started_at=time.time(), run_type="full")
        rows = [j for j in get_persisted_jobs() if j["type"] == "transit_fit"]
        keys = {j["key"] for j in rows}
        assert "transit_fit:sinistro/250710/HIP67522/lsc-central_2k_2x2-g" in keys
        assert "transit_fit:sinistro/250710/HIP67522/cpt-central_2k_2x2-g" in keys
        assert {j["run_id"] for j in rows} == {"lsc-central_2k_2x2-g", "cpt-central_2k_2x2-g"}


# ── run-aware file route ───────────────────────────────────────────────────

class TestRunFileRoute:
    @pytest.fixture
    def client(self, tmp_path, monkeypatch):
        from fastapi.testclient import TestClient
        monkeypatch.setenv("MUSCAT_TIMER_DIR", str(tmp_path / "timer"))
        monkeypatch.setenv("MUSCAT_DB_PATH", str(tmp_path / "muscat.db"))
        from muscat_db.web import app
        with TestClient(app) as c:
            yield c, tmp_path

    def test_serves_run_segment_and_legacy(self, client):
        c, tmp_path = client
        tdir = tmp_path / "timer" / "sinistro" / "250710" / "HIP67522"
        _make_run(tdir, "lsc-central_2k_2x2-g", "lsc", "central_2k_2x2", "g", 1_000_100)
        (tdir / "out").mkdir(parents=True, exist_ok=True)
        (tdir / "out" / "fit.png").write_bytes(b"\x89PNG\r\n")  # legacy too

        r = c.get("/transit-fit/file/sinistro/250710/HIP67522/run/lsc-central_2k_2x2-g/summary.csv")
        assert r.status_code == 200 and "t0[0]" in r.text
        r_legacy = c.get("/transit-fit/file/sinistro/250710/HIP67522/fit.png")
        assert r_legacy.status_code == 200

    def test_run_segment_rejects_traversal(self, client):
        c, _ = client
        r = c.get("/transit-fit/file/sinistro/250710/HIP67522/run/..%2Fevil/summary.csv")
        assert r.status_code in (400, 404)


# ── job status DB prioritization ───────────────────────────────────────────

class TestTransitFitJobStatus:
    def test_job_status_prioritizes_db_state(self, monkeypatch, tmp_path):
        monkeypatch.setenv("MUSCAT_DB_PATH", str(tmp_path / "muscat.db"))
        monkeypatch.setenv("MUSCAT_TIMER_DIR", str(tmp_path / "timer"))
        from muscat_db.database import save_job
        
        inst = "muscat4"
        date = "260127"
        target = "HIP67522"
        run_id = "default"
        
        # 1. Create a log file on disk to simulate a legacy/prior run log.
        rdir = fit.fit_output_dir(inst, date, target, run_id)
        rdir.mkdir(parents=True, exist_ok=True)
        log_path = rdir / "timer-fit.log"
        log_path.write_text("prior run completed log content")
        
        # Without any database state (and no job in-memory), it fallback to disk status "done"
        status = fit.job_status(inst, date, target, run_id)
        assert status["state"] == "done"
        assert "prior run completed" in status["log"]
        
        # 2. Queue a job in the database with status "pending".
        # It should now return "pending" instead of reading the finished disk log.
        save_job(
            type_="transit_fit",
            inst=inst, date=date, target=target, run_id=run_id,
            state="pending",
            returncode=None, elapsed=0,
            started_at=time.time(),
            run_type="full"
        )
        status = fit.job_status(inst, date, target, run_id)
        assert status["state"] == "pending"
        assert status["log"] == ""

        # 2.5. Queue a job in the database with status "running".
        # It should now return "running" instead of reading the finished disk log as completed.
        save_job(
            type_="transit_fit",
            inst=inst, date=date, target=target, run_id=run_id,
            state="running",
            returncode=None, elapsed=5,
            started_at=time.time() - 5,
            run_type="full"
        )
        status = fit.job_status(inst, date, target, run_id)
        assert status["state"] == "running"
        assert "prior run completed" in status["log"]
        
        # 3. Simulate a terminal state in the database like "cancelled".
        # It should return "cancelled" and read the log from disk.
        save_job(
            type_="transit_fit",
            inst=inst, date=date, target=target, run_id=run_id,
            state="cancelled",
            returncode=-1, elapsed=10,
            started_at=time.time(),
            run_type="full"
        )
        status = fit.job_status(inst, date, target, run_id)
        assert status["state"] == "cancelled"
        assert "prior run completed" in status["log"]
