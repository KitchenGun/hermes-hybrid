"""Adapter for the ``opencode`` CLI — Hermes Master 의 LLM lane.

Pattern mirrors :mod:`src.claude_adapter`: subprocess-driven, WSL or
local backend, JSON output parsing. The CLI is assumed to provide a
``-p`` (print) mode that takes the prompt on stdin and emits a single
line of JSON on stdout — same contract as ``claude -p``.

Why subprocess instead of an OpenAI API client:
  * the user's account uses ``opencode`` for $0 marginal access to
    GPT-5.5 (the API key path would charge per call)
  * keeps the auth surface in one place (``opencode auth login`` once,
    no ``OPENAI_API_KEY`` plumbing)
  * matches the ClaudeCodeAdapter mental model — heavy LLM lanes
    *always* go through a CLI

Scope (intentional limits, in line with the migration plan):
  * single-turn print mode (no ``--resume``, no MCP injection — the
    Hermes Master reconstructs context per call)
  * history is flattened into the prompt (CLI-agnostic)
  * concurrency is capped via ``settings.master_concurrency``
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


class OpenCodeAdapterError(RuntimeError):
    """Generic opencode CLI failure (non-zero exit, JSON parse, ...)."""


class OpenCodeTimeout(OpenCodeAdapterError):
    """The CLI exceeded ``master_timeout_ms``."""


class OpenCodeAuthError(OpenCodeAdapterError):
    """opencode reports auth / quota failure — user must re-auth."""


@dataclass
class OpenCodeResult:
    text: str
    model_name: str
    session_id: str | None = None
    duration_ms: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    total_cost_usd: float = 0.0
    raw: dict[str, Any] = field(default_factory=dict)


_AUTH_ERROR_RE = re.compile(
    r"(not (logged in|authenticated)|401|unauthorized|"
    r"please run\s+opencode\s+auth|expired token|invalid credentials)",
    re.IGNORECASE,
)
_QUOTA_RE = re.compile(
    r"(out of (credits|usage)|usage limit|rate limit.*reset|"
    r"quota exceeded|too many requests)",
    re.IGNORECASE,
)


class OpenCodeAdapter:
    """``opencode -p`` subprocess wrapper."""

    def __init__(
        self,
        settings: Settings,
        *,
        concurrency: int | None = None,
    ):
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
    ) -> OpenCodeResult:
        """Single-turn opencode call.

        ``history`` (if provided) is flattened into the prompt — opencode
        is invoked stateless to keep cost and behavior predictable.
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
                )
                try:
                    stdout_b, stderr_b = await asyncio.wait_for(
                        proc.communicate(stdin_payload.encode("utf-8")),
                        timeout=timeout_ms / 1000.0,
                    )
                except asyncio.TimeoutError:
                    proc.kill()
                    await proc.wait()
                    raise OpenCodeTimeout(
                        f"opencode timed out after {timeout_ms}ms"
                    )
            except FileNotFoundError as e:
                raise OpenCodeAdapterError(
                    f"opencode CLI not available: {e}"
                ) from e

        duration_ms = int(
            (asyncio.get_event_loop().time() - start) * 1000
        )
        stdout = stdout_b.decode("utf-8", errors="replace")
        stderr = stderr_b.decode("utf-8", errors="replace")

        if proc.returncode != 0:
            combined = stdout + "\n" + stderr
            if _AUTH_ERROR_RE.search(combined) or _QUOTA_RE.search(combined):
                raise OpenCodeAuthError(
                    f"opencode auth/quota failure: {combined[-300:]}"
                )
            raise OpenCodeAdapterError(
                f"opencode exited {proc.returncode}\n"
                f"stderr: {stderr[-500:]}"
            )

        try:
            data = json.loads(stdout.strip())
        except json.JSONDecodeError as e:
            raise OpenCodeAdapterError(
                f"opencode returned non-JSON: {stdout[:300]}"
            ) from e

        # ``is_error`` mirrors Claude Code's contract; opencode may or
        # may not emit it but we honor the field if present so an error
        # at the JSON layer doesn't silently produce empty text.
        if data.get("is_error"):
            msg = data.get("result") or data.get("subtype") or "unknown"
            if _AUTH_ERROR_RE.search(str(msg)) or _QUOTA_RE.search(str(msg)):
                raise OpenCodeAuthError(f"opencode auth/quota: {msg}")
            raise OpenCodeAdapterError(
                f"opencode reported is_error: {msg}"
            )

        text = str(data.get("result") or data.get("text") or "").strip()
        usage = data.get("usage") or {}
        model_usage = data.get("modelUsage") or {}
        model_name = (
            next(iter(model_usage.keys()), None)
            or data.get("model")
            or model
        )

        return OpenCodeResult(
            text=text,
            model_name=str(model_name),
            session_id=data.get("session_id"),
            duration_ms=duration_ms,
            input_tokens=int(usage.get("input_tokens", 0) or 0),
            output_tokens=int(usage.get("output_tokens", 0) or 0),
            total_cost_usd=float(data.get("total_cost_usd", 0.0) or 0.0),
            raw=data,
        )

    # ---- internals ----------------------------------------------------

    def _build_cmd(self, *, model: str) -> list[str]:
        args = [
            self.settings.opencode_cli_path,
            "-p",
            "--model", model,
            "--output-format", "json",
        ]
        if self.settings.opencode_cli_backend == "wsl_subprocess":
            inner = " ".join(shlex.quote(a) for a in args)
            return [
                "wsl",
                "-d", self.settings.wsl_distro,
                "bash", "-lc", inner,
            ]
        return args

    @staticmethod
    def _build_stdin(
        *, prompt: str, history: list[dict[str, str]]
    ) -> str:
        """Flatten history + prompt into a single stdin payload.

        opencode -p reads the user-turn payload from stdin. We prepend
        a transcript of prior turns as plain text so the model sees the
        context without needing the CLI to maintain a session.
        """
        if not history:
            return prompt
        lines: list[str] = []
        for m in history:
            role = m.get("role", "user")
            content = m.get("content", "")
            if not content:
                continue
            lines.append(f"[{role}]\n{content}")
        lines.append(f"[user]\n{prompt}")
        return "\n\n".join(lines)
