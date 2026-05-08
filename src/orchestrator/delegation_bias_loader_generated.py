"""W12 — delegation bias loader.

Reads the latest data/delegation_patterns_*.yaml and exposes:
  - classify_intent_cluster(text) -> str
  - suggest_agents(intent_cluster) -> list[str]

Day-0 mode is OBSERVATION-ONLY. The W12 marker block in
src/orchestrator/hermes_master.py:_dispatch_master() only LOGS the suggestion
to bot_stdout.log; it does NOT mutate task.agent_handles.

Honors HERMES_DISABLE_GROWTH_BLOCKS (caller checks).
"""
from __future__ import annotations

import re
from pathlib import Path

import yaml


_REPO_ROOT = Path(__file__).resolve().parent.parent.parent
_PATTERNS_GLOB = "delegation_patterns_*.yaml"
_DATA_DIR = _REPO_ROOT / "data"

_cache: tuple[float, dict] | None = None


def _latest_yaml() -> Path | None:
    if not _DATA_DIR.exists():
        return None
    candidates = sorted(_DATA_DIR.glob(_PATTERNS_GLOB))
    return candidates[-1] if candidates else None


def _load(path: Path) -> dict:
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except (OSError, yaml.YAMLError):
        return {}
    return data if isinstance(data, dict) else {}


def _ensure_cache() -> dict:
    global _cache
    p = _latest_yaml()
    if p is None:
        _cache = (0.0, {})
        return {}
    try:
        mtime = p.stat().st_mtime
    except OSError:
        _cache = (0.0, {})
        return {}
    if _cache is not None and _cache[0] == mtime:
        return _cache[1]
    data = _load(p)
    _cache = (mtime, data)
    return data


_INTENT_TOKENS = {
    "github_repo_analysis": ("github", "repo", "레포"),
    "code_review": ("review", "리뷰", "diff"),
    "research": ("검색", "찾아", "조사"),
    "schedule_logging": ("기록", "일기", "schedule"),
    "weather": ("날씨", "weather"),
    "interview": ("면접", "interview"),
}


def classify_intent_cluster(text: str) -> str:
    """Light-touch keyword classifier. Returns 'generic' when no match."""
    if not text:
        return "generic"
    low = text.lower()
    for cluster, kws in _INTENT_TOKENS.items():
        for k in kws:
            if k in low:
                return cluster
    return "generic"


def suggest_agents(intent_cluster: str) -> list[str]:
    """Return @handles ranked by avg_score from the latest patterns yaml.

    Empty list when no data exists for the cluster (graceful default).
    """
    data = _ensure_cache()
    rows = data.get("clusters") if isinstance(data, dict) else None
    if not rows:
        rows = []
        for k, v in (data or {}).items():
            if isinstance(v, dict) and v.get("intent_cluster") == intent_cluster:
                rows = [v]
                break
    out: list[str] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        if row.get("intent_cluster") != intent_cluster:
            continue
        best = row.get("best_combos") or []
        for combo in best:
            agents = (combo or {}).get("agents") or []
            for a in agents:
                if isinstance(a, str) and a not in out:
                    out.append(a)
        break
    return out


__all__ = ["classify_intent_cluster", "suggest_agents"]
