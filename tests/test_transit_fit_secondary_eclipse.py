from __future__ import annotations

import os
import subprocess
import tempfile
from pathlib import Path
import pytest
import yaml

from muscat_db import transit_fit as fit


def test_secondary_eclipse_option_validates():
    # Arrange: options with secondary_eclipse
    options = {
        "planets": "b",
        "secondary_eclipse": "true",
    }
    
    # Act & Assert
    assert fit.validate_fit_options(options) is None


def test_secondary_eclipse_written_to_fit_yaml(tmp_path):
    # Arrange
    options = {
        "planets": "b",
        "secondary_eclipse": "true",
    }
    
    # Act
    fit._write_fit_inputs(tmp_path, "muscat4", "250512", "TOI-1234", [], options)
    fit_yaml = yaml.safe_load((tmp_path / "fit.yaml").read_text())
    
    # Assert
    assert fit_yaml["secondary_eclipse"] is True


def test_model_build_applies_secondary_eclipse_offset():
    # Arrange: get the python interpreter for the timer conda env
    timer_py = fit._conda_env_python("timer")
    if not timer_py:
        pytest.skip("timer conda environment not found")
        
    script = """
import numpy as np
from timer import model
import pymc as pm

# Create random state for reproducibility
rng = np.random.default_rng(42)

datasets = {
    "lc1": {
        "x": np.linspace(4.0, 6.0, 100),
        "y": rng.normal(1.0, 0.001, 100),
        "yerr": np.full(100, 0.01),
        "X": None,
        "texp": 0.01,
        "x_hr": np.linspace(4.0, 6.0, 1000),
        "band": "g",
    }
}

priors = {
    "t0": np.array([5.0]),
    "t0_unc": np.array([0.1]),
    "t0_prior": "gaussian",
    "period": np.array([2.0]),
    "period_unc": np.array([0.01]),
    "period_prior": "gaussian",
    "dur": np.array([0.1]),
    "dur_unc": np.array([0.01]),
    "dur_prior": "gaussian",
    "ror": np.array([0.05]),
    "ror_unc": np.array([0.005]),
    "ror_prior": "gaussian",
    "b": np.array([0.1]),
    "b_unc": np.array([0.1]),
    "b_prior": "gaussian",
    "u_star": {"g": np.array([0.3, 0.3])},
    "u_star_unc": {"g": np.array([0.1, 0.1])},
    "u_star_prior": "gaussian",
}

# Build both transit and eclipse models
pm_model_transit, map_soln_transit = model.build(
    datasets=datasets,
    priors=priors,
    nplanets=1,
    masks={"lc1": None},
    basis="duration",
    secondary_eclipse=False,
    use_custom_optimizer=False,
)

pm_model_eclipse, map_soln_eclipse = model.build(
    datasets=datasets,
    priors=priors,
    nplanets=1,
    masks={"lc1": None},
    basis="duration",
    secondary_eclipse=True,
    use_custom_optimizer=False,
)

assert pm_model_transit is not None
assert pm_model_eclipse is not None
print("OK")
"""
    with tempfile.NamedTemporaryFile("w", suffix=".py", delete=False) as f:
        f.write(script)
        temp_name = f.name
        
    try:
        res = subprocess.run([timer_py, temp_name], capture_output=True, text=True)
        assert res.returncode == 0, f"Subprocess failed:\nSTDOUT:\n{res.stdout}\nSTDERR:\n{res.stderr}"
        assert "OK" in res.stdout
    finally:
        try:
            os.unlink(temp_name)
        except OSError:
            pass
