"""Activity extractor — 자연어 발화 → 24-필드 JSON.

Master Claude CLI 어댑터에 system+user 프롬프트를 던져 list-or-dict JSON 을
받는다. legacy SOUL.md / log_activity.yaml prompt 의 **추출 부분만** 남기고
응답 포맷·코칭·terminal 호출 지시 등 master 지향 instruction 은 모두 제거.

응답 텍스트가 JSON 으로 파싱되지 않으면 best-effort 첫 ``{...}`` / ``[...]``
구간만 잘라 재시도. 실패 시 ``ExtractionError`` 발생 — 호출자가 사용자에게
재질문을 보낸다.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

from src.claude_adapter import ClaudeCodeAdapter
from src.obs import get_logger

log = get_logger(__name__)

DEFAULT_TZ = ZoneInfo("Asia/Seoul")
WEEKDAY_EN = ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday")

EXTRACT_SYSTEM = """\
You are a personal activity logger. Convert the user's Korean/English message
into a 24-field activity record (single object) or array of records (multiple
activities). Output STRICT JSON only — no markdown, no commentary.

Schema (Title-Case keys exactly as written, slashes/spaces preserved):
  Date (YYYY-MM-DD), Weekday (Monday..Sunday|null),
  Start Time (HH:MM 24h|null), End Time (HH:MM|null),
  Duration (int minutes|null), Activity (string, required),
  Category (Work|Study|Health|Life|Rest|Leisure|Admin|Social|Other|null),
  Subcategory (string|null), Tags (array of strings|null),
  Priority (High|Medium|Low|null),
  Focus Score (1..5|null), Energy Score (1..5|null), Difficulty (1..5|null),
  Deep Work (true|false|null), Planned/Unplanned (Planned|Unplanned|null),
  Outcome (string|null), Notes (string|null), Location (string|null),
  Device (PC|Mobile|Tablet|Paper|Other|null), Interruptions (string|null),
  Mood (string|null).

Rules:
- The user message will be prefixed with ``[현재 날짜: YYYY-MM-DD (요일),
  현재 시각: HH:MM KST]``. Resolve "지금/방금/어제/오늘" against this.
- If the user does not specify a time, set Start Time to the prefix's time
  and Date to the prefix's date. NEVER leave Date null.
- Score fields (Focus/Energy/Difficulty): only fill when the user explicitly
  said or strongly implied a 1-5 number. NEVER guess.
- "컨디션 N" → Energy Score N. "기상" = waking up (Health), not weather.
- ``Planned/Unplanned`` is a SINGLE key with the slash literal — never split
  into two boolean keys.
- Unknown / unmentioned fields → null.
- Multiple activities in one message → JSON array. Single → JSON object.
- Output a SINGLE-LINE JSON. Escape any newline inside string values as \\n.
- No prose, no code fences."""

JSON_FENCE_RE = re.compile(r"```(?:json)?\s*(\{.*?\}|\[.*?\])\s*```", re.DOTALL)


class ExtractionError(RuntimeError):
    """Raised when the LLM output cannot be parsed into a list of activity dicts."""


@dataclass(frozen=True)
class ExtractionResult:
    rows: list[dict[str, Any]]
    raw_response: str
    duration_ms: int
    model: str


def build_now_prefix(now: datetime | None = None) -> str:
    """Returns the ``[현재 날짜: ..., 현재 시각: ...]`` prefix injected into prompts."""
    now = now or datetime.now(DEFAULT_TZ)
    weekday = WEEKDAY_EN[now.weekday()]
    return (
        f"[현재 날짜: {now.strftime('%Y-%m-%d')} ({weekday}), "
        f"현재 시각: {now.strftime('%H:%M')} KST]"
    )


def _extract_json_blob(raw: str) -> Any:
    """Best-effort JSON object/array extraction from noisy LLM output."""
    text = raw.strip()
    if not text:
        raise ExtractionError("empty LLM response")
    fence = JSON_FENCE_RE.search(text)
    if fence:
        text = fence.group(1)
    try:
        return json.loads(text, strict=False)
    except json.JSONDecodeError:
        pass
    # Find first { or [ and matching last } or ].
    for opener, closer in (("{", "}"), ("[", "]")):
        start = text.find(opener)
        end = text.rfind(closer)
        if start != -1 and end > start:
            try:
                return json.loads(text[start : end + 1], strict=False)
            except json.JSONDecodeError:
                continue
    raise ExtractionError(f"LLM output is not parseable JSON: {raw[:200]!r}")


def _ensure_rows(parsed: Any) -> list[dict[str, Any]]:
    if isinstance(parsed, dict):
        return [parsed]
    if isinstance(parsed, list):
        if not parsed:
            raise ExtractionError("LLM returned an empty list")
        if not all(isinstance(x, dict) for x in parsed):
            raise ExtractionError("LLM list items must all be objects")
        return list(parsed)
    raise ExtractionError(f"LLM returned {type(parsed).__name__}, expected object or array")


def _backfill_date(rows: list[dict[str, Any]], now: datetime) -> list[dict[str, Any]]:
    """If LLM left Date or Start Time null/empty, fill with the prefix now — legacy behavior.

    Apps Script and the appender treat empty Date as a missing-required failure;
    backfilling here mirrors the legacy "Date 누락은 발생할 수 없다" rule. Start Time
    is also backfilled so a bare "운동 기분 좋음" gets the current time, matching
    the user's "일자/시간 미기록시 현재 시간" 정책.
    """
    today = now.strftime("%Y-%m-%d")
    now_hm = now.strftime("%H:%M")
    out = []
    for r in rows:
        if not isinstance(r, dict):
            out.append(r)
            continue
        copy = dict(r)
        date_val = copy.get("Date") or copy.get("date") or copy.get("날짜")
        if not (isinstance(date_val, str) and date_val.strip()):
            copy["Date"] = today
        start_val = copy.get("Start Time") or copy.get("start_time") or copy.get("시작")
        if not (isinstance(start_val, str) and start_val.strip()):
            copy["Start Time"] = now_hm
        out.append(copy)
    return out


async def extract_activities(
    user_message: str,
    *,
    adapter: ClaudeCodeAdapter,
    now: datetime | None = None,
    model: str | None = None,
    timeout_ms: int | None = None,
) -> ExtractionResult:
    """Run the master Claude CLI to produce a list of 24-field rows.

    The caller (pipeline) handles validation/normalization via sheets_append.
    """
    now = now or datetime.now(DEFAULT_TZ)
    prefix = build_now_prefix(now)
    user_payload = (
        f"{prefix}\n\n"
        f"발화: {user_message}\n\n"
        "위 발화를 24-필드 JSON 으로 변환하여 한 줄 JSON 으로만 응답하라."
    )
    full_prompt = f"{EXTRACT_SYSTEM}\n\n{user_payload}"
    result = await adapter.run(prompt=full_prompt, model=model, timeout_ms=timeout_ms)
    parsed = _extract_json_blob(result.text)
    rows = _ensure_rows(parsed)
    rows = _backfill_date(rows, now)
    log.info(
        "journal.extracted",
        rows=len(rows),
        duration_ms=result.duration_ms,
        model=result.model_name,
    )
    return ExtractionResult(
        rows=rows,
        raw_response=result.text,
        duration_ms=result.duration_ms,
        model=result.model_name,
    )


__all__ = [
    "ExtractionError",
    "ExtractionResult",
    "build_now_prefix",
    "extract_activities",
]
