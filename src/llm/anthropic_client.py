"""Anthropic client — used as C2 (final fallback / heavy reasoning).

Per design doc §8: Claude is called ONLY as a last resort. Budget is enforced
at the Orchestrator level, but this client still accepts a per-call cap.
"""
from __future__ import annotations

from anthropic import AsyncAnthropic

from .base import LLMAuthError, LLMResponse, classify_openai_exception


class AnthropicClient:
    name = "anthropic"

    def __init__(self, api_key: str, model: str = "claude-opus-4-7"):
        if not api_key:
            raise LLMAuthError("ANTHROPIC_API_KEY not set")
        self._client = AsyncAnthropic(api_key=api_key)
        self.model = model

    async def generate(
        self,
        messages: list[dict[str, str]],
        *,
        max_tokens: int = 2048,
        temperature: float = 0.2,
    ) -> LLMResponse:
        system = ""
        normalized: list[dict[str, str]] = []
        for m in messages:
            if m["role"] == "system":
                system = (system + "\n" + m["content"]).strip()
            else:
                normalized.append({"role": m["role"], "content": m["content"]})

        try:
            resp = await self._client.messages.create(
                model=self.model,
                max_tokens=max_tokens,
                temperature=temperature,
                system=system or None,
                messages=normalized,  # type: ignore[arg-type]
            )
        except Exception as e:  # noqa: BLE001
            raise classify_openai_exception(e) from e

        text_parts = [block.text for block in resp.content if getattr(block, "type", "") == "text"]
        text = "\n".join(text_parts).strip()
        return LLMResponse(
            text=text,
            model=self.model,
            prompt_tokens=getattr(resp.usage, "input_tokens", 0),
            completion_tokens=getattr(resp.usage, "output_tokens", 0),
        )
