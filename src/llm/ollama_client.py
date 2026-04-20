"""Ollama client — local L2/L3 tier. Stub unless OLLAMA_ENABLED=true.

Uses the REST API at /api/chat. Kept dependency-light (httpx only) so that
the `ollama` extra is optional.
"""
from __future__ import annotations

import httpx

from .base import (
    LLMConnectionError,
    LLMError,
    LLMResponse,
    LLMServerError,
    LLMTimeoutError,
)


class OllamaClient:
    name = "ollama"

    def __init__(
        self,
        base_url: str,
        model: str,
        *,
        keep_alive: str = "10m",
        request_timeout: float = 120.0,
    ):
        self._base = base_url.rstrip("/")
        self.model = model
        # Hint Ollama to keep the model resident. Default 5m is too tight when
        # three models (7B/14B/32B) share a single GPU — shorter residence means
        # constant reload churn between router/work/worker calls.
        self._keep_alive = keep_alive
        self._timeout = request_timeout

    async def generate(
        self,
        messages: list[dict[str, str]],
        *,
        max_tokens: int = 2048,
        temperature: float = 0.2,
    ) -> LLMResponse:
        payload = {
            "model": self.model,
            "messages": messages,
            "stream": False,
            "keep_alive": self._keep_alive,
            "options": {
                "temperature": temperature,
                "num_predict": max_tokens,
            },
        }
        try:
            async with httpx.AsyncClient(timeout=self._timeout) as c:
                r = await c.post(f"{self._base}/api/chat", json=payload)
                r.raise_for_status()
                data = r.json()
        except httpx.TimeoutException as e:
            raise LLMTimeoutError(f"Ollama timeout: {e}") from e
        except httpx.ConnectError as e:
            raise LLMConnectionError(f"Ollama connect failed: {e}") from e
        except httpx.HTTPStatusError as e:
            raise LLMServerError(f"Ollama server error {e.response.status_code}") from e
        except Exception as e:  # noqa: BLE001
            raise LLMError(f"Ollama error: {e}") from e

        text = (data.get("message", {}) or {}).get("content", "").strip()
        return LLMResponse(
            text=text,
            model=self.model,
            prompt_tokens=int(data.get("prompt_eval_count", 0)),
            completion_tokens=int(data.get("eval_count", 0)),
        )


async def list_ollama_models(base_url: str, timeout: float = 5.0) -> list[str]:
    """Return the list of locally-available model names. Raises LLMConnectionError
    if the Ollama server is unreachable (preflight uses this to fail fast)."""
    try:
        async with httpx.AsyncClient(timeout=timeout) as c:
            r = await c.get(f"{base_url.rstrip('/')}/api/tags")
            r.raise_for_status()
            data = r.json()
    except httpx.ConnectError as e:
        raise LLMConnectionError(f"Ollama not reachable at {base_url}: {e}") from e
    except httpx.HTTPStatusError as e:
        raise LLMServerError(f"Ollama /api/tags returned {e.response.status_code}") from e
    except Exception as e:  # noqa: BLE001
        raise LLMError(f"Ollama tags fetch failed: {e}") from e

    return [m.get("name", "") for m in data.get("models", []) if m.get("name")]
