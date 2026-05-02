"""Tests for src/job_factory/runner.py."""
from __future__ import annotations

import asyncio
import json

import pytest

from src.job_factory.runner import (
    ALWAYS_ALLOWED_TOOLS,
    ActionRunner,
    ParsedAction,
    ToolCall,
    ToolRegistry,
    ToolResult,
    parse_llm_output,
)


# ---- parse_llm_output -----------------------------------------------------


def test_parse_empty_returns_no_action():
    p = parse_llm_output("")
    assert p.call is None
    assert p.response == ""


def test_parse_plain_text_is_response_only():
    p = parse_llm_output("just chatting, hi there")
    assert p.call is None
    assert p.response == "just chatting, hi there"


def test_parse_full_json_with_tool_call():
    payload = json.dumps({
        "thought": "user wants to log",
        "action": {"tool": "sheets_append", "args": {"row": [1, 2, 3]}},
        "response_to_user": "✅ saved",
    })
    p = parse_llm_output(payload)
    assert p.call is not None
    assert p.call.tool == "sheets_append"
    assert p.call.args == {"row": [1, 2, 3]}
    assert p.response == "✅ saved"


def test_parse_strips_code_fences():
    payload = '```json\n{"action": {"tool": "x", "args": {}}}\n```'
    p = parse_llm_output(payload)
    assert p.call is not None
    assert p.call.tool == "x"


def test_parse_response_only_json():
    """JSON without an action is just a response."""
    payload = json.dumps({"response_to_user": "hello"})
    p = parse_llm_output(payload)
    assert p.call is None
    assert p.response == "hello"


def test_parse_action_missing_tool_field_treated_as_no_call():
    payload = json.dumps({"action": {"args": {}}})  # no tool
    p = parse_llm_output(payload)
    assert p.call is None


def test_parse_action_args_must_be_dict():
    payload = json.dumps({"action": {"tool": "x", "args": "not a dict"}})
    p = parse_llm_output(payload)
    assert p.call is None


def test_parse_invalid_json_falls_back_to_text():
    p = parse_llm_output('{"action": {broken')
    assert p.call is None
    # Falls back to plain text — preserves the input for the dispatcher
    # to potentially show the user.
    assert p.response.startswith('{"action"')


# ---- ToolRegistry ---------------------------------------------------------


def test_tool_registry_register_and_get():
    reg = ToolRegistry()

    async def add(args):
        return args["a"] + args["b"]

    reg.register("add", add)
    assert reg.has("add")
    assert reg.get("add") is add
    assert "add" in reg.names()


def test_tool_registry_duplicate_register_raises():
    reg = ToolRegistry()
    async def fn(_): return None
    reg.register("x", fn)
    with pytest.raises(ValueError, match="already registered"):
        reg.register("x", fn)


def test_tool_registry_empty_name_raises():
    reg = ToolRegistry()
    async def fn(_): return None
    with pytest.raises(ValueError):
        reg.register("", fn)


def test_tool_registry_get_unknown_returns_none():
    reg = ToolRegistry()
    assert reg.get("nope") is None


# ---- ActionRunner: success cases ------------------------------------------


@pytest.mark.asyncio
async def test_execute_calls_registered_tool_with_args():
    reg = ToolRegistry()
    captured = {}

    async def my_tool(args):
        captured["args"] = args
        return {"result": args["x"] * 2}

    reg.register("my_tool", my_tool)
    runner = ActionRunner(reg)
    payload = json.dumps({
        "action": {"tool": "my_tool", "args": {"x": 5}},
    })
    result = await runner.execute(
        payload, job_required_tools=("my_tool",),
    )
    assert result.status == "ok"
    assert result.tool == "my_tool"
    assert result.output == {"result": 10}
    assert captured["args"] == {"x": 5}


@pytest.mark.asyncio
async def test_execute_plain_text_returns_respond_only():
    reg = ToolRegistry()
    runner = ActionRunner(reg)
    result = await runner.execute("just hi")
    assert result.status == "respond_only"
    assert result.output == "just hi"


@pytest.mark.asyncio
async def test_execute_response_only_json_returns_respond_only():
    reg = ToolRegistry()
    runner = ActionRunner(reg)
    result = await runner.execute(
        json.dumps({"response_to_user": "hello"}),
    )
    assert result.status == "respond_only"
    assert result.output == "hello"


@pytest.mark.asyncio
async def test_execute_respond_to_user_tool_is_always_allowed():
    """``respond_to_user`` is in ALWAYS_ALLOWED_TOOLS — works without
    job ACL."""
    reg = ToolRegistry()
    runner = ActionRunner(reg)
    payload = json.dumps({
        "action": {"tool": "respond_to_user", "args": {"text": "OK"}},
    })
    # Empty allowlist on purpose.
    result = await runner.execute(payload, job_required_tools=())
    assert result.status == "respond_only"
    assert result.output == "OK"


# ---- ActionRunner: ACL ----------------------------------------------------


@pytest.mark.asyncio
async def test_execute_blocks_tool_not_in_acl():
    reg = ToolRegistry()
    async def secret(_): return "secret data"
    reg.register("secret", secret)

    runner = ActionRunner(reg)
    payload = json.dumps({"action": {"tool": "secret", "args": {}}})
    result = await runner.execute(
        payload, job_required_tools=("not_secret",),
    )
    assert result.status == "denied"
    assert "secret" in result.error


@pytest.mark.asyncio
async def test_execute_unknown_tool_returns_error():
    reg = ToolRegistry()
    runner = ActionRunner(reg)
    payload = json.dumps({"action": {"tool": "nope", "args": {}}})
    result = await runner.execute(
        payload, job_required_tools=("nope",),  # ACL OK but not registered
    )
    assert result.status == "error"
    assert "not registered" in result.error


# ---- ActionRunner: failure / timeout --------------------------------------


@pytest.mark.asyncio
async def test_execute_tool_exception_propagates_as_error():
    reg = ToolRegistry()

    async def crashing(_):
        raise ValueError("boom")

    reg.register("crashing", crashing)
    runner = ActionRunner(reg)
    payload = json.dumps({"action": {"tool": "crashing", "args": {}}})
    result = await runner.execute(
        payload, job_required_tools=("crashing",),
    )
    assert result.status == "error"
    assert "ValueError" in result.error
    assert "boom" in result.error


@pytest.mark.asyncio
async def test_execute_tool_timeout():
    reg = ToolRegistry()

    async def slow(_):
        await asyncio.sleep(2.0)

    reg.register("slow", slow)
    runner = ActionRunner(reg)
    payload = json.dumps({"action": {"tool": "slow", "args": {}}})
    result = await runner.execute(
        payload, job_required_tools=("slow",), timeout_s=0.05,
    )
    assert result.status == "error"
    assert "timed out" in result.error


@pytest.mark.asyncio
async def test_execute_no_action_with_no_text_returns_no_action():
    reg = ToolRegistry()
    runner = ActionRunner(reg)
    result = await runner.execute("")
    assert result.status == "no_action"


# ---- always-allowed tool list ---------------------------------------------


def test_respond_to_user_is_always_allowed():
    assert "respond_to_user" in ALWAYS_ALLOWED_TOOLS


# ---- ToolResult convenience constructors ---------------------------------


def test_tool_result_ok_constructor():
    r = ToolResult.ok(tool="x", output={"y": 1})
    assert r.status == "ok"
    assert r.tool == "x"
    assert r.output == {"y": 1}


def test_tool_result_error_constructor():
    r = ToolResult.error(tool="x", error="boom")
    assert r.status == "error"
    assert r.error == "boom"


def test_tool_result_denied_constructor():
    r = ToolResult.denied(tool="x", error="not allowed")
    assert r.status == "denied"


def test_tool_result_respond_only_constructor():
    r = ToolResult.respond_only(output="hello")
    assert r.status == "respond_only"
    assert r.output == "hello"
    assert r.tool == ""
