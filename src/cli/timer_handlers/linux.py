"""Linux/WSL timer handler — delegates to existing systemd-user installers.

Phase 19 (2026-05-07). The shell installers in ``scripts/install_*_timer.sh``
already exist (Phase 14/15 era) so this handler is a thin orchestrator.

WSL is treated as Linux: schtasks via /mnt/c/Windows/System32 is reachable
but introduces timezone + path translation. systemd-user works on WSL2
when the user has ``systemctl --user`` enabled.
"""
from __future__ import annotations

import subprocess
from pathlib import Path

_INSTALLERS: tuple[tuple[str, str], ...] = (
    ("hermes-reflection.timer", "scripts/install_reflection_timer.sh"),
    # Phase 21 (2026-05-07): weekly A/B report between Reflection and
    # Curator. Sunday 22:30 KST.
    ("hermes-ab-report.timer",  "scripts/install_ab_report_timer.sh"),
    ("hermes-curator.timer",    "scripts/install_curator_timer.sh"),
)


def plan(repo: Path) -> list[list[str]]:
    return [["bash", str(repo / rel)] for _, rel in _INSTALLERS]


def register(repo: Path, *, ack: bool = True) -> list[str]:
    if not ack:
        return []
    registered: list[str] = []
    for unit, rel in _INSTALLERS:
        installer = repo / rel
        if not installer.exists():
            _err(f"installer missing: {installer}")
            continue
        try:
            r = subprocess.run(
                ["bash", str(installer)],
                capture_output=True, text=True, timeout=30,
            )
            if r.returncode == 0:
                registered.append(unit)
            else:
                _err(f"{installer.name} exit {r.returncode}: {r.stderr.strip()}")
        except (OSError, subprocess.TimeoutExpired) as e:
            _err(f"installer invocation failed for {installer.name}: {e}")
    return registered


def _err(msg: str) -> None:
    import sys
    sys.stderr.write("[hermes-setup:linux] " + msg + "\n")


__all__ = ["plan", "register"]
