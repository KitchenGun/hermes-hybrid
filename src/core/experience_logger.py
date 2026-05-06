"""Experience Logger — append-only JSONL of every finished task.

The first brick of the growth loop. Every Orchestrator request that reaches
``_log_task_end`` produces one JSONL line in
``{root}/{YYYY-MM-DD}.jsonl`` (UTC date — the file rolls at 00:00 UTC, not
KST, so weekly aggregations across timezones stay consistent).

Privacy
-------
The raw ``user_message`` and ``final_response`` are NEVER stored — only:
  * sha256 hex prefix (16 chars) — enough to dedupe identical inputs,
    not enough to reverse the text
  * character length

That keeps these files cheap to share for debugging without leaking
journal entries / calendar contents / job-search context.

Read path
---------
``query`` streams records from the date-sharded files in time order.
Reflection / Curator jobs read this to extract patterns. The logger
itself never reads its own output — keeping it write-only also means
a corrupt file (partial last line) only affects analysis, not the bot.
"""
from __future__ import annotations

import hashlib
import json
import os
from collections.abc import Iterator
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field

from src.state import TaskState


def _sha16(s: str) -> str:
    """SHA-256 hex prefix (16 chars). Empty string → ``''`` (still a marker)."""
    if not s:
        return ""
    return hashlib.sha256(s.encode("utf-8", errors="replace")).hexdigest()[:16]


def _utc_today() -> date:
    return datetime.now(timezone.utc).date()


class ExperienceRecord(BaseModel):
    """One row of the experience log.

    Schema is intentionally flat — JSONL consumers (jq, pandas, awk) work
    best on flat keys. Nested dicts are reserved for genuinely tree-shaped
    data (``tool_outputs``).
    """
    ts: str                           # ISO8601 UTC, second precision
    task_id: str
    session_id: str
    user_id: str

    # Routing context
    profile: str | None = None        # JobFactory v1 match (job_profile_id)
    forced_profile: str | None = None # channel-pinned profile, if any
    heavy: bool = False               # !heavy opt-in path
    route: str = "local"              # local | worker | cloud
    tier: str = "L2"                  # final tier reached
    handled_by: str = ""              # _log_task_end's handled_by token

    # Phase 1.5 (2026-05-06): expanded routing context.
    # Stamped on TaskState by Orchestrator's matching branch; logger just
    # passes them through. None / empty defaults mean "this dispatch path
    # didn't carry that signal".
    job_id: str | None = None
    job_category: str | None = None
    trigger_type: str = "discord_message"
    trigger_source: str | None = None
    v2_job_type: str | None = None
    v2_classification_method: str | None = None
    skill_ids: list[str] = Field(default_factory=list)
    slash_skill: str | None = None
    model_provider: str | None = None
    model_name: str | None = None
    memory_inject_count: int = 0

    # Outcome
    status: str = "pending"           # TaskState.status at end
    outcome: str = "succeeded"        # succeeded | failed | degraded
    degraded: bool = False
    latency_ms: int = 0

    # Cost
    cloud_calls: int = 0
    cloud_models: list[str] = Field(default_factory=list)
    prompt_tokens: int = 0
    completion_tokens: int = 0

    # Retries
    retries: int = 0
    tier_ups: int = 0
    same_tier_retries: int = 0

    # Errors (truncated)
    error_types: list[str] = Field(default_factory=list)
    last_error_message: str = ""

    # Tools (no payloads — just shape & success)
    tool_calls: list[dict[str, Any]] = Field(default_factory=list)
    hermes_turns: int = 0
    hermes_reflection_count: int = 0

    # Critic — soft quality signal stamped by ``src.core.critic.Critic``.
    # Diagnostic only; never used for retry/tier policy. Range [0, 1].
    self_score: float = 0.0

    # Privacy-preserved input/output
    input_text_hash: str = ""
    input_text_length: int = 0
    response_hash: str = ""
    response_length: int = 0


def _record_from_task(
    task: TaskState,
    *,
    handled_by: str,
    latency_ms: int,
) -> ExperienceRecord:
    """Project a ``TaskState`` into the public, privacy-safe record."""
    error_types = [e.error_type for e in task.error_history]
    last_error = (
        task.error_history[-1].message[:200] if task.error_history else ""
    )

    tool_calls: list[dict[str, Any]] = [
        {"tool": t.tool, "ok": t.ok, "ms": t.ms} for t in task.tool_outputs
    ]
    # Hermes-lane tool calls live in hermes_trace.actions/observations.
    # Pair them up so the tool name matches the success flag.
    hermes_acts = task.hermes_trace.actions
    hermes_obs = {o.action_id: o for o in task.hermes_trace.observations}
    for act in hermes_acts:
        obs = hermes_obs.get(act.action_id)
        tool_calls.append({
            "tool": act.tool,
            "ok": bool(obs and obs.schema_ok),
            "ms": obs.duration_ms if obs else 0,
            "lane": "hermes",
        })

    prompt_tokens = sum(m.prompt_tokens for m in task.model_outputs)
    completion_tokens = sum(m.completion_tokens for m in task.model_outputs)

    if task.status == "succeeded" and task.degraded:
        outcome = "degraded"
    elif task.status == "succeeded":
        outcome = "succeeded"
    else:
        outcome = "failed"

    return ExperienceRecord(
        ts=datetime.now(timezone.utc).isoformat(timespec="seconds"),
        task_id=task.task_id,
        session_id=task.session_id,
        user_id=str(task.user_id),
        profile=task.job_profile_id,
        forced_profile=task.forced_profile,
        heavy=task.heavy,
        route=task.route,
        tier=task.current_tier,
        handled_by=handled_by,
        status=task.status,
        outcome=outcome,
        degraded=task.degraded,
        latency_ms=latency_ms,
        cloud_calls=task.cloud_call_count,
        cloud_models=list(task.cloud_model_used),
        prompt_tokens=prompt_tokens,
        completion_tokens=completion_tokens,
        retries=task.retry_count,
        tier_ups=task.tier_up_retries,
        same_tier_retries=task.same_tier_retries,
        error_types=error_types,
        last_error_message=last_error,
        tool_calls=tool_calls,
        hermes_turns=len(hermes_acts),
        hermes_reflection_count=len(task.hermes_trace.reflections),
        self_score=task.internal_confidence,
        # Phase 1.5: expanded routing context — passed through verbatim.
        job_id=task.job_id,
        job_category=task.job_category,
        trigger_type=task.trigger_type,
        trigger_source=task.trigger_source,
        v2_job_type=task.v2_job_type,
        v2_classification_method=task.v2_classification_method,
        skill_ids=list(task.skill_ids),
        slash_skill=task.slash_skill,
        model_provider=task.model_provider,
        model_name=task.model_name,
        memory_inject_count=task.memory_inject_count,
        input_text_hash=_sha16(task.user_message),
        input_text_length=len(task.user_message or ""),
        response_hash=_sha16(task.final_response),
        response_length=len(task.final_response or ""),
    )


class ExperienceLogger:
    """Append-only JSONL writer. Cheap, sync, and silent on failure.

    Failures (disk full, permission, encoding) are logged at WARN and
    swallowed — the bot's response path must never fail because the
    experience log can't be written.
    """

    def __init__(self, root: Path, *, enabled: bool = True):
        self.root = Path(root)
        self.enabled = enabled

    # ---- write ----

    def append(
        self,
        task: TaskState,
        *,
        handled_by: str,
        latency_ms: int,
    ) -> ExperienceRecord | None:
        if not self.enabled:
            return None
        record = _record_from_task(task, handled_by=handled_by, latency_ms=latency_ms)
        try:
            self._write_line(record)
        except OSError:
            # Best-effort: the bot must keep working even if the log can't
            # be written. The structlog ``task.end`` line in the orchestrator
            # is still produced, so there is a parallel signal.
            return record
        return record

    def _write_line(self, record: ExperienceRecord) -> None:
        path = self._path_for(_utc_today())
        path.parent.mkdir(parents=True, exist_ok=True)
        line = record.model_dump_json(exclude_defaults=False)
        with path.open("a", encoding="utf-8") as f:
            f.write(line + "\n")
            # fsync would protect against power loss but at ~30x cost.
            # The structlog task.end line is the durable signal; this file
            # is for analysis, so eventual consistency is acceptable.

    def _path_for(self, day: date) -> Path:
        return self.root / f"{day.isoformat()}.jsonl"

    # ---- read (used by reflection_job, curator_job) ----

    def query(
        self,
        *,
        since: datetime,
        until: datetime | None = None,
        profile: str | None = None,
    ) -> Iterator[ExperienceRecord]:
        """Stream records in [since, until) UTC, optionally filtered by profile.

        Skips malformed lines silently — corrupt tail of an incomplete write
        will not block analysis of the rest.
        """
        end = until or datetime.now(timezone.utc)
        if since.tzinfo is None:
            since = since.replace(tzinfo=timezone.utc)
        if end.tzinfo is None:
            end = end.replace(tzinfo=timezone.utc)

        day = since.date()
        last_day = end.date()
        while day <= last_day:
            path = self._path_for(day)
            if path.exists():
                yield from self._iter_file(path, since=since, until=end, profile=profile)
            day = day + timedelta(days=1)

    def _iter_file(
        self,
        path: Path,
        *,
        since: datetime,
        until: datetime,
        profile: str | None,
    ) -> Iterator[ExperienceRecord]:
        with path.open("r", encoding="utf-8") as f:
            for raw in f:
                raw = raw.strip()
                if not raw:
                    continue
                try:
                    data = json.loads(raw)
                    rec = ExperienceRecord(**data)
                except (ValueError, TypeError):
                    continue
                ts = datetime.fromisoformat(rec.ts)
                if ts.tzinfo is None:
                    ts = ts.replace(tzinfo=timezone.utc)
                if ts < since or ts >= until:
                    continue
                if profile is not None and rec.profile != profile:
                    continue
                yield rec
