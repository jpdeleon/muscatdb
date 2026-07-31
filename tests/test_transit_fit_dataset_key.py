"""The fit.yaml dataset key is timer's legend label, not an internal id.

timer renders `data:` keys directly into the light-curve plot, with `file` and
`band` as sub-keys beneath. The key therefore has to read well in a finished
figure and stay unique.
"""

from __future__ import annotations

import yaml

from muscat_db import transit_fit as fit

_HEADER = "BJD_TDB,Flux,Err,Airmass\n"
_ROWS = "2460000.1,1.0,0.001,1.21\n2460000.2,1.0,0.001,1.22\n"


def _run(tmp_path, inst, date, target, names):
    src = tmp_path / "lc"
    src.mkdir(exist_ok=True)
    csvs = []
    for n in names:
        p = src / n
        p.write_text(_HEADER + _ROWS)
        csvs.append(p)
    rdir = tmp_path / "run"
    rdir.mkdir(exist_ok=True)
    fit._write_fit_inputs(rdir, inst, date, target, csvs, {})
    return yaml.safe_load((rdir / "fit.yaml").read_text())["data"]


def test_key_is_the_band_not_a_filename_fragment(tmp_path):
    data = _run(tmp_path, "muscat3", "260101", "TOI-1",
                ["TOI-1_muscat3_gp_260101.csv", "TOI-1_muscat3_zs_260101.csv"])
    assert set(data) == {"g", "z"}
    assert not any(".csv" in k for k in data), "key must not carry the filename"


def test_file_and_band_remain_sub_keys(tmp_path):
    data = _run(tmp_path, "muscat3", "260101", "TOI-1",
                ["TOI-1_muscat3_gp_260101.csv"])
    assert data["g"]["file"] == "TOI-1_muscat3_gp_260101.csv"
    assert data["g"]["band"] == "g"


def test_sinistro_same_band_two_sites_both_get_the_site(tmp_path):
    """validate_no_duplicate_datasets allows this, so the keys must differ and
    neither may be left bare, or the legend cannot say which site is which."""
    data = _run(tmp_path, "sinistro", "250710", "HIP67522",
                ["HIP67522_sinistro_cpt_gp_250710.csv",
                 "HIP67522_sinistro_lsc_gp_250710.csv"])
    assert set(data) == {"g_cpt", "g_lsc"}
    assert all(d["band"] == "g" for d in data.values())


def test_sinistro_single_dataset_keeps_the_plain_band(tmp_path):
    data = _run(tmp_path, "sinistro", "250710", "HIP67522",
                ["HIP67522_sinistro_cpt_gp_250710.csv"])
    assert set(data) == {"g"}


def test_keys_stay_unique_even_without_a_distinguishing_token(tmp_path):
    data = _run(tmp_path, "muscat3", "260101", "TOI-1",
                ["TOI-1_muscat3_gp_260101.csv", "TOI-1_muscat3_gp_260101_b.csv"])
    assert len(data) == 2, "a collision must not silently drop a dataset"
