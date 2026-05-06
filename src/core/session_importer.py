"""Hermes session JSON → ExperienceRecord importer (Phase 2).

Cron jobs and watchers run via the Hermes scheduler — they bypass the
Orchestrator entirely, so the existing ``_log_task_end`` hook never sees
them. Without this importer, the experience log only contains direct
Discord traffic, which is a tiny minority of the bot's actual workload.

Mechanism: hermes writes one JSON per LLM invocation to
``~/.hermes/sessions/session_<id>.json``. We poll that directory, convert
unprocessed files into ExperienceRecord rows, and let ReflectionJob /
CuratorJob aggregate them alongside the Orchestrator's own writes.

Privacy contract is the same as the live logger: input/response text is
hashed (sha16) and length-stamped only — never stored verbatim.

Dedup: the importer keeps a ``.processed`` JSON next to the experience
log root recording every session_id it has already converted. Re-running
is idempotent.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from src.core.experience_logger import ExperienceLogger, ExperienceRecord, _sha16


_PROCESSED_FILE_NAME = ".hermes_sessions_processed.json"


def _load_processed(state_path: Path) -> set[str]:
    if not state_path.exists():
        return set()
    try:
        data = json.loads(state_path.read_text(encoding="utf-8"))
    except (ValueError, OSError):
        return set()
    if isinstance(data, dict):
        ids = data.get("processed_session_ids", [])
    else:
        ids = data
    return {str(x) for x in ids if x}


def _save_processed(state_path: Path, processed: set[str]) -> None:
    state_path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "updated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "count": len(processed),
        "processed_session_ids": sorted(processed),
    }
    state_path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
    )


def _first_text(messages: list[dict[str, Any]], role: str) -> str:
    """First message content with the given role, as a string. Empty if missing."""
    for m in messages:
        if m.get("role") != role:
            continue
        c = m.get("content")
        if isinstance(c, str) and c.strip():
            return c
        if isinstance(c, list):
            return json.dumps(c, ensure_ascii=False)[:1000]
    return ""


def _last_text(messages: list[dict[str, Any]], role: str) -> str:
    for m in reversed(messages):
        if m.get("role") != role:
            continue
        c = m.get("content")
        if isinstance(c, str) and c.strip():
            return c
        if isinstance(c, list):
            return json.dumps(c, ensure_ascii=False)[:1000]
    return ""


def _tool_calls_from_messages(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Extract per-tool (tool, ok, ms) entries from the messages array.

    Hermes doesn't tag tool failures in-band, so ``ok`` defaults to True
    for now and is overwritten if the next ``role=tool`` message reports
    an explicit error string. Time-per-tool is unavailable at this level
    (would require diff'ing message timestamps); ``ms=0`` is honest.
    """
    out: list[dict[str, Any]] = []
    pending: dict[str, str] = {}
    for m in messages:
        role = m.get("role")
        if role == "assistant":
            for tc in m.get("tool_calls") or []:
                fn = (tc.get("function") or {}) if isinstance(tc, dict) else {}
                tool = fn.get("name") or "?"
                tc_id = tc.get("id") if isinstance(tc, dict) else None
                pending[tc_id or tool] = tool
                out.append({"tool": tool, "ok": True, "ms": 0, "lane": "hermes"})
        elif role == "tool":
            # Best-effort failure detection: tool messages that look like
            # error reports flip the most recent ``ok`` for that tool.
            content = m.get("content")
            text = content if isinstance(content, str) else json.dumps(content or "")
            if any(marker in text.lower() for marker in ("error", "exception", "traceback")):
                for entry in reversed(out):
                    if entry.get("tool") == m.get("name"):
                        entry["ok"] = False
                        break
    return out


def session_to_record(
    session_json: dict[str, Any],
    *,
    profile_id: str | None,
    job_id: str | None,
    trigger_type: str,
    trigger_source: str | None,
    fallback_user_id: str = "system",
    file_mtime: datetime | None = None,
) -> ExperienceRecord:
    """Project a hermes session JSON into an ExperienceRecord.

    Caller-supplied ``profile_id`` / ``job_id`` / ``trigger_type`` come
    from the file path (``~/.hermes/profiles/{profile}/sessions/...``)
    or the cron jobs.json metadata — the session JSON itself rarely
    carries that context.
    """
    messages = session_json.get("messages") or []
    if not isinstance(messages, list):
        messages = []

    user_text = _first_text(messages, "user")
    response_text = _last_text(messages, "assistant")

    # ts: prefer session_json's ended_at, else mtime, else now()
    ts_raw = session_json.get("ended_at") or session_json.get("started_at")
    if ts_raw:
        try:
            ts = datetime.fromisoformat(str(ts_raw).replace("Z", "+00:00"))
        except (ValueError, TypeError):
            ts = file_mtime or datetime.now(timezone.utc)
    elif file_mtime:
        ts = file_mtime
    else:
        ts = datetime.now(timezone.utc)
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)

    # Cost / model: from modelUsage[] if available, else top-level model
    usages = session_json.get("modelUsage") or []
    if not isinstance(usages, list):
        usages = []
    prompt_tokens = sum(int(u.get("prompt_tokens") or 0) for u in usages)
    completion_tokens = sum(int(u.get("completion_tokens") or 0) for u in usages)
    cost_usd = float(session_json.get("total_cost_usd") or 0.0)
    cloud_calls = sum(1 for u in usages if (u.get("cost_usd") or 0) > 0)
    cloud_models = [str(u.get("model")) for u in usages if (u.get("cost_usd") or 0) > 0]

    # Provider/model: top-level provider + first usage's model.
    provider = session_json.get("provider")
    model_name = None
    if usages:
        model_name = usages[0].get("model")
    if not model_name:
        model_name = session_json.get("model")

    # tool_calls
    tool_calls = _tool_calls_from_messages(messages)
    skills_invoked = session_json.get("skills_invoked") or []
    if not isinstance(skills_invoked, list):
        skills_invoked = []

    # Outcome: hermes session JSON usually has no explicit status. Best-
    # effort: any tool_call marked ok=False → degraded; non-empty
    # response_text → succeeded; empty → failed.
    has_response = bool(response_text.strip())
    has_failed_tool = any(not tc.get("ok", True) for tc in tool_calls)
    if not has_response:
        outcome, status = "failed", "failed"
    elif has_failed_tool:
        outcome, status = "degraded", "succeeded"
    else:
        outcome, status = "succeeded", "succeeded"

    # session_id → task_id (1:1 mapping for dedup); fallback to a hash.
    session_id = str(session_json.get("session_id") or "")
    task_id = session_id or _sha16(json.dumps(session_json, sort_keys=True)[:500])

    # turns_used → hermes_turns
    hermes_turns = int(session_json.get("turns_used") or len(usages))

    return ExperienceRecord(
        ts=ts.isoformat(timespec="seconds"),
        task_id=task_id,
        session_id=session_id or task_id,
        user_id=fallback_user_id,
        profile=profile_id,
        forced_profile=None,
        heavy=False,
        route="cloud" if cloud_calls > 0 else "local",
        tier="C1" if cloud_calls > 0 else "L2",
        handled_by=f"hermes-session:{trigger_type}",
        status=status,
        outcome=outcome,
        degraded=(outcome == "degraded"),
        latency_ms=int(session_json.get("duration_ms") or 0),
        cloud_calls=cloud_calls,
        cloud_models=cloud_models,
        prompt_tokens=prompt_tokens,
        completion_tokens=completion_tokens,
        retries=0,
        tier_ups=0,
        same_tier_retries=0,
        error_types=[],
        last_error_message="",
        tool_calls=tool_calls,
        hermes_turns=hermes_turns,
        hermes_reflection_count=0,
        self_score=0.0,  # cron path doesn't go through Critic
        # Phase 1.5 routing context
        job_id=job_id,
        job_category=None,
        trigger_type=trigger_type,
        trigger_source=trigger_source,
        v2_job_type=None,
        v2_classification_method=None,
        skill_ids=[str(s) for s in skills_invoked],
        slash_skill=None,
        model_provider=str(provider) if provider else None,
        model_name=str(model_name) if model_name else None,
        memory_inject_count=0,
        # Privacy
        input_text_hash=_sha16(user_text),
        input_text_length=len(user_text),
        response_hash=_sha16(response_text),
        response_length=len(response_text),
    )


def import_sessions(
    sessions_dir: Path,
    logger: ExperienceLogger,
    *,
    state_path: Path | None = None,
    trigger_type: str = "cron",
    profile_id_from_path: bool = True,
    fallback_user_id: str = "system",
) -> dict[str, Any]:
    """Scan ``sessions_dir`` for new session_*.json and append them.

    Returns a metrics dict (``imported``, ``skipped``, ``errors``).

    ``state_path`` defaults to ``logger.root / .hermes_sessions_processed.json``.
    Re-running is idempotent because every session_id already in the
    state file is skipped.
    """
    sessions_dir = Path(sessions_dir)
    if not sessions_dir.exists():
        return {"imported": 0, "skipped": 0, "errors": 0, "reason": "missing dir"}

    state_path = state_path or (logger.root / _PROCESSED_FILE_NAME)
    processed = _load_processed(state_path)

    imported = 0
    skipped = 0
    errors = 0
    new_processed: set[str] = set()

    for path in sorted(sessions_dir.rglob("session_*.json")):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (ValueError, OSError):
            errors += 1
            continue
        if not isinstance(data, dict):
            errors += 1
            continue

        session_id = str(data.get("session_id") or "")
        dedup_key = session_id or path.as_posix()
        if dedup_key in processed:
            skipped += 1
            continue

        # Derive profile_id / job_id / trigger_source from path.
        # Heuristic: ``.../profiles/{profile}/sessions/...`` → profile.
        profile_id = None
        job_id = None
        if profile_id_from_path:
            parts = path.resolve().parts
            for i, p in enumerate(parts):
                if p == "profiles" and i + 1 < len(parts):
                    profile_id = parts[i + 1]
                    break

        try:
            mtime = datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc)
        except OSError:
            mtime = None

        try:
            record = session_to_record(
                data,
                profile_id=profile_id,
                job_id=job_id,
                trigger_type=trigger_type,
                trigger_source=path.as_posix(),
                fallback_user_id=fallback_user_id,
                file_mtime=mtime,
            )
        except Exception:  # noqa: BLE001
            errors += 1
            continue

        # Append directly: bypass logger.append (which expects a TaskState).
        # Reuse the writer path so date-sharding stays consistent.
        try:
            logger._write_line(record)  # noqa: SLF001 — intentional reuse
        except OSError:
            errors += 1
            continue

        imported += 1
        new_processed.add(dedup_key)

    if new_processed:
        _save_processed(state_path, processed | new_processed)

    return {
        "imported": imported,
        "skipped": skipped,
        "errors": errors,
        "state_path": str(state_path),
    }


__all__ = [
    "import_sessions",
    "session_to_record",
]
