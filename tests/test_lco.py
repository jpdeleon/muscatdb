from __future__ import annotations

import unittest
from unittest.mock import patch, MagicMock
import io
import os
import socket
import shutil
import tempfile
import threading
import time
from pathlib import Path

from muscat_db import lco

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
        self.assertIn("target", config) # Target also in config now
        self.assertIn("constraints", config) # Constraints also in config now
        self.assertEqual(config["constraints"]["max_airmass"], 2.5)
        self.assertEqual(config["constraints"]["min_lunar_distance"], 18)

        self.assertEqual(len(config["instrument_configs"]), 4)
        instrument_config = config["instrument_configs"][0]
        self.assertEqual(instrument_config["exposure_time"], 30)
        self.assertEqual(instrument_config["exposure_count"], 2)
        self.assertEqual(instrument_config["mode"], "MUSCAT_FAST")
        self.assertEqual(instrument_config["optical_elements"]["filter"], "g")
        self.assertEqual(instrument_config["optical_elements"]["narrowband_g_position"], "in")
        self.assertIn("extra_params", instrument_config)
        self.assertEqual(instrument_config["extra_params"]["exposure_mode"], "ASYNCHRONOUS")
        self.assertEqual(instrument_config["extra_params"]["exposure_time_g"], 30)

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

    def test_build_requestgroup_invalid_payload(self):
        with self.assertRaises(lco.LcoError):
            lco.build_requestgroup("muscat3", {})

    @patch.dict(os.environ, {"LCO_API_TOKEN": ""})
    def test_get_token_missing(self):
        with self.assertRaises(lco.LcoError) as cm:
            lco._get_lco_api_token()
        self.assertEqual(cm.exception.status, 503)

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
