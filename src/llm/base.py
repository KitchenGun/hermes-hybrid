"""LLM client protocol + typed error hierarchy.

Per design doc: LLM is an execution engine. Tool calling happens via Hermes.

Error hierarchy (R18):
  LLMError                     — generic
   ├─ LLMTimeoutError          — request timed out
   ├─ LLMRateLimitError        — 429
   ├─ LLMAuthError             — 401/403
   ├─ LLMConnectionError       — network / DNS / refused
   └─ LLMServerError           — 5xx / unknown upstream fault
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol


class LLMError(RuntimeError):
    """Base LLM failure. Subclass to convey category to the Validator."""


class LLMTimeoutError(LLMError):
    pass


class LLMRateLimitError(LLMError):
    pass


class LLMAuthError(LLMError):
    pass


class LLMConnectionError(LLMError):
    pass


class LLMServerError(LLMError):
    pass


@dataclass
class LLMResponse:
    text: str
    model: str
    prompt_tokens: int = 0
    completion_tokens: int = 0


class LLMClient(Protocol):
    name: str
    model: str

    async def generate(
        self,
        messages: list[dict[str, str]],
        *,
        max_tokens: int = 2048,
        temperature: float = 0.2,
    ) -> LLMResponse: ...
