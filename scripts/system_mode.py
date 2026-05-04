#!/usr/bin/env python3
"""system_mode — Kanban-era system state for game/playtest gating.

Replaces the deprecated dev/playtest/gaming 3-mode (`src/runtime_mode.py`,
removed 2026-05-04 — see ``memory/project_mode_system_deprecation.md``).

Two-mode design (intentional simplification):
  - ``active``: normal operation, all cron workers + dispatcher allowed
  - ``quiet``:  external game / engine playtest in progress; background cron
                workers should silently skip. on_demand jobs (explicit user
                intent) keep working — gating is for background cycles only.

State file: ``state/system_mode.json`` (atomic writes: tmp + os.replace).
Cache: 1s in-process for the (rare) hot path.

CLI:
    python3 scripts/system_mode.py get                   # prints "active" | "quiet"
    python3 scripts/system_mode.py set quiet --source manual
    python3 scripts/system_mode.py status                # full JSON

Worker integration (Step 0 guard, auto-prepended by ``register_cron_jobs.py``):
    python3 /mnt/e/hermes-hybrid/scripts/system_mode.py get
    → "quiet" → silent skip (cron); "active" → proceed.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import threading
import time
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Literal

Mode = Literal["active", "quiet"]
Source = Literal["manual", "watcher", "hotkey", "pie_webhook"]

_KST = timezone(timedelta(hours=9), name="KST")
_REPO_ROOT = Path(__file__).resolve().parent.parent
_STATE_FILE = _REPO_ROOT / "state" / "system_mode.json"
_CACHE_TTL = 1.0


@dataclass(frozen=True)
class SystemMode:
    mode: Mode
    since: str  # ISO8601 KST
    source: Source
    detail: str = ""

    @classmethod
    def default(cls) -> "SystemMode":
        return cls(
            mode="active",
            since=datetime.now(_KST).isoformat(timespec="seconds"),
            source="manual",
            detail="default",
        )


_lock = threading.Lock()
_cache: tuple[SystemMode, float] | None = None


def get() -> SystemMode:
    """Return current mode. Default ``active`` if state file missing/corrupt."""
    global _cache
    with _lock:
        if _cache is not None:
            cached, when = _cache
            if time.monotonic() - when < _CACHE_TTL:
                return cached
        if not _STATE_FILE.exists():
            mode = SystemMode.default()
        else:
            try:
                data = json.loads(_STATE_FILE.read_text(encoding="utf-8"))
                mode = SystemMode(
                    mode=data["mode"],
                    since=data["since"],
                    source=data["source"],
                    detail=data.get("detail", ""),
                )
            except (json.JSONDecodeError, KeyError, ValueError, OSError):
                # Corrupt → default to active. Next set() rewrites cleanly.
                mode = SystemMode.default()
        _cache = (mode, time.monotonic())
        return mode


def set_mode(mode: Mode, *, source: Source, detail: str = "") -> SystemMode:
    """Atomically switch system mode. Returns the new state."""
    new = SystemMode(
        mode=mode,
        since=datetime.now(_KST).isoformat(timespec="seconds"),
        source=source,
        detail=detail,
    )
    _STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = _STATE_FILE.with_suffix(".json.tmp")
    tmp.write_text(
        json.dumps(asdict(new), ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    os.replace(tmp, _STATE_FILE)
    global _cache
    with _lock:
        _cache = (new, time.monotonic())
    return new


def invalidate_cache() -> None:
    """Force next get() to re-read disk. For tests."""
    global _cache
    with _lock:
        _cache = None


# ---- CLI ----------------------------------------------------------------


def _cli() -> int:
    p = argparse.ArgumentParser(description="system_mode (active|quiet) CLI")
    sub = p.add_subparsers(dest="cmd", required=False)

    sub.add_parser("get", help="Print current mode (active|quiet)")
    sub.add_parser("status", help="Print full state as JSON")

    p_set = sub.add_parser("set", help="Set mode")
    p_set.add_argument("mode", choices=["active", "quiet"])
    p_set.add_argument(
        "--source",
        default="manual",
        choices=["manual", "watcher", "hotkey", "pie_webhook"],
    )
    p_set.add_argument("--detail", default="")

    args = p.parse_args()

    if args.cmd in (None, "get"):
        print(get().mode)
        return 0

    if args.cmd == "status":
        m = get()
        print(json.dumps(asdict(m), ensure_ascii=False, indent=2))
        return 0

    if args.cmd == "set":
        new = set_mode(args.mode, source=args.source, detail=args.detail)
        print(f"mode -> {new.mode} (source={new.source})", file=sys.stderr)
        print(new.mode)
        return 0

    p.print_help()
    return 2


if __name__ == "__main__":
    raise SystemExit(_cli())
