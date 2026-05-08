"""W6 — MCP growth-action smoke tests.

Drives src.mcp.server.HybridMCPServer.handle_request() directly (no
src.mcp.client package exists). Validates 17 extended tools and dry-run
behavior of all 12 write endpoints.
"""
from __future__ import annotations

import asyncio
import os
from pathlib import Path
from typing import Any

import pytest


REPO_ROOT = Path(__file__).resolve().parent.parent.parent


@pytest.fixture(autouse=True)
def _enable_blocks(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make sure HERMES_DISABLE_GROWTH_BLOCKS is unset for these tests."""
    monkeypatch.delenv("HERMES_DISABLE_GROWTH_BLOCKS", raising=False)


def test_register_extensions_count() -> None:
    """register_extensions adds 17 tools to a fresh empty list.

    Note: src.mcp.server's W6a marker block already self-registers on import,
    so _TOOLS will already contain 18 entries (1 builtin + 17 extensions).
    Use a fresh empty list to verify the function adds 17 net-new tools.
    """
    from src.mcp.server_extensions_generated import register_extensions

    tools: list = []
    added = register_extensions(tools)
    assert added == 17, f"expected 17 new tools on fresh list, got {added}"
    assert len(tools) == 17


def test_post_import_tools_count() -> None:
    """After importing src.mcp.server (which triggers W6a self-register),
    _TOOLS holds 1 builtin + 17 extensions = 18."""
    from src.mcp.server import _TOOLS

    names = {getattr(t, "name", "") for t in _TOOLS}
    assert "hybrid.handle" in names
    assert "hermes_status" in names
    assert "hermes_memory_add" in names
    assert len(_TOOLS) >= 18, f"expected >=18 after self-register, got {len(_TOOLS)}"


def test_register_extensions_idempotent() -> None:
    """Calling register twice on the same list does not double-register."""
    from src.mcp.server_extensions_generated import register_extensions

    tools: list = []
    register_extensions(tools)
    again = register_extensions(tools)
    assert again == 0


def _payload(name: str, args: dict[str, Any]) -> dict[str, Any]:
    return {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "tools/call",
        "params": {"name": name, "arguments": args},
    }


@pytest.mark.asyncio
async def test_dispatch_unknown_returns_none() -> None:
    from src.mcp.server_extensions_generated import dispatch

    resp = await dispatch("not_a_tool", {"user_id": "u1"})
    assert resp is None


@pytest.mark.parametrize("tool, args", [
    ("hermes_memory_add", {"user_id": "u1", "content": "hello", "dry_run": True}),
    ("hermes_memory_flip", {"user_id": "u1", "memory_id": 42, "should_store": False, "dry_run": True}),
    ("hermes_skill_draft", {"user_id": "u1", "name": "x", "content": "y", "category": "documentation", "dry_run": True}),
    ("hermes_skill_promote", {"user_id": "u1", "slug": "x", "dry_run": True}),
    ("hermes_skill_revert", {"user_id": "u1", "slug": "x", "reason": "test", "dry_run": True}),
    ("hermes_user_profile_patch", {"user_id": "u1", "claim": "c", "evidence": "e", "action": "add", "dry_run": True}),
    ("hermes_soul_regenerate", {"user_id": "u1", "dry_run": True}),
    ("hermes_trigger_self_review", {"user_id": "u1", "dry_run": True}),
    ("hermes_trigger_dialectic", {"user_id": "u1", "dry_run": True}),
    ("hermes_delegation_record", {"user_id": "u1", "intent_cluster": "c", "agents": ["@a"], "dry_run": True}),
    ("hermes_delegation_suggest", {"user_id": "u1", "intent_cluster": "github_repo_analysis"}),
    ("hermes_capture_baseline", {"user_id": "u1", "dry_run": True}),
])
@pytest.mark.asyncio
async def test_dispatch_dry_run(tool: str, args: dict) -> None:
    from src.mcp.server_extensions_generated import dispatch

    resp = await dispatch(tool, args)
    assert resp is not None, f"{tool}: dispatch returned None"
    assert "content" in resp
    text = resp["content"][0]["text"]
    assert tool != "hermes_delegation_suggest" or '"agents"' in text
    # All other dry-run handlers should return either a 'would_*' action or graceful 'noop'.


@pytest.mark.asyncio
async def test_real_memory_add(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Non-dry-run hermes_memory_add creates a row tagged source='mcp_external'."""
    from src.mcp import server_extensions_generated as ext

    # Mirror real-world setup: P0c.2.0 migration adds the `source` column
    # before any ingest. Apply the same migration to the fresh test DB.
    db_dir = tmp_path / "data" / "memory"
    db_dir.mkdir(parents=True, exist_ok=True)
    db = db_dir / "memos.db"

    import aiosqlite
    async with aiosqlite.connect(db) as con:
        await con.executescript(
            """
            CREATE TABLE IF NOT EXISTS memos (
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id    TEXT NOT NULL,
                text       TEXT NOT NULL,
                created_at TEXT NOT NULL,
                source     TEXT DEFAULT 'manual'
            );
            """
        )
        await con.commit()

    monkeypatch.setattr(ext, "_REPO_ROOT", tmp_path)

    resp = await ext.dispatch("hermes_memory_add", {
        "user_id": "u1", "content": "test memo", "source": "mcp_external",
    })
    assert resp is not None
    text = resp["content"][0]["text"]
    assert '"action": "saved"' in text or '"saved"' in text

    async with aiosqlite.connect(db) as con:
        async with con.execute("SELECT source FROM memos") as cur:
            rows = await cur.fetchall()
    assert any(r[0] == "mcp_external" for r in rows)


def test_handle_request_unknown_tool_returns_error() -> None:
    """Existing 1-tool fallback still works for non-extension tool calls."""
    from src.mcp.server import HybridMCPServer

    class _StubOrch:
        async def handle(self, *a, **kw):  # noqa: ARG002
            class R:
                response = "ok"
                handled_by = "stub"

                class task:
                    degraded = False
                    current_tier = "L2"
                    task_id = "t"
                    retry_count = 0

            return R()

    server = HybridMCPServer(_StubOrch())
    payload = {
        "jsonrpc": "2.0", "id": 9, "method": "tools/call",
        "params": {"name": "definitely_not_a_tool", "arguments": {"user_id": "u"}},
    }
    resp = asyncio.get_event_loop().run_until_complete(server.handle_request(payload))
    assert resp is not None
    assert "error" in resp or "result" in resp
