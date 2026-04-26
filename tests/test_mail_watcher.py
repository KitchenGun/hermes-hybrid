"""Tests for the mail watcher pipeline.

Live IMAP / Gmail API are not exercised — those require credentials.
The tests below cover the wiring that runs in-process:
  - ProfileLoader picks up watcher YAMLs
  - Repository persists per-account dedup state
  - AccountLoader parses accounts.yaml correctly
  - NaverProvider error handling when password is missing
  - WatcherRunner notification formatting
"""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

from src.orchestrator.profile_loader import ProfileLoader, WatcherMeta
from src.skills.mail.accounts import AccountLoader, AccountConfigError
from src.skills.mail.base import MailMessage, MailProviderError
from src.skills.mail.naver import NaverProvider
from src.state.repository import Repository
from src.watcher.runner import WatcherRunner, _apply_filter


# ---- ProfileLoader watcher discovery -----------------------------------


def _write_profile_with_watcher(root: Path, profile_id: str, watcher_yaml: str) -> Path:
    pdir = root / profile_id
    (pdir / "watchers").mkdir(parents=True)
    (pdir / "config.yaml").write_text("agent:\n  max_turns: 1\n", encoding="utf-8")
    (pdir / "watchers" / "demo.yaml").write_text(watcher_yaml, encoding="utf-8")
    return pdir


def test_profile_loader_discovers_mail_watcher(tmp_path):
    yaml_text = """
name: demo
category: watcher
description: test
trigger:
  type: watcher
  interval_seconds: 60
  source:
    type: mail_poll
    accounts: [a1, a2]
skills: [mail]
delivery:
  channel: webhook
  target_env: TEST_HOOK
"""
    _write_profile_with_watcher(tmp_path, "demo_profile", yaml_text)
    loader = ProfileLoader(tmp_path, cache_ttl_seconds=0.0)
    watchers = loader.iter_watchers()
    assert len(watchers) == 1
    w = watchers[0]
    assert w.profile_id == "demo_profile"
    assert w.name == "demo"
    assert w.source_type == "mail_poll"
    assert w.interval_seconds == 60
    assert w.source.get("accounts") == ["a1", "a2"]
    assert w.delivery.get("target_env") == "TEST_HOOK"


def test_profile_loader_ignores_non_watcher_yaml(tmp_path):
    yaml_text = """
name: not_a_watcher
trigger:
  type: cron
  schedule: "0 8 * * *"
"""
    _write_profile_with_watcher(tmp_path, "demo_profile", yaml_text)
    loader = ProfileLoader(tmp_path, cache_ttl_seconds=0.0)
    assert loader.iter_watchers() == []


# ---- Repository watcher_state ------------------------------------------


@pytest.mark.asyncio
async def test_watcher_state_per_account_isolation(tmp_path):
    repo = Repository(tmp_path / "test.db")
    await repo.init()
    await repo.update_watcher_state("p1", "w1", "msg-100", account="acc_a")
    await repo.update_watcher_state("p1", "w1", "msg-200", account="acc_b")
    assert await repo.get_watcher_state("p1", "w1", account="acc_a") == "msg-100"
    assert await repo.get_watcher_state("p1", "w1", account="acc_b") == "msg-200"
    # No row for this account → None
    assert await repo.get_watcher_state("p1", "w1", account="acc_c") is None


@pytest.mark.asyncio
async def test_watcher_state_upsert_overwrites(tmp_path):
    repo = Repository(tmp_path / "test.db")
    await repo.init()
    await repo.update_watcher_state("p1", "w1", "msg-1", account="a")
    await repo.update_watcher_state("p1", "w1", "msg-2", account="a")
    assert await repo.get_watcher_state("p1", "w1", account="a") == "msg-2"


# ---- AccountLoader -----------------------------------------------------


def test_account_loader_parses_mixed_providers(tmp_path):
    pdir = tmp_path / "p"
    pdir.mkdir()
    (pdir / "accounts.yaml").write_text(
        """
accounts:
  - name: g1
    provider: gmail
    address: a@gmail.com
    token_file: ./secrets/g1.json
  - name: n1
    provider: naver
    address: x@naver.com
    password_env: NAVER_APP_PASSWORD
""",
        encoding="utf-8",
    )
    loader = AccountLoader(pdir)
    accounts = loader.load()
    assert set(accounts.keys()) == {"g1", "n1"}
    assert accounts["g1"].provider == "gmail"
    assert accounts["n1"].provider == "naver"


def test_account_loader_rejects_unknown_provider(tmp_path):
    pdir = tmp_path / "p"
    pdir.mkdir()
    (pdir / "accounts.yaml").write_text(
        """
accounts:
  - name: x
    provider: outlook
    address: a@x.com
""",
        encoding="utf-8",
    )
    with pytest.raises(AccountConfigError):
        AccountLoader(pdir).load()


def test_account_loader_rejects_duplicate_names(tmp_path):
    pdir = tmp_path / "p"
    pdir.mkdir()
    (pdir / "accounts.yaml").write_text(
        """
accounts:
  - name: dup
    provider: gmail
    address: a@gmail.com
    token_file: ./t.json
  - name: dup
    provider: naver
    address: b@naver.com
    password_env: PW
""",
        encoding="utf-8",
    )
    with pytest.raises(AccountConfigError):
        AccountLoader(pdir).load()


def test_account_loader_missing_file_returns_empty(tmp_path):
    pdir = tmp_path / "p"
    pdir.mkdir()
    assert AccountLoader(pdir).load() == {}


# ---- NaverProvider error handling --------------------------------------


def test_naver_provider_raises_when_password_env_unset(monkeypatch):
    monkeypatch.delenv("FAKE_NAVER_PW", raising=False)
    p = NaverProvider(
        account="t", address="x@naver.com", password_env="FAKE_NAVER_PW"
    )
    with pytest.raises(MailProviderError, match="app password"):
        p._password()


def test_naver_provider_strict_uid_filter_drops_rfc_3501_courtesy_match(monkeypatch):
    """Regression: per RFC 3501 §6.4.8, "UID X:*" always returns the
    highest UID even when X > highest. Without a strict client-side
    filter, we'd re-notify the latest message every poll forever.
    """
    from types import SimpleNamespace

    # Fake imap_tools.MailBox + AND
    class _FakeMsg:
        def __init__(self, uid, subject):
            self.uid = uid
            self.subject = subject
            self.from_ = "user@example.com"
            self.from_values = SimpleNamespace(name="User", email="user@example.com")
            self.date = datetime(2026, 4, 26, 23, 5, tzinfo=timezone.utc)

    class _FakeMailBox:
        def __init__(self, host, port):
            self._fetch_result = []

        def login(self, address, password, initial_folder=None):
            return self

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def fetch(self, *, criteria, limit, **kw):
            # Server-side: returns latest UID even when criteria range is empty
            return iter(self._fetch_result)

    # Build a fake mailbox where the highest UID (32798) was already seen
    # last poll. Server "obediently" returns it for "UID 32799:*".
    fake_box = _FakeMailBox("h", 993)
    fake_box._fetch_result = [_FakeMsg("32798", "Test")]

    def fake_imap_tools():
        class _AND:
            pass
        return (lambda h, p: fake_box), _AND

    monkeypatch.setattr("src.skills.mail.naver._lazy_imap_tools", fake_imap_tools)

    p = NaverProvider(
        account="t", address="x@naver.com", password_env="FAKE_NAVER_PW"
    )
    monkeypatch.setenv("FAKE_NAVER_PW", "x")

    # last_message_id == "32798": the server returns UID 32798 (RFC courtesy),
    # but our strict filter must drop it.
    out = p.list_new_since("32798", limit=20)
    assert out == [], f"strict filter failed: returned {[m.message_id for m in out]}"


def test_naver_provider_strict_uid_filter_keeps_genuinely_new(monkeypatch):
    """Companion case: when there IS a genuinely new UID, it must survive."""
    from types import SimpleNamespace

    class _FakeMsg:
        def __init__(self, uid, subject):
            self.uid = uid
            self.subject = subject
            self.from_ = "user@example.com"
            self.from_values = SimpleNamespace(name="User", email="user@example.com")
            self.date = datetime(2026, 4, 26, 23, 5, tzinfo=timezone.utc)

    class _FakeMailBox:
        def __init__(self, host, port):
            self._fetch_result = []

        def login(self, address, password, initial_folder=None):
            return self

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def fetch(self, *, criteria, limit, **kw):
            return iter(self._fetch_result)

    fake_box = _FakeMailBox("h", 993)
    # Server returns the new UID 32800 + the RFC-courtesy UID 32798
    fake_box._fetch_result = [_FakeMsg("32800", "New"), _FakeMsg("32798", "Old")]

    def fake_imap_tools():
        class _AND:
            pass
        return (lambda h, p: fake_box), _AND

    monkeypatch.setattr("src.skills.mail.naver._lazy_imap_tools", fake_imap_tools)
    monkeypatch.setenv("FAKE_NAVER_PW", "x")

    p = NaverProvider(
        account="t", address="x@naver.com", password_env="FAKE_NAVER_PW"
    )

    out = p.list_new_since("32798", limit=20)
    # Only the genuinely-new 32800 should pass the filter
    assert [m.message_id for m in out] == ["32800"]


# ---- WatcherRunner notification formatting -----------------------------


def _make_msg(account: str, subject: str, sender: str = "u@x", snippet: str = "") -> MailMessage:
    return MailMessage(
        provider="gmail",
        account=account,
        address="x@y",
        message_id=f"id-{subject}",
        subject=subject,
        sender=sender,
        snippet=snippet,
        received_at=datetime.now(tz=timezone.utc),
    )


def test_watcher_runner_render_body_groups_per_account():
    items = [
        _make_msg("personal_gmail", "Hello", "boss@x", "Quick question"),
        _make_msg("work_gmail", "Meeting", "pm@y"),
        _make_msg("personal_naver", "공지", "no-reply@naver.com", "안녕하세요"),
    ]
    body = WatcherRunner._render_body(items)
    assert "[personal_gmail]" in body
    assert "[work_gmail]" in body
    assert "[personal_naver]" in body
    assert "Hello" in body
    assert "Meeting" in body
    assert "공지" in body


def test_watcher_runner_render_body_truncates_long_snippet():
    long = "x" * 500
    body = WatcherRunner._render_body([_make_msg("a", "S", snippet=long)])
    # snippet is clipped to 160 chars in the rendered line
    assert "x" * 160 in body
    assert "x" * 161 not in body


# ---- WatcherRunner mail tick (provider mocked) -------------------------


@pytest.mark.asyncio
async def test_watcher_runner_seeds_high_water_on_first_run(tmp_path, monkeypatch):
    """First poll for an account must record the newest id WITHOUT notifying."""
    repo = Repository(tmp_path / "t.db")
    await repo.init()

    # Build a profile dir with accounts.yaml + watcher
    profiles_dir = tmp_path / "profiles"
    pdir = profiles_dir / "demo"
    (pdir / "watchers").mkdir(parents=True)
    (pdir / "config.yaml").write_text("agent: {max_turns: 1}\n", encoding="utf-8")
    (pdir / "accounts.yaml").write_text(
        """
accounts:
  - name: a1
    provider: gmail
    address: a@gmail.com
    token_file: ./secrets/a.json
""",
        encoding="utf-8",
    )
    (pdir / "watchers" / "w.yaml").write_text(
        """
name: w
trigger:
  type: watcher
  interval_seconds: 60
  source:
    type: mail_poll
    accounts: [a1]
delivery:
  channel: webhook
  target_env: HOOK_URL
""",
        encoding="utf-8",
    )

    loader = ProfileLoader(profiles_dir, cache_ttl_seconds=0.0)

    class _Settings:
        watcher_default_interval_seconds = 300

    runner = WatcherRunner(
        settings=_Settings(),
        repo=repo,
        profile_loader=loader,
        profiles_dir=profiles_dir,
    )

    # Stub provider so we don't hit Gmail
    fake_msgs = [
        _make_msg("a1", "newest"),
        _make_msg("a1", "older"),
    ]
    fake_msgs[0] = MailMessage(**{**fake_msgs[0].__dict__, "message_id": "MSG-NEW"})

    def fake_build(self_loader, cfg):  # noqa: ARG001
        class FakeProvider:
            account = cfg.name
            address = cfg.address
            name = cfg.provider

            def list_new_since(self, last_id, *, limit=20):
                return fake_msgs

        return FakeProvider()

    notify_calls: list[Any] = []

    async def fake_notify(meta, items):
        notify_calls.append((meta, items))

    monkeypatch.setattr(AccountLoader, "build", fake_build)
    monkeypatch.setattr(runner, "_notify", fake_notify)

    [meta] = loader.iter_watchers()
    await runner._tick_mail(meta)

    # First-run seeding: no notification, but high-water mark stored
    assert notify_calls == []
    assert await repo.get_watcher_state("demo", "w", account="a1") == "MSG-NEW"


@pytest.mark.asyncio
async def test_watcher_runner_notifies_after_seed(tmp_path, monkeypatch):
    """Second run with a stored high-water mark must surface new items."""
    repo = Repository(tmp_path / "t.db")
    await repo.init()
    await repo.update_watcher_state("demo", "w", "OLD", account="a1")

    profiles_dir = tmp_path / "profiles"
    pdir = profiles_dir / "demo"
    (pdir / "watchers").mkdir(parents=True)
    (pdir / "config.yaml").write_text("agent: {max_turns: 1}\n", encoding="utf-8")
    (pdir / "accounts.yaml").write_text(
        """
accounts:
  - name: a1
    provider: gmail
    address: a@gmail.com
    token_file: ./t.json
""",
        encoding="utf-8",
    )
    (pdir / "watchers" / "w.yaml").write_text(
        """
name: w
trigger:
  type: watcher
  interval_seconds: 60
  source:
    type: mail_poll
    accounts: [a1]
delivery:
  channel: webhook
  target_env: HOOK_URL
""",
        encoding="utf-8",
    )

    loader = ProfileLoader(profiles_dir, cache_ttl_seconds=0.0)

    class _Settings:
        watcher_default_interval_seconds = 300

    runner = WatcherRunner(
        settings=_Settings(),
        repo=repo,
        profile_loader=loader,
        profiles_dir=profiles_dir,
    )

    fresh = MailMessage(
        provider="gmail",
        account="a1",
        address="a@gmail.com",
        message_id="NEW-1",
        subject="hi",
        sender="x@y",
        snippet="",
        received_at=datetime.now(tz=timezone.utc),
    )

    def fake_build(self_loader, cfg):  # noqa: ARG001
        class FakeProvider:
            account = cfg.name
            address = cfg.address
            name = cfg.provider

            def list_new_since(self, last_id, *, limit=20):
                assert last_id == "OLD"
                return [fresh]

        return FakeProvider()

    notify_calls: list[Any] = []

    async def fake_notify(meta, items):
        notify_calls.append((meta, items))

    monkeypatch.setattr(AccountLoader, "build", fake_build)
    monkeypatch.setattr(runner, "_notify", fake_notify)

    [meta] = loader.iter_watchers()
    await runner._tick_mail(meta)

    assert len(notify_calls) == 1
    _, items = notify_calls[0]
    assert items[0].message_id == "NEW-1"
    assert await repo.get_watcher_state("demo", "w", account="a1") == "NEW-1"


# ---- _apply_filter (sender / keyword) ----------------------------------


def _msg(account: str, sender: str, subject: str) -> MailMessage:
    return MailMessage(
        provider="gmail",
        account=account,
        address="x@y",
        message_id=f"id-{subject}",
        subject=subject,
        sender=sender,
        snippet="",
        received_at=datetime.now(tz=timezone.utc),
    )


def test_apply_filter_no_config_keeps_all():
    items = [_msg("a", "u@x.com", "hi"), _msg("a", "noreply@x.com", "promo")]
    assert _apply_filter(items, {}) == items
    assert _apply_filter(items, None or {}) == items


def test_apply_filter_sender_exclude_glob():
    items = [
        _msg("a", "boss@important.org", "review"),
        _msg("a", "noreply@newsletter.com", "weekly"),
        _msg("a", "team@important.org", "lunch"),
    ]
    out = _apply_filter(items, {"sender_exclude": ["noreply@*"]})
    assert [m.sender for m in out] == ["boss@important.org", "team@important.org"]


def test_apply_filter_sender_include_only():
    items = [
        _msg("a", "boss@important.org", "review"),
        _msg("a", "stranger@unknown.com", "spam-ish"),
    ]
    out = _apply_filter(items, {"sender_include": ["*@important.org"]})
    assert [m.sender for m in out] == ["boss@important.org"]


def test_apply_filter_subject_keywords_any():
    items = [
        _msg("a", "u@x", "Urgent: review needed"),
        _msg("a", "u@x", "weekly digest"),
        _msg("a", "u@x", "긴급 회의 변경"),
    ]
    out = _apply_filter(items, {"subject_keywords_any": ["urgent", "긴급"]})
    assert [m.subject for m in out] == ["Urgent: review needed", "긴급 회의 변경"]


def test_apply_filter_subject_keywords_none_blocks():
    items = [
        _msg("a", "u@x", "Q4 plan"),
        _msg("a", "u@x", "Promotional offer 50% off"),
        _msg("a", "u@x", "광고: 특가 안내"),
    ]
    out = _apply_filter(items, {"subject_keywords_none": ["promo", "광고"]})
    assert [m.subject for m in out] == ["Q4 plan"]


def test_apply_filter_exclude_wins_over_include():
    """When both sender_include and sender_exclude match, exclude must drop."""
    items = [_msg("a", "noreply@important.org", "auto")]
    out = _apply_filter(
        items,
        {
            "sender_include": ["*@important.org"],   # matches
            "sender_exclude": ["noreply@*"],         # also matches → drop
        },
    )
    assert out == []


# ---- _tick_mail isolation: one bad account doesn't block others ---------


@pytest.mark.asyncio
async def test_tick_mail_isolates_account_failures(tmp_path, monkeypatch):
    """If account A's provider raises, account B's notifications still fire."""
    repo = Repository(tmp_path / "t.db")
    await repo.init()
    await repo.update_watcher_state("demo", "w", "OLD-A", account="a1")
    await repo.update_watcher_state("demo", "w", "OLD-B", account="a2")

    profiles_dir = tmp_path / "profiles"
    pdir = profiles_dir / "demo"
    (pdir / "watchers").mkdir(parents=True)
    (pdir / "config.yaml").write_text("agent: {max_turns: 1}\n", encoding="utf-8")
    (pdir / "accounts.yaml").write_text(
        """
accounts:
  - name: a1
    provider: gmail
    address: a1@x
    token_file: ./t1.json
  - name: a2
    provider: gmail
    address: a2@x
    token_file: ./t2.json
""",
        encoding="utf-8",
    )
    (pdir / "watchers" / "w.yaml").write_text(
        """
name: w
trigger:
  type: watcher
  interval_seconds: 60
  source:
    type: mail_poll
    accounts: [a1, a2]
delivery:
  channel: webhook
  target_env: HOOK_URL
""",
        encoding="utf-8",
    )

    loader = ProfileLoader(profiles_dir, cache_ttl_seconds=0.0)

    class _Settings:
        watcher_default_interval_seconds = 300

    runner = WatcherRunner(
        settings=_Settings(),
        repo=repo,
        profile_loader=loader,
        profiles_dir=profiles_dir,
    )

    fresh_b = MailMessage(
        provider="gmail", account="a2", address="a2@x",
        message_id="NEW-B", subject="hi", sender="z@y", snippet="",
        received_at=datetime.now(tz=timezone.utc),
    )

    def fake_build(self_loader, cfg):  # noqa: ARG001
        class FakeProvider:
            account = cfg.name
            address = cfg.address
            name = cfg.provider

            def list_new_since(self, last_id, *, limit=20):
                if self.account == "a1":
                    raise MailProviderError("simulated IMAP failure on a1")
                return [fresh_b]

        return FakeProvider()

    notify_calls: list[Any] = []

    async def fake_notify(meta, items):
        notify_calls.append((meta, items))

    monkeypatch.setattr(AccountLoader, "build", fake_build)
    monkeypatch.setattr(runner, "_notify", fake_notify)

    [meta] = loader.iter_watchers()
    await runner._tick_mail(meta)

    # a1 failure logged, a2 alert delivered
    assert len(notify_calls) == 1
    _, items = notify_calls[0]
    assert [m.message_id for m in items] == ["NEW-B"]
    # a2 high-water mark advanced; a1 stayed at OLD-A
    assert await repo.get_watcher_state("demo", "w", account="a2") == "NEW-B"
    assert await repo.get_watcher_state("demo", "w", account="a1") == "OLD-A"
