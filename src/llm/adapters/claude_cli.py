"""Claude Code CLI adapter — wraps :class:`ClaudeCodeAdapter`.

Provider id: ``"claude_cli"``. Model is the Claude alias (``"haiku"`` /
``"sonnet"`` / ``"opus"``) or the full model name. Uses Max OAuth — zero
marginal API cost, but session/hour quota applies.

The Claude Code CLI takes a single ``prompt`` plus a list of prior
messages (``history``); we adopt the convention that the LAST entry of
``request.messages`` (must be role=user) is the new prompt and everything
before it (system + earlier turns) becomes ``history``. System messages
are merged into the prompt prefix because the CLI doesn't have a separate
system slot.
"""
from __future__ import annotations

from src.claude_adapter.adapter import ClaudeCodeAdapter
from src.llm.adapters.base import (
    AdapterRequest,
    AdapterResponse,
    ChatMessage,
)


class ClaudeCLIAdapter:
    """Adapter around :class:`ClaudeCodeAdapter`. Heavy/C1 path users.

    ``persist_session`` is hard-coded to False because the Job Factory
    routes are stateless turns; the heavy path with session reuse stays
    on the orchestrator's existing ``HeavySessionRegistry`` and isn't
    routed through this adapter.
    """

    def __init__(self, claude: ClaudeCodeAdapter, model: str):
        self._claude = claude
        self._model = model

    @property
    def provider(self) -> str:
        return "claude_cli"

    @property
    def model(self) -> str:
        return self._model

    async def generate(self, request: AdapterRequest) -> AdapterResponse:
        prompt, history = _split_for_claude_cli(request.messages)

        # The CLI's own timeout argument is in ms; honor request.timeout_s.
        timeout_ms = (
            int(request.timeout_s * 1000) if request.timeout_s else None
        )

        result = await self._claude.run(
            prompt=prompt,
            history=history,
            model=self._model,
            timeout_ms=timeout_ms,
            resume_session_id=None,
            persist_session=False,
        )
        return AdapterResponse(
            text=result.text,
            provider=self.provider,
            model=result.model_name or self._model,
            prompt_tokens=result.input_tokens,
            completion_tokens=result.output_tokens,
            duration_ms=result.duration_ms,
            raw=result,
        )


def _split_for_claude_cli(
    messages: list[ChatMessage],
) -> tuple[str, list[dict[str, str]]]:
    """Convert ChatMessage list to (prompt, history) for ClaudeCodeAdapter.

    Strategy:
      * System messages → prepended to the prompt with "[system]" label
        so Claude treats them as instructions even without a native role.
      * Last user message → the prompt.
      * Everything in between → history (passed through as role/content).

    If the last message isn't user, we still treat it as the prompt
    (ClaudeCodeAdapter doesn't reject this — it just sends what we send).
    """
    if not messages:
        raise ValueError("messages must be non-empty")

    # Pull system messages out into a prefix.
    systems = [m.content for m in messages if m.role == "system"]
    non_system = [m for m in messages if m.role != "system"]

    if not non_system:
        # Edge case: only system messages. Treat as prompt.
        return "\n\n".join(systems), []

    last = non_system[-1]
    history_msgs = non_system[:-1]

    history: list[dict[str, str]] = [
        {"role": m.role, "content": m.content} for m in history_msgs
    ]

    if systems:
        prompt = "[system]\n" + "\n\n".join(systems) + "\n\n" + last.content
    else:
        prompt = last.content

    return prompt, history
