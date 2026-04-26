#!/usr/bin/env python3
"""Bootstrap and verify mail accounts declared in profiles/<id>/accounts.yaml.

Examples
--------
python scripts/setup_mail_accounts.py --list
python scripts/setup_mail_accounts.py --account personal_gmail --auth
python scripts/setup_mail_accounts.py --account personal_naver --auth
python scripts/setup_mail_accounts.py --account personal_gmail --test
"""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

# Windows console (cp949) chokes on Korean / em-dash output. Same fix as run_bot.py.
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

# python-dotenv is in the base deps; load .env so password_env works.
try:
    from dotenv import load_dotenv  # type: ignore
    load_dotenv(PROJECT_ROOT / ".env")
except Exception:  # noqa: BLE001
    pass

from src.skills.mail import PROVIDERS  # noqa: E402
from src.skills.mail.accounts import AccountConfig, AccountLoader  # noqa: E402
from src.skills.mail.base import MailProviderError  # noqa: E402
from src.skills.mail.gmail import GMAIL_SCOPES  # noqa: E402


DEFAULT_PROFILE = "mail_ops"


def _find_loader(profile: str) -> AccountLoader:
    profile_dir = PROJECT_ROOT / "profiles" / profile
    if not profile_dir.exists():
        sys.exit(f"profile directory not found: {profile_dir}")
    return AccountLoader(profile_dir)


def cmd_list(loader: AccountLoader) -> int:
    accounts = loader.load()
    if not accounts:
        print(f"(no accounts declared in {loader.accounts_file})")
        return 0
    print(f"{'NAME':<20} {'PROVIDER':<8} {'ADDRESS':<32} STATUS")
    print("-" * 80)
    for cfg in accounts.values():
        status = _status(cfg)
        print(f"{cfg.name:<20} {cfg.provider:<8} {cfg.address:<32} {status}")
    return 0


def _status(cfg: AccountConfig) -> str:
    if cfg.provider == "gmail":
        token = cfg.raw.get("token_file")
        if not token:
            return "MISSING token_file"
        path = Path(token)
        if not path.is_absolute():
            path = PROJECT_ROOT / path
        return "OK (token present)" if path.exists() else f"NO TOKEN — run --auth"
    if cfg.provider == "naver":
        env = cfg.raw.get("password_env")
        if not env:
            return "MISSING password_env"
        return "OK (env set)" if os.environ.get(env, "").strip() else f"NO PASSWORD ({env} unset)"
    return "?"


def cmd_auth(loader: AccountLoader, account_name: str) -> int:
    accounts = loader.load()
    cfg = accounts.get(account_name)
    if cfg is None:
        sys.exit(f"account '{account_name}' not found in {loader.accounts_file}")
    if cfg.provider == "gmail":
        return _auth_gmail(cfg)
    if cfg.provider == "naver":
        return _auth_naver(cfg)
    sys.exit(f"unsupported provider: {cfg.provider}")


def _auth_gmail(cfg: AccountConfig) -> int:
    try:
        from google_auth_oauthlib.flow import InstalledAppFlow  # type: ignore
    except ImportError:
        sys.exit(
            "google-auth-oauthlib is required for --auth. "
            "Install with: pip install -e .[mail]"
        )
    cred_path_str = cfg.raw.get("credentials_file")
    if not cred_path_str:
        sys.exit(
            f"account '{cfg.name}' missing 'credentials_file' "
            "(path to OAuth client secret JSON from Google Cloud Console)."
        )
    cred_path = Path(cred_path_str)
    if not cred_path.is_absolute():
        cred_path = PROJECT_ROOT / cred_path
    if not cred_path.exists():
        sys.exit(
            f"credentials_file not found: {cred_path}. "
            "Download an OAuth 2.0 Desktop client JSON from Google Cloud Console."
        )
    token_path_str = cfg.raw.get("token_file")
    if not token_path_str:
        sys.exit(f"account '{cfg.name}' missing 'token_file' path")
    token_path = Path(token_path_str)
    if not token_path.is_absolute():
        token_path = PROJECT_ROOT / token_path
    token_path.parent.mkdir(parents=True, exist_ok=True)

    print(
        f"\n>>> Browser will open. Sign in as **{cfg.address}** "
        "(use an incognito window if you have multiple Google accounts).\n"
    )
    flow = InstalledAppFlow.from_client_secrets_file(str(cred_path), GMAIL_SCOPES)
    creds = flow.run_local_server(port=0)
    token_path.write_text(creds.to_json(), encoding="utf-8")
    print(f"\n[OK] token saved to {token_path}")
    return 0


def _auth_naver(cfg: AccountConfig) -> int:
    pw_env = cfg.raw.get("password_env") or "NAVER_APP_PASSWORD"
    print(
        "\nNaver account setup checklist (no automated auth — IMAP only):\n"
        "  1. Naver Mail → 환경설정 → POP3/IMAP 설정 → IMAP/SMTP 사용 ON\n"
        "  2. Naver 계정 → 보안설정 → 2단계 인증 ON\n"
        "  3. Naver 계정 → 보안설정 → 애플리케이션 비밀번호 관리 → 발급\n"
        f"  4. 발급된 8자리 문자열을 .env 의 {pw_env}=... 에 저장\n"
        f"  5. python scripts/setup_mail_accounts.py --account {cfg.name} --test\n"
    )
    return 0


def cmd_test(loader: AccountLoader, account_name: str) -> int:
    accounts = loader.load()
    cfg = accounts.get(account_name)
    if cfg is None:
        sys.exit(f"account '{account_name}' not found")
    try:
        provider = loader.build(cfg)
        items = provider.list_new_since(None, limit=5)
    except MailProviderError as e:
        sys.exit(f"[FAIL] {e}")
    print(f"[OK] {cfg.name} ({cfg.provider}) — {len(items)} INBOX items fetched.")
    for m in items:
        print(f"  - {m.received_at.isoformat()}  {m.sender}  ::  {m.subject}")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Mail account bootstrap & verify.")
    parser.add_argument("--profile", default=DEFAULT_PROFILE, help="profile id (default: mail_ops)")
    parser.add_argument("--list", action="store_true", help="list configured accounts")
    parser.add_argument("--account", help="account name (matches accounts.yaml `name`)")
    parser.add_argument("--auth", action="store_true", help="run OAuth (gmail) or print Naver guide")
    parser.add_argument("--test", action="store_true", help="fetch a few INBOX messages")
    args = parser.parse_args()

    loader = _find_loader(args.profile)
    if args.list:
        return cmd_list(loader)
    if args.account and args.auth:
        return cmd_auth(loader, args.account)
    if args.account and args.test:
        return cmd_test(loader, args.account)
    parser.print_help()
    return 1


if __name__ == "__main__":
    sys.exit(main())
