"""``@bot`` codebase assistant: routes chat questions to a Gemma model on ollama.

When a chat message addresses the agent (``@bot <question>``), :mod:`chat` hands
the question here. We build a system prompt from the project's ``CLAUDE.md`` plus
the package's module map, prepend a short window of recent conversation for
follow-up context, and POST a *non-streaming* request to the ollama ``/api/chat``
endpoint (by default the box on ``muscat-ut4``). The reply is posted back into
chat as an ``agent`` message.

Everything is configured through environment variables (mirrored in ``config.py``
and ``.env.example``):

- ``MUSCAT_CHAT_AGENT_NAME``      the ``@name`` that invokes it (default ``bot``)
- ``MUSCAT_OLLAMA_URL``           base URL of the ollama server
                                  (default ``http://muscat-ut4.c.u-tokyo.ac.jp:11434``)
- ``MUSCAT_OLLAMA_MODEL``         model tag to run (default ``gemma4:latest``)
- ``MUSCAT_OLLAMA_TIMEOUT_S``     per-request generation timeout (default ``120``)
- ``MUSCAT_OLLAMA_MAX_CONCURRENT``in-flight requests before callers get "busy"
                                  (default ``2``)

The name is read once at import (it must line up with the frontend + reserved
mentions); URL/model/timeout are read per call so an ``.env`` change or a test
override takes effect without re-importing.
"""
from __future__ import annotations

import logging
import os
import re
from functools import lru_cache
from pathlib import Path

from . import http_client

logger = logging.getLogger(__name__)

# ----- identity ------------------------------------------------------------
# The @name is lower-cased and validated to the same character class the chat
# mention parser accepts, falling back to "bot" if someone sets garbage.
_NAME_RE = re.compile(r"[A-Za-z0-9._-]{1,32}")


def _resolve_name() -> str:
    raw = (os.environ.get("MUSCAT_CHAT_AGENT_NAME") or "bot").strip().lower()
    return raw if _NAME_RE.fullmatch(raw) else "bot"


AGENT_NAME = _resolve_name()
DISPLAY_NAME = AGENT_NAME  # author name stored/shown for the agent's messages

# A standalone @<name> token: same lookbehind guard as the chat mention regex,
# so "email@bot.com", "@robot" and "@botanist" do not trigger the agent.
_MENTION_RE = re.compile(rf"(?<![\w@])@{re.escape(AGENT_NAME)}\b", re.IGNORECASE)

# ----- limits --------------------------------------------------------------
_MAX_QUESTION_LEN = 4000
_MAX_CLAUDEMD_CHARS = 8000
_DEFAULT_URL = "http://muscat-ut4.c.u-tokyo.ac.jp:11434"
_DEFAULT_MODEL = "gemma4:latest"
_DEFAULT_TIMEOUT_S = 120.0
_DEFAULT_MAX_CONCURRENT = 2

# In-flight request accounting. Single-worker deployment on one event loop, so a
# plain counter is race-free without locking (see chat.py's presence dicts).
_inflight = 0


# ----- config getters ------------------------------------------------------
def ollama_url() -> str:
    return (os.environ.get("MUSCAT_OLLAMA_URL") or _DEFAULT_URL).rstrip("/")


def model_name() -> str:
    return os.environ.get("MUSCAT_OLLAMA_MODEL") or _DEFAULT_MODEL


def timeout_s() -> float:
    try:
        return float(os.environ.get("MUSCAT_OLLAMA_TIMEOUT_S", _DEFAULT_TIMEOUT_S))
    except (TypeError, ValueError):
        return _DEFAULT_TIMEOUT_S


def max_concurrent() -> int:
    try:
        return max(1, int(os.environ.get("MUSCAT_OLLAMA_MAX_CONCURRENT", _DEFAULT_MAX_CONCURRENT)))
    except (TypeError, ValueError):
        return _DEFAULT_MAX_CONCURRENT


# ----- mention handling ----------------------------------------------------
def addressed(text: str) -> bool:
    """True if ``text`` contains a standalone ``@<agent>`` token."""
    return bool(text) and _MENTION_RE.search(text) is not None


def extract_question(text: str) -> str:
    """Strip the (first) ``@<agent>`` token and normalise whitespace, leaving the
    actual question."""
    stripped = _MENTION_RE.sub(" ", text or "", count=1)
    return re.sub(r"\s+", " ", stripped).strip()[:_MAX_QUESTION_LEN]


# ----- concurrency guard ---------------------------------------------------
def reserve() -> bool:
    """Claim an in-flight slot. Returns False (without claiming) when the model
    is already at capacity, so the caller can tell the user to try again."""
    global _inflight
    if _inflight >= max_concurrent():
        return False
    _inflight += 1
    return True


def release() -> None:
    global _inflight
    _inflight = max(0, _inflight - 1)


# ----- system prompt -------------------------------------------------------
def _repo_root() -> Path:
    # src/muscat_db/chat_agent.py -> parents[2] == repo root (holds CLAUDE.md).
    return Path(__file__).resolve().parents[2]


def _read_claude_md() -> str:
    try:
        text = (_repo_root() / "CLAUDE.md").read_text(encoding="utf-8", errors="replace").strip()
    except OSError:
        logger.debug("chat agent: CLAUDE.md not readable", exc_info=True)
        return ""
    if len(text) > _MAX_CLAUDEMD_CHARS:
        text = text[:_MAX_CLAUDEMD_CHARS] + "\n…(truncated)…"
    return text


def _module_map() -> str:
    pkg = Path(__file__).resolve().parent
    names = sorted(p.stem for p in pkg.glob("*.py") if not p.stem.startswith("_"))
    return ", ".join(names)


@lru_cache(maxsize=1)
def _system_prompt() -> str:
    """Static grounding context (cached; edits to CLAUDE.md need a restart, like
    the Jinja templates)."""
    parts = [
        f"You are @{AGENT_NAME}, a concise, friendly assistant embedded in the "
        "team chat of the muscat-db web application. muscat-db manages MuSCAT "
        "multi-band photometry observation logs and drives a photometry and "
        "transit-fitting pipeline over data from five instruments (muscat, "
        "muscat2, muscat3, muscat4, sinistro).\n\n"
        "Answer questions about this codebase: its data model, workflows, "
        "configuration, and how the pieces fit together.\n\n"
        "Capabilities and limits (READ-ONLY):\n"
        "- You are a strictly read-only advisor. You can read and explain this "
        "codebase, but you CANNOT modify, create, or delete any file, run any "
        "command, change the database, or take any action in the system. You "
        "have no tools and no file access.\n"
        "- Your reply is shown as plain chat text and nothing more. It is never "
        "executed or applied.\n"
        "- If asked to make a change (edit a file, run a job, fix a bug), do not "
        "claim to have done it. Explain what a human would change and where, and "
        "let them apply it.\n\n"
        "Guidelines:\n"
        "- Ground answers in the project notes and module map below.\n"
        "- If the information is not here or you are unsure, say so plainly "
        "instead of inventing details.\n"
        "- Keep replies short and readable in a small chat window; use brief "
        "code spans or bullet lists when they help.\n"
    ]
    notes = _read_claude_md()
    if notes:
        parts.append("\n## Project notes (CLAUDE.md)\n" + notes + "\n")
    modules = _module_map()
    if modules:
        parts.append("\n## Python modules (src/muscat_db/)\n" + modules + "\n")
    return "".join(parts)


# ----- ollama call ---------------------------------------------------------
async def answer(question: str, history: list[dict] | None = None) -> str:
    """Ask the model ``question`` (with optional prior turns) and return its reply.

    Raises on transport/HTTP errors or an empty completion; the caller turns that
    into a user-facing "couldn't reach the model" note.
    """
    question = (question or "").strip()[:_MAX_QUESTION_LEN]
    if not question:
        raise ValueError("empty question")

    messages: list[dict] = [{"role": "system", "content": _system_prompt()}]
    if history:
        messages.extend(history)
    messages.append({"role": "user", "content": question})

    # READ-ONLY by construction: we never send a ``tools``/``functions`` field,
    # so the model cannot request tool/function calls, and we treat the reply as
    # inert text (returned as a string; the caller only stores/broadcasts it, and
    # never executes it or uses it as a path). Keep this payload tool-free.
    payload = {
        "model": model_name(),
        "messages": messages,
        "stream": False,
        "options": {"temperature": 0.2},
    }

    client = http_client.get_async_client()
    resp = await client.post(f"{ollama_url()}/api/chat", json=payload, timeout=timeout_s())
    resp.raise_for_status()
    data = resp.json()
    content = ((data or {}).get("message") or {}).get("content") or ""
    content = content.strip()
    if not content:
        raise ValueError("empty response from model")
    return content
