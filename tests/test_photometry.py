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
    for suf in ("_lightcurves.png", "_systematics.png", "_stacks.png", "_raw_flux.png"):
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

    raw_base = tmp_path / "data"
    raw_base.mkdir()
    monkeypatch.setenv("MUSCAT_DATA_DIR", str(raw_base))

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
        assert set(out["summary"]) == {"lightcurves", "raw_flux", "covariates", "stacks"}
        assert out["summary"]["lightcurves"] == f"{TARGET}_{INST}_{DATE}_lightcurves.png"
        assert out["summary"]["raw_flux"] == f"{TARGET}_{INST}_{DATE}_raw_flux.png"
        assert out["npz"] == f"{TARGET}_{INST}_{DATE}.npz"
        assert out["log"].endswith(".log")

    def test_discovers_masters_for_muscat(self, prose_dir, tmp_path):
        raw_base = tmp_path / "data"
        mdir = raw_base / f"{DATE}_calibrated"
        mdir.mkdir(parents=True, exist_ok=True)
        (mdir / "master_flat_gp.png").write_bytes(b"\x89PNG\r\n")
        (mdir / "master_bias.png").write_bytes(b"\x89PNG\r\n")

        rdir = prose_dir / "muscat" / DATE
        rdir.mkdir(parents=True, exist_ok=True)
        stem = f"{TARGET}_muscat_{DATE}"
        (rdir / (stem + "_lightcurves.png")).write_bytes(b"\x89PNG\r\n")

        out = phot.list_outputs("muscat", DATE, TARGET)
        assert out["has_any"]
        assert out["masters"] == ["master_bias.png", "master_flat_gp.png"]


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

    def test_get_photometry_status_none(self, prose_dir):
        status = phot.get_photometry_status(INST, DATE, "UnknownTarget")
        assert status == "none"

    def test_get_photometry_status_full_from_csv(self, prose_dir):
        rdir = prose_dir / INST / DATE
        bstem = f"{TARGET}_{INST}_gp_{DATE}"
        (rdir / (bstem + ".csv")).write_text(
            "BJD_TDB,Flux,Flux_Err\n" + "\n".join("2460807.84,1.0001,0.0019" for _ in range(20))
        )
        status = phot.get_photometry_status(INST, DATE, TARGET)
        assert status == "full"

    def test_get_photometry_status_test_run(self, prose_dir):
        rdir = prose_dir / INST / DATE
        for lf in rdir.glob("*.log"):
            lf.unlink()
        (rdir / "run.log").write_text(f"Running reduction for {TARGET}\n--test_run option enabled\n")
        status = phot.get_photometry_status(INST, DATE, TARGET)
        assert status == "test"


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
        assert "--gif" not in cmd
        assert "--use_barycorrpy" in cmd
        assert "--gif_stride 50" in s

    def test_plot_gaia_sources_default_on(self, monkeypatch, tmp_path):
        monkeypatch.setenv("MUSCAT_PROSE_DIR", str(tmp_path))
        # checked by default -> emit the flag
        cmd = phot.build_command(INST, DATE, TARGET, {})
        assert "--plot_gaia_sources" in cmd
        # unchecked -> no flag emitted (pipeline default is False)
        cmd = phot.build_command(INST, DATE, TARGET, {"plot_gaia_sources": False})
        assert "--plot_gaia_sources" not in cmd

    def test_avoid_comparison_ids_passed_through(self, monkeypatch, tmp_path):
        monkeypatch.setenv("MUSCAT_PROSE_DIR", str(tmp_path))
        cmd = phot.build_command(INST, DATE, TARGET,
                                 {"avoid_comparison_ids": "5,7,12"}, test_run=False)
        s = " ".join(cmd)
        assert "--avoid_cids" in s
        assert " --avoid_cids 5 7 12" in s or "--avoid_cids 5 7 12 " in s

    def test_empty_avoid_comparison_ids_emits_nothing(self, monkeypatch, tmp_path):
        monkeypatch.setenv("MUSCAT_PROSE_DIR", str(tmp_path))
        cmd = phot.build_command(INST, DATE, TARGET,
                                 {"avoid_comparison_ids": ""}, test_run=False)
        assert "--avoid_cids" not in cmd

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

    def test_cancel_no_job(self):
        r = phot.cancel_run(INST, "222222", "Nobody")
        assert r["ok"] is False

    def test_cancel_running_job(self, monkeypatch, tmp_path):
        # Launch a harmless long-running process as the "pipeline" and cancel it.
        monkeypatch.setenv("MUSCAT_PROSE_DIR", str(tmp_path / "out"))
        monkeypatch.setenv("MUSCAT_PROSE_PYTHON", "/bin/sh")
        from dataclasses import replace
        from muscat_db.instruments import INSTRUMENTS as _INST
        raw = tmp_path / "raw" / DATE
        raw.mkdir(parents=True)
        patched = dict(_INST)
        patched[INST] = replace(_INST[INST], data_dir=str(tmp_path / "raw"))
        monkeypatch.setattr("muscat_db.photometry.INSTRUMENTS", patched)

        # Replace build_command so the "pipeline" is just `sleep 60`.
        monkeypatch.setattr(
            phot, "build_command",
            lambda *a, **k: ["/bin/sh", "-c", "sleep 60"],
        )
        res = phot.start_run(INST, DATE, TARGET, test_run=True)
        assert res["ok"], res
        assert phot.job_status(INST, DATE, TARGET)["state"] in ("running", "cancelling")

        cancel = phot.cancel_run(INST, DATE, TARGET)
        assert cancel["ok"] is True

        # The process should terminate; status becomes 'cancelled'.
        import time as _t
        deadline = _t.time() + 10
        state = None
        while _t.time() < deadline:
            state = phot.job_status(INST, DATE, TARGET)["state"]
            if state == "cancelled":
                break
            _t.sleep(0.2)
        assert state == "cancelled"


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
        assert r.headers.get("cache-control") == "no-store, no-cache, must-revalidate, max-age=0"

    def test_file_route_serves_master_calibration(self, client, tmp_path, monkeypatch):
        raw_base = tmp_path / "data"
        monkeypatch.setenv("MUSCAT_DATA_DIR", str(raw_base))
        mdir = raw_base / f"{DATE}_calibrated"
        mdir.mkdir(parents=True, exist_ok=True)
        (mdir / "master_bias.png").write_bytes(b"\x89PNG\r\n")

        r = client.get(f"/photometry/file/muscat/{DATE}/master_bias.png")
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

    def test_page_has_run_and_cancel_buttons(self, client):
        r = client.get(f"/photometry?inst={INST}&date={DATE}&target={TARGET}")
        html = r.text
        assert 'id="run-test-btn"' in html
        assert 'id="run-full-btn"' in html
        assert 'id="cancel-btn"' in html
        assert "Run full reduction" in html

    def test_cancel_route_no_job(self, client):
        r = client.post("/photometry/cancel", json={
            "inst": INST, "date": "222222", "target": "Nobody",
        })
        assert r.status_code == 400
        assert r.json()["ok"] is False

    def test_summary_is_sortable_single_column(self, client):
        r = client.get(f"/photometry?inst={INST}&date={DATE}&target={TARGET}")
        html = r.text
        # single-column sortable summary container
        assert 'fig-grid col sortable" data-sort-key="summary"' in html
        # default order: light curve, then raw flux, then covariates, then stacks
        i_lc = html.index('data-fig-id="lightcurves"')
        i_rf = html.index('data-fig-id="raw_flux"')
        i_sy = html.index('data-fig-id="covariates"')
        i_st = html.index('data-fig-id="stacks"')
        assert i_lc < i_rf < i_sy < i_st
        # drag affordance + per-band grids are sortable too
        assert "drag-handle" in html
        assert 'fig-grid col sortable" data-sort-key="band"' in html

    def test_photometry_page_shows_broadband(self, client):
        db_path = os.environ["MUSCAT_DB_PATH"]
        import sqlite3
        conn = sqlite3.connect(db_path)
        conn.execute(
            "INSERT INTO frames (instrument, obsdate, ccd, filename, object, filter) VALUES (?, ?, ?, ?, ?, ?)",
            (INST, DATE, 0, "file1.fits", TARGET, "gp")
        )
        conn.commit()
        conn.close()

        r = client.get(f"/photometry?inst={INST}&date={DATE}&target={TARGET}")
        assert r.status_code == 200
        assert "(broadband)" in r.text
        assert "(narrowband)" not in r.text

    def test_photometry_page_shows_narrowband(self, client):
        db_path = os.environ["MUSCAT_DB_PATH"]
        import sqlite3
        conn = sqlite3.connect(db_path)
        conn.execute("DELETE FROM frames")
        conn.execute(
            "INSERT INTO frames (instrument, obsdate, ccd, filename, object, filter) VALUES (?, ?, ?, ?, ?, ?)",
            (INST, DATE, 0, "file1.fits", TARGET, "g_narrow")
        )
        conn.commit()
        conn.close()

        r = client.get(f"/photometry?inst={INST}&date={DATE}&target={TARGET}")
        assert r.status_code == 200
        assert "(narrowband)" in r.text
        assert "(broadband)" not in r.text

    def test_index_page(self, client):
        r = client.get("/")
        assert r.status_code == 200
        assert "MuSCAT + LCO database (Last updated on" in r.text

    def test_logs_page(self, client):
        r = client.get("/logs")
        assert r.status_code == 200
        assert "Logs" in r.text
        assert "Instruments" in r.text
        assert "Data Summary" in r.text

    def test_transit_fit_page(self, client):
        r = client.get("/transit-fit")
        assert r.status_code == 200
        assert "Transit Fit" in r.text
        assert "Instrument" in r.text
        assert "Transit Fitting Pipeline" in r.text

    def test_transit_fit_page_with_lightcurves(self, client, tmp_path, mocker):
        dummy_csv = tmp_path / "dummy_muscat3_250717.csv"
        dummy_csv.write_text("dummy data")
        
        mocker.patch("muscat_db.transit_fit.get_csv_lightcurves", return_value=[dummy_csv])
        mocker.patch("muscat_db.transit_fit.get_fit_outputs", return_value=None)
        mocker.patch("muscat_db.transit_fit.get_target_parameters", return_value={})
        mocker.patch("muscat_db.web._get_dates", return_value=[])
        mocker.patch("muscat_db.web._get_objects", return_value=[])
        mocker.patch("muscat_db.photometry.discovered_targets", return_value=[])
        
        r = client.get("/transit-fit?inst=muscat3&date=250717&target=dummy")
        assert r.status_code == 200
        assert "dummy_muscat3_250717.csv" in r.text
        assert "Created:" in r.text

    def test_transit_fit_query_archive_success(self, client, mocker):
        mock_response = mocker.MagicMock()
        mock_response.__enter__.return_value = mock_response
        mock_response.read.return_value = b'[{"pl_name": "WASP-104 b", "st_teff": 5475.0, "st_tefferr1": 127.0, "st_tefferr2": -127.0}]'
        mocker.patch("urllib.request.urlopen", return_value=mock_response)
        
        r = client.get("/transit-fit/query-archive?target=WASP-104")
        assert r.status_code == 200
        data = r.json()
        assert data["ok"] is True
        assert data["pl_name"] == "WASP-104 b"
        assert data["params"]["teff"] == 5475.0

    def test_jobs_page(self, client, monkeypatch):
        mock_jobs = [
            {
                "key": "photometry:muscat2/220226/TOI-5684.01",
                "type": "photometry",
                "inst": "muscat2",
                "date": "220226",
                "target": "TOI-5684.01",
                "state": "running",
                "returncode": None,
                "elapsed": 10,
                "started_at": 1645833600.0,
                "error_desc": None
            },
            {
                "key": "photometry:muscat3/220226/TOI-5684.02",
                "type": "photometry",
                "inst": "muscat3",
                "date": "220226",
                "target": "TOI-5684.02",
                "state": "done",
                "returncode": 0,
                "elapsed": 120,
                "started_at": 1645833500.0,
            }
        ]
        monkeypatch.setattr("muscat_db.web.get_persisted_jobs", lambda: mock_jobs)
        monkeypatch.setattr("muscat_db.photometry.sync_jobs", lambda: None)
        monkeypatch.setattr("muscat_db.transit_fit.sync_jobs", lambda: None)
    
        r = client.get("/jobs")
        assert r.status_code == 200
        assert "Jobs" in r.text
        assert "Photometry Jobs" in r.text
        assert "Transit Fit Jobs" in r.text
        assert "View reduction" not in r.text
        assert "cancelJob(this)" in r.text
        assert 'data-target="TOI-5684.01"' in r.text
        assert "TOI-5684.02" in r.text

    def test_workflow_route(self, client):
        r = client.get("/workflow")
        assert r.status_code == 200
        assert "MuSCAT-db Pipeline Workflow" in r.text
        assert "mermaid" in r.text


class TestTransitFitOptions:
    def test_validate_fit_options_success(self):
        from muscat_db.transit_fit import validate_fit_options
        
        # Valid single planet
        opts_single = {
            "planets": "b",
            "teff": "5000",
            "period": "1.23",
            "period_unc": "0.01",
        }
        assert validate_fit_options(opts_single) is None
        
        # Valid multiple planets
        opts_multi = {
            "planets": "b,c",
            "teff": "5000",
            "period_b": "1.23",
            "period_unc_b": "0.01",
            "period_c": "4.56",
            "period_unc_c": "0.02",
        }
        assert validate_fit_options(opts_multi) is None

    def test_validate_fit_options_failure(self):
        from muscat_db.transit_fit import validate_fit_options

        # Invalid planet format
        assert "planets must be single letters" in validate_fit_options({"planets": "b,c2"})
        
        # Invalid stellar parameter (negative Teff)
        assert "Teff (K) must be greater than 0" in validate_fit_options({
            "planets": "b",
            "teff": "-100",
        })

        # Invalid stellar parameter (non-numeric logg)
        assert "log g must be a number" in validate_fit_options({
            "planets": "b",
            "logg": "abc",
        })

        # Invalid planetary parameter (negative period on first planet)
        assert "Period (days) (planet b) must be greater than 0" in validate_fit_options({
            "planets": "b,c",
            "period_b": "-1.23",
        })

        # Invalid planetary parameter (non-numeric period on second planet)
        assert "Period (days) (planet c) must be a number" in validate_fit_options({
            "planets": "b,c",
            "period_c": "xyz",
        })

        # Invalid Rp/R* (>= 1)
        assert "Rp/R* (planet c) must be less than 1" in validate_fit_options({
            "planets": "b,c",
            "ror_c": "1.2",
        })

    def test_write_fit_inputs(self, tmp_path):
        from muscat_db.transit_fit import _write_fit_inputs
        import yaml
        
        csv_file = tmp_path / "target_muscat3_260613_gp.csv"
        csv_file.write_text("time,flux,error")
        
        options = {
            "planets": "b,c",
            "teff": "5500",
            "teff_unc": "120",
            "period_b": "2.5",
            "period_unc_b": "0.02",
            "period_c": "5.0",
            "period_unc_c": "0.05",
            "t0_b": "2450000.1",
            "t0_unc_b": "0.001",
            "t0_c": "2450000.2",
            "t0_unc_c": "0.002",
        }
        
        rdir = tmp_path / "run_dir"
        rdir.mkdir()
        
        _write_fit_inputs(rdir, "muscat3", "260613", [csv_file], options)
        
        # Verify files created
        assert (rdir / "fit.yaml").is_file()
        assert (rdir / "sys.yaml").is_file()
        assert (rdir / csv_file.name).is_file()
        
        # Load fit.yaml and verify
        with open(rdir / "fit.yaml") as f:
            fit_data = yaml.safe_load(f)
        assert fit_data["planets"] == "b,c"
        
        # Load sys.yaml and verify
        with open(rdir / "sys.yaml") as f:
            sys_data = yaml.safe_load(f)
            
        assert sys_data["star"]["teff"] == [5500.0, 120.0]
        assert "b" in sys_data["planets"]
        assert "c" in sys_data["planets"]
        assert sys_data["planets"]["b"]["period"] == [2.5, 0.02]
        assert sys_data["planets"]["c"]["period"] == [5.0, 0.05]
        assert sys_data["planets"]["b"]["t0"] == [2450000.1, 0.001]
        assert sys_data["planets"]["c"]["t0"] == [2450000.2, 0.002]


# ── real example output (optional) ───────────────────────────────────────────

@pytest.mark.skipif(not REAL_EXAMPLE.is_dir(), reason="example output not mounted")
class TestRealExample:
    def test_real_outputs_classified(self):
        # Uses the default MUSCAT_PROSE_DIR (/ut2/jerome/ql/prose).
        os.environ.pop("MUSCAT_PROSE_DIR", None)
        out = phot.list_outputs(INST, DATE, TARGET)
        assert out["has_any"]
        assert {"lightcurves", "covariates", "stacks"}.issubset(set(out["summary"]))
        assert list(out["bands"]) == BANDS
        assert out["npz"] == f"{TARGET}_{INST}_{DATE}.npz"


class TestBandsFromFilters:
    def test_canonicalizes_muscat_filters(self):
        # raw obslog FILTER values (g, r, i, z_s) -> prose --bands tokens.
        assert phot.bands_from_filters(["g", "r", "i", "z_s"]) == ["gp", "rp", "ip", "zs"]

    def test_sinistro_passthrough_and_order(self):
        # Unknown filters (R, V) have no alias and pass through unchanged;
        # known broadbands are ordered first, extras keep first-seen order.
        assert phot.bands_from_filters(["R", "rp", "V", "gp"]) == ["gp", "rp", "R", "V"]

    def test_narrowbands_preserved(self):
        assert phot.bands_from_filters(["g_narrow", "Na_D"]) == ["g_narrow", "Na_D"]

    def test_dedupes_aliased_duplicates(self):
        assert phot.bands_from_filters(["g", "gp"]) == ["gp"]

    def test_empty_and_blank(self):
        assert phot.bands_from_filters([]) == []
        assert phot.bands_from_filters(["", None]) == []
