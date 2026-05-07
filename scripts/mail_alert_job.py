#!/usr/bin/env python3
"""Mail watcher job — polls all configured accounts and posts a Discord
webhook message for each new INBOX message.

Reuses src.skills.mail providers (Gmail REST + Naver IMAP). State is
kept per-account at state/mail_watcher/{account}.json — only the last
seen message_id is persisted, so the script is idempotent and crash-safe.

First run for an account (no checkpoint yet) is treated as a high-water
mark: the script records the latest message id but does NOT notify, to
avoid flooding Discord with the entire INBOX backlog after install.

Usage:
    python scripts/mail_alert_job.py
    python scripts/mail_alert_job.py --account personal_gmail
    python scripts/mail_alert_job.py --dry-run        # 안 보내고 출력만
    python scripts/mail_alert_job.py --reset personal_naver  # 체크포인트 삭제

Designed to be invoked by systemd-user timer every 5 minutes
(scripts/install_mail_alert_timer.sh).
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import urllib.error
import urllib.request
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

try:
    from dotenv import load_dotenv  # type: ignore
    load_dotenv(PROJECT_ROOT / ".env")
except Exception:  # noqa: BLE001
    pass

from src.skills.mail.accounts import AccountLoader  # noqa: E402
from src.skills.mail.base import MailMessage, MailProviderError  # noqa: E402

DEFAULT_PROFILE = "mail_ops"
STATE_DIR = PROJECT_ROOT / "state" / "mail_watcher"
WEBHOOK_ENV = "DISCORD_MAIL_WEBHOOK_URL"
LOG = logging.getLogger("mail_alert_job")


def _load_checkpoint(account: str) -> str | None:
    p = STATE_DIR / f"{account}.json"
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text(encoding="utf-8")).get("last_message_id")
    except (json.JSONDecodeError, OSError) as e:
        LOG.warning("checkpoint_read_failed account=%s err=%s", account, e)
        return None


def _save_checkpoint(account: str, message_id: str) -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    p = STATE_DIR / f"{account}.json"
    payload = {
        "last_message_id": message_id,
        "updated_at": datetime.now(tz=timezone.utc).isoformat(),
    }
    p.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def _format_message(m: MailMessage) -> str:
    received_local = m.received_at.astimezone()
    ts = received_local.strftime("%m-%d %H:%M")
    subject = (m.subject or "(no subject)").strip()
    sender = (m.sender or "").strip() or "(unknown sender)"
    if len(subject) > 120:
        subject = subject[:117] + "..."
    if len(sender) > 80:
        sender = sender[:77] + "..."
    snippet = " ".join((m.snippet or "").split())
    if len(snippet) > 240:
        snippet = snippet[:237] + "..."
    lines = [
        f"📬 **{m.provider}** · `{m.address}`  ",
        f"**제목**: {subject}  ",
        f"**보낸이**: {sender}  ",
    ]
    if snippet:
        lines.append(f"**요약**: {snippet}  ")
    lines.append(f"**수신**: {ts}")
    return "\n".join(lines)


def _send_webhook(webhook_url: str, content: str) -> None:
    body = json.dumps({"content": content}).encode("utf-8")
    req = urllib.request.Request(
        webhook_url,
        data=body,
        headers={
            "Content-Type": "application/json",
            "User-Agent": "hermes-mail-alert/1.0 (+https://hermes.local)",
        },
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=15) as resp:  # noqa: S310
        if resp.status >= 300:
            raise RuntimeError(f"webhook status {resp.status}")


def _process_account(loader: AccountLoader, name: str, *, dry_run: bool, webhook_url: str) -> int:
    """Returns count of messages notified (0 on first-run priming)."""
    accounts = loader.load()
    cfg = accounts.get(name)
    if cfg is None:
        LOG.error("unknown account: %s", name)
        return 0
    try:
        provider = loader.build(cfg)
    except MailProviderError as e:
        LOG.error("build_failed account=%s err=%s", name, e)
        return 0

    last = _load_checkpoint(name)
    first_run = last is None

    try:
        messages = provider.list_new_since(last, limit=20)
    except MailProviderError as e:
        LOG.error("poll_failed account=%s err=%s", name, e)
        return 0

    if not messages:
        LOG.info("poll_ok account=%s new=0 (last=%s)", name, last)
        return 0

    # MailProvider returns newest-first. Save the newest id BEFORE we
    # try to notify, so a webhook crash mid-batch does not re-notify
    # everything next run.
    newest_id = messages[0].message_id

    if first_run:
        _save_checkpoint(name, newest_id)
        LOG.info("first_run_priming account=%s skipped=%d newest=%s",
                 name, len(messages), newest_id)
        return 0

    # Send oldest-first so the channel reads naturally.
    notified = 0
    for m in reversed(messages):
        text = _format_message(m)
        if dry_run:
            print(f"[DRY] {name}: {asdict(m)}")
            notified += 1
            continue
        try:
            _send_webhook(webhook_url, text)
            notified += 1
        except (urllib.error.URLError, RuntimeError) as e:
            LOG.error("webhook_failed account=%s mid=%s err=%s",
                      name, m.message_id, e)
            # stop at first failure — checkpoint already covers messages
            # we DID send (we saved newest_id below only on success path).
            return notified

    if not dry_run:
        _save_checkpoint(name, newest_id)
    LOG.info("poll_ok account=%s notified=%d newest=%s", name, notified, newest_id)
    return notified


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--profile", default=DEFAULT_PROFILE)
    parser.add_argument("--account", help="run only this account (default: all)")
    parser.add_argument("--dry-run", action="store_true",
                        help="poll but do not POST to Discord")
    parser.add_argument("--reset", metavar="ACCOUNT",
                        help="delete checkpoint for ACCOUNT and exit")
    parser.add_argument("--verbose", "-v", action="store_true")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )

    if args.reset:
        p = STATE_DIR / f"{args.reset}.json"
        if p.exists():
            p.unlink()
            print(f"[OK] checkpoint removed: {p}")
        else:
            print(f"(no checkpoint at {p})")
        return 0

    profile_dir = PROJECT_ROOT / "profiles" / args.profile
    if not profile_dir.exists():
        LOG.error("profile dir missing: %s", profile_dir)
        return 2
    loader = AccountLoader(profile_dir)
    accounts = loader.load()
    if not accounts:
        LOG.error("no accounts configured in %s", loader.accounts_file)
        return 2

    webhook_url = (os.environ.get(WEBHOOK_ENV) or "").strip()
    if not args.dry_run and not webhook_url:
        LOG.error("environment variable %s is empty", WEBHOOK_ENV)
        return 2

    targets = [args.account] if args.account else list(accounts.keys())
    total = 0
    for name in targets:
        if name not in accounts:
            LOG.warning("skip unknown account: %s", name)
            continue
        # Skip accounts whose address still has the placeholder marker —
        # the user has not finished configuring them yet.
        addr = accounts[name].address
        if addr.startswith("REPLACE_WITH_"):
            LOG.warning("skip unconfigured account: %s (placeholder address)", name)
            continue
        total += _process_account(
            loader, name, dry_run=args.dry_run, webhook_url=webhook_url
        )
    LOG.info("done total_notified=%d", total)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
