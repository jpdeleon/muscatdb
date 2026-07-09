"""Shared fixtures + resource-availability guards for the muscat_db test suite.

The fast suite is designed to "skip cleanly off-host": a few tests need
resources that only exist on a configured MuSCAT host — chiefly the large
NASA/TOI catalog CSVs under ``data/`` (git-ignored, so absent on CI and fresh
checkouts). The :func:`catalog` fixture lets those tests skip instead of failing
there, while still running wherever the catalogs are present.

It also resets ``muscat_db.web``'s module-level catalog caches between tests:
those caches are keyed by target name and persist for the process lifetime, so a
test that queries a catalog while the data is unavailable can otherwise poison a
later test with an empty cached result.
"""

from __future__ import annotations

from pathlib import Path

import pytest

_DATA_DIR = Path(__file__).resolve().parent.parent / "data"


def catalog_available() -> bool:
    """True when the NASA + TOI catalog CSVs are present under ``data/``."""
    return (_DATA_DIR / "nexsci_pscomppars.csv").is_file() and (_DATA_DIR / "TOIs.csv").is_file()


@pytest.fixture
def catalog():
    """Skip a test unless the local NASA/TOI catalog CSVs are available.

    The CSVs are git-ignored (too large to track), so they are absent on CI and
    fresh clones; tests that read them skip there rather than fail.
    """
    if not catalog_available():
        pytest.skip("NASA/TOI catalog CSVs under data/ are unavailable (skips off-host)")


# Module-level caches in muscat_db.web that must be cleared between tests so a
# negative/empty result cached under one test's data configuration does not leak
# into another. LRUCache and plain dicts both expose .clear().
_WEB_CACHE_ATTRS = (
    "_CATALOG_CACHE",
    "_index_cache",
    "_toi_cache",
    "_toi_db_cache",
    "_nexsci_cache",
    "_harps_cache",
    "_boyle_cache",
)


@pytest.fixture(autouse=True)
def _reset_web_catalog_caches():
    """Clear muscat_db.web's process-level catalog caches before each test."""
    web = __import__("sys").modules.get("muscat_db.web")
    if web is not None:
        for attr in _WEB_CACHE_ATTRS:
            cache = getattr(web, attr, None)
            if cache is not None and hasattr(cache, "clear"):
                cache.clear()
    yield
