"""LLMAdapter Protocol + request/response dataclasses.

Phase 11 (2026-05-06): master 는 ``ClaudeCodeAdapter`` (`src/claude_
adapter/`) 만 사용. 이 모듈의 Protocol 은 bench harness
(``scripts/bench_local_models.py``) 의 Ollama / Claude CLI 비교 측정 시
유지. master 핫패스 가 아님.

Why a separate layer over the existing :class:`src.llm.base.LLMClient`?
  * ``LLMClient`` has no ``provider`` axis — needed by bench score keys.
  * Claude Code CLI takes ``query`` / ``prompt`` rather than messages;
    flattening logic belongs in the adapter, not in
    every caller.
  * ``AdapterResponse`` carries ``provider`` + ``duration_ms`` for ledger
    /selector observability.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal, Protocol, runtime_checkable

Role = Literal["system", "user", "assistant"]


@dataclass(frozen=True)
class ChatMessage:
    """One conversation turn. Frozen so the same instance can be safely
    reused across adapters."""

    role: Role
    content: str

    def to_dict(self) -> dict[str, str]:
        return {"role": self.role, "content": self.content}


@dataclass(frozen=True)
class AdapterRequest:
    """Normalized input for any LLMAdapter.

    Attributes:
        messages: Ordered conversation. Must contain at least one entry.
        max_tokens: Maximum completion tokens. Adapters MAY clamp lower.
        temperature: Sampling temperature (0.0–2.0).
        timeout_s: Per-request timeout. ``None`` defers to adapter default.
        extra: Provider-specific knobs (e.g., ``response_format`` for
            OpenAI JSON mode). Adapters consume what they understand.
    """

    messages: list[ChatMessage]
    max_tokens: int = 2048
    temperature: float = 0.2
    timeout_s: float | None = None
    extra: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.messages:
            raise ValueError("AdapterRequest.messages must be non-empty")
        if self.max_tokens <= 0:
            raise ValueError("max_tokens must be positive")
        if not 0.0 <= self.temperature <= 2.0:
            raise ValueError("temperature must be in [0.0, 2.0]")


@dataclass(frozen=True)
class AdapterResponse:
    """Normalized output from any LLMAdapter.

    Attributes:
        text: The model's reply (no role prefix, no system framing).
        provider: Stable provider identifier — "ollama" | "openai" |
            "claude_cli" | "hermes_profile". Used as the ScoreMatrix
            secondary key prefix.
        model: The exact model identifier the provider reports it used
            (e.g. "qwen2.5:14b-instruct", "gpt-4o-mini",
            "claude-sonnet-4", "hermes:journal_ops/gpt-4o-mini").
        prompt_tokens: Tokens consumed by the request, if reported.
        completion_tokens: Tokens generated, if reported.
        duration_ms: Wall-clock time of the call (adapter-side measure).
        raw: Untouched provider response — for debugging/ledger only.
    """

    text: str
    provider: str
    model: str
    prompt_tokens: int = 0
    completion_tokens: int = 0
    duration_ms: int = 0
    raw: Any = None

    @property
    def total_tokens(self) -> int:
        return self.prompt_tokens + self.completion_tokens


@runtime_checkable
class LLMAdapter(Protocol):
    """The single interface Job Factory v2 talks to.

    Concrete adapters MUST be safe to share across coroutines (i.e.,
    ``generate`` is reentrant) and MUST raise the ``LLMError`` hierarchy
    from :mod:`src.llm.base` for failure cases so the validator can
    classify them uniformly.
    """

    @property
    def provider(self) -> str:
        """Stable provider id: 'ollama' / 'openai' / 'claude_cli' /
        'hermes_profile'. Never includes model name."""
        ...

    @property
    def model(self) -> str:
        """Model identifier the adapter will use by default. Combined
        with ``provider`` it forms a unique ScoreMatrix key (matrix uses
        ``f"{provider}/{model}"`` to avoid collisions when two providers
        both expose a model named e.g. 'sonnet')."""
        ...

    async def generate(self, request: AdapterRequest) -> AdapterResponse:
        """Run one turn. Raises :class:`src.llm.base.LLMError` subclasses
        (timeout / rate-limit / auth / connection / server / generic)."""
        ...


# ---- helpers --------------------------------------------------------------


def messages_to_dicts(messages: list[ChatMessage]) -> list[dict[str, str]]:
    """Convert ChatMessage list to the dict format used by LLMClient.

    :class:`OllamaClient` accepts ``list[dict[str, str]]`` with the
    standard role/content keys, so adapters wrapping it just call this.
    """
    return [m.to_dict() for m in messages]


def flatten_to_prompt(messages: list[ChatMessage]) -> str:
    """Flatten messages into a single prompt string for adapters that
    don't speak chat (Hermes profile CLI, Claude Code CLI when not
    using its native history format).

    Format:
        System content goes first, no prefix.
        User/assistant turns are tagged with uppercase role:.
        Blank line between turns for legibility.

    The trailing turn is always the most recent user/assistant message,
    so callers can append additional context (e.g., bump prefixes) by
    prefixing the request before constructing AdapterRequest.
    """
    parts: list[str] = []
    for m in messages:
        if m.role == "system":
            parts.append(m.content)
        else:
            parts.append(f"{m.role.upper()}: {m.content}")
    return "\n\n".join(parts)
