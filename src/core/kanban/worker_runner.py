"""Master Claude CLI subprocess fork for Kanban workers (Phase 2-A).

The dispatcher calls ``spawn_master_worker(task, settings)`` to fork an
OS process running master Claude CLI in worker mode. The worker reads
``HERMES_KANBAN_TASK`` from env, sees ``KANBAN_GUIDANCE`` in its initial
message, and drives the work via ``python scripts/kanban_cli.py <verb>``
shell-outs (Phase 2-A: terminal-tool driven; Phase 2-B may add an MCP
server for richer tool schemas).

Mirrors ``ClaudeCodeAdapter._build_cmd`` so the same Max OAuth subscription
is used. Differs in two ways:
  - env carries ``HERMES_KANBAN_*`` vars
  - we Popen and return the pid; the dispatcher reaps via PID monitoring
"""
from __future__ import annotations

import asyncio
import os
import shlex
from pathlib import Path

from src.config import Settings
from src.core.kanban.guidance import KANBAN_GUIDANCE
from src.core.kanban.models import KanbanTask


def _repo_root() -> Path:
    # src/core/kanban/worker_runner.py → repo root is parents[3]
    return Path(__file__).resolve().parents[3]


def _build_master_cmd(settings: Settings) -> list[str]:
    """Build the ``claude -p`` argv. Mirrors ClaudeCodeAdapter._build_cmd."""
    args = [
        settings.master_cli_path,
        "-p",
        "--model", settings.master_model,
        "--output-format", "json",
        "--no-session-persistence",
    ]
    if settings.master_cli_backend == "wsl_subprocess":
        inner = " ".join(shlex.quote(a) for a in args)
        return ["wsl", "-d", settings.wsl_distro, "bash", "-lc", inner]
    if settings.master_cli_backend == "local_subprocess":
        return args
    raise ValueError(
        f"Unsupported master_cli_backend: {settings.master_cli_backend!r}"
    )


def _build_worker_initial_message(task: KanbanTask) -> str:
    """Compose the stdin payload for a worker-mode master CLI subprocess."""
    cli_path = _repo_root() / "scripts" / "kanban_cli.py"
    lines = [
        KANBAN_GUIDANCE.strip(),
        "",
        "# Initial task brief",
        f"task_id: {task.id}",
        f"title: {task.title}",
        f"assignee: {task.assignee or '(unassigned)'}",
        f"tenant: {task.tenant or '(none)'}",
        f"workspace: {task.workspace_path or '(none)'}",
    ]
    if task.skills:
        lines.append(f"skills: {', '.join(task.skills)}")
    lines.extend([
        "",
        "## Body",
        task.body or "(no body)",
        "",
        "## How to drive Kanban from this worker",
        f"Shell out via your terminal tool: `python {cli_path} <verb>`",
        "Common verbs: show / heartbeat / complete / block / comment / create / link.",
        f"Start with: `python {cli_path} show {task.id}`",
        "End with exactly one of: kanban complete OR kanban block.",
    ])
    if task.skills:
        lines.append("")
        lines.append("## Per-task skills")
        lines.append(
            "The dispatcher attached the skills above to this task — load "
            "their reference docs first if you have a `skill_view` or "
            "`skill_load` tool available, then proceed."
        )
    return "\n".join(lines)


def _build_worker_env(task: KanbanTask) -> dict[str, str]:
    env = os.environ.copy()
    env["HERMES_KANBAN_TASK"] = task.id
    env["HERMES_KANBAN_BOARD"] = task.board_id
    if task.workspace_path:
        env["HERMES_KANBAN_WORKSPACE"] = task.workspace_path
    if task.tenant:
        env["HERMES_TENANT"] = task.tenant
    if task.skills:
        env["HERMES_KANBAN_SKILLS"] = ",".join(task.skills)
    return env


async def spawn_master_worker(task: KanbanTask, settings: Settings) -> int:
    """Fork a master CLI subprocess in worker mode and return its PID.

    The subprocess is detached: we write the initial KANBAN_GUIDANCE +
    task brief to stdin, close it, and return immediately. The dispatcher
    polls PID liveness via ``os.kill(pid, 0)`` and TTL-checks claims.
    Output is sent to /dev/null because the Kanban events table is the
    durable record — stdout/stderr would just bloat disk.
    """
    cmd = _build_master_cmd(settings)
    env = _build_worker_env(task)
    initial = _build_worker_initial_message(task)

    proc = await asyncio.create_subprocess_exec(
        *cmd,
        env=env,
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.DEVNULL,
    )
    if proc.stdin is not None:
        proc.stdin.write(initial.encode("utf-8"))
        await proc.stdin.drain()
        proc.stdin.close()
    return proc.pid


__all__ = ["spawn_master_worker"]
