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


def classify_openai_exception(e: Exception) -> LLMError:
    """Map OpenAI/Anthropic SDK exceptions to our typed errors."""
    name = type(e).__name__.lower()
    msg = str(e)
    if "timeout" in name or "timeout" in msg.lower():
        return LLMTimeoutError(msg)
    if "ratelimit" in name or "429" in msg:
        return LLMRateLimitError(msg)
    if "authentic" in name or "401" in msg or "403" in msg or "unauthorized" in msg.lower():
        return LLMAuthError(msg)
    if "connect" in name or "apiconnection" in name:
        return LLMConnectionError(msg)
    if "internalserver" in name or "serviceunavailable" in name or "500" in msg or "502" in msg or "503" in msg:
        return LLMServerError(msg)
    return LLMError(msg)
