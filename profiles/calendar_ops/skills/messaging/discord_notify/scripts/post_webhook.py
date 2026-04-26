#!/usr/bin/env python3
"""
post_webhook.py — Discord webhook sender for calendar_ops profile.

Usage:
    echo "본문 내용" | python post_webhook.py --title "오늘 일정 브리핑"
    python post_webhook.py --title "..." --body-file ./briefing.txt
    python post_webhook.py --title "..." --color 0xED4245 --footer "error"

Env:
    DISCORD_BRIEFING_WEBHOOK_URL  (required)

Exit codes:
    0  sent successfully
    1  invalid input
    2  webhook URL missing
    3  network/HTTP error after retries
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from datetime import datetime, timezone
from typing import Optional
from urllib import request as urlreq
from urllib.error import HTTPError, URLError

DEFAULT_COLOR = 0x5865F2  # Discord blurple
MAX_DESCRIPTION = 4000
MAX_TITLE = 256
MAX_FOOTER = 2048
MAX_RETRIES = 2
TIMEOUT_SEC = 10


def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Post message to Discord via webhook.")
    p.add_argument("--title", required=True, help="Embed title (max 256 chars)")
    p.add_argument("--body-file", help="File path for body; if omitted, read stdin")
    p.add_argument(
        "--color",
        type=lambda x: int(x, 0),
        default=DEFAULT_COLOR,
        help="Embed color as int or 0xHEX (default: 0x5865F2)",
    )
    p.add_argument("--footer", default=None, help="Optional footer text")
    p.add_argument(
        "--timestamp",
        default=None,
        help="ISO8601 timestamp; default = now (UTC)",
    )
    p.add_argument(
        "--dry-run",
        action="store_true",
        help="Validate and print payload without sending",
    )
    return p.parse_args()


def _load_body(path: Optional[str]) -> str:
    if path:
        with open(path, "r", encoding="utf-8") as f:
            return f.read()
    return sys.stdin.read()


def _truncate(text: str, limit: int, suffix: str = "…") -> str:
    if len(text) <= limit:
        return text
    return text[: limit - len(suffix)] + suffix


def _build_payload(
    title: str,
    body: str,
    color: int,
    footer: Optional[str],
    timestamp: Optional[str],
) -> dict:
    ts = timestamp or datetime.now(timezone.utc).isoformat()
    embed = {
        "title": _truncate(title, MAX_TITLE),
        "description": _truncate(body, MAX_DESCRIPTION),
        "color": color,
        "timestamp": ts,
    }
    if footer:
        embed["footer"] = {"text": _truncate(footer, MAX_FOOTER)}
    return {"embeds": [embed]}


USER_AGENT = "hermes-hybrid-cron/0.1 (+https://github.com/anthropics/hermes-hybrid)"


def _send(url: str, payload: dict) -> int:
    data = json.dumps(payload).encode("utf-8")
    # User-Agent is required: Discord's Cloudflare frontend blocks the
    # default Python-urllib UA with error 1010 (Forbidden).
    req = urlreq.Request(
        url,
        data=data,
        headers={
            "Content-Type": "application/json",
            "User-Agent": USER_AGENT,
        },
        method="POST",
    )

    last_err: Optional[Exception] = None
    for attempt in range(MAX_RETRIES + 1):
        try:
            with urlreq.urlopen(req, timeout=TIMEOUT_SEC) as resp:
                return resp.status
        except HTTPError as e:
            last_err = e
            if e.code == 429:
                retry_after = float(e.headers.get("X-RateLimit-Reset-After", "1"))
                time.sleep(min(retry_after, 5.0))
                continue
            if 500 <= e.code < 600:
                time.sleep(1.0 * (attempt + 1))
                continue
            # 4xx 외 기타: 즉시 실패
            print(
                f"[post_webhook] HTTP {e.code}: {e.reason}",
                file=sys.stderr,
            )
            return e.code
        except URLError as e:
            last_err = e
            time.sleep(1.0 * (attempt + 1))
            continue
        except Exception as e:  # pragma: no cover
            last_err = e
            break

    print(
        f"[post_webhook] 실패 after {MAX_RETRIES + 1} attempts: {last_err}",
        file=sys.stderr,
    )
    return -1


def main() -> int:
    args = _parse_args()

    webhook_url = os.environ.get("DISCORD_BRIEFING_WEBHOOK_URL", "").strip()
    if not webhook_url and not args.dry_run:
        print(
            "[post_webhook] DISCORD_BRIEFING_WEBHOOK_URL 환경변수 없음",
            file=sys.stderr,
        )
        return 2

    try:
        body = _load_body(args.body_file)
    except OSError as e:
        print(f"[post_webhook] body 읽기 실패: {e}", file=sys.stderr)
        return 1

    if not body.strip():
        print("[post_webhook] body empty", file=sys.stderr)
        return 1

    payload = _build_payload(
        title=args.title,
        body=body,
        color=args.color,
        footer=args.footer,
        timestamp=args.timestamp,
    )

    if args.dry_run:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return 0

    status = _send(webhook_url, payload)
    if status == 204:
        return 0
    return 3


if __name__ == "__main__":
    sys.exit(main())
