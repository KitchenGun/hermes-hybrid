"""Router: returns routing decision JSON only. No planning, no execution (R11).

Contract (design doc §3):
  {
    "route": "local|worker|cloud",
    "confidence": float,
    "reason": str (<=120),
    "requires_planning": bool
  }

Heuristics are intentionally kept simple and honest. The Orchestrator
handles the downstream fallback (e.g. Ollama disabled → surrogate), so
the Router only signals intent — not feasibility.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Literal

from src.config import Settings
from src.llm.base import LLMError
from src.llm.ollama_client import OllamaClient
from src.obs import get_logger

log = get_logger(__name__)

Route = Literal["local", "worker", "cloud"]

# FIX#1: Router may recommend a concrete *provider* for downstream wiring, but
# the set is deliberately restricted to providers that go through Hermes /
# OpenAI. ``claude-code`` is NOT a member — it can only be reached via the
# explicit ``!heavy`` path. Encoding this as a ``Literal`` means a regression
# that tries to add ``claude-code`` to the Router will fail type checks, not
# silently burn Max quota at runtime.
Provider = Literal["ollama", "openai"]


def _route_to_provider(route: Route, *, ollama_enabled: bool) -> Provider:
    """Resolve a route to the provider that should serve it.

    - ``local`` / ``worker`` → ``ollama`` when enabled, else ``openai``
      (surrogate path, still not claude-code).
    - ``cloud`` → ``openai`` (C1 surrogate / main). Claude is never reached
      via the router.
    """
    if route in ("local", "worker") and ollama_enabled:
        return "ollama"
    return "openai"


@dataclass(frozen=True)
class RouterDecision:
    route: Route
    confidence: float
    reason: str
    requires_planning: bool
    # FIX#1: recommended provider, constrained by the ``Provider`` Literal to
    # exclude ``claude-code`` at the type level. Defaults to ``openai`` so
    # call sites that construct ``RouterDecision`` directly (tests, tooling)
    # never accidentally emit ``claude-code``.
    provider: Provider = "openai"

    def to_dict(self) -> dict:
        return {
            "route": self.route,
            "confidence": round(self.confidence, 3),
            "reason": self.reason,
            "requires_planning": self.requires_planning,
            "provider": self.provider,
        }


# --- detectors ---------------------------------------------------------------

_CODE_RE = re.compile(
    r"(```|def\s+\w+|class\s+\w+|import\s+\w+|function\s+\w+|=>\s*\{|"
    r"\bSELECT\b|\bINSERT\b|\.py\b|\.ts\b|\.tsx\b|\.js\b)",
    re.IGNORECASE,
)
_STRUCTURED_OUTPUT_RE = re.compile(
    r"(json\s*(형식|로|으로|schema|output)|스키마|schema|yaml|xml|테이블로|"
    r"structured\s+output|as\s+json)",
    re.IGNORECASE,
)
_URL_RE = re.compile(r"https?://\S+")
_FILE_REF_RE = re.compile(r"\b[\w\-/]+\.(md|py|ts|tsx|js|json|yaml|yml|csv|txt|log|rs|go|java|c|cpp)\b", re.IGNORECASE)
_CONDITIONAL_RE = re.compile(r"(만약|만일|\bif\s+.+?\s+then\b|조건에?\s?따라)", re.IGNORECASE)
_MULTISTEP_KR_RE = re.compile(r"(그리고|하고|해서|및|또한|그 다음|이후에|끝나면)")
_MULTISTEP_EN_RE = re.compile(r"\b(and then|then|also|after that|finally)\b", re.IGNORECASE)
_SHORT_LEN = 140
_LONG_LEN = 600


def _requires_planning(msg: str, prev_failure: bool) -> bool:
    if prev_failure:
        return True
    if _URL_RE.search(msg) or _FILE_REF_RE.search(msg) or _CONDITIONAL_RE.search(msg):
        return True
    # multi-step indicator AND message long enough to actually have multiple steps
    if len(msg) > 80 and (
        len(_MULTISTEP_KR_RE.findall(msg)) >= 2
        or _MULTISTEP_EN_RE.search(msg)
    ):
        return True
    return False


def _heuristic(message: str, prev_failure: bool) -> RouterDecision:
    msg = message.strip()
    n = len(msg)
    planning = _requires_planning(msg, prev_failure)

    if _CODE_RE.search(msg) or _STRUCTURED_OUTPUT_RE.search(msg):
        return RouterDecision(
            route="worker", confidence=0.85,
            reason="code or structured-output signal",
            requires_planning=planning,
        )
    if planning and n > _LONG_LEN:
        return RouterDecision(
            route="cloud", confidence=0.82,
            reason="long multi-step task",
            requires_planning=True,
        )
    if planning:
        return RouterDecision(
            route="cloud", confidence=0.70,
            reason="planning signals",
            requires_planning=True,
        )
    if n <= _SHORT_LEN:
        return RouterDecision(
            route="local", confidence=0.80,
            reason="short conversational",
            requires_planning=False,
        )
    return RouterDecision(
        route="local", confidence=0.58,
        reason="medium length, no strong signal",
        requires_planning=False,
    )


# --- Router ------------------------------------------------------------------


_REFINE_SYSTEM = (
    "You are a routing classifier. Read the user message and respond with ONLY "
    "a compact JSON object — no prose, no markdown fences.\n"
    'Schema: {"route":"local|worker|cloud","confidence":0.0-1.0,'
    '"requires_planning":true|false,"reason":"<=60 chars"}\n'
    "Rules:\n"
    " - local: short conversation, small-talk, simple Q&A.\n"
    " - worker: code / structured-output / single-file task.\n"
    " - cloud: multi-step, URLs, file refs, conditional logic, long planning.\n"
    "Respond with the JSON object only."
)


def _parse_refined(raw: str) -> dict | None:
    """Extract a JSON object from a possibly-noisy model response."""
    if not raw:
        return None
    # Try full string first
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        pass
    # Then look for the first balanced {...} block.
    start = raw.find("{")
    end = raw.rfind("}")
    if start != -1 and end > start:
        try:
            return json.loads(raw[start : end + 1])
        except json.JSONDecodeError:
            return None
    return None


class Router:
    def __init__(self, settings: Settings):
        self.settings = settings
        self._ollama_router: OllamaClient | None = None

    def _router_client(self) -> OllamaClient:
        if self._ollama_router is None:
            # Short keep_alive: router calls are frequent & small; we'd rather
            # free VRAM for the work/worker models than keep the 7B pinned.
            self._ollama_router = OllamaClient(
                self.settings.ollama_base_url,
                self.settings.ollama_router_model,
                keep_alive="5m",
                request_timeout=30.0,
            )
        return self._ollama_router

    async def decide(
        self,
        message: str,
        history_window: list[dict[str, str]] | None = None,
        prev_failure: bool = False,
    ) -> RouterDecision:
        decision = _heuristic(message, prev_failure)

        if self.settings.ollama_enabled:
            try:
                decision = await self._refine_with_ollama(message, decision, history_window or [])
            except (LLMError, Exception) as e:  # noqa: BLE001
                # Router MUST always return — never let a 7B hiccup block the bot.
                log.info("router.refine_failed_fallback_to_heuristic", err=str(e))

        # Confidence-threshold policy (design §3.3)
        if decision.confidence < self.settings.router_conf_tier_up:
            return self._finalize(RouterDecision(
                route="cloud",
                confidence=decision.confidence,
                reason=f"low-conf→cloud: {decision.reason}",
                requires_planning=True,
            ))
        if decision.confidence < self.settings.router_conf_accept:
            bumped = {"local": "worker", "worker": "cloud", "cloud": "cloud"}[decision.route]
            return self._finalize(RouterDecision(
                route=bumped,  # type: ignore[arg-type]
                confidence=decision.confidence,
                reason=f"mid-conf→bump: {decision.reason}",
                requires_planning=decision.requires_planning,
            ))
        return self._finalize(decision)

    def _finalize(self, decision: RouterDecision) -> RouterDecision:
        """FIX#1: stamp the provider on every decision before it leaves the
        Router. Provider is derived from (route, ollama_enabled) via the
        ``Provider`` Literal — claude-code is impossible by construction."""
        return RouterDecision(
            route=decision.route,
            confidence=decision.confidence,
            reason=decision.reason,
            requires_planning=decision.requires_planning,
            provider=_route_to_provider(
                decision.route, ollama_enabled=self.settings.ollama_enabled
            ),
        )

    async def _refine_with_ollama(
        self,
        message: str,
        heuristic: RouterDecision,
        history: list[dict[str, str]],
    ) -> RouterDecision:
        """Ask the 7B router model to validate/adjust the heuristic.

        The 7B runs as a supplement — when it emits garbage or disagrees
        without enough confidence, we keep the heuristic. This is the safest
        behavior: the heuristic is already decent, the 7B only matters when
        it's *clearly* right.
        """
        client = self._router_client()
        # Limit history injected — keep the 7B fast. Last 2 turns is enough context.
        hist = (history or [])[-4:]
        msgs = [{"role": "system", "content": _REFINE_SYSTEM}]
        msgs += hist
        msgs.append({"role": "user", "content": message})
        # 7B at 128 tokens is plenty for a single JSON object.
        resp = await client.generate(msgs, max_tokens=128, temperature=0.0)
        parsed = _parse_refined(resp.text)
        if not parsed:
            log.info("router.refine_unparseable", raw=resp.text[:120])
            return heuristic

        route = parsed.get("route")
        if route not in ("local", "worker", "cloud"):
            return heuristic
        try:
            conf = float(parsed.get("confidence", 0.0))
        except (TypeError, ValueError):
            conf = 0.0
        conf = max(0.0, min(1.0, conf))
        planning = bool(parsed.get("requires_planning", heuristic.requires_planning))
        reason = str(parsed.get("reason", ""))[:60] or "7b refined"

        # Accept the 7B's verdict only if it's confident. Otherwise stick with
        # the heuristic — the 7B disagreeing at 0.5 conf isn't a strong signal.
        if conf < self.settings.router_conf_accept:
            return heuristic

        return RouterDecision(
            route=route,  # type: ignore[arg-type]
            confidence=conf,
            reason=f"7b: {reason}",
            requires_planning=planning,
        )
