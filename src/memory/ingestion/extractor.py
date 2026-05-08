"""Rule-based candidate extractor (P1, v1).

LLM-based extraction is gated behind ``memory_llm_extraction_enabled``
and not implemented here. v1 looks for explicit shapes that are
unambiguous enough to extract without a model:

- ``# heading``-prefixed sections that name a known type label
  (``# Decision: ...``, ``# Failure pattern: ...``)
- bullet lists under a recognised heading
- prompt templates wrapped in fenced code blocks following a
  ``Prompt:`` / ``Template:`` marker
- explicit ``Preferred:`` / ``선호:`` / ``Style:`` / ``스타일:`` lines
  that map to ``user_preference`` or ``response_style``

Each extracted candidate carries:

- ``type``: one of ALLOWED_TYPES (writer.py)
- ``title``: short label used for slug
- ``body``: extracted content
- ``confidence``: low | medium | high — defaults to ``medium`` for
  rule matches; only ``user_correction`` source items can produce
  ``high`` (and then via the writer's user_correction branch, not
  here).
- ``tags``: detected secondary categorisation (e.g. ``language``,
  ``formatting``)
- ``source_sha16`` / ``source``: copied through from the SourceItem

Confidence is intentionally capped at ``medium`` for v1 because rule
extraction does not understand intent — it only matches surface
shapes. Users can override via the ``user_correction`` source path
(handled by the writer, not here).
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Iterable, Iterator, Sequence

from .manifest import sha16
from .sources import SourceItem


@dataclass(frozen=True, slots=True)
class Candidate:
    type: str
    title: str
    body: str
    confidence: str = "medium"
    tags: tuple[str, ...] = ()
    source: str = ""
    source_sha16: str = ""
    source_path: str = ""


# ---------------------------------------------------------------------------
# Rule patterns
# ---------------------------------------------------------------------------
# Heading-line type tags. Matches a markdown ATX heading whose label
# starts with a recognised keyword. Order matters: more specific
# patterns first so "Failure pattern:" wins over a bare "pattern" hit.
_HEADING_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"^#{1,6}\s*(?:failure[\s_-]?pattern|실패\s*패턴)\s*[:：]?\s*(.+)$", re.IGNORECASE), "failure_pattern"),
    (re.compile(r"^#{1,6}\s*(?:decision|결정)\s*[:：]?\s*(.+)$", re.IGNORECASE), "decision"),
    (re.compile(r"^#{1,6}\s*(?:project[\s_-]?context|프로젝트\s*컨텍스트|프로젝트\s*맥락)\s*[:：]?\s*(.+)$", re.IGNORECASE), "project_context"),
    (re.compile(r"^#{1,6}\s*(?:prompt[\s_-]?template|prompt|프롬프트(?:\s*템플릿)?)\s*[:：]?\s*(.+)$", re.IGNORECASE), "prompt_template"),
    (re.compile(r"^#{1,6}\s*(?:reusable[\s_-]?skill|skill|스킬)\s*[:：]?\s*(.+)$", re.IGNORECASE), "reusable_skill"),
    (re.compile(r"^#{1,6}\s*(?:response[\s_-]?style|응답\s*스타일|스타일)\s*[:：]?\s*(.+)$", re.IGNORECASE), "response_style"),
    (re.compile(r"^#{1,6}\s*(?:user[\s_-]?preference|preference|preferred|선호)\s*[:：]?\s*(.+)$", re.IGNORECASE), "user_preference"),
)

# Inline single-line declarations (no heading). These are weaker
# evidence so they cap at confidence "low".
_INLINE_PATTERNS: tuple[tuple[re.Pattern[str], str, tuple[str, ...]], ...] = (
    (re.compile(r"(?im)^\s*(?:preferred|선호)\s*[:：]\s*(.+)$"), "user_preference", ()),
    (re.compile(r"(?im)^\s*(?:style|스타일)\s*[:：]\s*(.+)$"), "response_style", ()),
    (re.compile(r"(?im)^\s*(?:language|언어)\s*[:：]\s*(.+)$"), "user_preference", ("language",)),
    (re.compile(r"(?im)^\s*(?:formatting|포맷)\s*[:：]\s*(.+)$"), "user_preference", ("formatting",)),
)

# Fenced code block following a "Prompt:" or "Template:" marker.
_PROMPT_FENCE_RE = re.compile(
    r"(?:Prompt|Template|프롬프트|템플릿)\s*[:：]\s*\n```[a-zA-Z0-9_-]*\n(?P<body>.*?)\n```",
    re.DOTALL | re.IGNORECASE,
)


# ---------------------------------------------------------------------------
# Extractor
# ---------------------------------------------------------------------------
class RuleExtractor:
    """Stateless rule-based extractor.

    Usage::

        for item in source.iter_items():
            for cand in RuleExtractor().extract(item):
                writer.write(...)
    """

    def extract(self, item: SourceItem) -> list[Candidate]:
        out: list[Candidate] = []
        sha = sha16(item.content)
        seen_titles: set[tuple[str, str]] = set()

        # 1. Heading-driven sections.
        out.extend(
            self._dedupe(
                self._extract_headings(item.content, item.source, sha, item.source_path),
                seen_titles,
            )
        )

        # 2. Inline single-line declarations (lower confidence).
        out.extend(
            self._dedupe(
                self._extract_inlines(item.content, item.source, sha, item.source_path),
                seen_titles,
            )
        )

        # 3. Prompt fences.
        out.extend(
            self._dedupe(
                self._extract_prompts(item.content, item.source, sha, item.source_path),
                seen_titles,
            )
        )

        return out

    # ------------------------------------------------------------------
    # Pattern handlers
    # ------------------------------------------------------------------
    @staticmethod
    def _dedupe(
        gen: Iterable[Candidate], seen: set[tuple[str, str]]
    ) -> Iterator[Candidate]:
        for c in gen:
            key = (c.type, c.title.casefold())
            if key in seen:
                continue
            seen.add(key)
            yield c

    @staticmethod
    def _extract_headings(
        text: str, source: str, sha: str, source_path: str
    ) -> Iterator[Candidate]:
        # Walk lines; when a heading matches a pattern, capture body
        # until the next heading of any depth.
        lines = text.splitlines()
        i = 0
        while i < len(lines):
            line = lines[i]
            matched_type = None
            matched_title = ""
            for pat, type_ in _HEADING_PATTERNS:
                m = pat.match(line)
                if m:
                    matched_type = type_
                    matched_title = m.group(1).strip()
                    break
            if not matched_type:
                i += 1
                continue
            # Capture body until next heading or EOF.
            body_lines: list[str] = []
            j = i + 1
            while j < len(lines):
                if lines[j].lstrip().startswith("#"):
                    break
                body_lines.append(lines[j])
                j += 1
            body = "\n".join(body_lines).strip()
            if not matched_title:
                matched_title = body.split("\n", 1)[0][:80] if body else "(untitled)"
            if matched_title or body:
                yield Candidate(
                    type=matched_type,
                    title=matched_title or "(untitled)",
                    body=body or matched_title,
                    confidence="medium",
                    source=source,
                    source_sha16=sha,
                    source_path=source_path,
                )
            i = j

    @staticmethod
    def _extract_inlines(
        text: str, source: str, sha: str, source_path: str
    ) -> Iterator[Candidate]:
        for pat, type_, tags in _INLINE_PATTERNS:
            for m in pat.finditer(text):
                value = m.group(1).strip()
                if not value:
                    continue
                title = value[:80]
                yield Candidate(
                    type=type_,
                    title=title,
                    body=value,
                    confidence="low",
                    tags=tags,
                    source=source,
                    source_sha16=sha,
                    source_path=source_path,
                )

    @staticmethod
    def _extract_prompts(
        text: str, source: str, sha: str, source_path: str
    ) -> Iterator[Candidate]:
        for m in _PROMPT_FENCE_RE.finditer(text):
            body = m.group("body").strip()
            if not body:
                continue
            title = body.split("\n", 1)[0][:80].strip() or "(prompt)"
            yield Candidate(
                type="prompt_template",
                title=title,
                body=body,
                confidence="medium",
                source=source,
                source_sha16=sha,
                source_path=source_path,
            )


__all__ = ["Candidate", "RuleExtractor"]
