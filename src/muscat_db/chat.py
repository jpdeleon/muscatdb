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
import os
import re
import time
from collections import deque

from . import chat_agent
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
# @-tokens that are commands or noise, never real usernames to notify. The
# codebase assistant's name is reserved too: @bot triggers the agent (below) and
# must never be resolved as a person to nudge.
_RESERVED_MENTIONS = {"here", "test", "everyone", "channel", "all", chat_agent.AGENT_NAME}
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
    # Hand the question to the codebase assistant when it is addressed (@bot).
    if chat_agent.addressed(text):
        _spawn_agent(sid, chat_agent.extract_question(text), msg.get("id"))


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


# ----- codebase assistant (@bot) -------------------------------------------
# Recent turns handed to the model for follow-up context.
_AGENT_HISTORY_TURNS = 8
# Auto-clear: history older than this (seconds) is dropped, so an idle gap resets
# @bot's context on its own. 0/negative disables the time cutoff.
_DEFAULT_HISTORY_TTL_S = 900.0
# Persisted system divider posted by "@bot /reset"; _agent_history treats the
# most recent one as a hard cutoff so the next question starts fresh.
_RESET_MARKER = "🧹 @bot conversation context cleared"
# Re-emit the "typing" indicator this often while the model works, so it does
# not hit the client's ~4s auto-clear during a long (multi-second) generation.
_AGENT_TYPING_HEARTBEAT_S = 2.0
# Keep a strong reference to in-flight agent tasks so they are not garbage
# collected before completion (asyncio only holds a weak reference).
_agent_tasks: set[asyncio.Task] = set()


def _spawn_agent(sid: str, question: str, exclude_id: int | None) -> None:
    """Answer the question in the background so the message handler returns at
    once (the model call can take many seconds)."""
    if _LOOP is None:  # no running loop captured yet; nothing to schedule onto
        return
    task = asyncio.create_task(_run_agent(sid, question, exclude_id))
    _agent_tasks.add(task)
    task.add_done_callback(_agent_tasks.discard)


def _ephemeral_agent_payload(text: str) -> dict:
    """A live-only agent message (never persisted): busy notes, errors, intro."""
    return {
        "id": None, "user": chat_agent.DISPLAY_NAME, "text": text,
        "kind": "agent", "ephemeral": True, "ts": time.time(), "sid": None,
        "mentions": [], "reactions": [], "edited": False,
    }


def _history_ttl_s() -> float:
    try:
        return float(os.environ.get("MUSCAT_AGENT_HISTORY_TTL_S", _DEFAULT_HISTORY_TTL_S))
    except (TypeError, ValueError):
        return _DEFAULT_HISTORY_TTL_S


def _agent_history(exclude_id: int | None) -> list[dict]:
    """Recent chat mapped to chat-model roles: the agent's own replies become
    ``assistant`` turns, everyone else becomes ``user`` turns prefixed with the
    speaker's name. Skips system rows and the just-posted question.

    Context is auto-cleared two ways: turns older than ``MUSCAT_AGENT_HISTORY_TTL_S``
    are dropped (idle-gap reset), and everything up to the most recent ``@bot``
    reset marker is discarded (explicit ``@bot /reset``)."""
    try:
        recent = db.get_recent_chat_messages()
    except Exception:
        logger.debug("agent history load failed", exc_info=True)
        return []

    # Idle-gap auto-clear: keep only turns newer than the TTL.
    ttl = _history_ttl_s()
    if ttl > 0:
        cutoff_ts = time.time() - ttl
        recent = [m for m in recent if (m.get("ts") or 0) >= cutoff_ts]

    # Explicit clear: discard everything up to and including the last reset marker.
    last_reset = max(
        (i for i, m in enumerate(recent)
         if m.get("kind") == "system" and (m.get("text") or "") == _RESET_MARKER),
        default=-1,
    )
    if last_reset >= 0:
        recent = recent[last_reset + 1:]

    turns: list[dict] = []
    for m in recent:
        if m.get("id") == exclude_id:
            continue
        kind = m.get("kind")
        body = (m.get("text") or "").strip()
        if not body or kind == "system":
            continue
        author = m.get("user") or "user"
        if kind == "agent" and author.lower() == chat_agent.AGENT_NAME:
            turns.append({"role": "assistant", "content": body})
        else:
            turns.append({"role": "user", "content": f"{author}: {body}"})
    return turns[-_AGENT_HISTORY_TURNS:]


async def _reset_agent_context() -> None:
    """Persist + broadcast the reset divider so _agent_history cuts off here and
    the next @bot question starts with a clean context (shared across the room)."""
    try:
        msg = db.save_chat_message("system", _RESET_MARKER, kind="system")
    except Exception:
        logger.exception("failed to persist @bot reset marker")
        await sio.emit(
            "message",
            _ephemeral_agent_payload("Couldn't clear my context just now — please try again."),
        )
        return
    await sio.emit("message", {**msg, "sid": None})


async def _typing_heartbeat() -> None:
    """Keep the agent's typing indicator alive until cancelled."""
    try:
        while True:
            await sio.emit("typing", {"user": chat_agent.DISPLAY_NAME, "typing": True})
            await asyncio.sleep(_AGENT_TYPING_HEARTBEAT_S)
    except asyncio.CancelledError:
        pass


async def _run_agent(sid: str, question: str, exclude_id: int | None) -> None:
    if not question:  # bare "@bot": show a short intro, no model call
        intro = (
            f"Hi! I'm @{chat_agent.AGENT_NAME}, the muscat-db codebase assistant. "
            f"Ask me things like \"@{chat_agent.AGENT_NAME} how does the photometry "
            "job lifecycle work?\""
        )
        await sio.emit("message", _ephemeral_agent_payload(intro), to=sid)
        return
    if chat_agent.is_reset_command(question):  # "@bot /reset": clear context, no model call
        await _reset_agent_context()
        return
    if not chat_agent.reserve():  # model already at capacity
        await sio.emit(
            "message",
            _ephemeral_agent_payload(
                "I'm still answering an earlier question — give me a moment and ask again."
            ),
            to=sid,
        )
        return
    heartbeat = asyncio.create_task(_typing_heartbeat())
    try:
        reply = await chat_agent.answer(question, _agent_history(exclude_id))
        try:
            msg = db.save_chat_message(chat_agent.DISPLAY_NAME, reply, kind="agent")
        except Exception:
            logger.exception("failed to persist agent reply")
            await sio.emit("message", _ephemeral_agent_payload(reply))
        else:
            await sio.emit("message", {**msg, "sid": None})
    except Exception as e:
        logger.warning("chat agent request failed: %s", e, exc_info=True)
        await sio.emit(
            "message",
            _ephemeral_agent_payload(
                "⚠️ Sorry, I couldn't reach the model right now. Please try again shortly."
            ),
            to=sid,
        )
    finally:
        heartbeat.cancel()
        chat_agent.release()
        await sio.emit("typing", {"user": chat_agent.DISPLAY_NAME, "typing": False})


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
