"""Telegram gateway — Phase 5 MVP.

Long-polling Telegram client that hands every allowed user message to
the same Orchestrator that the Discord gateway uses. Stdlib-only
(``urllib`` + ``json``) — no python-telegram-bot dependency, so this
runs on the same Python install as the rest of hermes-hybrid.

Scope (intentional limits):
  * Text messages only (no media / inline / keyboard / file uploads)
  * Long-polling (``getUpdates`` with ``timeout``); no webhook support
  * No persistent state — last update_id is tracked in memory; restart
    re-reads the most recent ~24h of updates (Telegram's default window)
  * Allowlist (``telegram_allowed_user_ids``) is fail-closed when set;
    empty allowlist + ``require_allowlist=True`` rejects everyone

The class is async-driven so the rest of the bot's asyncio runtime
shares its loop — same shape as ``DiscordBot``.
"""
from __future__ import annotations

import asyncio
import json
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

from src.config import Settings
from src.obs import get_logger
from src.orchestrator.orchestrator import Orchestrator

log = get_logger(__name__)

_API = "https://api.telegram.org"
_DISCORD_CHUNK = 4000  # Telegram caps text messages at 4096; leave a margin


class TelegramAPIError(RuntimeError):
    """Raised when Telegram returns ``ok: false`` or a non-2xx response."""


class TelegramBot:
    """Minimal long-polling Telegram client.

    Construction does NOT make any HTTP calls — that lets tests build a
    bot, swap out ``_get_updates`` / ``_send_message`` for stubs, and
    drive ``handle_update`` directly.
    """

    def __init__(
        self,
        settings: Settings,
        orchestrator: Orchestrator,
        *,
        api_base: str = _API,
        long_poll_timeout: int = 25,
    ):
        self.settings = settings
        self.orchestrator = orchestrator
        self.api_base = api_base.rstrip("/")
        self.long_poll_timeout = long_poll_timeout
        self._token = settings.telegram_bot_token
        self._allowlist = settings.telegram_allowlist
        self._last_update_id: int | None = None
        self._stopping = False

    # ---- public --------------------------------------------------------

    async def run(self) -> None:
        """Long-poll until ``stop()`` is called."""
        if not self._token:
            raise RuntimeError(
                "telegram_bot_token is empty — set TELEGRAM_BOT_TOKEN in .env"
            )
        log.info("telegram.bot_starting", allowlist_size=len(self._allowlist))
        while not self._stopping:
            try:
                updates = await self._get_updates()
            except Exception as e:  # noqa: BLE001
                log.warning("telegram.poll_failed", err=str(e))
                await asyncio.sleep(5)
                continue
            for upd in updates:
                try:
                    await self.handle_update(upd)
                except Exception as e:  # noqa: BLE001
                    log.warning(
                        "telegram.update_handler_failed",
                        err=str(e),
                        update_id=upd.get("update_id"),
                    )

    def stop(self) -> None:
        self._stopping = True

    # ---- update handling ----------------------------------------------

    async def handle_update(self, update: dict[str, Any]) -> None:
        """Single Telegram update → optional Orchestrator dispatch.

        Updates the last-seen ID even if we ignore the message — so we
        don't re-fetch the same garbage on the next poll.
        """
        update_id = update.get("update_id")
        if isinstance(update_id, int):
            if self._last_update_id is None or update_id > self._last_update_id:
                self._last_update_id = update_id

        msg = update.get("message") or update.get("edited_message")
        if not msg:
            return

        text = msg.get("text")
        if not isinstance(text, str) or not text.strip():
            return

        from_user = msg.get("from") or {}
        user_id = from_user.get("id")
        chat = msg.get("chat") or {}
        chat_id = chat.get("id")

        if not isinstance(user_id, int) or not isinstance(chat_id, int):
            return

        # Allowlist check — fail-closed when require_allowlist is on.
        if self.settings.require_allowlist:
            if not self._allowlist or user_id not in self._allowlist:
                log.warning(
                    "telegram.user_not_allowed",
                    user_id=user_id,
                    chat_id=chat_id,
                )
                return

        log.info(
            "telegram.message_received",
            user_id=user_id,
            chat_id=chat_id,
            text_len=len(text),
        )

        # Hand off to the same Orchestrator the Discord gateway uses.
        result = await self.orchestrator.handle(
            text,
            user_id=str(user_id),
            history=None,
            heavy=text.lstrip().startswith("!heavy"),
        )

        # Send the response back, chunking if needed.
        await self._send_chunks(chat_id, result.response)

    # ---- HTTP layer (mockable) ----------------------------------------

    async def _get_updates(self) -> list[dict[str, Any]]:
        """Block on Telegram long-poll up to ``long_poll_timeout`` seconds.

        Returned list is empty when no new updates — totally normal.
        """
        params: dict[str, Any] = {"timeout": self.long_poll_timeout}
        if self._last_update_id is not None:
            params["offset"] = self._last_update_id + 1
        return await asyncio.to_thread(
            self._call, "getUpdates", params, self.long_poll_timeout + 5
        )

    async def _send_message(self, chat_id: int, text: str) -> None:
        await asyncio.to_thread(
            self._call,
            "sendMessage",
            {"chat_id": chat_id, "text": text},
            10,
        )

    async def _send_chunks(self, chat_id: int, text: str) -> None:
        if not text:
            return
        for i in range(0, len(text), _DISCORD_CHUNK):
            chunk = text[i : i + _DISCORD_CHUNK]
            try:
                await self._send_message(chat_id, chunk)
            except TelegramAPIError as e:
                log.warning(
                    "telegram.send_failed", err=str(e), chat_id=chat_id
                )
                break

    # ---- raw call ------------------------------------------------------

    def _call(
        self,
        method: str,
        params: dict[str, Any],
        timeout_s: int,
    ) -> Any:
        url = f"{self.api_base}/bot{self._token}/{method}"
        body = json.dumps(params).encode("utf-8")
        req = urllib.request.Request(
            url,
            data=body,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=timeout_s) as resp:
                payload = json.loads(resp.read().decode("utf-8"))
        except (urllib.error.URLError, OSError, ValueError) as e:
            raise TelegramAPIError(f"{method} HTTP error: {e}") from e

        if not payload.get("ok"):
            raise TelegramAPIError(
                f"{method} returned ok=false: {payload.get('description', '')}"
            )
        return payload.get("result")


__all__ = ["TelegramBot", "TelegramAPIError"]
