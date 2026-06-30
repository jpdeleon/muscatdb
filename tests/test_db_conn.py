"""Tests for the get_conn() connection abstraction (architecture audit M3).

The previous open-coded `connect(...) ... close()` helpers leaked the handle
whenever the body raised between the two. get_conn() is a contextmanager that
guarantees close on every path and standardizes timeout/row_factory.
"""

import sqlite3

import pytest

from muscat_db.database import get_conn


@pytest.fixture
def dbfile(tmp_path):
    path = str(tmp_path / "t.db")
    with get_conn(path) as conn:
        conn.execute("CREATE TABLE t (k TEXT PRIMARY KEY, v INTEGER)")
        conn.execute("INSERT INTO t VALUES ('a', 1)")
        conn.commit()
    return path


def test_yields_usable_connection_and_commits(dbfile):
    with get_conn(dbfile) as conn:
        (v,) = conn.execute("SELECT v FROM t WHERE k = 'a'").fetchone()
    assert v == 1


def test_closes_connection_on_normal_exit(dbfile):
    with get_conn(dbfile) as conn:
        pass
    # Operating on a closed connection raises ProgrammingError.
    with pytest.raises(sqlite3.ProgrammingError):
        conn.execute("SELECT 1")


def test_closes_connection_when_body_raises(dbfile):
    captured = {}
    with pytest.raises(ValueError):
        with get_conn(dbfile) as conn:
            captured["conn"] = conn
            raise ValueError("boom")
    # Even though the body raised, the connection was closed (no leak).
    with pytest.raises(sqlite3.ProgrammingError):
        captured["conn"].execute("SELECT 1")


def test_row_factory_applied(dbfile):
    with get_conn(dbfile, row_factory=sqlite3.Row) as conn:
        row = conn.execute("SELECT k, v FROM t WHERE k = 'a'").fetchone()
    assert row["k"] == "a"
    assert row["v"] == 1


def test_defaults_to_env_db_path(monkeypatch, tmp_path):
    target = tmp_path / "muscat.db"
    monkeypatch.setenv("MUSCAT_DB_PATH", str(target))
    with get_conn() as conn:  # no explicit path -> db_path() from env
        conn.execute("CREATE TABLE x (a)")
        conn.commit()
    assert target.exists()
