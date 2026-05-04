"""Live Ollama smoke tests — skipped unless OLLAMA_LIVE=1.

These tests hit a real local Ollama server, so CI and offline dev skip them.
Run with:
    set OLLAMA_LIVE=1 && python -m pytest tests/test_ollama_live.py -v
"""
from __future__ import annotations

import json
import os

import pytest

from src.config import Settings
from src.llm.ollama_client import OllamaClient, list_ollama_models

pytestmark = pytest.mark.skipif(
    os.environ.get("OLLAMA_LIVE") != "1",
    reason="Set OLLAMA_LIVE=1 to run live Ollama tests",
)


@pytest.fixture
def live_settings() -> Settings:
    """Use real defaults — hits localhost:11434."""
    return Settings(
        _env_file=None,  # type: ignore[call-arg]
        discord_bot_token="", discord_allowed_user_ids="",
        require_allowlist=False,
        ollama_enabled=True,
    )


@pytest.mark.asyncio
async def test_server_reachable(live_settings: Settings):
    models = await list_ollama_models(live_settings.ollama_base_url)
    assert isinstance(models, list)


@pytest.mark.asyncio
async def test_required_models_installed(live_settings: Settings):
    models = await list_ollama_models(live_settings.ollama_base_url)
    required = [
        live_settings.ollama_router_model,
        live_settings.ollama_work_model,
        live_settings.ollama_worker_model,
    ]
    missing = [m for m in required if m not in models]
    assert not missing, f"pull these first: {missing}"


@pytest.mark.asyncio
async def test_router_7b_generates_json(live_settings: Settings):
    """7B must produce parseable routing JSON. This is the gate for whether
    the 7B refinement actually helps — if it can't follow instructions, we
    stick with the heuristic."""
    c = OllamaClient(
        live_settings.ollama_base_url,
        live_settings.ollama_router_model,
        keep_alive="1m",
    )
    msg = (
        "You are a routing classifier. Respond with ONLY a compact JSON object "
        '{"route":"local|worker|cloud","confidence":0.0-1.0,'
        '"requires_planning":true|false,"reason":"<=60 chars"}'
    )
    resp = await c.generate(
        [
            {"role": "system", "content": msg},
            {"role": "user", "content": "hi there"},
        ],
        max_tokens=128, temperature=0.0,
    )
    assert resp.text
    # Must be parseable as JSON (allow optional surrounding prose)
    start = resp.text.find("{")
    end = resp.text.rfind("}")
    assert start != -1 and end > start, f"no JSON block in: {resp.text!r}"
    obj = json.loads(resp.text[start : end + 1])
    assert obj["route"] in ("local", "worker", "cloud")


@pytest.mark.asyncio
async def test_work_14b_generates_text(live_settings: Settings):
    c = OllamaClient(
        live_settings.ollama_base_url,
        live_settings.ollama_work_model,
        keep_alive="1m",
        request_timeout=180.0,
    )
    resp = await c.generate(
        [{"role": "user", "content": "Say 'hello world' and nothing else."}],
        max_tokens=32, temperature=0.0,
    )
    assert resp.text
    assert "hello" in resp.text.lower()


@pytest.mark.asyncio
async def test_worker_32b_generates_code(live_settings: Settings):
    c = OllamaClient(
        live_settings.ollama_base_url,
        live_settings.ollama_worker_model,
        keep_alive="1m",
        request_timeout=300.0,  # 32B first-token latency can be high
    )
    resp = await c.generate(
        [{"role": "user", "content": "Write a Python one-liner that returns 2+2. Code only, no prose."}],
        max_tokens=64, temperature=0.0,
    )
    assert resp.text
    # Very loose check — the point is just that the 32B produced non-empty output.
    assert len(resp.text) > 0
