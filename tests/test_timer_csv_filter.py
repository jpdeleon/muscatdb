"""The CSV handed to timer must contain only columns timer should model.

``read_afphot`` maps BJD_TDB/Flux/Err to time/flux/error and promotes every
other column to a detrending covariate, so what muscat-db copies into the run
directory decides the design matrix.
"""

from __future__ import annotations

import pathlib

from muscat_db.transit_fit import (
    _TIMER_COVARIATE_COLUMNS,
    _copy_lightcurve_for_timer,
)

# The real prose2 afphot header.
_HEADER = (
    "BJD_TDB,Flux,Err,Bkg(ADU),Airmass,Dx(pix),Dy(pix),Peak(ADU),FWHM(pix),GJD_UTC"
)
_ROW = "2460000.1,1.0,0.001,120.5,1.21,0.3,-0.2,15000,4.1,2460000.0993"


def _write(tmp_path: pathlib.Path, header: str, rows: list[str]) -> pathlib.Path:
    src = tmp_path / "in.csv"
    src.write_text("\n".join([header, *rows]) + "\n")
    return src


def _read_header(path: pathlib.Path) -> list[str]:
    return path.read_text().splitlines()[0].split(",")


def test_gjd_utc_is_dropped(tmp_path):
    """GJD_UTC is a second time axis, near-collinear with the trend block."""
    dst = tmp_path / "out.csv"
    _copy_lightcurve_for_timer(_write(tmp_path, _HEADER, [_ROW, _ROW]), dst)
    cols = _read_header(dst)
    assert "GJD_UTC" not in cols
    assert cols == ["BJD_TDB", "Flux", "Err", *_TIMER_COVARIATE_COLUMNS]


def test_unknown_columns_are_dropped_not_passed_through(tmp_path):
    """Allow-list, not drop-list: a new prose2 column must not silently become
    a covariate."""
    header = _HEADER + ",SomeNewColumn"
    dst = tmp_path / "out.csv"
    _copy_lightcurve_for_timer(_write(tmp_path, header, [_ROW + ",7"]), dst)
    assert "SomeNewColumn" not in _read_header(dst)


def test_all_nan_covariate_is_dropped(tmp_path):
    """timer's bin_df ends in dropna(), so one all-NaN column empties the fit."""
    rows = [_ROW.replace(",1.21,", ",nan,"), _ROW.replace(",1.21,", ",,")]
    dst = tmp_path / "out.csv"
    _copy_lightcurve_for_timer(_write(tmp_path, _HEADER, rows), dst)
    cols = _read_header(dst)
    assert "Airmass" not in cols
    assert "FWHM(pix)" in cols, "only the empty covariate should go"


def test_partly_populated_covariate_is_kept(tmp_path):
    rows = [_ROW.replace(",1.21,", ",,"), _ROW]
    dst = tmp_path / "out.csv"
    _copy_lightcurve_for_timer(_write(tmp_path, _HEADER, rows), dst)
    assert "Airmass" in _read_header(dst)


def test_reserved_columns_survive_even_when_empty(tmp_path):
    """Dropping Flux would be worse than letting timer report the problem."""
    dst = tmp_path / "out.csv"
    _copy_lightcurve_for_timer(
        _write(tmp_path, _HEADER, [_ROW.replace(",1.0,", ",,")]), dst
    )
    assert _read_header(dst)[:3] == ["BJD_TDB", "Flux", "Err"]


def test_unrecognized_format_is_copied_verbatim(tmp_path):
    """An unexpected header should fail inside timer, not silently here."""
    header = "time,flux,flux_err,airmass"
    dst = tmp_path / "out.csv"
    src = _write(tmp_path, header, ["1,2,3,4"])
    _copy_lightcurve_for_timer(src, dst)
    assert dst.read_text() == src.read_text()


def test_row_values_track_the_kept_columns(tmp_path):
    dst = tmp_path / "out.csv"
    _copy_lightcurve_for_timer(_write(tmp_path, _HEADER, [_ROW]), dst)
    header, row = dst.read_text().splitlines()
    assert dict(zip(header.split(","), row.split(",")))["Airmass"] == "1.21"
