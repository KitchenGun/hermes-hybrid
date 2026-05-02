"""ActionRunner — translate LLM action JSON into actual tool execution.

The Job Factory v2 contract (per design doc):

  > LLM is a planner, not an executor. It outputs a structured ``action``
  > JSON describing what to do. A separate Runner inspects the action,
  > checks it against a tool registry + per-job allowlist, and runs it.

The action JSON shape (the LLM is prompted to produce this):
    {
      "thought": "<reasoning, optional>",
      "action": {"tool": "<tool_name>", "args": {<kwargs>}},
      "response_to_user": "<final reply, optional>"
    }

If the LLM produces a plain text response (no JSON), the Runner returns
``ToolResult.respond_only`` so the dispatcher just sends the text back.
This is the common path for ``simple_chat`` / ``summarize`` jobs.

Security model:
  * Each tool is a coroutine registered in :class:`ToolRegistry`.
  * A tool name in the action MUST be either:
      - present in :data:`ALWAYS_ALLOWED_TOOLS`, or
      - present in the current job's ``required_tools`` list.
    Otherwise → :class:`ToolResult.denied`.
  * Tools never see raw LLM text directly — args are validated upstream.
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any, Literal

log = logging.getLogger(__name__)


# ---- shape ----------------------------------------------------------------


# Always-allowed tools — execute regardless of job_type. Right now only
# ``respond_to_user`` (a no-op that signals "no tool, just talk").
ALWAYS_ALLOWED_TOOLS: frozenset[str] = frozenset({"respond_to_user"})


ToolStatus = Literal["ok", "error", "denied", "respond_only", "no_action"]


@dataclass(frozen=True)
class ToolCall:
    """Parsed action description from LLM output."""

    tool: str
    args: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class ToolResult:
    """Outcome of executing one action.

    Attributes:
        status: ok / error / denied / respond_only / no_action.
        tool: The tool we tried to invoke (or "" if none).
        output: Tool's return value on success. Empty/error info on
            failure. For ``respond_only``, this is the user-facing text.
        error: One-line error description for non-ok statuses. Empty
            otherwise.
    """

    status: ToolStatus
    tool: str = ""
    output: Any = None
    error: str = ""

    @classmethod
    def ok(cls, *, tool: str, output: Any = None) -> "ToolResult":
        return cls(status="ok", tool=tool, output=output)

    @classmethod
    def error(cls, *, tool: str, error: str) -> "ToolResult":
        return cls(status="error", tool=tool, error=error)

    @classmethod
    def denied(cls, *, tool: str, error: str) -> "ToolResult":
        return cls(status="denied", tool=tool, error=error)

    @classmethod
    def respond_only(cls, *, output: str) -> "ToolResult":
        """LLM produced plain text without an action — pass it back as-is."""
        return cls(status="respond_only", tool="", output=output)

    @classmethod
    def no_action(cls, *, error: str = "") -> "ToolResult":
        """LLM output looked structured but had no usable action."""
        return cls(status="no_action", tool="", error=error)


# ---- Tool registry --------------------------------------------------------


# A tool is just an async callable taking a kwargs dict and returning
# anything JSON-serializable. The Runner doesn't enforce a schema — that's
# the tool's responsibility.
ToolFn = Callable[[dict[str, Any]], Awaitable[Any]]


class ToolRegistry:
    """Map tool_name → async callable.

    Concurrency: the registry itself isn't locked because tools are
    typically registered at startup. Tool *invocations* are concurrent —
    the tool implementations are responsible for their own thread/IO
    safety.
    """

    def __init__(self):
        self._tools: dict[str, ToolFn] = {}

    def register(self, name: str, fn: ToolFn) -> None:
        if not name or not isinstance(name, str):
            raise ValueError("tool name must be a non-empty string")
        if name in self._tools:
            raise ValueError(f"tool already registered: {name!r}")
        self._tools[name] = fn

    def has(self, name: str) -> bool:
        return name in self._tools

    def get(self, name: str) -> ToolFn | None:
        return self._tools.get(name)

    def names(self) -> list[str]:
        return sorted(self._tools.keys())


# ---- Action parsing -------------------------------------------------------


_CODE_FENCE_RE = re.compile(
    r"^\s*```(?:json)?\s*(?P<body>.*?)\s*```\s*$",
    re.DOTALL,
)


@dataclass(frozen=True)
class ParsedAction:
    """Structured form of LLM output for the Runner to consume.

    Either:
      * ``call`` is set ⇒ act on it; ``response`` is the optional
        accompanying user-visible text.
      * ``call`` is None and ``response`` is set ⇒ treat as plain reply.
      * Both None ⇒ unparseable / empty.
    """

    call: ToolCall | None
    response: str = ""
    raw: str = ""


def parse_llm_output(text: str) -> ParsedAction:
    """Decode LLM output into ParsedAction.

    Tolerates:
      * Plain text (no JSON at all) → returned as ``response``.
      * Code-fence-wrapped JSON (``\\`\\`\\`json {…} \\`\\`\\```).
      * JSON missing optional fields.

    Returns ParsedAction with both fields None when the input is empty
    or completely unparseable.
    """
    if not text or not text.strip():
        return ParsedAction(call=None, response="", raw=text or "")

    cleaned = text.strip()
    m = _CODE_FENCE_RE.match(cleaned)
    body = m.group("body").strip() if m else cleaned

    # Try JSON.
    if body.startswith("{") and body.endswith("}"):
        try:
            obj = json.loads(body)
        except json.JSONDecodeError:
            obj = None
    else:
        obj = None

    if obj is None or not isinstance(obj, dict):
        # Plain text response.
        return ParsedAction(call=None, response=cleaned, raw=text)

    response = str(obj.get("response_to_user") or "").strip()
    action_raw = obj.get("action")

    call: ToolCall | None = None
    if isinstance(action_raw, dict):
        tool = action_raw.get("tool")
        args = action_raw.get("args", {})
        if isinstance(tool, str) and tool and isinstance(args, dict):
            call = ToolCall(tool=tool, args=dict(args))

    return ParsedAction(call=call, response=response, raw=text)


# ---- ActionRunner ---------------------------------------------------------


class ActionRunner:
    """Execute parsed actions against a ToolRegistry, with per-job ACL.

    Args:
        registry: The tool registry — typically constructed at startup
            and shared by every dispatcher invocation.
    """

    def __init__(self, registry: ToolRegistry):
        self._registry = registry

    async def execute(
        self,
        llm_output: str,
        *,
        job_required_tools: tuple[str, ...] = (),
        timeout_s: float | None = 30.0,
    ) -> ToolResult:
        """Decode + run + return.

        Args:
            llm_output: Raw text from the LLM.
            job_required_tools: ACL whitelist for this job_type. Tools
                in :data:`ALWAYS_ALLOWED_TOOLS` are always usable.
            timeout_s: Per-tool timeout. ``None`` = no timeout.
        """
        parsed = parse_llm_output(llm_output)

        if parsed.call is None:
            if parsed.response:
                return ToolResult.respond_only(output=parsed.response)
            return ToolResult.no_action(error="empty or unparseable LLM output")

        return await self._invoke(
            parsed.call,
            allowlist=set(job_required_tools),
            timeout_s=timeout_s,
        )

    async def _invoke(
        self,
        call: ToolCall,
        *,
        allowlist: set[str],
        timeout_s: float | None,
    ) -> ToolResult:
        # ACL check.
        allowed = (
            call.tool in ALWAYS_ALLOWED_TOOLS or call.tool in allowlist
        )
        if not allowed:
            return ToolResult.denied(
                tool=call.tool,
                error=(
                    f"tool {call.tool!r} not in job's allowed tools "
                    f"({sorted(allowlist) or 'none'})"
                ),
            )

        # respond_to_user is a no-op recognized by the dispatcher.
        if call.tool == "respond_to_user":
            text = str(call.args.get("text", "")).strip()
            return ToolResult.respond_only(output=text)

        fn = self._registry.get(call.tool)
        if fn is None:
            return ToolResult.error(
                tool=call.tool,
                error=f"tool {call.tool!r} not registered",
            )

        try:
            if timeout_s is None:
                output = await fn(call.args)
            else:
                output = await asyncio.wait_for(fn(call.args), timeout_s)
        except asyncio.TimeoutError:
            return ToolResult.error(
                tool=call.tool,
                error=f"tool {call.tool!r} timed out after {timeout_s}s",
            )
        except Exception as e:  # noqa: BLE001
            return ToolResult.error(
                tool=call.tool,
                error=f"{type(e).__name__}: {e}",
            )

        return ToolResult.ok(tool=call.tool, output=output)
