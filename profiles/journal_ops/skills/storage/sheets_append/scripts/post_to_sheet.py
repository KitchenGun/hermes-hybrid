#!/usr/bin/env python3
"""post_to_sheet.py — Google Sheets appender via Apps Script doPost webhook.

Forked from `profiles/calendar_ops/skills/messaging/discord_notify/scripts/post_webhook.py`
— same urllib + retry + User-Agent pattern, but POSTs `{"rows": [...]}`
to an Apps Script web app instead of a Discord embed.

Usage:
    printf '%s' '{"Date":"2026-04-29","Activity":"운동",...}' | python3 post_to_sheet.py
    printf '%s' '[{...},{...}]' | python3 post_to_sheet.py
    python3 post_to_sheet.py --dry-run < payload.json

Env:
    GOOGLE_SHEETS_WEBHOOK_URL    (required, except for --dry-run)
    JOURNAL_ALERT_WEBHOOK_URL    (optional) — Discord webhook fired only when
                                 the sheets append fails. Best-effort: alert
                                 errors do NOT change this script's exit code.

Exit codes:
    0  appended successfully — stdout: "OK rows=N"
    1  invalid input (not valid JSON, missing required fields, etc.)
    2  webhook URL missing
    3  HTTP failure / Apps Script returned ok=false / 4xx-5xx
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from typing import Any
from urllib import request as urlreq
from urllib.error import HTTPError, URLError

# Column order — MUST match the first row of the target Google Sheet exactly.
# Apps Script's setValues() takes a list-of-list, and we drive that order from
# here so a column reorder in the sheet doesn't silently misalign data.
COLUMNS: list[str] = [
    "Date",
    "Weekday",
    "Start Time",
    "End Time",
    "Duration",
    "Activity",
    "Category",
    "Subcategory",
    "Tags",
    "Priority",
    "Focus Score",
    "Energy Score",
    "Difficulty",
    "Deep Work",
    "Planned/Unplanned",
    "Outcome",
    "Notes",
    "Location",
    "Device",
    "Interruptions",
    "Mood",
]
assert len(COLUMNS) == 21, f"COLUMNS length must be 21, got {len(COLUMNS)}"

MAX_RETRIES = 1            # 5xx만 1회 재시도 (4xx는 즉시 실패)
TIMEOUT_SEC = 15
USER_AGENT = "hermes-hybrid-journal/0.1 (+https://github.com/anthropics/hermes-hybrid)"

# Discord embed colors (decimal).
DISCORD_RED = 0xED4245
DISCORD_ALERT_TIMEOUT_SEC = 5
DISCORD_ALERT_MAX_DESCRIPTION = 4000


def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Append journal rows to Google Sheet via Apps Script webhook.",
    )
    p.add_argument(
        "--dry-run",
        action="store_true",
        help="Validate input + render payload, do NOT POST.",
    )
    return p.parse_args()


def _normalize(rows: list[dict[str, Any]]) -> list[list[Any]]:
    """dict 리스트 → list-of-list (COLUMNS 순서 강제).

    - Tags가 list면 ", "로 join하여 문자열 변환 (Apps Script가 dict→cell이라 list 처리 곤란)
    - 누락 필드는 빈 문자열 ""
    - bool/int/None은 그대로 전달 (Apps Script가 그대로 setValue)
    """
    out: list[list[Any]] = []
    for r in rows:
        if not isinstance(r, dict):
            print(
                f"[post_to_sheet] row is not an object: {type(r).__name__}",
                file=sys.stderr,
            )
            continue
        row: list[Any] = []
        for col in COLUMNS:
            val = r.get(col)
            if col == "Tags" and isinstance(val, list):
                val = ", ".join(str(t) for t in val)
            elif val is None:
                val = ""
            row.append(val)
        out.append(row)
    return out


def _load_input() -> list[dict[str, Any]]:
    """stdin에서 JSON 읽기. 단일 객체면 [obj]로 wrap."""
    raw = sys.stdin.read().strip()
    if not raw:
        print("[post_to_sheet] stdin empty", file=sys.stderr)
        sys.exit(1)
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as e:
        print(f"[post_to_sheet] invalid JSON: {e}", file=sys.stderr)
        sys.exit(1)
    if isinstance(data, dict):
        return [data]
    if isinstance(data, list):
        if not data:
            print("[post_to_sheet] empty array", file=sys.stderr)
            sys.exit(1)
        return data
    print(
        f"[post_to_sheet] expected object or array, got {type(data).__name__}",
        file=sys.stderr,
    )
    sys.exit(1)


def _validate_required(rows: list[dict[str, Any]]) -> None:
    """Required 필드 검증 — Date, Activity가 누락되면 exit 1."""
    for i, r in enumerate(rows):
        if not isinstance(r, dict):
            continue
        missing = []
        if not r.get("Date"):
            missing.append("Date")
        if not r.get("Activity"):
            missing.append("Activity")
        if missing:
            print(
                f"[post_to_sheet] row {i}: missing required field(s): "
                f"{', '.join(missing)}",
                file=sys.stderr,
            )
            sys.exit(1)


def _send(url: str, payload: dict[str, Any]) -> tuple[int, dict[str, Any] | None]:
    """POST payload to url. Returns (http_status, parsed_body_or_None).

    Retry policy: 5xx → 1초 대기 후 1회 재시도. 4xx는 즉시 반환.
    """
    data = json.dumps(payload).encode("utf-8")
    req = urlreq.Request(
        url,
        data=data,
        headers={
            "Content-Type": "application/json",
            "User-Agent": USER_AGENT,
        },
        method="POST",
    )

    last_err: Exception | None = None
    for attempt in range(MAX_RETRIES + 1):
        try:
            with urlreq.urlopen(req, timeout=TIMEOUT_SEC) as resp:
                body_raw = resp.read().decode("utf-8", errors="replace")
                try:
                    body = json.loads(body_raw) if body_raw else None
                except json.JSONDecodeError:
                    body = None
                return resp.status, body
        except HTTPError as e:
            last_err = e
            if 500 <= e.code < 600 and attempt < MAX_RETRIES:
                time.sleep(1.0)
                continue
            # 4xx 즉시 실패. 5xx는 마지막 시도면 종료.
            try:
                body_raw = e.read().decode("utf-8", errors="replace")
                body = json.loads(body_raw) if body_raw else None
            except (json.JSONDecodeError, AttributeError):
                body = None
            print(
                f"[post_to_sheet] HTTP {e.code}: {e.reason}",
                file=sys.stderr,
            )
            return e.code, body
        except URLError as e:
            last_err = e
            if attempt < MAX_RETRIES:
                time.sleep(1.0)
                continue
            break
        except Exception as e:  # pragma: no cover  # noqa: BLE001
            last_err = e
            break

    print(
        f"[post_to_sheet] failed after {MAX_RETRIES + 1} attempt(s): {last_err}",
        file=sys.stderr,
    )
    return -1, None


def _truncate(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    return text[: limit - 1] + "…"


def _fire_alert(*, status: int, body: dict[str, Any] | None, row_count: int) -> None:
    """Best-effort Discord alert on sheets_append failure.

    Posts a red embed to ``JOURNAL_ALERT_WEBHOOK_URL`` if set, summarizing
    HTTP status, response body, and how many rows were attempted. Errors
    here are swallowed (printed to stderr) so they don't change the script's
    exit code — the caller's exit semantics drive the LLM's user-facing
    failure message in #일기, and this alert is a parallel ops signal.

    Why duplicate Discord notification? The bot's reply lands in #일기
    (mixed with successful entries). A separate ops channel via this
    webhook lets you spot integration regressions without scrolling.
    """
    alert_url = os.environ.get("JOURNAL_ALERT_WEBHOOK_URL", "").strip()
    if not alert_url:
        return  # silently no-op when not configured

    error_msg = ""
    if isinstance(body, dict):
        error_msg = str(body.get("error", ""))[:300]

    description_lines = [
        f"**Status**: HTTP {status}",
        f"**Rows attempted**: {row_count}",
    ]
    if error_msg:
        description_lines.append(f"**Apps Script error**: `{error_msg}`")

    embed = {
        "title": "🔴 sheets_append failed",
        "description": _truncate(
            "\n".join(description_lines), DISCORD_ALERT_MAX_DESCRIPTION
        ),
        "color": DISCORD_RED,
        "footer": {"text": "journal_ops · post_to_sheet.py"},
    }
    payload = {"embeds": [embed]}
    data = json.dumps(payload).encode("utf-8")
    req = urlreq.Request(
        alert_url,
        data=data,
        headers={"Content-Type": "application/json", "User-Agent": USER_AGENT},
        method="POST",
    )
    try:
        with urlreq.urlopen(req, timeout=DISCORD_ALERT_TIMEOUT_SEC) as resp:
            # Discord returns 204 No Content on success.
            if resp.status not in (200, 204):
                print(
                    f"[post_to_sheet] alert webhook returned {resp.status}",
                    file=sys.stderr,
                )
    except (HTTPError, URLError) as e:
        print(f"[post_to_sheet] alert webhook failed: {e}", file=sys.stderr)
    except Exception as e:  # noqa: BLE001  # alert is best-effort
        print(f"[post_to_sheet] alert webhook unexpected error: {e}", file=sys.stderr)


def main() -> int:
    args = _parse_args()
    rows_in = _load_input()
    _validate_required(rows_in)
    rows_norm = _normalize(rows_in)

    if not rows_norm:
        print("[post_to_sheet] no valid rows after normalization", file=sys.stderr)
        return 1

    payload = {"rows": rows_norm}

    if args.dry_run:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return 0

    webhook_url = os.environ.get("GOOGLE_SHEETS_WEBHOOK_URL", "").strip()
    if not webhook_url:
        print(
            "[post_to_sheet] GOOGLE_SHEETS_WEBHOOK_URL env var not set",
            file=sys.stderr,
        )
        return 2

    status, body = _send(webhook_url, payload)

    # Apps Script 응답: {"ok": bool, "rows": N, "error": "..."}
    if status == 200 and isinstance(body, dict) and body.get("ok") is True:
        n = body.get("rows", len(rows_norm))
        print(f"OK rows={n}")
        return 0

    # 200이지만 ok=false 또는 4xx/5xx → 실패 + 운영 경보
    err_msg = ""
    if isinstance(body, dict):
        err_msg = str(body.get("error", ""))[:200]
    print(
        f"[post_to_sheet] sheets_append_failed: status={status}, error={err_msg}",
        file=sys.stderr,
    )
    _fire_alert(status=status, body=body, row_count=len(rows_norm))
    return 3


if __name__ == "__main__":
    sys.exit(main())
