"""External game launcher watcher — flips runtime_mode to "gaming" when
a whitelisted game process appears, "dev" when it disappears.

Polling-based (psutil) at 5s interval — light enough to leave running
indefinitely. Manual mode flips (hotkey, CLI, future webhook) are
respected: we only revert from gaming → dev if the watcher itself was
the source of the gaming flip. A user-set "playtest" stays put until
the user clears it.

Run:
    python scripts/game_watcher.py

For autostart, register as a Windows Task Scheduler task with trigger
"At log on" and action "python E:\\hermes-hybrid\\scripts\\game_watcher.py".
"""
from __future__ import annotations

import logging
import sys
import time
from pathlib import Path

import psutil
import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from src.runtime_mode import get as get_mode, set_mode  # noqa: E402

WHITELIST_PATH = REPO_ROOT / "config" / "game_whitelist.yaml"
POLL_INTERVAL_SECONDS = 5.0


def _load_whitelist() -> set[str]:
    if not WHITELIST_PATH.exists():
        return set()
    data = yaml.safe_load(WHITELIST_PATH.read_text(encoding="utf-8")) or {}
    games = data.get("games", []) or []
    out: set[str] = set()
    for entry in games:
        if isinstance(entry, dict):
            proc = entry.get("process", "")
        elif isinstance(entry, str):
            proc = entry
        else:
            continue
        if proc:
            out.add(proc.lower())
    return out


def _running_game(whitelist: set[str]) -> str | None:
    for proc in psutil.process_iter(["name"]):
        try:
            name = (proc.info.get("name") or "").lower()
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue
        if name in whitelist:
            return name
    return None


def main() -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [game_watcher] %(message)s",
    )
    log = logging.getLogger()

    whitelist = _load_whitelist()
    if not whitelist:
        log.warning(
            "whitelist empty (config/game_whitelist.yaml missing or has no "
            "entries) — watcher will idle. Edit the yaml to add games."
        )
    else:
        log.info("loaded %d whitelisted process names", len(whitelist))

    last_game: str | None = None

    while True:
        current = get_mode()
        running = _running_game(whitelist)

        if running and current.mode == "dev":
            log.info("game detected: %s -> gaming mode", running)
            set_mode("gaming", source="watcher", detail=running)
            last_game = running
        elif (
            not running
            and current.mode == "gaming"
            and current.source == "watcher"
        ):
            # Only revert if WE were the source. Manual gaming (rare) stays.
            log.info("game ended (was: %s) -> dev mode", last_game or "?")
            set_mode("dev", source="watcher", detail=f"game ended: {last_game}")
            last_game = None

        time.sleep(POLL_INTERVAL_SECONDS)


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        raise SystemExit(0)
