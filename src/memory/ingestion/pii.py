"""PII detection — first gate before content reaches processed_memory.

Findings are not corrections — they are flags. The writer (P0-B) takes a
``PIIFinding`` list and stamps ``pii_candidate=true`` on the candidate
item, sending it to ``data/processed_memory/needs_review.md`` instead of
the topic-specific markdown file.

Design choices:

- Regex-based, deliberately conservative. False positives are
  preferred over false negatives — better to send something to NEEDS_REVIEW
  than leak it into a compiled prompt.
- No raw text is logged. ``PIIFinding.match_excerpt`` masks the middle of
  the matched span so debugging output cannot accidentally re-leak the
  secret it just flagged.
- Keyword list catches the cases where regex shape doesn't apply
  (``password is hunter2``, ``token: secret``). The match excerpt for
  keyword findings is the keyword itself; the surrounding payload is
  not echoed.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Iterable, Sequence


# ---------------------------------------------------------------------------
# Regex patterns
# ---------------------------------------------------------------------------
# Email — RFC-ish, intentionally loose.
_EMAIL_RE = re.compile(
    r"\b[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}\b",
)
# Phone — covers KR (010-XXXX-XXXX), US ((XXX) XXX-XXXX or XXX-XXX-XXXX),
# and international (+XX XXX...). Conservative: requires hyphen/space.
_PHONE_RE = re.compile(
    r"""
    (?<![\w@])                       # left boundary (avoid email tails)
    (?:
        \+?\d{1,3}[\s.-]?            # optional country code
    )?
    (?:                              # number body
        \(?\d{2,4}\)?[\s.-]\d{3,4}[\s.-]\d{4}
        |
        \d{2,4}[\s.-]\d{3,4}[\s.-]\d{4}
    )
    (?!\d)
    """,
    re.VERBOSE,
)
# Credit card — 13 to 19 digits, possibly grouped by spaces or dashes.
_CARD_RE = re.compile(
    r"""
    (?<!\d)
    (?:\d[\s-]?){12,18}\d
    (?!\d)
    """,
    re.VERBOSE,
)
# SSN — US shape XXX-XX-XXXX.
_SSN_RE = re.compile(r"\b\d{3}-\d{2}-\d{4}\b")
# API key shapes — Anthropic (sk-ant-…), generic OpenAI (sk-…), AWS (AKIA…),
# GitHub PAT (ghp_, gho_, ghs_, ghu_, ghr_), and PEM private keys.
_API_KEY_RE = re.compile(
    r"""
    \b
    (?:
        sk-ant-[A-Za-z0-9_-]{20,}        # Anthropic
        | sk-[A-Za-z0-9_-]{20,}          # OpenAI-style
        | AKIA[0-9A-Z]{16}               # AWS access key
        | ASIA[0-9A-Z]{16}               # AWS temporary access key
        | gh[opsur]_[A-Za-z0-9]{20,}     # GitHub PAT family
        | xox[baprs]-[A-Za-z0-9-]{10,}   # Slack tokens
    )
    \b
    """,
    re.VERBOSE,
)
_PEM_RE = re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----")

# Keyword markers — used when the surrounding text shape is not a regex
# shape we can recognise. Conservative on purpose; expand as we see misses.
_DEFAULT_KEYWORDS: tuple[str, ...] = (
    "password",
    "passwd",
    "pwd",
    "api_key",
    "apikey",
    "access_token",
    "refresh_token",
    "client_secret",
    "session_cookie",
    "credit card",
    "social security",
)


def _mask_excerpt(text: str, start: int, end: int, *, keep: int = 3) -> str:
    """Return a masked excerpt safe to log.

    Keeps the first ``keep`` and last ``keep`` characters of the matched
    span and replaces the middle with ``…``. For very short matches the
    whole match is masked.
    """
    span = text[start:end]
    if len(span) <= 2 * keep + 1:
        return "…" * len(span)
    return f"{span[:keep]}…{span[-keep:]}"


@dataclass(frozen=True, slots=True)
class PIIFinding:
    """One PII match. ``match_excerpt`` is masked — never raw."""

    category: str       # email | phone | card | ssn | api_key | pem | keyword
    start: int
    end: int
    match_excerpt: str  # masked, safe to log
    detail: str = ""    # extra context, e.g. matched keyword


class PIIScanner:
    """Stateless PII detector.

    Usage::

        scanner = PIIScanner()
        findings = scanner.scan(text)
        if findings:
            # caller marks the item pii_candidate=true and routes it to
            # data/processed_memory/needs_review.md
            ...
    """

    def __init__(
        self,
        *,
        extra_keywords: Sequence[str] = (),
    ) -> None:
        self._keywords: tuple[str, ...] = tuple(_DEFAULT_KEYWORDS) + tuple(
            k.lower() for k in extra_keywords
        )

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------
    def scan(self, text: str) -> list[PIIFinding]:
        if not text:
            return []
        findings: list[PIIFinding] = []
        findings.extend(self._regex_scan(text, _EMAIL_RE, "email"))
        findings.extend(self._regex_scan(text, _PHONE_RE, "phone"))
        findings.extend(self._regex_scan(text, _CARD_RE, "card"))
        findings.extend(self._regex_scan(text, _SSN_RE, "ssn"))
        findings.extend(self._regex_scan(text, _API_KEY_RE, "api_key"))
        findings.extend(self._regex_scan(text, _PEM_RE, "pem"))
        findings.extend(self._keyword_scan(text))
        # Stable order helps tests; sort by (start, category).
        findings.sort(key=lambda f: (f.start, f.category))
        return findings

    def has_pii(self, text: str) -> bool:
        """Cheap check — returns on the first finding."""
        return bool(self.scan(text))

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------
    def _regex_scan(
        self, text: str, pattern: re.Pattern[str], category: str
    ) -> Iterable[PIIFinding]:
        for match in pattern.finditer(text):
            yield PIIFinding(
                category=category,
                start=match.start(),
                end=match.end(),
                match_excerpt=_mask_excerpt(text, match.start(), match.end()),
            )

    def _keyword_scan(self, text: str) -> Iterable[PIIFinding]:
        lower = text.lower()
        for kw in self._keywords:
            start = 0
            while True:
                idx = lower.find(kw, start)
                if idx < 0:
                    break
                # Word-boundary-ish: keyword must not be glued to alnum
                # on both sides (e.g. "password" inside "crosspasswording"
                # doesn't count, but "the password is" does).
                left_ok = idx == 0 or not text[idx - 1].isalnum()
                right_idx = idx + len(kw)
                right_ok = right_idx >= len(text) or not text[right_idx].isalnum()
                if left_ok and right_ok:
                    yield PIIFinding(
                        category="keyword",
                        start=idx,
                        end=right_idx,
                        match_excerpt=kw,
                        detail=f"keyword={kw}",
                    )
                start = idx + len(kw)
