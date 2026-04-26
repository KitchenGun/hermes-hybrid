"""Rule Layer: deterministic short-circuit for confirmed patterns.

Handles exact commands (/help, /status, /retry, /cancel, /ping)
and trivial canned responses without any LLM call.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Callable


@dataclass(frozen=True)
class RuleMatch:
    handler: str                 # e.g. "help", "status"
    args: dict[str, str]
    response: str | None = None  # pre-computed response if static


HELP_TEXT = (
    "**hermes-hybrid** commands:\n"
    "`/help` — show this help\n"
    "`/status <task_id>` — task status\n"
    "`/retry <task_id>` — retry a failed task\n"
    "`/cancel <task_id>` — cancel in-flight task\n"
    "`/confirm <task_id> yes|no` — text fallback for the HITL button\n"
    "`/ping` — liveness check\n"
    "Any other text is routed through the orchestrator."
)


_STATIC_RULES: list[tuple[re.Pattern[str], Callable[[re.Match[str]], RuleMatch]]] = [
    (
        re.compile(r"^\s*/ping\s*$", re.IGNORECASE),
        lambda m: RuleMatch(handler="ping", args={}, response="pong"),
    ),
    (
        re.compile(r"^\s*/help\s*$", re.IGNORECASE),
        lambda m: RuleMatch(handler="help", args={}, response=HELP_TEXT),
    ),
    (
        re.compile(r"^\s*/status\s+(?P<task_id>[\w\-]+)\s*$", re.IGNORECASE),
        lambda m: RuleMatch(handler="status", args={"task_id": m.group("task_id")}),
    ),
    (
        re.compile(r"^\s*/retry\s+(?P<task_id>[\w\-]+)\s*$", re.IGNORECASE),
        lambda m: RuleMatch(handler="retry", args={"task_id": m.group("task_id")}),
    ),
    (
        re.compile(r"^\s*/cancel\s+(?P<task_id>[\w\-]+)\s*$", re.IGNORECASE),
        lambda m: RuleMatch(handler="cancel", args={"task_id": m.group("task_id")}),
    ),
    (
        # HITL text fallback — used when the button view expired or
        # message_id was lost (bot restart). Accepts yes/no/y/n.
        re.compile(
            r"^\s*/confirm\s+(?P<task_id>[\w\-]+)\s+(?P<decision>yes|no|y|n)\s*$",
            re.IGNORECASE,
        ),
        lambda m: RuleMatch(
            handler="confirm",
            args={
                "task_id": m.group("task_id"),
                "decision": (
                    "confirm"
                    if m.group("decision").lower() in ("yes", "y")
                    else "cancel"
                ),
            },
        ),
    ),
]


class RuleLayer:
    """Exact-pattern matcher. Returns a RuleMatch if the message is a known command."""

    def match(self, message: str) -> RuleMatch | None:
        for pattern, factory in _STATIC_RULES:
            m = pattern.match(message)
            if m:
                return factory(m)
        return None
