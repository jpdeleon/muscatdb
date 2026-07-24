"""Frontend input wiring contract.

This suite enforces the CLAUDE.md rule that *every* form input added to a
template is registered in the page's JavaScript persistence helpers
(``collectOptions``, ``restoreOptions``, and the defaults/clear listener) **and**
is actually consumed by the backend. Unlike a substring smoke-test, these checks
parse the real field IDs out of each template and cross-reference them against
the JS function bodies and the owning Python module, so they fail when someone
adds an ``<input id="opt-...">`` and forgets to wire it end-to-end.

Two levels of rigor:

* **Structural** (all option pages): template field IDs must appear in
  ``collectOptions`` / ``restoreOptions`` / the defaults listener.
* **Backend consumption**:
    - photometry keys must be referenced as literals in ``photometry.py``
      (clean ``normalize_run_options`` → ``build_command`` mapping).
    - transit-fit keys are verified *functionally* by building ``fit.yaml`` /
      ``sys.yaml`` from a fully-populated options dict and asserting the values
      land in the config timer actually reads (its consumption is f-string
      driven, so literal matching would be misleading).

Run with: pytest tests/test_frontend_input_audit.py -v
"""

import re
from pathlib import Path

import pytest
import yaml

from muscat_db import transit_fit as fit


HERE = Path(__file__).parent
PROJECT_ROOT = HERE.parent
SRC = PROJECT_ROOT / "src" / "muscat_db"
TEMPLATES_DIR = SRC / "templates"
WEB_PY = SRC / "web.py"
STYLES_CSS = SRC / "static" / "styles.css"


# --------------------------------------------------------------------------- #
# Parsing helpers
# --------------------------------------------------------------------------- #

def _read_template(name: str) -> str:
    return (TEMPLATES_DIR / name).read_text()


def _field_ids(html: str) -> set[str]:
    """Every ``opt-<field>`` id attached to a real form control in the template.

    Only ``<input>``/``<select>``/``<textarea>`` are matched, so container
    ``<details id="opt-panel">`` / ``<div id="opt-error">`` are excluded.
    """
    return set(
        re.findall(
            r'<(?:input|select|textarea)\b[^>]*\bid="opt-([A-Za-z0-9_]+)"', html
        )
    )


def _brace_body(text: str, start: int) -> str:
    """Return the ``{...}`` block (inclusive) starting at/after ``start``.

    The JS bodies here never contain literal braces inside string literals, so a
    plain depth counter is sufficient and keeps the test dependency-free.
    """
    open_idx = text.index("{", start)
    depth = 0
    for i in range(open_idx, len(text)):
        c = text[i]
        if c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0:
                return text[open_idx : i + 1]
    raise AssertionError("unbalanced braces while extracting JS body")


def _function_body(html: str, name: str) -> str:
    marker = f"function {name}("
    assert marker in html, f"{name}() not found in template"
    return _brace_body(html, html.index(marker))


def _click_handler_body(html: str, button_id: str) -> str:
    """Body of the ``click`` handler bound to ``getElementById('<button_id>')``."""
    anchor = html.index(f"'{button_id}'")
    listener = html.index("addEventListener", anchor)
    return _brace_body(html, listener)


def _py_function_src(text: str, def_name: str) -> str:
    """Source of a module-level Python function, up to the next top-level def."""
    start = text.index(f"def {def_name}(")
    rest = text[start:]
    # Stop at the next module-level ``def``/``@app.`` (column 0) after the first line.
    m = re.search(r"\n(?:def |@app\.)", rest[1:])
    return rest if m is None else rest[: m.start() + 1]


def _control_ids(html: str, prefix: str) -> set[str]:
    """Every form-control id starting with ``prefix`` (e.g. ``sch-``)."""
    return set(
        re.findall(
            rf'<(?:input|select|textarea)\b[^>]*\bid="({re.escape(prefix)}[A-Za-z0-9_-]+)"',
            html,
        )
    )


def _js_string_array(html: str, name: str) -> set[str]:
    """Quoted string entries of a ``var NAME = [ ... ];`` JS array literal."""
    start = html.index(f"var {name} = ")
    body = html[start : html.index("]", start) + 1]
    return set(re.findall(r"'([^']+)'", body))


def _mentions(field: str, region: str) -> bool:
    """True if ``field`` is referenced as a quoted token in ``region``.

    Matches ``'field'``/``"field"`` or the ``'opt-field'``/``"opt-field"`` id
    form. Quoting avoids false positives from prefix collisions such as
    ``teff`` inside ``teff_unc``.
    """
    return bool(
        re.search(rf"""['"](?:opt-)?{re.escape(field)}['"]""", region)
    )


# --------------------------------------------------------------------------- #
# Structural contract: template fields must be collected / restored / defaulted
# --------------------------------------------------------------------------- #

# Fields intentionally omitted from the defaults/clear listener, with rationale.
_DEFAULTS_EXCLUSIONS = {
    # The run label is a per-run identifier, not a tunable default; "Clear"
    # deliberately preserves it rather than blanking the user's run name.
    "transit_fit.html": {"run_name"},
    "photometry.html": set(),
}
_RESTORE_EXCLUSIONS = {
    "transit_fit.html": set(),
    "photometry.html": set(),
}

_OPTION_PAGES = [
    ("photometry.html", "defaults-btn"),
    ("transit_fit.html", "clear-btn"),
]


@pytest.mark.parametrize("page,_btn", _OPTION_PAGES)
def test_every_input_is_collected(page, _btn):
    html = _read_template(page)
    collect = _function_body(html, "collectOptions")
    missing = {f for f in _field_ids(html) if not _mentions(f, collect)}
    assert not missing, f"{page}: inputs missing from collectOptions(): {sorted(missing)}"


@pytest.mark.parametrize("page,_btn", _OPTION_PAGES)
def test_every_input_is_restored(page, _btn):
    html = _read_template(page)
    restore = _function_body(html, "restoreOptions")
    excluded = _RESTORE_EXCLUSIONS[page]
    missing = {
        f for f in _field_ids(html) if f not in excluded and not _mentions(f, restore)
    }
    assert not missing, f"{page}: inputs missing from restoreOptions(): {sorted(missing)}"


@pytest.mark.parametrize("page,btn", _OPTION_PAGES)
def test_every_input_has_a_default(page, btn):
    html = _read_template(page)
    handler = _click_handler_body(html, btn)
    excluded = _DEFAULTS_EXCLUSIONS[page]
    missing = {
        f for f in _field_ids(html) if f not in excluded and not _mentions(f, handler)
    }
    assert not missing, (
        f"{page}: inputs missing from the defaults listener: {sorted(missing)}. "
        f"If intentional, add to _DEFAULTS_EXCLUSIONS with a rationale."
    )


def test_photometry_run_options_update_without_reloading_page():
    """Pipeline options are live form state, not server-rendered view filters."""
    html = _read_template("photometry.html")
    options = html[
        html.index("// ----- options form -----"):
        html.index("// ----- copy command -----")
    ]

    assert "window.location.reload()" not in options
    assert "panel.addEventListener('change', debounce(refreshCmd, 150))" in options
    assert "saveOptions();" in _function_body(html, "refreshCmd")

    # Sinistro site/telescope/mode controls are the deliberate exception: they
    # filter server-rendered runs and outputs, so they navigate with URL state.
    sinistro_navigation = _function_body(html, "navigateSinistroFilters")
    assert "window.location.href = '/photometry?'" in sinistro_navigation


# --------------------------------------------------------------------------- #
# LCO schedule: inputs must be registered for persistence, and buildParams must
# supply every field build_requestgroup requires.
# --------------------------------------------------------------------------- #

def test_lco_schedule_inputs_registered_for_persistence():
    """Every sch-*/win-* control must be in TEXT_IDS/CHECK_IDS.

    collectOptions/restoreOptions iterate those arrays, so an unregistered
    input silently fails to persist or restore across navigation — the exact
    failure that leaves required scheduling fields blank on a saved-view load.
    """
    html = _read_template("lco_schedule.html")
    registered = _js_string_array(html, "TEXT_IDS") | _js_string_array(html, "CHECK_IDS")
    controls = _control_ids(html, "sch-") | _control_ids(html, "win-")
    # 'win-all' is a derived "select all windows" toggle, recomputed on every
    # render from the row checkboxes; persisting it would be meaningless.
    # 'sch-clone-id' is a one-shot action trigger (enter an id -> clone into the
    # form): it must NOT persist/restore, so it is deliberately unregistered.
    excluded = {"win-all", "sch-clone-id"}
    missing = controls - registered - excluded
    assert not missing, (
        f"lco_schedule.html inputs not registered in TEXT_IDS/CHECK_IDS "
        f"(won't persist/restore): {sorted(missing)}"
    )


def test_lco_required_fields_are_supplied_by_build_params():
    """build_requestgroup's required keys must all be produced by buildParams."""
    html = _read_template("lco_schedule.html")
    build_params = _function_body(html, "buildParams")
    lco_src = (SRC / "lco.py").read_text()
    # The required-field guard lists them as _REQUIRED_LABELS keys.
    required = set(re.findall(r'"(name|proposal|target_name|ra|dec)":', lco_src))
    assert {"name", "proposal", "target_name", "ra", "dec"} <= required
    # ra/dec are assembled from parseCoords(); the rest are direct keys.
    for key in ("name", "proposal", "target_name"):
        assert f"{key}:" in build_params, f"buildParams never sets '{key}'"
    assert "parseCoords()" in build_params, "buildParams must derive ra/dec from parseCoords()"


def test_lco_prediction_inputs_invalidate_generated_windows():
    html = _read_template("lco_schedule.html")
    registered = _js_string_array(html, "WINDOW_PREDICTION_IDS")
    expected = {
        "sch-target", "sch-planet", "sch-source", "sch-coords",
        "sch-range-start", "sch-range-end", "sch-t0", "sch-period",
        "sch-duration", "sch-pad-before", "sch-pad-after",
        "sch-include-padding", "sch-sites", "sch-twilight",
        "sch-obs-airmass", "sch-moon-sep", "sch-max-lunar-phase",
    }
    assert registered == expected
    assert "win-filter" not in registered

    invalidate = _function_body(html, "invalidateGeneratedWindows")
    assert "clearWindows()" in invalidate
    assert "lastDryRunHash = null" in invalidate
    assert "el('vis-figure').style.display = 'none'" in invalidate
    assert "Generate windows" in invalidate
    assert "node.addEventListener('input', invalidateGeneratedWindows)" in html
    assert "node.addEventListener('change', invalidateGeneratedWindows)" in html


def test_lco_submit_confirmation_uses_message_modal():
    """Live LCO submission must use the styled app modal, not a browser popup."""
    base = _read_template("base.html")
    html = _read_template("lco_schedule.html")
    assert "showConfirmModal" in base
    assert "showConfirmModal(" in html
    assert "window.confirm(" not in html


def test_no_native_browser_popups_in_templates_or_static_js():
    """Use the styled message modal instead of browser alert/confirm/prompt."""
    offenders = []
    for path in list((SRC / "templates").glob("*.html")) + list((SRC / "static").rglob("*.js")):
        text = path.read_text()
        for match in re.finditer(r"\b(?:window\.)?(?:alert|confirm|prompt)\s*\(", text):
            offenders.append(f"{path.relative_to(SRC)}:{text[:match.start()].count(chr(10)) + 1}")
    assert not offenders, "native browser popup calls found: " + ", ".join(offenders)


# --------------------------------------------------------------------------- #
# Photometry backend consumption: every collected key is used in photometry.py
# --------------------------------------------------------------------------- #

def _collect_object_keys(collect_body: str) -> set[str]:
    """Keys of the object literal returned by a ``key: val(...)`` collectOptions."""
    return set(re.findall(r"(\w+):\s*(?:val|chk)\(", collect_body))


def test_photometry_collected_keys_consumed_by_backend():
    html = _read_template("photometry.html")
    keys = _collect_object_keys(_function_body(html, "collectOptions"))
    assert keys, "failed to parse photometry collectOptions keys"

    phot_src = (SRC / "photometry.py").read_text()
    # Every key must be referenced as a string literal somewhere in the module
    # (normalize_run_options / validate_run_options / build_command).
    literals = set(re.findall(r"""['"]([a-z_][a-z0-9_]*)['"]""", phot_src))
    unconsumed = keys - literals
    assert not unconsumed, (
        f"photometry keys collected by the UI but never referenced in "
        f"photometry.py: {sorted(unconsumed)}"
    )


# --------------------------------------------------------------------------- #
# Transit-fit backend consumption: functional round-trip into fit.yaml/sys.yaml
# --------------------------------------------------------------------------- #

def _rich_fit_options() -> dict:
    """A fully-populated options payload with distinctive non-default values."""
    return {
        "planets": "b",
        "run_name": "audit",
        "chromatic": "true",
        "fit_basis": "ror",
        "trend": "true",
        "run_mode": "continue",  # -> clobber False
        "plot_midtransit": "false",
        "plot_ingress_egress": "false",
        "tune": "1234",
        "draws": "2345",
        "chains": "3",
        "cores": "4",
        "include_mean": "false",
        "use_custom_optimizer": "false",
        "secondary_eclipse": "true",
        "spline": "true",
        "spline_knots": "9",
        "add_bias": "true",
        "quadratic": "true",
        "clip": "true",
        "clip_nsig": "4.5",
        "chunk_offset": "true",
        "chunk_thresh": "0.25",
        "trim_beg": "7",
        "trim_end": "8",
        "use_gp": "true",
        "gp_log_amp": "-2.5",
        "gp_log_amp_unc": "1.5",
        "gp_log_amp_prior": "gaussian",
        "gp_log_scale": "-0.5",
        "gp_log_scale_unc": "1.25",
        "gp_log_scale_prior": "gaussian",
        "gp_per_dataset_log_amp": "true",
        "gp_per_dataset_log_scale": "false",
        "include_bump": "true",
        "chromatic_bump": "false",
        "bump_tcenter": "0.03,0.02",
        "bump_tcenter_prior": "gaussian",
        "bump_width": "0.04,0.01",
        "bump_width_prior": "gaussian",
        "bump_ampl": "0.05,0.01",
        "bump_ampl_prior": "gaussian",
        "include_flare": "true",
        "chromatic_flare": "false",
        "flare_tpeak": "0.06,0.02",
        "flare_tpeak_prior": "gaussian",
        "flare_fwhm": "0.07,0.01",
        "flare_fwhm_prior": "gaussian",
        "flare_ampl": "0.08,0.01",
        "flare_ampl_prior": "gaussian",
        "teff": "6100",
        "teff_unc": "150",
        "logg": "4.2",
        "logg_unc": "0.2",
        "feh": "0.3",
        "feh_unc": "0.05",
        "fixed": ["u_star"],
        # planet-scoped priors (first planet 'b')
        "period_b": "3.5",
        "period_unc_b": "0.001",
        "t0_b": "2459000.5",
        "t0_unc_b": "0.01",
        "dur_b": "0.12",
        "dur_unc_b": "0.01",
        "ror_b": "0.09",
        "ror_unc_b": "0.005",
        "b_b": "0.3",
        "b_unc_b": "0.1",
    }


@pytest.fixture()
def fit_config(tmp_path):
    fit._write_fit_inputs(tmp_path, "muscat4", "250512", "TOI-1234", [], _rich_fit_options())
    return (
        yaml.safe_load((tmp_path / "fit.yaml").read_text()),
        yaml.safe_load((tmp_path / "sys.yaml").read_text()),
    )


def test_transit_fit_sampler_and_model_options_wired(fit_config):
    fit_yaml, _ = fit_config
    assert fit_yaml["tune"] == 1234
    assert fit_yaml["draws"] == 2345
    assert fit_yaml["chains"] == 3
    assert fit_yaml["cores"] == 4
    assert fit_yaml["include_mean"] is False
    assert fit_yaml["use_custom_optimizer"] is False
    assert fit_yaml["secondary_eclipse"] is True
    assert fit_yaml["fit_basis"] == "ror"
    assert fit_yaml["chromatic"] is True
    assert fit_yaml["clobber"] is False  # run_mode="continue"
    assert fit_yaml["plot_midtransit"] is False
    assert fit_yaml["plot_ingress_egress"] is False
    assert fit_yaml["fixed"] == ["u_star"]


def test_transit_fit_gp_bump_flare_blocks_wired(fit_config):
    fit_yaml, _ = fit_config
    assert fit_yaml["use_gp"] is True
    gp = fit_yaml["gp"]
    assert gp["log_amp"] == -2.5 and gp["log_amp_unc"] == 1.5
    assert gp["log_scale"] == -0.5 and gp["log_scale_unc"] == 1.25
    assert gp.get("per_dataset") == ["log_amp"]  # only log_amp toggled on

    assert fit_yaml["include_bump"] is True
    assert fit_yaml["bump"]["tcenter"] == 0.03 and fit_yaml["bump"]["width"] == 0.04

    assert fit_yaml["include_flare"] is True
    assert fit_yaml["flare"]["tpeak"] == 0.06 and fit_yaml["flare"]["fwhm"] == 0.07


def test_transit_fit_stellar_and_planet_priors_wired(fit_config):
    _, sys_yaml = fit_config
    assert sys_yaml["star"]["teff"] == [6100.0, 150.0]
    assert sys_yaml["star"]["logg"] == [4.2, 0.2]
    assert sys_yaml["star"]["feh"] == [0.3, 0.05]
    planet = sys_yaml["planets"]["b"]
    assert planet["period"] == [3.5, 0.001]
    assert planet["t0"] == [2459000.5, 0.01]
    assert planet["ror"] == [0.09, 0.005]
    assert planet["b"] == [0.3, 0.1]


def test_transit_fit_detrending_options_wired(tmp_path):
    """Per-dataset detrending only appears when a light curve is present."""
    opts = _rich_fit_options()
    # Source CSV lives outside the run dir so _write_fit_inputs can copy it in
    # (copying onto itself would raise SameFileError).
    src_dir = tmp_path / "lc"
    src_dir.mkdir()
    csv = src_dir / "TOI-1234_muscat4_g_250512.csv"
    csv.write_text("BJD,flux\n2459000.0,1.0\n")
    rdir = tmp_path / "run"
    rdir.mkdir()
    fit._write_fit_inputs(rdir, "muscat4", "250512", "TOI-1234", [csv], opts)
    fit_yaml = yaml.safe_load((rdir / "fit.yaml").read_text())
    band = next(iter(fit_yaml["data"].values()))
    assert band["spline"] is True and band["spline_knots"] == 9
    assert band["add_bias"] is True and band["quadratic"] is True
    assert band["clip"] is True and band["clip_nsig"] == 4.5
    assert band["chunk_offset"] is True and band["chunk_thresh"] == 0.25
    assert band["trim_beg"] == 7 and band["trim_end"] == 8
    assert band["trend"] == 1


def test_transit_fit_archive_query_modal_uses_shared_style_variants():
    html = _read_template("transit_fit.html")
    base = _read_template("base.html")
    css = STYLES_CSS.read_text()

    assert "function modalOptions(opts)" in base
    assert 'id="message-modal" data-mode="message" data-kind="default"' in base
    assert "modal.dataset.mode = 'message'" in base
    assert "modal.dataset.mode = 'confirm'" in base
    assert 'modal.dataset.kind = opts.kind || \'default\'' in base
    assert '#message-modal[data-mode="message"] #message-modal-cancel' in css
    assert '#message-modal[data-kind="success"] .modal-title' in css
    assert '#message-modal[data-kind="error"] .modal-title' in css
    assert '#message-modal[data-kind="notice"] .modal-title' in css
    assert "showMessageModal('Notice', 'Please enter a target name.', 'notice')" in html
    assert "showMessageModal('Error', err.message, 'error')" in html
    assert "', 'success')" in html


def test_transit_center_prior_displays_timer_uniform_width_semantics():
    """The UI must not present timer's full-width field as a +/- uncertainty."""
    html = _read_template("transit_fit.html")
    add_card = _function_body(html, "addPlanetCard")

    assert "Transit-center prior" in add_card
    assert "not a Gaussian uncertainty" in add_card
    assert "Uniform prior: center, full width (days)" in add_card
    assert 'aria-label="Predicted transit center in BJD"' in add_card
    assert 'aria-label="Uniform prior full width in days"' in add_card
    assert 'class="planet-tc-prior-bounds"' not in add_card
    assert '<span style="color: var(--text-dim); font-size: 0.8rem;">±</span>' not in add_card


def test_transit_fit_results_summary_closes_its_layout_wrappers():
    """The downloads row must not be swallowed by an unclosed summary div."""
    html = _read_template("transit_fit.html")
    start = html.index("{% if outputs.summary %}")
    end = html.index("{% endif %}", start)
    summary_block = html[start:end]

    assert summary_block.count("<div") == summary_block.count("</div>")
    assert "</table>\n      </div>\n    </div>" in summary_block


def test_transit_fit_dense_option_rows_have_mobile_layout_hooks():
    html = _read_template("transit_fit.html")

    assert html.count('class="transit-option-grid"') >= 6
    assert html.count('class="fit-option-row"') == 2
    assert "@media (max-width: 560px)" in html


def test_ephemeris_disclosure_triangles_use_standard_size():
    html = _read_template("ephemeris.html")
    css = STYLES_CSS.read_text()

    # Every fold on this page uses one deterministic indicator instead of a
    # mix of undersized custom glyphs and browser-dependent native markers.
    assert html.count("ephemeris-fold") == 7
    rule = re.search(
        r"\.ephemeris-fold\s*>\s*summary::before\s*\{(?P<body>[^}]*)\}",
        css,
    )
    assert rule is not None
    assert "font-size: 1rem" in rule.group("body")

    assert html.count("phot-section phot-fold ephemeris-fold") == 3
    top_level_rule = re.search(
        r"\.ephemeris-fold\.phot-fold\s*>\s*summary::before\s*"
        r"\{(?P<body>[^}]*)\}",
        css,
    )
    assert top_level_rule is not None
    assert "font-size: 1.35rem" in top_level_rule.group("body")


def test_ephemeris_csv_preview_labels_notes_and_centers_dialog():
    html = _read_template("ephemeris.html")

    assert "<th>New epoch</th>" in html
    assert "<th>Note</th>" in html
    assert "<th>Page epoch</th>" not in html
    assert 'id="transit-csv-instrument"' in html
    assert "instrument: instrument" in html
    assert "const instrument = document.getElementById('transit-csv-instrument').value.trim()" in html
    dialog_rule = re.search(
        r"#transit-csv-dialog\s*\{(?P<body>[^}]*)\}", html
    )
    assert dialog_rule is not None
    body = dialog_rule.group("body")
    assert "position: fixed" in body
    assert "inset: 0" in body
    assert "margin: auto" in body


def test_ephemeris_ttv_fit_log_has_dedicated_new_tab_link():
    html = _read_template("ephemeris.html")

    assert 'file=harmonic.log`' in html
    assert 'target="_blank" rel="noopener">📄 fit run log</a>' in html
    assert "f !== 'harmonic.log'" in html


def test_ephemeris_utc_axis_preserves_plot_area_height():
    html = _read_template("ephemeris.html")

    assert "const OC_PLOT_BASE_HEIGHT = 450" in html
    assert "const OC_PLOT_UTC_AXIS_EXTRA_HEIGHT = 105" in html
    assert "const secondaryAxisExtraHeight = showTwin" in html
    assert "const plotHeight = OC_PLOT_BASE_HEIGHT + secondaryAxisExtraHeight" in html
    assert "plotDiv.style.height = plotHeight + 'px'" in html
    assert "height: plotHeight" in html
    assert "height: 600 + (showTwin" in html


def test_ephemeris_epoch_twin_axes_attach_to_each_planet_and_exclude_utc_axis():
    html = _read_template("ephemeris.html")

    assert 'id="show-epoch-checkbox"' in html
    assert "Show epoch numbers" in html
    assert "const OC_PLOT_EPOCH_AXIS_EXTRA_HEIGHT = 90" in html

    epoch_ticks = _function_body(html, "epochTwinTicks")
    assert ".map(p => Number(p.epoch))" in epoch_ticks
    assert "t0 + epoch * period - bjdOffset" in epoch_ticks
    assert "ticktext: shownEpochs.map(String)" in epoch_ticks

    draw_plot = _function_body(html, "drawPlot")
    assert "&& !showTwin" in draw_plot
    assert "const axisNumber = i + 2" in draw_plot
    assert "const yaxisRef = i === 0 ? 'y' : `y${i + 1}`" in draw_plot
    assert "title: `Epoch (planet ${pl.toLowerCase()})`" in draw_plot
    assert "anchor: yaxisRef" in draw_plot
    assert "overlaying: 'x'" in draw_plot

    view_state = _function_body(html, "collectEphemerisViewState")
    assert "show_epoch:" in view_state
    apply_state = _function_body(html, "applyViewStateToStorage")
    assert "typeof state.show_epoch === 'boolean'" in apply_state
    assert "if (state.show_epoch)" in apply_state

    assert "showTwinCheckbox.checked = false" in html
    assert "epochTwin.checked = false" in html
    assert "STORAGE_OPTS_PREFIX + 'show-epoch'" in html


def test_ephemeris_csv_import_saves_datasetless_view_before_success():
    html = _read_template("ephemeris.html")

    save_now = _function_body(html, "saveEphemerisViewNow")
    assert "loadedTargets.length === 0" in save_now
    assert "combinedDatasets.length" not in save_now
    assert "updateViewUrl(res.slug)" in save_now
    assert "window.importTransitCSVRows = async function()" in html
    assert "await saveEphemerisViewNow()" in html
    assert "added and saved in this view" in html


def test_ephemeris_unselect_clears_plot_without_deleting_imported_points():
    html = _read_template("ephemeris.html")

    remove_body = html.split("window.removeTarget = function(targetName) {", 1)[1].split("\n  };", 1)[0]
    clear_body = _function_body(html, "clearFitPresentation")

    assert "clearFitPresentation();" in remove_body
    assert "computeFitRequestSeq += 1" in clear_body
    assert "clearTimeout(computeFitTimer)" in clear_body
    assert "Plotly.purge(plot)" in clear_body
    assert "resultsGrid.innerHTML = ''" in clear_body
    assert "removeItem(manualStorageKey())" not in remove_body
    assert "removeItem(manualStorageKey())" not in clear_body


def test_ephemeris_manual_planet_card_is_added_inline_and_persisted():
    html = _read_template("ephemeris.html")

    assert 'id="add-planet-btn"' in html
    assert 'onclick="addManualPlanet()"' in html
    assert 'id="manual-planet-dialog"' not in html

    collect_state = _function_body(html, "collectEphemerisViewState")
    apply_state = _function_body(html, "applyViewStateToStorage")
    update_ui = _function_body(html, "updateCombinedUI")
    render_cards = _function_body(html, "renderPlanetCards")
    add_planet = html.split("window.addManualPlanet = function() {", 1)[1].split("\n  };", 1)[0]
    remove_planet = html.split("window.removeManualPlanet = function(planet) {", 1)[1].split("\n  };", 1)[0]

    assert "manual_planets:" in collect_state
    assert "Array.isArray(state.manual_planets)" in apply_state
    assert update_ui.index("loadManualPlanets()") < update_ui.index("renderPlanetCards(")
    assert 'value="manual"' in render_cards
    assert "!combinedPlanets.includes(planet)" in add_planet
    assert "opts[planet + '_t0'] = ''" in add_planet
    assert "opts[planet + '_period'] = ''" in add_planet
    assert "saveManualPlanets()" in add_planet
    assert "updateCombinedUI({skipFit: true})" in add_planet
    assert "manualPoints.some" in remove_planet
    assert "saveManualPlanets()" in remove_planet


def test_ephemeris_ttv_run_selection_is_preserved_in_shareable_url():
    html = _read_template("ephemeris.html")

    update_view_url = _function_body(html, "updateViewUrl")
    assert "const run = parseUrlRun()" in update_view_url
    assert "&run=${encodeURIComponent(run)}" in update_view_url

    update_run_url = _function_body(html, "updateTTVRunUrl")
    assert "url.searchParams.set('run', run)" in update_run_url
    assert "historyMode === 'push' ? 'pushState' : 'replaceState'" in update_run_url
    assert "window.addEventListener('popstate'" in html
    assert "selectedTTVRun = normalizedRun" in html

    load_outputs = _function_body(html, "loadTTVRunOutputs")
    assert "updateTTVRunUrl(runName, historyMode)" in load_outputs
    assert "loadTTVRunOutputs(target, run, runs, 'push')" in html


def test_ephemeris_selected_ttv_run_model_controls_extend_plot_to_utc_date():
    html = _read_template("ephemeris.html")

    assert 'id="ttv-model-controls"' in html
    assert 'id="show-ttv-model-checkbox"' in html
    assert 'id="ttv-model-end-date"' in html
    assert 'type="date"' in html
    assert "outputs.has_model" in html

    load_model = _function_body(html, "loadSelectedTTVModel")
    assert "run_name: runName" in load_model
    assert "qs.set('end_date', endDate)" in load_model
    assert "selectedTTVRun !== runName" in load_model
    assert "ttvModelRun = runName" in load_model

    model_trace = _function_body(html, "getTTVModelTrace")
    assert "ttvModelData?.points?.[planet]" in model_trace
    assert "(tc - (t0 + plotEpoch * period)) * 1440" in model_trace
    assert "TTV best fit" in model_trace

    draw_plot = _function_body(html, "drawPlot")
    assert "ttvModelRun === selectedTTVRun" in draw_plot
    assert "Math.max(sharedXSpan.max, requestedEndX)" in draw_plot
    assert "layout.xaxis.range" in draw_plot

    view_state = _function_body(html, "collectEphemerisViewState")
    assert "show_ttv_model:" in view_state
    assert "ttv_model_end_date:" in view_state
    apply_state = _function_body(html, "applyViewStateToStorage")
    assert "state.show_ttv_model" in apply_state
    assert "state.ttv_model_end_date" in apply_state


def test_ephemeris_plot_download_actions_are_bare_links():
    html = _read_template("ephemeris.html")

    assert '<a id="oc-summary-link" href="#" target="_blank" rel="noopener">tcs.txt</a>' in html
    assert '<a id="download-plot-btn" href="#">📥 Download PNG</a>' in html
    assert 'class="btn sm" id="oc-summary-link"' not in html
    assert '<button class="btn sm primary" type="button" id="download-plot-btn"' not in html
    assert 'class="dl-row" style="align-items: center; margin-top: 0;"' in html

    download_plot = _function_body(html, "downloadPlotPNG")
    assert "event.preventDefault()" in download_plot


def test_ephemeris_dataset_run_name_uses_transit_coverage_suffix():
    html = _read_template("ephemeris.html")

    render_table = _function_body(html, "renderDatasetsTable")
    assert "['full', 'ing', 'egr'].includes(d.transit_coverage)" in render_table
    assert "${d.run_name}${coverageSuffix}" in render_table
    assert "runTypeBadge" not in render_table


def test_ephemeris_dataset_target_column_only_shows_for_multiple_unique_targets():
    html = _read_template("ephemeris.html")

    target_visibility = _function_body(html, "showDatasetTargetColumn")
    assert "new Set(" in target_visibility
    assert "loadedTargets.map" in target_visibility
    assert "uniqueTargetNames.size > 1" in target_visibility

    render_table = _function_body(html, "renderDatasetsTable")
    assert "const showTargetColumn = showDatasetTargetColumn()" in render_table
    assert "targetHeader.style.display = showTargetColumn ? '' : 'none'" in render_table
    assert "const columnCount = showTargetColumn ? 8 : 7" in render_table
    assert "renderManualRow(tbody, mp, showTargetColumn)" in render_table

    manual_row = _function_body(html, "renderManualRow")
    assert "showTargetColumn ? `<td>" in manual_row


def test_ephemeris_dataset_table_epochs_follow_selected_reference_ephemeris():
    html = _read_template("ephemeris.html")

    assert 'E=0 is the chosen planetary reference epoch">Epoch</th>' in html

    render_table = _function_body(html, "renderDatasetsTable")
    assert "const epoch = planetEpochForTC(pl, Number(item.tc))" in render_table
    assert 'class="badge dataset-epoch"' in render_table
    assert "${epochsSummary}" in render_table
    assert "pl.toLowerCase())}: ${epochText}" not in render_table

    raw_ephem = _function_body(html, "rawPlanetEphemeris")
    assert "card.querySelector('.planet-t0')" in raw_ephem
    assert "card.querySelector('.planet-period')" in raw_ephem

    epoch_for_tc = _function_body(html, "planetEpochForTC")
    assert "effectivePlanetEphemeris(planet)" in epoch_for_tc
    assert "Math.round((tc - ephem.t0) / ephem.period)" in epoch_for_tc

    refresh_epochs = _function_body(html, "refreshDisplayedEpochs")
    assert "document.querySelectorAll('.dataset-epoch')" in refresh_epochs
    assert "document.querySelectorAll('.manual-epoch')" in refresh_epochs
    assert "planet.toLowerCase()" not in refresh_epochs

    schedule_fit = _function_body(html, "scheduleComputeFit")
    assert "refreshDisplayedEpochs()" in schedule_fit


def test_ephemeris_reference_epoch_can_center_on_included_dataset_midpoint():
    html = _read_template("ephemeris.html")

    assert 'id="center-epoch-checkbox"' in html
    assert "Shift reference epoch near dataset midpoint" in html
    assert "T0′ = chosen T0 + N × period" in html

    epoch_shift = _function_body(html, "planetEpochShift")
    assert "document.querySelectorAll('.dataset-checkbox:checked')" in epoch_shift
    assert "if (!mp.checked || mp.planet !== planet" in epoch_shift
    assert "const eMin = Math.min(...epochs)" in epoch_shift
    assert "const eMax = Math.max(...epochs)" in epoch_shift
    assert "eMin + Math.floor((eMax - eMin) / 2)" in epoch_shift

    effective_ephem = _function_body(html, "effectivePlanetEphemeris")
    assert "ephem.t0 + epochShift * ephem.period" in effective_ephem

    fit_request = _function_body(html, "collectFitRequest")
    assert "const epochShift = planetEpochShift(pl)" in fit_request
    assert "t0: t0 + epochShift * period" in fit_request

    view_state = _function_body(html, "collectEphemerisViewState")
    assert "center_epoch:" in view_state
    apply_state = _function_body(html, "applyViewStateToStorage")
    assert "typeof state.center_epoch === 'boolean'" in apply_state
    assert "cb.checked = state.center_epoch" in apply_state

    assert "STORAGE_OPTS_PREFIX + 'center-epoch'" in html
    assert "centerEpochCheckbox.addEventListener('change'" in html


def test_ephemeris_imported_csv_epoch_is_shown_only_in_epoch_column():
    html = _read_template("ephemeris.html")

    manual_row = _function_body(html, "renderManualRow")
    assert "CSV E=" not in manual_row
    assert 'class="manual-epoch"' in manual_row

    # Preserve the source epoch for validation/provenance even though the
    # transit-center cell no longer displays a second, competing epoch badge.
    collect_manual = _function_body(html, "collectManualPointsForRequest")
    assert "source_epoch: mp.source_epoch" in collect_manual


def test_ephemeris_bjd_axis_offset_uses_effective_reference_epoch():
    html = _read_template("ephemeris.html")

    plot_offset = _function_body(html, "plotBjdOffset")
    assert "fitResults?.[pl]?.t0_ref" in plot_offset
    assert "Math.round(referenceT0)" in plot_offset
    assert "Math.floor(firstPoint.bjd)" in plot_offset

    draw_plot = _function_body(html, "drawPlot")
    assert "const referenceBjdOffset = plotBjdOffset(planets, showExcluded)" in draw_plot
    assert "const bjdOffset = foundBjdOffset ? referenceBjdOffset : 2450000" in draw_plot
    assert "`Time (BJD - ${bjdOffset})`" in draw_plot


def test_ephemeris_cached_fit_results_match_current_inputs():
    html = _read_template("ephemeris.html")

    # A target-only cache key can retain points from datasets that the backend
    # no longer returns. Cache entries must therefore be versioned and tied to
    # the exact request that generated their Plotly results.
    assert "const FIT_RESULTS_CACHE_VERSION = 2" in html
    assert "function fitRequestSignature(request)" in html
    assert "fitCache.version === FIT_RESULTS_CACHE_VERSION" in html
    assert "fitCache.signature === fitRequestSignature(cachedRequest)" in html
    assert "signature: fitRequestSignature(request)" in html
    assert "localStorage.removeItem(fitResultsCacheKey(combinedKey))" in html


def test_ephemeris_plot_primary_x_axis_is_only_integer_offset_bjd_time():
    html = _read_template("ephemeris.html")

    assert 'name="x-axis-type"' not in html
    assert 'value="epoch"' not in html
    assert "const x = p.bjd - bjdOffset" in html

    draw_plot = _function_body(html, "drawPlot")
    assert "title: bjdAxisTitle" in draw_plot
    assert "xType" not in draw_plot

    plot_offset = _function_body(html, "plotBjdOffset")
    assert "Math.round(referenceT0)" in plot_offset


def test_ephemeris_imported_plot_point_scrolls_to_editable_table_row():
    html = _read_template("ephemeris.html")

    traces = _function_body(html, "getPlotlyTracesForPlanet")
    assert "manual_id: p.manual_id" in traces
    assert "imported: !!p.source_file" in traces
    assert "Click point to edit imported row" in traces

    draw_plot = _function_body(html, "drawPlot")
    assert "if (cd.manual)" in draw_plot
    assert "focusManualPointRow(cd.manual_id)" in draw_plot
    assert "window.open(url, '_blank')" in draw_plot

    focus_row = _function_body(html, "focusManualPointRow")
    assert "section.open = true" in focus_row
    assert "candidate.dataset.mid === String(manualId)" in focus_row
    assert "row.classList.add('manual-row-focus')" in focus_row
    assert "row.scrollIntoView({behavior: 'smooth', block: 'center'})" in focus_row
    assert "tcInput.focus({preventScroll: true})" in focus_row
    assert "row.classList.remove('manual-row-focus')" in focus_row

    assert "tr.manual-row-focus" in html


# --------------------------------------------------------------------------- #
# Endpoint coverage: frontend fetch targets have backend handlers
# --------------------------------------------------------------------------- #

class TestBackendEndpoints:
    def test_photometry_endpoints(self):
        from muscat_db.web import app
        from starlette.routing import Route
        routes = [(r.path, r.methods) for r in app.routes if isinstance(r, Route)]
        
        def assert_has_endpoint(path_suffix: str, method: str):
            for path, methods in routes:
                if method in methods:
                    if path == path_suffix or path == f"/api{path_suffix}":
                        return
            pytest.fail(f"missing endpoint for {path_suffix} with method {method}")
            
        assert_has_endpoint("/api/photometry/run", "POST")
        assert_has_endpoint("/api/photometry/command", "POST")
        assert_has_endpoint("/api/photometry/cancel", "POST")
        assert_has_endpoint("/api/photometry/status", "GET")
        assert_has_endpoint("/api/photometry/status-batch", "POST")

    def test_transit_fit_endpoints(self):
        from muscat_db.web import app
        from starlette.routing import Route
        routes = [(r.path, r.methods) for r in app.routes if isinstance(r, Route)]
        
        def assert_has_endpoint(path_suffix: str, method: str):
            for path, methods in routes:
                if method in methods:
                    if path == path_suffix or path == f"/api{path_suffix}":
                        return
            pytest.fail(f"missing endpoint for {path_suffix} with method {method}")
            
        assert_has_endpoint("/api/transit-fit/run", "POST")
        assert_has_endpoint("/api/transit-fit/cancel", "POST")

    def test_jobs_endpoints(self):
        from muscat_db.web import app
        from starlette.routing import Route
        routes = [(r.path, r.methods) for r in app.routes if isinstance(r, Route)]
        
        def assert_has_endpoint(path_suffix: str, method: str, exact: bool = True):
            for path, methods in routes:
                if method in methods:
                    if exact:
                        if path == path_suffix or path == f"/api{path_suffix}":
                            return
                    else:
                        if path.startswith(path_suffix) or path.startswith(f"/api{path_suffix}"):
                            return
            pytest.fail(f"missing endpoint for {path_suffix} with method {method}")
            
        assert_has_endpoint("/api/jobs/rerun", "POST")
        assert_has_endpoint("/api/jobs/status", "GET")
        assert_has_endpoint("/api/jobs/log/", "GET", exact=False)

    def test_exposure_and_fov_and_lco_endpoints(self):
        from muscat_db.web import app
        from starlette.routing import Route
        handler_names = {r.endpoint.__name__ for r in app.routes if isinstance(r, Route)}
        for handler in (
            "exposure_calculate",
            "api_fov_optimize",
            "api_lco_ipp",
            "api_lco_submit",
        ):
            assert handler in handler_names, f"missing handler: {handler}"

    def test_exposure_payload_keys_consumed(self):
        """Keys the exposure form posts are all read by the handler."""
        web = WEB_PY.read_text()
        body = _py_function_src(web, "exposure_calculate")
        for key in ("instrument", "mags", "focus_mm", "airmass", "sat_frac",
                    "mode", "exptime", "target_adu", "confmode"):
            assert f'"{key}"' in body, f"exposure_calculate ignores '{key}'"

    def test_fov_payload_keys_consumed(self):
        web = WEB_PY.read_text()
        body = _py_function_src(web, "api_fov_optimize")
        for key in ("instrument", "target", "ra", "dec", "margin_arcsec",
                    "comp_margin_arcsec", "mag_limit", "mag_min", "mag_max",
                    "mag_delta", "avoid_mag", "allow_rotation", "sinistro_mode"):
            assert f'"{key}"' in body, f"api_fov_optimize ignores '{key}'"


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
