"""W10 — real-time recurring-request detector.

Maintains data/recurring_request_log.jsonl (30-day rolling). On each new
request:
  - append { ts, user_id, text, text_hash } to the log
  - tokenize and run a simple Jaccard similarity check vs last-30d entries
  - if >=3 entries within last 14 days have Jaccard >= 0.7 AND no existing
    SKILL.md when_to_use covers the cluster, append to data/skill_draft_queue.jsonl

Called from src/orchestrator/hermes_master.py:_dispatch_master() inside
the W10 marker block. Best-effort: any failure logs a warning and returns.

Notes:
  - ExperienceRecord does NOT store raw user_message text (only hash + length
    per src/core/experience_logger.py:139). This module maintains its own
    text store for similarity work.
  - Honors HERMES_DISABLE_GROWTH_BLOCKS (caller checks).
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent.parent
LOG_PATH = _REPO_ROOT / "data" / "recurring_request_log.jsonl"
QUEUE_PATH = _REPO_ROOT / "data" / "skill_draft_queue.jsonl"
AGENTS_ROOT = _REPO_ROOT / "agents"

JACCARD_THRESHOLD = 0.7
MIN_SIMILAR = 3
WINDOW_DAYS = 14
RETENTION_DAYS = 30

_TOKEN_RE = re.compile(r"[a-zA-Z가-힣0-9]{2,}")
_LOCK = asyncio.Lock()


def _tokens(text: str) -> set[str]:
    return set(_TOKEN_RE.findall((text or "").lower()))


def _jaccard(a: set[str], b: set[str]) -> float:
    if not a or not b:
        return 0.0
    inter = len(a & b)
    union = len(a | b)
    return inter / union if union else 0.0


def _hash(text: str) -> str:
    return hashlib.sha256((text or "").encode("utf-8")).hexdigest()[:16]


def _read_recent(now: datetime, days: int) -> list[dict]:
    if not LOG_PATH.exists():
        return []
    cutoff = now - timedelta(days=days)
    out: list[dict] = []
    try:
        for line in LOG_PATH.read_text(encoding="utf-8", errors="replace").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except (ValueError, TypeError):
                continue
            ts = row.get("ts", "")
            try:
                dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
            except (ValueError, TypeError):
                continue
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            if dt >= cutoff:
                out.append(row)
    except OSError:
        return []
    return out


def _existing_skill_covers(tokens: set[str]) -> bool:
    if not AGENTS_ROOT.exists():
        return False
    for md in AGENTS_ROOT.glob("**/SKILL.md"):
        try:
            text = md.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        body_tokens = _tokens(text)
        if _jaccard(tokens, body_tokens) >= 0.5:
            return True
    return False


def _append_log(row: dict) -> None:
    LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    with LOG_PATH.open("a", encoding="utf-8") as f:
        f.write(json.dumps(row, ensure_ascii=False) + "\n")


def _enqueue_draft(cluster: dict) -> None:
    QUEUE_PATH.parent.mkdir(parents=True, exist_ok=True)
    with QUEUE_PATH.open("a", encoding="utf-8") as f:
        f.write(json.dumps(cluster, ensure_ascii=False) + "\n")


async def maybe_enqueue_skill_draft(*, user_text: str, user_id: str) -> bool:
    """Append the request, run the similarity check, possibly enqueue.

    Returns True if a draft was enqueued.
    """
    if os.environ.get("HERMES_DISABLE_GROWTH_BLOCKS"):
        return False
    text = (user_text or "").strip()
    if not text:
        return False

    now = datetime.now(timezone.utc)
    row = {
        "ts": now.isoformat(timespec="seconds"),
        "user_id": user_id or "",
        "text": text,
        "text_hash": _hash(text),
    }

    async with _LOCK:
        _append_log(row)
        recent = _read_recent(now, WINDOW_DAYS)

    new_tokens = _tokens(text)
    if not new_tokens:
        return False

    similar = []
    for r in recent:
        if r.get("text_hash") == row["text_hash"]:
            continue
        rt = _tokens(r.get("text") or "")
        if _jaccard(new_tokens, rt) >= JACCARD_THRESHOLD:
            similar.append(r)

    if len(similar) + 1 < MIN_SIMILAR:
        return False
    if _existing_skill_covers(new_tokens):
        return False

    cluster = {
        "ts": now.isoformat(timespec="seconds"),
        "intent_token_sample": sorted(new_tokens)[:20],
        "similar_count": len(similar) + 1,
        "user_ids": list({user_id, *(r.get("user_id") or "" for r in similar)}),
        "first_seen": min(r.get("ts", row["ts"]) for r in similar) if similar else row["ts"],
        "latest_seen": row["ts"],
        "intent_cluster_hint": "github_repo_analysis"
        if any("github" in t for t in new_tokens)
        else "generic",
    }
    async with _LOCK:
        _enqueue_draft(cluster)
    return True


def prune_old(now: datetime | None = None) -> int:
    """Drop log rows older than RETENTION_DAYS. Returns rows kept."""
    if not LOG_PATH.exists():
        return 0
    now = now or datetime.now(timezone.utc)
    cutoff = now - timedelta(days=RETENTION_DAYS)
    kept: list[str] = []
    try:
        for line in LOG_PATH.read_text(encoding="utf-8", errors="replace").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except (ValueError, TypeError):
                continue
            ts = row.get("ts", "")
            try:
                dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
            except (ValueError, TypeError):
                continue
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            if dt >= cutoff:
                kept.append(line)
    except OSError:
        return 0
    LOG_PATH.write_text("\n".join(kept) + ("\n" if kept else ""), encoding="utf-8")
    return len(kept)


__all__ = ["maybe_enqueue_skill_draft", "prune_old"]
