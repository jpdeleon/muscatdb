from __future__ import annotations

import unittest
from unittest.mock import patch, MagicMock
import os
import datetime

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
        self.assertEqual(rg["observation_type"], "IMAGING") # Now at top level
        
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
        self.assertEqual(windows[0]["epoch"], 2036)
        self.assertEqual(windows[1]["epoch"], 2037)
        self.assertEqual(windows[2]["epoch"], 2038)

if __name__ == "__main__":
    unittest.main()
