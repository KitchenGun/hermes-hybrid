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

# Korean → canonical column-name aliases. Local LLMs (qwen3:14b, gpt-oss:20b)
# occasionally emit Korean keys despite the Title-Case examples in the prompt
# — extracting "활동":"미용실 펌" instead of "Activity":"미용실 펌". Without
# this map, _validate_required fails with "missing Activity" even though the
# value is right there under a different key, which masquerades as a user
# error in the Discord reply. Keep this list narrow: only the keys the prompt
# explicitly enumerates in Korean (Date / Activity / time / score / etc.).
KOREAN_KEY_ALIASES: dict[str, str] = {
    "날짜": "Date",
    "요일": "Weekday",
    "시작시간": "Start Time",
    "시작 시간": "Start Time",
    "시작": "Start Time",
    "종료시간": "End Time",
    "종료 시간": "End Time",
    "종료": "End Time",
    "지속시간": "Duration",
    "소요시간": "Duration",
    "활동": "Activity",
    "활동명": "Activity",
    "분류": "Category",
    "카테고리": "Category",
    "세부분류": "Subcategory",
    "태그": "Tags",
    "우선순위": "Priority",
    "집중도": "Focus Score",
    "집중": "Focus Score",
    "컨디션": "Energy Score",
    "에너지": "Energy Score",
    "난이도": "Difficulty",
    "딥워크": "Deep Work",
    "계획여부": "Planned/Unplanned",
    "결과": "Outcome",
    "메모": "Notes",
    "노트": "Notes",
    "장소": "Location",
    "위치": "Location",
    "기기": "Device",
    "방해": "Interruptions",
    "기분": "Mood",
}

# Score columns must be 1~5 ints (or null). Out-of-range values used to flow
# through and surface as cell strings like "10" — the prompt's prose says the
# LLM should clamp, but local models slip up (e.g. "컨디션 10" → Energy
# Score: 10). Drop invalid scores to None at the script boundary so a single
# rogue value can't poison Apps Script's setValues row.
SCORE_COLUMNS: tuple[str, ...] = ("Focus Score", "Energy Score", "Difficulty")

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


def _merge_planned_unplanned(r: dict[str, Any]) -> dict[str, Any]:
    """Local LLMs sometimes split the single "Planned/Unplanned" column into
    two separate boolean fields ({"Planned": null, "Unplanned": null}) — the
    slash in the column name reads like an "or" instruction. Merge them back
    into the canonical single field so the row aligns with the sheet header.
    """
    if "Planned/Unplanned" in r:
        return r  # already canonical — leave as-is
    p_keys = ("Planned", "planned")
    u_keys = ("Unplanned", "unplanned")
    has_split = any(k in r for k in p_keys + u_keys)
    if not has_split:
        return r
    p_val = next((r[k] for k in p_keys if k in r), None)
    u_val = next((r[k] for k in u_keys if k in r), None)
    merged: Any = None
    if isinstance(p_val, str) and p_val.strip():
        merged = "Planned"
    elif p_val is True:
        merged = "Planned"
    elif isinstance(u_val, str) and u_val.strip():
        merged = "Unplanned"
    elif u_val is True:
        merged = "Unplanned"
    cleaned = {k: v for k, v in r.items() if k not in p_keys + u_keys}
    cleaned["Planned/Unplanned"] = merged
    return cleaned


def _canonicalize_keys(r: dict[str, Any]) -> dict[str, Any]:
    """Map common case/snake variants ("date", "start_time", "deep_work") and
    Korean aliases ("활동", "컨디션") to canonical Title-Case ("Date",
    "Start Time", "Activity", "Energy Score"). Local LLMs (gpt-oss:20b,
    qwen3) often emit lowercased, snake_cased, or Korean keys despite the
    Title-Case examples in the prompt; rejecting those just because of case
    turned a working extraction into a "missing required field" failure and
    a 300s timeout retry. Normalize defensively at the script boundary so
    the LLM's output style doesn't gate row insertion.
    """
    r = _merge_planned_unplanned(r)
    canonical = {col.lower().replace(" ", "_").replace("/", "_"): col for col in COLUMNS}
    out: dict[str, Any] = {}
    for k, v in r.items():
        # 1) exact Title-Case hit — keep
        if k in canonical.values():
            out[k] = v
            continue
        # 2) Korean alias (e.g. "활동" → "Activity"). Match key as-is and
        # also after stripping internal whitespace, since LLMs sometimes
        # emit "시작 시간" with or without the space.
        ko = KOREAN_KEY_ALIASES.get(k) or KOREAN_KEY_ALIASES.get(k.replace(" ", ""))
        if ko is not None:
            out[ko] = v
            continue
        # 3) normalize: lowercase, strip spaces/underscores/slashes
        norm = k.lower().replace(" ", "_").replace("/", "_").replace("-", "_")
        col = canonical.get(norm)
        if col is not None:
            out[col] = v
        else:
            out[k] = v  # unknown key — preserve so caller can debug
    return out


def _coerce_score(val: Any) -> Any:
    """Coerce a Focus/Energy/Difficulty value to a 1~5 int or None.

    Accepts ints in range, numeric strings ("4"), and clamps anything outside
    1~5 (including the "컨디션 10" case observed in production). The prompt
    instructs the LLM to keep scores within 1~5, but local models occasionally
    pass through the user's raw number — this is the last safety net before
    the value reaches Apps Script.
    """
    if val is None:
        return None
    if isinstance(val, bool):  # bool is an int subclass — reject explicitly
        return None
    if isinstance(val, int):
        return val if 1 <= val <= 5 else None
    if isinstance(val, float):
        if val.is_integer():
            i = int(val)
            return i if 1 <= i <= 5 else None
        return None
    if isinstance(val, str):
        s = val.strip()
        if not s:
            return None
        try:
            i = int(s)
        except ValueError:
            return None
        return i if 1 <= i <= 5 else None
    return None


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
        r = _canonicalize_keys(r)
        row: list[Any] = []
        for col in COLUMNS:
            val = r.get(col)
            if col in SCORE_COLUMNS:
                val = _coerce_score(val)
            if col == "Tags" and isinstance(val, list):
                val = ", ".join(str(t) for t in val)
            elif val is None:
                val = ""
            row.append(val)
        out.append(row)
    return out


def _load_input() -> list[dict[str, Any]]:
    """stdin에서 JSON 읽기. 단일 객체면 [obj]로 wrap.

    `strict=False` lets raw control characters (real newlines/tabs) survive
    inside string values. Local LLMs occasionally pretty-print Notes with a
    real newline instead of the escaped ``\\n`` sequence, and strict parsing
    would reject the whole row over what's effectively a cosmetic glitch.
    """
    raw = sys.stdin.read().strip()
    if not raw:
        print("[post_to_sheet] stdin empty", file=sys.stderr)
        sys.exit(1)
    try:
        data = json.loads(raw, strict=False)
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
    """Required 필드 검증 — Date, Activity가 누락되면 exit 1.

    Runs on the canonicalized dict so case/underscore variants from the LLM
    (date, start_time) match the Title-Case requirement.
    """
    for i, r in enumerate(rows):
        if not isinstance(r, dict):
            continue
        canon = _canonicalize_keys(r)
        missing = []
        if not canon.get("Date"):
            missing.append("Date")
        if not canon.get("Activity"):
            missing.append("Activity")
        if missing:
            # Include the raw keys the LLM actually sent — without this, a
            # repeat of the "활동":"미용실 펌" → missing-Activity case looks
            # like a phantom user error from the bot's reply alone, with no
            # way to tell whether canonicalization or the LLM's output is at
            # fault. Sorted for deterministic ordering across Python builds.
            raw_keys = sorted(r.keys()) if isinstance(r, dict) else []
            print(
                f"[post_to_sheet] row {i}: missing required field(s): "
                f"{', '.join(missing)} | got keys: {raw_keys}",
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
