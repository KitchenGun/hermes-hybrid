"""Revision loop — Phase 13 (2026-05-07).

1ilkhamov/opencode-hermes-multiagent 의 "Revision Loops: Up to 3 iterations
for fixes before escalation" 패턴 + Hermes Agent 의 plan/act/observe/reflect/
retry 5단계 흡수.

흐름:
  plan/act → adapter.run (1회)
  observe → Critic.evaluate → self_score
  reflect → score < threshold 이거나 timeout/tool_error 발생 시
  retry  → 같은 model 로 retry context 추가 ("이전 응답이 약했음:") + 재시도
           재시도 실패 누적 시 모델 escalate (haiku → sonnet → opus)

Cap: max_retries (default 3). cap 도달 시 마지막 시도 결과를 반환 (degraded 표시).
설정으로 비활성 가능 (revision_loop_enabled=False) — 그러면 single-shot.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Optional

from src.obs import get_logger

log = get_logger(__name__)


@dataclass
class RevisionAttempt:
    attempt_index: int                    # 0-based
    model: str
    success: bool
    response: str = ""
    self_score: float = 0.0
    error: str = ""
    prompt_tokens: int = 0
    completion_tokens: int = 0
    duration_ms: int = 0
    tier_up: bool = False                 # 모델 escalation 발생 여부


@dataclass
class RevisionResult:
    final_response: str
    final_model: str
    final_self_score: float
    attempts: list[RevisionAttempt] = field(default_factory=list)
    succeeded: bool = False
    escalated: bool = False               # 모델 escalation 발생 여부

    @property
    def attempt_count(self) -> int:
        return len(self.attempts)


class RevisionLoop:
    """Wrap an adapter call with reflect-and-retry semantics.

    Caller passes in the initial prompt + a Critic-shaped scorer (callable
    that returns ``self_score: float`` 0~1). RevisionLoop runs up to
    ``max_retries`` attempts, escalating model when score stays low.
    """

    def __init__(
        self,
        adapter: Any,                     # ClaudeCodeAdapter-like
        critic_scorer: Any,               # callable: (text) -> float | Critic.evaluate-shaped
        *,
        max_retries: int = 3,
        score_threshold: float = 0.5,
        model_escalation: tuple[str, ...] = ("haiku", "sonnet", "opus"),
    ):
        self.adapter = adapter
        self.scorer = critic_scorer
        self.max_retries = max(1, max_retries)
        self.score_threshold = max(0.0, min(1.0, score_threshold))
        self.model_escalation = model_escalation

    async def run(
        self,
        *,
        prompt: str,
        history: list[dict[str, str]] | None = None,
        initial_model: str | None = None,
    ) -> RevisionResult:
        """Try up to N times. Return the best attempt (highest self_score)
        or the final attempt if none reached threshold.
        """
        attempts: list[RevisionAttempt] = []
        current_prompt = prompt
        current_model = initial_model
        # Build escalation queue starting from initial_model (or first
        # in escalation tuple).
        models_used: list[str] = []
        escalated = False

        for attempt_idx in range(self.max_retries):
            # Pick model — escalate one step every failed attempt past first.
            if attempt_idx == 0:
                model = current_model or self.model_escalation[0]
            else:
                last = attempts[-1]
                if last.self_score < self.score_threshold and not last.success:
                    # try same model first if first attempt failed; then escalate
                    next_model = self._next_model(last.model)
                    if next_model != last.model:
                        escalated = True
                    model = next_model
                elif last.self_score < self.score_threshold:
                    # response generated but weak — escalate
                    next_model = self._next_model(last.model)
                    if next_model != last.model:
                        escalated = True
                    model = next_model
                else:
                    # threshold met — done
                    break
            models_used.append(model)

            t0 = time.perf_counter()
            try:
                result = await self.adapter.run(
                    prompt=current_prompt,
                    history=history or [],
                    model=model,
                )
            except Exception as e:  # noqa: BLE001
                attempt = RevisionAttempt(
                    attempt_index=attempt_idx,
                    model=model,
                    success=False,
                    error=f"{type(e).__name__}: {e}",
                    duration_ms=int((time.perf_counter() - t0) * 1000),
                    tier_up=(escalated and attempt_idx > 0),
                )
                attempts.append(attempt)
                log.warning(
                    "revision.attempt_failed",
                    attempt=attempt_idx,
                    model=model,
                    err=str(e),
                )
                # build retry context
                current_prompt = (
                    prompt
                    + "\n\n[reflection]\n이전 시도 실패: "
                    + attempt.error[:200]
                    + "\n다시 응답하시오."
                )
                continue

            response_text = getattr(result, "text", "") or ""
            score = self._score(response_text)

            attempt = RevisionAttempt(
                attempt_index=attempt_idx,
                model=model,
                success=True,
                response=response_text,
                self_score=score,
                prompt_tokens=int(getattr(result, "input_tokens", 0) or 0),
                completion_tokens=int(getattr(result, "output_tokens", 0) or 0),
                duration_ms=int((time.perf_counter() - t0) * 1000),
                tier_up=(escalated and attempt_idx > 0),
            )
            attempts.append(attempt)

            log.info(
                "revision.attempt",
                attempt=attempt_idx,
                model=model,
                score=score,
                threshold=self.score_threshold,
                tokens_in=attempt.prompt_tokens,
                tokens_out=attempt.completion_tokens,
            )

            if score >= self.score_threshold:
                # threshold met — done
                break

            # build retry context
            current_prompt = (
                prompt
                + f"\n\n[reflection]\n이전 응답 self_score={score:.2f} "
                f"(threshold={self.score_threshold}). 더 정확하고 완전한 "
                f"응답으로 개선하시오. 이전 응답:\n{response_text[:500]}"
            )

        # Pick best attempt (highest self_score, success preferred)
        best = self._pick_best(attempts)
        return RevisionResult(
            final_response=best.response,
            final_model=best.model,
            final_self_score=best.self_score,
            attempts=attempts,
            succeeded=best.success and best.self_score >= self.score_threshold,
            escalated=escalated,
        )

    def _next_model(self, current: str) -> str:
        """Move one step up in escalation. Returns same model if already at top."""
        try:
            idx = self.model_escalation.index(current)
        except ValueError:
            return current
        if idx + 1 < len(self.model_escalation):
            return self.model_escalation[idx + 1]
        return current

    def _score(self, text: str) -> float:
        """Bridge to Critic-like scorer. Returns 0~1.

        ``self.scorer`` may be a callable returning float, or a Critic-like
        object with .self_score(text) method.
        """
        try:
            if callable(self.scorer):
                v = self.scorer(text)
            else:
                v = self.scorer.self_score(text)
        except Exception:  # noqa: BLE001
            return 0.0
        try:
            f = float(v)
        except (TypeError, ValueError):
            return 0.0
        return max(0.0, min(1.0, f))

    @staticmethod
    def _pick_best(attempts: list[RevisionAttempt]) -> RevisionAttempt:
        if not attempts:
            return RevisionAttempt(attempt_index=0, model="", success=False)
        # Prefer success + highest score
        return max(
            attempts,
            key=lambda a: (a.success, a.self_score, -a.attempt_index),
        )


__all__ = ["RevisionAttempt", "RevisionLoop", "RevisionResult"]
