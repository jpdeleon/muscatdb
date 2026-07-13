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
        (rdir / (bstem + "_cutouts.png")).write_bytes(b"\x89PNG\r\n")
        (rdir / (bstem + "_alignment.png")).write_bytes(b"\x89PNG\r\n")
        (rdir / (bstem + ".gif")).write_bytes(b"GIF89a")
        (rdir / (bstem + ".csv")).write_text(
            "BJD_TDB,Flux,Flux_Err\n2460807.84,1.0001,0.0019\n2460807.85,0.9998,0.0020\n"
        )
    return rdir


def _make_run_outputs(base: Path, run_id: str, *, inst: str = INST, date: str = DATE, target: str = TARGET) -> Path:
    """Create synthetic prose outputs inside a named photometry run dir."""
    rdir = base / inst / date / "_runs" / target.replace(" ", "") / run_id
    rdir.mkdir(parents=True)
    stem = f"{target}_{inst}_{date}"
    (rdir / (stem + "_lightcurves.png")).write_bytes(b"\x89PNG\r\n")
    (rdir / (stem + ".npz")).write_bytes(b"npz")
    (rdir / "_webrun_meta.json").write_text(
        '{"run_id":"' + run_id + '","run_name":"' + run_id.split("-")[-1] + '","site":"","mode":""}'
    )
    bstem = f"{target}_{inst}_gp_{date}"
    (rdir / (bstem + ".csv")).write_text("BJD_TDB,Flux\n1,1\n")
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

    def test_photometry_run_id_omits_default_sinistro_mode(self):
        assert phot.build_run_id("sinistro", "lsc", "central_2k_2x2", "default") == "lsc-default"
        assert phot.build_run_id("sinistro", "lsc", "full_frame", "default") == "lsc-full_frame-default"
        assert phot.build_run_id("muscat4", "", "", "default") == "default"

    def test_photometry_run_id_includes_telescope(self):
        assert (
            phot.build_run_id("sinistro", "lsc", "central_2k_2x2", "default", telescope="1m0-05")
            == "lsc-tel05-default"
        )
        assert (
            phot.build_run_id("sinistro", "lsc", "full_frame", "default", telescope="1m0-05")
            == "lsc-tel05-full_frame-default"
        )
        # Telescope is ignored (forced blank) for non-sinistro instruments.
        assert phot.build_run_id("muscat4", "", "", "default", telescope="1m0-05") == "default"

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
        assert out["summary"]["lightcurves"]["file"] == f"{TARGET}_{INST}_{DATE}_lightcurves.png"
        assert out["summary"]["lightcurves"]["version"].isdigit()
        assert out["summary"]["raw_flux"]["file"] == f"{TARGET}_{INST}_{DATE}_raw_flux.png"
        assert out["npz"] == f"{TARGET}_{INST}_{DATE}.npz"
        assert out["log"].endswith(".log")

    def test_list_outputs_reads_named_run_dir(self, prose_dir):
        _make_run_outputs(prose_dir, "default")
        out = phot.list_outputs(INST, DATE, TARGET, run_id="default")
        assert out["has_any"]
        assert out["summary"]["lightcurves"]["file"] == f"{TARGET}_{INST}_{DATE}_lightcurves.png"

    def test_list_outputs_reads_band_scoped_summary_products(self, prose_dir):
        import time
        rdir = prose_dir / INST / DATE / "_runs" / TARGET.replace(" ", "") / "subset"
        rdir.mkdir(parents=True)
        stem = f"{TARGET}_{INST}_gp_zs_{DATE}"
        (rdir / f"{TARGET}_{INST}_gp_{DATE}_lightcurves.png").write_bytes(b"\x89PNG\r\n")
        time.sleep(0.01)  # Ensure band-set file has newer mtime
        (rdir / f"{stem}_lightcurves.png").write_bytes(b"\x89PNG\r\n")
        (rdir / f"{stem}_raw_flux.png").write_bytes(b"\x89PNG\r\n")
        (rdir / f"{stem}_covariates.png").write_bytes(b"\x89PNG\r\n")
        (rdir / f"{stem}_stacks.png").write_bytes(b"\x89PNG\r\n")
        (rdir / f"{stem}.npz").write_bytes(b"npz")

        out = phot.list_outputs(INST, DATE, TARGET, run_id="subset")

        assert set(out["summary"]) == {"lightcurves", "raw_flux", "covariates", "stacks"}
        # Only the newest lightcurves file (band-set-scoped) should appear in summary_items
        assert {item["file"] for item in out["summary_items"] if item["key"] == "lightcurves"} == {
            f"{stem}_lightcurves.png",
        }
        assert out["npz"] == f"{stem}.npz"

    def test_list_photometry_runs_includes_legacy_and_named(self, prose_dir):
        _make_run_outputs(prose_dir, "default")
        runs, run_outputs = phot.list_photometry_runs(INST, DATE, TARGET)
        assert {r.run_id for r in runs} == {"", "default"}
        assert any(r.is_legacy and r.run_name == "legacy" for r in runs)
        assert None in run_outputs and "default" in run_outputs

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
        assert set(gp) == {"ref", "apertures", "cutouts", "alignment", "gif", "csv"}
        assert gp["csv"]["file"] == f"{TARGET}_{INST}_gp_{DATE}.csv"

    def test_classifies_underscored_band_names(self, monkeypatch, tmp_path):
        # Narrow-band / Johnson filters embed underscores in the band token
        # (g_narrow, Na_D, z_s); discovery must not split them on the date.
        base = tmp_path / "prose"
        base.mkdir()
        monkeypatch.setenv("MUSCAT_PROSE_DIR", str(base))
        rdir = base / "muscat2" / "250424"
        rdir.mkdir(parents=True)
        for band in ("g_narrow", "Na_D", "z_s"):
            bstem = f"TOI07147.01_muscat2_{band}_250424"
            (rdir / (bstem + ".csv")).write_text("BJD_TDB,Flux\n1,1\n")
            (rdir / (bstem + "_ref.png")).write_bytes(b"\x89PNG\r\n")
        out = phot.list_outputs("muscat2", "250424", "TOI07147.01")
        assert out["has_any"]
        assert set(out["bands"]) == {"g_narrow", "Na_D", "z_s"}
        assert set(out["bands"]["Na_D"]) == {"csv", "ref"}
        assert phot.discovered_targets("muscat2", "250424") == ["TOI07147.01"]

    def test_classifies_sinistro_site_token(self, monkeypatch, tmp_path):
        # Sinistro reduced with --site embeds the site between inst and the
        # band/date: <target>_sinistro_<site>_<date> (summary) and
        # <target>_sinistro_<site>_<band>_<date> (per-band). The site token must
        # not be mistaken for a band, and summary plots/npz must still classify.
        base = tmp_path / "prose"
        base.mkdir()
        monkeypatch.setenv("MUSCAT_PROSE_DIR", str(base))
        rdir = base / "sinistro" / "240101"
        rdir.mkdir(parents=True)
        sstem = "TOI-1234_sinistro_coj_240101"
        for suf in ("_lightcurves.png", "_covariates.png", "_stacks.png"):
            (rdir / (sstem + suf)).write_bytes(b"\x89PNG\r\n")
        (rdir / (sstem + ".npz")).write_bytes(b"npz")
        for band in ("R", "V", "B"):
            bstem = f"TOI-1234_sinistro_coj_{band}_240101"
            (rdir / (bstem + ".csv")).write_text("BJD_TDB,Flux\n1,1\n")
            (rdir / (bstem + "_ref.png")).write_bytes(b"\x89PNG\r\n")

        out = phot.list_outputs("sinistro", "240101", "TOI-1234")
        assert out["has_any"]
        assert set(out["summary"]) == {"lightcurves", "covariates", "stacks"}
        assert out["summary"]["lightcurves"]["file"] == f"{sstem}_lightcurves.png"
        assert out["npz"] == f"{sstem}.npz"
        # Band token is just the filter, not "<site>_<band>".
        assert set(out["bands"]) == {"R", "V", "B"}
        assert out["bands"]["R"]["csv"]["file"] == "TOI-1234_sinistro_coj_R_240101.csv"
        assert phot.discovered_targets("sinistro", "240101") == ["TOI-1234"]

    def test_sinistro_without_site_still_classifies(self, monkeypatch, tmp_path):
        # Legacy / unfiltered sinistro runs have no site token; the optional
        # site slot must not break the older naming.
        base = tmp_path / "prose"
        base.mkdir()
        monkeypatch.setenv("MUSCAT_PROSE_DIR", str(base))
        rdir = base / "sinistro" / "240101"
        rdir.mkdir(parents=True)
        (rdir / "TOI-1234_sinistro_240101_lightcurves.png").write_bytes(b"\x89PNG\r\n")
        (rdir / "TOI-1234_sinistro_R_240101.csv").write_text("BJD_TDB,Flux\n1,1\n")

        out = phot.list_outputs("sinistro", "240101", "TOI-1234")
        assert set(out["summary"]) == {"lightcurves"}
        assert set(out["bands"]) == {"R"}

    def test_multi_site_detects_and_filters(self, monkeypatch, tmp_path):
        # A single sinistro date+target can hold two sites with identical bands.
        # list_outputs must surface both sites and show one at a time (not a
        # newest-wins mix). Mirrors /ut2/jerome/ql/prose/sinistro/250710.
        import os
        base = tmp_path / "prose"
        base.mkdir()
        monkeypatch.setenv("MUSCAT_PROSE_DIR", str(base))
        rdir = base / "sinistro" / "250710"
        rdir.mkdir(parents=True)
        bands = ("gp", "zs")
        # Write cpt first (older), then lsc (newer) so newest-wins would pick lsc.
        for i, site in enumerate(("cpt", "lsc")):
            sstem = f"HIP67522_sinistro_{site}_250710"
            for suf in ("_lightcurves.png", "_covariates.png", "_stacks.png"):
                (rdir / (sstem + suf)).write_bytes(b"\x89PNG\r\n")
            (rdir / (sstem + ".npz")).write_bytes(b"npz")
            for b in bands:
                bstem = f"HIP67522_sinistro_{site}_{b}_250710"
                (rdir / (bstem + ".csv")).write_text("BJD_TDB,Flux\n1,1\n")
                (rdir / (bstem + "_ref.png")).write_bytes(b"\x89PNG\r\n")
            # bump mtimes so lsc (i=1) is strictly newer than cpt (i=0)
            for p in rdir.glob(f"HIP67522_sinistro_{site}_*"):
                os.utime(p, (1_000_000 + i, 1_000_000 + i))

        # Both sites detected; default view is the newest reduction (lsc).
        out = phot.list_outputs("sinistro", "250710", "HIP67522")
        assert out["sites"] == ["cpt", "lsc"]
        assert out["site"] == "lsc"
        assert out["npz"] == "HIP67522_sinistro_lsc_250710.npz"
        assert set(out["bands"]) == {"gp", "zs"}
        assert out["bands"]["gp"]["csv"]["file"] == "HIP67522_sinistro_lsc_gp_250710.csv"

        # Explicit site selection shows only that site's products.
        out_cpt = phot.list_outputs("sinistro", "250710", "HIP67522", site="cpt")
        assert out_cpt["site"] == "cpt"
        assert out_cpt["sites"] == ["cpt", "lsc"]
        assert out_cpt["npz"] == "HIP67522_sinistro_cpt_250710.npz"
        assert out_cpt["bands"]["zs"]["csv"]["file"] == "HIP67522_sinistro_cpt_zs_250710.csv"
        assert phot.discovered_targets("sinistro", "250710") == ["HIP67522"]

    def test_multi_telescope_detects_and_filters(self, monkeypatch, tmp_path):
        # A single sinistro site+date can hold products from more than one
        # physical 1m telescope (e.g. lsc has units 04, 05, 09). The telescope
        # token sits right after the site token in the filename.
        import os
        base = tmp_path / "prose"
        base.mkdir()
        monkeypatch.setenv("MUSCAT_PROSE_DIR", str(base))
        rdir = base / "sinistro" / "250710"
        rdir.mkdir(parents=True)
        bands = ("gp", "zs")
        # Write tel04 first (older), then tel05 (newer) so newest-wins would
        # pick tel05 by default.
        for i, tel in enumerate(("tel04", "tel05")):
            sstem = f"HIP67522_sinistro_lsc_{tel}_250710"
            for suf in ("_lightcurves.png", "_covariates.png", "_stacks.png"):
                (rdir / (sstem + suf)).write_bytes(b"\x89PNG\r\n")
            (rdir / (sstem + ".npz")).write_bytes(b"npz")
            for b in bands:
                bstem = f"HIP67522_sinistro_lsc_{tel}_{b}_250710"
                (rdir / (bstem + ".csv")).write_text("BJD_TDB,Flux\n1,1\n")
                (rdir / (bstem + "_ref.png")).write_bytes(b"\x89PNG\r\n")
            for p in rdir.glob(f"HIP67522_sinistro_lsc_{tel}_*"):
                os.utime(p, (1_000_000 + i, 1_000_000 + i))

        out = phot.list_outputs("sinistro", "250710", "HIP67522")
        assert out["site"] == "lsc"
        assert out["telescopes"] == ["1m0-04", "1m0-05"]
        assert out["telescope"] == "1m0-05"
        assert out["npz"] == "HIP67522_sinistro_lsc_tel05_250710.npz"
        assert out["bands"]["gp"]["csv"]["file"] == "HIP67522_sinistro_lsc_tel05_gp_250710.csv"

        out_04 = phot.list_outputs("sinistro", "250710", "HIP67522", telescope="1m0-04")
        assert out_04["telescope"] == "1m0-04"
        assert out_04["npz"] == "HIP67522_sinistro_lsc_tel04_250710.npz"
        assert out_04["bands"]["zs"]["csv"]["file"] == "HIP67522_sinistro_lsc_tel04_zs_250710.csv"

    def test_telescope_without_site_still_classifies(self, monkeypatch, tmp_path):
        # A telescope token can appear without a site token (prose's --telescope
        # without --site).
        base = tmp_path / "prose"
        base.mkdir()
        monkeypatch.setenv("MUSCAT_PROSE_DIR", str(base))
        rdir = base / "sinistro" / "240101"
        rdir.mkdir(parents=True)
        (rdir / "TOI-1234_sinistro_tel09_240101_lightcurves.png").write_bytes(b"\x89PNG\r\n")
        (rdir / "TOI-1234_sinistro_tel09_R_240101.csv").write_text("BJD_TDB,Flux\n1,1\n")

        out = phot.list_outputs("sinistro", "240101", "TOI-1234")
        assert out["site"] is None
        assert out["telescopes"] == ["1m0-09"]
        assert out["telescope"] == "1m0-09"
        assert set(out["summary"]) == {"lightcurves"}
        assert set(out["bands"]) == {"R"}

    def test_single_site_has_no_chips(self, monkeypatch, tmp_path):
        # One site only -> sites has a single entry so the template shows no chips.
        base = tmp_path / "prose"
        base.mkdir()
        monkeypatch.setenv("MUSCAT_PROSE_DIR", str(base))
        rdir = base / "sinistro" / "250710"
        rdir.mkdir(parents=True)
        sstem = "HIP67522_sinistro_lsc_250710"
        (rdir / (sstem + "_lightcurves.png")).write_bytes(b"\x89PNG\r\n")
        (rdir / "HIP67522_sinistro_lsc_gp_250710.csv").write_text("BJD_TDB,Flux\n1,1\n")
        out = phot.list_outputs("sinistro", "250710", "HIP67522")
        assert out["sites"] == ["lsc"]
        assert set(out["bands"]) == {"gp"}

    def test_multi_mode_detects_and_filters(self, monkeypatch, tmp_path):
        # prose appends ``_full`` for full_frame; central_2k_2x2 has no token.
        # Both modes on one site+date must surface as two modes and filter
        # independently without mistaking ``_full`` for a band/suffix.
        import os
        base = tmp_path / "prose"
        base.mkdir()
        monkeypatch.setenv("MUSCAT_PROSE_DIR", str(base))
        rdir = base / "sinistro" / "250710"
        rdir.mkdir(parents=True)
        # token "" -> central_2k_2x2 (older); "_full" -> full_frame (newer)
        for i, token in enumerate(("", "_full")):
            sstem = f"HIP67522_sinistro_lsc_250710{token}"
            bstem = f"HIP67522_sinistro_lsc_gp_250710{token}"
            files = [
                rdir / (sstem + "_lightcurves.png"),
                rdir / (sstem + ".npz"),
                rdir / (bstem + ".csv"),
                rdir / (bstem + "_ref.png"),
            ]
            for f in files:
                f.write_bytes(b"x")
                os.utime(f, (1_000_000 + i, 1_000_000 + i))  # _full (i=1) newest

        # Both modes detected; default view is the newest reduction (full_frame).
        out = phot.list_outputs("sinistro", "250710", "HIP67522")
        assert out["modes"] == ["central_2k_2x2", "full_frame"]
        assert out["mode"] == "full_frame"
        assert out["npz"] == "HIP67522_sinistro_lsc_250710_full.npz"
        assert out["bands"]["gp"]["csv"]["file"] == "HIP67522_sinistro_lsc_gp_250710_full.csv"
        assert out["bands"]["gp"]["csv"]["file"].count("full") == 1

        # Explicit mode selection shows only that mode's products.
        out_2x2 = phot.list_outputs("sinistro", "250710", "HIP67522", mode="central_2k_2x2")
        assert out_2x2["mode"] == "central_2k_2x2"
        assert out_2x2["npz"] == "HIP67522_sinistro_lsc_250710.npz"
        assert out_2x2["bands"]["gp"]["csv"]["file"] == "HIP67522_sinistro_lsc_gp_250710.csv"
        # central_2k_2x2 must not pick up the _full products.
        assert "full" not in out_2x2["bands"]["gp"]["csv"]["file"]

    def test_single_mode_has_no_chips(self, monkeypatch, tmp_path):
        # Only 2x2 present -> modes has one entry so the template shows no chips.
        base = tmp_path / "prose"
        base.mkdir()
        monkeypatch.setenv("MUSCAT_PROSE_DIR", str(base))
        rdir = base / "sinistro" / "250710"
        rdir.mkdir(parents=True)
        (rdir / "HIP67522_sinistro_lsc_250710_lightcurves.png").write_bytes(b"\x89PNG\r\n")
        (rdir / "HIP67522_sinistro_lsc_gp_250710.csv").write_text("BJD_TDB,Flux\n1,1\n")
        out = phot.list_outputs("sinistro", "250710", "HIP67522")
        assert out["modes"] == ["central_2k_2x2"]
        assert set(out["bands"]) == {"gp"}

    def test_classifies_ref_header(self, prose_dir):
        # prose writes the reference frame header as a <stem>_ref_header.txt
        # sidecar; it is discovered for the "view ref header" link.
        rdir = prose_dir / INST / DATE
        (rdir / f"{TARGET}_{INST}_{DATE}_ref_header.txt").write_text("SIMPLE = T\nEXPTIME = 30.0\n")
        out = phot.list_outputs(INST, DATE, TARGET)
        assert out["ref_header"] == f"{TARGET}_{INST}_{DATE}_ref_header.txt"

    def test_classifies_ref_selection(self, prose_dir):
        # --ref_select quality writes a <stem>_ref_selection.txt audit sidecar;
        # it is discovered for the "view ref selection" link.
        rdir = prose_dir / INST / DATE
        (rdir / f"{TARGET}_{INST}_{DATE}_ref_selection.txt").write_text("method: quality\n")
        out = phot.list_outputs(INST, DATE, TARGET)
        assert out["ref_selection"] == f"{TARGET}_{INST}_{DATE}_ref_selection.txt"

    def test_ref_header_follows_selected_site(self, monkeypatch, tmp_path):
        # The ref header is per-reduction, so it must track the selected site.
        base = tmp_path / "prose"
        base.mkdir()
        monkeypatch.setenv("MUSCAT_PROSE_DIR", str(base))
        rdir = base / "sinistro" / "250710"
        rdir.mkdir(parents=True)
        for site in ("cpt", "lsc"):
            stem = f"HIP67522_sinistro_{site}_250710"
            (rdir / (stem + "_ref_header.txt")).write_text(f"SITEID = {site}\n")
            (rdir / f"HIP67522_sinistro_{site}_gp_250710.csv").write_text("BJD_TDB,Flux\n1,1\n")
        assert phot.list_outputs("sinistro", "250710", "HIP67522", site="cpt")["ref_header"] \
            == "HIP67522_sinistro_cpt_250710_ref_header.txt"
        assert phot.list_outputs("sinistro", "250710", "HIP67522", site="lsc")["ref_header"] \
            == "HIP67522_sinistro_lsc_250710_ref_header.txt"

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

    def test_get_photometry_status_detects_run_scoped_only(self, prose_dir):
        """Regression: photometry written *only* to a
        ``_runs/<target>/<run_id>/`` subdir (no legacy dir) must still be
        detected. Aggregating only the legacy dir made the Targets/target pages
        report 'none' for every modern run-scoped reduction."""
        target = "RUN-ONLY"  # not seeded in the legacy dir by the fixture
        rdir = _make_run_outputs(prose_dir, "default", target=target)
        bstem = f"{target}_{INST}_gp_{DATE}"
        (rdir / (bstem + ".csv")).write_text(
            "BJD_TDB,Flux,Flux_Err\n" + "\n".join("2460807.84,1.0001,0.0019" for _ in range(20))
        )
        assert phot.get_photometry_status(INST, DATE, target) == "full"

    def test_get_photometry_status_full_wins_across_runs(self, prose_dir):
        """A full run anywhere makes the target full even if another run is only a
        stub, and the result is not clobbered by run iteration order."""
        target = "MULTI-RUN"
        # A stub run (short CSV, no full log) plus a full run (long CSV).
        stub = _make_run_outputs(prose_dir, "stub", target=target)
        (stub / "run.log").write_text("--test_run option enabled\n")
        full = _make_run_outputs(prose_dir, "full", target=target)
        bstem = f"{target}_{INST}_gp_{DATE}"
        (full / (bstem + ".csv")).write_text(
            "BJD_TDB,Flux,Flux_Err\n" + "\n".join("2460807.84,1.0001,0.0019" for _ in range(20))
        )
        assert phot.get_photometry_status(INST, DATE, target) == "full"


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
                     "--ccd_trim", "--edge_margin", "--bin_size_minutes", "--ref_band",
                     "--ref_select", "--ref_select_top_k",
                     "--aper_radii", "--no_gif", "--use_barycorrpy"):
            assert flag not in cmd
        assert "--avoid_nearby_star" in cmd
        assert cmd[cmd.index("--bands") + 1:cmd.index("--bands") + 5] == BANDS

    def test_options_are_passed_through(self, monkeypatch, tmp_path):
        monkeypatch.setenv("MUSCAT_PROSE_DIR", str(tmp_path))
        opts = {
            "bands": ["gp", "rp"],
            "ref_band": "gp",
            "refid": "3",
            "ref_select": "quality",
            "ref_select_top_k": "3",
            "aper_radii": "10,20,2",
            "annulus": "25,40",
            "aper_unit": "fwhm",
            "max_num_stars": "6",
            "min_star_separation": "12",
            "ccd_trim": "5,5",
            "edge_margin": "20",
            "make_gif": False,
            "use_barycorrpy": True,
            "gif_stride": "50",
            "nan_imputation_method": "median",
        }
        cmd = phot.build_command(INST, DATE, TARGET, opts, test_run=False)
        s = " ".join(cmd)
        assert cmd[cmd.index("--bands") + 1:cmd.index("--bands") + 3] == ["gp", "rp"]
        assert "--ref_band gp" in s
        assert "--refid 3" in s
        assert "--ref_select quality" in s
        assert "--ref_select_top_k 3" in s
        assert "--aper_radii 10,20,2" in s
        assert "--annulus 25,40" in s
        assert "--aper_unit fwhm" in s
        assert "--max_num_stars 6" in s
        assert "--ccd_trim 5,5" in s
        assert "--edge_margin 20" in s
        assert "--gif" not in cmd
        assert "--use_barycorrpy" in cmd
        assert "--gif_stride 50" in s
        assert "--nan-imputation-method median" in s

    def test_ref_select_quality_default_top_k_not_echoed(self, monkeypatch, tmp_path):
        # ref_select_top_k left at the RUN_DEFAULTS value should not be echoed
        # even when ref_select=quality is (mirrors the numeric-override-only-
        # when-changed convention used elsewhere in build_command).
        monkeypatch.setenv("MUSCAT_PROSE_DIR", str(tmp_path))
        cmd = phot.build_command(
            INST, DATE, TARGET,
            {"ref_select": "quality", "ref_select_top_k": phot.RUN_DEFAULTS["ref_select_top_k"]},
            test_run=False,
        )
        assert "--ref_select quality" in " ".join(cmd)
        assert "--ref_select_top_k" not in cmd

    def test_ref_select_position_never_echoed_even_with_custom_top_k(self, monkeypatch, tmp_path):
        # ref_select=position (default strategy) should never emit either flag,
        # even if ref_select_top_k was changed -- top_k is meaningless without
        # quality mode.
        monkeypatch.setenv("MUSCAT_PROSE_DIR", str(tmp_path))
        cmd = phot.build_command(
            INST, DATE, TARGET, {"ref_select": "position", "ref_select_top_k": "9"}, test_run=False
        )
        assert "--ref_select" not in cmd
        assert "--ref_select_top_k" not in cmd

    def test_edge_margin_zero_is_emitted_to_disable(self, monkeypatch, tmp_path):
        # 0 is a meaningful value (disable edge exclusion), distinct from the
        # blank auto default, so it must be passed through explicitly.
        monkeypatch.setenv("MUSCAT_PROSE_DIR", str(tmp_path))
        cmd = phot.build_command(INST, DATE, TARGET, {"edge_margin": "0"}, test_run=False)
        assert "--edge_margin 0" in " ".join(cmd)

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

    def test_avoid_nearby_star_blank_uses_auto_flag(self, monkeypatch, tmp_path):
        monkeypatch.setenv("MUSCAT_PROSE_DIR", str(tmp_path))
        cmd = phot.build_command(
            INST,
            DATE,
            TARGET,
            {"avoid_nearby_star_mode": "auto", "avoid_nearby_star": ""},
            test_run=False,
        )
        idx = cmd.index("--avoid_nearby_star")
        assert idx == len(cmd) - 1 or cmd[idx + 1].startswith("--")

    def test_avoid_nearby_star_custom_arcsec_is_emitted(self, monkeypatch, tmp_path):
        monkeypatch.setenv("MUSCAT_PROSE_DIR", str(tmp_path))
        cmd = phot.build_command(
            INST,
            DATE,
            TARGET,
            {"avoid_nearby_star_mode": "custom", "avoid_nearby_star": "4.5"},
            test_run=False,
        )
        assert "--avoid_nearby_star 4.5" in " ".join(cmd)

    def test_avoid_nearby_star_off_emits_nothing(self, monkeypatch, tmp_path):
        monkeypatch.setenv("MUSCAT_PROSE_DIR", str(tmp_path))
        cmd = phot.build_command(
            INST,
            DATE,
            TARGET,
            {"avoid_nearby_star_mode": "off", "avoid_nearby_star": "4.5"},
            test_run=False,
        )
        assert "--avoid_nearby_star" not in cmd

    def test_avoid_nearby_star_legacy_checkbox_migrates_to_auto(self):
        opts = phot.normalize_run_options({"bands": ["gp"], "avoid_nearby_stars": True, "avoid_nearby_star": ""})
        assert opts["avoid_nearby_star_mode"] == "auto"

    def test_avoid_nearby_star_legacy_checkbox_migrates_to_custom(self):
        opts = phot.normalize_run_options({"bands": ["gp"], "avoid_nearby_stars": True, "avoid_nearby_star": "4.5"})
        assert opts["avoid_nearby_star_mode"] == "custom"

    def test_centroid_method_default_not_echoed(self, monkeypatch, tmp_path):
        monkeypatch.setenv("MUSCAT_PROSE_DIR", str(tmp_path))
        cmd = phot.build_command(INST, DATE, TARGET, {}, test_run=False)
        assert "--centroid_method" not in cmd

    def test_centroid_method_non_default_is_emitted(self, monkeypatch, tmp_path):
        monkeypatch.setenv("MUSCAT_PROSE_DIR", str(tmp_path))
        cmd = phot.build_command(INST, DATE, TARGET, {"centroid_method": "com"}, test_run=False)
        assert "--centroid_method com" in " ".join(cmd)

    def test_validate_requires_band(self):
        assert phot.validate_run_options(phot.normalize_run_options({"bands": []}))

    def test_validate_rejects_sinistro_reference_band_for_multiband(self):
        err = phot.validate_run_options(
            phot.normalize_run_options({"bands": ["gp", "rp"], "ref_band": "gp"}),
            inst="sinistro",
        )
        assert err and "multi-band sinistro" in err.lower()

    def test_validate_allows_sinistro_reference_band_for_single_band(self):
        err = phot.validate_run_options(
            phot.normalize_run_options({"bands": ["gp"], "ref_band": "gp", "avoid_comparison_ids": "5"}),
            inst="sinistro",
        )
        assert err is None

    def test_validate_rejects_unknown_nan_imputation_method(self):
        err = phot.validate_run_options(
            phot.normalize_run_options({"bands": ["gp"], "nan_imputation_method": "bogus"})
        )
        assert err and "nan imputation method" in err.lower()

    def test_validate_rejects_unknown_centroid_method(self):
        err = phot.validate_run_options(
            phot.normalize_run_options({"bands": ["gp"], "centroid_method": "bogus"})
        )
        assert err and "centroid method" in err.lower()

    def test_validate_accepts_known_centroid_methods(self):
        for method in phot.CENTROID_METHODS:
            err = phot.validate_run_options(
                phot.normalize_run_options({"bands": ["gp"], "centroid_method": method})
            )
            assert err is None

    def test_validate_rejects_reference_band_not_selected(self):
        err = phot.validate_run_options(
            phot.normalize_run_options({"bands": ["gp"], "ref_band": "zs"}),
            inst="sinistro",
        )
        assert err and "one of the selected bands" in err.lower()

    def test_validate_rejects_bad_ref_select(self):
        err = phot.validate_run_options(
            phot.normalize_run_options({"bands": ["gp"], "ref_select": "best"})
        )
        assert err and "position" in err.lower() and "quality" in err.lower()

    def test_validate_rejects_non_positive_ref_select_top_k(self):
        err = phot.validate_run_options(
            phot.normalize_run_options({"bands": ["gp"], "ref_select_top_k": "0"})
        )
        assert err and "top-k" in err.lower()

    def test_validate_avoid_ids_require_reference_band(self):
        err = phot.validate_run_options(
            phot.normalize_run_options({"bands": ["gp"], "avoid_comparison_ids": "5"})
        )
        assert err and "requires a reference band" in err.lower()

    def test_validate_avoid_nearby_star_must_be_positive(self):
        err = phot.validate_run_options(
            phot.normalize_run_options({"bands": ["gp"], "avoid_nearby_star_mode": "custom", "avoid_nearby_star": "0"})
        )
        assert err and "> 0 arcsec" in err

    def test_validate_avoid_nearby_star_must_be_numeric(self):
        err = phot.validate_run_options(
            phot.normalize_run_options({"bands": ["gp"], "avoid_nearby_star_mode": "custom", "avoid_nearby_star": "abc"})
        )
        assert err and "must be a number" in err

    def test_validate_avoid_nearby_star_custom_requires_value(self):
        err = phot.validate_run_options(
            phot.normalize_run_options({"bands": ["gp"], "avoid_nearby_star_mode": "custom", "avoid_nearby_star": ""})
        )
        assert err and "required in custom mode" in err.lower()

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

    def test_overwrite_false_refuses_existing_products_without_deleting(
        self, monkeypatch, tmp_path
    ):
        from dataclasses import replace
        from muscat_db.instruments import INSTRUMENTS

        raw_root = tmp_path / "raw"
        (raw_root / DATE).mkdir(parents=True)
        out_root = tmp_path / "out"
        rdir = out_root / INST / DATE / "_runs" / TARGET.replace(" ", "") / "default"
        rdir.mkdir(parents=True)
        stale_csv = rdir / f"{TARGET}_{INST}_gp_{DATE}.csv"
        stale_png = rdir / f"{TARGET}_{INST}_{DATE}_lightcurves.png"
        stale_csv.write_text("old\n")
        stale_png.write_bytes(b"old")

        patched = dict(INSTRUMENTS)
        patched[INST] = replace(INSTRUMENTS[INST], data_dir=str(raw_root))
        monkeypatch.setenv("MUSCAT_PROSE_DIR", str(out_root))
        monkeypatch.setattr("muscat_db.photometry.INSTRUMENTS", patched)
        monkeypatch.setattr(phot.subprocess, "Popen", lambda *_a, **_k: pytest.fail("pipeline should not launch"))

        result = phot.start_run(
            INST,
            DATE,
            TARGET,
            options={"run_name": "", "overwrite": False},
            test_run=True,
        )

        assert result["ok"] is False
        assert "already exist" in result["error"]
        assert stale_csv.read_text() == "old\n"
        assert stale_png.read_bytes() == b"old"

    def test_overwrite_true_deletes_previous_products_before_launch(
        self, monkeypatch, tmp_path
    ):
        from dataclasses import replace
        from muscat_db.instruments import INSTRUMENTS

        class FakeProc:
            pid = os.getpid()

            def poll(self):
                return None

        class Store:
            def save(self, **_kwargs):
                pass

            def delete(self, _key):
                pass

        raw_root = tmp_path / "raw"
        (raw_root / DATE).mkdir(parents=True)
        out_root = tmp_path / "out"
        rdir = out_root / INST / DATE / "_runs" / TARGET.replace(" ", "") / "default"
        rdir.mkdir(parents=True)
        old_files = [
            rdir / f"{TARGET}_{INST}_gp_{DATE}.csv",
            rdir / f"{TARGET}_{INST}_{DATE}.npz",
            rdir / f"{TARGET}_{INST}_{DATE}_ref_header.txt",
        ]
        for p in old_files:
            p.write_text("old\n")
        other_target = out_root / INST / DATE / f"Other_{INST}_{DATE}.npz"
        other_target.write_text("keep\n")

        patched = dict(INSTRUMENTS)
        patched[INST] = replace(INSTRUMENTS[INST], data_dir=str(raw_root))
        monkeypatch.setenv("MUSCAT_PROSE_DIR", str(out_root))
        monkeypatch.setattr("muscat_db.photometry.INSTRUMENTS", patched)
        monkeypatch.setattr(phot, "get_job_store", lambda: Store())
        monkeypatch.setattr(phot.subprocess, "Popen", lambda *_a, **_k: FakeProc())

        with phot._LOCK:
            phot._JOBS.clear()
        try:
            result = phot.start_run(
                INST,
                DATE,
                TARGET,
                options={"run_name": "", "overwrite": True},
                test_run=True,
            )
            assert result["ok"] is True
            assert all(not p.exists() for p in old_files)
            assert other_target.read_text() == "keep\n"
            assert phot._run_log_path(rdir, INST, DATE, TARGET, "default").is_file()
        finally:
            with phot._LOCK:
                for job in phot._JOBS.values():
                    try:
                        job.logf.close()
                    except OSError:
                        pass
                phot._JOBS.clear()

    def test_job_status_none_when_not_started(self):
        s = phot.job_status(INST, "111111", "Nobody")
        assert s["state"] == "none"

    def test_existing_full_job_reused_before_capacity_queue(self, monkeypatch, tmp_path):
        """Same-key running jobs must not be converted to pending rows just
        because the full-job queue is at capacity."""
        from dataclasses import replace
        from muscat_db.instruments import INSTRUMENTS

        class RunningProc:
            pid = 12345

            def poll(self):
                return None

        class NoQueueStore:
            def enqueue(self, **_kwargs):
                pytest.fail("same-key running job must be reused, not queued")

        raw_root = tmp_path / "raw"
        (raw_root / DATE).mkdir(parents=True)
        monkeypatch.setenv("MUSCAT_PROSE_DIR", str(tmp_path / "out"))
        patched = dict(INSTRUMENTS)
        patched[INST] = replace(INSTRUMENTS[INST], data_dir=str(raw_root))
        monkeypatch.setattr("muscat_db.photometry.INSTRUMENTS", patched)
        monkeypatch.setattr(phot, "_count_running_full", lambda: 1)
        monkeypatch.setattr(phot, "get_job_store", lambda: NoQueueStore())

        log_path = tmp_path / "running.log"
        logf = log_path.open("w")
        key = phot.job_key(INST, DATE, TARGET, "default")
        with phot._LOCK:
            phot._JOBS.clear()
            phot._JOBS[key] = phot.Job(
                key=key,
                inst=INST,
                date=DATE,
                target=TARGET,
                cmd=["prose"],
                proc=RunningProc(),
                logf=logf,
                log_path=log_path,
                run_type="full",
                run_id="default",
                run_name="default",
            )
        try:
            result = phot.start_run(
                INST, DATE, TARGET, options={"overwrite": False}, test_run=False
            )
            assert result == {
                "ok": True,
                "key": key,
                "already_running": True,
                "run_id": "default",
            }
        finally:
            logf.close()
            with phot._LOCK:
                phot._JOBS.clear()

    def test_job_status_reports_persisted_error_when_job_gone(
        self, monkeypatch, tmp_path
    ):
        """A run popped from _JOBS (watchdog kill, server restart) must still
        report its persisted terminal state plus log tail, not a silent 'none'."""
        with phot._LOCK:
            phot._JOBS.clear()

        rdir = tmp_path / INST / DATE
        rdir.mkdir(parents=True)
        phot._run_log_path(rdir, INST, DATE, TARGET).write_text(
            "$ python -m prose.scripts.run_photometry\n"
            "Traceback (most recent call last):\n"
            "RuntimeError: pipeline blew up\n"
        )
        monkeypatch.setattr(phot, "results_dir", lambda inst, date: rdir)

        jobs = [{
            "key": f"photometry:{INST}/{DATE}/{TARGET}",
            "type": "photometry",
            "inst": INST,
            "date": DATE,
            "target": TARGET,
            "state": "error",
            "returncode": -1,
            "elapsed": 12,
            "started_at": 1.0,
            "error_desc": "watchdog: no log output for 25m",
            "run_type": "full",
            "params": "",
        }]
        monkeypatch.setattr("muscat_db.database.get_persisted_jobs", lambda: jobs)

        s = phot.job_status(INST, DATE, TARGET)
        assert s["state"] == "error"
        assert s["error_desc"] == "watchdog: no log output for 25m"
        assert "pipeline blew up" in s["log"]

    def test_terminal_state_treats_partial_failure_log_as_error(self, tmp_path):
        log = tmp_path / "_webrun.log"
        log.write_text(
            "2026-06-18 15:06:36,352 - ERROR: photometry PARTIAL FAILURE: "
            "2/4 bands reduced (156s elapsed); failed/skipped=['gp', 'rp']\n"
        )

        assert phot._terminal_job_state(0, False, log) == "error"

    def test_sync_jobs_repairs_persisted_done_partial_failure(
        self, monkeypatch, tmp_path
    ):
        with phot._LOCK:
            phot._JOBS.clear()

        rdir = tmp_path / INST / DATE
        rdir.mkdir(parents=True)
        phot._run_log_path(rdir, INST, DATE, TARGET).write_text(
            "$ python -m prose.scripts.run_photometry\n"
            "2026-06-18 15:06:36,352 - ERROR: photometry PARTIAL FAILURE: "
            "2/4 bands reduced (156s elapsed); failed/skipped=['gp', 'rp']\n"
        )
        monkeypatch.setattr(phot, "results_dir", lambda inst, date: rdir)

        jobs = [
            {
                "key": f"photometry:{INST}/{DATE}/{TARGET}",
                "type": "photometry",
                "inst": INST,
                "date": DATE,
                "target": TARGET,
                "state": "done",
                "returncode": 0,
                "elapsed": 156,
                "started_at": 1.0,
                "error_desc": "",
                "run_type": "test",
                "params": "",
            }
        ]
        saved = []

        monkeypatch.setattr("muscat_db.database.get_persisted_jobs", lambda: jobs)
        monkeypatch.setattr(
            "muscat_db.database.save_job",
            lambda **kwargs: saved.append(kwargs),
        )

        phot.sync_jobs()

        assert saved
        assert saved[0]["state"] == "error"
        assert saved[0]["returncode"] == 0
        assert "PARTIAL FAILURE" in saved[0]["error_desc"]

    def test_sync_jobs_uses_target_specific_partial_failure_log(
        self, monkeypatch, tmp_path
    ):
        with phot._LOCK:
            phot._JOBS.clear()

        rdir = tmp_path / INST / DATE
        rdir.mkdir(parents=True)
        phot._run_log_path(rdir, INST, DATE, "Other Target").write_text(
            "ERROR: photometry PARTIAL FAILURE\n"
        )
        monkeypatch.setattr(phot, "results_dir", lambda inst, date: rdir)
        jobs = [{
            "key": f"photometry:{INST}/{DATE}/{TARGET}",
            "type": "photometry",
            "inst": INST,
            "date": DATE,
            "target": TARGET,
            "state": "done",
            "returncode": 0,
            "elapsed": 10,
            "started_at": 1.0,
            "error_desc": "",
            "run_type": "test",
            "params": "",
        }]
        saved = []
        monkeypatch.setattr("muscat_db.database.get_persisted_jobs", lambda: jobs)
        monkeypatch.setattr(
            "muscat_db.database.save_job", lambda **kwargs: saved.append(kwargs)
        )

        phot.sync_jobs()

        assert saved == []

    def test_cancel_no_job(self):
        r = phot.cancel_run(INST, "222222", "Nobody")
        assert r["ok"] is False

    def test_cancel_running_job(self, monkeypatch, tmp_path):
        # Launch a harmless long-running process as the "pipeline" and cancel it.
        monkeypatch.setenv("MUSCAT_DB_PATH", str(tmp_path / "muscat.db"))
        monkeypatch.setenv("MUSCAT_PROSE_DIR", str(tmp_path / "out"))
        monkeypatch.setenv("MUSCAT_PROSE_PYTHON", "/bin/sh")
        # build_command is mocked below, so the pipeline cwd only needs to be a
        # real directory. Point it at tmp_path so the test is hermetic and does
        # not require the ext_tools/prose2 checkout (absent off-host / on CI).
        monkeypatch.setenv("MUSCAT_PROSE_PROJECT", str(tmp_path))
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
        run_id = res["run_id"]
        assert phot.job_status(INST, DATE, TARGET, run_id)["state"] in ("running", "cancelling")
        assert phot.job_status(INST, DATE, TARGET)["state"] == "none"

        cancel = phot.cancel_run(INST, DATE, TARGET, run_id)
        assert cancel["ok"] is True

        # The process should terminate; status becomes 'cancelled'.
        import time as _t
        deadline = _t.time() + 10
        state = None
        while _t.time() < deadline:
            state = phot.job_status(INST, DATE, TARGET, run_id)["state"]
            if state == "cancelled":
                break
            _t.sleep(0.2)
        assert state == "cancelled"


class _FakeProc:
    """Minimal stand-in for subprocess.Popen with a controllable poll()."""

    def __init__(self, rc: int | None = None):
        self._rc = rc
        self.pid = os.getpid()

    def poll(self):
        return self._rc

    def wait(self, timeout=None):
        return self._rc


class TestFinalizeGrace:
    """The tracked parent process can exit while prose's multiprocessing workers
    keep appending to the log. job_status must stay non-terminal (finalizing)
    while the log grows, then go terminal once the log is quiescent — so the
    photometry page's live log does not freeze at parent-exit."""

    def _make_job(self, monkeypatch, tmp_path):
        with phot._LOCK:
            phot._JOBS.clear()
        rdir = tmp_path / INST / DATE
        rdir.mkdir(parents=True)
        log = phot._run_log_path(rdir, INST, DATE, TARGET)
        log.write_text("$ run_photometry\nINFO: started\n")
        monkeypatch.setattr(phot, "results_dir", lambda inst, date: rdir)
        proc = _FakeProc(rc=None)
        key = phot.job_key(INST, DATE, TARGET)
        job = phot.Job(
            key=key, inst=INST, date=DATE, target=TARGET,
            cmd=["x"], proc=proc, logf=open(log, "a"),
            log_path=log, run_type="full",
        )
        with phot._LOCK:
            phot._JOBS[key] = job
        return job, proc, log

    def test_stays_finalizing_while_log_grows_then_terminal(
        self, monkeypatch, tmp_path
    ):
        monkeypatch.setattr(phot, "_FINALIZE_GRACE_S", 1)  # speed up the window
        _job, proc, log = self._make_job(monkeypatch, tmp_path)
        try:
            # Parent still alive -> running.
            assert phot.job_status(INST, DATE, TARGET)["state"] == "running"

            # Parent exits 0 but a worker just appended -> finalizing, not done,
            # and the freshly written line is visible in the live log.
            proc._rc = 0
            with open(log, "a") as f:
                f.write("INFO: wrote TOI-6715_apertures.png\n")
            s = phot.job_status(INST, DATE, TARGET)
            assert s["state"] == "finalizing"
            assert "_apertures.png" in s["log"]

            # A further worker line keeps it finalizing (log still growing).
            with open(log, "a") as f:
                f.write("INFO: wrote lightcurve.csv\n")
            assert phot.job_status(INST, DATE, TARGET)["state"] == "finalizing"

            # Log goes quiescent past the grace window -> terminal done, with the
            # full trailing output preserved.
            import time as _t
            _t.sleep(1.2)
            s = phot.job_status(INST, DATE, TARGET)
            assert s["state"] == "done"
            assert "lightcurve.csv" in s["log"]
        finally:
            with phot._LOCK:
                phot._JOBS.clear()

    def test_terminal_marker_shortens_finalize_window(self, monkeypatch, tmp_path):
        """Once prose logs a terminal result line, the finalize window shrinks to
        the short terminal grace even when the default is large — so a successful
        short run reloads quickly instead of waiting out the conservative window."""
        monkeypatch.setattr(phot, "_FINALIZE_GRACE_S", 600)  # huge default
        monkeypatch.setattr(phot, "_FINALIZE_GRACE_TERMINAL_S", 1)
        _job, proc, log = self._make_job(monkeypatch, tmp_path)
        try:
            proc._rc = 0
            # A non-terminal worker line + huge default -> still finalizing.
            with open(log, "a") as f:
                f.write("INFO: wrote apertures.png\n")
            assert phot.job_status(INST, DATE, TARGET)["state"] == "finalizing"

            # The terminal result line shrinks the window; freshly written so
            # still finalizing for now.
            with open(log, "a") as f:
                f.write("INFO: photometry SUCCEEDED: 1/1 bands (9s elapsed)\n")
            assert phot.job_status(INST, DATE, TARGET)["state"] == "finalizing"

            # Past the short terminal window -> terminal done despite the 600s
            # default that would otherwise still hold.
            import time as _t
            _t.sleep(1.2)
            assert phot.job_status(INST, DATE, TARGET)["state"] == "done"
        finally:
            with phot._LOCK:
                phot._JOBS.clear()

    def test_partial_failure_marker_shortens_finalize_window(
        self, monkeypatch, tmp_path
    ):
        """A PARTIAL FAILURE result line is equally terminal and must shorten the
        window too; the run then resolves to 'error' (partial run)."""
        monkeypatch.setattr(phot, "_FINALIZE_GRACE_S", 600)
        monkeypatch.setattr(phot, "_FINALIZE_GRACE_TERMINAL_S", 1)
        _job, proc, log = self._make_job(monkeypatch, tmp_path)
        try:
            proc._rc = 0
            with open(log, "a") as f:
                f.write("WARNING: photometry PARTIAL FAILURE: 1/2 bands reduced\n")
            assert phot.job_status(INST, DATE, TARGET)["state"] == "finalizing"
            import time as _t
            _t.sleep(1.2)
            assert phot.job_status(INST, DATE, TARGET)["state"] == "error"
        finally:
            with phot._LOCK:
                phot._JOBS.clear()

    def test_cancelled_job_finalizes_immediately(self, monkeypatch, tmp_path):
        # A large grace window proves Cancel bypasses the finalize gate even
        # while the log still looks fresh.
        monkeypatch.setattr(phot, "_FINALIZE_GRACE_S", 600)
        job, proc, log = self._make_job(monkeypatch, tmp_path)
        try:
            job.cancelled = True
            proc._rc = -15
            with open(log, "a") as f:
                f.write("INFO: still writing during cancel\n")
            assert phot.job_status(INST, DATE, TARGET)["state"] == "cancelled"
        finally:
            with phot._LOCK:
                phot._JOBS.clear()

    def test_sync_jobs_persists_finalizing_as_running(self, monkeypatch, tmp_path):
        """While finalizing, sync_jobs must persist the DB row as 'running' so the
        Jobs page (which reads state from the DB) stays consistent with the
        photometry page instead of flipping to a terminal state early."""
        monkeypatch.setattr(phot, "_FINALIZE_GRACE_S", 600)
        _job, proc, log = self._make_job(monkeypatch, tmp_path)
        proc._rc = 0
        with open(log, "a") as f:
            f.write("INFO: wrote something\n")  # fresh mtime -> finalizing
        saved: list[dict] = []
        monkeypatch.setattr("muscat_db.database.get_persisted_jobs", lambda: [])
        monkeypatch.setattr(
            "muscat_db.database.save_job", lambda **kw: saved.append(kw)
        )
        try:
            phot.sync_jobs()
            phot_saves = [s for s in saved if s.get("target") == TARGET]
            assert phot_saves, "expected the finalizing job to be persisted"
            assert phot_saves[-1]["state"] == "running"
            assert phot_saves[-1]["returncode"] is None
        finally:
            with phot._LOCK:
                phot._JOBS.clear()


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
        assert "MuscatRouteState.rememberPhotometry" in r.text

    def test_photometry_page_versions_artifact_urls(self, client):
        r = client.get(f"/photometry?inst={INST}&date={DATE}&target={TARGET}")
        assert r.status_code == 200
        name = f"{TARGET}_{INST}_{DATE}_lightcurves.png"
        assert f"/api/photometry/file/{INST}/{DATE}/{name}?v=" in r.text

    def test_photometry_page_versions_named_run_urls(self, client, prose_dir):
        _make_run_outputs(prose_dir, "default")
        r = client.get(f"/photometry?inst={INST}&date={DATE}&target={TARGET}&run=default")
        assert r.status_code == 200
        name = f"{TARGET}_{INST}_{DATE}_lightcurves.png"
        assert f"/api/photometry/file/{INST}/{DATE}/{TARGET}/run/default/{name}?v=" in r.text
        assert "- default" in r.text

    def test_photometry_page_hides_other_runs_on_test_run(self, client, prose_dir):
        rdir = _make_run_outputs(prose_dir, "my_test")
        rdir.joinpath("_webrun_meta.json").write_text(
            '{"run_id":"my_test","run_name":"my_test","site":"","mode":"","run_type":"test"}'
        )
        _make_run_outputs(prose_dir, "other_run")
        r = client.get(f"/photometry?inst={INST}&date={DATE}&target={TARGET}&run=my_test")
        assert r.status_code == 200
        assert "my_test" in r.text
        assert "run=other_run" not in r.text
        assert "run=__legacy__" not in r.text

    def test_run_file_route_serves_named_run_artifact(self, client, prose_dir):
        _make_run_outputs(prose_dir, "default")
        name = f"{TARGET}_{INST}_{DATE}_lightcurves.png"
        r = client.get(f"/api/photometry/file/{INST}/{DATE}/{TARGET}/run/default/{name}")
        assert r.status_code == 200

    def test_ref_header_link_and_inline_serving(self, client, prose_dir):
        # The "view ref header" link appears when the sidecar exists and the file
        # route serves it inline as text (so target=_blank opens it in a tab).
        name = f"{TARGET}_{INST}_{DATE}_ref_header.txt"
        (prose_dir / INST / DATE / name).write_text("SIMPLE = T\nEXPTIME = 30.0\nFILTER = gp\n")
        r = client.get(f"/photometry?inst={INST}&date={DATE}&target={TARGET}")
        assert "view ref header" in r.text
        assert name in r.text
        fr = client.get(f"/api/photometry/file/{INST}/{DATE}/{name}")
        assert fr.status_code == 200
        assert fr.headers["content-type"].startswith("text/plain")
        assert "attachment" not in (fr.headers.get("content-disposition") or "")
        assert "EXPTIME" in fr.text

    def test_photometry_page_empty_selectors(self, client):
        r = client.get("/photometry")
        assert r.status_code == 200
        assert "select an instrument" in r.text.lower() or "Pick an instrument" in r.text

    def test_file_route_serves_png(self, client):
        name = f"{TARGET}_{INST}_{DATE}_stacks.png"
        r = client.get(f"/api/photometry/file/{INST}/{DATE}/{name}")
        assert r.status_code == 200
        assert r.headers.get("cache-control") == "no-store, no-cache, must-revalidate, max-age=0"

    def test_file_route_serves_master_calibration(self, client, tmp_path, monkeypatch):
        raw_base = tmp_path / "data"
        monkeypatch.setenv("MUSCAT_DATA_DIR", str(raw_base))
        mdir = raw_base / f"{DATE}_calibrated"
        mdir.mkdir(parents=True, exist_ok=True)
        (mdir / "master_bias.png").write_bytes(b"\x89PNG\r\n")

        r = client.get(f"/api/photometry/file/muscat/{DATE}/master_bias.png")
        assert r.status_code == 200

    def test_file_route_rejects_bad_ext(self, client):
        r = client.get(f"/api/photometry/file/{INST}/{DATE}/evil.sh")
        assert r.status_code == 404

    def test_status_route(self, client):
        r = client.get(f"/api/photometry/status?inst={INST}&date=111111&target=Nobody")
        assert r.status_code == 200
        assert r.json()["state"] == "none"

    def test_status_batch_route(self, client):
        r = client.post("/api/photometry/status-batch", json={
            "jobs": [
                {"inst": INST, "date": "111111", "target": "Nobody", "run": ""},
                {"inst": "muscat2", "date": "220521", "target": "TOI04030.01", "run": "default"},
            ]
        })
        assert r.status_code == 200
        data = r.json()
        assert "jobs" in data
        assert len(data["jobs"]) == 2
        assert data["jobs"][0]["state"] == "none"
        assert data["jobs"][1]["state"] == "none"
        assert data["jobs"][0]["inst"] == INST
        assert data["jobs"][1]["inst"] == "muscat2"

    def test_status_batch_route_rejects_bad_input(self, client):
        r = client.post("/api/photometry/status-batch", json={"jobs": "not_a_list"})
        assert r.status_code == 400
        assert "must be a list" in r.json()["error"]

    def test_status_batch_route_rejects_oversized_batch(self, client):
        r = client.post("/api/photometry/status-batch", json={"jobs": [{}] * 101})
        assert r.status_code == 400
        assert "at most 100" in r.json()["error"]

    def test_status_batch_route_handles_non_object_entry(self, client):
        r = client.post("/api/photometry/status-batch", json={"jobs": [None]})
        assert r.status_code == 200
        assert r.json()["jobs"] == [{"error": "each job must be an object"}]

    def test_status_batch_route_rejects_long_fields(self, client):
        r = client.post("/api/photometry/status-batch", json={
            "jobs": [{"inst": INST, "date": DATE, "target": "x" * 257}],
        })
        assert r.status_code == 200
        assert r.json()["jobs"] == [{"error": "job fields are too long"}]

    def test_status_batch_route_missing_fields(self, client):
        r = client.post("/api/photometry/status-batch", json={
            "jobs": [{"inst": INST}]
        })
        assert r.status_code == 200
        data = r.json()
        assert "error" in data["jobs"][0]

    def test_run_route_rejects_missing_raw(self, client, tmp_path, monkeypatch):
        # raw data dir for date 111111 won't exist
        r = client.post("/api/photometry/run", json={
            "inst": INST, "date": "111111", "target": TARGET, "test_run": True,
        })
        assert r.status_code == 400
        assert r.json()["ok"] is False

    def test_command_route_echoes_options(self, client):
        r = client.post("/api/photometry/command", json={
            "inst": INST, "date": DATE, "target": TARGET, "test_run": False,
            "options": {"bands": ["gp"], "use_barycorrpy": True, "max_num_stars": 7},
        })
        assert r.status_code == 200
        body = r.json()
        assert body["error"] is None
        assert "--use_barycorrpy" in body["command"]
        assert "--max_num_stars 7" in body["command"]

    def test_command_route_echoes_ref_select_quality(self, client):
        r = client.post("/api/photometry/command", json={
            "inst": INST, "date": DATE, "target": TARGET, "test_run": False,
            "options": {"bands": ["gp"], "ref_select": "quality", "ref_select_top_k": 3},
        })
        assert r.status_code == 200
        body = r.json()
        assert body["error"] is None
        assert "--ref_select quality" in body["command"]
        assert "--ref_select_top_k 3" in body["command"]

    def test_command_route_reports_validation_error(self, client):
        r = client.post("/api/photometry/command", json={
            "inst": INST, "date": DATE, "target": TARGET,
            "options": {"aper_radii": "10,20,2"},  # missing annulus
        })
        assert r.status_code == 200
        assert "annulus" in r.json()["error"]

    def test_page_has_options_form(self, client):
        r = client.get(f"/photometry?inst={INST}&date={DATE}&target={TARGET}")
        assert r.status_code == 200
        for token in ("opt-ref_band", "opt-ref_select", "opt-ref_select_top_k",
                      "opt-aper_radii", "opt-max_num_stars",
                      "opt-use_barycorrpy", "Pipeline options"):
            assert token in r.text

    def test_photometry_page_sinistro_dynamic_selectors(self, client, tmp_path):
        # Site/mode run dropdowns are obslog-derived and shown only when the
        # obslog offers more than one choice for that target+date. Here two sites
        # share a single mode: the Site dropdown appears (cpt, lsc) while the
        # single-choice Mode dropdown is hidden, and values absent from the
        # obslog (other sites, full_frame) are never offered.
        import sqlite3
        db = tmp_path / "muscat.db"
        conn = sqlite3.connect(db)
        conn.executemany(
            """INSERT INTO frames (instrument, obsdate, ccd, filename, object, read_mode)
               VALUES (?, ?, ?, ?, ?, ?)""",
            [
                ("sinistro", "250710", 0, "cpt1m010-fa14-20250710-0082-e91", "HIP67522", "central_2k_2x2"),
                ("sinistro", "250710", 0, "lsc1m009-fa15-20250710-0083-e91", "HIP67522", "central_2k_2x2"),
            ],
        )
        conn.commit()
        conn.close()

        r = client.get("/photometry?inst=sinistro&date=250710&target=HIP67522")
        assert r.status_code == 200
        html = r.text
        # two sites -> Site dropdown shown with exactly the obslog sites
        assert 'id="opt-site"' in html
        assert 'value="cpt"' in html
        assert 'value="lsc"' in html
        assert 'value="coj"' not in html
        # one mode -> Mode dropdown hidden; full_frame never offered
        assert 'id="opt-mode"' not in html
        assert 'value="full_frame"' not in html

    def test_photometry_url_filter_seeds_sinistro_option(self, client, tmp_path):
        """A Sinistro telescope selected in the URL remains selected after reload."""
        import sqlite3
        conn = sqlite3.connect(tmp_path / "muscat.db")
        conn.executemany(
            """INSERT INTO frames (instrument, obsdate, ccd, filename, object, read_mode)
               VALUES (?, ?, ?, ?, ?, ?)""",
            [
                ("sinistro", "250710", 0, "lsc1m005-fa15-20250710-0082-e91", "HIP67522", "central_2k_2x2"),
                ("sinistro", "250710", 0, "lsc1m009-fa15-20250710-0083-e91", "HIP67522", "central_2k_2x2"),
            ],
        )
        conn.commit()
        conn.close()

        r = client.get(
            "/photometry?inst=sinistro&date=250710&target=HIP67522"
            "&site=lsc&telescope=1m0-05&mode=central_2k_2x2"
        )
        assert r.status_code == 200
        assert '<option value="1m0-05" selected>1m0-05</option>' in r.text

    def _insert_two_sites(self, tmp_path):
        import sqlite3
        conn = sqlite3.connect(tmp_path / "muscat.db")
        conn.executemany(
            """INSERT INTO frames (instrument, obsdate, ccd, filename, object, read_mode)
               VALUES (?, ?, ?, ?, ?, ?)""",
            [
                ("sinistro", "250710", 0, "cpt1m010-fa14-20250710-0082-e91", "HIP67522", "central_2k_2x2"),
                ("sinistro", "250710", 0, "lsc1m009-fa15-20250710-0083-e91", "HIP67522", "central_2k_2x2"),
            ],
        )
        conn.commit()
        conn.close()

    def test_sinistro_run_blocked_without_site_when_multi_site(self, client, tmp_path):
        # Two sites + no site chosen -> the run is refused (400) so prose never
        # silently merges frames from different telescopes.
        self._insert_two_sites(tmp_path)
        r = client.post("/api/photometry/run", json={
            "inst": "sinistro", "date": "250710", "target": "HIP67522",
            "test_run": True, "options": {"bands": ["gp"]},
        })
        assert r.status_code == 400
        assert "select a site" in r.json()["error"].lower()

    def test_sinistro_command_blocks_then_allows_with_site(self, client, tmp_path):
        # The command preview reports the block (which disables the run buttons)
        # until a site is selected, then clears.
        self._insert_two_sites(tmp_path)
        body = {"inst": "sinistro", "date": "250710", "target": "HIP67522", "test_run": False}
        r = client.post("/api/photometry/command", json={**body, "options": {"bands": ["gp"]}})
        assert "select a site" in (r.json()["error"] or "").lower()
        r = client.post("/api/photometry/command", json={**body, "options": {"bands": ["gp"], "site": "lsc"}})
        assert r.json()["error"] is None

    def test_sinistro_command_blocks_multiband_reference_band_even_with_site(self, client, tmp_path):
        self._insert_two_sites(tmp_path)
        r = client.post("/api/photometry/command", json={
            "inst": "sinistro", "date": "250710", "target": "HIP67522",
            "test_run": False,
            "options": {"bands": ["gp", "rp"], "site": "lsc", "ref_band": "gp"},
        })
        assert "multi-band sinistro" in (r.json()["error"] or "").lower()

    def test_sinistro_command_allows_single_band_ref_and_avoid_ids(self, client, tmp_path):
        self._insert_two_sites(tmp_path)
        r = client.post("/api/photometry/command", json={
            "inst": "sinistro", "date": "250710", "target": "HIP67522",
            "test_run": False,
            "options": {"bands": ["gp"], "site": "lsc", "ref_band": "gp", "avoid_comparison_ids": "5,7"},
        })
        data = r.json()
        assert data["error"] is None
        assert "--ref_band gp" in data["command"]
        assert "--avoid_cids 5 7" in data["command"]

    def test_sinistro_single_site_not_blocked(self, client, tmp_path):
        import sqlite3
        conn = sqlite3.connect(tmp_path / "muscat.db")
        conn.execute(
            """INSERT INTO frames (instrument, obsdate, ccd, filename, object, read_mode)
               VALUES (?, ?, ?, ?, ?, ?)""",
            ("sinistro", "250710", 0, "lsc1m009-fa15-20250710-0083-e91", "HIP67522", "central_2k_2x2"),
        )
        conn.commit()
        conn.close()
        r = client.post("/api/photometry/command", json={
            "inst": "sinistro", "date": "250710", "target": "HIP67522",
            "test_run": False, "options": {"bands": ["gp"]},
        })
        assert r.json()["error"] is None

    def test_page_has_run_and_cancel_buttons(self, client):
        r = client.get(f"/photometry?inst={INST}&date={DATE}&target={TARGET}")
        html = r.text
        assert 'id="run-test-btn"' in html
        assert 'id="run-full-btn"' in html
        assert 'id="cancel-btn"' in html
        assert "▶ Run Full Reduction (all frames)" in html

    def test_cancel_route_no_job(self, client):
        r = client.post("/api/photometry/cancel", json={
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
        i_lc = html.index('data-fig-id="lightcurves:')
        i_rf = html.index('data-fig-id="raw_flux:')
        i_sy = html.index('data-fig-id="covariates:')
        i_st = html.index('data-fig-id="stacks:')
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
        assert "MuscatRouteState.rememberTransitFit" in r.text

    def test_transit_fit_sinistro_site_mode_chips(self, client, tmp_path, mocker):
        import os
        # two sites (cpt, lsc); lsc also has a full_frame variant.
        specs = [
            ("HIP67522_sinistro_cpt_gp_250710.csv", 100),
            ("HIP67522_sinistro_lsc_gp_250710.csv", 200),
            ("HIP67522_sinistro_lsc_gp_250710_full.csv", 300),
        ]
        paths = []
        for name, t in specs:
            p = tmp_path / name
            p.write_text("BJD_TDB,Flux,Flux_Err\n1,1,0.1\n")
            os.utime(p, (1_000_000 + t, 1_000_000 + t))
            paths.append(p)
        mocker.patch("muscat_db.transit_fit.get_csv_lightcurves", return_value=paths)
        mocker.patch("muscat_db.transit_fit.get_fit_outputs", return_value=None)
        mocker.patch("muscat_db.transit_fit.list_fit_runs", return_value=[])
        mocker.patch("muscat_db.transit_fit.get_target_parameters", return_value={})
        mocker.patch("muscat_db.web._get_dates", return_value=[])
        mocker.patch("muscat_db.web._get_objects", return_value=[])
        mocker.patch("muscat_db.photometry.discovered_targets", return_value=[])

        # Default view is "all": both site chips with an All option, mixing the
        # site/mode is allowed so every lightcurve is shown for selection.
        r = client.get("/transit-fit?inst=sinistro&date=250710&target=HIP67522")
        assert r.status_code == 200
        assert "Site:" in r.text and ">all</a>" in r.text and ">cpt</a>" in r.text and ">lsc</a>" in r.text
        assert "Mode:" in r.text and ">full_frame</a>" in r.text
        assert "HIP67522_sinistro_lsc_gp_250710_full.csv" in r.text
        assert "HIP67522_sinistro_cpt_gp_250710.csv" in r.text  # all sites shown by default

        # explicit site+mode narrows the displayed lightcurves
        r2 = client.get("/transit-fit?inst=sinistro&date=250710&target=HIP67522&site=lsc&mode=central_2k_2x2")
        assert 'data-csv-name="HIP67522_sinistro_lsc_gp_250710.csv"' in r2.text
        assert "HIP67522_sinistro_lsc_gp_250710_full.csv" not in r2.text
        assert "HIP67522_sinistro_cpt_gp_250710.csv" not in r2.text

    def test_transit_fit_file_rejects_bad_target(self, client):
        r = client.get("/api/transit-fit/file/muscat3/250717/evil..target/timer-fit.log")
        assert r.status_code == 400

    def test_transit_fit_log_rejects_bad_target(self, client):
        r = client.get("/api/jobs/log/transit_fit/muscat3/250717/evil..target")
        assert r.status_code == 404

    def test_transit_fit_query_archive_success(self, client, mocker):
        import httpx

        mock_response = httpx.Response(
            200,
            content=b'[{"pl_name": "WASP-104 b", "st_teff": 5475.0, "st_tefferr1": 127.0, "st_tefferr2": -127.0}]',
            request=httpx.Request("GET", "https://example.invalid"),
        )
        mocker.patch("muscat_db.web._async_get", return_value=mock_response)

        r = client.get("/api/transit-fit/query-archive?target=WASP-104")
        assert r.status_code == 200
        data = r.json()
        assert data["ok"] is True
        assert data["pl_name"] == "WASP-104 b"
        assert data["params"]["teff"] == 5475.0

    def test_transit_fit_query_archive_escapes_adql_literals(self, client, mocker):
        import httpx

        seen_queries = []

        async def side_effect(url, **kwargs):
            from urllib.parse import parse_qs, urlparse
            seen_queries.append(parse_qs(urlparse(url).query).get("query", [""])[0])
            return httpx.Response(200, content=b"[]", request=httpx.Request("GET", url))

        mocker.patch("muscat_db.web._async_get", side_effect=side_effect)

        r = client.get("/api/transit-fit/query-archive", params={"target": "WASP-104' OR 'x'='x"})
        assert r.status_code == 200
        assert seen_queries
        assert "WASP-104'' OR ''x''=''x" in seen_queries[0]

    def test_transit_fit_query_archive_escapes_toi_literals(self, client, mocker):
        import httpx

        seen_queries = []

        async def side_effect(url, **kwargs):
            from urllib.parse import parse_qs, urlparse
            seen_queries.append(parse_qs(urlparse(url).query).get("query", [""])[0])
            return httpx.Response(200, content=b"[]", request=httpx.Request("GET", url))

        mocker.patch("muscat_db.web._async_get", side_effect=side_effect)

        r = client.get("/api/transit-fit/query-archive", params={"target": "TOI' OR '1'='1", "source": "toi"})
        assert r.status_code == 200
        assert seen_queries
        assert "TOI'' OR ''1''=''1" in seen_queries[0]

    def test_transit_fit_query_archive_hip_target(self, client, mocker):
        import httpx

        hip_data = b'[{"pl_name": "HIP 67522 b", "hostname": "HIP 67522", "hip_name": "HIP 67522", "st_teff": 5675.0, "st_tefferr1": 75.0, "st_tefferr2": -75.0, "st_logg": 4.0, "st_loggerr1": null, "st_loggerr2": null, "st_met": 0.0, "st_meterr1": null, "st_meterr2": null, "pl_orbper": 6.9594731, "pl_orbpererr1": 2.2e-06, "pl_orbpererr2": -2.2e-06, "pl_tranmid": 2458604.02376, "pl_tranmiderr1": 0.00033, "pl_tranmiderr2": -0.00032, "pl_trandur": 4.85, "pl_trandurerr1": 1.13, "pl_trandurerr2": -0.36, "pl_ratror": 0.06644, "pl_ratrorerr1": 0.0015, "pl_ratrorerr2": -0.0014, "pl_imppar": 0.03, "pl_impparerr1": 0.19, "pl_impparerr2": -0.22, "st_teff_reflink": "", "pl_orbper_reflink": ""}]'

        seen_urls = []

        async def side_effect(url, **kwargs):
            from urllib.parse import urlparse, parse_qs
            q = parse_qs(urlparse(url).query).get("query", [""])[0]
            seen_urls.append(q)
            if "hip_name = 'HIP 67522'" in q or "hostname = 'HIP 67522'" in q:
                content = hip_data
            else:
                content = b'[]'
            return httpx.Response(200, content=content, request=httpx.Request("GET", url))

        mocker.patch("muscat_db.web._async_get", side_effect=side_effect)

        r = client.get("/api/transit-fit/query-archive?target=HIP67522")
        assert r.status_code == 200
        data = r.json()
        assert data["ok"] is True
        assert data["pl_name"] == "HIP 67522 b"
        assert data["params"]["teff"] == 5675.0
        assert data["params"]["period"] == 6.9594731

        norm_queries = [
            u for u in seen_urls
            if "hip_name = 'HIP 67522'" in u or "hostname = 'HIP 67522'" in u
        ]
        assert len(norm_queries) >= 1, \
            f"Should have queried with space-normalized target, got: {seen_urls}"

    def test_transit_fit_query_archive_local_csv(self, client, catalog):
        # 1. Test local NASA Exoplanet Archive CSV query
        r = client.get("/api/transit-fit/query-archive?target=HIP67522")
        assert r.status_code == 200
        data = r.json()
        assert data["ok"] is True
        assert data["pl_name"] == "HIP 67522 b"
        assert data["params"]["teff"] == 5675.0
        assert data["params"]["period"] == 6.9594731
        assert data["params"]["st_ref"] != ""

        # 2. Test local TOI Catalog CSV query
        r2 = client.get("/api/transit-fit/query-archive?target=TOI-101.01&source=toi")
        assert r2.status_code == 200
        data2 = r2.json()
        assert data2["ok"] is True
        assert "TOI-101.01" in data2["pl_name"]
        assert data2["params"]["teff"] == 5600.0
        assert data2["params"]["period"] == 1.43036994965074

    def test_transit_fit_query_archive_toi_zero_padding(self, client, catalog):
        """Test that TOI queries handle zero-padding correctly (toi02688.01 != toi00688.01)."""
        # Query with zero-padded format should find TOI-101.01 (not any substring match)
        r = client.get("/api/transit-fit/query-archive?target=toi0101.01&source=toi")
        assert r.status_code == 200
        data = r.json()
        assert data["ok"] is True
        assert "TOI-101.01" in data["pl_name"]

        # Also test with different padding styles
        r2 = client.get("/api/transit-fit/query-archive?target=TOI-101.01&source=toi")
        assert r2.status_code == 200
        data2 = r2.json()
        assert data2["ok"] is True
        assert data2["pl_name"] == data["pl_name"]  # Should get same result

        r3 = client.get("/api/transit-fit/query-archive?target=toi101.01&source=toi")
        assert r3.status_code == 200
        data3 = r3.json()
        assert data3["ok"] is True
        assert data3["pl_name"] == data["pl_name"]  # Should get same result

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
        # The Jobs page reads through the job-store seam (audit C2), which routes
        # to muscat_db.database.get_persisted_jobs.
        monkeypatch.setattr("muscat_db.database.get_persisted_jobs", lambda: mock_jobs)
        monkeypatch.setattr("muscat_db.photometry.sync_jobs", lambda: None)
        monkeypatch.setattr("muscat_db.transit_fit.sync_jobs", lambda: None)
    
        r = client.get("/jobs")
        assert r.status_code == 200
        assert "Jobs" in r.text
        assert 'data-type="photometry"' in r.text
        assert 'data-type="transit_fit"' in r.text
        assert "cancelJob(this)" in r.text
        assert 'data-target="TOI-5684.01"' in r.text
        assert "TOI-5684.02" in r.text

    def test_workflow_route(self, client):
        r = client.get("/workflow")
        assert r.status_code == 200
        assert "MuSCAT-db Pipeline Workflow" in r.text
        assert "mermaid" in r.text

    def test_ephemeris_route(self, client):
        r = client.get("/ephemeris")
        assert r.status_code == 200
        assert "Ephemeris" in r.text
        assert "transit timing variation" in r.text.lower()

    def test_api_ephemeris_targets(self, client):
        r = client.get("/api/ephemeris/targets")
        assert r.status_code == 200
        res = r.json()
        assert res["ok"] is True
        assert isinstance(res["targets"], list)

    def test_api_ephemeris_target_info(self, client, catalog):
        r = client.get("/api/ephemeris/target-info")
        assert r.status_code == 422
        
        # Test no-match behavior for a dummy target. Missing ephemerides should
        # stay empty; the scheduler must not silently use placeholder values.
        r2 = client.get("/api/ephemeris/target-info?target=test_star")
        assert r2.status_code == 200
        res = r2.json()
        assert res["ok"] is True
        assert "planets" in res
        assert "reference_ephemeris" in res
        assert "nasa_ephemeris" in res
        assert "toi_ephemeris" in res
        assert "datasets" in res
        assert res["coordinates"] is None
        assert res["planets"] == []
        assert res["reference_ephemeris"] == {}
        assert res["nasa_ephemeris"] == {}
        assert res["toi_ephemeris"] == {}

        # Test local confirmed planet query (TOI-1136)
        r3 = client.get("/api/ephemeris/target-info?target=TOI-1136")
        assert r3.status_code == 200
        res3 = r3.json()
        assert res3["ok"] is True
        ref_ephem3 = res3["reference_ephemeris"]
        nasa_ephem3 = res3["nasa_ephemeris"]
        assert "b" in ref_ephem3
        assert "c" in ref_ephem3
        assert "g" in ref_ephem3
        assert "b" in nasa_ephem3
        assert abs(nasa_ephem3["b"]["t0"] - 2458684.7) < 1e-3
        assert abs(nasa_ephem3["b"]["period"] - 4.1727) < 1e-3
        
        # Test local TOI candidate query (TOI-736)
        r4 = client.get("/api/ephemeris/target-info?target=TOI-736")
        assert r4.status_code == 200
        res4 = r4.json()
        assert res4["ok"] is True
        ref_ephem4 = res4["reference_ephemeris"]
        toi_ephem4 = res4["toi_ephemeris"]
        assert res4["coordinates"]["ra"] == pytest.approx(165.6905, rel=0, abs=1e-4)
        assert res4["coordinates"]["dec"] == pytest.approx(-16.406444444444443, rel=0, abs=1e-4)
        assert "b" in ref_ephem4
        assert "c" in ref_ephem4
        assert "b" in toi_ephem4
        assert abs(toi_ephem4["b"]["t0"] - 2458546.508066) < 1e-3
        assert abs(toi_ephem4["b"]["period"] - 4.9899175) < 1e-3

        # Test local TOI candidate query by TIC ID (TIC 181804752, resolved to confirmed LP 791-18)
        r5 = client.get("/api/ephemeris/target-info?target=TIC+181804752")
        assert r5.status_code == 200
        res5 = r5.json()
        assert res5["ok"] is True
        ref_ephem5 = res5["reference_ephemeris"]
        nasa_ephem5 = res5["nasa_ephemeris"]
        assert "b" in ref_ephem5
        assert "c" in ref_ephem5
        assert "d" in ref_ephem5
        assert "b" in nasa_ephem5
        assert abs(nasa_ephem5["b"]["t0"] - 2458774.86973) < 1e-3
        assert abs(nasa_ephem5["b"]["period"] - 0.9479981) < 1e-3
        assert abs(nasa_ephem5["c"]["t0"] - 2458546.50923) < 1e-3 or abs(nasa_ephem5["c"]["t0"] - 2458771.055182) < 1e-3
        assert abs(nasa_ephem5["c"]["period"] - 4.98991) < 1e-3 or abs(nasa_ephem5["c"]["period"] - 4.9899) < 1e-3

    def test_api_ephemeris_calculate(self, client):
        r = client.post("/api/ephemeris/calculate", json={})
        assert r.status_code == 400
        
        payload = {
            "target": "test_star",
            "planets_ephem": {
                "b": {"t0": 2459000.1, "period": 3.5}
            },
            "datasets": []
        }
        r2 = client.post("/api/ephemeris/calculate", json=payload)
        assert r2.status_code == 200
        res = r2.json()
        assert res["ok"] is True
        assert "results" in res
        assert "b" in res["results"]
        assert res["results"]["b"]["was_fit"] is False

        # Test calculation with multiple targets (list)
        payload_list = {
            "target": ["test_star", "test_star_2"],
            "planets_ephem": {
                "b": {"t0": 2459000.1, "period": 3.5}
            },
            "datasets": [
                {"target": "test_star", "instrument": "muscat", "date": "240624", "run_id": "default", "checked": True}
            ]
        }
        r3 = client.post("/api/ephemeris/calculate", json=payload_list)
        assert r3.status_code == 200
        res3 = r3.json()
        assert res3["ok"] is True

    def test_api_ephemeris_view_save_and_load(self, client):
        state = {
            "targets": ["TOI-736", "WASP-104"],
            "checked_datasets": {
                "TOI-736|muscat4|250501|run_a": True,
                "WASP-104|muscat3|240122|run_b": False,
            },
            "fit_method": "weighted",
            "x_axis": "bjd",
            "show_excluded": True,
            "show_utc": True,
            "plot_title": "TOI-736 + WASP-104",
        }

        r = client.post("/api/ephemeris/view", json={"state": state})
        assert r.status_code == 200
        res = r.json()
        assert res["ok"] is True
        assert isinstance(res["slug"], str)
        assert len(res["slug"]) >= 8

        r_repeat = client.post("/api/ephemeris/view", json={"state": state})
        assert r_repeat.status_code == 200
        assert r_repeat.json()["slug"] == res["slug"]

        r_load = client.get(f"/api/ephemeris/view/{res['slug']}")
        assert r_load.status_code == 200
        loaded = r_load.json()
        assert loaded["ok"] is True
        assert loaded["slug"] == res["slug"]
        assert loaded["state"] == state

        r_missing = client.get("/api/ephemeris/view/not_found")
        assert r_missing.status_code == 404

    def test_target_name_normalization(self):
        from muscat_db.web import _normalize_target_name
        assert _normalize_target_name("V1298Tau") == "V1298TAU"
        assert _normalize_target_name("V1298Tau_b") == "V1298TAU"
        assert _normalize_target_name("V1298Tauc") == "V1298TAU"
        assert _normalize_target_name("TOI02016.03") == "TOI2016"
        assert _normalize_target_name("TOI-4600") == "TOI4600"
        assert _normalize_target_name("TOI-6109") == "TOI6109"
        assert _normalize_target_name("TOI06109.01") == "TOI6109"
        assert _normalize_target_name("TOI06109.02") == "TOI6109"
        assert _normalize_target_name("TOI 06109 b") == "TOI6109"
        assert _normalize_target_name("HIP 67522") == "HIP67522"

    def test_target_name_normalization_does_not_reinterpret_malformed_tois(self):
        from muscat_db.web import _normalize_target_name

        assert _normalize_target_name("TOI06209-01") == "TOI0620901"
        assert _normalize_target_name("TOI2106.01--exp0") == "TOI2106.01EXP0"
        assert _normalize_target_name("TOI3915TRACK") == "TOI3915TRACK"


class TestTransitFitJobs:
    def test_sync_jobs_marks_invalid_pending_target_error(self, monkeypatch):
        from muscat_db import transit_fit as fit

        pending_job = {
            "key": "transit_fit:muscat3/250717/evil..target",
            "type": "transit_fit",
            "inst": "muscat3",
            "date": "250717",
            "target": "evil..target",
            "state": "pending",
            "started_at": 1.0,
            "params": "{}",
        }
        saved = []
        monkeypatch.setattr("muscat_db.database.get_persisted_jobs", lambda: [pending_job])
        monkeypatch.setattr("muscat_db.database.save_job", lambda **kwargs: saved.append(kwargs))
        monkeypatch.setattr(fit, "_FIT_JOBS", {})

        fit.sync_jobs()

        assert saved[-1]["state"] == "error"
        assert saved[-1]["target"] == "evil..target"
        assert saved[-1]["error_desc"] == "Invalid target"


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
        
        _write_fit_inputs(rdir, "muscat3", "260613", "target", [csv_file], options)
        
        # Verify files created
        assert (rdir / "fit.yaml").is_file()
        assert (rdir / "sys.yaml").is_file()
        assert (rdir / csv_file.name).is_file()
        
        # Load fit.yaml and verify
        with open(rdir / "fit.yaml") as f:
            fit_data = yaml.safe_load(f)
        assert fit_data["planets"] == "bc"
        
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
