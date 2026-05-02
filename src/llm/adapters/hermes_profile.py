"""Hermes profile-pinned adapter — wraps :class:`HermesAdapter`.

Provider id: ``"hermes_profile"``. The "model" identifier is the profile
name (e.g., ``"journal_ops"``, ``"calendar_ops"``) because that's what
actually drives Hermes's choice of underlying LLM (the profile's
``config.yaml`` does the model/provider selection internally).

This adapter exists for two reasons:
  1. Job Factory v2 may want to invoke an existing profile as a step
     in a fallback chain (e.g., if ``schedule_logging`` job_type is
     classified, route to journal_ops profile rather than re-implementing
     the 24-field extraction).
  2. ScoreMatrix needs a stable key for "hermes via profile X" so the
     bandit can compare its quality against direct Ollama/OpenAI calls.

The adapter flattens chat messages into a single ``query`` because the
Hermes CLI's ``chat -q`` interface is single-shot. Conversation history
should be summarized into the prompt by the caller; long histories would
inflate the query and Hermes wasn't designed for chat-mode replay.
"""
from __future__ import annotations

from src.hermes_adapter.adapter import HermesAdapter
from src.llm.adapters.base import (
    AdapterRequest,
    AdapterResponse,
    flatten_to_prompt,
)


class HermesProfileAdapter:
    """Adapter pinned to one Hermes profile.

    Construct one instance per profile; the profile name doubles as the
    ``model`` identifier so the ScoreMatrix can track each profile as a
    separate arm.
    """

    def __init__(self, hermes: HermesAdapter, profile: str):
        self._hermes = hermes
        self._profile = profile

    @property
    def provider(self) -> str:
        return "hermes_profile"

    @property
    def model(self) -> str:
        return self._profile

    async def generate(self, request: AdapterRequest) -> AdapterResponse:
        query = flatten_to_prompt(request.messages)

        timeout_ms = (
            int(request.timeout_s * 1000) if request.timeout_s else None
        )

        # model=None / provider=None → defer to profile config.yaml
        # (the calendar_ops / journal_ops style — which we already
        # validated works in the existing forced_profile path).
        result = await self._hermes.run(
            query,
            model=None,
            provider=None,
            profile=self._profile,
            timeout_ms=timeout_ms,
        )
        # We report the profile as the model identifier to keep the
        # ScoreMatrix arms stable across underlying-model changes (e.g.,
        # if calendar_ops switches from gpt-4o to gpt-4o-mini, all prior
        # scores for "hermes_profile/calendar_ops" remain comparable).
        return AdapterResponse(
            text=result.text,
            provider=self.provider,
            model=self._profile,
            prompt_tokens=result.prompt_tokens,
            completion_tokens=result.completion_tokens,
            duration_ms=result.duration_ms,
            raw=result,
        )
