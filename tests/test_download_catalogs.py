"""Contracts for the catalog snapshots downloaded in CI."""

from pathlib import Path


def test_downloaded_catalogs_include_ephemeris_columns():
    """CI snapshots must contain the fields used by target-info lookups."""
    script = (
        Path(__file__).resolve().parent.parent / "scripts" / "download_catalogs.sh"
    ).read_text()
    query_text = script.replace(r'\"', '"')

    assert 'pl_tranmid AS "Epoch (BJD)"' in query_text
    assert 'pl_tranmiderr1 AS "Epoch (BJD) err"' in query_text
    assert "pl_tranmid, pl_tranmiderr1, pl_tranmiderr2" in query_text
    assert "pl_orbper, pl_orbpererr1, pl_orbpererr2" in query_text
    assert "pl_trandur, pl_trandurerr1, pl_trandurerr2" in query_text
