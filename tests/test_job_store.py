"""Tests for the job-store persistence seam (architecture audit C2).

DatabaseJobStore is exercised against a real temp SQLite DB; the seam's swap
point (set_job_store/get_job_store) and Protocol conformance are checked too.
"""

import time

import pytest

from muscat_db import job_store
from muscat_db.job_store import (
    DatabaseJobStore,
    JobQueue,
    JobRepository,
    get_job_store,
    set_job_store,
)


@pytest.fixture
def store(monkeypatch, tmp_path):
    monkeypatch.setenv("MUSCAT_DB_PATH", str(tmp_path / "muscat.db"))
    return DatabaseJobStore()


def _save(store, *, target, state, started_at, type_="photometry", run_id="", **kw):
    store.save(
        type_=type_, inst="muscat4", date="260101", target=target,
        state=state, returncode=None, elapsed=0, started_at=started_at,
        run_id=run_id, **kw,
    )


class TestDatabaseJobStore:
    def test_save_then_all_and_get(self, store):
        _save(store, target="HIP1", state="running", started_at=100.0)
        rows = store.all()
        assert len(rows) == 1
        assert rows[0]["state"] == "running"
        assert rows[0]["inst"] == "muscat4"  # database aliases instrument->inst

        got = store.get("photometry:muscat4/260101/HIP1")
        assert got is not None and got["target"] == "HIP1"
        assert store.get("photometry:muscat4/260101/NOPE") is None

    def test_save_upserts_by_key(self, store):
        _save(store, target="HIP1", state="running", started_at=100.0)
        _save(store, target="HIP1", state="done", started_at=100.0)
        rows = store.all()
        assert len(rows) == 1  # same key -> one row
        assert rows[0]["state"] == "done"

    def test_all_is_newest_first(self, store):
        _save(store, target="OLD", state="done", started_at=100.0)
        _save(store, target="NEW", state="done", started_at=200.0)
        assert [r["target"] for r in store.all()] == ["NEW", "OLD"]

    def test_delete_removes_only_that_key(self, store):
        _save(store, target="A", state="done", started_at=100.0)
        _save(store, target="B", state="done", started_at=101.0)
        store.delete("photometry:muscat4/260101/A")
        assert {r["target"] for r in store.all()} == {"B"}

    def test_delete_missing_key_is_noop(self, store):
        _save(store, target="A", state="done", started_at=100.0)
        store.delete("photometry:muscat4/260101/GONE")  # must not raise
        assert len(store.all()) == 1

    def test_enqueue_records_pending(self, store):
        store.enqueue(
            type_="photometry", inst="muscat4", date="260101", target="HIP1",
            started_at=time.time(), run_type="full",
        )
        rows = store.all()
        assert rows[0]["state"] == "pending"

    def test_pending_is_fifo_and_type_filtered(self, store):
        _save(store, target="P2", state="pending", started_at=200.0)
        _save(store, target="P1", state="pending", started_at=100.0)
        _save(store, target="R", state="running", started_at=150.0)
        _save(store, type_="transit_fit", target="OTHER", state="pending", started_at=50.0)
        pend = store.pending("photometry")
        assert [r["target"] for r in pend] == ["P1", "P2"]  # oldest-first, photometry only


class TestSeamSwap:
    def test_get_returns_installed_store(self):
        original = get_job_store()
        try:
            sentinel = object()
            set_job_store(sentinel)
            assert get_job_store() is sentinel
        finally:
            set_job_store(original)

    def test_database_store_satisfies_protocols(self):
        s = DatabaseJobStore()
        assert isinstance(s, JobRepository)
        assert isinstance(s, JobQueue)

    def test_default_store_is_database_backed(self):
        assert isinstance(job_store.get_job_store(), DatabaseJobStore)
