"""OpenAI client — used as C1 (cloud primary / buffer layer)."""
from __future__ import annotations

from openai import AsyncOpenAI

from .base import LLMAuthError, LLMResponse, classify_openai_exception


class OpenAIClient:
    name = "openai"

    def __init__(self, api_key: str, model: str = "gpt-4o"):
        if not api_key:
            raise LLMAuthError("OPENAI_API_KEY not set")
        self._client = AsyncOpenAI(api_key=api_key)
        self.model = model

    async def generate(
        self,
        messages: list[dict[str, str]],
        *,
        max_tokens: int = 2048,
        temperature: float = 0.2,
    ) -> LLMResponse:
        try:
            resp = await self._client.chat.completions.create(
                model=self.model,
                messages=messages,  # type: ignore[arg-type]
                max_tokens=max_tokens,
                temperature=temperature,
            )
        except Exception as e:  # noqa: BLE001
            raise classify_openai_exception(e) from e

        choice = resp.choices[0]
        text = (choice.message.content or "").strip()
        usage = resp.usage
        return LLMResponse(
            text=text,
            model=self.model,
            prompt_tokens=getattr(usage, "prompt_tokens", 0) if usage else 0,
            completion_tokens=getattr(usage, "completion_tokens", 0) if usage else 0,
        )
