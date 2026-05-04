"""Tests for src/llm/adapters/ — LLMAdapter Protocol + 4 concrete adapters.

Each adapter is exercised against a fake underlying client to keep the
tests hermetic (no subprocess / no network). The tests verify that
  * AdapterRequest validation rejects malformed inputs.
  * Helpers (messages_to_dicts, flatten_to_prompt) round-trip correctly.
  * Each adapter normalizes its underlying response into AdapterResponse
    with the right provider/model identifiers.
  * Timeout is honored where the adapter supports it.
  * ClaudeCLIAdapter's prompt/history split handles the system-message
    merge and last-user-message edge cases.
  * isinstance() works against the runtime_checkable Protocol.
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any

import pytest

from src.llm.adapters import (
    AdapterRequest,
    AdapterResponse,
    ChatMessage,
    ClaudeCLIAdapter,
    HermesProfileAdapter,
    LLMAdapter,
    OllamaAdapter,
    flatten_to_prompt,
    messages_to_dicts,
)
from src.llm.adapters.claude_cli import _split_for_claude_cli
from src.llm.base import LLMResponse


# ---- ChatMessage / AdapterRequest validation ------------------------------


def test_chat_message_to_dict():
    m = ChatMessage(role="user", content="hi")
    assert m.to_dict() == {"role": "user", "content": "hi"}


def test_adapter_request_rejects_empty_messages():
    with pytest.raises(ValueError, match="messages"):
        AdapterRequest(messages=[])


def test_adapter_request_rejects_invalid_max_tokens():
    with pytest.raises(ValueError, match="max_tokens"):
        AdapterRequest(
            messages=[ChatMessage(role="user", content="hi")],
            max_tokens=0,
        )


def test_adapter_request_rejects_invalid_temperature():
    with pytest.raises(ValueError, match="temperature"):
        AdapterRequest(
            messages=[ChatMessage(role="user", content="hi")],
            temperature=2.5,
        )


def test_adapter_request_defaults_are_safe():
    req = AdapterRequest(messages=[ChatMessage(role="user", content="hi")])
    assert req.max_tokens > 0
    assert 0.0 <= req.temperature <= 2.0
    assert req.timeout_s is None
    assert req.extra == {}


# ---- helpers --------------------------------------------------------------


def test_messages_to_dicts():
    msgs = [
        ChatMessage(role="system", content="be concise"),
        ChatMessage(role="user", content="hi"),
        ChatMessage(role="assistant", content="hello"),
    ]
    assert messages_to_dicts(msgs) == [
        {"role": "system", "content": "be concise"},
        {"role": "user", "content": "hi"},
        {"role": "assistant", "content": "hello"},
    ]


def test_flatten_to_prompt_format():
    msgs = [
        ChatMessage(role="system", content="be concise"),
        ChatMessage(role="user", content="hi"),
        ChatMessage(role="assistant", content="hello"),
        ChatMessage(role="user", content="bye"),
    ]
    flat = flatten_to_prompt(msgs)
    # System content first, no role prefix.
    assert flat.startswith("be concise\n\n")
    # User/assistant turns labeled with uppercase.
    assert "USER: hi" in flat
    assert "ASSISTANT: hello" in flat
    assert flat.endswith("USER: bye")


def test_adapter_response_total_tokens():
    r = AdapterResponse(
        text="ok", provider="ollama", model="x",
        prompt_tokens=12, completion_tokens=8,
    )
    assert r.total_tokens == 20


# ---- Fake clients ---------------------------------------------------------


@dataclass
class _FakeLLMClient:
    """Stand-in for OllamaClient/OpenAIClient. Records last call."""

    model: str = "fake-model"
    name: str = "fake"
    response_text: str = "ok"
    response_tokens: tuple[int, int] = (10, 5)
    delay_s: float = 0.0
    last_messages: list[dict[str, str]] | None = None
    last_max_tokens: int | None = None
    last_temperature: float | None = None

    async def generate(
        self,
        messages: list[dict[str, str]],
        *,
        max_tokens: int = 2048,
        temperature: float = 0.2,
    ) -> LLMResponse:
        self.last_messages = messages
        self.last_max_tokens = max_tokens
        self.last_temperature = temperature
        if self.delay_s:
            await asyncio.sleep(self.delay_s)
        return LLMResponse(
            text=self.response_text,
            model=self.model,
            prompt_tokens=self.response_tokens[0],
            completion_tokens=self.response_tokens[1],
        )


@dataclass
class _FakeClaudeResult:
    text: str = "claude-reply"
    model_name: str = "claude-sonnet-4"
    session_id: str = "sid-1"
    duration_ms: int = 42
    input_tokens: int = 11
    output_tokens: int = 22
    total_cost_usd: float = 0.0
    raw: dict = None

    def __post_init__(self):
        if self.raw is None:
            self.raw = {}


class _FakeClaudeAdapter:
    """Stand-in for ClaudeCodeAdapter."""

    def __init__(self):
        self.calls: list[dict[str, Any]] = []
        self.result = _FakeClaudeResult()

    async def run(
        self,
        *,
        prompt: str,
        history: list[dict[str, str]] | None = None,
        model: str | None = None,
        timeout_ms: int | None = None,
        resume_session_id: str | None = None,
        persist_session: bool = False,
    ) -> _FakeClaudeResult:
        self.calls.append({
            "prompt": prompt,
            "history": list(history or []),
            "model": model,
            "timeout_ms": timeout_ms,
            "resume_session_id": resume_session_id,
            "persist_session": persist_session,
        })
        return self.result


@dataclass
class _FakeHermesResult:
    text: str = "hermes-reply"
    session_id: str = "hsid-1"
    tier_used: str = "L2"
    model_name: str = "gpt-4o-mini"
    provider: str = "openai"
    duration_ms: int = 88
    stdout_raw: str = ""
    stderr_raw: str = ""
    prompt_tokens: int = 30
    completion_tokens: int = 15
    primary_model: str = "gpt-4o-mini"
    turns_used: int = 1
    raw_json: dict = None

    def __post_init__(self):
        if self.raw_json is None:
            self.raw_json = {}


class _FakeHermesAdapter:
    def __init__(self):
        self.calls: list[dict[str, Any]] = []
        self.result = _FakeHermesResult()

    async def run(
        self,
        query: str,
        *,
        model: str | None = None,
        provider: str | None = None,
        resume_session: str | None = None,
        max_turns: int | None = None,
        extra_args: list[str] | None = None,
        timeout_ms: int | None = None,
        profile: str | None = None,
        preload_skills: list[str] | None = None,
    ) -> _FakeHermesResult:
        self.calls.append({
            "query": query,
            "model": model,
            "provider": provider,
            "profile": profile,
            "timeout_ms": timeout_ms,
        })
        return self.result


# ---- OllamaAdapter --------------------------------------------------------


@pytest.mark.asyncio
async def test_ollama_adapter_provider_and_model():
    fake = _FakeLLMClient(model="qwen2.5:14b-instruct")
    a = OllamaAdapter(fake)
    assert a.provider == "ollama"
    assert a.model == "qwen2.5:14b-instruct"


@pytest.mark.asyncio
async def test_ollama_adapter_generate_normalizes_response():
    fake = _FakeLLMClient(
        model="qwen2.5:14b-instruct",
        response_text="hello",
        response_tokens=(20, 5),
    )
    a = OllamaAdapter(fake)
    req = AdapterRequest(
        messages=[ChatMessage(role="user", content="hi")],
        max_tokens=128,
        temperature=0.5,
    )
    resp = await a.generate(req)
    assert resp.text == "hello"
    assert resp.provider == "ollama"
    assert resp.model == "qwen2.5:14b-instruct"
    assert resp.prompt_tokens == 20
    assert resp.completion_tokens == 5
    assert resp.duration_ms >= 0
    # Underlying client received the right kwargs.
    assert fake.last_max_tokens == 128
    assert fake.last_temperature == 0.5
    assert fake.last_messages == [{"role": "user", "content": "hi"}]


@pytest.mark.asyncio
async def test_ollama_adapter_honors_timeout():
    fake = _FakeLLMClient(delay_s=1.0)
    a = OllamaAdapter(fake)
    req = AdapterRequest(
        messages=[ChatMessage(role="user", content="hi")],
        timeout_s=0.05,
    )
    with pytest.raises(asyncio.TimeoutError):
        await a.generate(req)


# ---- ClaudeCLIAdapter -----------------------------------------------------


def test_split_for_claude_cli_simple_user():
    """Single user message → prompt only, empty history, no system."""
    msgs = [ChatMessage(role="user", content="hello")]
    prompt, history = _split_for_claude_cli(msgs)
    assert prompt == "hello"
    assert history == []


def test_split_for_claude_cli_with_system():
    """System messages → merged into prompt prefix with [system] marker."""
    msgs = [
        ChatMessage(role="system", content="be concise"),
        ChatMessage(role="user", content="hi"),
    ]
    prompt, history = _split_for_claude_cli(msgs)
    assert prompt.startswith("[system]")
    assert "be concise" in prompt
    assert "hi" in prompt
    assert history == []


def test_split_for_claude_cli_with_history():
    """Earlier user/assistant turns → history; last → prompt."""
    msgs = [
        ChatMessage(role="user", content="q1"),
        ChatMessage(role="assistant", content="a1"),
        ChatMessage(role="user", content="q2"),
    ]
    prompt, history = _split_for_claude_cli(msgs)
    assert prompt == "q2"
    assert history == [
        {"role": "user", "content": "q1"},
        {"role": "assistant", "content": "a1"},
    ]


def test_split_for_claude_cli_empty_raises():
    with pytest.raises(ValueError):
        _split_for_claude_cli([])


@pytest.mark.asyncio
async def test_claude_cli_adapter_invokes_with_split_prompt():
    fake = _FakeClaudeAdapter()
    a = ClaudeCLIAdapter(fake, model="sonnet")
    assert a.provider == "claude_cli"
    assert a.model == "sonnet"

    req = AdapterRequest(
        messages=[
            ChatMessage(role="system", content="be brief"),
            ChatMessage(role="user", content="explain async"),
        ],
        timeout_s=30.0,
    )
    resp = await a.generate(req)

    # Underlying ClaudeCodeAdapter saw the merged prompt and the right kwargs.
    call = fake.calls[0]
    assert "be brief" in call["prompt"]
    assert "explain async" in call["prompt"]
    assert call["history"] == []
    assert call["model"] == "sonnet"
    assert call["timeout_ms"] == 30_000
    assert call["resume_session_id"] is None
    assert call["persist_session"] is False

    # Response normalized.
    assert resp.text == "claude-reply"
    assert resp.provider == "claude_cli"
    assert resp.model == "claude-sonnet-4"
    assert resp.prompt_tokens == 11
    assert resp.completion_tokens == 22
    assert resp.duration_ms == 42


# ---- HermesProfileAdapter -------------------------------------------------


@pytest.mark.asyncio
async def test_hermes_profile_adapter_pins_profile_and_flattens():
    fake = _FakeHermesAdapter()
    a = HermesProfileAdapter(fake, profile="journal_ops")
    assert a.provider == "hermes_profile"
    assert a.model == "journal_ops"

    req = AdapterRequest(
        messages=[
            ChatMessage(role="system", content="ctx"),
            ChatMessage(role="user", content="log activity"),
        ],
        timeout_s=60.0,
    )
    resp = await a.generate(req)

    call = fake.calls[0]
    # Hermes call had profile pinned and model/provider deferred to config.yaml.
    assert call["profile"] == "journal_ops"
    assert call["model"] is None
    assert call["provider"] is None
    assert call["timeout_ms"] == 60_000
    # Query is the flattened prompt — both system and user content present.
    assert "ctx" in call["query"]
    assert "log activity" in call["query"]

    # Response normalized; model is the profile name (stable arm key).
    assert resp.provider == "hermes_profile"
    assert resp.model == "journal_ops"
    assert resp.text == "hermes-reply"
    assert resp.prompt_tokens == 30
    assert resp.completion_tokens == 15
    assert resp.duration_ms == 88


# ---- Protocol conformance -------------------------------------------------


@pytest.mark.asyncio
async def test_all_adapters_satisfy_llmadapter_protocol():
    """isinstance() against runtime_checkable LLMAdapter must accept all 3."""
    adapters = [
        OllamaAdapter(_FakeLLMClient()),
        ClaudeCLIAdapter(_FakeClaudeAdapter(), model="haiku"),
        HermesProfileAdapter(_FakeHermesAdapter(), profile="calendar_ops"),
    ]
    for a in adapters:
        assert isinstance(a, LLMAdapter), f"{type(a).__name__} not LLMAdapter"
        # And generate works.
        resp = await a.generate(
            AdapterRequest(messages=[ChatMessage(role="user", content="hi")]),
        )
        assert isinstance(resp, AdapterResponse)
        assert resp.provider in {
            "ollama", "openai", "claude_cli", "hermes_profile",
        }
