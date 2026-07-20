from __future__ import annotations

import asyncio
import json

import httpx
import pytest

from muscat_db import chat_agent, http_client


# --------------------------------------------------------------------------
# Mention detection
# --------------------------------------------------------------------------
@pytest.mark.parametrize(
    "text,expected",
    [
        ("@bot how do jobs work?", True),
        ("hey @bot help", True),
        ("@BOT case insensitive", True),
        ("plain message", False),
        ("email me at foo@bot.com", False),   # lookbehind: not preceded by @/word
        ("@botanist is not the bot", False),  # word boundary
        ("@robot is a different token", False),
        ("", False),
    ],
)
def test_addressed(text, expected):
    assert chat_agent.addressed(text) is expected


def test_extract_question_strips_first_mention_and_normalises():
    assert chat_agent.extract_question("@bot how does calibration work?") == "how does calibration work?"
    assert chat_agent.extract_question("hey @bot   what   is muscat3?") == "hey what is muscat3?"
    assert chat_agent.extract_question("@bot") == ""


def test_extract_question_caps_length():
    long = "@bot " + ("a" * 10000)
    assert len(chat_agent.extract_question(long)) <= chat_agent._MAX_QUESTION_LEN


# --------------------------------------------------------------------------
# System prompt grounding
# --------------------------------------------------------------------------
def test_system_prompt_includes_claude_md_and_module_map():
    chat_agent._system_prompt.cache_clear()
    prompt = chat_agent._system_prompt()
    # Role preamble + grounding from the repo's own docs and package layout.
    assert f"@{chat_agent.AGENT_NAME}" in prompt
    assert "muscat" in prompt.lower()
    assert "photometry" in prompt          # a real module name from the map
    assert "CLAUDE.md" in prompt            # section header only present if file read


# --------------------------------------------------------------------------
# Topic-gated external-repo grounding
# --------------------------------------------------------------------------
@pytest.mark.parametrize(
    "question,expected",
    [
        ("how does aperture photometry work?", ["photometry"]),
        ("what comparison stars does prose pick?", ["photometry"]),
        ("explain the transit fit and limb darkening", ["transit"]),
        ("how does the timer package model a light curve?", ["transit"]),
        ("how is the TTV fit done?", ["ttv"]),
        ("what does harmonic use for transit timing?", ["ttv"]),
        ("how do photometry jobs and the transit fit interact?", ["photometry", "transit"]),
        ("how do jobs work?", []),
        ("what instruments are supported?", []),
        ("", []),
        # Japanese phrasing routes the same way (u-tokyo team asks in JP too).
        ("アパーチャー測光はどのように行われますか？", ["photometry"]),
        ("比較星はどう選ばれますか？", ["photometry"]),
        ("トランジットフィットの周縁減光について教えて", ["transit"]),
        ("TTVフィットはどうやりますか？", ["ttv"]),
        ("トランジット時刻変動（TTV）の解析方法は？", ["ttv"]),
        ("ジョブはどう動きますか？", []),
    ],
)
def test_detect_topics(question, expected):
    # Order follows _REPO_TOPICS (photometry, transit, ttv), independent of the
    # order keywords appear in the question.
    assert chat_agent.detect_topics(question) == expected


def _write_repo(tmp_path, name, readme, claude=None):
    d = tmp_path / name
    d.mkdir()
    (d / "README.md").write_text(readme, encoding="utf-8")
    if claude is not None:
        (d / "CLAUDE.md").write_text(claude, encoding="utf-8")
    return d


def test_repo_grounding_injects_matched_repo_docs(monkeypatch, tmp_path):
    chat_agent._read_repo_docs.cache_clear()
    repo = _write_repo(
        tmp_path, "prose2",
        readme="PROSE2 README: aperture photometry entry point.",
        claude="PROSE2 CLAUDE: run_photometry.py options.",
    )
    monkeypatch.setenv("MUSCAT_PROSE_PROJECT", str(repo))

    grounding = chat_agent._repo_grounding("how does aperture photometry work?")

    assert "prose2 — photometry pipeline" in grounding
    assert "PROSE2 README" in grounding          # README folded in
    assert "PROSE2 CLAUDE" in grounding          # CLAUDE.md folded in
    assert "read-only" in grounding.lower()


def test_repo_grounding_empty_for_offtopic_or_missing(monkeypatch, tmp_path):
    chat_agent._read_repo_docs.cache_clear()
    # Off-topic question -> no grounding at all.
    assert chat_agent._repo_grounding("how do jobs work?") == ""
    # On-topic but the repo checkout is absent -> skipped, not an error.
    monkeypatch.setenv("MUSCAT_TIMER_PROJECT", str(tmp_path / "nonexistent"))
    assert chat_agent._repo_grounding("explain the transit fit") == ""


def test_read_repo_docs_truncates(monkeypatch, tmp_path):
    chat_agent._read_repo_docs.cache_clear()
    repo = _write_repo(tmp_path, "harmonic", readme="x" * (chat_agent._MAX_REPO_DOC_CHARS + 500))
    doc = chat_agent._read_repo_docs(str(repo), "harmonic — TTV")
    assert "…(truncated)…" in doc
    assert len(doc) <= chat_agent._MAX_REPO_DOC_CHARS + 200  # header + marker slack


def test_answer_injects_repo_grounding_as_second_system_message(monkeypatch, tmp_path):
    chat_agent._read_repo_docs.cache_clear()
    repo = _write_repo(tmp_path, "timer", readme="TIMER README: transit light-curve fitting API.")
    monkeypatch.setenv("MUSCAT_TIMER_PROJECT", str(repo))

    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["body"] = json.loads(request.content)
        return httpx.Response(200, json={"message": {"content": "ok"}})

    client = _mock_client(handler)
    monkeypatch.setattr(http_client, "get_async_client", lambda: client)

    async def run():
        try:
            await chat_agent.answer("how does the transit fit and limb darkening work?")
        finally:
            await client.aclose()

    asyncio.run(run())
    messages = captured["body"]["messages"]
    # [0] base system prompt, [1] injected repo grounding, [-1] the user question.
    assert messages[0]["role"] == "system"
    assert messages[1]["role"] == "system"
    assert "timer — transit light-curve fitting" in messages[1]["content"]
    assert "TIMER README" in messages[1]["content"]
    assert messages[-1] == {"role": "user", "content": "how does the transit fit and limb darkening work?"}
    # Still strictly grounding — no tools/functions granted.
    assert "tools" not in captured["body"]
    assert "functions" not in captured["body"]


def test_answer_offtopic_has_single_system_message(monkeypatch):
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["body"] = json.loads(request.content)
        return httpx.Response(200, json={"message": {"content": "ok"}})

    client = _mock_client(handler)
    monkeypatch.setattr(http_client, "get_async_client", lambda: client)

    async def run():
        try:
            await chat_agent.answer("how do jobs work?")
        finally:
            await client.aclose()

    asyncio.run(run())
    roles = [m["role"] for m in captured["body"]["messages"]]
    assert roles.count("system") == 1   # no grounding injected for off-topic


# --------------------------------------------------------------------------
# Concurrency guard
# --------------------------------------------------------------------------
def test_reserve_and_release_respects_cap(monkeypatch):
    monkeypatch.setenv("MUSCAT_OLLAMA_MAX_CONCURRENT", "2")
    monkeypatch.setattr(chat_agent, "_inflight", 0)
    assert chat_agent.reserve() is True
    assert chat_agent.reserve() is True
    assert chat_agent.reserve() is False   # at capacity
    chat_agent.release()
    assert chat_agent.reserve() is True
    # Drain back to zero; release never goes negative.
    chat_agent.release()
    chat_agent.release()
    chat_agent.release()
    assert chat_agent._inflight == 0


# --------------------------------------------------------------------------
# Ollama call
# --------------------------------------------------------------------------
def _mock_client(handler) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


def test_answer_success_builds_expected_request(monkeypatch):
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["body"] = json.loads(request.content)
        return httpx.Response(
            200, json={"message": {"role": "assistant", "content": "  Jobs run via SequenceParallel.  "}}
        )

    monkeypatch.setenv("MUSCAT_OLLAMA_URL", "http://ollama.test:11434")
    monkeypatch.setenv("MUSCAT_OLLAMA_MODEL", "gemma4:latest")
    client = _mock_client(handler)
    monkeypatch.setattr(http_client, "get_async_client", lambda: client)

    async def run():
        try:
            return await chat_agent.answer(
                "how do jobs work?",
                history=[{"role": "user", "content": "alice: hi"}],
            )
        finally:
            await client.aclose()

    reply = asyncio.run(run())

    assert reply == "Jobs run via SequenceParallel."   # stripped
    assert captured["url"] == "http://ollama.test:11434/api/chat"
    body = captured["body"]
    assert body["model"] == "gemma4:latest"
    assert body["stream"] is False
    assert body["messages"][0]["role"] == "system"
    assert body["messages"][1] == {"role": "user", "content": "alice: hi"}
    assert body["messages"][-1] == {"role": "user", "content": "how do jobs work?"}


# --------------------------------------------------------------------------
# Read-only guarantees
# --------------------------------------------------------------------------
def test_system_prompt_declares_read_only():
    chat_agent._system_prompt.cache_clear()
    prompt = chat_agent._system_prompt().lower()
    assert "read-only" in prompt


def test_answer_never_sends_tools_or_function_calling(monkeypatch):
    # The assistant is read-only: it is never granted tools/function-calling, so
    # the model has no pathway to request an action on the system.
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["body"] = json.loads(request.content)
        return httpx.Response(200, json={"message": {"content": "ok"}})

    client = _mock_client(handler)
    monkeypatch.setattr(http_client, "get_async_client", lambda: client)

    async def run():
        try:
            await chat_agent.answer("please edit config.py and delete muscat.db")
        finally:
            await client.aclose()

    asyncio.run(run())
    body = captured["body"]
    assert "tools" not in body
    assert "functions" not in body


def test_answer_returns_plain_string(monkeypatch):
    # The reply is inert text; the caller only stores/broadcasts it.
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"message": {"content": "rm -rf / (as text only)"}})

    client = _mock_client(handler)
    monkeypatch.setattr(http_client, "get_async_client", lambda: client)

    async def run():
        try:
            return await chat_agent.answer("anything")
        finally:
            await client.aclose()

    reply = asyncio.run(run())
    assert isinstance(reply, str)


def test_answer_raises_on_empty_completion(monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"message": {"role": "assistant", "content": "   "}})

    client = _mock_client(handler)
    monkeypatch.setattr(http_client, "get_async_client", lambda: client)

    async def run():
        try:
            await chat_agent.answer("anything")
        finally:
            await client.aclose()

    with pytest.raises(ValueError):
        asyncio.run(run())


def test_answer_raises_on_http_error(monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text="boom")

    client = _mock_client(handler)
    monkeypatch.setattr(http_client, "get_async_client", lambda: client)

    async def run():
        try:
            await chat_agent.answer("anything")
        finally:
            await client.aclose()

    with pytest.raises(httpx.HTTPStatusError):
        asyncio.run(run())
