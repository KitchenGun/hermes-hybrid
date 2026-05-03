#!/usr/bin/env python3
"""extract_applied_companies.py — Pull applied-company names from Gmail.

Searches kangkk9714@gmail.com (gmail_kk account) for application-related
threads in the last N days and extracts company names from the From
header / subject. Output: JSON list of normalized company tokens —
consumed by morning_game_jobs to set raw.applied=true.

Usage:
    python3 extract_applied_companies.py --days 180 --output /tmp/applied.json

Exit codes:
    0  output file written (may contain 0 items)
    1  invalid args / Gmail token missing / token expired
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

GMAIL_SCOPES = ["https://www.googleapis.com/auth/gmail.readonly"]
# script: <root>/profiles/kk_job/skills/research/job_crawler/scripts/<this>.py
# parents[6] = repo root → secrets/ holds gmail_kk_token.json
SECRETS_DIR = Path(__file__).resolve().parents[6] / "secrets"
DEFAULT_TOKEN = SECRETS_DIR / "gmail_kk_token.json"

# Search query: 한국어 채용 키워드. -in:promotions 로 광고 채용 메일 제외.
SEARCH_Q = (
    'subject:(지원 OR 접수 OR 서류 OR 합격 OR 불합격 OR 면접 OR '
    'application OR interview OR offer) '
    '-in:spam -in:trash -category:promotions'
)


def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Extract applied-company set from Gmail.")
    p.add_argument("--days", type=int, default=180,
                   help="Look back this many days (default 180)")
    p.add_argument("--output", required=True, help="Output JSON file path")
    p.add_argument("--token-file", default=str(DEFAULT_TOKEN),
                   help="Gmail OAuth token JSON path (default: secrets/gmail_kk_token.json)")
    p.add_argument("--max-results", type=int, default=200,
                   help="Max Gmail messages to scan (default 200)")
    return p.parse_args()


def _load_service(token_file: str):  # type: ignore[no-untyped-def]
    try:
        from google.auth.transport.requests import Request  # type: ignore
        from google.oauth2.credentials import Credentials  # type: ignore
        from googleapiclient.discovery import build  # type: ignore
    except ImportError as e:
        print(f"[applied] google-api packages missing: {e}", file=sys.stderr)
        return None

    p = Path(token_file)
    if not p.exists():
        print(f"[applied] token not found: {p}", file=sys.stderr)
        return None

    try:
        creds = Credentials.from_authorized_user_file(str(p), GMAIL_SCOPES)
        if not creds.valid:
            if creds.expired and creds.refresh_token:
                creds.refresh(Request())
                p.write_text(creds.to_json(), encoding="utf-8")
            else:
                print("[applied] gmail credentials expired — re-auth required",
                      file=sys.stderr)
                return None
        return build("gmail", "v1", credentials=creds, cache_discovery=False)
    except Exception as e:  # noqa: BLE001
        print(f"[applied] gmail auth failed: {e}", file=sys.stderr)
        return None


_FROM_NAME_RE = re.compile(r'^"?([^"<]+?)"?\s*<')
_DOMAIN_RE = re.compile(r"@([a-zA-Z0-9.\-]+)")
# 회사명 후보 정규화: 흔한 채용플랫폼/일반 단어 제외
_PLATFORM_WORDS = {
    "wanted", "saramin", "jobkorea", "rocketpunch", "programmers",
    "jumpit", "linkedin", "gmail", "naver", "google", "noreply",
    "no-reply", "info", "admin", "support", "career", "careers",
    "recruit", "recruiting", "hr", "talent", "team", "system",
}


def _extract_company_from_address(from_header: str) -> str | None:
    """Best-effort: From: '회사명 <id@domain>' or 'X <name@회사도메인>'."""
    if not from_header:
        return None
    m = _FROM_NAME_RE.search(from_header)
    name = m.group(1).strip() if m else ""
    if name and not any(p in name.lower() for p in _PLATFORM_WORDS):
        return _normalize_company(name)
    m = _DOMAIN_RE.search(from_header)
    if m:
        domain = m.group(1)
        primary = domain.split(".")[0]
        if primary.lower() in _PLATFORM_WORDS:
            return None
        return _normalize_company(primary)
    return None


def _normalize_company(name: str) -> str:
    """Strip parentheses, suffixes, whitespace; keep core token."""
    n = re.sub(r"[\(\[].*?[\)\]]", "", name)
    n = re.sub(r"\b(inc|llc|ltd|corp|co\.?|주식회사|㈜|\(주\))\b", "", n,
               flags=re.IGNORECASE)
    return re.sub(r"\s+", " ", n).strip().lower()


def main() -> int:
    args = _parse_args()
    svc = _load_service(args.token_file)
    if svc is None:
        # Graceful: still write empty list so caller doesn't break.
        try:
            with open(args.output, "w", encoding="utf-8") as f:
                json.dump([], f)
            print("[applied] wrote empty list (gmail unavailable)",
                  file=sys.stderr)
            return 0
        except OSError as e:
            print(f"[applied] cannot write output: {e}", file=sys.stderr)
            return 1

    q = f"{SEARCH_Q} newer_than:{args.days}d"
    try:
        resp = svc.users().messages().list(  # type: ignore[attr-defined]
            userId="me", q=q, maxResults=args.max_results,
        ).execute()
    except Exception as e:  # noqa: BLE001
        print(f"[applied] gmail list failed: {e}", file=sys.stderr)
        # write empty list so cron can still proceed
        with open(args.output, "w", encoding="utf-8") as f:
            json.dump([], f)
        return 0

    ids = [m["id"] for m in resp.get("messages") or []]
    print(f"[applied] gmail matched {len(ids)} messages (q={SEARCH_Q[:60]}...)",
          file=sys.stderr)

    companies: set[str] = set()
    for mid in ids:
        try:
            m = svc.users().messages().get(  # type: ignore[attr-defined]
                userId="me", id=mid, format="metadata",
                metadataHeaders=["From", "Subject"],
            ).execute()
        except Exception as e:  # noqa: BLE001
            print(f"[applied] msg {mid} fetch failed: {e}", file=sys.stderr)
            continue
        headers = {h["name"].lower(): h["value"]
                   for h in m.get("payload", {}).get("headers", [])}
        company = _extract_company_from_address(headers.get("from", ""))
        if company and len(company) >= 2:
            companies.add(company)

    out = sorted(companies)
    print(f"[applied] extracted {len(out)} unique companies", file=sys.stderr)
    try:
        with open(args.output, "w", encoding="utf-8") as f:
            json.dump(out, f, ensure_ascii=False, indent=2)
    except OSError as e:
        print(f"[applied] cannot write output: {e}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
