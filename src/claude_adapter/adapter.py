"""Adapter for Claude Code CLI — the ONLY path that calls Claude.

Invoked exclusively by the orchestrator's heavy path (`!heavy ...`). Uses
the user's Claude Max subscription via the Claude Code CLI's OAuth token
at `~/.claude/.credentials.json`, which means zero extra API cost.

Key design decisions:
  - `claude -p --output-format json --no-session-persistence` → reliable
    single-line JSON we can parse, no leftover session files.
  - Prompt piped via stdin (safer than CLI arg for long/quoted text).
  - Concurrency capped (Max subscription has session limits — avoid
    stampeding).
  - Not invoked by automatic tier escalation. Orchestrator never calls
    this from the validator-driven retry loop.
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
    """OAuth token expired / missing / Max subscription lapsed."""


class ClaudeCodeResumeFailed(ClaudeCodeAdapterError):
    """FIX#4: ``--resume <sid>`` pointed at a session that's gone.

    Happens when the previous session was evicted by the CLI's TTL, or was
    never actually persisted (e.g. prior call ran with
    ``--no-session-persistence``), or the session file got corrupted.
    The orchestrator catches this, invalidates the registry entry for the
    user, and retries without ``--resume`` so the user's request still
    goes through.
    """


@dataclass
class ClaudeCodeResult:
    text: str
    model_name: str
    session_id: str | None
    duration_ms: int
    input_tokens: int = 0
    output_tokens: int = 0
    total_cost_usd: float = 0.0
    raw: dict[str, Any] = field(default_factory=dict)


# Patterns to classify non-zero exit output.
_AUTH_ERROR_RE = re.compile(
    r"(not (logged in|authenticated)|oauth|401|unauthorized|expired|credentials)",
    re.IGNORECASE,
)
# "out of extra usage" / "out of credits" / hourly limit — treat as auth-ish
# (user needs to wait for Max reset or add credits; not a retryable transient).
_QUOTA_RE = re.compile(
    r"(out of extra usage|out of credits|usage limit|rate limit.*reset)",
    re.IGNORECASE,
)
# FIX#4: detect --resume pointing at a missing / evicted / malformed session.
_RESUME_FAIL_RE = re.compile(
    r"(session\s+(not\s+found|does\s+not\s+exist|missing|unknown)|"
    r"no\s+such\s+session|conversation\s+not\s+found|"
    r"invalid\s+session[_\s-]?id|cannot\s+resume)",
    re.IGNORECASE,
)


class ClaudeCodeAdapter:
    def __init__(self, settings: Settings):
        self.settings = settings
        self._sem = asyncio.Semaphore(max(1, settings.claude_code_concurrency))

    async def run(
        self,
        *,
        prompt: str,
        history: list[dict[str, str]] | None = None,
        model: str | None = None,
        timeout_ms: int | None = None,
        resume_session_id: str | None = None,
        persist_session: bool = False,
    ) -> ClaudeCodeResult:
        """Execute one Claude Code -p turn. Heavy path only.

        Stateless default: history is flattened into the prompt (no
        ``--resume``) and ``--no-session-persistence`` is set so each turn
        leaves no session file behind. Predictable cost, no churn.

        FIX#4 (session reuse): when the caller provides ``resume_session_id``
        we attach ``--resume <sid>`` and drop ``--no-session-persistence``
        so the CLI actually loads prior context. If the CLI reports the
        session is missing we raise :class:`ClaudeCodeResumeFailed` and the
        orchestrator falls back to a fresh run. ``persist_session=True`` on
        a first-turn call asks the CLI to keep the new session around for
        the registry to reuse later.
        """
        model = model or self.settings.claude_code_model
        timeout_ms = timeout_ms or self.settings.claude_code_timeout_ms
        cmd = self._build_cmd(
            model=model,
            resume_session_id=resume_session_id,
            persist_session=persist_session,
        )
        # When resuming, Claude Code already has the history — avoid re-sending
        # it (which would waste tokens and confuse the conversation).
        effective_history = [] if resume_session_id else (history or [])
        stdin_payload = self._build_stdin(prompt=prompt, history=effective_history)

        async with self._sem:
            start = asyncio.get_event_loop().time()
            try:
                proc = await asyncio.create_subprocess_exec(
                    *cmd,
                    stdin=asyncio.subprocess.PIPE,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
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
            # FIX#4: check resume-specific failures first so the orchestrator
            # can invalidate the registry entry and fall back to a fresh run
            # rather than surfacing a generic error to the user.
            if resume_session_id and _RESUME_FAIL_RE.search(combined):
                raise ClaudeCodeResumeFailed(
                    f"Claude Code could not resume session {resume_session_id!r}: "
                    f"{combined[-300:]}"
                )
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

        # `is_error=true` with subtype=error_during_execution etc.
        if data.get("is_error"):
            api_status = data.get("api_error_status")
            msg = data.get("result") or data.get("subtype") or "unknown"
            if resume_session_id and _RESUME_FAIL_RE.search(str(msg)):
                raise ClaudeCodeResumeFailed(
                    f"Claude Code could not resume session "
                    f"{resume_session_id!r}: {msg}"
                )
            if api_status in (401, 403) or _AUTH_ERROR_RE.search(str(msg)):
                raise ClaudeCodeAuthError(f"Claude Code auth error: {msg}")
            if _QUOTA_RE.search(str(msg)):
                raise ClaudeCodeAuthError(f"Claude Code quota error: {msg}")
            raise ClaudeCodeAdapterError(
                f"Claude Code reported is_error (api_status={api_status}): {msg}"
            )

        text = str(data.get("result") or "").strip()
        usage = data.get("usage") or {}
        # Pick the last model actually used (JSON has a modelUsage dict keyed by
        # model names; we prefer that over the `model` field on the result,
        # since Claude Code may route internally to sub-agents).
        model_usage = data.get("modelUsage") or {}
        model_name = next(iter(model_usage.keys()), model)

        return ClaudeCodeResult(
            text=text,
            model_name=model_name,
            session_id=data.get("session_id"),
            duration_ms=duration_ms,
            input_tokens=int(usage.get("input_tokens", 0) or 0),
            output_tokens=int(usage.get("output_tokens", 0) or 0),
            total_cost_usd=float(data.get("total_cost_usd", 0.0) or 0.0),
            raw=data,
        )

    # ---- internals ----

    def _build_cmd(
        self,
        *,
        model: str,
        resume_session_id: str | None = None,
        persist_session: bool = False,
    ) -> list[str]:
        args = [
            self.settings.claude_code_cli_path,
            "-p",
            "--model", model,
            "--output-format", "json",
        ]
        # FIX#4: when the caller wants to resume or save for future reuse,
        # we must NOT pass --no-session-persistence — it would either make
        # --resume fail immediately or silently drop the new session.
        if resume_session_id:
            args += ["--resume", resume_session_id]
        elif not persist_session:
            args.append("--no-session-persistence")

        if self.settings.claude_code_cli_backend == "wsl_subprocess":
            inner = " ".join(shlex.quote(a) for a in args)
            return [
                "wsl", "-d", self.settings.hermes_wsl_distro,
                "bash", "-lc", inner,
            ]
        if self.settings.claude_code_cli_backend == "local_subprocess":
            return args
        raise ClaudeCodeAdapterError(
            f"Unsupported backend: {self.settings.claude_code_cli_backend}"
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
            lines.append(f"[{role}] {content}")
        lines.append(f"[user] {prompt}")
        return "\n\n".join(lines)
