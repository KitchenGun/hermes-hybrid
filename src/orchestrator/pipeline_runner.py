"""Sequential pipeline executor — Phase 12 (2026-05-07).

Runs a :class:`Pipeline` start-to-end, calling the master adapter per
``@handle`` step with the SKILL.md frontmatter inject + prior step
results. Progress is reported via a callback so the gateway can stream
"🔄 N/M @handle 진행 중..." back to Discord.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Optional

from src.obs import get_logger

from .pipelines import Pipeline

log = get_logger(__name__)


@dataclass
class PipelineStageResult:
    """One step output."""
    handle: str
    stage_index: int                        # 0-based
    success: bool
    response: str = ""
    error: str = ""
    prompt_tokens: int = 0
    completion_tokens: int = 0
    duration_ms: int = 0


@dataclass
class PipelineRunResult:
    """Full pipeline outcome."""
    pipeline_id: str
    stages: list[PipelineStageResult] = field(default_factory=list)
    completed: bool = False
    aborted: bool = False
    abort_reason: str = ""

    @property
    def succeeded_count(self) -> int:
        return sum(1 for s in self.stages if s.success)

    @property
    def failed_count(self) -> int:
        return sum(1 for s in self.stages if not s.success)

    def aggregate_text(self) -> str:
        """Join stage responses with `### @handle` headers, like Phase 10."""
        if not self.stages:
            return ""
        parts: list[str] = []
        for s in self.stages:
            header = f"### {s.handle} ({s.stage_index + 1}/{len(self.stages)})"
            if not s.success:
                header += " — failed"
                body = s.error or "(no error)"
            else:
                body = s.response or "(empty)"
            parts.append(f"{header}\n{body}")
        return "\n\n".join(parts)


# Callback signatures — keep loose so gateway can pass anything async.
ProgressCallback = Callable[[int, int, str, str], Awaitable[None]]
"""(stage_index, total_stages, handle, status) — status in 'start' | 'done' | 'fail' | 'checkpoint'"""


class PipelineRunner:
    """Drive a :class:`Pipeline` step-by-step against an agent registry +
    master adapter.

    The runner asks the registry for each agent's SKILL.md frontmatter
    and reuses the same prompt-composition helper as Phase 9/10 so the
    LLM context is consistent across (single-shot / parallel / pipeline).
    """

    def __init__(
        self,
        adapter: Any,                       # ClaudeCodeAdapter-like
        agents: Any,                        # AgentRegistry
        *,
        per_stage_timeout_ms: int | None = None,
    ):
        self.adapter = adapter
        self.agents = agents
        self.per_stage_timeout_ms = per_stage_timeout_ms

    async def run(
        self,
        *,
        pipeline: Pipeline,
        user_message: str,
        progress: Optional[ProgressCallback] = None,
    ) -> PipelineRunResult:
        """Execute the pipeline. Each stage receives prior stage outputs
        as transcript-style context. Failure of one stage does not abort
        the rest — downstream stages see the failure note and decide.
        """
        from src.core.delegation import _compose_agent_prompt

        result = PipelineRunResult(pipeline_id=pipeline.pipeline_id)
        prior_responses: list[str] = []

        for idx, handle in enumerate(pipeline.sequence):
            entry = self.agents.by_handle(handle)
            if entry is None:
                stage = PipelineStageResult(
                    handle=handle,
                    stage_index=idx,
                    success=False,
                    error=f"unknown agent handle: {handle}",
                )
                result.stages.append(stage)
                if progress:
                    await progress(idx, len(pipeline.sequence), handle, "fail")
                continue

            if progress:
                await progress(idx, len(pipeline.sequence), handle, "start")

            # Compose stage-specific prompt: SKILL.md frontmatter
            # + prior transcript + user message.
            stage_prompt = _compose_agent_prompt(entry, user_message)
            if prior_responses:
                transcript = "\n\n".join(
                    f"[prior:{pipeline.sequence[i]}]\n{prior_responses[i]}"
                    for i in range(len(prior_responses))
                )
                stage_prompt = (
                    transcript + "\n\n---\n\n" + stage_prompt
                )

            t0 = time.perf_counter()
            try:
                kwargs: dict[str, Any] = {"prompt": stage_prompt, "history": []}
                if self.per_stage_timeout_ms is not None:
                    kwargs["timeout_ms"] = self.per_stage_timeout_ms
                adapter_result = await self.adapter.run(**kwargs)
            except Exception as e:  # noqa: BLE001
                stage = PipelineStageResult(
                    handle=handle,
                    stage_index=idx,
                    success=False,
                    error=f"{type(e).__name__}: {e}",
                    duration_ms=int((time.perf_counter() - t0) * 1000),
                )
                result.stages.append(stage)
                prior_responses.append(stage.error)  # let next stage see failure
                if progress:
                    await progress(idx, len(pipeline.sequence), handle, "fail")
                continue

            response_text = getattr(adapter_result, "text", "") or ""
            stage = PipelineStageResult(
                handle=handle,
                stage_index=idx,
                success=True,
                response=response_text,
                prompt_tokens=int(getattr(adapter_result, "input_tokens", 0) or 0),
                completion_tokens=int(getattr(adapter_result, "output_tokens", 0) or 0),
                duration_ms=int((time.perf_counter() - t0) * 1000),
            )
            result.stages.append(stage)
            prior_responses.append(response_text)

            status = "checkpoint" if handle in pipeline.checkpoint_after else "done"
            if progress:
                await progress(idx, len(pipeline.sequence), handle, status)

            log.info(
                "pipeline.stage_done",
                pipeline_id=pipeline.pipeline_id,
                stage=idx,
                handle=handle,
                tokens_in=stage.prompt_tokens,
                tokens_out=stage.completion_tokens,
                duration_ms=stage.duration_ms,
            )

        result.completed = True
        return result


__all__ = ["PipelineRunner", "PipelineRunResult", "PipelineStageResult", "ProgressCallback"]
