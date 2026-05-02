"""OpenAI adapter — wraps :class:`OpenAIClient`.

Provider id: ``"openai"``. Model is the OpenAI model name (e.g.,
``"gpt-4o-mini"``, ``"gpt-4o"``).
"""
from __future__ import annotations

import asyncio
import time

from src.llm.adapters.base import (
    AdapterRequest,
    AdapterResponse,
    messages_to_dicts,
)
from src.llm.openai_client import OpenAIClient


class OpenAIAdapter:
    """Adapter around :class:`OpenAIClient` exposing the LLMAdapter Protocol."""

    def __init__(self, client: OpenAIClient):
        self._client = client

    @property
    def provider(self) -> str:
        return "openai"

    @property
    def model(self) -> str:
        return self._client.model

    async def generate(self, request: AdapterRequest) -> AdapterResponse:
        start = time.perf_counter()
        coro = self._client.generate(
            messages_to_dicts(request.messages),
            max_tokens=request.max_tokens,
            temperature=request.temperature,
        )
        if request.timeout_s is not None:
            resp = await asyncio.wait_for(coro, timeout=request.timeout_s)
        else:
            resp = await coro
        duration_ms = int((time.perf_counter() - start) * 1000)
        return AdapterResponse(
            text=resp.text,
            provider=self.provider,
            model=resp.model or self._client.model,
            prompt_tokens=resp.prompt_tokens,
            completion_tokens=resp.completion_tokens,
            duration_ms=duration_ms,
            raw=resp,
        )
