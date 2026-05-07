"""hermes-setup — Phase 19 (2026-05-07).

Cross-platform timer auto-registration for the growth-loop jobs:
  * ReflectionJob — Sunday 22:00 KST
  * CuratorJob    — Sunday 23:00 KST
  * SkillPromoter — Sunday 23:30 KST (curator subprocess)

Platform handlers live in ``src/cli/timer_handlers/{windows,linux,darwin}.py``
and share a tiny contract: ``register(repo_root, *, ack=True)`` returns a
list of registered task names.

Skip conditions:
  * ``HERMES_NO_AUTO_TIMER`` env set → silent skip (CI, Docker)
  * ``/.dockerenv`` exists → skip (Docker container)
  * ``settings.auto_timer_enabled`` False → skip
  * ``ack=False`` and stdin not a tty → skip with stderr notice

The ack flow is intentionally idempotent: once the user consents, the
function appends ``HERMES_AUTO_TIMER_ACK=true`` to ``.env`` and skips the
prompt on subsequent runs. ``--non-interactive`` plus an existing ack
sources from ``.env`` makes scripted reinstalls safe.
"""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

from src.config import get_settings


_ACK_LINE = "HERMES_AUTO_TIMER_ACK=true"


def _repo_root() -> Path:
    # src/cli/setup.py → repo root is parents[2]
    return Path(__file__).resolve().parents[2]


def _is_container() -> bool:
    return Path("/.dockerenv").exists()


def _is_wsl() -> bool:
    """Detect WSL by /proc/version Microsoft signature.

    WSL maps to the Linux handler — schtasks via /mnt/c is technically
    callable but introduces timezone + path translation issues. We chose
    systemd-user as the single Linux+WSL story.
    """
    if sys.platform != "linux":
        return False
    proc = Path("/proc/version")
    if not proc.exists():
        return False
    try:
        return "microsoft" in proc.read_text(encoding="utf-8", errors="ignore").lower()
    except OSError:
        return False


def _platform_module_name() -> str:
    if sys.platform == "win32":
        return "windows"
    if sys.platform == "darwin":
        return "darwin"
    if sys.platform == "linux":
        return "linux"        # WSL re-uses Linux handler.
    raise RuntimeError(f"unsupported platform: {sys.platform}")


def _confirm(message: str, *, non_interactive: bool, ack: bool) -> bool:
    if ack:
        return True
    if non_interactive:
        sys.stderr.write(
            "[hermes-setup] non-interactive + no prior ack → skip.\n"
            "Run `hermes-setup` interactively to consent, or set "
            "HERMES_AUTO_TIMER_ACK=true.\n"
        )
        return False
    if not sys.stdin.isatty():
        sys.stderr.write(
            "[hermes-setup] stdin not a tty → skipping prompt. "
            "Set HERMES_AUTO_TIMER_ACK=true to enable silent install.\n"
        )
        return False
    sys.stdout.write(message)
    sys.stdout.flush()
    answer = sys.stdin.readline().strip().lower()
    return answer in ("y", "yes")


def _persist_ack(repo: Path) -> None:
    env_path = repo / ".env"
    try:
        existing = env_path.read_text(encoding="utf-8") if env_path.exists() else ""
    except OSError:
        existing = ""
    if _ACK_LINE in existing:
        return
    try:
        with env_path.open("a", encoding="utf-8") as f:
            if existing and not existing.endswith("\n"):
                f.write("\n")
            f.write(_ACK_LINE + "\n")
    except OSError as e:
        sys.stderr.write(f"[hermes-setup] could not persist ack: {e}\n")


def _import_handler(name: str):
    if name == "windows":
        from src.cli.timer_handlers import windows
        return windows
    if name == "linux":
        from src.cli.timer_handlers import linux
        return linux
    if name == "darwin":
        from src.cli.timer_handlers import darwin
        return darwin
    raise RuntimeError(f"no handler module for {name}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="hermes-setup",
        description="Register growth-loop timers on this host OS.",
    )
    parser.add_argument(
        "--non-interactive", action="store_true",
        help="Do not prompt for confirmation. Requires HERMES_AUTO_TIMER_ACK=true.",
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Print planned commands without executing.",
    )
    args = parser.parse_args(argv)

    if os.environ.get("HERMES_NO_AUTO_TIMER"):
        print("[hermes-setup] HERMES_NO_AUTO_TIMER set → skipping.")
        return 0
    if _is_container():
        print("[hermes-setup] container detected (/.dockerenv) → skipping.")
        return 0

    settings = get_settings()
    if not settings.auto_timer_enabled:
        print("[hermes-setup] auto_timer_enabled=false → skipping.")
        return 0

    repo = _repo_root()
    handler_name = _platform_module_name()
    handler = _import_handler(handler_name)

    plan = handler.plan(repo)
    print("[hermes-setup] platform: " + handler_name + (" (WSL)" if _is_wsl() else ""))
    print("[hermes-setup] would register:")
    for cmd in plan:
        print("  " + " ".join(str(c) for c in cmd))

    if args.dry_run:
        return 0

    consent = _confirm(
        "[hermes-setup] register the above tasks? [y/N] ",
        non_interactive=args.non_interactive,
        ack=settings.auto_timer_ack,
    )
    if not consent:
        return 0

    registered = handler.register(repo, ack=True)
    print(f"[hermes-setup] registered {len(registered)} task(s):")
    for name in registered:
        print(f"  ✓ {name}")

    if not settings.auto_timer_ack:
        _persist_ack(repo)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
