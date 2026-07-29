from __future__ import annotations

import asyncio
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
    r = toggle_chat_reaction(m["id"], "alice", "👍")  # toggle off
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
        chat,
        "_known_cache",
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


def test_connect_refuses_anonymous_when_auth_required(monkeypatch):
    """socket.io must fail closed like the HTTP middleware.

    /socket.io is served by an ASGI wrapper mounted in front of the app, so it
    never passes through the auth middleware. Without this gate a peer reaching
    the loopback port directly is handed the whole chat history and can post.
    """
    import asyncio

    monkeypatch.setenv("MUSCAT_REQUIRE_AUTH", "1")
    monkeypatch.delenv("MUSCAT_PROXY_SECRET", raising=False)
    monkeypatch.setenv("MUSCAT_PROXY_SECRET_FILE", "/nonexistent-secret")

    def must_not_read_history():
        raise AssertionError("chat history must not be loaded for a refused connection")

    monkeypatch.setattr(chat.db, "get_recent_chat_messages", must_not_read_history)
    env = {"asgi.scope": {"client": ("127.0.0.1", 5)}}  # no forwarded user
    with pytest.raises(ConnectionRefusedError):
        asyncio.run(chat.connect("sid-anon", env))


def test_connect_allows_anonymous_when_auth_not_required(monkeypatch):
    """The default single-user/dev deployment keeps working unauthenticated."""
    import asyncio

    monkeypatch.delenv("MUSCAT_REQUIRE_AUTH", raising=False)
    monkeypatch.delenv("MUSCAT_PROXY_SECRET", raising=False)
    monkeypatch.setenv("MUSCAT_PROXY_SECRET_FILE", "/nonexistent-secret")
    monkeypatch.setattr(chat.db, "get_recent_chat_messages", lambda: [])

    emitted = []
    monkeypatch.setattr(chat.sio, "save_session", _async_noop)
    monkeypatch.setattr(chat.sio, "enter_room", _async_noop)
    monkeypatch.setattr(chat.sio, "emit", lambda *a, **k: _record_async(emitted, *a, **k))
    env = {"asgi.scope": {"client": ("127.0.0.1", 5)}}
    asyncio.run(chat.connect("sid-open", env))
    assert any(event == "history" for event, *_ in emitted)
    chat._users_by_sid.pop("sid-open", None)


async def _async_noop(*args, **kwargs):
    return None


def _record_async(sink, event, *args, **kwargs):
    sink.append((event, args, kwargs))

    async def _done():
        return None

    return _done()


# --------------------------------------------------------------------------
# Job-finished system message hook
# --------------------------------------------------------------------------
def test_on_job_finished_persists_system_message(chat_db, monkeypatch):
    monkeypatch.setattr(chat, "_LOOP", None)  # no event loop -> skip emit, just persist
    chat.on_job_finished(
        job_key="photometry:muscat3/2026-07-18/TOI-1234",
        type_="photometry",
        target="TOI-1234",
        inst="muscat3",
        date="2026-07-18",
        state="done",
    )
    msgs = get_recent_chat_messages(days=7)
    assert len(msgs) == 1
    assert msgs[0]["kind"] == "system"
    assert "TOI-1234" in msgs[0]["text"]
    assert "finished" in msgs[0]["text"]
    assert "/target?name=TOI-1234" in msgs[0]["text"]
    assert "localhost" not in msgs[0]["text"]


def test_set_event_loop_and_valid_loop():
    loop = asyncio.new_event_loop()
    try:
        chat.set_event_loop(loop)
        assert chat._LOOP is loop
        assert chat._valid_loop() is loop
        # Close the loop — _valid_loop should detect and clear the stale ref
        loop.close()
        assert chat._valid_loop() is None
        assert chat._LOOP is None
    finally:
        chat._LOOP = None


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
    chat._agent_ctx_add(
        key, {"role": "user", "content": "alice: q1"}, {"role": "assistant", "content": "a1"}
    )
    turns = chat._agent_ctx_get(key)
    assert turns == [
        {"role": "user", "content": "alice: q1"},
        {"role": "assistant", "content": "a1"},
    ]


def test_agent_ctx_caps_at_history_turns():
    key = chat._ctx_key("alice", "sid1")
    for i in range(20):
        chat._agent_ctx_add(
            key, {"role": "user", "content": f"q{i}"}, {"role": "assistant", "content": f"a{i}"}
        )
    # The deque keeps only the most recent _AGENT_HISTORY_TURNS entries.
    assert len(chat._agent_ctx_get(key)) == chat._AGENT_HISTORY_TURNS


def test_agent_ctx_drops_after_idle_ttl(monkeypatch):
    monkeypatch.setenv("MUSCAT_AGENT_HISTORY_TTL_S", "600")
    key = chat._ctx_key("alice", "sid1")
    chat._agent_ctx_add(
        key, {"role": "user", "content": "q"}, {"role": "assistant", "content": "a"}
    )
    # Age the last-activity stamp past the TTL; the next read resets it.
    _, turns = chat._agent_ctx[key]
    chat._agent_ctx[key] = (time.time() - 1000, turns)
    assert chat._agent_ctx_get(key) == []
    assert key not in chat._agent_ctx  # auto-cleared


def test_agent_ctx_reset_clears_only_that_conversation():
    a = chat._ctx_key("alice", "s1")
    b = chat._ctx_key("bob", "s2")
    for k in (a, b):
        chat._agent_ctx_add(
            k, {"role": "user", "content": "q"}, {"role": "assistant", "content": "a"}
        )
    chat._agent_ctx_clear(a)
    assert chat._agent_ctx_get(a) == []
    assert chat._agent_ctx_get(b) != []  # bob's private thread is untouched


def test_private_target_routes_to_user_room_or_sid():
    # Authenticated → the user's room (reaches every tab + the pop-out window).
    assert chat._private_target("alice", "sidX") == {"room": "user:alice"}
    # Anonymous → just this one connection.
    assert chat._private_target(None, "sidX") == {"to": "sidX"}


def test_private_agent_payload_is_unsaved_and_flagged():
    p = chat._private_agent_payload("hello")
    assert p["id"] is None  # no edit/delete/react controls client-side
    assert p["private"] is True  # drives the 'only you' badge
    assert p["kind"] == "agent"


@pytest.mark.parametrize(
    "text, matches",
    [
        ("/me observing TOI-1234", True),
        ("  /ME waves", True),
        ("/media query", False),  # word boundary: not the /me command
        ("hi /me", False),  # only at the start of the message
    ],
)
def test_me_command_prefix(text, matches):
    assert bool(chat._ME_PREFIX_RE.match(text)) is matches


@pytest.mark.parametrize(
    "text, matches",
    [
        ("heads up @everyone", True),
        ("@all please check", True),
        ("posting to @channel", True),
        ("email me at a@everyone.com", False),  # not preceded by word/@ char
        ("ping @allen", False),  # word boundary: @allen is a name
        ("no ping here", False),
    ],
)
def test_broadcast_mention_detection(text, matches):
    assert bool(chat._BROADCAST_MENTION_RE.search(text)) is matches


def test_online_usernames_excludes_anonymous(monkeypatch):
    monkeypatch.setattr(
        chat, "_users_by_sid", {"s1": "alice", "s2": "alice", "s3": None, "s4": "bob"}
    )
    assert chat._online_usernames() == {"alice", "bob"}  # de-duped, no anonymous


# --------------------------------------------------------------------------
# socket.io handlers
#
# The tests above cover the database layer beneath these handlers. The handlers
# themselves are what receive untrusted client input, so they need their own
# coverage: they are where malformed payloads are rejected, where identity is
# resolved, and where author-only edit/delete is enforced before the DB is
# reached at all.
# --------------------------------------------------------------------------
class _FakeSio:
    """Records emits so a handler can be driven without a socket.io server."""

    def __init__(self):
        self.emits = []

    async def emit(self, event, payload=None, **kwargs):
        self.emits.append((event, payload, kwargs))

    def events(self):
        return [event for event, _payload, _kw in self.emits]


@pytest.fixture
def sio_harness(monkeypatch):
    fake = _FakeSio()
    monkeypatch.setattr(chat, "sio", fake)
    monkeypatch.setattr(chat, "_rate", {})
    monkeypatch.setattr(chat, "_users_by_sid", {})
    return fake


def _as_user(monkeypatch, name):
    async def _session_user(_sid):
        return name

    monkeypatch.setattr(chat, "_session_user", _session_user)


@pytest.mark.parametrize("payload", [None, "text", 42, ["a"], {"text": 123}, {"text": "   "}])
def test_message_ignores_malformed_payloads(sio_harness, monkeypatch, payload):
    """Malformed input must be dropped silently, never persisted."""
    import asyncio

    _as_user(monkeypatch, "alice")
    monkeypatch.setattr(
        chat.db,
        "save_chat_message",
        lambda *a, **k: pytest.fail("malformed payload must not be persisted"),
    )
    asyncio.run(chat.message("sid-1", payload))
    assert sio_harness.emits == []


def test_message_rate_limit_drops_the_burst(sio_harness, monkeypatch):
    import asyncio

    _as_user(monkeypatch, "alice")
    saved = []
    monkeypatch.setattr(
        chat.db,
        "save_chat_message",
        lambda user, text, **k: (
            saved.append(text) or {"id": len(saved), "user": user, "text": text}
        ),
    )
    for i in range(chat._RATE_MAX + 5):
        asyncio.run(chat.message("sid-1", {"text": f"m{i}"}))
    assert len(saved) == chat._RATE_MAX, "rate limit did not cap the burst"


def test_test_prefixed_message_is_ephemeral_and_never_persisted(sio_harness, monkeypatch):
    """@test broadcasts live but must never reach the database."""
    import asyncio

    _as_user(monkeypatch, "alice")
    monkeypatch.setattr(
        chat.db,
        "save_chat_message",
        lambda *a, **k: pytest.fail("an @test message must not be persisted"),
    )
    asyncio.run(chat.message("sid-1", {"text": "@test hello"}))

    assert sio_harness.events() == ["message"]
    _event, payload, _kw = sio_harness.emits[0]
    assert payload["ephemeral"] is True
    assert payload["id"] is None
    assert payload["text"] == "hello"


def test_message_notifies_each_mentioned_user(sio_harness, monkeypatch):
    import asyncio

    _as_user(monkeypatch, "alice")
    monkeypatch.setattr(chat, "_known_users_map", lambda: {"bob": "bob", "carol": "carol"})
    monkeypatch.setattr(
        chat.db,
        "save_chat_message",
        lambda user, text, **k: {"id": 1, "user": user, "text": text},
    )
    asyncio.run(chat.message("sid-1", {"text": "@bob @carol look at this"}))

    rooms = [kw.get("room") for event, _p, kw in sio_harness.emits if event == "mention"]
    assert sorted(rooms) == ["user:bob", "user:carol"]


def test_edit_and_delete_require_an_identity(sio_harness, monkeypatch):
    """Anonymous connections must not reach the database at all."""
    import asyncio

    _as_user(monkeypatch, None)
    monkeypatch.setattr(
        chat.db,
        "edit_chat_message",
        lambda *a, **k: pytest.fail("anonymous edit must not reach the database"),
    )
    monkeypatch.setattr(
        chat.db,
        "delete_chat_message",
        lambda *a, **k: pytest.fail("anonymous delete must not reach the database"),
    )
    asyncio.run(chat.edit_message("sid-1", {"id": 1, "text": "hi"}))
    asyncio.run(chat.delete_message("sid-1", {"id": 1}))
    assert sio_harness.emits == []


def test_edit_rejected_for_another_users_message(sio_harness, monkeypatch):
    """The DB returns None when the editor is not the author; the handler must
    report that back rather than broadcasting an edit."""
    import asyncio

    _as_user(monkeypatch, "mallory")
    monkeypatch.setattr(chat.db, "edit_chat_message", lambda *a, **k: None)
    asyncio.run(chat.edit_message("sid-1", {"id": 7, "text": "changed"}))

    assert sio_harness.events() == ["chat_error"]
    assert "not allowed" in sio_harness.emits[0][1]["error"]


def test_delete_rejected_for_another_users_message(sio_harness, monkeypatch):
    import asyncio

    _as_user(monkeypatch, "mallory")
    monkeypatch.setattr(chat.db, "delete_chat_message", lambda *a, **k: False)
    asyncio.run(chat.delete_message("sid-1", {"id": 7}))

    assert sio_harness.events() == ["chat_error"]


def test_successful_edit_and_delete_broadcast(sio_harness, monkeypatch):
    import asyncio

    _as_user(monkeypatch, "alice")
    monkeypatch.setattr(chat.db, "edit_chat_message", lambda mid, u, t: {"id": mid, "text": t})
    monkeypatch.setattr(chat.db, "delete_chat_message", lambda mid, u: True)
    asyncio.run(chat.edit_message("sid-1", {"id": 3, "text": "fixed"}))
    asyncio.run(chat.delete_message("sid-1", {"id": 3}))

    assert sio_harness.events() == ["message_edited", "message_deleted"]


def test_reaction_requires_identity_and_valid_emoji(sio_harness, monkeypatch):
    import asyncio

    monkeypatch.setattr(
        chat.db,
        "toggle_chat_reaction",
        lambda *a, **k: pytest.fail("must not reach the database"),
    )
    _as_user(monkeypatch, None)
    asyncio.run(chat.toggle_reaction("sid-1", {"id": 1, "emoji": "👍"}))
    _as_user(monkeypatch, "alice")
    asyncio.run(chat.toggle_reaction("sid-1", {"id": 1, "emoji": "   "}))
    asyncio.run(chat.toggle_reaction("sid-1", {"id": "notanint", "emoji": "👍"}))
    assert sio_harness.emits == []
