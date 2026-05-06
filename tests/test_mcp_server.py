"""Tests for the Phase 3 MCP server stub (``src/mcp/server.py``).

Covers the JSON-RPC 2.0 surface: initialize handshake, tools/list,
tools/call dispatch to the Orchestrator, notification vs request
(id presence), and error responses for unknown methods / invalid tools
/ invalid args.
"""
from __future__ import annotations

from typing import Any

import pytest

from src.config import Settings
from src.mcp import HybridMCPServer
from src.orchestrator import Orchestrator, OrchestratorResult
from src.state.task_state import TaskState


# ---- fake orchestrator ------------------------------------------------------


class _FakeOrchestrator:
    """Minimal stand-in for Orchestrator — we only need ``handle()``."""

    def __init__(self, response: str = "ok"):
        self._response = response
        self.calls: list[dict[str, Any]] = []

    async def handle(
        self,
        user_message: str,
        *,
        user_id: str,
        session_id: str | None = None,
        history: list[dict[str, str]] | None = None,
    ) -> OrchestratorResult:
        self.calls.append({"user_message": user_message, "user_id": user_id})
        task = TaskState(
            session_id=session_id or "sid-fake",
            user_id=user_id,
            user_message=user_message,
        )
        task.status = "succeeded"
        task.final_response = self._response
        return OrchestratorResult(task=task, response=self._response, handled_by="rule")


@pytest.fixture
def server():
    return HybridMCPServer(_FakeOrchestrator("fake reply"))  # type: ignore[arg-type]


# ---- initialize handshake ---------------------------------------------------


@pytest.mark.asyncio
async def test_initialize_returns_protocol_and_server_info(server):
    resp = await server.handle_request({
        "jsonrpc": "2.0", "id": 1, "method": "initialize",
        "params": {"protocolVersion": "2024-11-05", "capabilities": {}},
    })
    assert resp is not None
    assert resp["jsonrpc"] == "2.0"
    assert resp["id"] == 1
    assert "result" in resp
    r = resp["result"]
    assert "protocolVersion" in r
    assert r["serverInfo"]["name"] == "hermes-hybrid"


@pytest.mark.asyncio
async def test_initialized_notification_is_silent(server):
    """Notifications (no ``id``) must NOT produce a response."""
    resp = await server.handle_request({
        "jsonrpc": "2.0", "method": "notifications/initialized",
    })
    assert resp is None


# ---- tools/list -------------------------------------------------------------


@pytest.mark.asyncio
async def test_tools_list_advertises_hybrid_handle(server):
    resp = await server.handle_request({
        "jsonrpc": "2.0", "id": 2, "method": "tools/list",
    })
    tools = resp["result"]["tools"]
    assert any(t["name"] == "hybrid.handle" for t in tools)
    hh = next(t for t in tools if t["name"] == "hybrid.handle")
    assert "inputSchema" in hh
    props = hh["inputSchema"]["properties"]
    assert "user_message" in props
    assert "user_id" in props
    # Phase 11: heavy 인자 폐기 → schema 에서 제거됨


# ---- tools/call -------------------------------------------------------------


@pytest.mark.asyncio
async def test_tools_call_dispatches_to_orchestrator(server):
    resp = await server.handle_request({
        "jsonrpc": "2.0", "id": 3, "method": "tools/call",
        "params": {
            "name": "hybrid.handle",
            "arguments": {"user_message": "hi there", "user_id": "u1"},
        },
    })
    assert "result" in resp
    r = resp["result"]
    assert r["content"][0]["type"] == "text"
    assert r["content"][0]["text"] == "fake reply"
    assert r["isError"] is False
    assert r["_meta"]["handled_by"] == "rule"
    assert "task_id" in r["_meta"]


# Phase 11 (2026-05-06): heavy 인자 폐기 — test_tools_call_passes_heavy_flag 제거.


# ---- error handling ---------------------------------------------------------


@pytest.mark.asyncio
async def test_unknown_method_returns_method_not_found(server):
    resp = await server.handle_request({
        "jsonrpc": "2.0", "id": 5, "method": "bogus/method",
    })
    assert "error" in resp
    assert resp["error"]["code"] == -32601


@pytest.mark.asyncio
async def test_unknown_tool_returns_invalid_params(server):
    resp = await server.handle_request({
        "jsonrpc": "2.0", "id": 6, "method": "tools/call",
        "params": {"name": "not.a.tool", "arguments": {}},
    })
    assert "error" in resp
    assert resp["error"]["code"] == -32602


@pytest.mark.asyncio
async def test_tools_call_missing_args_errors(server):
    resp = await server.handle_request({
        "jsonrpc": "2.0", "id": 7, "method": "tools/call",
        "params": {"name": "hybrid.handle", "arguments": {"user_message": ""}},
    })
    assert "error" in resp
    assert resp["error"]["code"] == -32602


@pytest.mark.asyncio
async def test_invalid_jsonrpc_version_rejected(server):
    resp = await server.handle_request({"jsonrpc": "1.0", "id": 8, "method": "initialize"})
    assert "error" in resp
    assert resp["error"]["code"] == -32600


@pytest.mark.asyncio
async def test_orchestrator_exception_returns_server_error():
    class _BoomOrch:
        async def handle(self, *a, **kw):
            raise RuntimeError("kaboom")

    server = HybridMCPServer(_BoomOrch())  # type: ignore[arg-type]
    resp = await server.handle_request({
        "jsonrpc": "2.0", "id": 9, "method": "tools/call",
        "params": {
            "name": "hybrid.handle",
            "arguments": {"user_message": "x", "user_id": "u1"},
        },
    })
    assert "error" in resp
    assert resp["error"]["code"] == -32000
    assert "RuntimeError" in resp["error"]["message"]


# ---- integration with real Orchestrator ------------------------------------


@pytest.mark.asyncio
async def test_tools_call_with_real_orchestrator_rule_path(settings: Settings):
    """A real Orchestrator + static rule hit → MCP returns the rule output.

    2026-05-06: ``/status`` etc. are dynamic RuleLayer handlers that
    only resolve when the master path is wired in. ``/ping`` is the
    static-response rule that short-circuits regardless, so it's the
    safer regression target post-commit-4."""
    o = Orchestrator(settings)
    server = HybridMCPServer(o)
    resp = await server.handle_request({
        "jsonrpc": "2.0", "id": 10, "method": "tools/call",
        "params": {
            "name": "hybrid.handle",
            "arguments": {"user_message": "/ping", "user_id": "u1"},
        },
    })
    r = resp["result"]
    assert r["_meta"]["handled_by"] == "rule"
    assert "pong" in r["content"][0]["text"]
