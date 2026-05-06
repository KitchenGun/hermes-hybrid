"""Tests for the Telegram MVP gateway (Phase 5).

We mock the Orchestrator and the HTTP layer entirely — the contract
under test is:
  * non-text updates are skipped without crashing
  * allowlist enforcement (fail-closed when require_allowlist=True)
  * text messages are forwarded to orchestrator.handle exactly once
  * the orchestrator's response is sent back, chunked for >4000 chars
  * !heavy prefix flips the heavy flag on the orchestrator call
  * last_update_id advances even when the message is ignored (so we
    don't re-fetch the same garbage on the next poll)
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import pytest

from src.config import Settings
from src.gateway.telegram_bot import TelegramBot


@dataclass
class _OrchestratorResult:
    response: str
    handled_by: str = "stub"
    task: Any = None


class _StubOrchestrator:
    def __init__(self, response_text: str = "ok"):
        self.calls: list[dict[str, Any]] = []
        self.response_text = response_text

    async def handle(
        self,
        user_message: str,
        *,
        user_id: str,
        history: Any = None,
        heavy: bool = False,
        forced_profile: str | None = None,
    ) -> _OrchestratorResult:
        self.calls.append(
            {
                "user_message": user_message,
                "user_id": user_id,
                "heavy": heavy,
                "forced_profile": forced_profile,
            }
        )
        return _OrchestratorResult(response=self.response_text)


def _make_bot(
    settings_kwargs: dict[str, Any] | None = None,
    *,
    response: str = "ok",
) -> tuple[TelegramBot, _StubOrchestrator, list[dict[str, Any]]]:
    """Builder for tests: returns (bot, orchestrator, sent_messages)."""
    base_kwargs = {
        "_env_file": None,
        "discord_bot_token": "",
        "discord_allowed_user_ids": "",
        "require_allowlist": False,
        "ollama_enabled": False,
        "telegram_bot_token": "stub-token",
        "telegram_allowed_user_ids": "100,200",
        "experience_log_enabled": False,
    }
    if settings_kwargs:
        base_kwargs.update(settings_kwargs)
    settings = Settings(**base_kwargs)  # type: ignore[arg-type]

    orch = _StubOrchestrator(response_text=response)
    bot = TelegramBot(settings, orch)  # type: ignore[arg-type]

    sent: list[dict[str, Any]] = []

    async def _stub_send(chat_id: int, text: str) -> None:
        sent.append({"chat_id": chat_id, "text": text})

    bot._send_message = _stub_send  # type: ignore[method-assign]
    return bot, orch, sent


@pytest.mark.asyncio
async def test_text_message_dispatches_to_orchestrator():
    bot, orch, sent = _make_bot({"require_allowlist": True})
    update = {
        "update_id": 7,
        "message": {
            "from": {"id": 100},
            "chat": {"id": 555},
            "text": "안녕",
        },
    }
    await bot.handle_update(update)
    assert len(orch.calls) == 1
    assert orch.calls[0]["user_message"] == "안녕"
    assert orch.calls[0]["user_id"] == "100"
    assert sent == [{"chat_id": 555, "text": "ok"}]


@pytest.mark.asyncio
async def test_allowlist_rejects_non_listed_user():
    bot, orch, sent = _make_bot({"require_allowlist": True})
    update = {
        "update_id": 7,
        "message": {
            "from": {"id": 999},          # not in allowlist
            "chat": {"id": 555},
            "text": "hi",
        },
    }
    await bot.handle_update(update)
    assert orch.calls == []
    assert sent == []


@pytest.mark.asyncio
async def test_allowlist_disabled_admits_anyone():
    bot, orch, _sent = _make_bot({"require_allowlist": False})
    update = {
        "update_id": 7,
        "message": {
            "from": {"id": 999},
            "chat": {"id": 555},
            "text": "hi",
        },
    }
    await bot.handle_update(update)
    assert len(orch.calls) == 1


@pytest.mark.asyncio
async def test_non_text_message_is_skipped():
    bot, orch, _sent = _make_bot()
    update = {
        "update_id": 8,
        "message": {
            "from": {"id": 100},
            "chat": {"id": 555},
            # no "text" key — typical photo/voice update
            "photo": [{"file_id": "abc"}],
        },
    }
    await bot.handle_update(update)
    assert orch.calls == []


@pytest.mark.asyncio
async def test_last_update_id_advances_even_on_skip():
    bot, _orch, _sent = _make_bot()
    await bot.handle_update({"update_id": 42})  # no message at all
    assert bot._last_update_id == 42

    # New higher update with non-text content also advances the cursor.
    await bot.handle_update(
        {"update_id": 43, "message": {"chat": {"id": 1}, "from": {"id": 1}}}
    )
    assert bot._last_update_id == 43


@pytest.mark.asyncio
async def test_heavy_prefix_sets_heavy_flag():
    bot, orch, _sent = _make_bot({"require_allowlist": False})
    update = {
        "update_id": 1,
        "message": {
            "from": {"id": 100},
            "chat": {"id": 555},
            "text": "!heavy 복잡한 작업",
        },
    }
    await bot.handle_update(update)
    assert orch.calls[0]["heavy"] is True


@pytest.mark.asyncio
async def test_long_response_is_chunked():
    big = "x" * 10_000
    bot, _orch, sent = _make_bot({"require_allowlist": False}, response=big)
    update = {
        "update_id": 1,
        "message": {
            "from": {"id": 100},
            "chat": {"id": 555},
            "text": "give me 10k",
        },
    }
    await bot.handle_update(update)
    assert len(sent) >= 2
    assert "".join(s["text"] for s in sent) == big
