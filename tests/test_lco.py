from __future__ import annotations

import unittest
from unittest.mock import patch, MagicMock
import datetime
import io
import os
import socket
import shutil
import tempfile
import threading
import time
from pathlib import Path

from muscat_db import lco
from muscat_db.database import set_user_lco_token

class LcoTest(unittest.TestCase):

    def test_build_requestgroup_muscat(self):
        params = {
            "name": "Test MUSCAT Request",
            "proposal": "LCO2026A-001",
            "target_name": "WASP-12",
            "ra": "06:30:33",
            "dec": "+29:40:20",
            "kind": "muscat3",
            "exposure_times": {"g": 30, "r": 30, "i": 30, "z": 30},
            "exposure_count": 2,
            "windows": [{"start": "2026-07-01T00:00:00Z", "end": "2026-07-01T01:00:00Z"}],
            "readout_mode": "MUSCAT_FAST",
            "narrowband": {"g": "in"},
            "repeat_duration": 18179,
            "exposure_mode": "ASYNCHRONOUS",
            "max_airmass": 2.5,
            "min_lunar_distance": 18,
            "max_seeing": 2.0,
            "min_transparency": "Clear",
            "guiding_config": "OFF",
        }
        rg = lco.build_requestgroup("muscat3", params)
        self.assertEqual(rg["name"], "Test MUSCAT Request")
        self.assertEqual(rg["observation_type"], "NORMAL") # Now at top level
        
        request = rg["requests"][0]
        self.assertEqual(request["instrument_type"], "2M0-SCICAM-MUSCAT")
        self.assertIn("target", request)
        self.assertEqual(request["target"]["name"], "WASP-12")
        self.assertIn("constraints", request) # Constraints are still here
        self.assertNotIn("observation_type", request) # Moved to top level

        config = request["configurations"][0]
        self.assertEqual(config["type"], "REPEAT_EXPOSE") # Default type changed
        self.assertEqual(config["repeat_duration"], 18179)
        # LCO instruments API: MUSCAT only supports the "OFF" acquisition mode.
        self.assertEqual(config["acquisition_config"]["mode"], "OFF")
        self.assertIn("target", config) # Target also in config now
        self.assertIn("constraints", config) # Constraints also in config now
        self.assertEqual(config["constraints"]["max_airmass"], 2.5)
        self.assertEqual(config["constraints"]["min_lunar_distance"], 18)

        # MuSCAT is a simultaneous 4-band imager -> exactly one instrument_config
        # matching LCO's accepted request shape (no per-band `filter`).
        self.assertEqual(len(config["instrument_configs"]), 1)
        instrument_config = config["instrument_configs"][0]
        self.assertEqual(instrument_config["exposure_time"], 30)  # longest band
        self.assertEqual(instrument_config["exposure_count"], 1)
        self.assertEqual(instrument_config["mode"], "MUSCAT_FAST")
        self.assertNotIn("filter", instrument_config["optical_elements"])
        self.assertEqual(instrument_config["optical_elements"]["narrowband_g_position"], "in")
        self.assertIn("extra_params", instrument_config)
        ep = instrument_config["extra_params"]
        self.assertEqual(ep["exposure_mode"], "ASYNCHRONOUS")
        # Every band's exposure is carried in extra_params, plus binning/offsets.
        for b in ("g", "r", "i", "z"):
            self.assertEqual(ep[f"exposure_time_{b}"], 30)
        self.assertEqual((ep["bin_x"], ep["bin_y"]), (1, 1))
        self.assertEqual((ep["offset_ra"], ep["offset_dec"]), (0, 0))
        # telescope_class is present even though this request set no site.
        self.assertEqual(request["location"]["telescope_class"], "2m0")
        self.assertNotIn("site", request["location"])

    def test_build_requestgroup_sinistro(self):
        params = {
            "name": "Test Sinistro Request",
            "proposal": "LCO2026A-001",
            "target_name": "WASP-12",
            "ra": "06:30:33",
            "dec": "+29:40:20",
            "kind": "sinistro",
            "exposure_time": 60,
            "exposure_count": 5,
            "filter": "rp",
            "windows": [{"start": "2026-07-01T00:00:00Z", "end": "2026-07-01T01:00:00Z"}],
            "max_airmass": 1.8, # Different default from MUSCAT
            "readout_mode": "central_2k_2x2",
        }
        rg = lco.build_requestgroup("sinistro", params)
        self.assertEqual(rg["name"], "Test Sinistro Request")
        self.assertEqual(rg["observation_type"], "NORMAL") # Sinistro default is NORMAL

        request = rg["requests"][0]
        self.assertEqual(request["instrument_type"], "1M0-SCICAM-SINISTRO")
        self.assertIn("target", request)
        self.assertEqual(request["constraints"]["max_airmass"], 1.8)
        
        config = request["configurations"][0]
        self.assertEqual(config["type"], "EXPOSE")
        self.assertNotIn("repeat_duration", config)  # only for REPEAT_EXPOSE
        self.assertIn("target", config)
        self.assertIn("constraints", config)
        self.assertEqual(config["acquisition_config"]["mode"], "OFF")
        self.assertTrue(config["guiding_config"]["optional"])

        self.assertEqual(len(config["instrument_configs"]), 1)
        inst_config = config["instrument_configs"][0]
        self.assertEqual(inst_config["exposure_time"], 60)
        self.assertEqual(inst_config["optical_elements"]["filter"], "rp")
        self.assertEqual(inst_config["mode"], "central_2k_2x2")
        self.assertIn("extra_params", inst_config)
        self.assertEqual(inst_config["extra_params"]["bin_x"], 2)
        self.assertEqual(inst_config["extra_params"]["bin_y"], 2)

    def test_defocus_defaults_to_zero(self):
        params = {
            "name": "n", "proposal": "p", "target_name": "t",
            "ra": 10.0, "dec": -5.0, "kind": "sinistro",
            "exposure_time": 60, "filter": "rp",
            "windows": [{"start": "2026-07-01T00:00:00Z", "end": "2026-07-01T01:00:00Z"}],
        }
        config = lco.build_requestgroup("sinistro", params)["requests"][0]["configurations"][0]
        self.assertEqual(config["instrument_configs"][0]["extra_params"]["defocus"], 0.0)

    def test_defocus_passed_through_for_muscat(self):
        params = {
            "name": "n", "proposal": "p", "target_name": "t",
            "ra": 10.0, "dec": -5.0, "kind": "muscat3", "defocus": "3.5",
            "exposure_times": {"g": 30, "r": 30, "i": 30, "z": 30},
            "windows": [{"start": "2026-07-01T00:00:00Z", "end": "2026-07-01T01:00:00Z"}],
        }
        config = lco.build_requestgroup("muscat3", params)["requests"][0]["configurations"][0]
        self.assertEqual(config["instrument_configs"][0]["extra_params"]["defocus"], 3.5)

    def test_defocus_passed_through_for_sinistro(self):
        params = {
            "name": "n", "proposal": "p", "target_name": "t",
            "ra": 10.0, "dec": -5.0, "kind": "sinistro", "defocus": -4,
            "exposure_time": 60, "filter": "rp",
            "windows": [{"start": "2026-07-01T00:00:00Z", "end": "2026-07-01T01:00:00Z"}],
        }
        config = lco.build_requestgroup("sinistro", params)["requests"][0]["configurations"][0]
        self.assertEqual(config["instrument_configs"][0]["extra_params"]["defocus"], -4.0)

    def test_defocus_rejects_out_of_range_for_sinistro(self):
        # Sinistro's live LCO limit is +/-5mm, tighter than MuSCAT's +/-8mm.
        params = {
            "name": "n", "proposal": "p", "target_name": "t",
            "ra": 10.0, "dec": -5.0, "kind": "sinistro", "defocus": 6,
            "exposure_time": 60, "filter": "rp",
            "windows": [{"start": "2026-07-01T00:00:00Z", "end": "2026-07-01T01:00:00Z"}],
        }
        with self.assertRaises(lco.LcoError) as cm:
            lco.build_requestgroup("sinistro", params)
        self.assertEqual(cm.exception.status, 400)
        self.assertIn("5mm", str(cm.exception))

    def test_defocus_rejects_out_of_range_for_muscat(self):
        params = {
            "name": "n", "proposal": "p", "target_name": "t",
            "ra": 10.0, "dec": -5.0, "kind": "muscat3", "defocus": 9,
            "exposure_times": {"g": 30, "r": 30, "i": 30, "z": 30},
            "windows": [{"start": "2026-07-01T00:00:00Z", "end": "2026-07-01T01:00:00Z"}],
        }
        with self.assertRaises(lco.LcoError) as cm:
            lco.build_requestgroup("muscat3", params)
        self.assertEqual(cm.exception.status, 400)
        self.assertIn("8mm", str(cm.exception))

    def test_defocus_rejects_non_numeric(self):
        params = {
            "name": "n", "proposal": "p", "target_name": "t",
            "ra": 10.0, "dec": -5.0, "kind": "sinistro", "defocus": "abc",
            "exposure_time": 60, "filter": "rp",
            "windows": [{"start": "2026-07-01T00:00:00Z", "end": "2026-07-01T01:00:00Z"}],
        }
        with self.assertRaises(lco.LcoError) as cm:
            lco.build_requestgroup("sinistro", params)
        self.assertEqual(cm.exception.status, 400)
        self.assertIn("number", str(cm.exception))

    def test_muscat_repeat_duration_computed_from_windows(self):
        """A REPEAT_EXPOSE config with no explicit duration derives it from the window."""
        params = {
            "name": "n", "proposal": "p", "target_name": "t",
            "ra": 10.0, "dec": -5.0, "kind": "muscat4",
            "exposure_times": {"g": 30, "r": 30, "i": 30, "z": 30},
            "windows": [{"start": "2026-07-04T07:00:00Z", "end": "2026-07-04T10:00:00Z"}],
        }
        config = lco.build_requestgroup("muscat4", params)["requests"][0]["configurations"][0]
        self.assertEqual(config["type"], "REPEAT_EXPOSE")
        # 3 h window (10800 s) minus the 180 s setup overhead.
        self.assertEqual(config["repeat_duration"], 10620)

    def test_muscat_repeat_duration_uses_shortest_window(self):
        """One repeat_duration must fit every selected window, so use the shortest."""
        params = {
            "name": "n", "proposal": "p", "target_name": "t",
            "ra": 10.0, "dec": -5.0, "kind": "muscat4",
            "exposure_times": {"g": 30, "r": 30, "i": 30, "z": 30},
            "windows": [
                {"start": "2026-07-04T07:00:00Z", "end": "2026-07-04T10:00:00Z"},  # 3 h
                {"start": "2026-07-05T07:00:00Z", "end": "2026-07-05T08:00:00Z"},  # 1 h
            ],
        }
        config = lco.build_requestgroup("muscat4", params)["requests"][0]["configurations"][0]
        self.assertEqual(config["repeat_duration"], 3600 - 180)

    def test_muscat_repeat_expose_forces_single_exposure_block(self):
        """REPEAT_EXPOSE repeats one exposure block; packed counts make LCO reject it."""
        params = {
            "name": "n", "proposal": "p", "target_name": "t",
            "ra": 10.0, "dec": -5.0, "kind": "muscat4",
            "exposure_times": {"g": 30, "r": 30, "i": 30, "z": 30},
            "exposure_count": 506,
            "type": "REPEAT_EXPOSE",
            "windows": [{"start": "2026-07-04T07:00:00Z", "end": "2026-07-04T11:56:11Z"}],
        }
        config = lco.build_requestgroup("muscat4", params)["requests"][0]["configurations"][0]
        self.assertEqual(config["type"], "REPEAT_EXPOSE")
        self.assertEqual(config["repeat_duration"], 17591)
        self.assertEqual(config["instrument_configs"][0]["exposure_count"], 1)

    @patch("muscat_db.transit_obs.classify_transits")
    def test_muscat_repeat_expose_rejects_padded_partial_window(self, mock_classify):
        mock_classify.return_value = [{"rating": "partial", "sites": ["ogg"], "best_site": "ogg"}]
        params = {
            "name": "n", "proposal": "p", "target_name": "t",
            "ra": 261.82914, "dec": -25.92151, "kind": "muscat",
            "site": "ogg", "max_airmass": 2.0, "min_lunar_distance": 30,
            "twilight": "nautical",
            "exposure_times": {"g": 30, "r": 30, "i": 30, "z": 30},
            "type": "REPEAT_EXPOSE",
            "windows": [{"start": "2026-07-18T05:30:12Z", "end": "2026-07-18T10:26:24Z"}],
        }
        with self.assertRaises(lco.LcoError) as cm:
            lco.build_requestgroup("muscat", params)
        self.assertEqual(cm.exception.status, 400)
        self.assertIn("not fully observable", cm.exception.message)
        self.assertIn("Include padding", cm.exception.detail)
        mock_classify.assert_called_once()
        _, kwargs = mock_classify.call_args
        self.assertTrue(kwargs["include_padding"])
        self.assertEqual(kwargs["sites"], ["ogg"])
        self.assertEqual(kwargs["twilight"], "nautical")

    @patch("muscat_db.transit_obs.classify_transits")
    def test_muscat_repeat_expose_accepts_padded_full_window(self, mock_classify):
        mock_classify.return_value = [{"rating": "full", "sites": ["ogg"], "best_site": "ogg"}]
        params = {
            "name": "n", "proposal": "p", "target_name": "t",
            "ra": 261.82914, "dec": -25.92151, "kind": "muscat",
            "site": "ogg", "max_airmass": 2.0, "min_lunar_distance": 30,
            "exposure_times": {"g": 30, "r": 30, "i": 30, "z": 30},
            "type": "REPEAT_EXPOSE",
            "windows": [{"start": "2026-07-18T05:30:12Z", "end": "2026-07-18T10:26:24Z"}],
        }
        config = lco.build_requestgroup("muscat", params)["requests"][0]["configurations"][0]
        self.assertEqual(config["type"], "REPEAT_EXPOSE")
        self.assertEqual(config["instrument_configs"][0]["exposure_count"], 1)

    def test_muscat_expose_type_omits_repeat_duration(self):
        params = {
            "name": "n", "proposal": "p", "target_name": "t",
            "ra": 10.0, "dec": -5.0, "kind": "muscat4", "type": "EXPOSE",
            "exposure_count": 7,
            "exposure_times": {"g": 30, "r": 30, "i": 30, "z": 30},
            "windows": [{"start": "2026-07-04T07:00:00Z", "end": "2026-07-04T10:00:00Z"}],
        }
        config = lco.build_requestgroup("muscat4", params)["requests"][0]["configurations"][0]
        self.assertEqual(config["type"], "EXPOSE")
        self.assertNotIn("repeat_duration", config)
        self.assertEqual(config["instrument_configs"][0]["exposure_count"], 7)

    def test_build_requestgroup_invalid_payload(self):
        with self.assertRaises(lco.LcoError) as cm:
            lco.build_requestgroup("muscat3", {})
        # Every required field is named so the UI can point the user at them.
        self.assertEqual(cm.exception.status, 400)
        for label in ("request name", "proposal", "target", "RA", "Dec"):
            self.assertIn(label, str(cm.exception))

    def test_build_requestgroup_names_single_missing_field(self):
        """A payload missing only the proposal must call out the proposal."""
        params = {
            "name": "Test", "proposal": "", "target_name": "WASP-12",
            "ra": "06:30:33", "dec": "+29:40:20", "kind": "muscat3",
        }
        with self.assertRaises(lco.LcoError) as cm:
            lco.build_requestgroup("muscat3", params)
        msg = str(cm.exception)
        self.assertIn("proposal", msg)
        self.assertNotIn("target", msg)
        self.assertNotIn("request name", msg)

    @patch.dict(os.environ, {"LCO_API_TOKEN": ""})
    def test_get_token_missing(self):
        with self.assertRaises(lco.LcoError) as cm:
            lco._get_lco_api_token()
        self.assertEqual(cm.exception.status, 503)

    def test_get_token_prefers_user_token_and_falls_back_to_global(self):
        fd, path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        try:
            with patch.dict(
                os.environ,
                {
                    "MUSCAT_DB_PATH": path,
                    "MUSCAT_DB_SECRET": "test-secret",
                    "LCO_API_TOKEN": "global-token",
                },
            ):
                set_user_lco_token("alice", "alice-token")
                self.assertEqual(lco._get_lco_api_token("alice"), "alice-token")
                self.assertEqual(lco._get_lco_api_token("bob"), "global-token")
                state = lco.config_state("alice")
                self.assertTrue(state["user_token_configured"])
                self.assertEqual(state["token_source"], "user")
        finally:
            os.unlink(path)

    @patch.dict(os.environ, {"LCO_API_TOKEN": "test-token"})
    @patch("urllib.request.urlopen")
    def test_get_proposals_ok(self, mock_urlopen):
        mock_response = MagicMock()
        mock_response.status = 200
        mock_response.read.return_value = b'{"results": [{"id": "LCO2026A-001"}]}'
        mock_urlopen.return_value.__enter__.return_value = mock_response

        result = lco.get_proposals()
        self.assertIn("results", result)
        self.assertEqual(len(result["results"]), 1)

    @patch.dict(os.environ, {"LCO_API_TOKEN": "test-token"})
    @patch("urllib.request.urlopen")
    def test_archive_search_ok(self, mock_urlopen):
        mock_response = MagicMock()
        mock_response.status = 200
        mock_response.read.return_value = b'{"results": [{"filename": "test.fits"}]}'
        mock_urlopen.return_value.__enter__.return_value = mock_response

        result = lco.archive_search({"OBJECT": "WASP-12"})
        self.assertIn("results", result)
        self.assertEqual(result["results"][0]["filename"], "test.fits")

        # Regression: the LCO Science Archive authenticates with the DRF
        # "Token" scheme, not "Bearer". Using Bearer returns HTTP 401
        # {"detail": "No Such User"}.
        request = mock_urlopen.call_args[0][0]
        self.assertEqual(request.get_header("Authorization"), "Token test-token")

    @patch.dict(os.environ, {"LCO_API_TOKEN": "test-token"})
    @patch("urllib.request.urlopen")
    def test_archive_search_preserves_raw_reduction_level_zero(self, mock_urlopen):
        mock_response = MagicMock()
        mock_response.status = 200
        mock_response.read.return_value = b'{"count": 0, "results": []}'
        mock_urlopen.return_value.__enter__.return_value = mock_response

        lco.archive_search({"request_id": 123, "reduction_level": 0})

        url = mock_urlopen.call_args.args[0].full_url
        self.assertIn("request_id=123", url)
        self.assertIn("reduction_level=0", url)

    def test_generate_windows(self):
        windows = lco.generate_windows(
            t0=2459000.5,
            period=1.0914,
            duration_h=2.5,
            start_dt="2026-07-01",
            end_dt="2026-07-03",
            pad_before_min=30,
            pad_after_min=30,
        )
        self.assertEqual(len(windows), 3)
        self.assertEqual(windows[0]["epoch"], 0)  # Normalized to 0-indexed within date range
        self.assertEqual(windows[0]["epoch_abs"], 2036)  # Absolute epoch preserved
        self.assertEqual(windows[1]["epoch"], 1)
        self.assertEqual(windows[2]["epoch"], 2)

    def test_generate_windows_preserves_precise_boundaries(self):
        # 2026-07-01 00:01:00 UTC; the resulting boundaries must retain the
        # one-minute offset rather than being rounded to 5 minutes. Computed
        # as 1 minute past JD 2461222.5 (midnight) rather than a hardcoded
        # decimal literal, but a JD at this magnitude (~2.46e6) only has
        # float64 headroom for a handful of microseconds of precision in its
        # fractional day regardless of how it's constructed, so the assertion
        # below checks the real invariant (not snapped to a 5-minute grid)
        # with a millisecond tolerance instead of an exact string match.
        t0 = 2461222.5 + 1.0 / 1440.0
        windows = lco.generate_windows(
            t0=t0,
            period=1.0,
            duration_h=1.0,
            start_dt="2026-07-01",
            end_dt="2026-07-01",
            pad_before_min=0,
            pad_after_min=0,
        )
        self.assertEqual(len(windows), 1)
        start = datetime.datetime.fromisoformat(windows[0]["start"].replace("Z", "+00:00"))
        end = datetime.datetime.fromisoformat(windows[0]["end"].replace("Z", "+00:00"))
        expected_start = datetime.datetime(2026, 6, 30, 23, 31, tzinfo=datetime.timezone.utc)
        expected_end = datetime.datetime(2026, 7, 1, 0, 31, tzinfo=datetime.timezone.utc)
        self.assertLess(abs((start - expected_start).total_seconds()), 0.001)
        self.assertLess(abs((end - expected_end).total_seconds()), 0.001)

class FrameDestSecurityTest(unittest.TestCase):
    """frame_dest / URL validation must block path traversal and SSRF."""

    def setUp(self):
        self._env = patch.dict(os.environ, {"MUSCAT_LCO_DIR": "/tmp/lco-root"}, clear=False)
        self._env.start()

    def tearDown(self):
        self._env.stop()

    def test_valid_frame_resolves_under_root(self):
        dest = lco.frame_dest("sinistro", "230101", "cpt1m010-fa16-20230101-0123-e91.fits.fz")
        self.assertEqual(
            str(dest),
            "/tmp/lco-root/Sinistro/230101/cpt1m010-fa16-20230101-0123-e91.fits.fz",
        )

    def test_instrument_directory_uses_case_sensitive_data_mapping(self):
        cases = {
            "sinistro": "Sinistro",
            "muscat": "MuSCAT",
            "muscat2": "MuSCAT2",
            "muscat3": "MuSCAT3",
            "muscat4": "MuSCAT4",
        }
        for instrument, dirname in cases.items():
            with self.subTest(instrument=instrument):
                dest = lco.frame_dest(instrument, "230101", "frame.fits.fz")
                self.assertEqual(dest.parts[-3], dirname)

    def test_filename_traversal_rejected(self):
        with self.assertRaises(lco.LcoError):
            lco.frame_dest("sinistro", "230101", "../../../../etc/passwd")

    def test_slash_in_filename_rejected(self):
        with self.assertRaises(lco.LcoError):
            lco.frame_dest("sinistro", "230101", "sub/dir/frame.fits")

    def test_obsdate_traversal_rejected(self):
        # A crafted DATE_OBS could otherwise inject "../.." via obsdate.
        with self.assertRaises(lco.LcoError):
            lco.frame_dest("sinistro", "../secret", "frame.fits")

    def test_url_must_be_https_lco_or_s3(self):
        for bad in ("http://archive-api.lco.global/x", "https://evil.example.com/x",
                    "file:///etc/passwd", "", None):
            with self.assertRaises(lco.LcoError):
                lco._validate_download_url(bad)

    def test_url_allows_archive_and_s3(self):
        for ok in ("https://archive-api.lco.global/frames/1/",
                   "https://archive-lco-global.s3.amazonaws.com/x?sig=1"):
            self.assertEqual(lco._validate_download_url(ok), ok)


class _StallingResponse:
    """Fake urlopen result that yields no data and stalls on the first read,
    mimicking a hung archive/S3 socket mid-stream."""

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def read(self, size=-1):
        raise socket.timeout("stalled mid-stream")


class DownloadToFileTest(unittest.TestCase):
    """_download_to_file must stream atomically and never leave a partial file —
    the regression that let a stalled urlretrieve wedge the whole server."""

    def setUp(self):
        base = os.path.join(os.path.expanduser("~/temp"), "muscatdb-test")
        os.makedirs(base, exist_ok=True)
        self.dir = tempfile.mkdtemp(dir=base)

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    def test_streams_atomically_and_leaves_no_part_file(self):
        dest = Path(self.dir) / "frame.fits.fz"
        payload = b"BINARYFITS" * 1000
        with patch("muscat_db.lco.urllib.request.urlopen", return_value=io.BytesIO(payload)):
            lco._download_to_file("https://archive-api.lco.global/frames/1/", dest)
        self.assertEqual(dest.read_bytes(), payload)
        self.assertFalse(dest.with_name(dest.name + ".part").exists())

    def test_stall_raises_and_cleans_partial(self):
        dest = Path(self.dir) / "frame.fits.fz"
        with patch("muscat_db.lco.urllib.request.urlopen", return_value=_StallingResponse()):
            with self.assertRaises(socket.timeout):
                lco._download_to_file("https://archive-api.lco.global/frames/1/", dest, timeout=0.01)
        # No truncated frame and no leftover .part after the stall.
        self.assertFalse(dest.exists())
        self.assertFalse(dest.with_name(dest.name + ".part").exists())

    def test_download_root_prefers_lco_dir_then_data_dir(self):
        with patch.dict(os.environ, {"MUSCAT_LCO_DIR": "/data", "MUSCAT_DATA_DIR": "/raw"}, clear=True):
            self.assertEqual(str(lco.download_root()), "/data")
        with patch.dict(os.environ, {"MUSCAT_DATA_DIR": "/raw"}, clear=True):
            self.assertEqual(str(lco.download_root()), "/raw")
        with patch.dict(os.environ, {}, clear=True):
            self.assertIsNone(lco.download_root())

    def test_download_frames_reports_dest_path(self):
        frame = {
            "filename": "ogg2m001-ep05-20260102-0001-e91.fits.fz",
            "SITEID": "ogg", "TELID": "2m0a", "INSTRUME": "ep05",
            "DATE_OBS": "2026-01-02T05:00:00",
            "url": "https://archive-api.lco.global/frames/1/",
        }
        with patch.dict(os.environ, {"MUSCAT_LCO_DIR": self.dir}, clear=False), \
                patch("muscat_db.lco._download_to_file") as dl:
            results = lco.download_frames([frame])
        dl.assert_called_once()
        self.assertEqual(results[0]["status"], "downloaded")
        # <root>/<inferred instrument>/<YYMMDD>/<filename>; frame_dest resolves
        # symlinks in the root, so compare against the resolved base.
        self.assertEqual(
            results[0]["dest"],
            os.path.join(str(Path(self.dir).resolve()), "MuSCAT3", "260102", frame["filename"]),
        )

    def test_funpack_file_writes_fits_next_to_fz_without_deleting_source(self):
        src = Path(self.dir) / "frame.fits.fz"
        src.write_bytes(b"packed")
        calls = []

        def fake_run(cmd, **kwargs):
            calls.append((cmd, kwargs))
            Path(cmd[2]).write_bytes(b"fits")
            return MagicMock(returncode=0, stdout="", stderr="")

        with patch("muscat_db.lco.shutil.which", return_value="/usr/bin/funpack"), \
                patch("muscat_db.lco.subprocess.run", side_effect=fake_run):
            result = lco._funpack_file(src)

        self.assertEqual(result["status"], "unpacked")
        self.assertEqual(result["dest"], str(Path(self.dir) / "frame.fits"))
        self.assertTrue(src.exists())
        self.assertEqual(calls[0][0], ["/usr/bin/funpack", "-O", str(Path(self.dir) / "frame.fits"), str(src)])


class ArchiveDownloadJobTest(unittest.TestCase):
    def test_interactive_download_scans_ingests_and_links_photometry(self):
        temp_dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, temp_dir, ignore_errors=True)
        frame = {
            "filename": "ogg2m001-ep05-20260102-0001-e91.fits",
            "SITEID": "ogg",
            "TELID": "2m0a",
            "INSTRUME": "ep05",
            "DATE_OBS": "2026-01-02T05:00:00",
            "OBJECT": "WASP-12",
        }
        downloaded = {
            "filename": frame["filename"],
            "status": "downloaded",
            "dest": str(Path(temp_dir) / "MuSCAT3" / "260102" / frame["filename"]),
        }
        scanned = []
        ingested = []

        def fake_scan(inst, obsdate, max_workers=None, data_root=None):
            scanned.append((inst, obsdate, max_workers, data_root))
            return {"total": 1, "per_ccd": {0: 1}}

        def fake_ingest(path, inst, obsdate):
            ingested.append((path, inst, obsdate))
            return 1

        with patch("muscat_db.lco._download_frame", return_value=downloaded), \
                patch("muscat_db.lco.download_root", return_value=Path(temp_dir)), \
                patch("muscat_db.lco._db_path", return_value="/data/muscat.db"), \
                patch("muscat_db.scanner.scan_date", side_effect=fake_scan), \
                patch("muscat_db.database.ingest_date", side_effect=fake_ingest):
            job = lco.start_archive_download([frame], auto_ingest=True)
            deadline = time.time() + 2
            done = job
            while time.time() < deadline:
                done = lco.archive_download_status(job["job_id"])
                if done["state"] in {"done", "error"}:
                    break
                time.sleep(0.01)

        self.assertEqual(done["state"], "done")
        self.assertEqual(done["phase"], "done")
        self.assertEqual(scanned, [("muscat3", "260102", 1, temp_dir)])
        self.assertEqual(ingested, [("/data/muscat.db", "muscat3", "260102")])
        self.assertEqual(done["processing_results"][0]["ingested_count"], 1)
        self.assertEqual(
            done["photometry_url"],
            "/photometry?inst=muscat3&date=260102&target=WASP-12",
        )

    def test_interactive_download_does_not_link_when_scan_fails(self):
        temp_dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, temp_dir, ignore_errors=True)
        frame = {
            "filename": "ogg2m001-ep05-20260102-0001-e91.fits",
            "SITEID": "ogg",
            "TELID": "2m0a",
            "DATE_OBS": "2026-01-02T05:00:00",
            "OBJECT": "WASP-12",
        }
        downloaded = {
            "filename": frame["filename"],
            "status": "downloaded",
            "dest": str(Path(temp_dir) / "MuSCAT3" / "260102" / frame["filename"]),
        }

        with patch("muscat_db.lco._download_frame", return_value=downloaded), \
                patch("muscat_db.lco.download_root", return_value=Path(temp_dir)), \
                patch("muscat_db.scanner.scan_date", return_value={}), \
                patch("muscat_db.database.ingest_date") as ingest:
            job = lco.start_archive_download([frame], auto_ingest=True)
            deadline = time.time() + 2
            failed = job
            while time.time() < deadline:
                failed = lco.archive_download_status(job["job_id"])
                if failed["state"] in {"done", "error"}:
                    break
                time.sleep(0.01)

        self.assertEqual(failed["state"], "error")
        self.assertIn("scan found no reduced FITS", failed["error"])
        self.assertEqual(failed["photometry_url"], "")
        ingest.assert_not_called()

    def test_background_download_status_updates_without_blocking_submitter(self):
        started = threading.Event()
        release = threading.Event()

        def slow_download(frame, overwrite=False):
            started.set()
            release.wait(timeout=2)
            return {"filename": frame["filename"], "status": "downloaded", "dest": ""}

        with patch("muscat_db.lco._download_frame", side_effect=slow_download):
            job = lco.start_archive_download([{"filename": "frame.fits.fz"}])
            self.assertIn(job["state"], {"pending", "running"})
            self.assertEqual(job["frames_total"], 1)
            self.assertEqual(job["frames_done"], 0)
            self.assertTrue(started.wait(timeout=1))

            running = lco.archive_download_status(job["job_id"])
            self.assertEqual(running["state"], "running")
            self.assertEqual(running["frames_done"], 0)

            release.set()
            deadline = time.time() + 2
            done = running
            while time.time() < deadline:
                done = lco.archive_download_status(job["job_id"])
                if done["state"] == "done":
                    break
                time.sleep(0.01)

        self.assertEqual(done["state"], "done")
        self.assertEqual(done["frames_done"], 1)
        self.assertEqual(done["results"][0]["status"], "downloaded")

    def test_background_download_fetches_frames_in_parallel(self):
        started: list[str] = []
        started_lock = threading.Lock()
        both_started = threading.Event()
        release = threading.Event()

        def slow_download(frame, overwrite=False):
            with started_lock:
                started.append(frame["filename"])
                if len(started) == 2:
                    both_started.set()
            release.wait(timeout=2)
            return {"filename": frame["filename"], "status": "downloaded", "dest": ""}

        frames = [{"filename": "a.fits.fz"}, {"filename": "b.fits.fz"}]
        with patch("muscat_db.lco._ARCHIVE_DOWNLOAD_FRAME_WORKERS", 2), \
                patch("muscat_db.lco._download_frame", side_effect=slow_download):
            job = lco.start_archive_download(frames)
            self.assertTrue(both_started.wait(timeout=1))

            running = lco.archive_download_status(job["job_id"])
            self.assertEqual(running["state"], "running")
            self.assertEqual(running["frames_done"], 0)

            release.set()
            deadline = time.time() + 2
            done = running
            while time.time() < deadline:
                done = lco.archive_download_status(job["job_id"])
                if done["state"] == "done":
                    break
                time.sleep(0.01)

        self.assertEqual(done["state"], "done")
        self.assertEqual(done["frames_done"], 2)
        self.assertEqual(sorted(r["filename"] for r in done["results"]), ["a.fits.fz", "b.fits.fz"])

    def test_funpack_progress_updates_after_each_file_finishes(self):
        blocked_started = threading.Event()
        release_blocked = threading.Event()

        def fake_download(frame, overwrite=False):
            return {
                "filename": frame["filename"],
                "status": "downloaded",
                "dest": str(Path("/data/MuSCAT3/260102") / frame["filename"]),
            }

        def fake_funpack(path):
            if path.name == "b.fits.fz":
                blocked_started.set()
                release_blocked.wait(timeout=2)
            return {
                "filename": path.name,
                "src": str(path),
                "dest": str(path.with_name(path.name[:-3])),
                "status": "unpacked",
            }

        frames = [{"filename": "a.fits.fz"}, {"filename": "b.fits.fz"}]
        with patch("muscat_db.lco._ARCHIVE_FUNPACK_WORKERS", 2), \
                patch("muscat_db.lco._download_frame", side_effect=fake_download), \
                patch("muscat_db.lco._funpack_file", side_effect=fake_funpack):
            job = lco.start_archive_download(frames)
            self.assertTrue(blocked_started.wait(timeout=1))

            deadline = time.time() + 2
            funpacking = None
            while time.time() < deadline:
                funpacking = lco.archive_download_status(job["job_id"])
                if funpacking["phase"] == "funpacking" and funpacking["funpack_done"] == 1:
                    break
                time.sleep(0.01)

            self.assertIsNotNone(funpacking)
            self.assertEqual(funpacking["phase"], "funpacking")
            self.assertEqual(funpacking["funpack_total"], 2)
            self.assertEqual(funpacking["funpack_done"], 1)

            release_blocked.set()
            deadline = time.time() + 2
            done = funpacking
            while time.time() < deadline:
                done = lco.archive_download_status(job["job_id"])
                if done["state"] == "done":
                    break
                time.sleep(0.01)

        self.assertEqual(done["state"], "done")
        self.assertEqual(done["funpack_done"], 2)

    def test_archive_download_rejects_when_active_queue_is_full(self):
        active_job = {
            "job_id": "active",
            "state": "pending",
            "frames": [{"filename": "active.fits.fz"}],
            "frames_total": 1,
            "overwrite": False,
            "results": [],
            "funpack_results": [],
            "funpack_total": 0,
            "phase": "pending",
            "started_at": time.time(),
            "finished_at": None,
            "error": None,
        }
        with patch("muscat_db.lco._ARCHIVE_DOWNLOAD_MAX_JOBS", 1), \
                patch("muscat_db.lco._ARCHIVE_DOWNLOAD_JOBS", {"active": active_job}):
            with self.assertRaises(lco.LcoError) as ctx:
                lco.start_archive_download([{"filename": "new.fits.fz"}])

        self.assertEqual(ctx.exception.status, 429)

    def test_archive_download_prunes_finished_job_to_make_queue_room(self):
        finished_job = {
            "job_id": "finished",
            "state": "done",
            "frames": [{"filename": "finished.fits.fz"}],
            "frames_total": 1,
            "overwrite": False,
            "results": [],
            "funpack_results": [],
            "funpack_total": 0,
            "phase": "done",
            "started_at": time.time() - 20,
            "finished_at": time.time() - 10,
            "error": None,
        }
        jobs = {"finished": finished_job}
        with patch("muscat_db.lco._ARCHIVE_DOWNLOAD_MAX_JOBS", 1), \
                patch("muscat_db.lco._ARCHIVE_DOWNLOAD_JOBS", jobs), \
                patch("muscat_db.lco._ARCHIVE_DOWNLOAD_EXECUTOR.submit") as submit:
            job = lco.start_archive_download([{"filename": "new.fits.fz"}])

        self.assertEqual(job["state"], "pending")
        self.assertEqual(len(jobs), 1)
        self.assertIn(job["job_id"], jobs)
        self.assertNotIn("finished", jobs)
        submit.assert_called_once()


if __name__ == "__main__":
    unittest.main()
