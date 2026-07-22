from __future__ import annotations

import time

import pytest

from muscat_db import chat
from muscat_db.database import (
    save_chat_message,
    get_recent_chat_messages,
    edit_chat_message,
    delete_chat_message,
    toggle_chat_reaction,
    get_known_chat_usernames,
)


@pytest.fixture
def chat_db(monkeypatch, tmp_path):
    """Fresh SQLite DB for chat persistence tests."""
    path = str(tmp_path / "chat.db")
    monkeypatch.setenv("MUSCAT_DB_PATH", path)
    # Force per-path schema ensure to run against this fresh file.
    import muscat_db.database as db
    db._chat_migrated_paths.discard(path)
    return path


# --------------------------------------------------------------------------
# Persistence (database layer)
# --------------------------------------------------------------------------
def test_save_and_get_recent(chat_db):
    a = save_chat_message("alice", "hello", mentions=["bob"])
    b = save_chat_message("bob", "hi @alice", mentions=["alice"])
    assert a["id"] < b["id"]
    msgs = get_recent_chat_messages(days=7)
    assert [m["user"] for m in msgs] == ["alice", "bob"]  # oldest first
    assert msgs[0]["mentions"] == ["bob"]
    assert msgs[0]["reactions"] == []
    assert msgs[0]["edited"] is False


def test_backfill_window_excludes_old_messages(chat_db):
    save_chat_message("alice", "ancient", created_at=time.time() - 10 * 86400)
    save_chat_message("alice", "recent", created_at=time.time() - 1 * 86400)
    texts = [m["text"] for m in get_recent_chat_messages(days=7)]
    assert texts == ["recent"]


def test_edit_is_author_only(chat_db):
    m = save_chat_message("alice", "typo", kind="user")
    assert edit_chat_message(m["id"], "mallory", "hacked") is None
    updated = edit_chat_message(m["id"], "alice", "fixed")
    assert updated["text"] == "fixed"
    assert updated["edited"] is True


def test_delete_is_author_only_and_hard(chat_db):
    m = save_chat_message("alice", "bye", kind="user")
    assert delete_chat_message(m["id"], "mallory") is False
    assert delete_chat_message(m["id"], "alice") is True
    assert get_recent_chat_messages(days=7) == []


def test_delete_removes_reactions(chat_db):
    m = save_chat_message("alice", "react me", kind="user")
    toggle_chat_reaction(m["id"], "bob", "👍")
    assert delete_chat_message(m["id"], "alice") is True
    # A new message reusing patterns should not inherit stale reactions.
    save_chat_message("alice", "again", kind="user")
    got = get_recent_chat_messages(days=7)
    assert got[0]["reactions"] == []


def test_toggle_reaction_add_and_remove(chat_db):
    m = save_chat_message("alice", "hi", kind="user")
    r = toggle_chat_reaction(m["id"], "alice", "👍")
    assert r["reactions"][0]["count"] == 1
    r = toggle_chat_reaction(m["id"], "bob", "👍")
    assert r["reactions"][0]["count"] == 2
    r = toggle_chat_reaction(m["id"], "alice", "👍")   # toggle off
    assert r["reactions"][0]["count"] == 1
    assert r["reactions"][0]["users"] == ["bob"]


def test_toggle_reaction_missing_message(chat_db):
    assert toggle_chat_reaction(99999, "alice", "👍") is None


def test_known_usernames_includes_chat_authors(chat_db):
    save_chat_message("zoe", "hi", kind="user")
    save_chat_message("amy", "yo", kind="user")
    assert get_known_chat_usernames() == ["amy", "zoe"]  # sorted, case-insensitive


def test_system_messages_are_not_editable_or_deletable(chat_db):
    m = save_chat_message("system", "job done", kind="system")
    assert edit_chat_message(m["id"], "system", "x") is None
    assert delete_chat_message(m["id"], "system") is False


# --------------------------------------------------------------------------
# Command / mention parsing (chat helpers)
# --------------------------------------------------------------------------
def test_test_prefix_matches_ephemeral():
    assert chat._TEST_PREFIX_RE.match("@test hello there")
    assert chat._TEST_PREFIX_RE.match("@TEST case-insensitive")
    assert not chat._TEST_PREFIX_RE.match("testing without at")
    assert not chat._TEST_PREFIX_RE.match("hello @test in middle")


def test_parse_mentions_resolves_known_and_skips_reserved(monkeypatch):
    monkeypatch.setattr(
        chat, "_known_cache",
        {"ts": time.time() + 9999, "map": {"alice": "alice", "bob": "Bob"}},
    )
    found = chat._parse_mentions("hey @alice and @Bob, ignore @here @test @nobody")
    assert found == ["alice", "Bob"]  # canonical casing, reserved/unknown dropped


def test_rate_limit_blocks_after_burst():
    sid = "rate-sid"
    chat._rate.pop(sid, None)
    allowed = sum(1 for _ in range(chat._RATE_MAX + 5) if chat._rate_ok(sid))
    assert allowed == chat._RATE_MAX
    chat._rate.pop(sid, None)


# --------------------------------------------------------------------------
# Identity extraction from the websocket handshake
# --------------------------------------------------------------------------
def test_extract_identity_trusts_loopback_forwarded_user(monkeypatch):
    monkeypatch.delenv("MUSCAT_PROXY_SECRET", raising=False)
    monkeypatch.setenv("MUSCAT_PROXY_SECRET_FILE", "/nonexistent-secret")
    env = {"HTTP_X_FORWARDED_USER": "alice", "asgi.scope": {"client": ("127.0.0.1", 5)}}
    assert chat._extract_identity(env) == "alice"


def test_extract_identity_rejects_non_loopback(monkeypatch):
    monkeypatch.delenv("MUSCAT_PROXY_SECRET", raising=False)
    monkeypatch.setenv("MUSCAT_PROXY_SECRET_FILE", "/nonexistent-secret")
    env = {"HTTP_X_FORWARDED_USER": "mallory", "asgi.scope": {"client": ("10.0.0.9", 5)}}
    assert chat._extract_identity(env) is None


def test_extract_identity_anonymous_when_no_header(monkeypatch):
    monkeypatch.delenv("MUSCAT_PROXY_SECRET", raising=False)
    monkeypatch.setenv("MUSCAT_PROXY_SECRET_FILE", "/nonexistent-secret")
    env = {"asgi.scope": {"client": ("127.0.0.1", 5)}}
    assert chat._extract_identity(env) is None


# --------------------------------------------------------------------------
# Job-finished system message hook
# --------------------------------------------------------------------------
def test_on_job_finished_persists_system_message(chat_db, monkeypatch):
    monkeypatch.setattr(chat, "_LOOP", None)  # no event loop -> skip emit, just persist
    chat.on_job_finished(
        job_key="photometry:muscat3/2026-07-18/TOI-1234",
        type_="photometry", target="TOI-1234", inst="muscat3",
        date="2026-07-18", state="done",
    )
    msgs = get_recent_chat_messages(days=7)
    assert len(msgs) == 1
    assert msgs[0]["kind"] == "system"
    assert "TOI-1234" in msgs[0]["text"]
    assert "finished" in msgs[0]["text"]


# --------------------------------------------------------------------------
# @bot: private, per-user, in-memory conversation context
# --------------------------------------------------------------------------
@pytest.fixture(autouse=True)
def _clear_agent_ctx():
    """The @bot context lives in a module global; isolate each test."""
    chat._agent_ctx.clear()
    yield
    chat._agent_ctx.clear()


def test_agent_ctx_roundtrips_user_and_assistant_turns():
    key = chat._ctx_key("alice", "sid1")
    assert chat._agent_ctx_get(key) == []
    chat._agent_ctx_add(key,
                        {"role": "user", "content": "alice: q1"},
                        {"role": "assistant", "content": "a1"})
    turns = chat._agent_ctx_get(key)
    assert turns == [
        {"role": "user", "content": "alice: q1"},
        {"role": "assistant", "content": "a1"},
    ]


def test_agent_ctx_caps_at_history_turns():
    key = chat._ctx_key("alice", "sid1")
    for i in range(20):
        chat._agent_ctx_add(key,
                            {"role": "user", "content": f"q{i}"},
                            {"role": "assistant", "content": f"a{i}"})
    # The deque keeps only the most recent _AGENT_HISTORY_TURNS entries.
    assert len(chat._agent_ctx_get(key)) == chat._AGENT_HISTORY_TURNS


def test_agent_ctx_drops_after_idle_ttl(monkeypatch):
    monkeypatch.setenv("MUSCAT_AGENT_HISTORY_TTL_S", "600")
    key = chat._ctx_key("alice", "sid1")
    chat._agent_ctx_add(key,
                        {"role": "user", "content": "q"},
                        {"role": "assistant", "content": "a"})
    # Age the last-activity stamp past the TTL; the next read resets it.
    _, turns = chat._agent_ctx[key]
    chat._agent_ctx[key] = (time.time() - 1000, turns)
    assert chat._agent_ctx_get(key) == []
    assert key not in chat._agent_ctx   # auto-cleared


def test_agent_ctx_reset_clears_only_that_conversation():
    a = chat._ctx_key("alice", "s1")
    b = chat._ctx_key("bob", "s2")
    for k in (a, b):
        chat._agent_ctx_add(k, {"role": "user", "content": "q"}, {"role": "assistant", "content": "a"})
    chat._agent_ctx_clear(a)
    assert chat._agent_ctx_get(a) == []
    assert chat._agent_ctx_get(b) != []   # bob's private thread is untouched


def test_private_target_routes_to_user_room_or_sid():
    # Authenticated → the user's room (reaches every tab + the pop-out window).
    assert chat._private_target("alice", "sidX") == {"room": "user:alice"}
    # Anonymous → just this one connection.
    assert chat._private_target(None, "sidX") == {"to": "sidX"}


def test_private_agent_payload_is_unsaved_and_flagged():
    p = chat._private_agent_payload("hello")
    assert p["id"] is None            # no edit/delete/react controls client-side
    assert p["private"] is True       # drives the 'only you' badge
    assert p["kind"] == "agent"


@pytest.mark.parametrize("text, matches", [
    ("/me observing TOI-1234", True),
    ("  /ME waves", True),
    ("/media query", False),          # word boundary: not the /me command
    ("hi /me", False),                # only at the start of the message
])
def test_me_command_prefix(text, matches):
    assert bool(chat._ME_PREFIX_RE.match(text)) is matches


@pytest.mark.parametrize("text, matches", [
    ("heads up @everyone", True),
    ("@all please check", True),
    ("posting to @channel", True),
    ("email me at a@everyone.com", False),   # not preceded by word/@ char
    ("ping @allen", False),                  # word boundary: @allen is a name
    ("no ping here", False),
])
def test_broadcast_mention_detection(text, matches):
    assert bool(chat._BROADCAST_MENTION_RE.search(text)) is matches


def test_online_usernames_excludes_anonymous(monkeypatch):
    monkeypatch.setattr(chat, "_users_by_sid",
                        {"s1": "alice", "s2": "alice", "s3": None, "s4": "bob"})
    assert chat._online_usernames() == {"alice", "bob"}   # de-duped, no anonymous
