"""Guards on transit-fit inputs and run-type detection.

``validate_no_duplicate_datasets`` is a science-correctness guard, not cosmetics:
fitting the same physical dataset twice double-counts those points and tightens
the posterior on a measurement that was only made once. Its notion of "same
dataset" is (site, telescope, readout mode, band), so for sinistro two files can
share a band and still be legitimately distinct.

``_detect_run_type`` decides whether a directory on disk is shown as a full or a
test run, and has to work from leftovers alone (no database row), so both of its
fallbacks matter.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from muscat_db import transit_fit as fit


# ---------------------------------------------------------------------------
# validate_no_duplicate_datasets
# ---------------------------------------------------------------------------
def test_distinct_bands_are_allowed():
    csvs = [
        Path("TOI-1_muscat3_gp_260101.csv"),
        Path("TOI-1_muscat3_rp_260101.csv"),
        Path("TOI-1_muscat3_ip_260101.csv"),
    ]
    assert fit.validate_no_duplicate_datasets("muscat3", "260101", csvs) is None


def test_same_band_twice_is_rejected():
    csvs = [
        Path("TOI-1_muscat3_gp_260101.csv"),
        Path("TOI-1_muscat3_gp_260101_v2.csv"),
    ]
    err = fit.validate_no_duplicate_datasets("muscat3", "260101", csvs)
    assert err is not None
    assert "same band" in err


def test_band_aliases_collapse_to_one_dataset():
    """'g' and 'gp' are the same band, so selecting both is still a duplicate."""
    csvs = [
        Path("TOI-1_muscat3_g_260101.csv"),
        Path("TOI-1_muscat3_gp_260101.csv"),
    ]
    assert fit.validate_no_duplicate_datasets("muscat3", "260101", csvs) is not None


def test_empty_selection_is_allowed():
    assert fit.validate_no_duplicate_datasets("muscat3", "260101", []) is None


def test_sinistro_same_band_at_different_sites_is_allowed():
    """Two sites observing the same band are genuinely independent datasets."""
    csvs = [
        Path("TOI-1_sinistro_lsc_tel05_full_frame_rp_260101.csv"),
        Path("TOI-1_sinistro_cpt_tel05_full_frame_rp_260101.csv"),
    ]
    assert fit.validate_no_duplicate_datasets("sinistro", "260101", csvs) is None


def test_sinistro_duplicate_names_the_distinguishing_fields():
    """The sinistro message must say *which* site/telescope/mode clashed, since
    band alone does not identify the dataset for that instrument."""
    name = "TOI-1_sinistro_lsc_tel05_full_frame_rp_260101.csv"
    err = fit.validate_no_duplicate_datasets("sinistro", "260101", [Path(name), Path(name)])
    assert err is not None
    assert "lsc" in err


# ---------------------------------------------------------------------------
# _detect_run_type
# ---------------------------------------------------------------------------
def test_run_type_read_from_meta_yaml(tmp_path):
    (tmp_path / "meta.yaml").write_text(yaml.safe_dump({"run_type": "test"}))
    assert fit._detect_run_type(tmp_path) == "test"


def test_run_type_falls_back_to_the_log_when_meta_is_missing(tmp_path):
    """A run predating meta.yaml is still classified from its command line."""
    (tmp_path / "timer-fit.log").write_text("$ timer-fit --test_run --target TOI-1\nrunning\n")
    assert fit._detect_run_type(tmp_path) == "test"


def test_run_type_defaults_to_full(tmp_path):
    assert fit._detect_run_type(tmp_path) == "full"
    (tmp_path / "timer-fit.log").write_text("$ timer-fit --target TOI-1\n")
    assert fit._detect_run_type(tmp_path) == "full"


def test_run_type_survives_unreadable_meta(tmp_path):
    """Malformed leftovers must not raise; they fall through to the default."""
    (tmp_path / "meta.yaml").write_text("{not: valid: yaml: at all")
    assert fit._detect_run_type(tmp_path) == "full"


@pytest.mark.parametrize("meta", [{}, {"run_type": ""}, {"other": "value"}])
def test_run_type_ignores_meta_without_a_usable_value(tmp_path, meta):
    (tmp_path / "meta.yaml").write_text(yaml.safe_dump(meta))
    assert fit._detect_run_type(tmp_path) == "full"
