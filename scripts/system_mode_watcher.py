#!/usr/bin/env python3
"""system_mode_watcher — thin Windows process polling shim.

Polls the Windows process list every N seconds for whitelisted game/launcher
executables. When any are running, flips ``system_mode`` to ``quiet``. When
all whitelisted processes have exited (and **we** were the ones that set quiet),
flips back to ``active``.

Why a separate process (not in WSL):
    Windows process visibility requires running on Windows-side Python.
    WSL processes can't see Windows processes via /proc.

Why stdlib only (no psutil):
    `tasklist /NH /FO CSV` is reliable on every Windows install since Vista.
    Adds zero dependencies on top of the .venv.

Manual override semantics:
    - User `system_mode set quiet --source manual` → watcher LEAVES it alone.
      Watcher only reverts quiet → active when the prior set's source was "watcher"
      (i.e. we own the transition).
    - User `system_mode set active --source manual` while game is running →
      watcher will flip back to quiet on the next tick (1 LLM call window).

Run (Windows side):
    E:\\hermes-hybrid\\.venv\\Scripts\\python.exe E:\\hermes-hybrid\\scripts\\system_mode_watcher.py

Recommended: Windows Task Scheduler "At log on" trigger.

Configuration via env (optional):
    GAME_WATCHER_INTERVAL    poll seconds (default: 5)
    GAME_WATCHER_WHITELIST   comma-separated process names (overrides DEFAULT_WHITELIST)
"""
from __future__ import annotations

import csv
import os
import subprocess
import sys
import time
from pathlib import Path

# Allow `from scripts.system_mode import ...` even when run as script.
_REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO_ROOT))

from scripts.system_mode import get as get_mode, set_mode  # noqa: E402

# Default whitelist — **actual game executables only**, not launchers.
#
# Rationale: launcher processes (steam.exe / riotclientservices.exe /
# bsglauncher.exe) stay running while the user works (Steam Friends list,
# Riot patcher, etc.). Triggering quiet on launcher presence causes
# false-positive game mode while the user is still in dev/work flow.
# Only the actual game binary spawn signals real "playing now".
#
# Edit GAME_WATCHER_WHITELIST env var (comma-separated, lowercase) to override.
# Process names are case-insensitive on Windows; we lowercase before compare.
DEFAULT_WHITELIST = {
    # Riot — League of Legends (game client only)
    "leagueoflegends.exe",
    "league of legends.exe",
    # Riot — Valorant (real game binary, not launcher)
    "valorant.exe",
    "valorant-win64-shipping.exe",
    # Battlestate — Escape from Tarkov (game binary, not launcher)
    "escapefromtarkov.exe",
    # Steam games are too varied to hardcode. Add your common titles via
    # GAME_WATCHER_WHITELIST=cs2.exe,dota2.exe,... (no steam.exe itself).
}

DEFAULT_INTERVAL = 5  # seconds


def _whitelist_from_env() -> set[str]:
    raw = os.environ.get("GAME_WATCHER_WHITELIST", "").strip()
    if not raw:
        return {p.lower() for p in DEFAULT_WHITELIST}
    return {p.strip().lower() for p in raw.split(",") if p.strip()}


def _running_processes() -> set[str]:
    """Return set of currently running process names (lowercased).

    Uses `tasklist /NH /FO CSV` — header-stripped, CSV-formatted. Robust on
    Windows 7+. Returns empty set on any failure (errs on the side of
    "no game running" so we don't accidentally hold the system in quiet).
    """
    try:
        result = subprocess.run(
            ["tasklist", "/NH", "/FO", "CSV"],
            capture_output=True,
            text=True,
            timeout=10,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except (subprocess.SubprocessError, FileNotFoundError, OSError):
        return set()
    if result.returncode != 0:
        return set()
    names: set[str] = set()
    for row in csv.reader(result.stdout.splitlines()):
        if row:
            names.add(row[0].strip().lower().strip('"'))
    return names


def _poll_once(whitelist: set[str]) -> tuple[str, list[str]]:
    """One poll tick. Returns (action, hits) where action is one of:
    'noop' / 'set_quiet' / 'set_active' and hits is the matched process list.
    """
    running = sorted(_running_processes() & whitelist)
    current = get_mode()
    if running:
        if current.mode != "quiet":
            detail = ",".join(running)[:200]
            set_mode("quiet", source="watcher", detail=detail)
            return "set_quiet", running
        return "noop", running
    # No whitelisted process running.
    if current.mode == "quiet" and current.source == "watcher":
        set_mode("active", source="watcher", detail="whitelist clear")
        return "set_active", []
    return "noop", []


def main() -> int:
    interval = max(1, int(os.environ.get("GAME_WATCHER_INTERVAL", DEFAULT_INTERVAL)))
    whitelist = _whitelist_from_env()
    print(
        f"[watcher] starting, interval={interval}s, "
        f"whitelist={sorted(whitelist)}",
        flush=True,
    )
    last_running: list[str] = []
    while True:
        try:
            action, running = _poll_once(whitelist)
            if action == "set_quiet":
                print(f"[watcher] quiet ← {running}", flush=True)
            elif action == "set_active":
                print("[watcher] active (whitelist clear)", flush=True)
            elif running != last_running:
                # Even when noop, log changes in the running set for visibility.
                print(f"[watcher] running set: {running}", flush=True)
            last_running = running
        except KeyboardInterrupt:
            print("[watcher] stopped (KeyboardInterrupt)", flush=True)
            return 0
        except Exception as exc:  # noqa: BLE001
            print(f"[watcher] poll error: {exc}", flush=True)
        time.sleep(interval)


if __name__ == "__main__":
    raise SystemExit(main())
