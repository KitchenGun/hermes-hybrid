"""Tests for the calendar watcher pipeline (hermes-delegated polling).

Live Hermes is not exercised — the adapter is replaced with a fake. These
tests cover the in-process wiring:
  - First-run seeding records the high-water and skips notification
  - NO_NOTIFICATION response advances the watermark without dispatching
  - A normal response triggers the DM dispatcher with the right title
  - Hermes failure leaves the watermark intact so the next tick retries
  - Dispatch failure (missing target env) leaves the watermark intact too

Watcher runtime intentionally does NOT call google API directly —
hermes drives the calendar_ops MCP. Tests assert that contract by
inspecting fake_hermes.calls.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest

from src.hermes_adapter.adapter import HermesResult, HermesTimeout
from src.orchestrator.profile_loader import ProfileLoader
from src.state.repository import Repository
from src.watcher.runner import WatcherRunner


def _write_calendar_watcher(
    profiles_dir: Path, source_type: str, name: str = "test_w"
) -> None:
    pdir = profiles_dir / "calendar_ops"
    (pdir / "watchers").mkdir(parents=True, exist_ok=True)
    if not (pdir / "config.yaml").exists():
        (pdir / "config.yaml").write_text(
            "agent:\n  max_turns: 5\n", encoding="utf-8"
        )
    yaml_text = f"""
name: {name}
category: watcher
description: test watcher
trigger:
  type: watcher
  interval_seconds: 300
  source: {source_type}
skills:
  - google_calendar
delivery:
  channel: dm
  target_env: TEST_DM_USER_ID
prompt: |
  test prompt body
"""
    (pdir / "watchers" / f"{name}.yaml").write_text(yaml_text, encoding="utf-8")


class _FakeHermes:
    def __init__(self, result_or_exc: Any):
        self._result = result_or_exc
        self.calls: list[dict[str, Any]] = []

    async def run(self, query: str, **kwargs: Any):
        self.calls.append({"query": query, **kwargs})
        if isinstance(self._result, Exception):
            raise self._result
        return self._result


class _FakeDmDispatcher:
    def __init__(self):
        self.calls: list[dict[str, Any]] = []

    async def send_dm(
        self, user_id: int, *, title: str, body: str, footer: str = ""
    ):
        self.calls.append(
            {"user_id": user_id, "title": title, "body": body, "footer": footer}
        )


def _hermes_ok(text: str) -> HermesResult:
    return HermesResult(
        text=text,
        session_id="sid",
        tier_used="C1",  # type: ignore[arg-type]
        model_name="qwen2.5:14b",
        provider="ollama",
        duration_ms=1,
        stdout_raw="",
        stderr_raw="",
    )


class _SettingsStub:
    watcher_default_interval_seconds = 300
    calendar_skill_timeout_ms = 180_000
    calendar_skill_max_turns = 5
    calendar_skill_model = ""
    calendar_skill_provider = ""


@pytest.mark.asyncio
async def test_calendar_watcher_seeds_high_water_on_first_run(tmp_path):
    """First tick must NOT call hermes or dispatcher — only record the
    watermark — so freshly registered watchers don't flood the user."""
    repo = Repository(tmp_path / "t.db")
    await repo.init()
    profiles_dir = tmp_path / "profiles"
    _write_calendar_watcher(profiles_dir, "internal.calendar_write_completed")

    loader = ProfileLoader(profiles_dir, cache_ttl_seconds=0.0)
    fake_hermes = _FakeHermes(_hermes_ok("이건 호출되면 안 됨"))
    dispatcher = _FakeDmDispatcher()
    runner = WatcherRunner(
        settings=_SettingsStub(),
        repo=repo,
        profile_loader=loader,
        profiles_dir=profiles_dir,
        dm_dispatcher=dispatcher,
        hermes=fake_hermes,
    )

    [meta] = loader.iter_watchers()
    await runner._tick(meta)

    assert fake_hermes.calls == []
    assert dispatcher.calls == []
    last_run = await repo.get_watcher_last_run("calendar_ops", "test_w")
    assert last_run is not None


@pytest.mark.asyncio
async def test_calendar_watcher_no_notification_response_skips_dispatch(tmp_path):
    """Hermes returns the marker → dispatcher untouched, watermark advances
    so the next tick examines a fresh window."""
    repo = Repository(tmp_path / "t.db")
    await repo.init()
    seed = (datetime.now(timezone.utc) - timedelta(minutes=5)).isoformat()
    await repo.update_watcher_state(
        "calendar_ops", "test_w", last_dedup_key=seed, account="",
    )

    profiles_dir = tmp_path / "profiles"
    _write_calendar_watcher(profiles_dir, "internal.calendar_write_completed")
    loader = ProfileLoader(profiles_dir, cache_ttl_seconds=0.0)
    fake_hermes = _FakeHermes(_hermes_ok("NO_NOTIFICATION"))
    dispatcher = _FakeDmDispatcher()
    runner = WatcherRunner(
        settings=_SettingsStub(),
        repo=repo,
        profile_loader=loader,
        profiles_dir=profiles_dir,
        dm_dispatcher=dispatcher,
        hermes=fake_hermes,
    )

    [meta] = loader.iter_watchers()
    await runner._tick(meta)

    assert len(fake_hermes.calls) == 1
    assert dispatcher.calls == []
    last_run = await repo.get_watcher_last_run("calendar_ops", "test_w")
    assert last_run is not None
    assert last_run > datetime.fromisoformat(seed)


@pytest.mark.asyncio
async def test_calendar_watcher_dispatches_normal_response_with_conflict_title(
    tmp_path, monkeypatch
):
    repo = Repository(tmp_path / "t.db")
    await repo.init()
    seed = (datetime.now(timezone.utc) - timedelta(minutes=5)).isoformat()
    await repo.update_watcher_state(
        "calendar_ops", "test_w", last_dedup_key=seed, account="",
    )
    monkeypatch.setenv("TEST_DM_USER_ID", "123456789")

    profiles_dir = tmp_path / "profiles"
    _write_calendar_watcher(profiles_dir, "internal.calendar_write_completed")
    loader = ProfileLoader(profiles_dir, cache_ttl_seconds=0.0)
    fake_hermes = _FakeHermes(_hermes_ok("⚠️ 새 이벤트: 회의 (4/30 14:00–15:00)"))
    dispatcher = _FakeDmDispatcher()
    runner = WatcherRunner(
        settings=_SettingsStub(),
        repo=repo,
        profile_loader=loader,
        profiles_dir=profiles_dir,
        dm_dispatcher=dispatcher,
        hermes=fake_hermes,
    )

    [meta] = loader.iter_watchers()
    await runner._tick(meta)

    assert len(dispatcher.calls) == 1
    call = dispatcher.calls[0]
    assert call["user_id"] == 123456789
    assert call["title"] == "⚠️ 일정 충돌 감지"
    assert "회의" in call["body"]
    # The hermes prompt must include the time-window context built by the runner.
    assert len(fake_hermes.calls) == 1
    assert "[감지 윈도우 (KST)]" in fake_hermes.calls[0]["query"]
    assert fake_hermes.calls[0]["profile"] == "calendar_ops"


@pytest.mark.asyncio
async def test_calendar_watcher_invitation_mode_uses_invitation_title(
    tmp_path, monkeypatch
):
    repo = Repository(tmp_path / "t.db")
    await repo.init()
    seed = (datetime.now(timezone.utc) - timedelta(minutes=5)).isoformat()
    await repo.update_watcher_state(
        "calendar_ops", "test_w", last_dedup_key=seed, account="",
    )
    monkeypatch.setenv("TEST_DM_USER_ID", "1")

    profiles_dir = tmp_path / "profiles"
    _write_calendar_watcher(profiles_dir, "google_calendar.push_notification")
    loader = ProfileLoader(profiles_dir, cache_ttl_seconds=0.0)
    fake_hermes = _FakeHermes(_hermes_ok("📨 새 초대"))
    dispatcher = _FakeDmDispatcher()
    runner = WatcherRunner(
        settings=_SettingsStub(),
        repo=repo,
        profile_loader=loader,
        profiles_dir=profiles_dir,
        dm_dispatcher=dispatcher,
        hermes=fake_hermes,
    )

    [meta] = loader.iter_watchers()
    await runner._tick(meta)

    assert dispatcher.calls[0]["title"] == "📨 새 초대 수신"


@pytest.mark.asyncio
async def test_calendar_watcher_hermes_failure_leaves_watermark_for_retry(tmp_path):
    """A HermesTimeout must NOT advance last_run — otherwise the affected
    polling window would be dropped silently."""
    repo = Repository(tmp_path / "t.db")
    await repo.init()
    seed_dt = datetime.now(timezone.utc) - timedelta(minutes=5)
    seed = seed_dt.isoformat()
    await repo.update_watcher_state(
        "calendar_ops", "test_w", last_dedup_key=seed, account="",
    )

    profiles_dir = tmp_path / "profiles"
    _write_calendar_watcher(profiles_dir, "internal.calendar_write_completed")
    loader = ProfileLoader(profiles_dir, cache_ttl_seconds=0.0)
    fake_hermes = _FakeHermes(HermesTimeout("simulated 180s timeout"))
    dispatcher = _FakeDmDispatcher()
    runner = WatcherRunner(
        settings=_SettingsStub(),
        repo=repo,
        profile_loader=loader,
        profiles_dir=profiles_dir,
        dm_dispatcher=dispatcher,
        hermes=fake_hermes,
    )

    [meta] = loader.iter_watchers()
    await runner._tick(meta)

    assert dispatcher.calls == []
    last_run = await repo.get_watcher_last_run("calendar_ops", "test_w")
    # Watermark unchanged — next tick re-examines the same window.
    assert last_run == seed_dt


@pytest.mark.asyncio
async def test_calendar_watcher_dispatch_failure_does_not_advance_watermark(
    tmp_path, monkeypatch
):
    """Missing target env → dispatch fails → watermark must stay so the
    notification is retried once the env is set."""
    repo = Repository(tmp_path / "t.db")
    await repo.init()
    seed_dt = datetime.now(timezone.utc) - timedelta(minutes=5)
    seed = seed_dt.isoformat()
    await repo.update_watcher_state(
        "calendar_ops", "test_w", last_dedup_key=seed, account="",
    )
    monkeypatch.delenv("TEST_DM_USER_ID", raising=False)

    profiles_dir = tmp_path / "profiles"
    _write_calendar_watcher(profiles_dir, "internal.calendar_write_completed")
    loader = ProfileLoader(profiles_dir, cache_ttl_seconds=0.0)
    fake_hermes = _FakeHermes(_hermes_ok("body"))
    dispatcher = _FakeDmDispatcher()
    runner = WatcherRunner(
        settings=_SettingsStub(),
        repo=repo,
        profile_loader=loader,
        profiles_dir=profiles_dir,
        dm_dispatcher=dispatcher,
        hermes=fake_hermes,
    )

    [meta] = loader.iter_watchers()
    await runner._tick(meta)

    assert dispatcher.calls == []
    last_run = await repo.get_watcher_last_run("calendar_ops", "test_w")
    assert last_run == seed_dt
