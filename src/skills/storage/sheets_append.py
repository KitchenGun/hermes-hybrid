"""Apps Script doPost Sheets appender — Phase 22 reborn.

Phase 8 (2026-05-06) 에서 폐기된 ``profiles/journal_ops/skills/storage/
sheets_append/scripts/post_to_sheet.py`` 의 핵심 로직을 import 가능한
모듈로 환생. 변경점:

- subprocess CLI 가 아닌 함수로 호출 (``append_rows``).
- COLUMNS 는 호출자가 주입 — journal_ops (21열) / kk_job 등 공유.
- Korean alias / score coercion / Planned/Unplanned merge / 5xx retry
  동작은 legacy 와 1:1.
- 실패 알림 webhook 은 caller 가 결정 (옵션).

URL 은 caller 가 명시 전달 — env var 직조회 안 함 (테스트 용이성).
"""
from __future__ import annotations

import json
import time
from dataclasses import dataclass
from typing import Any
from urllib import request as urlreq
from urllib.error import HTTPError, URLError

from src.obs import get_logger

log = get_logger(__name__)

JOURNAL_COLUMNS: tuple[str, ...] = (
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
)

# Required columns (legacy: Date, Activity). Date 는 caller 가 prefix 로
# 미리 채워주므로 사실상 Activity 만 검증.
JOURNAL_REQUIRED: tuple[str, ...] = ("Date", "Activity")
SCORE_COLUMNS: tuple[str, ...] = ("Focus Score", "Energy Score", "Difficulty")

# Local LLMs occasionally emit Korean keys despite Title-Case examples.
# Narrow alias map — only keys the prompt enumerates in Korean.
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

MAX_RETRIES = 1
TIMEOUT_SEC = 15
USER_AGENT = "hermes-hybrid-journal/0.2"
DISCORD_RED = 0xED4245
DISCORD_ALERT_TIMEOUT_SEC = 5


class SheetsAppendError(RuntimeError):
    """Raised on validation / HTTP failure."""

    def __init__(self, msg: str, *, status: int = -1, body: Any = None):
        super().__init__(msg)
        self.status = status
        self.body = body


@dataclass(frozen=True)
class SheetsAppendResult:
    rows_written: int
    status: int
    body: Any


def _merge_planned_unplanned(r: dict[str, Any]) -> dict[str, Any]:
    """Merge split ``Planned`` / ``Unplanned`` keys into the canonical single
    field — the slash in the column name reads like an "or" instruction to
    some local LLMs.
    """
    if "Planned/Unplanned" in r:
        return r
    p_keys = ("Planned", "planned")
    u_keys = ("Unplanned", "unplanned")
    if not any(k in r for k in p_keys + u_keys):
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


def canonicalize_keys(
    r: dict[str, Any],
    columns: tuple[str, ...] = JOURNAL_COLUMNS,
) -> dict[str, Any]:
    """Map case/snake variants and Korean aliases to canonical Title-Case keys."""
    r = _merge_planned_unplanned(r)
    canonical = {col.lower().replace(" ", "_").replace("/", "_"): col for col in columns}
    title_set = set(columns)
    out: dict[str, Any] = {}
    for k, v in r.items():
        if k in title_set:
            out[k] = v
            continue
        ko = KOREAN_KEY_ALIASES.get(k) or KOREAN_KEY_ALIASES.get(k.replace(" ", ""))
        if ko is not None:
            out[ko] = v
            continue
        norm = k.lower().replace(" ", "_").replace("/", "_").replace("-", "_")
        col = canonical.get(norm)
        if col is not None:
            out[col] = v
        else:
            out[k] = v
    return out


def coerce_score(val: Any) -> Any:
    """Coerce Focus / Energy / Difficulty to a 1-5 int or None."""
    if val is None:
        return None
    if isinstance(val, bool):
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


def normalize_rows(
    rows: list[dict[str, Any]],
    columns: tuple[str, ...] = JOURNAL_COLUMNS,
    score_columns: tuple[str, ...] = SCORE_COLUMNS,
) -> list[list[Any]]:
    """dict rows → list-of-list aligned with ``columns`` order."""
    out: list[list[Any]] = []
    for r in rows:
        if not isinstance(r, dict):
            continue
        canon = canonicalize_keys(r, columns)
        row: list[Any] = []
        for col in columns:
            val = canon.get(col)
            if col in score_columns:
                val = coerce_score(val)
            if col == "Tags" and isinstance(val, list):
                val = ", ".join(str(t) for t in val)
            elif val is None:
                val = ""
            row.append(val)
        out.append(row)
    return out


def validate_required(
    rows: list[dict[str, Any]],
    required: tuple[str, ...] = JOURNAL_REQUIRED,
    columns: tuple[str, ...] = JOURNAL_COLUMNS,
) -> None:
    """Raise SheetsAppendError on missing required fields."""
    for i, r in enumerate(rows):
        if not isinstance(r, dict):
            raise SheetsAppendError(f"row {i}: not an object ({type(r).__name__})")
        canon = canonicalize_keys(r, columns)
        missing = [k for k in required if not canon.get(k)]
        if missing:
            raw_keys = sorted(r.keys())
            raise SheetsAppendError(
                f"row {i}: missing required field(s): {', '.join(missing)} | "
                f"got keys: {raw_keys}"
            )


def _post_with_retry(url: str, payload: dict[str, Any]) -> tuple[int, Any]:
    data = json.dumps(payload).encode("utf-8")
    req = urlreq.Request(
        url,
        data=data,
        headers={"Content-Type": "application/json", "User-Agent": USER_AGENT},
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
            try:
                body_raw = e.read().decode("utf-8", errors="replace")
                body = json.loads(body_raw) if body_raw else None
            except (json.JSONDecodeError, AttributeError):
                body = None
            return e.code, body
        except URLError as e:
            last_err = e
            if attempt < MAX_RETRIES:
                time.sleep(1.0)
                continue
            break
        except Exception as e:  # noqa: BLE001
            last_err = e
            break
    raise SheetsAppendError(
        f"sheets_append network failure: {last_err}", status=-1, body=None
    )


def _truncate(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    return text[: limit - 1] + "…"


def fire_alert(
    *,
    alert_url: str,
    title: str,
    status: int,
    body: Any,
    row_count: int,
    footer: str = "sheets_append",
) -> None:
    """Best-effort Discord red embed on failure. Errors swallowed."""
    if not alert_url:
        return
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
        "title": f"🔴 {title}",
        "description": _truncate("\n".join(description_lines), 4000),
        "color": DISCORD_RED,
        "footer": {"text": footer},
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
            if resp.status not in (200, 204):
                log.warning("sheets_append.alert_status", status=resp.status)
    except (HTTPError, URLError) as e:
        log.warning("sheets_append.alert_failed", err=str(e))
    except Exception as e:  # noqa: BLE001
        log.warning("sheets_append.alert_unexpected", err=str(e))


def append_rows(
    rows: list[dict[str, Any]],
    *,
    webhook_url: str,
    columns: tuple[str, ...] = JOURNAL_COLUMNS,
    required: tuple[str, ...] = JOURNAL_REQUIRED,
    alert_url: str = "",
    alert_title: str = "sheets_append failed",
) -> SheetsAppendResult:
    """Validate, normalize and POST rows to an Apps Script doPost endpoint.

    On Apps Script ``ok=false`` or non-200 the alert webhook (if any) fires
    once and SheetsAppendError is raised.
    """
    if not webhook_url:
        raise SheetsAppendError("webhook_url is empty", status=-1, body=None)
    if not rows:
        raise SheetsAppendError("rows is empty", status=-1, body=None)

    validate_required(rows, required=required, columns=columns)
    normalized = normalize_rows(rows, columns=columns)
    if not normalized:
        raise SheetsAppendError("no valid rows after normalization", status=-1, body=None)

    payload = {"rows": normalized}
    status, body = _post_with_retry(webhook_url, payload)
    if status == 200 and isinstance(body, dict) and body.get("ok") is True:
        n = int(body.get("rows", len(normalized)) or len(normalized))
        log.info("sheets_append.ok", rows=n, status=status)
        return SheetsAppendResult(rows_written=n, status=status, body=body)

    err_msg = ""
    if isinstance(body, dict):
        err_msg = str(body.get("error", ""))[:200]
    log.warning(
        "sheets_append.failed",
        status=status,
        error=err_msg,
        rows_attempted=len(normalized),
    )
    if alert_url:
        fire_alert(
            alert_url=alert_url,
            title=alert_title,
            status=status,
            body=body,
            row_count=len(normalized),
        )
    raise SheetsAppendError(
        f"sheets_append failed: status={status}, error={err_msg}",
        status=status,
        body=body,
    )


__all__ = [
    "JOURNAL_COLUMNS",
    "JOURNAL_REQUIRED",
    "SCORE_COLUMNS",
    "SheetsAppendError",
    "SheetsAppendResult",
    "append_rows",
    "canonicalize_keys",
    "coerce_score",
    "fire_alert",
    "normalize_rows",
    "validate_required",
]
