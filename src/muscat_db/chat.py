"""Real-time team chat: presence, @-mentions, edit/delete, reactions, and
job-finished system messages.

Message *history* is persisted (see database.save_chat_message and the
chat_messages table), but rendering/escaping happens client-side (DOM text
nodes), so text is stored and relayed verbatim. Ephemeral ``@test`` messages
are broadcast live and never written to the database.

Identity is the nginx-authenticated user, extracted from the websocket
handshake headers (which nginx now forwards for /socket.io) and validated with
the same trust check as HTTP requests. It is the sole basis for message
attribution and for edit/delete/react authorization — the client cannot forge
who it is.
"""
from __future__ import annotations

import asyncio
import logging
import re
import time
from collections import deque

from . import jobs
from .auth import PROXY_SECRET_HEADER, trusted_forwarded_user
from . import database as db
from .web import sio

logger = logging.getLogger(__name__)

# ----- limits & tokens -----------------------------------------------------
_MAX_TEXT_LEN = 2000
_MAX_EMOJI_LEN = 16
_RATE_MAX = 12           # messages ...
_RATE_WINDOW_S = 10.0    # ... per this many seconds, per connection
# @-tokens that are commands or noise, never real usernames to notify.
_RESERVED_MENTIONS = {"here", "test", "everyone", "channel", "all"}
_MENTION_RE = re.compile(r"(?<![\w@])@([A-Za-z0-9._-]{1,64})")
_TEST_PREFIX_RE = re.compile(r"^\s*@test\b\s*", re.IGNORECASE)

# ----- live (in-memory) connection state -----------------------------------
# Single-worker deployment: these live in one process. A multi-worker/multi-host
# rollout would move presence + fan-out to the socket.io Redis manager.
_users_by_sid: dict[str, str | None] = {}   # sid -> username (None = anonymous)
_rate: dict[str, deque] = {}
_LOOP: asyncio.AbstractEventLoop | None = None
_known_cache: dict = {"ts": 0.0, "map": {}}


def set_event_loop(loop: asyncio.AbstractEventLoop) -> None:
    """Capture the server event loop so background threads (job finish hooks)
    can schedule broadcasts onto it."""
    global _LOOP
    _LOOP = loop


# ----- helpers -------------------------------------------------------------
def _extract_identity(environ: dict) -> str | None:
    """Return the trusted nginx user for a websocket handshake, or None.

    python-socketio's ASGI layer exposes handshake headers as WSGI-style
    ``HTTP_*`` keys and the peer address via the ASGI scope / ``REMOTE_ADDR``.
    We reuse the exact HTTP trust check (loopback peer + shared secret).
    """
    forwarded = environ.get("HTTP_X_FORWARDED_USER")
    secret_key = "HTTP_" + PROXY_SECRET_HEADER.upper().replace("-", "_")
    presented_secret = environ.get(secret_key)
    client_host = None
    scope = environ.get("asgi.scope")
    if isinstance(scope, dict) and scope.get("client"):
        client_host = scope["client"][0]
    if client_host is None:
        client_host = environ.get("REMOTE_ADDR")
    return trusted_forwarded_user(forwarded, client_host, presented_secret)


def _display(user: str | None) -> str:
    return user or "Anonymous"


def _online_users() -> list[str]:
    return sorted({_display(u) for u in _users_by_sid.values()}, key=str.lower)


async def _broadcast_presence() -> None:
    users = _online_users()
    await sio.emit("presence", {"count": len(users), "users": users})


def _known_users_map() -> dict[str, str]:
    """Lower-cased -> canonical username, cached briefly to avoid a DB hit per
    message."""
    now = time.time()
    if now - _known_cache["ts"] > 30:
        try:
            names = db.get_known_chat_usernames()
        except Exception:  # never let mention resolution break messaging
            logger.debug("get_known_chat_usernames failed", exc_info=True)
            names = []
        _known_cache["map"] = {n.lower(): n for n in names}
        _known_cache["ts"] = now
    return _known_cache["map"]


def _parse_mentions(text: str) -> list[str]:
    """Canonical usernames @-mentioned in text (reserved tokens excluded)."""
    known = _known_users_map()
    found: list[str] = []
    for raw in _MENTION_RE.findall(text):
        low = raw.lower()
        if low in _RESERVED_MENTIONS:
            continue
        canon = known.get(low)
        if canon and canon not in found:
            found.append(canon)
    return found


def _rate_ok(sid: str) -> bool:
    dq = _rate.setdefault(sid, deque())
    now = time.time()
    while dq and now - dq[0] > _RATE_WINDOW_S:
        dq.popleft()
    if len(dq) >= _RATE_MAX:
        return False
    dq.append(now)
    return True


async def _session_user(sid: str) -> str | None:
    try:
        session = await sio.get_session(sid)
        return (session or {}).get("user")
    except KeyError:
        return None


# ----- connection lifecycle ------------------------------------------------
@sio.event
async def connect(sid, environ):
    global _LOOP
    if _LOOP is None:
        _LOOP = asyncio.get_running_loop()
    user = _extract_identity(environ)
    await sio.save_session(sid, {"user": user})
    _users_by_sid[sid] = user
    if user:
        await sio.enter_room(sid, f"user:{user}")
    # Backfill recent history to this client only (never persisted here).
    try:
        history = db.get_recent_chat_messages()
    except Exception:
        logger.exception("failed to load chat history")
        history = []
    await sio.emit("history", {"messages": history}, to=sid)
    await _broadcast_presence()


@sio.event
async def disconnect(sid):
    _users_by_sid.pop(sid, None)
    _rate.pop(sid, None)
    await _broadcast_presence()


# ----- messaging -----------------------------------------------------------
@sio.on("message")
async def message(sid, data):
    if not isinstance(data, dict) or not _rate_ok(sid):
        return
    text = data.get("text")
    if not isinstance(text, str):
        return
    text = text.strip()
    if not text:
        return
    text = text[:_MAX_TEXT_LEN]

    user = await _session_user(sid)
    display = _display(user)

    # @test → ephemeral: broadcast live, never persisted, not editable.
    if _TEST_PREFIX_RE.match(text):
        body = _TEST_PREFIX_RE.sub("", text, count=1) or "(test message)"
        await sio.emit("message", {
            "id": None, "user": display, "text": body, "kind": "ephemeral",
            "ephemeral": True, "ts": time.time(), "sid": sid,
            "mentions": [], "reactions": [], "edited": False,
        })
        return

    mentions = _parse_mentions(text)
    try:
        msg = db.save_chat_message(display, text, mentions=mentions, kind="user")
    except Exception:
        logger.exception("failed to persist chat message")
        await sio.emit("chat_error", {"error": "message could not be saved"}, to=sid)
        return

    payload = {**msg, "sid": sid}
    await sio.emit("message", payload)
    # Nudge each mentioned user (on any page/tab) so they notice.
    for name in mentions:
        await sio.emit("mention", {"from": display, "message": payload}, room=f"user:{name}")


@sio.on("edit_message")
async def edit_message(sid, data):
    if not isinstance(data, dict):
        return
    user = await _session_user(sid)
    if not user:
        return
    msg_id = data.get("id")
    text = data.get("text")
    if not isinstance(msg_id, int) or not isinstance(text, str):
        return
    text = text.strip()[:_MAX_TEXT_LEN]
    if not text:
        return
    updated = db.edit_chat_message(msg_id, user, text)
    if updated is None:
        await sio.emit("chat_error", {"error": "not allowed to edit that message"}, to=sid)
        return
    await sio.emit("message_edited", updated)


@sio.on("delete_message")
async def delete_message(sid, data):
    if not isinstance(data, dict):
        return
    user = await _session_user(sid)
    if not user:
        return
    msg_id = data.get("id")
    if not isinstance(msg_id, int):
        return
    if db.delete_chat_message(msg_id, user):
        await sio.emit("message_deleted", {"id": msg_id})
    else:
        await sio.emit("chat_error", {"error": "not allowed to delete that message"}, to=sid)


@sio.on("toggle_reaction")
async def toggle_reaction(sid, data):
    if not isinstance(data, dict):
        return
    user = await _session_user(sid)
    if not user:  # reactions require an identity
        return
    msg_id = data.get("id")
    emoji = data.get("emoji")
    if not isinstance(msg_id, int) or not isinstance(emoji, str):
        return
    emoji = emoji.strip()[:_MAX_EMOJI_LEN]
    if not emoji:
        return
    result = db.toggle_chat_reaction(msg_id, user, emoji)
    if result is not None:
        await sio.emit("reaction_updated", result)


@sio.on("typing")
async def typing(sid, data):
    user = await _session_user(sid)
    is_typing = bool(isinstance(data, dict) and data.get("typing"))
    # Relay to everyone except the sender; ephemeral, never stored.
    await sio.emit("typing", {"user": _display(user), "typing": is_typing}, skip_sid=sid)


# ----- job-finished system messages ----------------------------------------
def on_job_finished(job_key: str, type_: str = "", target: str = "", inst: str = "",
                    date: str = "", state: str = "", **_) -> None:
    """jobs.fire_job_finished hook (runs in the job-sync thread). Persists a
    system message and schedules its broadcast onto the server event loop."""
    label = {"photometry": "Photometry", "transit_fit": "Transit fit"}.get(type_, type_ or "Job")
    verb = {"done": "finished", "error": "failed", "cancelled": "was cancelled"}.get(state, state)
    where = " ".join(p for p in (inst, date) if p)
    text = f"{label} for {target} ({where}) {verb}".strip()
    try:
        msg = db.save_chat_message("system", text, kind="system")
    except Exception:
        logger.exception("failed to persist job-finished chat message")
        return
    if _LOOP is None:
        return
    asyncio.run_coroutine_threadsafe(sio.emit("message", {**msg, "sid": None}), _LOOP)


# Register once at import so both pipelines' sync loops notify chat.
jobs.register_job_finished_hook(on_job_finished)
