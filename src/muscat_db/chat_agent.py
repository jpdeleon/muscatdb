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

When a question is about a specific pipeline stage, the matching external repo's
own docs (README + CLAUDE.md) are folded in as *extra read-only grounding* — the
agent gains no tools or file access, we just widen the static context the same
way ``CLAUDE.md`` is injected:

- photometry  -> prose2   (``MUSCAT_PROSE_PROJECT``,   default ``../ext_tools/prose2``)
- transit fit -> timer    (``MUSCAT_TIMER_PROJECT``,   default ``../ext_tools/timer``)
- TTV fit     -> harmonic (``MUSCAT_HARMONIC_PROJECT``, default ``../ext_tools/harmonic``)

The name is read once at import (it must line up with the frontend + reserved
mentions); URL/model/timeout and the repo paths are read per call so an ``.env``
change or a test override takes effect without re-importing.
"""
from __future__ import annotations

import logging
import os
import re
from dataclasses import dataclass
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
# ollama defaults num_ctx to ~4096 regardless of the model's real window; the
# system prompt + repo grounding + history can exceed that and get silently
# truncated, so we request an explicit, larger window (gemma4 supports 128k).
_DEFAULT_NUM_CTX = 8192

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


def num_ctx() -> int:
    try:
        return max(512, int(os.environ.get("MUSCAT_OLLAMA_NUM_CTX", _DEFAULT_NUM_CTX)))
    except (TypeError, ValueError):
        return _DEFAULT_NUM_CTX


# ----- mention handling ----------------------------------------------------
def addressed(text: str) -> bool:
    """True if ``text`` contains a standalone ``@<agent>`` token."""
    return bool(text) and _MENTION_RE.search(text) is not None


def extract_question(text: str) -> str:
    """Strip the (first) ``@<agent>`` token and normalise whitespace, leaving the
    actual question."""
    stripped = _MENTION_RE.sub(" ", text or "", count=1)
    return re.sub(r"\s+", " ", stripped).strip()[:_MAX_QUESTION_LEN]


# A short allow-list of exact reset commands (matched against the whole extracted
# question, so "how do I clear the cache?" does NOT trip it).
_RESET_COMMANDS = frozenset({"/reset", "/clear", "reset", "clear"})


def is_reset_command(question: str) -> bool:
    """True when ``question`` is *just* a context-reset command (e.g. ``@bot /reset``)."""
    return (question or "").strip().lower() in _RESET_COMMANDS


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


# ----- external repo grounding (topic-gated) -------------------------------
# The pipeline's heavy lifting lives in three sibling repos under ``ext_tools/``.
# When a question is about one of those stages we fold that repo's own docs into
# the model's context so it can answer with real API detail. This is grounding
# only: no tools, no query-time file access, and the doc set is a fixed
# allow-list (README.md + CLAUDE.md), never a path derived from user input.
_MAX_REPO_DOC_CHARS = 6000
_REPO_DOC_FILES = ("README.md", "CLAUDE.md")


@dataclass(frozen=True)
class _RepoTopic:
    """A question topic and the external repo whose docs ground it."""

    name: str
    env_var: str        # override for the repo path (mirrors config.ENV_VARS)
    subdir: str         # default location: <project>/ext_tools/<subdir>
    label: str          # human heading shown to the model
    keywords: tuple[str, ...]  # lower-cased substrings that select this topic

    def directory(self) -> Path:
        default = _repo_root().parent / "ext_tools" / self.subdir
        return Path(os.environ.get(self.env_var) or str(default))


# Order is stable/deterministic; a question may match more than one topic (e.g.
# a transit-fit + TTV question pulls in both timer and harmonic).
_REPO_TOPICS: tuple[_RepoTopic, ...] = (
    _RepoTopic(
        name="photometry",
        env_var="MUSCAT_PROSE_PROJECT",
        subdir="prose2",
        label="prose2 — photometry pipeline",
        keywords=(
            "photometr", "aperture", "prose", "comparison star", "differential",
            "flat field", "flatfield", "bias frame", "dark frame", "centroid",
            "fwhm", "flux extraction", "detrend", "calibrat",
            # Japanese
            "測光", "アパーチャー", "比較星", "相対測光", "較正", "キャリブレーション",
        ),
    ),
    _RepoTopic(
        name="transit",
        env_var="MUSCAT_TIMER_PROJECT",
        subdir="timer",
        label="timer — transit light-curve fitting",
        keywords=(
            "transit fit", "transit model", "transit depth", "transit light curve",
            "limb darken", "mid-transit", "timer", "rp/rs", "a/rs",
            "impact parameter", "light curve fit", "lightcurve fit",
            # Japanese (kept specific so a TTV question doesn't also match here)
            "トランジットフィット", "トランジット解析", "周縁減光", "トランジット深",
            "ライトカーブフィット",
        ),
    ),
    _RepoTopic(
        name="ttv",
        env_var="MUSCAT_HARMONIC_PROJECT",
        subdir="harmonic",
        label="harmonic — transit timing variation (TTV) fitting",
        keywords=(
            "ttv", "transit timing", "timing variation", "o-c", "o minus c",
            "harmonic", "period variation", "mid-time", "mid-times",
            # Japanese
            "トランジット時刻", "タイミング変動", "周期変動", "通過時刻",
        ),
    ),
)


def detect_topics(question: str) -> list[str]:
    """Names of the repo topics whose keywords appear in ``question``."""
    q = (question or "").lower()
    return [t.name for t in _REPO_TOPICS if any(k in q for k in t.keywords)]


@lru_cache(maxsize=8)
def _read_repo_docs(directory: str, label: str) -> str:
    """Read a repo's allow-listed docs into one bounded, headed block.

    Cached per resolved path (edits need a restart, like the templates). Missing
    repo/files yield ``""`` so an absent ext_tools checkout simply adds no
    grounding instead of erroring the whole reply.
    """
    base = Path(directory)
    chunks: list[str] = []
    for fname in _REPO_DOC_FILES:
        try:
            text = (base / fname).read_text(encoding="utf-8", errors="replace").strip()
        except OSError:
            continue  # file absent/unreadable — skip, not fatal
        if text:
            chunks.append(f"### {fname}\n{text}")
    if not chunks:
        logger.debug("chat agent: no docs for %s at %s", label, directory)
        return ""
    doc = "\n\n".join(chunks)
    if len(doc) > _MAX_REPO_DOC_CHARS:
        doc = doc[:_MAX_REPO_DOC_CHARS] + "\n…(truncated)…"
    return f"## {label}\n{doc}"


def _repo_grounding(question: str) -> str:
    """Extra grounding block for the repos this question touches (or ``""``)."""
    by_name = {t.name: t for t in _REPO_TOPICS}
    parts = []
    for name in detect_topics(question):
        topic = by_name[name]
        doc = _read_repo_docs(str(topic.directory()), topic.label)
        if doc:
            parts.append(doc)
    if not parts:
        return ""
    header = (
        "The question relates to the external pipeline component(s) below. Use "
        "their docs as additional grounding — they are read-only reference "
        "material and the same capabilities/limits above still apply.\n\n"
    )
    return header + "\n\n".join(parts)


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
    grounding = _repo_grounding(question)
    if grounding:
        messages.append({"role": "system", "content": grounding})
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
        "options": {"temperature": 0.2, "num_ctx": num_ctx()},
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
