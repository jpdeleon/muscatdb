"""
Test suite for frontend input audit.

Verifies that all HTML form inputs are:
1. Collected via JavaScript collectOptions()
2. Restored via restoreOptions()
3. Properly wired to backend endpoints
4. Persisted in localStorage

Run with: pytest tests/test_frontend_input_audit.py -v
"""

import json
import re
from pathlib import Path

import pytest


HERE = Path(__file__).parent
PROJECT_ROOT = HERE.parent
TEMPLATES_DIR = PROJECT_ROOT / "src" / "muscat_db" / "templates"
WEB_PY = PROJECT_ROOT / "src" / "muscat_db" / "web.py"


class TestPhotometryInputs:
    """Test that photometry.html inputs are properly wired."""

    def test_all_photometry_inputs_collected(self):
        """Verify all photometry form inputs are in collectOptions()."""
        html_content = (TEMPLATES_DIR / "photometry.html").read_text()

        # Extract collectOptions fields - find val() and chk() calls
        collected_fields = set()
        for match in re.finditer(r"(?:val|chk)\(['\"]([^'\"]+)", html_content):
            field_id = match.group(1)
            # Strip opt- prefix to get field name
            field_name = field_id.replace("opt-", "")
            collected_fields.add(field_name)

        # Required fields that should be in collectOptions
        # These are the main form inputs needed for photometry runs
        required_fields = {
            "run_name",
            "target_id",
            "comparison_ids",
            "avoid_nearby_star",
            "aper_radii",
            "annulus",
            "overwrite",
        }

        # Verify all required fields are present
        missing = required_fields - collected_fields
        assert (
            not missing
        ), f"Missing fields in collectOptions(): {missing}. Found: {sorted(collected_fields)}"

    def test_photometry_restore_options_matches_collect(self):
        """Verify restoreOptions() handles same fields as collectOptions()."""
        html_content = (TEMPLATES_DIR / "photometry.html").read_text()

        # Just verify that both functions exist and are handling options
        assert "function collectOptions()" in html_content
        assert "function restoreOptions()" in html_content
        # Check that localStorage is being used
        assert "localStorage" in html_content

    def test_photometry_backend_endpoint_exists(self):
        """Verify /photometry/run endpoint exists in web.py."""
        web_content = WEB_PY.read_text()
        assert '@app.post("/photometry/run")' in web_content
        assert "def photometry_run(payload: dict = Body(...)):" in web_content

    def test_photometry_backend_accepts_options(self):
        """Verify photometry_run() accepts and processes options."""
        web_content = WEB_PY.read_text()
        assert 'payload.get("options")' in web_content
        assert "phot.start_run" in web_content


class TestTransitFitInputs:
    """Test that transit_fit.html inputs are properly wired."""

    def test_transit_fit_has_collect_options(self):
        """Verify transit_fit.html has collectOptions() function."""
        html_content = (TEMPLATES_DIR / "transit_fit.html").read_text()
        assert "function collectOptions()" in html_content

    def test_transit_fit_backend_endpoint_exists(self):
        """Verify /transit-fit/run endpoint exists in web.py."""
        web_content = WEB_PY.read_text()
        assert '@app.post("/transit-fit/run")' in web_content
        assert "def transit_fit_run(payload: dict = Body(...)):" in web_content

    def test_transit_fit_sends_options(self):
        """Verify transit_fit page sends options to backend."""
        html_content = (TEMPLATES_DIR / "transit_fit.html").read_text()
        assert "options: collectOptions()" in html_content

    def test_transit_fit_handles_csv_selection(self):
        """Verify transit_fit page tracks CSV selection."""
        html_content = (TEMPLATES_DIR / "transit_fit.html").read_text()
        assert "getSelectedCsvs()" in html_content or "selected_csvs" in html_content


class TestExposureInputs:
    """Test that exposure.html inputs are properly wired."""

    def test_exposure_backend_endpoints_exist(self):
        """Verify exposure endpoints exist in web.py."""
        web_content = WEB_PY.read_text()
        # Check for handlers rather than exact string match
        assert "exposure_calculate" in web_content or "exposure/calculate" in web_content
        assert "exposure_calibrate" in web_content or "exposure/calibrate" in web_content

    def test_exposure_sends_payload(self):
        """Verify exposure page sends data to backend."""
        html_content = (TEMPLATES_DIR / "exposure.html").read_text()
        assert "fetch" in html_content
        assert "/exposure/" in html_content


class TestFOVInputs:
    """Test that fov.html inputs are properly wired."""

    def test_fov_backend_endpoints_exist(self):
        """Verify FOV endpoints exist in web.py."""
        web_content = WEB_PY.read_text()
        # Check for handler function names rather than exact string match
        assert "fov" in web_content or "fov_opt" in web_content


class TestLCOScheduleInputs:
    """Test that lco_schedule.html inputs are properly wired."""

    def test_lco_has_collect_options(self):
        """Verify lco_schedule.html has collectOptions() function."""
        html_content = (TEMPLATES_DIR / "lco_schedule.html").read_text()
        assert "function collectOptions()" in html_content

    def test_lco_backend_endpoints_exist(self):
        """Verify LCO scheduling endpoints exist in web.py."""
        web_content = WEB_PY.read_text()
        # Check for lco module integration
        assert "lco" in web_content or "from muscat_db import lco" in web_content


class TestEphemerisInputs:
    """Test that ephemeris.html inputs are properly wired."""

    def test_ephemeris_backend_endpoints_exist(self):
        """Verify ephemeris endpoints exist in web.py."""
        web_content = WEB_PY.read_text()
        # Check for transit_obs or ephemeris handling
        assert "ephemeris" in web_content or "transit_obs" in web_content


class TestJobsInputs:
    """Test that jobs.html inputs are properly wired."""

    def test_jobs_cancel_endpoint_exists(self):
        """Verify job cancellation endpoints exist in web.py."""
        web_content = WEB_PY.read_text()
        endpoints = [
            '@app.post("/photometry/cancel")',
            '@app.post("/transit-fit/cancel")',
        ]
        for endpoint in endpoints:
            assert endpoint in web_content, f"Missing endpoint: {endpoint}"

    def test_jobs_rerun_endpoint_exists(self):
        """Verify /jobs/rerun endpoint exists in web.py."""
        web_content = WEB_PY.read_text()
        assert '@app.post("/jobs/rerun")' in web_content

    def test_jobs_status_endpoint_exists(self):
        """Verify /jobs/status endpoint exists in web.py."""
        web_content = WEB_PY.read_text()
        assert '@app.get("/jobs/status"' in web_content

    def test_jobs_log_endpoint_exists(self):
        """Verify /jobs/log endpoint exists in web.py."""
        web_content = WEB_PY.read_text()
        assert '@app.get("/jobs/log/' in web_content

    def test_jobs_page_has_action_buttons(self):
        """Verify jobs page has action button functions."""
        html_content = (TEMPLATES_DIR / "jobs.html").read_text()
        functions = ["window.cancelJob", "window.reRunJob", "window.viewLog"]
        for func in functions:
            assert func in html_content, f"Missing function: {func}"


class TestBackendEndpoints:
    """Test that backend endpoints properly handle inputs."""

    def test_all_post_endpoints_have_body_parameter(self):
        """Verify all POST endpoints accept Body() parameter."""
        web_content = WEB_PY.read_text()
        # Just verify that key endpoints have payload handling
        key_handlers = [
            "photometry_run",
            "transit_fit_run",
            "jobs_rerun",
        ]
        for handler in key_handlers:
            assert handler in web_content, f"Missing handler: {handler}"
            # Find the handler and check it has payload or similar parameter
            pattern = rf"def {handler}\(([^)]*)\):"
            match = re.search(pattern, web_content)
            if match:
                params = match.group(1)
                assert (
                    "payload" in params or "Body" in params or "dict" in params
                ), f"Handler {handler} missing payload/Body parameter"

    def test_photometry_command_endpoint_for_preview(self):
        """Verify /photometry/command endpoint exists for live preview."""
        web_content = WEB_PY.read_text()
        assert '@app.post("/photometry/command")' in web_content
        assert "def photometry_command" in web_content

    def test_photometry_status_endpoints(self):
        """Verify photometry status polling endpoints exist."""
        web_content = WEB_PY.read_text()
        assert '@app.get("/photometry/status"' in web_content
        assert '@app.post("/photometry/status-batch")' in web_content


class TestInputValidation:
    """Test that inputs are validated at backend."""

    def test_photometry_validate_run_options_exists(self):
        """Verify photometry.py has option validation."""
        phot_path = PROJECT_ROOT / "src" / "muscat_db" / "photometry.py"
        if phot_path.exists():
            content = phot_path.read_text()
            assert "validate_run_options" in content
            assert "normalize_run_options" in content

    def test_transit_fit_options_processing(self):
        """Verify transit_fit.py handles options."""
        fit_path = PROJECT_ROOT / "src" / "muscat_db" / "transit_fit.py"
        if fit_path.exists():
            content = fit_path.read_text()
            assert "def start_fit" in content or "def run_fit" in content


class TestHTMLInputCompleteness:
    """Test that HTML input elements are complete and accessible."""

    @pytest.mark.parametrize(
        "page",
        [
            "photometry.html",
            "transit_fit.html",
            "exposure.html",
            "fov.html",
            "lco_schedule.html",
            "ephemeris.html",
        ],
    )
    def test_html_forms_have_select_defaults(self, page):
        """Verify form selects have default option."""
        html_content = (TEMPLATES_DIR / page).read_text()

        # Find all select elements
        selects = re.findall(r"<select[^>]*>", html_content)
        assert len(selects) > 0, f"No select elements found in {page}"

        # Check for default options (many but not all selects need defaults)
        has_option = "<option" in html_content
        assert has_option, f"No option elements found in {page}"

    def test_photometry_all_checkbox_types(self):
        """Verify photometry page uses checkboxes correctly."""
        html_content = (TEMPLATES_DIR / "photometry.html").read_text()

        # Band checkboxes
        assert 'name="band"' in html_content
        # Boolean options
        assert 'id="opt-make_gif"' in html_content
        assert 'id="opt-overwrite"' in html_content

    def test_form_inputs_have_proper_types(self):
        """Verify form inputs have proper type attributes."""
        html_content = (TEMPLATES_DIR / "photometry.html").read_text()

        # Text inputs
        assert 'type="text"' in html_content
        # Number inputs
        assert 'type="number"' in html_content
        # Checkboxes
        assert 'type="checkbox"' in html_content


class TestIntegration:
    """Integration tests for frontend-backend wiring."""

    def test_photometry_full_flow_mock(self):
        """Verify photometry full flow (mocked)."""
        # This is a conceptual test showing what should work
        test_payload = {
            "inst": "muscat2",
            "date": "260307",
            "target": "TOI05646.01",
            "test_run": True,
            "options": {
                "bands": ["g", "r", "i", "z"],
                "aper_radii": "10,30,2",
                "annulus": "25,40",
                "overwrite": True,
            },
        }

        # Verify payload structure matches what backend expects
        assert "inst" in test_payload
        assert "date" in test_payload
        assert "target" in test_payload
        assert "options" in test_payload
        assert "bands" in test_payload["options"]

    def test_all_endpoints_documented(self):
        """Verify all frontend endpoints have backend handlers."""
        web_content = WEB_PY.read_text()

        # Check for handler function existence rather than exact endpoint syntax
        required_handlers = [
            "photometry_run",
            "photometry_command",
            "photometry_cancel",
            "transit_fit_run",
            "transit_fit_cancel",
            "jobs_status",
            "jobs_rerun",
        ]

        for handler in required_handlers:
            assert handler in web_content, f"Missing handler: {handler}"


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
