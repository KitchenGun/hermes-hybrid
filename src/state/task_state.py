"""TaskState model per design doc §9.

Single source of truth for a task's lifecycle through the Orchestrator.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Any, Literal

from pydantic import BaseModel, Field

Route = Literal["local", "worker", "cloud"]
Tier = Literal["L2", "L3", "C1", "C2"]
Status = Literal[
    "pending",
    "planning",
    "acting",
    "reflecting",
    "retrying",
    "escalated",
    "awaiting_confirmation",
    "succeeded",
    "failed",
]
ErrorType = Literal["malformed_output", "low_quality", "timeout", "tool_error"]


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class ToolOutput(BaseModel):
    action_id: str
    tool: str
    result: Any
    ms: int
    ok: bool = True


class ModelOutput(BaseModel):
    tier: Tier
    prompt_tokens: int = 0
    completion_tokens: int = 0
    text: str = ""
    model_name: str = ""
    # OpenCode: identifies which pipeline stage produced this output
    # ("plan" | "build" | "high" | "review"). Empty for legacy single-call paths.
    substage: str = ""


class ConfirmationContext(BaseModel):
    """Pending-write state captured when a task hits a ``requires_confirmation``
    gate. Persisted via state_json so the bot can resume after restart.
    """

    profile_id: str
    job_name: str
    preview_title: str
    preview_body: str
    preview_color: int = 0xFEE75C
    pending_payload: dict[str, Any] = Field(default_factory=dict)
    expires_at: datetime
    discord_message_id: int | None = None
    discord_channel_id: int | None = None

    def is_expired(self) -> bool:
        return _utcnow() >= self.expires_at


class ErrorEvent(BaseModel):
    at: datetime = Field(default_factory=_utcnow)
    error_type: ErrorType
    message: str
    tier: Tier | None = None


class HermesAction(BaseModel):
    action_id: str
    tool: str
    args: dict[str, Any]
    timeout_ms: int = 15_000
    expects_schema: str | None = None


class HermesObservation(BaseModel):
    action_id: str
    raw_output: Any
    schema_ok: bool
    duration_ms: int


class HermesReflection(BaseModel):
    at: datetime = Field(default_factory=_utcnow)
    success: bool
    error_type: ErrorType | None = None
    next_action: Literal["retry_act", "retry_plan", "escalate", "done"] = "done"
    note: str = ""


class HermesTrace(BaseModel):
    plan: dict[str, Any] = Field(default_factory=dict)
    actions: list[HermesAction] = Field(default_factory=list)
    observations: list[HermesObservation] = Field(default_factory=list)
    reflections: list[HermesReflection] = Field(default_factory=list)


class TaskState(BaseModel):
    # Identity
    session_id: str
    task_id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    user_id: str
    created_at: datetime = Field(default_factory=_utcnow)
    updated_at: datetime = Field(default_factory=_utcnow)

    # Input
    user_message: str
    history_window: list[dict[str, str]] = Field(default_factory=list)

    # Routing
    route: Route = "local"
    router_confidence: float = 0.0
    requires_planning: bool = False
    router_reason: str = ""

    # Channel-pinned forced profile routing — Phase 8 폐기. 호환을 위해
    # 필드만 보존 (DB 에 저장된 옛 task 가 깨지지 않도록).
    forced_profile: str | None = None

    # JobFactory: factory.decide()가 "match"를 반환할 때 profile_id 저장.
    # None = 팩토리 비활성화 또는 no_match/ambiguous 상태.
    job_profile_id: str | None = None

    # Phase 1.5 (2026-05-06): routing context fields for ExperienceLog.
    # Stamped by Orchestrator at the matching dispatch branch — left as
    # default when that branch isn't taken. ExperienceLogger projects
    # these directly onto ExperienceRecord without reinterpretation.
    job_id: str | None = None              # cron/on_demand yaml name OR slash skill name
    job_category: str | None = None        # read/write/analyze/monitor/watcher/chat
    trigger_type: str = "discord_message"  # discord_message/cron/watcher_event/watcher_poll/forced_profile/manual
    trigger_source: str | None = None      # cron expr / event name / poll URL / user_id
    v2_job_type: str | None = None         # JobFactory v2 classification (10 types)
    v2_classification_method: str | None = None  # keyword/llm/fallback
    skill_ids: list[str] = Field(default_factory=list)  # SkillEntry ids touched
    slash_skill: str | None = None         # slash skill matched (e.g. "hybrid-memo")
    model_provider: str | None = None      # ollama/custom/claude_cli
    model_name: str | None = None          # actual model name used (local OK)
    memory_inject_count: int = 0           # P0-C inject hit count

    # Phase 9 (2026-05-06): sub-agent mention dispatch.
    # IntentRouter 가 ``@coder`` 같은 mention 을 감지하면 검증된 핸들
    # 리스트를 여기에 stamp. master 가 prompt 에 SKILL.md snippet 을
    # inject. ExperienceLog 에서 agent 별 사용 빈도 집계 가능.
    agent_handles: list[str] = Field(default_factory=list)

    # Phase 12 (2026-05-07): pipeline workflow dispatch.
    # IntentRouter 가 trigger_keyword 매치 시 pipeline_id stamp →
    # HermesMaster 가 sequential PipelineRunner 호출. 단계별 결과는
    # pipeline_results 에 누적 (handle / response / tokens / duration).
    pipeline_id: str | None = None
    pipeline_stage: int = 0                  # 마지막 완료 단계 (0-based)
    pipeline_results: list[dict[str, Any]] = Field(default_factory=list)

    # Execution
    status: Status = "pending"
    current_tier: Tier = "L2"

    # Retry management
    retry_count: int = 0
    retry_budget: int = 4
    same_tier_retries: int = 0
    tier_up_retries: int = 0

    # Errors
    error_type: ErrorType | None = None
    error_history: list[ErrorEvent] = Field(default_factory=list)

    # Execution records
    tool_outputs: list[ToolOutput] = Field(default_factory=list)
    model_outputs: list[ModelOutput] = Field(default_factory=list)

    # Hermes runtime
    hermes_trace: HermesTrace = Field(default_factory=HermesTrace)
    internal_confidence: float = 0.0
    reflection_notes: list[str] = Field(default_factory=list)

    # FIX#2: single-line reminder for the next attempt, derived from the
    # last model output via ``src.orchestrator.bump.compress_for_bump``.
    # Non-cumulative: always reflects only the most recent attempt.
    bump_prefix: str = ""

    # Cloud budget
    cloud_call_count: int = 0
    cloud_model_used: list[str] = Field(default_factory=list)
    token_budget_remaining: int = 20_000

    # HITL: present only while status == "awaiting_confirmation"
    confirmation_context: ConfirmationContext | None = None

    # Output
    final_response: str = ""
    degraded: bool = False

    # Timestamps
    timestamps: dict[str, datetime] = Field(default_factory=dict)

    # ---- mutation helpers ----

    def touch(self) -> None:
        self.updated_at = _utcnow()

    def mark(self, key: str) -> None:
        self.timestamps[key] = _utcnow()
        self.touch()

    def record_error(self, err: ErrorType, message: str, tier: Tier | None = None) -> None:
        self.error_type = err
        self.error_history.append(ErrorEvent(error_type=err, message=message, tier=tier))
        self.touch()

    def record_model_output(
        self,
        tier: Tier,
        text: str,
        model_name: str,
        prompt_tokens: int = 0,
        completion_tokens: int = 0,
        substage: str = "",
    ) -> None:
        self.model_outputs.append(
            ModelOutput(
                tier=tier,
                text=text,
                model_name=model_name,
                prompt_tokens=prompt_tokens,
                completion_tokens=completion_tokens,
                substage=substage,
            )
        )
        if tier in ("C1", "C2"):
            self.cloud_call_count += 1
            if model_name and model_name not in self.cloud_model_used:
                self.cloud_model_used.append(model_name)
            self.token_budget_remaining -= prompt_tokens + completion_tokens
        self.touch()

    def can_retry_same_tier(self, cap: int) -> bool:
        return self.same_tier_retries < cap and self.retry_count < self.retry_budget

    def can_tier_up(self, cap: int) -> bool:
        return self.tier_up_retries < cap and self.retry_count < self.retry_budget

    def switch_tier(self, new_tier: Tier) -> None:
        """R8: When moving to a new tier, the same-tier counter must reset.

        Otherwise the new tier inherits the previous tier's exhausted counter
        and cannot retry in-place even once.
        """
        if new_tier != self.current_tier:
            self.current_tier = new_tier
            self.same_tier_retries = 0
            self.touch()
