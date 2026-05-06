"""Adapter for Claude Code CLI — Hermes Master 의 단일 LLM lane.

Phase 11 (2026-05-06): master = Claude CLI (Max OAuth) 단일 lane 으로
전환됨. opencode 폐기. 사용자 Max 구독료만 (API key 별도 비용 X — $0
marginal).

Key design:
  - ``claude -p --output-format json --no-session-persistence`` →
    한 줄 JSON 응답, leftover session 파일 X.
  - Prompt 는 stdin (긴/특수문자 텍스트 안전).
  - Concurrency cap: ``master_concurrency`` (default 1) — Max OAuth 시간당
    한도 보호.
  - Phase 9: HermesMaster 가 ``@handle`` mention SKILL.md inject 를
    prompt 에 미리 합쳐 보냄.
  - Phase 10: ClaudeAgentDelegator 가 병렬 fan-out 시 같은 어댑터를
    재사용 (semaphore 가 자연스럽게 cap).
"""
from __future__ import annotations

import asyncio
import json
import re
import shlex
from dataclasses import dataclass, field
from typing import Any

from src.config import Settings
from src.obs import get_logger

log = get_logger(__name__)


class ClaudeCodeAdapterError(RuntimeError):
    pass


class ClaudeCodeTimeout(ClaudeCodeAdapterError):
    pass


class ClaudeCodeAuthError(ClaudeCodeAdapterError):
    """OAuth token expired / missing / Max subscription lapsed / quota."""


@dataclass
class ClaudeCodeResult:
    text: str
    model_name: str
    session_id: str | None = None
    duration_ms: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    total_cost_usd: float = 0.0
    raw: dict[str, Any] = field(default_factory=dict)


_AUTH_ERROR_RE = re.compile(
    r"(not (logged in|authenticated)|oauth|401|unauthorized|expired|credentials)",
    re.IGNORECASE,
)
_QUOTA_RE = re.compile(
    r"(out of extra usage|out of credits|usage limit|rate limit.*reset)",
    re.IGNORECASE,
)


class ClaudeCodeAdapter:
    """``claude -p`` subprocess wrapper — Hermes Master 의 single LLM lane."""

    def __init__(self, settings: Settings, *, concurrency: int | None = None):
        self.settings = settings
        effective = (
            concurrency
            if concurrency is not None
            else settings.master_concurrency
        )
        self._sem = asyncio.Semaphore(max(1, effective))

    async def run(
        self,
        *,
        prompt: str,
        history: list[dict[str, str]] | None = None,
        model: str | None = None,
        timeout_ms: int | None = None,
    ) -> ClaudeCodeResult:
        """Single-turn Claude Code call.

        ``history`` 는 prompt 안에 평탄화돼 들어감 — stateless 호출.
        Phase 11 후 ``resume_session_id`` / ``mcp_config_path`` /
        ``allowed_tools`` / ``append_system_prompt`` 등 heavy 시절 부가
        인자는 모두 제거됨.
        """
        model = model or self.settings.master_model
        timeout_ms = timeout_ms or self.settings.master_timeout_ms
        cmd = self._build_cmd(model=model)
        stdin_payload = self._build_stdin(
            prompt=prompt, history=history or []
        )

        async with self._sem:
            start = asyncio.get_event_loop().time()
            try:
                proc = await asyncio.create_subprocess_exec(
                    *cmd,
                    stdin=asyncio.subprocess.PIPE,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                    cwd=str(self.settings.project_root),
                )
                try:
                    stdout_b, stderr_b = await asyncio.wait_for(
                        proc.communicate(stdin_payload.encode("utf-8")),
                        timeout=timeout_ms / 1000.0,
                    )
                except asyncio.TimeoutError:
                    proc.kill()
                    await proc.wait()
                    raise ClaudeCodeTimeout(
                        f"Claude Code timed out after {timeout_ms}ms"
                    )
            except FileNotFoundError as e:
                raise ClaudeCodeAdapterError(
                    f"Claude Code CLI not available: {e}"
                ) from e

        duration_ms = int((asyncio.get_event_loop().time() - start) * 1000)
        stdout = stdout_b.decode("utf-8", errors="replace")
        stderr = stderr_b.decode("utf-8", errors="replace")

        if proc.returncode != 0:
            combined = stdout + "\n" + stderr
            if _AUTH_ERROR_RE.search(combined) or _QUOTA_RE.search(combined):
                raise ClaudeCodeAuthError(
                    f"Claude Code auth/quota failure: {combined[-300:]}"
                )
            raise ClaudeCodeAdapterError(
                f"Claude Code exited {proc.returncode}\n"
                f"stderr: {stderr[-500:]}"
            )

        try:
            data = json.loads(stdout.strip())
        except json.JSONDecodeError as e:
            raise ClaudeCodeAdapterError(
                f"Claude Code returned non-JSON: {stdout[:300]}"
            ) from e

        if data.get("is_error"):
            api_status = data.get("api_error_status")
            msg = data.get("result") or data.get("subtype") or "unknown"
            if api_status in (401, 403) or _AUTH_ERROR_RE.search(str(msg)):
                raise ClaudeCodeAuthError(f"Claude Code auth error: {msg}")
            if _QUOTA_RE.search(str(msg)):
                raise ClaudeCodeAuthError(f"Claude Code quota error: {msg}")
            raise ClaudeCodeAdapterError(
                f"Claude Code reported is_error (api_status={api_status}): {msg}"
            )

        text = str(data.get("result") or "").strip()
        usage = data.get("usage") or {}
        model_usage = data.get("modelUsage") or {}
        model_name = next(iter(model_usage.keys()), model)

        return ClaudeCodeResult(
            text=text,
            model_name=str(model_name),
            session_id=data.get("session_id"),
            duration_ms=duration_ms,
            input_tokens=int(usage.get("input_tokens", 0) or 0),
            output_tokens=int(usage.get("output_tokens", 0) or 0),
            total_cost_usd=float(data.get("total_cost_usd", 0.0) or 0.0),
            raw=data,
        )

    # ---- internals ----

    def _build_cmd(self, *, model: str) -> list[str]:
        # ``-p`` (print) mode disables interactive permission prompts. Without
        # ``--permission-mode``, edits to paths that aren't pre-allowed in
        # ``.claude/settings.json`` are auto-denied — Claude responds with a
        # "권한 프롬프트를 한번 더 승인해주세요" text. ``acceptEdits`` lets the
        # master auto-approve Edit/Write/MultiEdit only; Bash/외부 호출은 여전히
        # allow 패턴에 의존 → R12 fail-closed 정책 보존.
        args = [
            self.settings.master_cli_path,
            "-p",
            "--model", model,
            "--output-format", "json",
            "--no-session-persistence",
            "--permission-mode", "acceptEdits",
        ]
        if self.settings.master_cli_backend == "wsl_subprocess":
            inner = " ".join(shlex.quote(a) for a in args)
            return [
                "wsl", "-d", self.settings.wsl_distro,
                "bash", "-lc", inner,
            ]
        if self.settings.master_cli_backend == "local_subprocess":
            return args
        raise ClaudeCodeAdapterError(
            f"Unsupported backend: {self.settings.master_cli_backend}"
        )

    @staticmethod
    def _build_stdin(*, prompt: str, history: list[dict[str, str]]) -> str:
        """Flatten history + new prompt into a single stdin payload.

        Claude Code in -p mode takes the full prompt from stdin. We include
        recent turns as plain-text context, not via --resume (stateless).
        """
        if not history:
            return prompt
        lines = []
        for turn in history:
            role = turn.get("role", "user")
            content = turn.get("content", "")
            if not content:
                continue
            lines.append(f"[{role}]\n{content}")
        lines.append(f"[user]\n{prompt}")
        return "\n\n".join(lines)


__all__ = [
    "ClaudeCodeAdapter",
    "ClaudeCodeAdapterError",
    "ClaudeCodeAuthError",
    "ClaudeCodeResult",
    "ClaudeCodeTimeout",
]
