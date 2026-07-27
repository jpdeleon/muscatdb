"""Tests for ``_ttv_rank_helper``, which normally runs under the harmonic env.

The helper imports ``harmonic`` at module scope, so it cannot be imported here
(harmonic lives in its own conda environment and is absent from CI). The module
is loaded with the harmonic imports stubbed, which is enough to exercise the
pure-python config handling that sits between muscat-db and harmonic.
"""

from __future__ import annotations

import importlib.util
import sys
import types
from pathlib import Path

import pytest

_HELPER = Path(__file__).resolve().parents[1] / "src" / "muscat_db" / "_ttv_rank_helper.py"


@pytest.fixture(scope="module")
def helper():
    stubs = {
        "harmonic": types.ModuleType("harmonic"),
        "harmonic.fisher": types.ModuleType("harmonic.fisher"),
        "harmonic.harmonic": types.ModuleType("harmonic.harmonic"),
        "harmonic.predict": types.ModuleType("harmonic.predict"),
    }
    stubs["harmonic.fisher"].rank_transits = lambda *a, **k: None
    stubs["harmonic.harmonic"].Harmonic = object
    stubs["harmonic.predict"].scan_transits = lambda *a, **k: None
    saved = {name: sys.modules.get(name) for name in stubs}
    sys.modules.update(stubs)
    try:
        spec = importlib.util.spec_from_file_location("_ttv_rank_helper_undertest", _HELPER)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        yield module
    finally:
        for name, previous in saved.items():
            if previous is None:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = previous


def test_t14_prefix_is_stripped_for_scan_transits(helper):
    """``scan_transits`` indexes durations by bare planet letter.

    muscat-db's ephemeris page writes this section as ``t14_b``, so passing the
    config keys through unchanged raises ``KeyError`` on every run this
    application has produced.
    """
    assert helper._planet_key("t14_b") == "b"
    assert helper._planet_key("T14_C") == "c"


def test_bare_planet_letters_are_left_alone(helper):
    """harmonic's own example configs use the bare letter; both must work."""
    assert helper._planet_key("b") == "b"
    assert helper._planet_key(" d ") == "d"


def _fake_harmonic_env(monkeypatch, helper, captured):
    """Minimal stand-ins for the harmonic objects ``evaluate`` drives."""
    import pandas as pd

    class _Spec:
        t_ref = 0.0
        names = ("P_b", "T0_b")

    class _Harmonic:
        def __init__(self, **kwargs):
            self.flatchain = "chain"
            self.ephem = "ephem"
            self.planet_letters = ["b"]
            self.non_transiting_outer = False
            self.phase_offsets = False
            self.spec = _Spec()
            self.times = pd.DataFrame({"planet": ["b", "b"], "tc_unc": [0.002, 0.004]})

        def _require_chain(self):
            return None

    def _scan_transits(flatchain, ephem, letters, nto, t14s, window, *a, **k):
        captured["t14s"] = t14s
        return "transits"

    def _rank_transits(*a, **k):
        return pd.DataFrame([{
            "planet": "b", "epoch": 7, "sigma": 0.002, "gain_total": 1.5,
            "gain_ttv": 0.9, "greedy_rank": 1, "greedy_gain": 0.9,
        }])

    monkeypatch.setattr(helper, "Harmonic", _Harmonic)
    monkeypatch.setattr(helper, "scan_transits", _scan_transits)
    monkeypatch.setattr(helper, "rank_transits", _rank_transits)


def test_evaluate_hands_scan_transits_bare_planet_letters(helper, monkeypatch, tmp_path):
    """Guards the wiring, not just the helper: a muscat-db-written config must
    reach ``scan_transits`` with keys it can index by planet letter."""
    (tmp_path / "fit_config.json").write_text('{"letters": "b", "t_offset": 0}')
    (tmp_path / "config.ini").write_text("[T14]\nt14_b = 0.269\n")

    captured = {}
    _fake_harmonic_env(monkeypatch, helper, captured)
    result = helper.evaluate(tmp_path, ["2026-08-01", "2026-09-01"], "ttv")

    assert captured["t14s"] == {"b": 0.269}, "scan_transits would raise KeyError"
    assert result["rows"][0]["planet"] == "b"
    assert result["rows"][0]["greedy_rank"] == 1
