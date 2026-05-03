"""Runtime mode — dev / playtest / gaming — drives ollama enable/disable.

Single source of truth for whether the local LLM should be running and
which cloud backend takes over when it isn't. State lives at
``state/runtime_mode.json``; ``Settings.effective_*`` properties read
this, while watchers / hotkey daemon / PIE webhook handlers write it.

Modes:
  - ``dev``      : editor work, idle — ollama runs, normal routing
  - ``playtest`` : PIE / standalone build playtest — ollama OFF, cloud only
  - ``gaming``   : external game launcher (Steam/Riot/BSG) — ollama OFF, cloud only

The ``playtest`` / ``gaming`` distinction is informational (for logs and
future per-mode policy); both flip ``local_llm_should_run`` to False.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Literal

Mode = Literal["dev", "playtest", "gaming"]
Source = Literal["manual", "watcher", "hotkey", "pie_webhook"]

_KST = timezone(timedelta(hours=9), name="KST")
_REPO_ROOT = Path(__file__).resolve().parent.parent
_STATE_FILE = _REPO_ROOT / "state" / "runtime_mode.json"

# Hot-path callers (Settings.effective_* in the orchestrator) hit this on
# every request, so we cache reads briefly to avoid hammering the FS. 1s
# is short enough that watcher/hotkey/webhook flips feel instant in
# practice but long enough to absorb a burst of LLM calls cheaply.
_CACHE_TTL_SECONDS = 1.0


@dataclass(frozen=True)
class RuntimeMode:
    mode: Mode
    since: str  # ISO8601, KST
    source: Source
    detail: str = ""

    @classmethod
    def default(cls) -> "RuntimeMode":
        return cls(
            mode="dev",
            since=datetime.now(_KST).isoformat(timespec="seconds"),
            source="manual",
            detail="default",
        )

    @property
    def local_llm_should_run(self) -> bool:
        return self.mode == "dev"


_lock = threading.Lock()
_cache: tuple[RuntimeMode, float] | None = None


def get() -> RuntimeMode:
    """Return current mode; cached ~1s for orchestrator hot-path reads."""
    global _cache
    with _lock:
        if _cache is not None:
            cached_mode, cached_at = _cache
            if time.monotonic() - cached_at < _CACHE_TTL_SECONDS:
                return cached_mode
        if not _STATE_FILE.exists():
            mode = RuntimeMode.default()
        else:
            try:
                data = json.loads(_STATE_FILE.read_text(encoding="utf-8"))
                mode = RuntimeMode(
                    mode=data["mode"],
                    since=data["since"],
                    source=data["source"],
                    detail=data.get("detail", ""),
                )
            except (json.JSONDecodeError, KeyError, ValueError, OSError):
                # Corrupt/unreadable file → fall back to default rather
                # than crash callers. The next set_mode() rewrites it.
                mode = RuntimeMode.default()
        _cache = (mode, time.monotonic())
        return mode


def invalidate_cache() -> None:
    """Force the next get() to re-read disk. Used by tests."""
    global _cache
    with _lock:
        _cache = None


def set_mode(
    mode: Mode,
    *,
    source: Source,
    detail: str = "",
    control_ollama: bool = True,
) -> RuntimeMode:
    """Atomically switch mode; optionally start/stop ollama for VRAM.

    Atomic write: tmpfile + os.replace, so a crashing writer can never
    leave a partial JSON for the orchestrator to choke on.
    """
    new = RuntimeMode(
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

    if control_ollama:
        if new.local_llm_should_run:
            _start_ollama_best_effort()
        else:
            _stop_ollama_best_effort()

    return new


# ---- ollama process control (best-effort, never raises) ----------------


def _ollama_running() -> bool:
    try:
        with urllib.request.urlopen(
            "http://localhost:11434/api/tags", timeout=2
        ) as resp:
            return resp.status == 200
    except (urllib.error.URLError, OSError, TimeoutError):
        return False


def _start_ollama_best_effort() -> None:
    if _ollama_running():
        return
    kwargs: dict = {
        "stdout": subprocess.DEVNULL,
        "stderr": subprocess.DEVNULL,
    }
    if os.name == "nt":
        kwargs["creationflags"] = subprocess.DETACHED_PROCESS
    try:
        subprocess.Popen(["ollama", "serve"], **kwargs)
    except (FileNotFoundError, OSError):
        # ollama not installed / not on PATH — caller path will use cloud
        # surrogates regardless, so this is a soft-fail by design.
        pass


def _stop_ollama_best_effort() -> None:
    """Unload all loaded models to free VRAM. Leaves the daemon up.

    We use ``POST /api/generate {keep_alive: 0}`` instead of the
    ``ollama stop`` subcommand. In Ollama 0.22.1 the subcommand has a
    side effect of *re-loading* the model briefly (the CLI implicitly
    pings the daemon which resets the keep-alive timer), which is the
    opposite of what we want during a game session.
    """
    try:
        with urllib.request.urlopen(
            "http://localhost:11434/api/ps", timeout=2
        ) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except (urllib.error.URLError, OSError, TimeoutError, json.JSONDecodeError):
        return
    for m in data.get("models", []):
        name = m.get("name") or m.get("model")
        if not name:
            continue
        try:
            req = urllib.request.Request(
                "http://localhost:11434/api/generate",
                data=json.dumps({"model": name, "keep_alive": 0}).encode("utf-8"),
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            urllib.request.urlopen(req, timeout=10).read()
        except (urllib.error.URLError, OSError, TimeoutError):
            continue


# ---- CLI ---------------------------------------------------------------


def _cli() -> int:
    args = sys.argv[1:]
    if not args or args[0] in ("status", "-s"):
        m = get()
        print(f"mode:    {m.mode}")
        print(f"since:   {m.since}")
        print(f"source:  {m.source}")
        if m.detail:
            print(f"detail:  {m.detail}")
        expected = "should run" if m.local_llm_should_run else "should be stopped"
        actual = "running" if _ollama_running() else "stopped"
        print(f"ollama:  {expected} / actually {actual}")
        return 0
    cmd = args[0]
    if cmd in ("dev", "playtest", "gaming"):
        detail = " ".join(args[1:]) if len(args) > 1 else ""
        new = set_mode(cmd, source="manual", detail=detail)
        verb = "starting" if new.local_llm_should_run else "stopping"
        print(f"mode → {new.mode}  (ollama {verb})")
        return 0
    print(
        "usage: python -m src.runtime_mode {dev|playtest|gaming|status}",
        file=sys.stderr,
    )
    return 2


if __name__ == "__main__":
    raise SystemExit(_cli())
