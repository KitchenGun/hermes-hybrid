#!/usr/bin/env python3
"""advisor_ops job inventory scanner.

Walks every profile under ``--profiles-root`` and emits a single JSON
inventory: config snapshot, skills tree, cron/on_demand/watcher jobs, and
a few prompt-text hints (TODO/FIXME, MCP/plugin mentions). The advisor
LLM consumes this JSON as the input for its recommendation pass.

Usage (typical):
    python3 scan_jobs.py --output /tmp/advisor_inventory.json

Design notes:
    - Read-only. Never writes outside --output (and --output is optional).
    - Single yaml parse failure does not abort the run; recorded in parse_errors.
    - Prompt body is hashed + length-only; full text stays on disk.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

try:
    import yaml
except ImportError:
    print("error: PyYAML required (pip install pyyaml)", file=sys.stderr)
    sys.exit(2)

_KST = timezone(timedelta(hours=9), name="KST")
_DEFAULT_PROFILES_ROOT = Path("/home/kang/.hermes/profiles")
_TRIGGER_DIRS = ("cron", "on_demand", "watchers")
_HINT_PATTERNS = [
    ("TODO", re.compile(r"\bTODO\b", re.IGNORECASE)),
    ("FIXME", re.compile(r"\bFIXME\b", re.IGNORECASE)),
    ("mcp", re.compile(r"\bMCP\b")),
    ("plugin", re.compile(r"\bplugin(s)?\b", re.IGNORECASE)),
    ("hook", re.compile(r"\bhook(s)?\b", re.IGNORECASE)),
]


def _safe_load_yaml(path: Path) -> tuple[dict | None, str | None]:
    try:
        with path.open(encoding="utf-8") as f:
            data = yaml.safe_load(f)
        if not isinstance(data, dict):
            return None, f"non-mapping yaml: {path}"
        return data, None
    except (yaml.YAMLError, OSError) as exc:
        return None, f"{path}: {exc}"


def _extract_config_snapshot(cfg: dict) -> dict[str, Any]:
    return {
        "model": cfg.get("model") or {},
        "web_backend": (cfg.get("web") or {}).get("backend"),
        "mcp_servers": cfg.get("mcp_servers") or {},
        "skills_auto_load": (cfg.get("skills") or {}).get("auto_load", []),
        "tier_policy": (cfg.get("x-hermes-hybrid") or {}).get("tier_policy") or {},
        "approvals": cfg.get("approvals") or {},
        "disabled_toolsets": (cfg.get("agent") or {}).get("disabled_toolsets", []),
    }


def _list_skills(profile_dir: Path) -> list[dict[str, Any]]:
    skills_dir = profile_dir / "skills"
    if not skills_dir.is_dir():
        return []
    out: list[dict[str, Any]] = []
    for category_dir in sorted(p for p in skills_dir.iterdir() if p.is_dir()):
        for skill_dir in sorted(p for p in category_dir.iterdir() if p.is_dir()):
            scripts_dir = skill_dir / "scripts"
            scripts = sorted(p.name for p in scripts_dir.iterdir()) if scripts_dir.is_dir() else []
            out.append({
                "category": category_dir.name,
                "name": skill_dir.name,
                "has_scripts": bool(scripts),
                "script_count": len(scripts),
                "skill_md_present": (skill_dir / "SKILL.md").exists(),
            })
    return out


def _hash_prompt(prompt: str) -> str:
    return hashlib.sha256(prompt.encode("utf-8")).hexdigest()[:16]


def _scan_prompt_hints(prompt: str) -> list[dict[str, str]]:
    """Cheap keyword matches; the LLM does the real analysis afterwards."""
    hints: list[dict[str, str]] = []
    if not prompt:
        return hints
    for kind, pat in _HINT_PATTERNS:
        m = pat.search(prompt)
        if m:
            start = max(0, m.start() - 30)
            end = min(len(prompt), m.end() + 30)
            snippet = prompt[start:end].replace("\n", " ").strip()
            hints.append({"kind": kind, "snippet": snippet[:140]})
    return hints


def _list_jobs(
    profile_dir: Path,
) -> tuple[list[dict[str, Any]], list[dict[str, str]], list[dict[str, str]]]:
    jobs: list[dict[str, Any]] = []
    hints: list[dict[str, str]] = []
    errors: list[dict[str, str]] = []
    for sub in _TRIGGER_DIRS:
        d = profile_dir / sub
        if not d.is_dir():
            continue
        for yaml_path in sorted(d.rglob("*.yaml")):
            data, err = _safe_load_yaml(yaml_path)
            if err:
                errors.append({"file": str(yaml_path), "error": err})
                continue
            if not data:
                continue
            prompt = data.get("prompt", "") or ""
            jobs.append({
                "name": data.get("name", yaml_path.stem),
                "file": str(yaml_path.relative_to(profile_dir)),
                "trigger_type": (data.get("trigger") or {}).get("type"),
                "trigger_schedule": (data.get("trigger") or {}).get("schedule"),
                "trigger_patterns": (data.get("trigger") or {}).get("patterns"),
                "trigger_interval_seconds": (data.get("trigger") or {}).get("interval_seconds"),
                "category": data.get("category"),
                "description": data.get("description", "")[:200],
                "skills_used": data.get("skills") or [],
                "tier_max": (data.get("tier") or {}).get("max"),
                "tier_prefer": (data.get("tier") or {}).get("prefer"),
                "budget_per_run_usd": (data.get("budget") or {}).get("usd_per_run_cap"),
                "delivery_channel": (data.get("delivery") or {}).get("channel"),
                "delivery_target_env": (data.get("delivery") or {}).get("target_env"),
                "safety_requires_confirmation": (data.get("safety") or {}).get("requires_confirmation"),
                "prompt_length": len(prompt),
                "prompt_hash": _hash_prompt(prompt) if prompt else None,
            })
            for h in _scan_prompt_hints(prompt):
                hints.append({"job": data.get("name", yaml_path.stem), **h})
    return jobs, hints, errors


def _scan_profile(profile_dir: Path) -> dict[str, Any]:
    profile_id = profile_dir.name
    cfg_path = profile_dir / "config.yaml"
    cfg, cfg_err = _safe_load_yaml(cfg_path) if cfg_path.exists() else (None, "config.yaml missing")
    skills = _list_skills(profile_dir)
    jobs, hints, parse_errors = _list_jobs(profile_dir)
    if cfg_err:
        parse_errors.append({"file": str(cfg_path), "error": cfg_err})
    return {
        "id": profile_id,
        "config": _extract_config_snapshot(cfg or {}),
        "skills": skills,
        "jobs": jobs,
        "hints": hints,
        "parse_errors": parse_errors,
    }


def _summarize(profiles: list[dict[str, Any]]) -> dict[str, Any]:
    job_count = sum(len(p["jobs"]) for p in profiles)
    skill_count = sum(len(p["skills"]) for p in profiles)
    mcp_count = sum(len(p["config"].get("mcp_servers") or {}) for p in profiles)
    tier_dist: dict[str, int] = {}
    trigger_dist: dict[str, int] = {}
    for p in profiles:
        for j in p["jobs"]:
            t = j.get("tier_max") or "unknown"
            tier_dist[t] = tier_dist.get(t, 0) + 1
            tt = j.get("trigger_type") or "unknown"
            trigger_dist[tt] = trigger_dist.get(tt, 0) + 1
    return {
        "profile_count": len(profiles),
        "job_count": job_count,
        "skill_count": skill_count,
        "mcp_count": mcp_count,
        "tier_distribution": tier_dist,
        "trigger_distribution": trigger_dist,
    }


def main() -> int:
    p = argparse.ArgumentParser(description="advisor_ops job inventory scanner")
    p.add_argument("--profiles-root", type=Path, default=_DEFAULT_PROFILES_ROOT)
    p.add_argument("--profile", help="단일 프로파일만 스캔 (생략 시 전체)")
    p.add_argument("--output", type=Path, help="JSON 출력 경로 (기본: stdout)")
    p.add_argument("--include-self", action="store_true",
                   help="advisor_ops 프로파일도 스캔 (기본: 제외)")
    args = p.parse_args()

    if not args.profiles_root.is_dir():
        sys.stderr.write(f"profiles root not found: {args.profiles_root}\n")
        return 1

    if args.profile:
        target_dirs = [args.profiles_root / args.profile]
        if not target_dirs[0].is_dir():
            sys.stderr.write(f"profile not found: {args.profile}\n")
            return 1
    else:
        target_dirs = [
            d for d in sorted(args.profiles_root.iterdir())
            if d.is_dir() and (args.include_self or d.name != "advisor_ops")
        ]

    profiles = [_scan_profile(d) for d in target_dirs]
    inventory = {
        "scanned_at": datetime.now(_KST).isoformat(timespec="seconds"),
        "profiles_root": str(args.profiles_root),
        "profiles": profiles,
        "summary": _summarize(profiles),
    }

    payload = json.dumps(inventory, ensure_ascii=False, indent=2)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(payload, encoding="utf-8")
        sys.stderr.write(f"wrote inventory: {args.output}\n")
    else:
        print(payload)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
