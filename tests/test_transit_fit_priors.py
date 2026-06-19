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
    fit._write_fit_inputs(tmp_path, INST, DATE, [], options)
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

    fit._write_fit_inputs(tmp_path, INST, DATE, [], options)
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

    fit._write_fit_inputs(tmp_path, inst, date, csvs, {"planets": "b"})
    fit_yaml = yaml.safe_load((tmp_path / "fit.yaml").read_text())

    assert list(fit_yaml["data"].keys()) == [
        "g_narrow", "Na_D", "i_narrow", "z_narrow",
    ]
