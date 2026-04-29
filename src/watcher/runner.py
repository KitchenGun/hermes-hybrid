"""Watcher runtime — long-running asyncio loop per watcher YAML.

Boundaries:
- Each watcher gets its own ``asyncio.Task``. Failures are isolated:
  one bad watcher does not stop the others.
- Per-account state lives in SQLite (``watcher_state`` table). On first
  run the provider returns recent INBOX items; we record the newest as
  the high-water mark and do NOT notify on it (avoiding a flood of
  "old mail" alerts when a watcher is freshly registered).
- Notifications go directly to the Discord webhook configured under
  ``delivery.target_env`` in the watcher YAML. We do not route through
  the orchestrator for mail alerts because the formatting is template,
  not LLM-driven; future LLM-driven watchers can grow that path.
"""
from __future__ import annotations

import asyncio
import fnmatch
import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from urllib import request as urlreq
from urllib.error import HTTPError, URLError

from src.hermes_adapter.adapter import HermesAdapterError
from src.obs import get_logger
from src.orchestrator.profile_loader import ProfileLoader, WatcherMeta
from src.skills.mail import PROVIDERS  # noqa: F401  (registers providers)
from src.skills.mail.accounts import AccountLoader, AccountConfig
from src.skills.mail.base import MailMessage, MailProvider, MailProviderError
from src.state.repository import Repository

log = get_logger(__name__)


_EMBED_COLOR_DEFAULT = 0x5865F2
_WEBHOOK_TIMEOUT_SEC = 10
_MIN_INTERVAL = 30
_KST = timezone(timedelta(hours=9))
_NO_NOTIFICATION_MARKER = "NO_NOTIFICATION"


class WatcherRunner:
    def __init__(
        self,
        settings: Any,
        repo: Repository,
        profile_loader: ProfileLoader,
        profiles_dir: Path,
        *,
        dm_dispatcher: Any = None,
        hermes: Any = None,
    ):
        self.settings = settings
        self.repo = repo
        self.profile_loader = profile_loader
        self.profiles_dir = Path(profiles_dir)
        self.dm_dispatcher = dm_dispatcher
        self.hermes = hermes
        self._tasks: dict[tuple[str, str], asyncio.Task[None]] = {}
        self._stop = asyncio.Event()

    async def start(self) -> None:
        """Spawn one asyncio task per watcher YAML found on disk."""
        watchers = self.profile_loader.iter_watchers()
        if not watchers:
            log.info("watcher_runner.no_watchers")
            return
        for meta in watchers:
            self._spawn(meta)
        log.info("watcher_runner.started", count=len(self._tasks))

    async def stop(self) -> None:
        self._stop.set()
        for key, task in list(self._tasks.items()):
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
        self._tasks.clear()

    def _spawn(self, meta: WatcherMeta) -> None:
        key = (meta.profile_id, meta.name)
        if key in self._tasks and not self._tasks[key].done():
            return
        interval = meta.interval_seconds or self.settings.watcher_default_interval_seconds
        if interval < _MIN_INTERVAL:
            log.warning(
                "watcher_runner.interval_too_low",
                profile=meta.profile_id,
                watcher=meta.name,
                requested=interval,
                clamped_to=_MIN_INTERVAL,
            )
            interval = _MIN_INTERVAL
        self._tasks[key] = asyncio.create_task(
            self._run_loop(meta, interval),
            name=f"watcher:{meta.profile_id}/{meta.name}",
        )

    async def _run_loop(self, meta: WatcherMeta, interval: int) -> None:
        log.info(
            "watcher_runner.loop_start",
            profile=meta.profile_id,
            watcher=meta.name,
            source=meta.source_type,
            interval=interval,
        )
        # Stagger first run slightly to avoid synchronized wake.
        await self._sleep_or_stop(min(15, interval))
        while not self._stop.is_set():
            try:
                await self._tick(meta)
            except Exception as e:  # noqa: BLE001
                log.error(
                    "watcher_runner.tick_failed",
                    profile=meta.profile_id,
                    watcher=meta.name,
                    err=str(e),
                )
            await self._sleep_or_stop(interval)

    async def _sleep_or_stop(self, seconds: int) -> None:
        try:
            await asyncio.wait_for(self._stop.wait(), timeout=seconds)
        except asyncio.TimeoutError:
            return

    async def _tick(self, meta: WatcherMeta) -> None:
        if meta.source_type == "mail_poll":
            await self._tick_mail(meta)
            return
        # Calendar watchers — yaml's source values are kept as the original
        # design intent (internal event vs. push) but until those upstream
        # mechanisms exist, we fall back to polling Hermes which uses the
        # google_calendar MCP under the calendar_ops profile.
        if meta.source_type == "internal.calendar_write_completed":
            await self._tick_calendar_via_hermes(meta, mode="conflicts")
            return
        if meta.source_type == "google_calendar.push_notification":
            await self._tick_calendar_via_hermes(meta, mode="invitations")
            return
        log.info(
            "watcher_runner.unsupported_source",
            profile=meta.profile_id,
            watcher=meta.name,
            source=meta.source_type,
        )

    async def _tick_mail(self, meta: WatcherMeta) -> None:
        account_names = list(meta.source.get("accounts") or [])
        if not account_names:
            log.warning(
                "watcher_runner.mail_no_accounts",
                profile=meta.profile_id,
                watcher=meta.name,
            )
            return
        loader = AccountLoader(self.profiles_dir / meta.profile_id)
        try:
            all_accounts = loader.load()
        except Exception as e:  # noqa: BLE001
            log.error(
                "watcher_runner.accounts_load_failed",
                profile=meta.profile_id,
                err=str(e),
            )
            return

        # Concurrent per-account polling with isolated error handling.
        # One IMAP timeout doesn't delay other accounts (Temporal-style
        # actor-per-mailbox pattern, scaled down to asyncio tasks).
        tasks = []
        names_in_order: list[str] = []
        for name in account_names:
            cfg = all_accounts.get(name)
            if cfg is None:
                log.warning(
                    "watcher_runner.account_missing",
                    profile=meta.profile_id,
                    watcher=meta.name,
                    account=name,
                )
                continue
            tasks.append(self._poll_account(meta, loader, cfg))
            names_in_order.append(name)

        if not tasks:
            return

        results = await asyncio.gather(*tasks, return_exceptions=True)

        new_items: list[MailMessage] = []
        for name, result in zip(names_in_order, results):
            if isinstance(result, MailProviderError):
                log.warning(
                    "watcher_runner.provider_failed",
                    profile=meta.profile_id,
                    watcher=meta.name,
                    account=name,
                    err=str(result),
                )
                continue
            if isinstance(result, BaseException):
                log.error(
                    "watcher_runner.provider_crashed",
                    profile=meta.profile_id,
                    watcher=meta.name,
                    account=name,
                    err=str(result),
                )
                continue
            new_items.extend(result)

        if not new_items:
            return
        await self._notify(meta, new_items)

    async def _poll_account(
        self,
        meta: WatcherMeta,
        loader: AccountLoader,
        cfg: AccountConfig,
    ) -> list[MailMessage]:
        provider: MailProvider = loader.build(cfg)
        last_id = await self.repo.get_watcher_state(
            meta.profile_id, meta.name, account=cfg.name
        )
        # Run blocking provider call off the event loop so IMAP/network
        # IO doesn't stall other watchers.
        items = await asyncio.to_thread(
            provider.list_new_since,
            last_id,
            limit=20,
        )
        if not items:
            return []
        # First-run seeding: when we had no last_id, treat this batch as
        # the high-water mark and don't notify — otherwise the bot will
        # spam "you have 20 old emails" right after registration.
        newest_id = items[0].message_id
        if last_id is None:
            await self.repo.update_watcher_state(
                meta.profile_id, meta.name, newest_id, account=cfg.name
            )
            log.info(
                "watcher_runner.seeded",
                profile=meta.profile_id,
                watcher=meta.name,
                account=cfg.name,
                high_water=newest_id,
            )
            return []
        await self.repo.update_watcher_state(
            meta.profile_id, meta.name, newest_id, account=cfg.name
        )
        return _apply_filter(items, meta.source.get("filter") or {})

    async def _notify(self, meta: WatcherMeta, items: list[MailMessage]) -> None:
        target_env = str(meta.delivery.get("target_env") or "DISCORD_BRIEFING_WEBHOOK_URL")
        webhook_url = os.environ.get(target_env, "").strip()
        if not webhook_url:
            log.error(
                "watcher_runner.webhook_missing",
                profile=meta.profile_id,
                watcher=meta.name,
                env=target_env,
            )
            return
        body = self._render_body(items)
        title = f"📧 새 메일 {len(items)}건"
        payload = _build_embed(
            title=title,
            body=body,
            footer=f"{meta.profile_id} · {meta.name}",
        )
        status = await asyncio.to_thread(_post_webhook, webhook_url, payload)
        if status == 204:
            log.info(
                "watcher_runner.notified",
                profile=meta.profile_id,
                watcher=meta.name,
                count=len(items),
            )
        else:
            log.error(
                "watcher_runner.notify_failed",
                profile=meta.profile_id,
                watcher=meta.name,
                count=len(items),
                status=status,
            )

    @staticmethod
    def _render_body(items: list[MailMessage]) -> str:
        lines: list[str] = []
        for m in items[:20]:
            sender = (m.sender or "").strip()
            subject = (m.subject or "(no subject)").strip()
            snippet = (m.snippet or "").strip()
            line = f"📨 [{m.account}] {sender} — {subject}"
            if snippet:
                line += f"\n  ↳ {snippet[:160]}"
            lines.append(line)
        return "\n".join(lines)

    # ---- calendar watchers (hermes-delegated polling) -----------------

    async def _tick_calendar_via_hermes(
        self, meta: WatcherMeta, *, mode: str
    ) -> None:
        """Poll calendar_ops via Hermes for conflict / invitation alerts.

        First run records the high-water mark and skips notifying — same
        seeding pattern as ``_tick_mail`` to avoid a flood of historical
        alerts when a watcher is freshly registered. Subsequent ticks ask
        Hermes to inspect changes since the last poll; the prompt YAML
        instructs Hermes to emit ``NO_NOTIFICATION`` for empty windows so
        we can short-circuit without DM'ing.

        High-water (``last_run_at``) is only advanced on either no-op
        responses or successful delivery — failed notifications get
        retried in the next tick.
        """
        if self.hermes is None:
            log.warning(
                "watcher_runner.hermes_missing",
                profile=meta.profile_id,
                watcher=meta.name,
            )
            return

        last_run = await self.repo.get_watcher_last_run(
            meta.profile_id, meta.name
        )
        now = datetime.now(timezone.utc)

        if last_run is None:
            await self.repo.update_watcher_state(
                meta.profile_id, meta.name,
                last_dedup_key=now.isoformat(), account="",
            )
            log.info(
                "watcher_runner.calendar_seeded",
                profile=meta.profile_id,
                watcher=meta.name,
                high_water=now.isoformat(),
            )
            return

        last_run_kst = last_run.astimezone(_KST)
        now_kst = now.astimezone(_KST)
        window = (
            f"[감지 윈도우 (KST)] "
            f"{last_run_kst.strftime('%Y-%m-%d %H:%M')} ~ "
            f"{now_kst.strftime('%Y-%m-%d %H:%M')}"
        )
        prompt = f"{window}\n\n{meta.prompt}".strip()

        timeout_ms = getattr(self.settings, "calendar_skill_timeout_ms", 180_000)
        max_turns = getattr(self.settings, "calendar_skill_max_turns", 10)

        try:
            result = await self.hermes.run(
                prompt,
                model=getattr(self.settings, "calendar_skill_model", "") or None,
                provider=getattr(self.settings, "calendar_skill_provider", "") or None,
                profile=meta.profile_id,
                preload_skills=list(meta.skills),
                max_turns=max_turns,
                timeout_ms=timeout_ms,
            )
        except HermesAdapterError as e:
            log.warning(
                "watcher_runner.calendar_hermes_failed",
                profile=meta.profile_id,
                watcher=meta.name,
                err=str(e)[:200],
            )
            return  # leave high-water unchanged → retry next tick
        except Exception:  # noqa: BLE001
            log.exception(
                "watcher_runner.calendar_unexpected",
                profile=meta.profile_id,
                watcher=meta.name,
            )
            return

        body = (result.text or "").strip()
        normalized = body.strip().strip("`'\"")

        if not body or normalized == _NO_NOTIFICATION_MARKER:
            await self.repo.update_watcher_state(
                meta.profile_id, meta.name,
                last_dedup_key=now.isoformat(), account="",
            )
            log.info(
                "watcher_runner.calendar_no_notification",
                profile=meta.profile_id,
                watcher=meta.name,
            )
            return

        title = (
            "⚠️ 일정 충돌 감지" if mode == "conflicts"
            else "📨 새 초대 수신"
        )
        sent = await self._dispatch_to_delivery(meta, title=title, body=body)
        if sent:
            await self.repo.update_watcher_state(
                meta.profile_id, meta.name,
                last_dedup_key=now.isoformat(), account="",
            )
            log.info(
                "watcher_runner.calendar_notified",
                profile=meta.profile_id,
                watcher=meta.name,
                mode=mode,
                length=len(body),
            )

    async def _dispatch_to_delivery(
        self, meta: WatcherMeta, *, title: str, body: str,
    ) -> bool:
        """Deliver via DM (delivery.channel='dm') or webhook fallback.

        Returns True on confirmed delivery, False otherwise. Callers use
        the boolean to decide whether to advance the polling watermark.
        """
        channel = str(meta.delivery.get("channel", "")).lower()
        target_env = str(meta.delivery.get("target_env") or "")
        footer = f"{meta.profile_id} · {meta.name}"

        if channel == "dm":
            user_id_str = os.environ.get(target_env, "").strip()
            if not user_id_str.isdigit():
                log.error(
                    "watcher_runner.dm_user_id_missing",
                    env=target_env,
                    profile=meta.profile_id,
                    watcher=meta.name,
                )
                return False
            if self.dm_dispatcher is None:
                log.error(
                    "watcher_runner.dm_dispatcher_missing",
                    profile=meta.profile_id,
                    watcher=meta.name,
                )
                return False
            try:
                await self.dm_dispatcher.send_dm(
                    int(user_id_str),
                    title=title,
                    body=body,
                    footer=footer,
                )
                return True
            except Exception as e:  # noqa: BLE001
                log.warning(
                    "watcher_runner.dm_failed",
                    profile=meta.profile_id,
                    watcher=meta.name,
                    err=str(e)[:200],
                )
                return False

        webhook_url = os.environ.get(
            target_env or "DISCORD_BRIEFING_WEBHOOK_URL", ""
        ).strip()
        if not webhook_url:
            log.error(
                "watcher_runner.webhook_missing",
                env=target_env,
                profile=meta.profile_id,
                watcher=meta.name,
            )
            return False
        payload = _build_embed(title=title, body=body, footer=footer)
        status = await asyncio.to_thread(_post_webhook, webhook_url, payload)
        return status == 204


def _apply_filter(
    items: list[MailMessage], filter_cfg: dict[str, Any]
) -> list[MailMessage]:
    """Filter mail messages per the watcher YAML's ``source.filter`` block.

    Schema (all fields optional, missing = no constraint)::

        filter:
          sender_include: ["*@boss.com"]      # glob, matches sender → keep
          sender_exclude: ["noreply@*"]       # glob, matches sender → drop
          subject_keywords_any: ["urgent"]    # at least one must hit (case-insensitive)
          subject_keywords_none: ["promo"]    # any hit → drop

    Precedence per item (drop wins):
        1. ``sender_exclude`` match → drop
        2. ``sender_include`` set AND no match → drop
        3. ``subject_keywords_none`` match → drop
        4. ``subject_keywords_any`` set AND no match → drop
        5. otherwise keep
    """
    if not filter_cfg:
        return items
    sender_include = [p.lower() for p in filter_cfg.get("sender_include") or []]
    sender_exclude = [p.lower() for p in filter_cfg.get("sender_exclude") or []]
    keywords_any = [k.lower() for k in filter_cfg.get("subject_keywords_any") or []]
    keywords_none = [k.lower() for k in filter_cfg.get("subject_keywords_none") or []]

    out: list[MailMessage] = []
    for m in items:
        sender = (m.sender or "").lower()
        subject = (m.subject or "").lower()

        if sender_exclude and any(fnmatch.fnmatch(sender, p) for p in sender_exclude):
            continue
        if sender_include and not any(fnmatch.fnmatch(sender, p) for p in sender_include):
            continue
        if keywords_none and any(k in subject for k in keywords_none):
            continue
        if keywords_any and not any(k in subject for k in keywords_any):
            continue
        out.append(m)
    return out


def _build_embed(*, title: str, body: str, footer: str | None) -> dict:
    embed: dict[str, Any] = {
        "title": title[:256],
        "description": body[:4000],
        "color": _EMBED_COLOR_DEFAULT,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }
    if footer:
        embed["footer"] = {"text": footer[:2048]}
    return {"embeds": [embed]}


_WEBHOOK_USER_AGENT = "hermes-hybrid-watcher/0.1 (+https://github.com/anthropics/hermes-hybrid)"


def _post_webhook(url: str, payload: dict) -> int:
    """POST a Discord webhook embed. Returns the HTTP status (204 on success).

    Cloudflare in front of discord.com rejects requests with the default
    Python-urllib user agent (error 1010). We send a stable identifier that
    Cloudflare's bot heuristics let through.
    """
    data = json.dumps(payload).encode("utf-8")
    req = urlreq.Request(
        url,
        data=data,
        headers={
            "Content-Type": "application/json",
            "User-Agent": _WEBHOOK_USER_AGENT,
        },
        method="POST",
    )
    try:
        with urlreq.urlopen(req, timeout=_WEBHOOK_TIMEOUT_SEC) as resp:
            return resp.status
    except HTTPError as e:
        body = ""
        try:
            body = e.read().decode("utf-8", errors="replace")[:200]
        except Exception:  # noqa: BLE001
            pass
        log.warning(
            "watcher_runner.webhook_http_error",
            code=e.code,
            reason=str(e.reason),
            body=body,
        )
        return e.code
    except URLError as e:
        log.warning("watcher_runner.webhook_url_error", err=str(e))
        return -1
