"""JobClassifier — message → job_type.

Two-stage strategy (per :class:`ClassifierConfig.fast_keyword_path`):

  1. **Fast keyword path** (default on). Each :class:`JobType` lists
     ``keywords_ko`` + ``keywords_en``; first substring match wins.
     Confidence 0.9 — keywords are explicit user intent so trust them.

  2. **LLM fallback**. A tiny model (default ``qwen2.5:3b-instruct``)
     is prompted with the full job_type list and the message, asked
     to output a single label. Confidence 0.6 — LLMs are good but not
     perfect at single-label classification.

If both stages fail (fast path no hit + LLM call errored / output
unparseable / unknown label), we return ``classifier.fallback_job_type``
with confidence 0.3 ("low — safe default").

Concurrency: stateless — safe to share across coroutines.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Literal

from src.job_factory.registry import JobTypeRegistry
from src.llm.adapters.base import (
    AdapterRequest,
    ChatMessage,
    LLMAdapter,
)

log = logging.getLogger(__name__)

ClassifyMethod = Literal["keyword", "llm", "fallback"]


@dataclass(frozen=True)
class JobClassification:
    """Result of :meth:`JobClassifier.classify`.

    Attributes:
        job_type: The chosen job_type name (always exists in registry).
        confidence: 0.0–1.0 — keyword=0.9, llm=0.6, fallback=0.3.
        method: How we got here.
        matched_keyword: For ``method="keyword"``, the substring that
            matched. Empty otherwise.
        llm_raw_output: For ``method="llm"``, the raw text from the
            classifier LLM (for ledger / debugging). Empty otherwise.
    """

    job_type: str
    confidence: float
    method: ClassifyMethod
    matched_keyword: str = ""
    llm_raw_output: str = ""


# Heuristic: the LLM's response should fit in this many tokens. We're
# asking for a single label so 64 is generous.
_LLM_MAX_TOKENS = 64

# Confidence per method.
_CONF_KEYWORD = 0.9
_CONF_LLM = 0.6
_CONF_FALLBACK = 0.3


class JobClassifier:
    """Decide job_type from a user message.

    Args:
        registry: The job_type registry; provides keywords + classifier
            config + valid label set.
        llm_adapter: Tiny LLM for the fallback path. ``None`` ⇒ skip LLM
            stage entirely (just keyword + fallback). Pass an adapter
            constructed from :class:`OllamaAdapter` wrapping the
            ``classifier.llm_model`` from the registry config.
    """

    def __init__(
        self,
        registry: JobTypeRegistry,
        *,
        llm_adapter: LLMAdapter | None = None,
    ):
        self._registry = registry
        self._llm = llm_adapter
        self._cfg = registry.classifier
        self._all_names = list(registry.names())
        # Pre-build a lower-case keyword index for fast substring matching.
        self._kw_index: list[tuple[str, str]] = []  # (lc_keyword, job_type)
        for jt in registry.job_types.values():
            for kw in jt.keywords_ko:
                if kw:
                    self._kw_index.append((kw.lower(), jt.name))
            for kw in jt.keywords_en:
                if kw:
                    self._kw_index.append((kw.lower(), jt.name))

    async def classify(self, message: str) -> JobClassification:
        text = message.strip()

        # Stage 1: keyword fast path.
        if self._cfg.fast_keyword_path:
            hit = self._keyword_match(text)
            if hit is not None:
                kw, jt = hit
                return JobClassification(
                    job_type=jt,
                    confidence=_CONF_KEYWORD,
                    method="keyword",
                    matched_keyword=kw,
                )

        # Stage 2: LLM fallback.
        if self._llm is not None:
            llm_result = await self._llm_classify(text)
            if llm_result is not None:
                return llm_result

        # Stage 3: hard fallback.
        return JobClassification(
            job_type=self._cfg.fallback_job_type,
            confidence=_CONF_FALLBACK,
            method="fallback",
        )

    # ---- stage 1: keyword -------------------------------------------------

    def _keyword_match(self, text: str) -> tuple[str, str] | None:
        """First substring match in ``text`` (case-insensitive). Longest
        keyword wins on tie — gives "코드 리뷰" priority over "코드"."""
        lc = text.lower()
        best: tuple[str, str] | None = None
        best_len = 0
        for kw_lc, jt in self._kw_index:
            if kw_lc in lc and len(kw_lc) > best_len:
                best = (kw_lc, jt)
                best_len = len(kw_lc)
        return best

    # ---- stage 2: LLM -----------------------------------------------------

    async def _llm_classify(
        self, text: str,
    ) -> JobClassification | None:
        """Returns None on any failure (timeout, parse error, unknown
        label). Caller falls through to stage 3."""
        prompt = self._build_llm_prompt(text)
        try:
            resp = await self._llm.generate(
                AdapterRequest(
                    messages=[ChatMessage(role="user", content=prompt)],
                    max_tokens=_LLM_MAX_TOKENS,
                    temperature=0.0,
                    timeout_s=float(self._cfg.llm_timeout_seconds),
                )
            )
        except Exception as e:  # noqa: BLE001
            log.warning(
                "classifier.llm_failed",
                extra={"err": str(e)},
            )
            return None

        label = self._extract_label(resp.text)
        if label is None:
            log.info(
                "classifier.llm_label_unknown",
                extra={"raw": resp.text[:120]},
            )
            return None

        return JobClassification(
            job_type=label,
            confidence=_CONF_LLM,
            method="llm",
            llm_raw_output=resp.text,
        )

    def _build_llm_prompt(self, message: str) -> str:
        labels = ", ".join(self._all_names)
        return (
            "You are a routing classifier. Output exactly ONE label "
            "from the list below — no other text.\n\n"
            f"Labels: {labels}\n\n"
            f"Message: {message!r}\n\n"
            "Output only the label."
        )

    def _extract_label(self, text: str) -> str | None:
        """Find a registered job_type label inside the LLM output.

        Strategy:
          1. If the trimmed response equals a label exactly → return it.
          2. Otherwise scan all known labels for substring presence in
             the lowercased text. Among matches, prefer the longest
             label (so "code_review" beats "simple_chat" if both
             appear, and "schedule_logging" beats "schedule" should we
             ever add the latter).

        Returns None when no registered label appears anywhere in the
        response — safer than guessing.
        """
        if not text:
            return None
        cleaned = text.strip().strip("`'\".? \n\t").lower()

        # Exact-match short-circuit.
        if cleaned in self._registry.job_types:
            return cleaned

        # Substring scan over the registry — only return labels we know.
        candidates = [
            name for name in self._registry.job_types
            if name in cleaned
        ]
        if not candidates:
            return None
        # Longest label wins (specificity).
        return max(candidates, key=len)
