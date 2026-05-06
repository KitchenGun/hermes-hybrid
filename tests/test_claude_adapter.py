"""Tests for src/claude_adapter/adapter.py — ClaudeCodeAdapter.

Phase 16 (2026-05-07) lockdowns:
  * ``_build_cmd`` includes ``--permission-mode acceptEdits`` so ``claude -p``
    auto-approves Edit/Write/MultiEdit (no interactive prompt available in
    print mode). Without this, requested file writes silently fail and
    Claude responds with a "권한 프롬프트를 한번 더 승인해주세요" text.
  * ``run`` passes ``cwd=settings.project_root`` to
    ``asyncio.create_subprocess_exec`` so Claude CLI finds the per-repo
    ``.claude/settings.json`` allow list.
"""
from __future__ import annotations

import asyncio
from unittest.mock import patch

import pytest

from src.claude_adapter import ClaudeCodeAdapter
from src.config import Settings


def _hermetic_settings(tmp_path) -> Settings:
    """Settings with project_root pointed at tmp_path so subprocess cwd
    assertions don't depend on the operator's actual repo location."""
    return Settings(
        _env_file=None,                          # type: ignore[call-arg]
        master_cli_path="/usr/bin/false",        # never actually invoked
        master_cli_backend="local_subprocess",   # avoid wsl wrapper
        master_concurrency=1,
        master_timeout_ms=5_000,
        project_root=tmp_path,
        state_db_path=tmp_path / "test.db",
        require_allowlist=False,
        master_enabled=False,
    )


def test_build_cmd_includes_permission_mode_accept_edits(tmp_path):
    """``-p`` mode 에서 권한 prompt 가 안 뜨므로 ``acceptEdits`` 명시 필수."""
    s = _hermetic_settings(tmp_path)
    a = ClaudeCodeAdapter(s)
    cmd = a._build_cmd(model="opus")
    # Pair must be present in this exact order — adjacent kwargs.
    assert "--permission-mode" in cmd
    idx = cmd.index("--permission-mode")
    assert cmd[idx + 1] == "acceptEdits"


def test_build_cmd_keeps_print_and_no_session_persistence(tmp_path):
    """Permission-mode addition must not break the existing flag set."""
    s = _hermetic_settings(tmp_path)
    a = ClaudeCodeAdapter(s)
    cmd = a._build_cmd(model="opus")
    assert "-p" in cmd
    assert "--no-session-persistence" in cmd
    assert "--output-format" in cmd
    assert cmd[cmd.index("--output-format") + 1] == "json"


def test_build_cmd_local_subprocess_returns_args_directly(tmp_path):
    """local_subprocess backend → no wsl wrapper around args."""
    s = _hermetic_settings(tmp_path)
    a = ClaudeCodeAdapter(s)
    cmd = a._build_cmd(model="haiku")
    assert cmd[0] == s.master_cli_path
    assert "wsl" not in cmd


@pytest.mark.asyncio
async def test_run_passes_cwd_to_subprocess(tmp_path):
    """The subprocess must be spawned with cwd=settings.project_root so
    Claude CLI finds the per-repo .claude/settings.json."""
    s = _hermetic_settings(tmp_path)
    a = ClaudeCodeAdapter(s)

    captured: dict = {}

    class _FakeProc:
        returncode = 0

        async def communicate(self, _stdin=None):
            return (
                b'{"result": "ok", "session_id": "s", "usage": {}, '
                b'"modelUsage": {"opus": {}}}',
                b"",
            )

        def kill(self):
            pass

        async def wait(self):
            pass

    async def _fake_exec(*args, **kwargs):
        captured["args"] = args
        captured["kwargs"] = kwargs
        return _FakeProc()

    with patch.object(asyncio, "create_subprocess_exec", _fake_exec):
        result = await a.run(prompt="hi")

    assert result.text == "ok"
    assert "cwd" in captured["kwargs"], (
        "create_subprocess_exec must be called with cwd kwarg"
    )
    assert captured["kwargs"]["cwd"] == str(s.project_root)
