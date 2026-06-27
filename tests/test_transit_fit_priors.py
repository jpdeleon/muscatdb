"""Tests for per-planet prior selection (Gaussian vs Uniform) in transit fits.

These cover both the validation guardrails in ``validate_fit_options`` and the
``uniform`` block ``_write_fit_inputs`` writes into fit.yaml. No light-curve
CSVs are needed: ``_write_fit_inputs`` is called with an empty CSV list so it
only exercises the YAML construction.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from muscat_db import transit_fit as fit

INST = "muscat4"
DATE = "250512"
TARGET = "TOI-1234"


def _write(tmp_path: Path, options: dict) -> dict:
    fit._write_fit_inputs(tmp_path, INST, DATE, TARGET, [], options)
    return yaml.safe_load((tmp_path / "fit.yaml").read_text())


# --- validation -----------------------------------------------------------


def test_gaussian_default_validates_and_omits_uniform_block(tmp_path):
    # Arrange: a plain single-planet config with no prior selectors.
    options = {"planets": "b", "ror_b": "0.1", "ror_unc_b": "0.01"}

    # Act
    error = fit.validate_fit_options(options)
    fit_yaml = _write(tmp_path, options)

    # Assert: default prior is Gaussian, so no uniform block is emitted.
    assert error is None
    assert "uniform" not in fit_yaml


def test_uniform_prior_validates_when_not_fixed(tmp_path):
    # Uniform fields hold [low, high] bounds.
    options = {
        "planets": "b",
        "ror_prior_b": "uniform",
        "ror_b": "0.0",
        "ror_unc_b": "0.5",
        "fixed": ["u_star"],
    }

    assert fit.validate_fit_options(options) is None


def test_mixed_prior_shapes_across_planets_rejected():
    options = {
        "planets": "b,c",
        "ror_prior_b": "uniform",
        "ror_prior_c": "gaussian",
    }

    error = fit.validate_fit_options(options)

    assert error is not None
    assert "same for every planet" in error


def test_duplicate_planet_designations_rejected():
    error = fit.validate_fit_options({"planets": "b,b"})

    assert error is not None
    assert "unique" in error


def test_uniform_and_fixed_conflict_rejected():
    options = {"planets": "b", "ror_prior_b": "uniform", "fixed": ["ror"]}

    error = fit.validate_fit_options(options)

    assert error is not None
    assert "fixed" in error and "uniform" in error


def test_uniform_ror_bounds_outside_unit_interval_rejected():
    options = {
        "planets": "b",
        "ror_prior_b": "uniform",
        "ror_b": "0.0",
        "ror_unc_b": "1.5",  # high > 1
        "fixed": ["u_star"],
    }

    error = fit.validate_fit_options(options)

    assert error is not None
    assert "[0, 1]" in error


def test_uniform_inverted_bounds_rejected():
    options = {
        "planets": "b",
        "b_prior_b": "uniform",
        "b_b": "0.5",  # low
        "b_unc_b": "0.5",  # high == low -> not low < high
        "fixed": ["u_star"],
    }

    error = fit.validate_fit_options(options)

    assert error is not None
    assert "low < high" in error


@pytest.mark.parametrize(
    ("options", "message"),
    [
        (
            {"planets": "b", "period_prior_b": "uniform", "period_b": "2", "period_unc_b": ""},
            "low < high",
        ),
        (
            {"planets": "b", "period_prior_b": "uniform", "period_b": "bad", "period_unc_b": "3"},
            "low bound",
        ),
        (
            {"planets": "b", "period_prior_b": "uniform", "period_b": "0", "period_unc_b": "1"},
            "greater than 0",
        ),
        (
            {"planets": "b", "dur_prior_b": "uniform", "dur_b": "-1", "dur_unc_b": "1"},
            "greater than 0",
        ),
    ],
)
def test_uniform_bounds_validate_input_and_effective_range(options, message):
    error = fit.validate_fit_options(options)

    assert error is not None
    assert message in error


def test_invalid_prior_choice_rejected():
    options = {"planets": "b", "ror_prior_b": "lognormal"}

    error = fit.validate_fit_options(options)

    assert error is not None
    assert "gaussian or uniform" in error


@pytest.mark.parametrize(
    ("options", "message"),
    [
        ({"use_gp": "true", "gp_log_amp": "bad"}, "log_amp"),
        ({"use_gp": "true", "gp_log_scale_prior": "lognormal"}, "gaussian or uniform"),
        ({"use_gp": "true", "gp_log_amp_unc": "0"}, "greater than 0"),
        (
            {
                "use_gp": "true",
                "gp_log_scale_prior": "uniform",
                "gp_log_scale": "2",
                "gp_log_scale_unc": "1",
            },
            "low < high",
        ),
    ],
)
def test_invalid_gp_prior_rejected(options, message):
    error = fit.validate_fit_options(options)

    assert error is not None
    assert message in error


# --- fit.yaml construction -------------------------------------------------


def test_single_planet_uniform_block_is_flat_bounds(tmp_path):
    options = {
        "planets": "b",
        "ror_prior_b": "uniform",
        "ror_b": "0.0",
        "ror_unc_b": "0.5",
        "fixed": ["u_star"],
    }

    fit_yaml = _write(tmp_path, options)

    # Fields hold [low, high] directly; a single planet emits flat bounds.
    bounds = fit_yaml["uniform"]["ror"]
    assert bounds[0] == pytest.approx(0.0) and bounds[1] == pytest.approx(0.5)


def test_uniform_param_sys_yaml_uses_bound_midpoint(tmp_path):
    options = {
        "planets": "b",
        "ror_prior_b": "uniform",
        "ror_b": "0.0",
        "ror_unc_b": "0.5",
        "fixed": ["u_star"],
    }

    fit._write_fit_inputs(tmp_path, INST, DATE, TARGET, [], options)
    sys_yaml = yaml.safe_load((tmp_path / "sys.yaml").read_text())

    # Uniform bounds [0, 0.5] -> sys.yaml seed [midpoint, half-width].
    ror = sys_yaml["planets"]["b"]["ror"]
    assert ror[0] == pytest.approx(0.25) and ror[1] == pytest.approx(0.25)


def test_multi_planet_uniform_block_is_per_planet_bounds(tmp_path):
    options = {
        "planets": "b,c",
        "ror_prior_b": "uniform",
        "ror_prior_c": "uniform",
        "ror_b": "0.0",
        "ror_unc_b": "0.5",
        "ror_c": "0.0",
        "ror_unc_c": "0.3",
        "fixed": ["period", "u_star"],
    }

    fit_yaml = _write(tmp_path, options)

    bounds = fit_yaml["uniform"]["ror"]
    assert len(bounds) == 2
    assert bounds[0][0] == pytest.approx(0.0) and bounds[0][1] == pytest.approx(0.5)
    assert bounds[1][0] == pytest.approx(0.0) and bounds[1][1] == pytest.approx(0.3)


def test_multi_planet_fit_yaml_uses_timer_planet_sequence(tmp_path):
    fit_yaml = _write(tmp_path, {"planets": "b,c"})

    assert fit_yaml["planets"] == "bc"


def test_explicit_empty_fixed_list_is_preserved(tmp_path):
    fit_yaml = _write(tmp_path, {"planets": "b", "fixed": []})

    assert fit_yaml["fixed"] == []


def test_uniform_gp_bounds_are_encoded_as_center_and_width(tmp_path):
    options = {
        "planets": "b",
        "use_gp": "true",
        "gp_log_amp_prior": "uniform",
        "gp_log_amp": "-5",
        "gp_log_amp_unc": "-1",
        "gp_log_scale_prior": "uniform",
        "gp_log_scale": "-3",
        "gp_log_scale_unc": "1",
    }

    assert fit.validate_fit_options(options) is None
    gp = _write(tmp_path, options)["gp"]
    assert gp["log_amp"] == pytest.approx(-3)
    assert gp["log_amp_unc"] == pytest.approx(4)
    assert gp["log_scale"] == pytest.approx(-1)
    assert gp["log_scale_unc"] == pytest.approx(4)


def test_fixed_param_never_enters_uniform_block(tmp_path):
    # period is selected uniform but also held fixed -> validation rejects it,
    # and even if it slips through, the writer drops fixed params from uniform.
    options = {
        "planets": "b",
        "period_prior_b": "uniform",
        "period_b": "2.9",
        "period_unc_b": "3.1",
        "fixed": ["period", "u_star"],
    }

    fit_yaml = _write(tmp_path, options)

    assert "period" not in fit_yaml.get("uniform", {})


# --- band ordering --------------------------------------------------------


def test_fit_yaml_data_keys_in_canonical_band_order(tmp_path):
    # Regression: safe_dump's default sort_keys=True re-alphabetized the data
    # block, floating capital "Na_D" ahead of lowercase "g_narrow". The writer
    # must preserve the canonical g_narrow -> Na_D -> i_narrow -> z_narrow order.
    inst, date, target = "muscat3", "240122", "WASP-104"
    # Source CSVs live outside rdir so _write_fit_inputs can copy them in.
    src_dir = tmp_path / "src"
    src_dir.mkdir()
    # Create CSVs in a deliberately non-canonical order on disk.
    raw_bands = ["Na_D", "z_narrow", "g_narrow", "i_narrow"]
    csvs = []
    for band in raw_bands:
        p = src_dir / f"{target}_{inst}_{band}_{date}.csv"
        p.write_text("time,flux\n")
        csvs.append(p)

    fit._write_fit_inputs(tmp_path, inst, date, target, csvs, {"planets": "b"})
    fit_yaml = yaml.safe_load((tmp_path / "fit.yaml").read_text())

    assert list(fit_yaml["data"].keys()) == [
        "g_narrow", "Na_D", "i_narrow", "z_narrow",
    ]


def test_fit_yaml_normalizes_sinistro_site_bands(tmp_path):
    inst, date, target = "sinistro", "250710", "HIP67522"
    src_dir = tmp_path / "src"
    src_dir.mkdir()
    # Create CSVs with Sinistro site-prefixed bands:
    # cpt_gp, cpt_zs, lsc_gp, lsc_zs
    raw_files = [
        "HIP67522_sinistro_cpt_gp_250710.csv",
        "HIP67522_sinistro_cpt_zs_250710.csv",
        "HIP67522_sinistro_lsc_gp_250710.csv",
        "HIP67522_sinistro_lsc_zs_250710.csv",
    ]
    csvs = []
    for fn in raw_files:
        p = src_dir / fn
        p.write_text("time,flux\n")
        csvs.append(p)

    fit._write_fit_inputs(tmp_path, inst, date, target, csvs, {"planets": "b"})
    fit_yaml = yaml.safe_load((tmp_path / "fit.yaml").read_text())

    # The keys in 'data' should match the extracted raw bands (e.g. cpt_gp, cpt_zs)
    # but the nested 'band' fields must be mapped to their normalized equivalents:
    # cpt_gp -> g, cpt_zs -> z, lsc_gp -> g, lsc_zs -> z
    assert fit_yaml["data"]["cpt_gp"]["band"] == "g"
    assert fit_yaml["data"]["cpt_zs"]["band"] == "z"
    assert fit_yaml["data"]["lsc_gp"]["band"] == "g"
    assert fit_yaml["data"]["lsc_zs"]["band"] == "z"


def test_write_log_banner_cleans_html_refs():
    import io
    options = {
        "stellar_ref": 'Source: <a refstr="REF" href="http://url">Stellar Author</a>',
        "pl_ref": 'Source: <a refstr="REF" href="http://url">Planet Author</a>',
        "pl_ref_b": 'Source: <a refstr="REF" href="http://url">Planet B Author</a>',
        "pl_ref_c": 'Source: <span>Some text</span> with <a href="http://url">Planet C Author</a>',
    }
    logf = io.StringIO()
    fit._write_log_banner(logf, ["cmd"], options)
    log_text = logf.getvalue()

    assert "  stellar_ref: 'Source: Stellar Author (http://url)'" in log_text
    assert "  pl_ref: 'Source: Planet Author (http://url)'" in log_text
    assert "  pl_ref_b: 'Source: Planet B Author (http://url)'" in log_text
    assert "  pl_ref_c: 'Source: Some text with Planet C Author (http://url)'" in log_text


def test_bump_model_options_are_encoded_correctly(tmp_path):
    inst = "muscat"
    date = "171103"
    target = "HAT-P-45"
    src_dir = tmp_path / "src"
    src_dir.mkdir()
    p = src_dir / "HAT-P-45_muscat_zs_171103.csv"
    p.write_text("time,flux\n")
    csvs = [p]

    opts = {
        "planets": "b",
        "include_bump": "true",
        "chromatic_bump": "true",
        "bump_tcenter": "0.1,0.01; 0.5,0.02",
        "bump_tcenter_prior": "gaussian",
        "bump_width": "0.01,0.03",
        "bump_width_prior": "uniform",
        "bump_ampl": "0.01,0.001",
        "bump_ampl_prior": "gaussian",
    }

    fit._write_fit_inputs(tmp_path, inst, date, target, csvs, opts)
    fit_yaml = yaml.safe_load((tmp_path / "fit.yaml").read_text())

    assert fit_yaml["include_bump"] is True
    assert fit_yaml["chromatic_bump"] is True
    
    bump = fit_yaml["bump"]
    # 2 bumps parsed from tcenter
    assert bump["tcenter"] == [0.1, 0.5]
    assert bump["tcenter_unc"] == [0.01, 0.02]
    assert bump["tcenter_prior"] == "gaussian"
    
    # width uniform prior: [0.01, 0.03] low/high converted to center/width
    # center = (0.01 + 0.03)/2 = 0.02, width = 0.03 - 0.01 = 0.02
    assert bump["width"] == pytest.approx(0.02)
    assert bump["width_unc"] == pytest.approx(0.02)
    assert bump["width_prior"] == "uniform"

    # ampl prior: single Gaussian
    assert bump["ampl"] == pytest.approx(0.01)
    assert bump["ampl_unc"] == pytest.approx(0.001)
    assert bump["ampl_prior"] == "gaussian"


def test_flare_model_options_are_encoded_correctly(tmp_path):
    inst = "muscat"
    date = "171103"
    target = "HAT-P-45"
    src_dir = tmp_path / "src"
    src_dir.mkdir()
    p = src_dir / "HAT-P-45_muscat_zs_171103.csv"
    p.write_text("time,flux\n")
    csvs = [p]

    opts = {
        "planets": "b",
        "include_flare": "true",
        "chromatic_flare": "true",
        "flare_tpeak": "0.1,0.01; 0.5,0.02",
        "flare_tpeak_prior": "gaussian",
        "flare_fwhm": "0.01,0.03",
        "flare_fwhm_prior": "uniform",
        "flare_ampl": "0.01,0.001",
        "flare_ampl_prior": "gaussian",
    }

    fit._write_fit_inputs(tmp_path, inst, date, target, csvs, opts)
    fit_yaml = yaml.safe_load((tmp_path / "fit.yaml").read_text())

    assert fit_yaml["include_flare"] is True
    assert fit_yaml["chromatic_flare"] is True
    
    flare = fit_yaml["flare"]
    # 2 flares parsed from tpeak
    assert flare["tpeak"] == [0.1, 0.5]
    assert flare["tpeak_unc"] == [0.01, 0.02]
    assert flare["tpeak_prior"] == "gaussian"
    
    # fwhm uniform prior: [0.01, 0.03] low/high converted to center/width
    # center = (0.01 + 0.03)/2 = 0.02, width = 0.03 - 0.01 = 0.02
    assert flare["fwhm"] == pytest.approx(0.02)
    assert flare["fwhm_unc"] == pytest.approx(0.02)
    assert flare["fwhm_prior"] == "uniform"

    # ampl prior: single Gaussian
    assert flare["ampl"] == pytest.approx(0.01)
    assert flare["ampl_unc"] == pytest.approx(0.001)
    assert flare["ampl_prior"] == "gaussian"


def test_fit_basis_option_is_encoded_correctly(tmp_path):
    inst = "muscat"
    date = "171103"
    target = "HAT-P-45"
    src_dir = tmp_path / "src"
    src_dir.mkdir()
    p = src_dir / "HAT-P-45_muscat_zs_171103.csv"
    p.write_text("time,flux\n")
    csvs = [p]

    # Test standard basis
    opts = {
        "planets": "b",
        "fit_basis": "standard",
    }
    fit._write_fit_inputs(tmp_path, inst, date, target, csvs, opts)
    fit_yaml = yaml.safe_load((tmp_path / "fit.yaml").read_text())
    assert fit_yaml["fit_basis"] == "standard"

    # Test duration basis (default)
    opts_default = {
        "planets": "b",
    }
    fit._write_fit_inputs(tmp_path, inst, date, target, csvs, opts_default)
    fit_yaml_default = yaml.safe_load((tmp_path / "fit.yaml").read_text())
    assert fit_yaml_default["fit_basis"] == "duration"





