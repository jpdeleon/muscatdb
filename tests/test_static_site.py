"""Tests for the static-site builder (``muscat_db.static_site``).

The builder is exercised against a tiny throwaway DB built the same way the real
pipeline materializes its tables (``_summary_rows`` → ``_insert_summary_rows`` →
``_populate_targets``), so the captured pages go through the real route handlers
without needing the 3 GB production ``muscat.db`` or any figures on disk.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from muscat_db.coord import CoordRepr
from muscat_db.database import (
    SCHEMA,
    _insert_summary_rows,
    _populate_targets,
    _summary_rows,
    set_note,
)
from muscat_db.static_site import _rewrite_link, _scrub_host_paths, build_site

_SECRET_NOTE = "SECRETNOTE12345"


def _build_tiny_db(db_path: str) -> None:
    """Create a minimal but valid DB with two instruments and a noted target."""
    conn = sqlite3.connect(db_path)
    conn.create_aggregate("coord_repr", 2, CoordRepr)
    conn.executescript(SCHEMA)
    frame_rows = [
        ("muscat", "260101", 0, "MSCT0_2601010001", "M67", 1.0),
        ("muscat", "260101", 0, "MSCT0_2601010002", "M67", 2.0),
        ("muscat3", "260102", 0, "MSCT3_2601020001", "TOI-1", 3.0),
        ("muscat3", "260102", 0, "MSCT3_2601020002", "TOI-1", 4.0),
    ]
    conn.executemany(
        """INSERT INTO frames
           (instrument, obsdate, ccd, filename, object, jd_start, ut_start,
            exptime, read_mode, filter, ra, declination, airmass, focus, pa)
           VALUES (?, ?, ?, ?, ?, ?, '00:00:00', 10, 'fast', 'gp', '', '', 1, 0, 0)""",
        frame_rows,
    )
    rows = _summary_rows(conn)
    _insert_summary_rows(conn, rows)
    _populate_targets(conn)
    conn.execute(
        "INSERT OR REPLACE INTO db_meta (key, value) VALUES ('last_build_at', '1700000000')"
    )
    conn.commit()
    conn.close()
    # A user-authored note that must be scrubbed unless --keep-notes is set.
    set_note(db_path, "M67", _SECRET_NOTE)


@pytest.fixture
def tiny_db(tmp_path):
    db = tmp_path / "muscat.db"
    _build_tiny_db(str(db))
    return str(db)


def _read(path):
    return path.read_text(encoding="utf-8")


def test_builds_core_pages_and_scaffolding(tiny_db, tmp_path):
    out = tmp_path / "site"
    stats = build_site(
        out, db_path=tiny_db, n_examples=1, include_figures=False, log=lambda _m: None
    )

    assert stats.pages > 0
    # Root landing + a nav page + a drill-down all materialized as index.html.
    assert (out / "index.html").is_file()
    assert (out / "logs" / "index.html").is_file()
    assert (out / "muscat" / "index.html").is_file()
    assert (out / "muscat" / "260101" / "index.html").is_file()
    # Pages-required scaffolding.
    assert (out / ".nojekyll").is_file()
    assert (out / "static" / "styles.css").is_file()


def test_no_absolute_internal_links_remain(tiny_db, tmp_path):
    out = tmp_path / "site"
    build_site(out, db_path=tiny_db, n_examples=1, include_figures=False, log=lambda _m: None)

    for page in out.rglob("index.html"):
        html = _read(page)
        # Root-absolute internal links must all have been relativized. External
        # (https:, //), data: URIs, and fragments are left untouched.
        assert 'href="/' not in html, f"absolute href in {page}"
        assert 'src="/' not in html, f"absolute src in {page}"
        assert 'action="/' not in html, f"absolute action in {page}"


def test_static_cache_buster_stripped_and_depth_relative(tiny_db, tmp_path):
    out = tmp_path / "site"
    build_site(out, db_path=tiny_db, n_examples=1, include_figures=False, log=lambda _m: None)

    root_html = _read(out / "index.html")
    assert "styles.css?v=" not in root_html
    assert 'href="static/styles.css"' in root_html

    # A one-level-deep page links back up with a relative prefix.
    logs_html = _read(out / "logs" / "index.html")
    assert "../static/styles.css" in logs_html


def test_snapshot_banner_injected(tiny_db, tmp_path):
    out = tmp_path / "site"
    build_site(out, db_path=tiny_db, n_examples=1, include_figures=False, log=lambda _m: None)
    assert "snapshot-banner" in _read(out / "index.html")


def test_no_live_data_notice_only_on_live_api_pages(tiny_db, tmp_path):
    out = tmp_path / "site"
    build_site(out, db_path=tiny_db, n_examples=1, include_figures=False, log=lambda _m: None)
    notice = "No live data in this static snapshot"
    # Live-API shells get the content-area notice + empty-box labeller script.
    ephemeris = _read(out / "ephemeris" / "index.html")
    assert notice in ephemeris
    assert "static-nodata-note" in ephemeris
    assert "muscat-static-nolivedata" in ephemeris
    # Ordinary server-rendered pages must NOT get it (they show real content).
    assert notice not in _read(out / "index.html")
    assert notice not in _read(out / "logs" / "index.html")


def test_scrub_notes_removes_note_text(tiny_db, tmp_path):
    out = tmp_path / "site"
    build_site(
        out, db_path=tiny_db, scrub_notes=True, n_examples=1,
        include_figures=False, log=lambda _m: None,
    )
    for page in out.rglob("index.html"):
        assert _SECRET_NOTE not in _read(page)


def test_keep_notes_preserves_note_text(tiny_db, tmp_path):
    out = tmp_path / "site"
    build_site(
        out, db_path=tiny_db, scrub_notes=False, n_examples=1,
        include_figures=False, log=lambda _m: None,
    )
    combined = "".join(_read(p) for p in out.rglob("index.html"))
    assert _SECRET_NOTE in combined


def test_base_path_makes_links_root_absolute(tiny_db, tmp_path):
    out = tmp_path / "site"
    build_site(
        out, db_path=tiny_db, base_path="/muscat-db", n_examples=1,
        include_figures=False, log=lambda _m: None,
    )
    logs_html = _read(out / "logs" / "index.html")
    assert "/muscat-db/static/styles.css" in logs_html


def test_only_figure_files_are_published():
    figures = {}

    image_url = "/api/photometry/file/muscat/260101/lightcurve.png?v=1"
    assert _rewrite_link(image_url, "../", {}, figures) == (
        "../assets/photometry/muscat/260101/lightcurve.png"
    )
    assert figures == {
        image_url: "assets/photometry/muscat/260101/lightcurve.png"
    }

    for filename in ("measurements.csv", "fit.yaml", "run.log", "samples.csv.gz"):
        url = f"/api/transit-fit/file/muscat/260101/{filename}"
        assert _rewrite_link(url, "../", {}, figures) == "#"
        assert url not in figures

    traversal_url = "/api/photometry/file/../../outside.png"
    assert _rewrite_link(traversal_url, "../", {}, figures) == "#"
    assert traversal_url not in figures


def test_query_detail_links_keep_their_identity():
    route_map = {
        "/target": "target/Example",
        "/photometry": "photometry/muscat/260101/Example",
    }

    assert _rewrite_link("/target", "../", route_map, {}) == "../target/Example/"
    assert _rewrite_link("/target?name=Other+Target", "../", route_map, {}) == (
        "../target/OtherTarget/"
    )
    assert _rewrite_link(
        "/photometry?inst=muscat3&date=260102&target=TOI-1",
        "../", route_map, {},
    ) == "../photometry/muscat3/260102/TOI-1/"


def test_refuses_output_directory_containing_database(tiny_db, tmp_path):
    with pytest.raises(ValueError, match="contains the database"):
        build_site(tmp_path, db_path=tiny_db, log=lambda _m: None)

    assert (tmp_path / "muscat.db").is_file()


def test_scrub_host_paths_redacts_home_aliases(monkeypatch):
    monkeypatch.setattr("muscat_db.static_site.Path.home", lambda: Path("/home/alice"))
    html = (
        "/home/alice/conda/python "
        "/ut2/alice/ql/result.csv "
        "/raid_ut2/home/alice/project"
    )

    scrubbed = _scrub_host_paths(html)

    assert "alice" not in scrubbed
    assert scrubbed == "~/conda/python ~/ql/result.csv ~/project"
