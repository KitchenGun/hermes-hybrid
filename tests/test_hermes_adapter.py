"""Hermes adapter unit tests — stdout parsing, trace projection (R1, R2/R9).

v2 contract tests (FIX#5) start at ``test_v2_extract_*`` below. They exercise
the new ``_extract_v2`` extraction, the ``provider_actual`` verification, the
turns_used budget trip, and backward-compat on pre-v2 session JSONs.
"""
from __future__ import annotations

import json

import pytest

from src.config import Settings
from src.hermes_adapter.adapter import (
    HermesAdapter,
    HermesAdapterError,
    HermesBudgetExceeded,
    HermesProviderMismatch,
    HermesResult,
    _providers_compatible,
)


def test_parse_stdout_extracts_session_id_and_strips_line(settings: Settings):
    a = HermesAdapter(settings)
    stdout = (
        "session_id: 20260419_120000_abc123\n"
        "Hello, this is the Hermes response.\n"
        "Second line of reply."
    )
    text, sid = a._parse_stdout(stdout)
    assert sid == "20260419_120000_abc123"
    assert "session_id" not in text
    assert text.startswith("Hello, this is the Hermes response.")
    assert "Second line of reply." in text


def test_parse_stdout_handles_no_session_id(settings: Settings):
    a = HermesAdapter(settings)
    text, sid = a._parse_stdout("plain reply\n")
    assert sid is None
    assert text == "plain reply"


def test_parse_stdout_handles_json_wrapper(settings: Settings):
    a = HermesAdapter(settings)
    payload = json.dumps({"response": "wrapped reply", "session_id": "sid-xyz"})
    text, sid = a._parse_stdout(payload)
    assert text == "wrapped reply"
    assert sid == "sid-xyz"


def test_project_trace_extracts_goal_actions_observations_reflections():
    session = {
        "session_id": "sid-1",
        "model": "gpt-4o",
        "message_count": 4,
        "tools": [{"name": "web_search"}, {"name": "calc"}],
        "messages": [
            {"role": "user", "content": "Find the population of Seoul and double it."},
            {
                "role": "assistant",
                "content": "I'll search for Seoul's population.",
                "tool_calls": [
                    {"function": {"name": "web_search", "arguments": '{"q":"seoul population"}'}}
                ],
            },
            {"role": "tool", "name": "web_search", "content": "Seoul population is 9.7M."},
            {"role": "assistant", "content": "Doubling gives 19.4M."},
        ],
    }
    trace = HermesAdapter._project_trace(session)
    assert trace["session_id"] == "sid-1"
    assert trace["model"] == "gpt-4o"
    assert trace["message_count"] == 4
    assert trace["plan"]["tools_declared"] == 2
    assert "Seoul" in trace["plan"]["goal"]
    assert len(trace["actions"]) == 1
    assert trace["actions"][0]["tool"] == "web_search"
    assert len(trace["observations"]) == 1
    assert trace["observations"][0]["tool"] == "web_search"
    assert "9.7M" in trace["observations"][0]["preview"]
    assert len(trace["reflections"]) == 2  # both assistant messages with text


def test_project_trace_handles_empty_session():
    trace = HermesAdapter._project_trace({})
    assert trace["actions"] == []
    assert trace["observations"] == []
    assert trace["reflections"] == []
    assert trace["plan"]["goal"] == ""


@pytest.mark.asyncio
async def test_run_requires_explicit_model_and_provider(settings: Settings, monkeypatch):
    """R2/R9: adapter must never pick a default model — caller owns the mapping.

    We verify this by asserting that `model` and `provider` are passed through
    to the constructed command untouched.
    """
    a = HermesAdapter(settings)
    cmd = a._build_cmd(
        query="hi", model="gpt-4o", provider="openrouter",
        resume_session=None, max_turns=5, extra_args=[],
    )
    # In wsl_subprocess mode the whole thing is passed as one bash -lc string.
    joined = " ".join(cmd)
    assert "-m gpt-4o" in joined
    assert "--provider openrouter" in joined


def test_build_cmd_rejects_unknown_backend(settings: Settings):
    settings.hermes_cli_backend = "mcp"  # stub backend not implemented yet (R17 deferred)
    a = HermesAdapter(settings)
    with pytest.raises(HermesAdapterError):
        a._build_cmd(
            query="hi", model="gpt-4o", provider="openrouter",
            resume_session=None, max_turns=5, extra_args=[],
        )


def test_build_cmd_passes_resume_session(settings: Settings):
    a = HermesAdapter(settings)
    cmd = a._build_cmd(
        query="hi", model="gpt-4o", provider="openrouter",
        resume_session="20260419_abc", max_turns=5, extra_args=[],
    )
    joined = " ".join(cmd)
    assert "--resume 20260419_abc" in joined


# ======================================================================
# v2 contract tests (FIX#5)
# ======================================================================


def test_v2_extract_populates_models_used_and_primary():
    """modelUsage rows → models_used list + primary_model = the one with the
    most turns. This is the FIX#3 observability hook."""
    session = {
        "provider": "ollama",
        "modelUsage": [
            {"model": "qwen2.5:7b-instruct", "turns": 1, "prompt_tokens": 50, "completion_tokens": 30},
            {"model": "qwen2.5:14b-instruct", "turns": 3, "prompt_tokens": 200, "completion_tokens": 150},
        ],
        "turns_used": 4,
        "skills_invoked": ["hybrid-status"],
        "mcp_tools_invoked": ["fetch.get"],
        "total_cost_usd": 0.0,
    }
    v2 = HermesAdapter._extract_v2(session, requested_provider="ollama", requested_model="qwen2.5:7b-instruct")
    assert v2["provider_actual"] == "ollama"
    assert v2["models_used"] == ["qwen2.5:7b-instruct", "qwen2.5:14b-instruct"]
    assert v2["primary_model"] == "qwen2.5:14b-instruct"  # 3 turns > 1 turn
    assert v2["turns_used"] == 4
    assert v2["skills_invoked"] == ["hybrid-status"]
    assert v2["mcp_tools_invoked"] == ["fetch.get"]
    assert v2["prompt_tokens"] == 250
    assert v2["completion_tokens"] == 180


def test_v2_extract_empty_session_returns_safe_defaults():
    """Pre-v2 Hermes builds return empty {} — adapter must not crash."""
    v2 = HermesAdapter._extract_v2({}, requested_provider="ollama", requested_model="qwen2.5:7b-instruct")
    assert v2["provider_actual"] == ""
    assert v2["models_used"] == []
    assert v2["primary_model"] == ""
    assert v2["turns_used"] == 0
    assert v2["total_cost_usd"] == 0.0


def test_v2_extract_falls_back_to_assistant_message_count_when_turns_missing():
    """If modelUsage is absent, turns_used ≈ number of assistant messages —
    crude upper bound so R2 still has something to compare against."""
    session = {
        "provider": "openai",
        "messages": [
            {"role": "user", "content": "hi"},
            {"role": "assistant", "content": "one"},
            {"role": "assistant", "content": "two"},
            {"role": "assistant", "content": "three"},
        ],
    }
    v2 = HermesAdapter._extract_v2(session, requested_provider="openai", requested_model="gpt-4o")
    assert v2["turns_used"] == 3
    assert v2["provider_actual"] == "openai"


def test_v2_provider_alias_openai_chat_compatible():
    """Hermes may spell ``openai-chat`` where we pinned ``openai`` — treat as
    equivalent so we don't false-positive on R1."""
    assert _providers_compatible("openai", "openai-chat") is True
    assert _providers_compatible("ollama", "ollama_local") is True
    assert _providers_compatible("openai", "anthropic") is False
    assert _providers_compatible("openai", "claude-code") is False


@pytest.mark.asyncio
async def test_v2_run_raises_provider_mismatch_when_hermes_falls_back(
    settings: Settings, monkeypatch
):
    """R1 fail-closed: if Hermes session JSON reports a different provider
    than we pinned, ``run()`` must raise ``HermesProviderMismatch`` instead
    of returning the response. Protects Max from sneaky cloud fallback."""
    a = HermesAdapter(settings)

    async def fake_exec(*cmd, **kwargs):
        class _Proc:
            returncode = 0
            async def communicate(self):
                return (b"session_id: sid-mismatch\nsome reply\n", b"")
        return _Proc()

    async def fake_load(self, session_id):  # noqa: ARG001
        # Hermes claims it used anthropic, even though we asked for openai.
        return {
            "provider": "anthropic",
            "modelUsage": [{"model": "claude-opus-4-7", "turns": 1}],
            "turns_used": 1,
            "messages": [{"role": "assistant", "content": "some reply"}],
        }

    monkeypatch.setattr("asyncio.create_subprocess_exec", fake_exec)
    monkeypatch.setattr(HermesAdapter, "_load_session_json", fake_load)

    with pytest.raises(HermesProviderMismatch) as ei:
        await a.run("hi", model="gpt-4o", provider="openai", max_turns=3, timeout_ms=5000)
    assert ei.value.requested == "openai"
    assert ei.value.actual == "anthropic"


@pytest.mark.asyncio
async def test_v2_run_raises_budget_exceeded_when_turns_overrun(
    settings: Settings, monkeypatch
):
    """R2 fail-closed: if Hermes burned more turns than --max-turns allowed,
    raise ``HermesBudgetExceeded``. Guards against a broken CLI that ignores
    the cap."""
    a = HermesAdapter(settings)

    async def fake_exec(*cmd, **kwargs):
        class _Proc:
            returncode = 0
            async def communicate(self):
                return (b"session_id: sid-overrun\nfinal answer\n", b"")
        return _Proc()

    async def fake_load(self, session_id):  # noqa: ARG001
        return {
            "provider": "ollama",
            "modelUsage": [{"model": "qwen2.5:7b-instruct", "turns": 10}],
            "turns_used": 10,  # we asked for max 3
            "messages": [{"role": "assistant", "content": "final answer"}],
        }

    monkeypatch.setattr("asyncio.create_subprocess_exec", fake_exec)
    monkeypatch.setattr(HermesAdapter, "_load_session_json", fake_load)

    with pytest.raises(HermesBudgetExceeded) as ei:
        await a.run(
            "hi", model="qwen2.5:7b-instruct", provider="ollama",
            max_turns=3, timeout_ms=5000,
        )
    assert ei.value.kind == "turns"
    assert ei.value.used == 10
    assert ei.value.cap == 3


@pytest.mark.asyncio
async def test_v2_run_populates_contract_on_happy_path(
    settings: Settings, monkeypatch
):
    """On success, all v2 fields are populated from the session JSON and
    returned as a ``HermesResult`` with backward-compat fields intact."""
    a = HermesAdapter(settings)

    async def fake_exec(*cmd, **kwargs):
        class _Proc:
            returncode = 0
            async def communicate(self):
                return (b"session_id: sid-ok\nHello back.\n", b"")
        return _Proc()

    async def fake_load(self, session_id):  # noqa: ARG001
        return {
            "provider": "ollama",
            "model": "qwen2.5:7b-instruct",
            "modelUsage": [
                {"model": "qwen2.5:7b-instruct", "turns": 2,
                 "prompt_tokens": 40, "completion_tokens": 20,
                 "cost_usd": 0.0},
            ],
            "turns_used": 2,
            "skills_invoked": ["hybrid-status"],
            "mcp_tools_invoked": [],
            "total_cost_usd": 0.0,
            "messages": [
                {"role": "user", "content": "hi"},
                {"role": "assistant", "content": "Hello back."},
            ],
        }

    monkeypatch.setattr("asyncio.create_subprocess_exec", fake_exec)
    monkeypatch.setattr(HermesAdapter, "_load_session_json", fake_load)

    result = await a.run(
        "hi", model="qwen2.5:7b-instruct", provider="ollama",
        max_turns=5, timeout_ms=5000,
    )
    assert isinstance(result, HermesResult)
    # backward-compat
    assert result.text == "Hello back."
    assert result.session_id == "sid-ok"
    assert result.model_name == "qwen2.5:7b-instruct"
    assert result.provider == "ollama"
    # v2 contract
    assert result.provider_requested == "ollama"
    assert result.provider_actual == "ollama"
    assert result.primary_model == "qwen2.5:7b-instruct"
    assert result.models_used == ["qwen2.5:7b-instruct"]
    assert result.turns_used == 2
    assert result.skills_invoked == ["hybrid-status"]
    assert result.prompt_tokens == 40
    assert result.completion_tokens == 20
    assert result.total_cost_usd == 0.0
    assert result.raw_json["provider"] == "ollama"
