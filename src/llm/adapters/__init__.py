"""LLM adapter layer — single Protocol over Ollama/Claude CLI/Hermes.

Job Factory v2 picks one of these per turn (driven by ScoreMatrix), and
they all expose ``async generate(request) -> AdapterResponse``. The
underlying clients (``OllamaClient``, ``ClaudeCodeAdapter``) are
unchanged — these adapters only normalize the request/response surface.

2026-05-04: OpenAI adapter removed when API legacy was purged. Cloud lane
is Claude CLI (Max OAuth) only.
"""
from src.llm.adapters.base import (
    AdapterRequest,
    AdapterResponse,
    ChatMessage,
    LLMAdapter,
    flatten_to_prompt,
    messages_to_dicts,
)
from src.llm.adapters.claude_cli import ClaudeCLIAdapter
from src.llm.adapters.hermes_profile import HermesProfileAdapter
from src.llm.adapters.ollama import OllamaAdapter

__all__ = [
    "AdapterRequest",
    "AdapterResponse",
    "ChatMessage",
    "LLMAdapter",
    "flatten_to_prompt",
    "messages_to_dicts",
    "OllamaAdapter",
    "ClaudeCLIAdapter",
    "HermesProfileAdapter",
]
