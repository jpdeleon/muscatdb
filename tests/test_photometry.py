"""Tests for the photometry module and routes.

Filesystem-touching tests build a synthetic prose output dir under a temp
``MUSCAT_PROSE_DIR`` so they don't depend on the live ``/ut2`` mount. One
optional test exercises the real example reduction when it is present.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from muscat_db import photometry as phot

# Mirrors the real example dir: TOI-6715 / muscat4 / 250512, bands gp rp ip zs.
INST = "muscat4"
DATE = "250512"
TARGET = "TOI-6715"
BANDS = ["gp", "rp", "ip", "zs"]
REAL_EXAMPLE = Path("/ut2/jerome/ql/prose/muscat4/250512")


def _make_outputs(base: Path) -> Path:
    """Create a synthetic prose output dir and return it."""
    rdir = base / INST / DATE
    rdir.mkdir(parents=True)
    stem = f"{TARGET}_{INST}_{DATE}"
    # multi-band summary plots + archive + log
    for suf in ("_lightcurves.png", "_systematics.png", "_stacks.png"):
        (rdir / (stem + suf)).write_bytes(b"\x89PNG\r\n")
    (rdir / (stem + ".npz")).write_bytes(b"npz")
    (rdir / "2026-06-11T22:35:53.901155.log").write_text("log\n")
    # per-band products
    for b in BANDS:
        bstem = f"{TARGET}_{INST}_{b}_{DATE}"
        (rdir / (bstem + "_ref.png")).write_bytes(b"\x89PNG\r\n")
        (rdir / (bstem + "_apertures.png")).write_bytes(b"\x89PNG\r\n")
        (rdir / (bstem + "_alignment.png")).write_bytes(b"\x89PNG\r\n")
        (rdir / (bstem + ".gif")).write_bytes(b"GIF89a")
        (rdir / (bstem + ".csv")).write_text(
            "BJD_TDB,Flux,Flux_Err\n2460807.84,1.0001,0.0019\n2460807.85,0.9998,0.0020\n"
        )
    return rdir


@pytest.fixture
def prose_dir(tmp_path, monkeypatch):
    base = tmp_path / "prose"
    base.mkdir()
    monkeypatch.setenv("MUSCAT_PROSE_DIR", str(base))
    _make_outputs(base)
    return base


# ── config / paths ───────────────────────────────────────────────────────────

class TestPaths:
    def test_output_base_env_override(self, monkeypatch, tmp_path):
        monkeypatch.setenv("MUSCAT_PROSE_DIR", str(tmp_path))
        assert phot.output_base() == tmp_path

    def test_results_dir(self, monkeypatch, tmp_path):
        monkeypatch.setenv("MUSCAT_PROSE_DIR", str(tmp_path))
        assert phot.results_dir(INST, DATE) == tmp_path / INST / DATE

    def test_raw_data_dir_uses_instrument_config(self):
        # MUSCAT4.data_dir == /data/MuSCAT4
        assert phot.raw_data_dir(INST, DATE) == Path("/data/MuSCAT4") / DATE

    def test_valid_date(self):
        assert phot.valid_date("250512")
        assert not phot.valid_date("2505")
        assert not phot.valid_date("abcdef")
        assert not phot.valid_date("")


# ── output discovery ─────────────────────────────────────────────────────────

class TestListOutputs:
    def test_classifies_all_products(self, prose_dir):
        out = phot.list_outputs(INST, DATE, TARGET)
        assert out["has_any"]
        assert set(out["summary"]) == {"lightcurves", "systematics", "stacks"}
        assert out["summary"]["lightcurves"] == f"{TARGET}_{INST}_{DATE}_lightcurves.png"
        assert out["npz"] == f"{TARGET}_{INST}_{DATE}.npz"
        assert out["log"].endswith(".log")

    def test_bands_ordered_and_complete(self, prose_dir):
        out = phot.list_outputs(INST, DATE, TARGET)
        assert list(out["bands"]) == BANDS  # canonical order gp, rp, ip, zs
        gp = out["bands"]["gp"]
        assert set(gp) == {"ref", "apertures", "alignment", "gif", "csv"}
        assert gp["csv"] == f"{TARGET}_{INST}_gp_{DATE}.csv"

    def test_missing_dir_returns_empty(self, monkeypatch, tmp_path):
        monkeypatch.setenv("MUSCAT_PROSE_DIR", str(tmp_path))
        out = phot.list_outputs(INST, "999999", TARGET)
        assert out["has_any"] is False
        assert out["bands"] == {}

    def test_does_not_match_other_target(self, prose_dir):
        out = phot.list_outputs(INST, DATE, "TOI-9999")
        assert out["has_any"] is False

    def test_discovered_targets(self, prose_dir):
        assert phot.discovered_targets(INST, DATE) == [TARGET]

    def test_output_dates(self, prose_dir):
        assert phot.output_dates(INST) == [DATE]

    def test_csv_preview(self, prose_dir):
        csv_path = prose_dir / INST / DATE / f"{TARGET}_{INST}_gp_{DATE}.csv"
        headers, rows = phot.csv_preview(csv_path, n=8)
        assert headers == ["BJD_TDB", "Flux", "Flux_Err"]
        assert len(rows) == 2
        assert rows[0][1] == "1.0001"


# ── safe file serving ────────────────────────────────────────────────────────

class TestSafeArtifactPath:
    def test_valid_file(self, prose_dir):
        name = f"{TARGET}_{INST}_{DATE}_lightcurves.png"
        p = phot.safe_artifact_path(INST, DATE, name)
        assert p is not None and p.is_file()

    def test_rejects_traversal(self, prose_dir):
        assert phot.safe_artifact_path(INST, DATE, "../../etc/passwd") is None
        assert phot.safe_artifact_path(INST, DATE, "..") is None

    def test_rejects_slash(self, prose_dir):
        assert phot.safe_artifact_path(INST, DATE, "sub/file.png") is None

    def test_rejects_bad_extension(self, prose_dir):
        (prose_dir / INST / DATE / "evil.sh").write_text("#!/bin/sh\n")
        assert phot.safe_artifact_path(INST, DATE, "evil.sh") is None

    def test_rejects_bad_instrument(self, prose_dir):
        name = f"{TARGET}_{INST}_{DATE}_stacks.png"
        assert phot.safe_artifact_path("nope", DATE, name) is None

    def test_rejects_bad_date(self, prose_dir):
        assert phot.safe_artifact_path(INST, "bad", "x.png") is None

    def test_missing_file_returns_none(self, prose_dir):
        assert phot.safe_artifact_path(INST, DATE, "absent.png") is None


# ── command building ─────────────────────────────────────────────────────────

class TestCommand:
    def test_test_run_command(self, monkeypatch, tmp_path):
        monkeypatch.setenv("MUSCAT_PROSE_DIR", str(tmp_path))
        monkeypatch.delenv("MUSCAT_PROSE_PYTHON", raising=False)
        cmd = phot.build_command(INST, DATE, TARGET, test_run=True)
        assert "--test_run" in cmd
        assert "--overwrite" in cmd
        assert "run_photometry" in " ".join(cmd)
        i = cmd.index("--target_name")
        assert cmd[i + 1] == TARGET
        j = cmd.index("--results_dir")
        assert cmd[j + 1] == str(tmp_path / INST / DATE)

    def test_explicit_python_used(self, monkeypatch, tmp_path):
        monkeypatch.setenv("MUSCAT_PROSE_DIR", str(tmp_path))
        monkeypatch.setenv("MUSCAT_PROSE_PYTHON", "/opt/env/bin/python")
        cmd = phot.build_command(INST, DATE, TARGET)
        assert cmd[0] == "/opt/env/bin/python"
        assert "uv" not in cmd

    def test_conda_env_python_used_by_default(self, monkeypatch, tmp_path):
        # Fabricate a conda install with an env named "prose".
        base = tmp_path / "miniconda3"
        envpy = base / "envs" / "prose" / "bin" / "python"
        envpy.parent.mkdir(parents=True)
        envpy.write_text("")
        envpy.chmod(0o755)
        monkeypatch.delenv("MUSCAT_PROSE_PYTHON", raising=False)
        monkeypatch.setenv("CONDA_EXE", str(base / "bin" / "conda"))
        monkeypatch.setenv("MUSCAT_PROSE_CONDA_ENV", "prose")
        cmd = phot.build_command(INST, DATE, TARGET)
        assert cmd[0] == str(envpy)
        assert cmd[1:3] == ["-m", "prose.scripts.run_photometry"]
        assert "uv" not in cmd

    def test_command_str_full_run_has_no_test_run(self, monkeypatch, tmp_path):
        monkeypatch.setenv("MUSCAT_PROSE_DIR", str(tmp_path))
        s = phot.command_str(INST, DATE, TARGET, test_run=False)
        assert "--test_run" not in s
        assert "--target_name TOI-6715" in s


class TestRunOptions:
    def test_defaults_emit_minimal_command(self, monkeypatch, tmp_path):
        monkeypatch.setenv("MUSCAT_PROSE_DIR", str(tmp_path))
        cmd = phot.build_command(INST, DATE, TARGET, {}, test_run=False)
        # default numerics are NOT echoed
        for flag in ("--gif_stride", "--max_num_stars", "--cutout_size",
                     "--ccd_trim", "--bin_size_minutes", "--ref_band",
                     "--aper_radii", "--no_gif", "--use_barycorrpy"):
            assert flag not in cmd
        assert cmd[cmd.index("--bands") + 1:cmd.index("--bands") + 5] == BANDS

    def test_options_are_passed_through(self, monkeypatch, tmp_path):
        monkeypatch.setenv("MUSCAT_PROSE_DIR", str(tmp_path))
        opts = {
            "bands": ["gp", "rp"],
            "ref_band": "gp",
            "refid": "3",
            "aper_radii": "10,20,2",
            "annulus": "25,40",
            "aper_unit": "fwhm",
            "max_num_stars": "6",
            "min_star_separation": "12",
            "ccd_trim": "5,5",
            "make_gif": False,
            "use_barycorrpy": True,
            "gif_stride": "50",
        }
        cmd = phot.build_command(INST, DATE, TARGET, opts, test_run=False)
        s = " ".join(cmd)
        assert cmd[cmd.index("--bands") + 1:cmd.index("--bands") + 3] == ["gp", "rp"]
        assert "--ref_band gp" in s
        assert "--refid 3" in s
        assert "--aper_radii 10,20,2" in s
        assert "--annulus 25,40" in s
        assert "--aper_unit fwhm" in s
        assert "--max_num_stars 6" in s
        assert "--ccd_trim 5,5" in s
        assert "--no_gif" in cmd
        assert "--use_barycorrpy" in cmd
        assert "--gif_stride 50" in s

    def test_validate_requires_band(self):
        assert phot.validate_run_options(phot.normalize_run_options({"bands": []}))

    def test_validate_aper_requires_annulus(self):
        err = phot.validate_run_options(
            phot.normalize_run_options({"aper_radii": "10,20,2"})
        )
        assert err and "annulus" in err

    def test_validate_bad_aper_format(self):
        err = phot.validate_run_options(
            phot.normalize_run_options({"aper_radii": "abc", "annulus": "25,40"})
        )
        assert err and "MIN,MAX,DR" in err

    def test_validate_ok(self):
        assert phot.validate_run_options(phot.normalize_run_options({})) is None


# ── job runner ───────────────────────────────────────────────────────────────

class TestStartRun:
    def test_rejects_unknown_instrument(self):
        r = phot.start_run("nope", DATE, TARGET)
        assert r["ok"] is False

    def test_rejects_bad_date(self):
        r = phot.start_run(INST, "bad", TARGET)
        assert r["ok"] is False

    def test_rejects_missing_raw_data(self, monkeypatch, tmp_path):
        # Point both output and raw data at empty temp dirs.
        monkeypatch.setenv("MUSCAT_PROSE_DIR", str(tmp_path / "out"))
        from dataclasses import replace
        from muscat_db.instruments import INSTRUMENTS
        patched = dict(INSTRUMENTS)
        patched[INST] = replace(INSTRUMENTS[INST], data_dir=str(tmp_path / "raw"))
        monkeypatch.setattr("muscat_db.photometry.INSTRUMENTS", patched)
        r = phot.start_run(INST, DATE, TARGET)
        assert r["ok"] is False
        assert "raw data not found" in r["error"]

    def test_job_status_none_when_not_started(self):
        s = phot.job_status(INST, "111111", "Nobody")
        assert s["state"] == "none"


# ── routes (FastAPI TestClient) ──────────────────────────────────────────────

class TestRoutes:
    @pytest.fixture
    def client(self, prose_dir, tmp_path, monkeypatch):
        from fastapi.testclient import TestClient
        # Empty DB so selector queries succeed without obslog data. Using the
        # client as a context manager fires the startup event that creates the
        # schema (frames/summaries/targets tables).
        db = tmp_path / "muscat.db"
        monkeypatch.setenv("MUSCAT_DB_PATH", str(db))
        from muscat_db.web import app
        with TestClient(app) as c:
            yield c

    def test_photometry_page_lists_outputs(self, client):
        r = client.get(f"/photometry?inst={INST}&date={DATE}&target={TARGET}")
        assert r.status_code == 200
        assert f"{TARGET}_{INST}_{DATE}_lightcurves.png" in r.text
        assert "Per-band products" in r.text

    def test_photometry_page_empty_selectors(self, client):
        r = client.get("/photometry")
        assert r.status_code == 200
        assert "select an instrument" in r.text.lower() or "Pick an instrument" in r.text

    def test_file_route_serves_png(self, client):
        name = f"{TARGET}_{INST}_{DATE}_stacks.png"
        r = client.get(f"/photometry/file/{INST}/{DATE}/{name}")
        assert r.status_code == 200

    def test_file_route_rejects_bad_ext(self, client):
        r = client.get(f"/photometry/file/{INST}/{DATE}/evil.sh")
        assert r.status_code == 404

    def test_status_route(self, client):
        r = client.get(f"/photometry/status?inst={INST}&date=111111&target=Nobody")
        assert r.status_code == 200
        assert r.json()["state"] == "none"

    def test_run_route_rejects_missing_raw(self, client, tmp_path, monkeypatch):
        # raw data dir for date 111111 won't exist
        r = client.post("/photometry/run", json={
            "inst": INST, "date": "111111", "target": TARGET, "test_run": True,
        })
        assert r.status_code == 400
        assert r.json()["ok"] is False

    def test_command_route_echoes_options(self, client):
        r = client.post("/photometry/command", json={
            "inst": INST, "date": DATE, "target": TARGET, "test_run": False,
            "options": {"bands": ["gp"], "use_barycorrpy": True, "max_num_stars": 7},
        })
        assert r.status_code == 200
        body = r.json()
        assert body["error"] is None
        assert "--use_barycorrpy" in body["command"]
        assert "--max_num_stars 7" in body["command"]

    def test_command_route_reports_validation_error(self, client):
        r = client.post("/photometry/command", json={
            "inst": INST, "date": DATE, "target": TARGET,
            "options": {"aper_radii": "10,20,2"},  # missing annulus
        })
        assert r.status_code == 200
        assert "annulus" in r.json()["error"]

    def test_page_has_options_form(self, client):
        r = client.get(f"/photometry?inst={INST}&date={DATE}&target={TARGET}")
        assert r.status_code == 200
        for token in ("opt-ref_band", "opt-aper_radii", "opt-max_num_stars",
                      "opt-use_barycorrpy", "Pipeline options"):
            assert token in r.text

    def test_summary_is_sortable_single_column(self, client):
        r = client.get(f"/photometry?inst={INST}&date={DATE}&target={TARGET}")
        html = r.text
        # single-column sortable summary container
        assert 'fig-grid col sortable" data-sort-key="summary"' in html
        # default order: light curve, then stack, then systematics
        i_lc = html.index('data-fig-id="lightcurves"')
        i_st = html.index('data-fig-id="stacks"')
        i_sy = html.index('data-fig-id="systematics"')
        assert i_lc < i_st < i_sy
        # drag affordance + per-band grids are sortable too
        assert "drag-handle" in html
        assert 'fig-grid sortable" data-sort-key="band"' in html


# ── real example output (optional) ───────────────────────────────────────────

@pytest.mark.skipif(not REAL_EXAMPLE.is_dir(), reason="example output not mounted")
class TestRealExample:
    def test_real_outputs_classified(self):
        # Uses the default MUSCAT_PROSE_DIR (/ut2/jerome/ql/prose).
        os.environ.pop("MUSCAT_PROSE_DIR", None)
        out = phot.list_outputs(INST, DATE, TARGET)
        assert out["has_any"]
        assert set(out["summary"]) == {"lightcurves", "systematics", "stacks"}
        assert list(out["bands"]) == BANDS
        assert out["npz"] == f"{TARGET}_{INST}_{DATE}.npz"
