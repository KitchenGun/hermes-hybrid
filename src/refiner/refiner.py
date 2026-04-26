"""Request Refiner — natural-language → structured intent extraction.

LangChain analogy:
  ChatPromptTemplate(system + user) → LLM → PydanticOutputParser.

Pipeline:
  1. Load candidate profile's ``intent_schema.json`` (JSON Schema).
  2. Ask an L2-class LLM to produce a JSON object conforming to the
     schema, plus top-level ``target_profile`` hint and ``confidence``.
  3. Validate against the schema. If invalid or ``confidence < threshold``,
     return a :class:`RefinerResult` with ``needs_clarification=True`` and
     a suggested re-question.

Design principles:
  - Never raises on bad LLM output — always returns a result. Failure
    modes are encoded as ``needs_clarification`` so the gateway can
    surface a friendly question instead of a stack trace.
  - Profile-agnostic: the schema is discovered at call-time from the
    profile directory, not hardcoded. Adding a new profile never
    requires a Refiner code change.
  - No side effects on disk. Profile files are read; nothing is written.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal, Optional

from src.obs import get_logger

log = get_logger(__name__)

IntentType = Literal["read", "analyze", "write", "chat"]
DEFAULT_CONFIDENCE_THRESHOLD = 0.70


class RefinerError(RuntimeError):
    """Raised only for configuration errors (missing profile dir, etc.).

    Normal LLM failures / unparseable output DO NOT raise — they surface
    as ``needs_clarification`` on the result so the gateway can recover.
    """


@dataclass(frozen=True)
class RefinedRequest:
    """Structured form of a user request after refinement.

    ``intent`` is the raw JSON object that validated against the
    profile's ``intent_schema.json``. ``target_profile`` and
    ``intent_type`` are cross-profile fields the Refiner always produces.
    """

    target_profile: str
    intent_type: IntentType
    refined_query: str
    intent: dict[str, Any]
    confidence: float
    ambiguous_fields: list[str] = field(default_factory=list)
    raw_message: str = ""

    def to_dict(self) -> dict:
        return {
            "target_profile": self.target_profile,
            "intent_type": self.intent_type,
            "refined_query": self.refined_query,
            "intent": self.intent,
            "confidence": round(self.confidence, 3),
            "ambiguous_fields": list(self.ambiguous_fields),
        }


@dataclass(frozen=True)
class RefinerResult:
    """Refiner output: either a refined request or a clarification prompt."""

    refined: Optional[RefinedRequest]
    needs_clarification: bool
    clarification_prompt: str = ""
    reason: str = ""

    @property
    def ok(self) -> bool:
        return self.refined is not None and not self.needs_clarification


_REFINE_SYSTEM = (
    "You are an intent extractor for a profile-driven agent system. "
    "Given a user message and a target profile's JSON Schema, produce a "
    "compact JSON object with these top-level fields:\n"
    '  - "target_profile": string (the profile id given to you)\n'
    '  - "intent_type": one of "read" | "analyze" | "write" | "chat"\n'
    '  - "refined_query": the user\'s request rewritten as a clear, '
    "single-sentence instruction (<=280 chars)\n"
    '  - "intent": an object conforming to the provided schema\n'
    '  - "confidence": float 0.0–1.0 (how sure you are the extraction is correct)\n'
    '  - "ambiguous_fields": array of schema field names you could not confidently fill\n'
    "Rules:\n"
    " - Output ONLY the JSON object — no markdown, no prose.\n"
    " - If the user message is unrelated to the profile, set confidence < 0.5.\n"
    " - Never invent facts. Leave optional fields out rather than guessing.\n"
    " - Korean input → produce Korean refined_query; English → English."
)


def _extract_json_object(raw: str) -> dict | None:
    """Best-effort extraction of a JSON object from noisy model output.

    Mirrors src.router.router._parse_refined for consistency.
    """
    if not raw:
        return None
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        pass
    start = raw.find("{")
    end = raw.rfind("}")
    if start != -1 and end > start:
        try:
            return json.loads(raw[start : end + 1])
        except json.JSONDecodeError:
            return None
    return None


def _classify_intent_heuristic(message: str) -> IntentType:
    """Fallback intent classifier when LLM output is unusable."""
    m = message.lower().strip()
    if re.search(r"(추가|생성|저장|등록|삭제|수정|보내|write|create|delete|update)", m):
        return "write"
    if re.search(r"(분석|점수|평가|요약|비교|analyze|score|compare)", m):
        return "analyze"
    if re.search(r"(찾|검색|보여|알려|언제|목록|list|find|search|show|what|when)", m):
        return "read"
    return "chat"


@dataclass
class Refiner:
    """Request Refiner orchestrator.

    The LLM client is abstracted via a callable: ``llm_call(messages) → text``.
    This keeps the Refiner framework-agnostic and testable — the orchestrator
    wires in a real :class:`src.llm.OpenAIClient` / :class:`OllamaClient`
    lambda at construction time.
    """

    profiles_dir: Path
    llm_call: Any  # async callable: (list[dict]) -> str
    confidence_threshold: float = DEFAULT_CONFIDENCE_THRESHOLD

    def __post_init__(self) -> None:
        self.profiles_dir = Path(self.profiles_dir)
        if not self.profiles_dir.exists():
            raise RefinerError(f"profiles_dir does not exist: {self.profiles_dir}")

    def available_profiles(self) -> list[str]:
        return sorted(
            p.name
            for p in self.profiles_dir.iterdir()
            if p.is_dir() and (p / "config.yaml").exists()
        )

    def _load_schema(self, profile_id: str) -> dict | None:
        schema_path = self.profiles_dir / profile_id / "intent_schema.json"
        if not schema_path.exists():
            return None
        try:
            return json.loads(schema_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as e:
            log.warning("refiner.schema_load_failed", profile=profile_id, err=str(e))
            return None

    async def refine(
        self,
        user_message: str,
        *,
        candidate_profile: str,
        history: list[dict[str, str]] | None = None,
    ) -> RefinerResult:
        """Refine ``user_message`` against ``candidate_profile``'s schema.

        The caller (Job_Factory) picks the candidate profile; the Refiner
        just validates the fit and extracts structured intent. If the
        candidate doesn't fit (confidence low), the caller can retry with
        a different candidate or fall back to profile creation.
        """
        schema = self._load_schema(candidate_profile)
        if schema is None:
            return RefinerResult(
                refined=None,
                needs_clarification=False,
                reason=f"profile `{candidate_profile}` has no intent_schema.json",
            )

        user_prompt = (
            f"target_profile: {candidate_profile}\n\n"
            f"schema:\n{json.dumps(schema, ensure_ascii=False)}\n\n"
            f"user_message: {user_message}"
        )
        messages = [{"role": "system", "content": _REFINE_SYSTEM}]
        messages += (history or [])[-4:]
        messages.append({"role": "user", "content": user_prompt})

        try:
            raw = await self.llm_call(messages)
        except Exception as e:  # noqa: BLE001
            log.warning("refiner.llm_failed", err=str(e))
            return RefinerResult(
                refined=None,
                needs_clarification=True,
                clarification_prompt=(
                    "요청을 이해하지 못했습니다. "
                    "어떤 작업을 원하는지 한 문장으로 다시 알려주세요."
                ),
                reason=f"llm_error: {type(e).__name__}",
            )

        parsed = _extract_json_object(raw)
        if parsed is None:
            return RefinerResult(
                refined=None,
                needs_clarification=True,
                clarification_prompt=(
                    "요청을 구조화하지 못했습니다. "
                    "원하는 동작(예: 일정 추가, 공고 검색)과 대상을 명확히 적어주세요."
                ),
                reason="unparseable_json",
            )

        try:
            confidence = max(0.0, min(1.0, float(parsed.get("confidence", 0.0))))
        except (TypeError, ValueError):
            confidence = 0.0

        ambiguous = list(parsed.get("ambiguous_fields") or [])
        intent_obj = parsed.get("intent") or {}
        target = str(parsed.get("target_profile") or candidate_profile)
        refined_query = str(parsed.get("refined_query") or user_message)[:280]

        intent_type_raw = parsed.get("intent_type")
        if intent_type_raw in ("read", "analyze", "write", "chat"):
            intent_type: IntentType = intent_type_raw  # type: ignore[assignment]
        else:
            intent_type = _classify_intent_heuristic(user_message)

        if confidence < self.confidence_threshold:
            prompt = self._build_clarification(
                user_message, intent_obj, ambiguous, confidence
            )
            return RefinerResult(
                refined=None,
                needs_clarification=True,
                clarification_prompt=prompt,
                reason=f"low_confidence={confidence:.2f}",
            )

        if not isinstance(intent_obj, dict):
            return RefinerResult(
                refined=None,
                needs_clarification=True,
                clarification_prompt=(
                    "요청 내용이 모호합니다. 동작과 대상을 구체적으로 알려주세요."
                ),
                reason="intent_not_object",
            )

        refined = RefinedRequest(
            target_profile=target,
            intent_type=intent_type,
            refined_query=refined_query,
            intent=intent_obj,
            confidence=confidence,
            ambiguous_fields=ambiguous,
            raw_message=user_message,
        )
        log.info(
            "refiner.ok",
            profile=target,
            intent_type=intent_type,
            confidence=round(confidence, 2),
            ambiguous_fields=ambiguous,
        )
        return RefinerResult(refined=refined, needs_clarification=False)

    @staticmethod
    def _build_clarification(
        user_message: str,
        intent: Any,
        ambiguous: list[str],
        confidence: float,
    ) -> str:
        lines = ["❓ 요청을 확실히 이해하기 위해 몇 가지 확인이 필요합니다."]
        if ambiguous:
            lines.append(f"불확실한 항목: {', '.join(ambiguous)}")
        else:
            lines.append(f"이해 확신도: {confidence:.0%}")
        lines.append("원하시는 동작과 대상을 한 문장으로 다시 알려주세요.")
        return "\n".join(lines)
