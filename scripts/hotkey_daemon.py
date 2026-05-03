"""Global hotkey daemon — Ctrl+Alt+G toggles dev <-> playtest.

Gaming mode (set by the watcher) takes precedence: the hotkey is a no-op
while a game is running, so a fat-fingered toggle can't re-enable ollama
mid-game. Once the game exits and the watcher reverts to dev, the
hotkey resumes working.

Run:
    python scripts/hotkey_daemon.py

Note: the `keyboard` library may require admin rights on Windows for
reliable global hooks. If hotkeys silently don't fire, run from an
elevated PowerShell.
"""
from __future__ import annotations

import logging
import sys
from pathlib import Path

import keyboard

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from src.runtime_mode import get as get_mode, set_mode  # noqa: E402

HOTKEY = "ctrl+alt+g"


def _toggle() -> None:
    log = logging.getLogger()
    current = get_mode()
    if current.mode == "gaming":
        log.info("hotkey ignored: gaming mode active (%s)", current.detail)
        return
    if current.mode == "dev":
        set_mode("playtest", source="hotkey", detail="hotkey toggle")
        log.info("hotkey: dev -> playtest (ollama stopping)")
    else:
        set_mode("dev", source="hotkey", detail="hotkey toggle")
        log.info("hotkey: %s -> dev (ollama starting)", current.mode)


def main() -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [hotkey] %(message)s",
    )
    log = logging.getLogger()
    keyboard.add_hotkey(HOTKEY, _toggle)
    log.info("registered '%s'. Ctrl+C to exit.", HOTKEY)
    try:
        keyboard.wait()
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
