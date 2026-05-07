"""Discord reply formatter — 한 줄 + 메타 + 코칭.

legacy SOUL.md 의 **출력 규칙** (블록 A 저장확인 / 블록 B 코칭 / 복수 활동)
을 코드로 옮겨, LLM 이 응답 포맷까지 책임지지 않게 한다 — 순수 텍스트
조립이라 결정적이고, null 슬립 같은 회귀를 차단할 수 있다.
"""
from __future__ import annotations

from typing import Any

# 우선순위 표 (legacy: log_activity.yaml). 위가 더 가치 높음.
COACHING_PRIORITY: list[tuple[str, str]] = [
    ("End Time", "종료 시각: \"10시 30분까지\" 또는 \"30분간 했어\""),
    ("Focus Score", "집중도: \"집중도 4\" (1=거의 못함 ~ 5=완전 몰입)"),
    ("Energy Score", "컨디션: \"컨디션 3\" (1=매우 피곤 ~ 5=매우 좋음)"),
    ("Mood", "기분: \"피곤했어\" / \"상쾌했어\""),
    ("Difficulty", "난이도: \"난이도 4\" (1=매우 쉬움 ~ 5=매우 어려움)"),
    ("Deep Work", "Deep Work: \"깊게 집중했어\" 또는 \"잡무였어\""),
    ("Outcome", "결과: 한 줄 — \"버그 수정 완료\", \"10페이지 읽음\""),
    ("Location", "장소: \"집에서\" / \"카페에서\""),
    ("Category", "분류: \"운동(Health)이야\""),
    ("Subcategory", "세부 분류: \"코딩→리팩토링\", \"운동→유산소\""),
    ("Notes", "메모: 추가 메모"),
    ("Tags", "태그: 키워드"),
    ("Priority", "우선순위: \"중요한 일이었어\""),
    ("Interruptions", "방해: \"두 번 끊겼어\""),
    ("Device", "기기: \"PC로 했어\""),
]


def _is_filled(val: Any) -> bool:
    if val is None:
        return False
    if isinstance(val, str):
        return bool(val.strip())
    if isinstance(val, list):
        return bool(val)
    return True


def _meta_line(row: dict[str, Any]) -> str:
    """Category·Focus·Energy·Difficulty·Deep Work·Mood·Location 순서로 채워진
    것만 가운뎃점(·)으로 연결. 모두 비면 빈 문자열.
    """
    parts: list[str] = []
    cat = row.get("Category")
    if _is_filled(cat):
        parts.append(str(cat))
    focus = row.get("Focus Score")
    if _is_filled(focus):
        parts.append(f"Focus {focus}")
    energy = row.get("Energy Score")
    if _is_filled(energy):
        parts.append(f"Energy {energy}")
    diff = row.get("Difficulty")
    if _is_filled(diff):
        parts.append(f"난이도 {diff}")
    if row.get("Deep Work") is True:
        parts.append("Deep Work")
    mood = row.get("Mood")
    if _is_filled(mood):
        parts.append(f"기분: {mood}")
    loc = row.get("Location")
    if _is_filled(loc):
        parts.append(f"장소: {loc}")
    return " · ".join(parts)


def _time_activity_line(row: dict[str, Any]) -> str:
    activity = row.get("Activity") or "(기록)"
    start = row.get("Start Time")
    end = row.get("End Time")
    duration = row.get("Duration")
    has_start = _is_filled(start)
    has_end = _is_filled(end)
    if has_start and has_end:
        if _is_filled(duration):
            return f"{start}-{end} ({duration}분) / {activity}"
        return f"{start}-{end} / {activity}"
    if has_start:
        return f"{start} / {activity}"
    if _is_filled(duration):
        return f"{duration}분 / {activity}"
    return str(activity)


def _coaching_block(row: dict[str, Any], limit: int = 3) -> str:
    missing: list[str] = []
    for key, hint in COACHING_PRIORITY:
        if not _is_filled(row.get(key)):
            missing.append(hint)
        if len(missing) >= limit:
            break
    if not missing:
        return ""
    lines = ["", "💡 다음 기록부터 이런 정보도 함께 적어보세요:"]
    for hint in missing:
        lines.append(f"  • {hint}")
    return "\n".join(lines)


def _format_single(row: dict[str, Any]) -> str:
    lines = ["✅ 저장됨", f"  {_time_activity_line(row)}"]
    meta = _meta_line(row)
    if meta:
        lines.append(f"  {meta}")
    body = "\n".join(lines)
    coaching = _coaching_block(row)
    return body + ("\n" + coaching if coaching else "")


def _format_multi(rows: list[dict[str, Any]]) -> str:
    lines = [f"✅ {len(rows)}건 저장됨"]
    for r in rows:
        time_part = ""
        start = r.get("Start Time")
        end = r.get("End Time")
        if _is_filled(start) and _is_filled(end):
            time_part = f"{start}-{end} "
        elif _is_filled(start):
            time_part = f"{start} "
        activity = r.get("Activity") or "(기록)"
        cat = r.get("Category") or "?"
        lines.append(f"  • {time_part}{activity} ({cat})")
    return "\n".join(lines)


def format_success(rows: list[dict[str, Any]]) -> str:
    """1건이면 단일 포맷 + 코칭, 2건 이상이면 multi (코칭 생략)."""
    if not rows:
        return "✅ 저장됨"
    if len(rows) == 1:
        return _format_single(rows[0])
    return _format_multi(rows)


def format_failure(*, reason: str, status: int = -1) -> str:
    """⚠️ 저장 실패 한 줄 (status=-1 이면 http 라인 생략)."""
    lines = [f"⚠️ 저장 실패: {reason}"]
    if status > 0:
        lines.append(f"  http={status}")
    return "\n".join(lines)


def format_extraction_failure(reason: str) -> str:
    return (
        "⚠️ 24-필드 추출 실패. 발화를 다시 정리해주세요.\n"
        f"  사유: {reason}"
    )


__all__ = [
    "format_success",
    "format_failure",
    "format_extraction_failure",
]
