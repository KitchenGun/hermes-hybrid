"""Feedback Keywords — Phase 20 (2026-05-07).

사용자 텍스트에서 짧은 긍정/부정 신호를 추출. default OFF (false-positive
우려) — config.feedback_keyword_match_enabled 명시 ON 시에만 호출.

Word-boundary 매칭이 아니라 lowercased 부분 문자열 매칭 — 한국어 활용형
(틀려요, 틀리네) 까지 잡으려면 정규 한국어 형태소 분석이 필요한데 그건
오버엔지니어링. 짧은 명시 키워드만 사용 → 본문에 우연히 들어간 경우는
최소.

negative 가 positive 보다 우선 — "good but wrong" 류 혼합 신호는 negative
로 분류 (개선 신호가 더 가치 있음).
"""
from __future__ import annotations

from typing import Literal


_NEGATIVE = (
    "wrong", "incorrect", "그게 아니야",
    "틀려", "틀렸", "틀림", "오답", "잘못", "다시 해", "다시해",
    "엉터리", "이상해",
)

_POSITIVE = (
    "perfect", "그거 맞아",
    "정확", "감사합니다", "감사해요", "고마워", "좋아요", "최고",
    "완벽",
)


def match_text(text: str) -> Literal["positive", "negative"] | None:
    """Return the dominant feedback polarity, or None.

    The check is lowercased substring containment. negative wins ties.
    """
    if not text:
        return None
    low = text.lower()
    has_neg = any(k in low for k in _NEGATIVE)
    if has_neg:
        return "negative"
    if any(k in low for k in _POSITIVE):
        return "positive"
    return None


__all__ = ["match_text"]
