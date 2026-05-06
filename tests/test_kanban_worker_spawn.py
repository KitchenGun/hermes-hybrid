"""Tests for the master-CLI worker spawn helper (Phase 2-A).

Real subprocess fork is replaced by a fake ``create_subprocess_exec`` so
we can assert env vars, command shape, and stdin payload without a real
Claude CLI install.
"""
from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from src.config import Settings
from src.core.kanban.models import KanbanTask
from src.core.kanban.worker_runner import (
    _build_master_cmd,
    _build_worker_env,
    _build_worker_initial_message,
    spawn_master_worker,
)


def _task(**overrides) -> KanbanTask:
    base = dict(
        id="t_abc",
        title="echo hello",
        body="run echo hello",
        status="running",
        board_id="default",
        assignee="devops",
        tenant="biz-a",
        workspace_path="/tmp/ws",
        created_at="2026-05-07T00:00:00+00:00",
        updated_at="2026-05-07T00:00:00+00:00",
    )
    base.update(overrides)
    return KanbanTask(**base)


def test_build_master_cmd_local():
    s = Settings(
        master_cli_backend="local_subprocess",
        master_cli_path="/usr/bin/claude",
        master_model="opus",
    )
    assert _build_master_cmd(s) == [
        "/usr/bin/claude",
        "-p",
        "--model", "opus",
        "--output-format", "json",
        "--no-session-persistence",
    ]


def test_build_master_cmd_wsl_wraps_in_bash():
    s = Settings(
        master_cli_backend="wsl_subprocess",
        wsl_distro="Ubuntu",
        master_cli_path="/home/u/.local/bin/claude",
        master_model="sonnet",
    )
    cmd = _build_master_cmd(s)
    assert cmd[:4] == ["wsl", "-d", "Ubuntu", "bash"]
    # the inner shell payload should mention claude
    assert "claude" in cmd[-1]


def test_build_worker_initial_message_includes_guidance():
    msg = _build_worker_initial_message(_task())
    assert "Hermes Kanban worker" in msg  # KANBAN_GUIDANCE marker
    assert "t_abc" in msg
    assert "echo hello" in msg
    assert "kanban_cli.py show t_abc" in msg


def test_build_worker_env_sets_kanban_vars():
    env = _build_worker_env(_task())
    assert env["HERMES_KANBAN_TASK"] == "t_abc"
    assert env["HERMES_KANBAN_BOARD"] == "default"
    assert env["HERMES_KANBAN_WORKSPACE"] == "/tmp/ws"
    assert env["HERMES_TENANT"] == "biz-a"


def test_build_worker_env_skips_optional_when_missing():
    task = _task(tenant=None, workspace_path=None)
    env = _build_worker_env(task)
    assert env["HERMES_KANBAN_TASK"] == "t_abc"
    assert "HERMES_TENANT" not in env
    assert "HERMES_KANBAN_WORKSPACE" not in env


def test_build_worker_env_includes_skills_when_set():
    task = _task(skills=["security", "k8s"])
    env = _build_worker_env(task)
    assert env["HERMES_KANBAN_SKILLS"] == "security,k8s"


def test_build_worker_initial_message_lists_skills():
    task = _task(skills=["security", "k8s"])
    msg = _build_worker_initial_message(task)
    assert "security, k8s" in msg
    assert "Per-task skills" in msg


@pytest.mark.asyncio
async def test_spawn_master_worker_passes_env_and_stdin(monkeypatch):
    captured: dict = {}

    async def fake_create_subprocess_exec(*args, env=None, stdin=None,
                                          stdout=None, stderr=None):
        captured["argv"] = args
        captured["env"] = env or {}
        written = bytearray()

        async def _drain() -> None:
            return None

        stdin_obj = SimpleNamespace(
            write=lambda data: written.extend(data),
            drain=_drain,
            close=lambda: captured.update({"stdin": bytes(written)}),
        )
        return SimpleNamespace(pid=12345, stdin=stdin_obj)

    monkeypatch.setattr(
        asyncio, "create_subprocess_exec", fake_create_subprocess_exec
    )

    settings = Settings(
        master_cli_backend="local_subprocess",
        master_cli_path="/bin/echo",
        master_model="haiku",
    )
    pid = await spawn_master_worker(_task(), settings)
    assert pid == 12345
    assert captured["env"]["HERMES_KANBAN_TASK"] == "t_abc"
    payload = captured["stdin"].decode("utf-8")
    assert "Hermes Kanban worker" in payload
    assert "t_abc" in payload
