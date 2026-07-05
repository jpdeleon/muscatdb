"""Tests for the nginx htpasswd management helpers in muscat_db.cli.

Covers two fixes:
- the htpasswd file holds every user's password hash and must not be
  world-readable on a shared server (permission tightening).
- password hashing must not pass the plaintext password as an argv element,
  which would be visible to other local users via `ps`/`/proc` (stdin fix).
"""
from __future__ import annotations

import grp
import os
import stat

from typer.testing import CliRunner

from muscat_db import cli


def test_write_htpasswd_is_not_world_readable(tmp_path, monkeypatch):
    ht_path = tmp_path / "htpasswd-muscatdb"
    monkeypatch.setenv("MUSCAT_HTPASSWD_FILE", str(ht_path))

    cli._write_htpasswd({"alice": "hash1", "bob": "hash2"})

    mode = stat.S_IMODE(ht_path.stat().st_mode)
    assert mode == 0o640
    assert cli._read_htpasswd() == {"alice": "hash1", "bob": "hash2"}


def test_write_htpasswd_chowns_to_configured_group(tmp_path, monkeypatch):
    ht_path = tmp_path / "htpasswd-muscatdb"
    monkeypatch.setenv("MUSCAT_HTPASSWD_FILE", str(ht_path))
    own_group = grp.getgrgid(os.getgid()).gr_name
    monkeypatch.setenv("MUSCAT_NGINX_GROUP", own_group)

    cli._write_htpasswd({"alice": "hash1"})

    assert ht_path.stat().st_gid == os.getgid()


def test_write_htpasswd_tolerates_unknown_group(tmp_path, monkeypatch):
    """Best-effort: an unknown MUSCAT_NGINX_GROUP must not raise or block the write."""
    ht_path = tmp_path / "htpasswd-muscatdb"
    monkeypatch.setenv("MUSCAT_HTPASSWD_FILE", str(ht_path))
    monkeypatch.setenv("MUSCAT_NGINX_GROUP", "no-such-group-xyz")

    cli._write_htpasswd({"alice": "hash1"})

    assert ht_path.is_file()


def test_nginx_group_defaults_to_www_data(monkeypatch):
    monkeypatch.delenv("MUSCAT_NGINX_GROUP", raising=False)
    assert cli._nginx_group() == "www-data"


def test_nginx_group_is_overridable(monkeypatch):
    monkeypatch.setenv("MUSCAT_NGINX_GROUP", "nginx")
    assert cli._nginx_group() == "nginx"


def test_openssl_apr1_hashes_via_stdin_not_argv():
    hashed = cli._openssl_apr1("hunter2")
    assert hashed.startswith("$apr1$")
    assert hashed.count("$") == 3


def test_htpasswd_add_accepts_password_via_stdin(tmp_path, monkeypatch):
    monkeypatch.setenv("MUSCAT_HTPASSWD_FILE", str(tmp_path / "htpasswd-muscatdb"))
    monkeypatch.setenv("MUSCAT_DB_PATH", str(tmp_path / "test.db"))

    result = CliRunner().invoke(
        cli.app, ["htpasswd", "add", "alice", "--password-stdin"], input="s3cret\n"
    )

    assert result.exit_code == 0, result.output
    entries = cli._read_htpasswd()
    assert entries.get("alice", "").startswith("$apr1$")


def test_htpasswd_add_rejects_password_and_password_stdin_together(tmp_path, monkeypatch):
    monkeypatch.setenv("MUSCAT_HTPASSWD_FILE", str(tmp_path / "htpasswd-muscatdb"))

    result = CliRunner().invoke(
        cli.app,
        ["htpasswd", "add", "alice", "--password", "x", "--password-stdin"],
        input="y\n",
    )

    assert result.exit_code != 0
    assert "mutually exclusive" in result.output
    assert cli._read_htpasswd() == {}
