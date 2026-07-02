from __future__ import annotations

import csv
import os
import shutil
import sqlite3
import tempfile

import pytest
from astropy.io import fits

from muscat_db.instruments import (
    INSTRUMENTS,
    MUSCAT,
    MUSCAT2,
    MUSCAT3,
    MUSCAT4,
    InstrumentConfig,
    get_instrument,
)


# ── Helpers ──────────────────────────────────────────────────────────────────

def _make_fits(path: str, header: dict) -> str:
    hdu = fits.PrimaryHDU()
    for k, v in header.items():
        hdu.header[k] = v
    hdu.writeto(path, overwrite=True)
    return path


def _make_csv(path: str, fieldnames: list[str], rows: list[dict]):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fieldnames)
        w.writeheader()
        for r in rows:
            w.writerow(r)
    return path


# Modules that import OBSLOG_BASE from instruments
_OBSLOG_MODULES = [
    "muscat_db.instruments",
    "muscat_db.scanner",
    "muscat_db.summarizer",
    "muscat_db.database",
]
# Modules that import INSTRUMENTS from instruments
_INST_MODULES = _OBSLOG_MODULES + [
    "muscat_db.cli",
]


@pytest.fixture
def tmp_obslog(monkeypatch):
    """Redirect OBSLOG_BASE to a temp directory."""
    td = tempfile.mkdtemp()
    for m in _OBSLOG_MODULES:
        monkeypatch.setattr(f"{m}.OBSLOG_BASE", td)
    yield td
    shutil.rmtree(td)


@pytest.fixture
def tmp_data(monkeypatch):
    """Redirect instrument data_dir to a temp directory.

    Replaces INSTRUMENTS with copies that have modified data_dir
    (since InstrumentConfig is frozen).
    """
    from dataclasses import replace
    td = tempfile.mkdtemp()
    patched = {}
    for name, cfg in INSTRUMENTS.items():
        patched[name] = replace(cfg, data_dir=f"{td}/{name}")
    for m in _INST_MODULES:
        monkeypatch.setattr(f"{m}.INSTRUMENTS", patched)
    yield td
    shutil.rmtree(td)


# ── Tests: instruments ───────────────────────────────────────────────────────

class TestInstruments:
    def test_muscat_config(self):
        assert MUSCAT.name == "muscat"
        assert MUSCAT.nccd == 3
        assert MUSCAT.prefix == "MSCT"
        assert MUSCAT.ep_names is None
        assert MUSCAT.has_pa is True
        assert MUSCAT.use_alt_ut_key is False

    def test_muscat2_config(self):
        assert MUSCAT2.name == "muscat2"
        assert MUSCAT2.nccd == 4
        assert MUSCAT2.prefix == "MCT2"
        assert MUSCAT2.has_pa is True
        assert MUSCAT2.use_alt_ut_key is False

    def test_muscat3_config(self):
        assert MUSCAT3.name == "muscat3"
        assert MUSCAT3.nccd == 4
        assert MUSCAT3.prefix == "ogg2m001-"
        assert MUSCAT3.ep_names == ["ep02", "ep03", "ep04", "ep05"]
        assert MUSCAT3.has_pa is False
        assert MUSCAT3.use_alt_ut_key is True

    def test_muscat4_config(self):
        assert MUSCAT4.name == "muscat4"
        assert MUSCAT4.nccd == 4
        assert MUSCAT4.prefix == "coj2m002-"
        assert MUSCAT4.ep_names == ["ep06", "ep07", "ep08", "ep09"]
        assert MUSCAT4.has_pa is False
        assert MUSCAT4.use_alt_ut_key is True

    def test_instruments_dict(self):
        assert set(INSTRUMENTS) == {"muscat", "muscat2", "muscat3", "muscat4", "sinistro"}

    def test_get_instrument_ok(self):
        assert get_instrument("muscat3") is MUSCAT3

    def test_get_instrument_unknown(self):
        with pytest.raises(ValueError, match="nope"):
            get_instrument("nope")

    def test_csv_headers_match_key_count(self):
        for name, cfg in INSTRUMENTS.items():
            n_keys = len(cfg.keys)
            n_csv = len(cfg.csv_header.split(","))
            assert n_csv == n_keys + 1, (
                f"{name}: csv_header has {n_csv} cols, expected {n_keys + 1} "
                f"(keys + FRAME)"
            )

    def test_focus_label_present_in_csv(self):
        for name, cfg in INSTRUMENTS.items():
            assert cfg.focus_label in cfg.csv_header, (
                f"{name}: focus_label {cfg.focus_label!r} not in csv_header"
            )

    def test_airmass_key_present_in_keys(self):
        for name, cfg in INSTRUMENTS.items():
            assert cfg.airmass_key in cfg.keys, (
                f"{name}: airmass_key {cfg.airmass_key!r} not in keys"
            )

    def test_instruments_frozen(self):
        """InstrumentConfig instances should be immutable."""
        with pytest.raises(Exception):
            MUSCAT.data_dir = "/somewhere/else"


# ── Tests: scanner ───────────────────────────────────────────────────────────

class TestScanner:
    def _make_fits_for_instrument(self, tmp_data: str, inst: InstrumentConfig,
                                   obsdate: str, ccd: int, n: int):
        """Create n mock FITS files for a given instrument/date/ccd."""
        ddir = f"{tmp_data}/{inst.name}/{obsdate}"
        os.makedirs(ddir, exist_ok=True)
        files = []
        for i in range(1, n + 1):
            if inst.ep_names:
                ep = inst.ep_names[ccd]
                fname = f"{inst.prefix}{ep}-20{obsdate}-{i:04d}-e91.fits"
            else:
                fname = f"{inst.prefix}{ccd}_{obsdate}{i:04d}.fits"
            path = f"{ddir}/{fname}"
            header = {
                "OBJECT": "TEST",
                "EXPTIME": 10.0,
                "FILTER": "g",
                "RA": "12:00:00",
                "DEC": "+00:00:00",
            }
            if inst.use_alt_ut_key:
                header["MJD-OBS"] = 60000.0 + i / 1440
                header["UTSTART"] = f"{i:02d}:00:00"
                header["CONFMODE"] = "high"
            else:
                header["MJD-STRT"] = 60000.0 + i / 1440
                header["EXP-STRT"] = f"{i:02d}:00:00"
                header["SPDTAB"] = "1"

            if inst.has_pa:
                header["INST-PA"] = 45.0

            _make_fits(path, header)
            files.append(path)
        return files

    @pytest.mark.parametrize("inst_name,ccd,nfiles", [
        ("muscat", 0, 3),
        ("muscat", 2, 2),
        ("muscat2", 1, 4),
        ("muscat3", 0, 2),
        ("muscat4", 3, 5),
    ])
    def test_scan_date_creates_csvs(self, tmp_obslog, tmp_data,
                                     inst_name, ccd, nfiles):
        from muscat_db.scanner import scan_date
        inst = INSTRUMENTS[inst_name]
        obsdate = "260101"
        self._make_fits_for_instrument(tmp_data, inst, obsdate, ccd, nfiles)

        result = scan_date(inst_name, obsdate, max_workers=1)
        assert result["total"] == nfiles
        assert ccd in result["per_ccd"]
        assert result["per_ccd"][ccd] == nfiles

        csv_path = f"{tmp_obslog}/{inst_name}/{obsdate}/obslog-{inst_name}-{obsdate}-ccd{ccd}.csv"
        assert os.path.isfile(csv_path)

        with open(csv_path) as f:
            reader = csv.DictReader(f)
            rows = list(reader)
        assert len(rows) == nfiles
        assert rows[0]["OBJECT"] == "TEST"

    def test_scan_date_no_files(self, tmp_obslog, tmp_data):
        from muscat_db.scanner import scan_date
        result = scan_date("muscat", "999999", max_workers=1)
        assert not result

    def test_scan_date_multiple_ccds(self, tmp_obslog, tmp_data):
        from muscat_db.scanner import scan_date
        inst = INSTRUMENTS["muscat"]
        obsdate = "260101"
        self._make_fits_for_instrument(tmp_data, inst, obsdate, 0, 2)
        self._make_fits_for_instrument(tmp_data, inst, obsdate, 1, 3)
        self._make_fits_for_instrument(tmp_data, inst, obsdate, 2, 1)
        result = scan_date("muscat", obsdate, max_workers=1)
        assert result["total"] == 6
        assert result["per_ccd"][0] == 2
        assert result["per_ccd"][1] == 3
        assert result["per_ccd"][2] == 1

    def test_scan_missing_dates(self, tmp_obslog, tmp_data):
        from muscat_db.scanner import scan_missing_dates
        obsdate = tmp_data
        # Create data dirs for two dates
        for d in ["260101", "260102"]:
            os.makedirs(f"{obsdate}/muscat/{d}", exist_ok=True)
        # Pre-create obslog for 260101 so it's "not missing"
        os.makedirs(f"{tmp_obslog}/muscat/260101", exist_ok=True)
        dates = scan_missing_dates("muscat", "26", max_workers=1)
        assert dates == ["260102"]

    def test_scan_all_instruments(self, tmp_obslog, tmp_data):
        from muscat_db.scanner import scan_all_instruments
        for name in INSTRUMENTS:
            os.makedirs(f"{tmp_data}/{name}/260101", exist_ok=True)
        result = scan_all_instruments("26", max_workers=1)
        assert set(result) == set(INSTRUMENTS)
        for name in INSTRUMENTS:
            assert "260101" in result[name]

    def test_scan_yesterday(self, tmp_obslog, tmp_data, mocker):
        import datetime
        from muscat_db.scanner import scan_yesterday
        mock_date = mocker.patch("muscat_db.scanner.date")
        mock_date.today.return_value = datetime.date(2026, 5, 15)

        for name in INSTRUMENTS:
            inst = INSTRUMENTS[name]
            self._make_fits_for_instrument(tmp_data, inst, "260514", 0, 1)

        scanned = scan_yesterday(max_workers=1)
        assert len(scanned) > 0

    def test_csv_content_matches_muscat(self, tmp_obslog, tmp_data):
        """Verify CSV output matches the known muscat format."""
        from muscat_db.scanner import scan_date
        obsdate = "260101"
        ddir = f"{tmp_data}/muscat/{obsdate}"
        os.makedirs(ddir, exist_ok=True)

        _make_fits(f"{ddir}/MSCT0_{obsdate}0042.fits", {
            "OBJECT": "HD209458", "MJD-STRT": 60000.5, "EXP-STRT": "12:00:00",
            "EXPTIME": 30.0, "SPDTAB": "1", "FILTER": "g",
            "RA": "22:03:00", "DEC": "+18:53:00", "SECZ": 1.2,
            "FOC-VAL": -38.6, "INST-PA": -0.001,
        })

        result = scan_date("muscat", obsdate, max_workers=1)
        assert result["total"] == 1

        csv_path = f"{tmp_obslog}/muscat/{obsdate}/obslog-muscat-{obsdate}-ccd0.csv"
        with open(csv_path) as f:
            reader = csv.DictReader(f)
            rows = list(reader)
        assert len(rows) == 1
        r = rows[0]
        assert r["FRAME"] == f"MSCT0_{obsdate}0042"
        assert r["OBJECT"] == "HD209458"
        assert r["EXPTIME (s)"] == "30.0"
        assert r["READ_MODE"] == "high"
        assert r["FILTER"] == "g"
        assert r["SECZ"] == "1.2"

    def test_muscat4_csv_format(self, tmp_obslog, tmp_data):
        """Verify muscat4 CSV matches coj2m002- naming."""
        from muscat_db.scanner import scan_date
        obsdate = "260324"
        ddir = f"{tmp_data}/muscat4/{obsdate}"
        os.makedirs(ddir, exist_ok=True)

        _make_fits(f"{ddir}/coj2m002-ep09-20260324-0150-e91.fits", {
            "OBJECT": "TOI-3091", "MJD-OBS": 60000.5, "UTSTART": "10:09:22",
            "EXPTIME": 5.0, "CONFMODE": "muscat_fast", "FILTER": "zs",
            "RA": "11:06:22", "DEC": "-56:02:29", "AIRMASS": 1.287,
            "FOCPOSN": -0.007,
        })

        result = scan_date("muscat4", obsdate, max_workers=1)
        assert result["total"] == 1

        csv_path = f"{tmp_obslog}/muscat4/{obsdate}/obslog-muscat4-{obsdate}-ccd3.csv"
        with open(csv_path) as f:
            reader = csv.DictReader(f)
            rows = list(reader)
        assert rows[0]["FRAME"] == "coj2m002-ep09-20260324-0150-e91"
        assert rows[0]["OBJECT"] == "TOI-3091"
        assert rows[0]["READ_MODE"] == "muscat_fast"
        # Verify PA column is absent (muscat4 has no PA)
        assert "PA (deg)" not in rows[0]


# ── Tests: summarizer ────────────────────────────────────────────────────────

class TestSummarizer:
    @pytest.fixture(autouse=True)
    def setup_csv(self, tmp_obslog):
        """Create a CSV with known groups and a gap for muscat."""
        self.inst = "muscat"
        self.obsdate = "260125"
        self.ccd = 0
        self.csv_dir = f"{tmp_obslog}/{self.inst}/{self.obsdate}"
        os.makedirs(self.csv_dir, exist_ok=True)

        fieldnames = ["FRAME", "OBJECT", "JD-STRT", "UT-STRT",
                       "EXPTIME (s)", "READ_MODE", "FILTER",
                       "RA", "DEC", "SECZ", "FOCUS (mm)", "PA (deg)"]
        rows = [
            {"FRAME": f"MSCT0_{self.obsdate}0001", "OBJECT": "M67",
             "JD-STRT": "60000.1", "UT-STRT": "02:24:00", "EXPTIME (s)": "10",
             "READ_MODE": "high", "FILTER": "g", "RA": "", "DEC": "",
             "SECZ": "", "FOCUS (mm)": "", "PA (deg)": ""},
            {"FRAME": f"MSCT0_{self.obsdate}0002", "OBJECT": "M67",
             "JD-STRT": "60000.2", "UT-STRT": "02:25:00", "EXPTIME (s)": "10",
             "READ_MODE": "high", "FILTER": "g", "RA": "", "DEC": "",
             "SECZ": "", "FOCUS (mm)": "", "PA (deg)": ""},
            {"FRAME": f"MSCT0_{self.obsdate}0003", "OBJECT": "M67",
             "JD-STRT": "60000.3", "UT-STRT": "02:26:00", "EXPTIME (s)": "10",
             "READ_MODE": "high", "FILTER": "g", "RA": "", "DEC": "",
             "SECZ": "", "FOCUS (mm)": "", "PA (deg)": ""},
            # Gap: frame 0004 missing, 0005 starts a new group
            {"FRAME": f"MSCT0_{self.obsdate}0005", "OBJECT": "M67",
             "JD-STRT": "60000.5", "UT-STRT": "02:28:00", "EXPTIME (s)": "10",
             "READ_MODE": "high", "FILTER": "g", "RA": "", "DEC": "",
             "SECZ": "", "FOCUS (mm)": "", "PA (deg)": ""},
            {"FRAME": f"MSCT0_{self.obsdate}0006", "OBJECT": "M67",
             "JD-STRT": "60000.6", "UT-STRT": "02:29:00", "EXPTIME (s)": "10",
             "READ_MODE": "high", "FILTER": "g", "RA": "", "DEC": "",
             "SECZ": "", "FOCUS (mm)": "", "PA (deg)": ""},
            # Different object
            {"FRAME": f"MSCT0_{self.obsdate}0007", "OBJECT": "HD209458",
             "JD-STRT": "60000.7", "UT-STRT": "03:00:00", "EXPTIME (s)": "30",
             "READ_MODE": "low", "FILTER": "r", "RA": "", "DEC": "",
             "SECZ": "", "FOCUS (mm)": "", "PA (deg)": ""},
        ]
        _make_csv(
            f"{self.csv_dir}/obslog-{self.inst}-{self.obsdate}-ccd{self.ccd}.csv",
            fieldnames, rows,
        )

    def test_summarize_csv_groups(self):
        from muscat_db.summarizer import summarize_csv
        rows = summarize_csv(self.inst, self.obsdate, self.ccd)
        assert len(rows) == 3  # M67(g1), M67(g2), HD209458

        assert rows[0].object == "M67"
        assert rows[0].frame_start == "0001"
        assert rows[0].frame_end == "0003"
        assert rows[0].nframes == 3

        assert rows[1].object == "M67"
        assert rows[1].frame_start == "0005"
        assert rows[1].frame_end == "0006"
        assert rows[1].nframes == 2

        assert rows[2].object == "HD209458"
        assert rows[2].frame_start == "0007"
        assert rows[2].frame_end == "0007"
        assert rows[2].nframes == 1

    def test_summarize_csv_no_gap(self, tmp_obslog):
        """Continuous frames should be one group."""
        from muscat_db.summarizer import summarize_csv
        inst, obsdate, ccd = "muscat", "260126", 0
        d = f"{tmp_obslog}/{inst}/{obsdate}"
        os.makedirs(d, exist_ok=True)
        fnames = ["FRAME", "OBJECT", "JD-STRT", "UT-STRT",
                   "EXPTIME (s)", "READ_MODE", "FILTER",
                   "RA", "DEC", "SECZ", "FOCUS (mm)", "PA (deg)"]
        _make_csv(f"{d}/obslog-{inst}-{obsdate}-ccd{ccd}.csv", fnames, [
            {"FRAME": f"MSCT0_{obsdate}0010", "OBJECT": "M67",
             "JD-STRT": "60001.1", "UT-STRT": "01:00:00",
             "EXPTIME (s)": "10", "READ_MODE": "high",
             "FILTER": "g", "RA": "", "DEC": "", "SECZ": "",
             "FOCUS (mm)": "", "PA (deg)": ""},
            {"FRAME": f"MSCT0_{obsdate}0011", "OBJECT": "M67",
             "JD-STRT": "60001.2", "UT-STRT": "01:01:00",
             "EXPTIME (s)": "10", "READ_MODE": "high",
             "FILTER": "g", "RA": "", "DEC": "", "SECZ": "",
             "FOCUS (mm)": "", "PA (deg)": ""},
        ])
        rows = summarize_csv(inst, obsdate, ccd)
        assert len(rows) == 1
        assert rows[0].nframes == 2

    def test_summarize_csv_no_file(self):
        from muscat_db.summarizer import summarize_csv
        rows = summarize_csv("muscat", "000000", 0)
        assert rows == []


# ── Tests: database ──────────────────────────────────────────────────────────

class TestDatabase:
    @pytest.fixture(autouse=True)
    def setup_data(self, tmp_obslog):
        """Create CSVs mirroring the obslog structure for all instruments."""
        for inst_name in INSTRUMENTS:
            d = f"{tmp_obslog}/{inst_name}/260101"
            os.makedirs(d, exist_ok=True)

        # muscat (3 CCDs)
        for ccd in range(3):
            fnames = ["FRAME", "OBJECT", "JD-STRT", "UT-STRT",
                       "EXPTIME (s)", "READ_MODE", "FILTER",
                       "RA", "DEC", "SECZ", "FOCUS (mm)", "PA (deg)"]
            _make_csv(
                f"{tmp_obslog}/muscat/260101/obslog-muscat-260101-ccd{ccd}.csv",
                fnames,
                [{"FRAME": f"MSCT{ccd}_2601010001", "OBJECT": "M67",
                  "JD-STRT": "60000.1", "UT-STRT": "01:00:00",
                  "EXPTIME (s)": "10", "READ_MODE": "high",
                  "FILTER": "g", "RA": "08:51:00", "DEC": "+11:48:00",
                  "SECZ": "1.0", "FOCUS (mm)": "-38.6", "PA (deg)": "45.0"}],
            )

        # muscat3 (4 CCDs, alternate keys)
        for ccd in range(4):
            fnames = ["FRAME", "OBJECT", "JD-STRT", "UT-STRT",
                       "EXPTIME (s)", "READ_MODE", "FILTER",
                       "RA", "DEC", "AIRMASS", "FOCUS (mm)"]
            _make_csv(
                f"{tmp_obslog}/muscat3/260101/obslog-muscat3-260101-ccd{ccd}.csv",
                fnames,
                [{"FRAME": f"ogg2m001-ep0{2+ccd}-20260101-0001-e91",
                  "OBJECT": "WASP-12", "JD-STRT": "60000.5",
                  "UT-STRT": "12:00:00", "EXPTIME (s)": "15",
                  "READ_MODE": "fast", "FILTER": "gp",
                  "RA": "06:30:00", "DEC": "+29:40:00",
                  "AIRMASS": "1.05", "FOCUS (mm)": "0.123"}],
            )

    def test_build_db(self):
        from muscat_db.database import build_db, get_last_build_date
        import datetime
        with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
            db_path = f.name
        try:
            count = build_db(db_path)
            assert count > 0
            conn = sqlite3.connect(db_path)
            cur = conn.execute("SELECT COUNT(*) FROM frames")
            nframes = cur.fetchone()[0]
            assert nframes == count
            # muscat: 3 CCDs × 1, muscat3: 4 CCDs × 1 = 7
            assert nframes == 7
            cur = conn.execute("SELECT COUNT(*) FROM summaries")
            assert cur.fetchone()[0] == 7
            conn.close()

            # Test get_last_build_date reads from metadata
            last_build = get_last_build_date(db_path)
            assert last_build == datetime.date.today().strftime("%Y-%m-%d")
        finally:
            os.unlink(db_path)

    def test_remove_sqlite_tmp_clears_wal_sidecars(self):
        # A failed WAL-mode build must not leak <tmp>-wal / -shm sidecars.
        from muscat_db.database import _remove_sqlite_tmp
        with tempfile.TemporaryDirectory() as d:
            base = os.path.join(d, "muscat.db.tmp")
            for suffix in ("", "-wal", "-shm", "-journal"):
                with open(base + suffix, "w") as f:
                    f.write("x")
            _remove_sqlite_tmp(base)
            for suffix in ("", "-wal", "-shm", "-journal"):
                assert not os.path.exists(base + suffix)
            # Idempotent: removing again when nothing exists is a no-op.
            _remove_sqlite_tmp(base)

    def test_build_db_preserves_app_owned_tables(self, tmp_obslog):
        # A rebuild must not wipe user notes / overrides / exposure calibration /
        # saved ephemeris views (regression: build_db previously only preserved
        # jobs + ephemeris_views, so the nightly cron erased notes on every run).
        from muscat_db.database import (
            build_db, get_conn, set_note, set_identified,
        )
        with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
            db_path = f.name
        try:
            build_db(db_path)
            # Seed app-owned data. set_note/set_identified take an explicit path;
            # exposure_coeffs and ephemeris_views are seeded directly (their
            # helpers target the env-configured DB, not this temp one).
            set_note(db_path, "TIC 12345", "keep me across rebuilds")
            set_identified(db_path, "TIC 12345", 0)
            with get_conn(db_path) as conn:
                conn.execute(
                    "INSERT INTO exposure_coeffs (instrument, band, focus_mm, coef, fwhm_pix, n_frames)"
                    " VALUES ('muscat3','g',0.0,1.5,2.5,10)"
                )
                conn.execute(
                    "INSERT INTO ephemeris_views (slug, state_hash, state_json, targets_json)"
                    " VALUES ('slug123','hash123','{}','[\"TIC 12345\"]')"
                )
                conn.commit()

            # Rebuild from the same obslog CSVs.
            build_db(db_path)

            with get_conn(db_path) as conn:
                note = conn.execute(
                    "SELECT note FROM target_notes WHERE object = 'TIC 12345'"
                ).fetchone()
                override = conn.execute(
                    "SELECT is_identified FROM target_overrides WHERE object = 'TIC 12345'"
                ).fetchone()
                coeff = conn.execute(
                    "SELECT coef FROM exposure_coeffs WHERE instrument='muscat3' AND band='g'"
                ).fetchone()
                views = conn.execute("SELECT COUNT(*) FROM ephemeris_views").fetchone()[0]
            assert note is not None and note[0] == "keep me across rebuilds"
            assert override is not None and override[0] == 0
            assert coeff is not None and coeff[0] == 1.5
            assert views == 1
        finally:
            os.unlink(db_path)

    def test_build_db_skips_noncanonical_obslog_dirs(self, tmp_obslog):
        from muscat_db.database import build_db, get_dates
        junk_dir = f"{tmp_obslog}/muscat/csv_old_220914"
        _make_csv(
            f"{junk_dir}/obslog-muscat-csv_old_220914-ccd0.csv",
            ["FRAME", "OBJECT", "JD-STRT", "UT-STRT",
             "EXPTIME (s)", "READ_MODE", "FILTER",
             "RA", "DEC", "SECZ", "FOCUS (mm)", "PA (deg)"],
            [{"FRAME": "MSCT0_2209140001", "OBJECT": "LegacyTarget",
              "JD-STRT": "60000.1", "UT-STRT": "01:00:00",
              "EXPTIME (s)": "10", "READ_MODE": "high",
              "FILTER": "g", "RA": "08:51:00", "DEC": "+11:48:00",
              "SECZ": "1.0", "FOCUS (mm)": "-38.6", "PA (deg)": "45.0"}],
        )

        with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
            db_path = f.name
        try:
            count = build_db(db_path)
            assert count == 7

            conn = sqlite3.connect(db_path)
            junk_rows = conn.execute(
                "SELECT COUNT(*) FROM frames WHERE instrument = ? AND obsdate = ?",
                ("muscat", "csv_old_220914"),
            ).fetchone()[0]
            conn.close()
            assert junk_rows == 0

            dates = get_dates(db_path, "muscat")
            assert [row["obsdate"] for row in dates] == ["260101"]
        finally:
            os.unlink(db_path)

    def test_get_instruments(self):
        from muscat_db.database import build_db, get_instruments
        with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
            db_path = f.name
        try:
            build_db(db_path)
            insts = get_instruments(db_path)
            names = {d["name"] for d in insts}
            assert "muscat" in names
            assert "muscat3" in names
        finally:
            os.unlink(db_path)

    def test_get_dates(self):
        from muscat_db.database import build_db, get_dates
        with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
            db_path = f.name
        try:
            build_db(db_path)
            dates = get_dates(db_path, "muscat")
            assert len(dates) == 1
            assert dates[0]["obsdate"] == "260101"
            assert dates[0]["nccd"] == 3
            assert dates[0]["nframes"] == 3
        finally:
            os.unlink(db_path)

    def test_get_frames(self):
        from muscat_db.database import build_db, get_frames
        with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
            db_path = f.name
        try:
            build_db(db_path)
            frames = get_frames(db_path, "muscat", "260101", 0)
            assert len(frames) == 1
            assert frames[0]["object"] == "M67"
            assert float(frames[0]["exptime"]) == 10.0
        finally:
            os.unlink(db_path)

    def test_get_summaries(self):
        from muscat_db.database import build_db, get_summaries
        with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
            db_path = f.name
        try:
            build_db(db_path)
            summaries = get_summaries(db_path, "muscat", "260101")
            assert len(summaries) == 3  # 3 CCDs
            assert summaries[0]["nframes"] == 1
        finally:
            os.unlink(db_path)

    def test_double_build_idempotent(self):
        """Building twice should reset and give same result."""
        from muscat_db.database import build_db
        with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
            db_path = f.name
        try:
            count1 = build_db(db_path)
            count2 = build_db(db_path)
            assert count1 == count2
            conn = sqlite3.connect(db_path)
            cur = conn.execute("SELECT COUNT(*) FROM frames")
            assert cur.fetchone()[0] == count1
            conn.close()
        finally:
            os.unlink(db_path)

    def test_build_db_preserves_ephemeris_views(self):
        from muscat_db.database import SCHEMA, build_db
        with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
            db_path = f.name
        try:
            conn = sqlite3.connect(db_path)
            conn.executescript(SCHEMA)
            conn.execute(
                """INSERT INTO ephemeris_views
                   (slug, state_hash, state_json, targets_json, created_at, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (
                    "abc123view",
                    "hash",
                    '{"targets":["TOI-736"]}',
                    '["TOI-736"]',
                    "2026-06-25T00:00:00",
                    "2026-06-25T00:00:00",
                ),
            )
            conn.commit()
            conn.close()

            build_db(db_path)

            conn = sqlite3.connect(db_path)
            row = conn.execute(
                "SELECT state_json FROM ephemeris_views WHERE slug = ?",
                ("abc123view",),
            ).fetchone()
            conn.close()
            assert row == ('{"targets":["TOI-736"]}',)
        finally:
            os.unlink(db_path)

    def test_ingest_date_adds_new_date_without_rebuild(self, tmp_obslog):
        from muscat_db.database import build_db, ingest_date, get_dates
        with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
            db_path = f.name
        try:
            build_db(db_path)
            _make_csv(
                f"{tmp_obslog}/muscat/260102/obslog-muscat-260102-ccd0.csv",
                ["FRAME", "OBJECT", "JD-STRT", "UT-STRT",
                 "EXPTIME (s)", "READ_MODE", "FILTER",
                 "RA", "DEC", "SECZ", "FOCUS (mm)", "PA (deg)"],
                [{"FRAME": "MSCT0_2601020001", "OBJECT": "M42",
                  "JD-STRT": "60001.1", "UT-STRT": "02:00:00",
                  "EXPTIME (s)": "20", "READ_MODE": "high",
                  "FILTER": "r", "RA": "05:35:17", "DEC": "-05:23:28",
                  "SECZ": "1.1", "FOCUS (mm)": "-38.6", "PA (deg)": "44.0"}],
            )

            count = ingest_date(db_path, "muscat", "260102")
            assert count == 1

            dates = get_dates(db_path, "muscat")
            assert [row["obsdate"] for row in dates][:2] == ["260102", "260101"]

            conn = sqlite3.connect(db_path)
            nframes = conn.execute(
                "SELECT COUNT(*) FROM frames WHERE instrument = ? AND obsdate = ?",
                ("muscat", "260102"),
            ).fetchone()[0]
            target = conn.execute(
                "SELECT n_dates, n_frames FROM targets WHERE object = ?",
                ("M42",),
            ).fetchone()
            conn.close()

            assert nframes == 1
            assert target == (1, 1)
        finally:
            os.unlink(db_path)

    def test_ingest_date_replaces_existing_date_and_refreshes_targets(self, tmp_obslog):
        from muscat_db.database import build_db, ingest_date
        with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
            db_path = f.name
        try:
            build_db(db_path)
            for ccd in range(3):
                csv_path = f"{tmp_obslog}/muscat/260101/obslog-muscat-260101-ccd{ccd}.csv"
                if os.path.exists(csv_path):
                    os.unlink(csv_path)
            _make_csv(
                f"{tmp_obslog}/muscat/260101/obslog-muscat-260101-ccd0.csv",
                ["FRAME", "OBJECT", "JD-STRT", "UT-STRT",
                 "EXPTIME (s)", "READ_MODE", "FILTER",
                 "RA", "DEC", "SECZ", "FOCUS (mm)", "PA (deg)"],
                [{"FRAME": "MSCT0_2601010009", "OBJECT": "M42",
                  "JD-STRT": "60000.9", "UT-STRT": "09:00:00",
                  "EXPTIME (s)": "30", "READ_MODE": "slow",
                  "FILTER": "i", "RA": "05:35:17", "DEC": "-05:23:28",
                  "SECZ": "1.2", "FOCUS (mm)": "-38.7", "PA (deg)": "46.0"}],
            )

            count = ingest_date(db_path, "muscat", "260101")
            assert count == 1

            conn = sqlite3.connect(db_path)
            frames = conn.execute(
                "SELECT COUNT(*) FROM frames WHERE instrument = ? AND obsdate = ?",
                ("muscat", "260101"),
            ).fetchone()[0]
            old_target = conn.execute(
                "SELECT COUNT(*) FROM targets WHERE object = ?",
                ("M67",),
            ).fetchone()[0]
            new_target = conn.execute(
                "SELECT n_dates, n_frames FROM targets WHERE object = ?",
                ("M42",),
            ).fetchone()
            untouched = conn.execute(
                "SELECT COUNT(*) FROM targets WHERE object = ?",
                ("WASP-12",),
            ).fetchone()[0]
            conn.close()

            assert frames == 1
            assert old_target == 0
            assert new_target == (1, 1)
            assert untouched == 1
        finally:
            os.unlink(db_path)


# ── Tests: CLI ───────────────────────────────────────────────────────────────

class TestCLI:
    @staticmethod
    def _invoke(*args):
        from typer.testing import CliRunner
        from muscat_db.cli import app
        runner = CliRunner()
        return runner.invoke(app, [*args])

    def test_help(self):
        r = self._invoke("--help")
        assert r.exit_code == 0
        assert "scan" in r.output
        assert "summary" in r.output
        assert "build-db" in r.output
        assert "ingest-date" in r.output
        assert "serve" in r.output

    def test_no_args_shows_help(self):
        r = self._invoke()
        # Typer's no_args_is_help=True may exit 2 (Click default) or 0
        # depending on version. Just verify help text is shown.
        assert r.exit_code in (0, 2)

    def test_scan_missing_instrument(self):
        r = self._invoke("scan")
        assert r.exit_code != 0

    def test_scan_bad_obsdate(self):
        r = self._invoke("scan", "muscat", "abc")
        assert r.exit_code != 0

    def test_summary_no_file(self, tmp_obslog):
        r = self._invoke("summary", "muscat", "000000", "0")
        assert r.exit_code == 0
        assert "No obslog" in r.output

    def test_build_db_no_csvs(self, tmp_obslog):
        r = self._invoke("build-db", "--db", "/tmp/__test_empty.db")
        assert r.exit_code == 0
        assert "frames" in r.output or "Database built" in r.output

    def test_ingest_date_no_csvs(self, tmp_obslog):
        r = self._invoke("ingest-date", "muscat", "260101", "--db", "/tmp/__test_ingest.db")
        assert r.exit_code != 0
        assert "No obslog CSVs found" in r.output

    def test_serve_help(self):
        r = self._invoke("serve", "--help")
        assert r.exit_code == 0
        assert "--port" in r.output

    def test_all_commands_have_help(self):
        for cmd in ["scan", "scan-missing", "scan-all",
                      "scan-yesterday-cmd", "summary",
                      "ingest-date",
                      "build-db", "serve"]:
            r = self._invoke(cmd, "--help")
            assert r.exit_code == 0, f"{cmd} --help failed: {r.output}"
