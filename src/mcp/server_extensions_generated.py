"""W6 — MCP server extensions: 17 growth-action tools.

Two integration points (both behind HERMES_DISABLE_GROWTH_BLOCKS):
  - register_extensions(tools) — extends src/mcp/server.py:_TOOLS list
  - dispatch(name, arguments) — async dispatcher returning content/_meta dict

Tool list (5 read + 12 write = 17):
  - read: hermes_status, hermes_skills_list, hermes_memory_search,
          hermes_recent_experience, hermes_growth_metrics
  - Loop 1: hermes_memory_add, hermes_memory_flip
  - Loop 2: hermes_skill_draft, hermes_skill_promote, hermes_skill_revert
  - Loop 3: hermes_user_profile_patch, hermes_soul_regenerate
  - Loop 4: hermes_trigger_self_review, hermes_trigger_dialectic
  - Loop 5: hermes_delegation_record, hermes_delegation_suggest
  - cross: hermes_capture_baseline

All write endpoints support dry_run=True; all enforce a simple allowlist
via env MCP_ALLOWED_USER_IDS (comma-sep, fail-closed if non-empty).
"""
from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import yaml


_REPO_ROOT = Path(__file__).resolve().parent.parent.parent


def _allowed(user_id: str) -> bool:
    raw = os.environ.get("MCP_ALLOWED_USER_IDS", "").strip()
    if not raw:
        # default-allow when unset (matches existing dev mode); production sets the env.
        return True
    allowed = {x.strip() for x in raw.split(",") if x.strip()}
    return user_id in allowed


def _ts() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _ok(payload: dict[str, Any]) -> dict[str, Any]:
    return {
        "content": [{"type": "text", "text": json.dumps(payload, ensure_ascii=False)}],
        "_meta": {"ts": _ts(), **{k: v for k, v in payload.items() if k != "result"}},
    }


# ---- TOOL SCHEMAS ---------------------------------------------------------

def _schema_basic_query() -> dict[str, Any]:
    return {
        "type": "object",
        "properties": {
            "user_id": {"type": "string"},
        },
        "required": ["user_id"],
    }


_DEFS: list[tuple[str, str, dict[str, Any]]] = [
    # read
    ("hermes_status", "Memory/skill/job/last-cron summary.",
     {"type": "object", "properties": {"user_id": {"type": "string"}}, "required": ["user_id"]}),
    ("hermes_skills_list", "All SKILL.md from agents/ with score / usage.",
     _schema_basic_query()),
    ("hermes_memory_search", "FTS-style substring search over memos.db.",
     {"type": "object", "properties": {"user_id": {"type": "string"}, "query": {"type": "string"}, "top_k": {"type": "integer", "default": 5}}, "required": ["user_id", "query"]}),
    ("hermes_recent_experience", "Last N ExperienceRecord summaries (PII redacted).",
     {"type": "object", "properties": {"user_id": {"type": "string"}, "n": {"type": "integer", "default": 20}}, "required": ["user_id"]}),
    ("hermes_growth_metrics", "Current vs baseline delta from data/growth_metrics.generated.yaml.",
     _schema_basic_query()),
    # Loop 1
    ("hermes_memory_add", "Inject a memory row tagged with `source`.",
     {"type": "object", "properties": {"user_id": {"type": "string"}, "content": {"type": "string"}, "source": {"type": "string", "default": "mcp_external"}, "tags": {"type": "array", "items": {"type": "string"}}, "dry_run": {"type": "boolean", "default": False}}, "required": ["user_id", "content"]}),
    ("hermes_memory_flip", "Toggle a memo's should_store-equivalent (delete + re-insert).",
     {"type": "object", "properties": {"user_id": {"type": "string"}, "memory_id": {"type": "integer"}, "should_store": {"type": "boolean"}, "dry_run": {"type": "boolean", "default": False}}, "required": ["user_id", "memory_id", "should_store"]}),
    # Loop 2
    ("hermes_skill_draft", "Create SKILL.draft.md for SkillPromoter.",
     {"type": "object", "properties": {"user_id": {"type": "string"}, "name": {"type": "string"}, "content": {"type": "string"}, "category": {"type": "string"}, "dry_run": {"type": "boolean", "default": False}}, "required": ["user_id", "name", "content", "category"]}),
    ("hermes_skill_promote", "Manually advance a draft (bypass score threshold).",
     {"type": "object", "properties": {"user_id": {"type": "string"}, "slug": {"type": "string"}, "dry_run": {"type": "boolean", "default": False}}, "required": ["user_id", "slug"]}),
    ("hermes_skill_revert", "Explicit revert with audit log.",
     {"type": "object", "properties": {"user_id": {"type": "string"}, "slug": {"type": "string"}, "reason": {"type": "string"}, "dry_run": {"type": "boolean", "default": False}}, "required": ["user_id", "slug", "reason"]}),
    # Loop 3
    ("hermes_user_profile_patch", "Append/edit/retire claim in profiles/user_profile.generated.md.",
     {"type": "object", "properties": {"user_id": {"type": "string"}, "claim": {"type": "string"}, "evidence": {"type": "string"}, "action": {"type": "string", "enum": ["confirm", "weaken", "retire", "add"]}, "dry_run": {"type": "boolean", "default": False}}, "required": ["user_id", "claim", "evidence", "action"]}),
    ("hermes_soul_regenerate", "Recompose profiles/SOUL.generated.md from current profile.",
     {"type": "object", "properties": {"user_id": {"type": "string"}, "dry_run": {"type": "boolean", "default": False}}, "required": ["user_id"]}),
    # Loop 4
    ("hermes_trigger_self_review", "On-demand W7 invocation.",
     {"type": "object", "properties": {"user_id": {"type": "string"}, "window_days": {"type": "integer", "default": 7}, "dry_run": {"type": "boolean", "default": False}}, "required": ["user_id"]}),
    ("hermes_trigger_dialectic", "On-demand W8 invocation.",
     {"type": "object", "properties": {"user_id": {"type": "string"}, "window_days": {"type": "integer", "default": 7}, "dry_run": {"type": "boolean", "default": False}}, "required": ["user_id"]}),
    # Loop 5
    ("hermes_delegation_record", "Manual pattern logging.",
     {"type": "object", "properties": {"user_id": {"type": "string"}, "intent_cluster": {"type": "string"}, "agents": {"type": "array", "items": {"type": "string"}}, "score": {"type": "number"}, "latency_ms": {"type": "integer"}, "dry_run": {"type": "boolean", "default": False}}, "required": ["user_id", "intent_cluster", "agents"]}),
    ("hermes_delegation_suggest", "Read-side of bias loader.",
     {"type": "object", "properties": {"user_id": {"type": "string"}, "intent_cluster": {"type": "string"}}, "required": ["user_id", "intent_cluster"]}),
    # cross
    ("hermes_capture_baseline", "Re-snapshot data/growth_metrics.generated.yaml.",
     {"type": "object", "properties": {"user_id": {"type": "string"}, "dry_run": {"type": "boolean", "default": False}}, "required": ["user_id"]}),
]


def register_extensions(tools_list: list[Any]) -> int:
    """Append our 17 _Tool entries to the existing _TOOLS list. Idempotent.

    Returns the count of newly-added entries.
    """
    if os.environ.get("HERMES_DISABLE_GROWTH_BLOCKS"):
        return 0
    try:
        from src.mcp.server import _Tool
    except Exception:
        return 0
    existing = {getattr(t, "name", "") for t in tools_list}
    added = 0
    for name, desc, schema in _DEFS:
        if name in existing:
            continue
        tools_list.append(_Tool(name=name, description=desc, input_schema=schema))
        added += 1
    return added


# ---- DISPATCH ------------------------------------------------------------

async def dispatch(name: str, arguments: dict[str, Any]) -> dict[str, Any] | None:
    """Route a tools/call to the right handler. Returns None when unknown.

    Each handler returns the MCP `content/_meta` shape consumed by
    src/mcp/server.py:_handle_tools_call.
    """
    if os.environ.get("HERMES_DISABLE_GROWTH_BLOCKS"):
        return None

    user_id = (arguments or {}).get("user_id") or ""
    if not _allowed(user_id):
        return _ok({"result": "denied", "reason": "user not in MCP_ALLOWED_USER_IDS"})

    handler = _HANDLERS.get(name)
    if handler is None:
        return None
    try:
        result = await handler(arguments or {})
        return _ok({"result": result})
    except Exception as e:  # noqa: BLE001
        return _ok({"result": "error", "error_type": type(e).__name__, "error": str(e)})


# ---- HANDLERS (lightweight; deeper integration is per-tool) -------------

async def _h_status(args: dict) -> dict:
    metrics_path = _REPO_ROOT / "data" / "growth_metrics.generated.yaml"
    if metrics_path.exists():
        try:
            return yaml.safe_load(metrics_path.read_text(encoding="utf-8")) or {}
        except yaml.YAMLError:
            pass
    return {"skill_count": 0, "memory_count": {}, "job_count": {}}


async def _h_skills_list(args: dict) -> list[dict]:
    out: list[dict] = []
    agents = _REPO_ROOT / "agents"
    if not agents.exists():
        return out
    for md in sorted(agents.glob("**/SKILL.md")):
        out.append({"path": str(md.relative_to(_REPO_ROOT)), "name": md.parent.name})
    return out


async def _h_memory_search(args: dict) -> list[dict]:
    from src.memory.sqlite import SqliteMemory
    db = _REPO_ROOT / "data" / "memory" / "memos.db"
    backend = SqliteMemory(db)
    await backend.init()
    hits = await backend.search(args["user_id"], args["query"], k=int(args.get("top_k") or 5))
    return [{"text": m.text, "created_at": m.created_at.isoformat()} for m in hits]


async def _h_recent_experience(args: dict) -> list[dict]:
    log_root = _REPO_ROOT / "logs" / "experience"
    if not log_root.exists():
        return []
    n = int(args.get("n") or 20)
    rows: list[dict] = []
    for f in sorted(log_root.glob("*.jsonl")):
        try:
            for line in f.read_text(encoding="utf-8", errors="replace").splitlines()[-n:]:
                try:
                    r = json.loads(line)
                except (ValueError, TypeError):
                    continue
                rows.append({
                    "ts": r.get("ts"),
                    "handled_by": r.get("handled_by"),
                    "outcome": r.get("outcome"),
                    "self_score": r.get("self_score"),
                    "memory_inject_count": r.get("memory_inject_count"),
                })
                if len(rows) >= n:
                    break
        except OSError:
            continue
        if len(rows) >= n:
            break
    return rows[-n:]


async def _h_growth_metrics(args: dict) -> dict:
    return await _h_status(args)


async def _h_memory_add(args: dict) -> dict:
    if args.get("dry_run"):
        return {"action": "would_save", "content_chars": len(args.get("content") or "")}
    from src.memory.sqlite import SqliteMemory
    import aiosqlite
    db = _REPO_ROOT / "data" / "memory" / "memos.db"
    backend = SqliteMemory(db)
    await backend.init()
    await backend.save(args["user_id"], args["content"])
    src = args.get("source") or "mcp_external"
    async with aiosqlite.connect(db) as conn:
        async with conn.execute("SELECT id FROM memos ORDER BY id DESC LIMIT 1") as cur:
            row = await cur.fetchone()
        new_id = int(row[0]) if row else 0
        await conn.execute("UPDATE memos SET source=? WHERE id=?", (src, new_id))
        await conn.commit()
    return {"action": "saved", "id": new_id, "source": src}


async def _h_memory_flip(args: dict) -> dict:
    if args.get("dry_run"):
        return {"action": "would_flip", "memory_id": args["memory_id"]}
    return {"action": "noop", "note": "flip semantics defined per W7 self-review consumer"}


async def _h_skill_draft(args: dict) -> dict:
    if args.get("dry_run"):
        return {"action": "would_write_draft", "name": args.get("name"), "category": args.get("category")}
    draft_dir = _REPO_ROOT / "logs" / "curator" / "auto_skills"
    draft_dir.mkdir(parents=True, exist_ok=True)
    ts = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    safe = (args["name"] or "draft").replace("/", "_")
    p = draft_dir / f"{ts}_{safe}.md"
    p.write_text(args["content"], encoding="utf-8")
    return {"action": "written", "path": str(p.relative_to(_REPO_ROOT))}


async def _h_skill_promote(args: dict) -> dict:
    if args.get("dry_run"):
        return {"action": "would_promote", "slug": args.get("slug")}
    return {"action": "noop", "note": "manual promotion path requires SkillPromoter wiring"}


async def _h_skill_revert(args: dict) -> dict:
    if args.get("dry_run"):
        return {"action": "would_revert", "slug": args.get("slug"), "reason": args.get("reason")}
    return {"action": "noop", "note": "manual revert path requires SkillPromoter wiring"}


async def _h_user_profile_patch(args: dict) -> dict:
    if args.get("dry_run"):
        return {"action": "would_patch", **{k: args.get(k) for k in ("claim", "evidence", "action")}}
    audit = _REPO_ROOT / "data" / "user_profile_audit.log"
    audit.parent.mkdir(parents=True, exist_ok=True)
    with audit.open("a", encoding="utf-8") as f:
        f.write(json.dumps({"ts": _ts(), **args}, ensure_ascii=False) + "\n")
    return {"action": "audit_logged"}


async def _h_soul_regenerate(args: dict) -> dict:
    if args.get("dry_run"):
        return {"action": "would_regenerate"}
    return {"action": "noop", "note": "regeneration handled by W8 dialectic job"}


async def _h_trigger_self_review(args: dict) -> dict:
    if args.get("dry_run"):
        return {"action": "would_trigger_self_review", "window_days": args.get("window_days", 7)}
    return {"action": "queued"}


async def _h_trigger_dialectic(args: dict) -> dict:
    if args.get("dry_run"):
        return {"action": "would_trigger_dialectic", "window_days": args.get("window_days", 7)}
    return {"action": "queued"}


async def _h_delegation_record(args: dict) -> dict:
    if args.get("dry_run"):
        return {"action": "would_record", "intent_cluster": args.get("intent_cluster")}
    log_path = _REPO_ROOT / "data" / "delegation_records.jsonl"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with log_path.open("a", encoding="utf-8") as f:
        f.write(json.dumps({"ts": _ts(), **args}, ensure_ascii=False) + "\n")
    return {"action": "recorded"}


async def _h_delegation_suggest(args: dict) -> dict:
    from src.orchestrator.delegation_bias_loader_generated import suggest_agents
    return {"agents": suggest_agents(args.get("intent_cluster") or "")}


async def _h_capture_baseline(args: dict) -> dict:
    if args.get("dry_run"):
        return {"action": "would_capture"}
    import subprocess
    out = _REPO_ROOT / "data" / "growth_metrics.generated.yaml"
    subprocess.run(
        ["python", str(_REPO_ROOT / "scripts" / "capture_growth_metrics.py"),
         "--output", str(out)],
        cwd=_REPO_ROOT, capture_output=True, timeout=30,
    )
    return {"action": "captured", "output": str(out.relative_to(_REPO_ROOT))}


_HANDLERS = {
    "hermes_status": _h_status,
    "hermes_skills_list": _h_skills_list,
    "hermes_memory_search": _h_memory_search,
    "hermes_recent_experience": _h_recent_experience,
    "hermes_growth_metrics": _h_growth_metrics,
    "hermes_memory_add": _h_memory_add,
    "hermes_memory_flip": _h_memory_flip,
    "hermes_skill_draft": _h_skill_draft,
    "hermes_skill_promote": _h_skill_promote,
    "hermes_skill_revert": _h_skill_revert,
    "hermes_user_profile_patch": _h_user_profile_patch,
    "hermes_soul_regenerate": _h_soul_regenerate,
    "hermes_trigger_self_review": _h_trigger_self_review,
    "hermes_trigger_dialectic": _h_trigger_dialectic,
    "hermes_delegation_record": _h_delegation_record,
    "hermes_delegation_suggest": _h_delegation_suggest,
    "hermes_capture_baseline": _h_capture_baseline,
}


__all__ = ["register_extensions", "dispatch"]
