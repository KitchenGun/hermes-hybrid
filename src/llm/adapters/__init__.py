"""LLM adapter layer — Protocol over Ollama / Claude CLI.

Phase 11 (2026-05-06) 후 master 는 ``ClaudeCodeAdapter`` (`src/claude_
adapter/`) 만 사용. 이 디렉터리의 어댑터들은 bench harness
(``scripts/bench_local_models.py``) 가 모델 비교 측정 시 사용.

2026-05-04: OpenAI adapter removed (API legacy purged).
2026-05-06: HermesProfileAdapter removed (JobFactory v2 era + profile 폐기).
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
]
