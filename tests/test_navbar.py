"""Contracts for the shared current-page navbar treatment."""

from pathlib import Path

from bs4 import BeautifulSoup


ROOT = Path(__file__).resolve().parents[1]
BASE_HTML = ROOT / "src" / "muscat_db" / "templates" / "base.html"
STYLES_CSS = ROOT / "src" / "muscat_db" / "static" / "styles.css"


def test_visible_nav_links_have_unique_sections():
    soup = BeautifulSoup(BASE_HTML.read_text(), "html.parser")
    visible_links = [
        link for link in soup.select("nav a")
        if "display: none" not in link.get("style", "")
    ]
    sections = [link.get("data-nav-section") for link in visible_links]

    assert all(sections)
    assert len(sections) == len(set(sections))


def test_current_page_is_accessible_and_subtly_styled():
    base = BASE_HTML.read_text()
    styles = STYLES_CSS.read_text()

    assert "setAttribute('aria-current', 'page')" in base
    assert "path.indexOf('/lco/') === 0" in base
    assert "muscat2|muscat3|muscat4|sinistro" in base
    assert 'nav a[aria-current="page"]' in styles
    assert "text-decoration-thickness: 2px" in styles
