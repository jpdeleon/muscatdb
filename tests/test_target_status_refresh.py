"""Tests for the live per-job Targets-page status refresh (database.refresh_target_status).

After a photometry/transit-fit job reaches a terminal state, the pipelines call
``refresh_target_status(obj)`` to update the target's persisted ``phot_status`` /
``fit_status`` immediately, instead of waiting for the daily ``build_db`` cron.

The critical invariant is that the status is *aggregated across all of a target's
observation dates*: a reduction finishing on one date must never clobber a "full"
status earned on another date.
"""

from __future__ import annotations

import pytest

from muscat_db import database as db


@pytest.fixture
def targets_db(tmp_path, monkeypatch):
    """A minimal targets table with one two-date target, wired to MUSCAT_DB_PATH."""
    dbfile = tmp_path / "muscat.db"
    monkeypatch.setenv("MUSCAT_DB_PATH", str(dbfile))
    with db.get_conn(str(dbfile)) as conn:
        conn.executescript(db.SCHEMA)
        conn.execute(
            """INSERT INTO targets
               (object, n_dates, n_frames, instruments, dates, inst_dates,
                filters, total_exptime, is_identified, phot_status, fit_status)
               VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
            # Two dates: muscat3/240101 and muscat3/240102
            ("TOI-1234", 2, 100, "muscat3", "240101,240102",
             "muscat3:240101,muscat3:240102", "g", 3600.0, 1, "none", "none"),
        )
        conn.commit()
    return dbfile


def _read_status(dbfile, obj):
    with db.get_conn(str(dbfile)) as conn:
        row = conn.execute(
            "SELECT phot_status, fit_status FROM targets WHERE object = ?", (obj,)
        ).fetchone()
    return row


def test_refresh_aggregates_full_across_dates(targets_db, monkeypatch):
    """A full reduction on ONE date makes the whole target 'full', even when the
    other date has nothing. This is the regression guard for the clobber bug."""
    # date 240101 -> full phot, 240102 -> none
    def fake_phot_status(inst, date, obj):
        return "full" if date == "240101" else "none"

    monkeypatch.setattr("muscat_db.photometry.get_photometry_status", fake_phot_status)
    monkeypatch.setattr("muscat_db.transit_fit.has_fit_outputs", lambda *a, **k: False)

    db.refresh_target_status("TOI-1234")

    phot, fit = _read_status(targets_db, "TOI-1234")
    assert phot == "full"   # earned on 240101, not clobbered by empty 240102
    assert fit == "none"


def test_refresh_does_not_clobber_other_date(targets_db, monkeypatch):
    """Simulate a job finishing on the EMPTY date (240102): status must still
    resolve to 'full' because 240101 has a full reduction on disk."""
    # Pre-seed the row as already 'full' (as the daily build would have left it).
    with db.get_conn(str(targets_db)) as conn:
        conn.execute("UPDATE targets SET phot_status='full' WHERE object='TOI-1234'")
        conn.commit()

    # 240101 still full on disk; 240102 (the just-finished job) produced nothing.
    monkeypatch.setattr(
        "muscat_db.photometry.get_photometry_status",
        lambda inst, date, obj: "full" if date == "240101" else "none",
    )
    monkeypatch.setattr("muscat_db.transit_fit.has_fit_outputs", lambda *a, **k: False)

    db.refresh_target_status("TOI-1234")

    phot, _ = _read_status(targets_db, "TOI-1234")
    assert phot == "full"


def test_refresh_sets_test_when_only_test_runs(targets_db, monkeypatch):
    """'test' is the weakest non-empty phot status and only wins when no date is full."""
    monkeypatch.setattr(
        "muscat_db.photometry.get_photometry_status",
        lambda inst, date, obj: "test",
    )
    monkeypatch.setattr("muscat_db.transit_fit.has_fit_outputs", lambda *a, **k: False)

    db.refresh_target_status("TOI-1234")

    phot, fit = _read_status(targets_db, "TOI-1234")
    assert phot == "test"
    assert fit == "none"


def test_refresh_marks_fit_full_when_any_date_has_output(targets_db, monkeypatch):
    monkeypatch.setattr("muscat_db.photometry.get_photometry_status", lambda *a, **k: "none")
    monkeypatch.setattr(
        "muscat_db.transit_fit.has_fit_outputs",
        lambda inst, date, obj: date == "240102",
    )

    db.refresh_target_status("TOI-1234")

    phot, fit = _read_status(targets_db, "TOI-1234")
    assert phot == "none"
    assert fit == "full"


def test_refresh_unknown_target_is_noop(targets_db, monkeypatch):
    """A target with no row (unidentified / built after last run) is a silent no-op."""
    called = False

    def _should_not_run(*a, **k):
        nonlocal called
        called = True
        return "full"

    monkeypatch.setattr("muscat_db.photometry.get_photometry_status", _should_not_run)
    monkeypatch.setattr("muscat_db.transit_fit.has_fit_outputs", lambda *a, **k: True)

    # Must not raise, and must not even attempt status computation.
    db.refresh_target_status("DOES-NOT-EXIST")
    assert called is False
