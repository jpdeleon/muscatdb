"""The [T14] section muscat-db writes must use harmonic's key format.

harmonic's ``predict()`` validates this section and rejects anything that is not
a bare planet letter, because ``scan_transits`` indexes the mapping by letter.
The section is built in JavaScript on the ephemeris page, so the writer is
checked here against the template source: a regression would only surface as a
``ConfigurationError`` from the harmonic CLI on a run created much later.
"""

from __future__ import annotations

import re
from pathlib import Path

_TEMPLATE = (
    Path(__file__).resolve().parents[1]
    / "src" / "muscat_db" / "templates" / "ephemeris.html"
)


def _t14_writer_line() -> str:
    """The single line that appends one entry to the generated [T14] section."""
    html = _TEMPLATE.read_text(encoding="utf-8")
    matches = [ln.strip() for ln in html.splitlines() if "t14Section +=" in ln and "=" in ln]
    body = [m for m in matches if "[T14]" not in m]
    assert len(body) == 1, f"expected one [T14] entry writer, found {len(body)}: {body}"
    return body[0]


def test_t14_entries_are_written_as_bare_planet_letters():
    line = _t14_writer_line()
    assert "`${pl} = " in line, f"[T14] entry is not keyed by bare planet letter: {line}"


def test_t14_entries_carry_no_prefix():
    """Guards the specific regression: `t14_b = ...` is what harmonic rejects."""
    line = _t14_writer_line()
    assert "t14_${pl}" not in line, f"prefixed [T14] key would fail harmonic validation: {line}"


def test_help_text_documents_the_same_format_it_writes():
    """The panel tells the observer what the section looks like; if it still
    advertises the prefixed form, someone hand-editing will reintroduce it."""
    html = _TEMPLATE.read_text(encoding="utf-8")
    section = re.search(r"<code>\[T14\]</code>.{0,400}", html, re.S)
    assert section, "could not locate the [T14] help paragraph"
    assert "t14_b" not in section.group(0), "help text still advertises the prefixed form"
