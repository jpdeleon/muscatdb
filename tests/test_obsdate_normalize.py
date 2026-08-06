"""Tests for LCO DAY-OBS parsing and the obsdate normalizer.

The normalizer repairs "midnight splits": LCO stamps the observing night into
every archive filename (``...-YYYYMMDD-NNNN-e91``), but frames were historically
filed under the UTC calendar date from ``DATE_OBS``, so a night crossing 00:00
UTC lands in two directories. See the "UTC-midnight dataset split" section of
README.md.
"""

from __future__ import annotations

import shutil
import tempfile
import unittest
from pathlib import Path

from muscat_db import obsdate_normalize as norm
from muscat_db.dayobs import dayobs_from_filename


class DayobsFromFilenameTest(unittest.TestCase):
    def test_parses_lco_sinistro_filename(self):
        self.assertEqual(
            dayobs_from_filename("lsc1m004-fa03-20240417-0092-e91.fits"), "240417"
        )

    def test_parses_funpacked_and_packed_suffixes(self):
        for name in (
            "cpt1m010-fa16-20230101-0123-e91.fits",
            "cpt1m010-fa16-20230101-0123-e91.fits.fz",
            "cpt1m010-fa16-20230101-0123-e91",
        ):
            with self.subTest(name=name):
                self.assertEqual(dayobs_from_filename(name), "230101")

    def test_parses_muscat3_and_muscat4_filenames(self):
        self.assertEqual(
            dayobs_from_filename("ogg2m001-ep05-20260102-0001-e91.fits"), "260102"
        )
        self.assertEqual(
            dayobs_from_filename("coj2m002-ep06-20231231-0500-e91.fits"), "231231"
        )

    def test_returns_none_without_a_dayobs_token(self):
        # MuSCAT/MuSCAT2 names carry no LCO DAY-OBS token.
        for name in ("MCT20_2001130056.fits", "MSCT0_2401170001.fits", "", "e91.fits"):
            with self.subTest(name=name):
                self.assertIsNone(dayobs_from_filename(name))

    def test_returns_none_for_an_impossible_date(self):
        self.assertIsNone(dayobs_from_filename("lsc1m004-fa03-20241332-0092-e91.fits"))

    def test_ignores_a_non_date_eight_digit_run(self):
        # The sequence field is 4 digits; an 8-digit run that is not a valid
        # date must not be mistaken for DAY-OBS.
        self.assertEqual(
            dayobs_from_filename("lsc1m004-fa03-20240417-99999999-e91.fits"), "240417"
        )


class _TreeTestCase(unittest.TestCase):
    """Builds a throwaway raw-data + obslog tree for the normalizer."""

    def setUp(self):
        self.root = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)
        self.data_root = self.root / "data"
        self.obslog_root = self.root / "obslog"

    def _fits(self, obsdate: str, filename: str, inst: str = "sinistro") -> Path:
        subdir = {"sinistro": "Sinistro", "muscat3": "MuSCAT3", "muscat4": "MuSCAT4"}[inst]
        p = self.data_root / subdir / obsdate / filename
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(b"fake fits")
        return p

    def _csv(self, obsdate: str, ccd: int = 0, inst: str = "sinistro") -> Path:
        p = self.obslog_root / inst / obsdate / f"obslog-{inst}-{obsdate}-ccd{ccd}.csv"
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text("FRAME,OBJECT\n")
        return p

    def _plan(self, inst: str = "sinistro"):
        return norm.plan_instrument(
            inst, data_root=self.data_root, obslog_root=self.obslog_root
        )


class PlanInstrumentTest(_TreeTestCase):
    def test_clean_directory_yields_no_moves(self):
        self._fits("240418", "lsc1m004-fa03-20240418-0001-e91.fits")
        self._fits("240418", "lsc1m004-fa03-20240418-0002-e91.fits")

        plan = self._plan()

        self.assertEqual(plan.moves, ())
        self.assertEqual(plan.issues, ())

    def test_mixed_directory_moves_only_the_mismatched_files(self):
        stay = self._fits("240418", "lsc1m004-fa03-20240418-0001-e91.fits")
        move = self._fits("240418", "lsc1m004-fa03-20240417-0093-e91.fits")
        self._fits("240417", "lsc1m004-fa03-20240417-0092-e91.fits")

        plan = self._plan()

        self.assertEqual(len(plan.moves), 1)
        self.assertEqual(plan.moves[0].src, move)
        self.assertEqual(plan.moves[0].dst.parent.name, "240417")
        self.assertEqual(plan.moves[0].src_date, "240418")
        self.assertEqual(plan.moves[0].dst_date, "240417")
        self.assertTrue(stay.exists())
        self.assertEqual(sorted(plan.touched_dates), ["240417", "240418"])

    def test_pure_but_offset_directory_without_a_counterpart_is_left_alone(self):
        # Every file agrees with every other and that night has no home
        # elsewhere, so moving them would just rename the directory. Out of
        # scope: the label is referenced by photometry paths and notes.
        self._fits("210520", "ogg2m001-ep02-20210519-0001-e91.fits", inst="muscat3")
        self._fits("210520", "ogg2m001-ep02-20210519-0002-e91.fits", inst="muscat3")

        plan = self._plan("muscat3")

        self.assertEqual(plan.moves, ())
        self.assertEqual([i.reason for i in plan.issues], ["offset"])
        self.assertEqual(plan.issues[0].obsdate, "210520")

    def test_pure_directory_whose_counterpart_exists_is_a_split(self):
        # muscat3/210520 holding only 210519 frames while muscat3/210519 also
        # exists is the same night in two directories, not a relabel.
        self._fits("210519", "ogg2m001-ep02-20210519-0001-e91.fits", inst="muscat3")
        self._fits("210520", "ogg2m001-ep02-20210519-0002-e91.fits", inst="muscat3")

        plan = self._plan("muscat3")

        self.assertEqual(len(plan.moves), 1)
        self.assertEqual(plan.moves[0].src_date, "210520")
        self.assertEqual(plan.moves[0].dst_date, "210519")
        self.assertEqual(plan.issues, ())

    def test_mixed_directory_creates_a_missing_destination(self):
        # Two nights sharing one directory must be separated even when the
        # destination does not exist yet.
        self._fits("240418", "lsc1m004-fa03-20240418-0001-e91.fits")
        self._fits("240418", "lsc1m004-fa03-20240417-0093-e91.fits")

        plan = self._plan()

        self.assertEqual(len(plan.moves), 1)
        self.assertEqual(plan.moves[0].dst_date, "240417")
        self.assertEqual(plan.issues, ())
        norm.apply_plan(plan)
        self.assertTrue(
            (self.data_root / "Sinistro" / "240417" / "lsc1m004-fa03-20240417-0093-e91.fits").is_file()
        )

    def test_non_adjacent_stray_is_reported_not_moved(self):
        self._fits("220309", "lsc1m004-fa03-20220309-0001-e91.fits")
        stray = self._fits("220309", "lsc1m004-fa03-20221209-0181-e91.fits")

        plan = self._plan()

        self.assertEqual(plan.moves, ())
        self.assertEqual([i.reason for i in plan.issues], ["stray"])
        self.assertIn(stray.name, plan.issues[0].detail)

    def test_existing_destination_file_is_reported_as_conflict(self):
        self._fits("240418", "lsc1m004-fa03-20240418-0001-e91.fits")
        self._fits("240418", "lsc1m004-fa03-20240417-0092-e91.fits")
        # Same basename already present in the destination directory.
        self._fits("240417", "lsc1m004-fa03-20240417-0092-e91.fits")

        plan = self._plan()

        self.assertEqual(plan.moves, ())
        self.assertEqual([i.reason for i in plan.issues], ["conflict"])

    def test_files_without_a_dayobs_token_are_ignored(self):
        # Matches the scanner's glob but carries no DAY-OBS: cannot be judged,
        # so it is left where it is rather than assumed correct.
        self._fits("240418", "hand-copied-frame-e91.fits")

        plan = self._plan()

        self.assertEqual(plan.moves, ())
        self.assertEqual(plan.issues, ())

    def test_files_the_instrument_scanner_ignores_are_not_touched(self):
        # MuSCAT4 frames cross-filed under /data/MuSCAT3/ are invisible to the
        # muscat3 scanner, so relocating them would churn files without
        # changing anything the database sees.
        self._fits("231007", "coj2m002-ep06-20231008-0055-e91.fits", inst="muscat3")
        self._fits("231007", "ogg2m001-ep02-20231007-0001-e91.fits", inst="muscat3")

        plan = self._plan("muscat3")

        self.assertEqual(plan.moves, ())
        self.assertEqual(plan.issues, ())

    def test_non_canonical_date_directories_are_skipped(self):
        self._fits("240418_bkup", "lsc1m004-fa03-20240417-0092-e91.fits")

        plan = self._plan()

        self.assertEqual(plan.moves, ())
        self.assertEqual(plan.issues, ())


class ApplyPlanTest(_TreeTestCase):
    def _split_tree(self):
        self._fits("240417", "lsc1m004-fa03-20240417-0092-e91.fits")
        self._fits("240418", "lsc1m004-fa03-20240417-0093-e91.fits")
        self._fits("240418", "lsc1m004-fa03-20240418-0001-e91.fits")

    def test_apply_moves_files_into_the_directory_their_filename_names(self):
        self._split_tree()

        result = norm.apply_plan(self._plan())

        self.assertEqual(result.moved, 1)
        sin = self.data_root / "Sinistro"
        self.assertTrue((sin / "240417" / "lsc1m004-fa03-20240417-0093-e91.fits").is_file())
        self.assertFalse((sin / "240418" / "lsc1m004-fa03-20240417-0093-e91.fits").exists())
        # The frame that genuinely belongs to 240418 is left alone.
        self.assertTrue((sin / "240418" / "lsc1m004-fa03-20240418-0001-e91.fits").is_file())

    def test_apply_removes_stale_obslog_csvs_for_touched_dates(self):
        # Without this, build-db re-ingests the pre-move CSV and the split
        # reappears in the database even though the FITS moved.
        self._split_tree()
        src_csv = self._csv("240418")
        dst_csv = self._csv("240417")

        result = norm.apply_plan(self._plan())

        self.assertFalse(src_csv.exists())
        self.assertFalse(dst_csv.exists())
        self.assertEqual(sorted(result.touched_dates), ["240417", "240418"])

    def test_apply_is_idempotent(self):
        self._split_tree()
        norm.apply_plan(self._plan())

        second = self._plan()

        self.assertEqual(second.moves, ())
        self.assertEqual(norm.apply_plan(second).moved, 0)

    def test_apply_removes_a_directory_left_empty_by_the_move(self):
        # An emptied raw dir would otherwise sit in scan-missing's work list
        # forever, rescanned on every pass and producing nothing.
        self._fits("240417", "lsc1m004-fa03-20240417-0092-e91.fits")
        self._fits("240418", "lsc1m004-fa03-20240417-0093-e91.fits")

        norm.apply_plan(self._plan())

        self.assertFalse((self.data_root / "Sinistro" / "240418").exists())

    def test_dry_run_plan_does_not_touch_the_tree(self):
        self._split_tree()
        csv = self._csv("240418")

        plan = self._plan()

        self.assertEqual(len(plan.moves), 1)
        self.assertTrue(plan.moves[0].src.is_file())
        self.assertTrue(csv.exists())


class DedupeConflictsTest(_TreeTestCase):
    """Conflicts are duplicate copies of one frame. Deleting the redundant copy
    is gated on a DATASUM match: byte comparison is misleading because the FITS
    CHECKSUM card is recomputed on write."""

    def _real_fits(self, obsdate: str, filename: str, seed: int, *, checksum: bool = True):
        import numpy as np
        from astropy.io import fits

        path = self.data_root / "Sinistro" / obsdate / filename
        path.parent.mkdir(parents=True, exist_ok=True)
        data = np.full((4, 4), seed, dtype="int16")
        fits.PrimaryHDU(data).writeto(path, overwrite=True, checksum=checksum)
        return path

    def _conflict_tree(self, *, src_seed: int, dst_seed: int, checksum: bool = True):
        name = "lsc1m004-fa03-20240417-0093-e91.fits"
        # 240418 also holds a frame of its own, so the directory is mixed and
        # the 240417 frame is a move candidate.
        self._real_fits("240418", "lsc1m004-fa03-20240418-0001-e91.fits", 7)
        src = self._real_fits("240418", name, src_seed, checksum=checksum)
        dst = self._real_fits("240417", name, dst_seed, checksum=checksum)
        return src, dst

    def test_plan_records_the_conflicting_paths(self):
        src, dst = self._conflict_tree(src_seed=1, dst_seed=1)

        plan = self._plan()
        conflicts = [i for i in plan.issues if i.reason == "conflict"]

        self.assertEqual(len(conflicts), 1)
        self.assertEqual(conflicts[0].src, src)
        self.assertEqual(conflicts[0].dst, dst)

    def test_dry_run_reports_but_deletes_nothing(self):
        src, _dst = self._conflict_tree(src_seed=1, dst_seed=1)

        result = norm.dedupe_conflicts(self._plan(), dry_run=True)

        self.assertEqual(result.deleted, (src,))
        self.assertTrue(src.is_file())

    def test_deletes_the_source_copy_when_datasum_matches(self):
        src, dst = self._conflict_tree(src_seed=1, dst_seed=1)

        result = norm.dedupe_conflicts(self._plan(), dry_run=False)

        self.assertEqual(result.deleted, (src,))
        self.assertEqual(result.kept, ())
        self.assertFalse(src.exists())
        self.assertTrue(dst.is_file())

    def test_keeps_both_when_datasum_differs(self):
        src, dst = self._conflict_tree(src_seed=1, dst_seed=2)

        result = norm.dedupe_conflicts(self._plan(), dry_run=False)

        self.assertEqual(result.deleted, ())
        self.assertEqual([r for _p, r in result.kept], ["DATASUM differs"])
        self.assertTrue(src.is_file())
        self.assertTrue(dst.is_file())

    def test_keeps_both_when_datasum_is_unavailable(self):
        src, dst = self._conflict_tree(src_seed=1, dst_seed=1, checksum=False)

        result = norm.dedupe_conflicts(self._plan(), dry_run=False)

        self.assertEqual(result.deleted, ())
        self.assertEqual([r for _p, r in result.kept], ["DATASUM unavailable"])
        self.assertTrue(src.is_file())
        self.assertTrue(dst.is_file())

    def test_conflict_clears_after_dedupe_and_the_frame_then_moves(self):
        src, dst = self._conflict_tree(src_seed=1, dst_seed=1)
        norm.dedupe_conflicts(self._plan(), dry_run=False)

        plan = self._plan()

        self.assertEqual([i for i in plan.issues if i.reason == "conflict"], [])
        self.assertFalse(src.exists())
        self.assertTrue(dst.is_file())


class PlanAllInstrumentsTest(_TreeTestCase):
    def test_only_lco_fed_instruments_are_scanned(self):
        # muscat/muscat2 filenames carry no DAY-OBS token and their directory
        # naming is already observing-night based.
        self.assertEqual(set(norm.LCO_INSTRUMENTS), {"sinistro", "muscat3", "muscat4"})

    def test_plans_every_lco_instrument(self):
        self._fits("240418", "lsc1m004-fa03-20240417-0093-e91.fits")
        self._fits("240418", "lsc1m004-fa03-20240418-0001-e91.fits")
        self._fits("260102", "ogg2m001-ep05-20260101-0001-e91.fits", inst="muscat3")
        self._fits("260102", "ogg2m001-ep05-20260102-0002-e91.fits", inst="muscat3")

        plans = norm.plan_all(data_root=self.data_root, obslog_root=self.obslog_root)

        moved_by_inst = {p.instrument: len(p.moves) for p in plans if p.moves}
        self.assertEqual(moved_by_inst, {"sinistro": 1, "muscat3": 1})
