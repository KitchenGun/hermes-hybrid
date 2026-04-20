"""Smoke test: invoke the real Claude Code adapter through heavy path end-to-end.

Runs with the real Max subscription OAuth. Outputs latency + model + response.
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.config import get_settings, reset_settings  # noqa: E402
from src.obs import setup_logging  # noqa: E402
from src.orchestrator import Orchestrator  # noqa: E402
from src.state import Repository  # noqa: E402


async def main() -> int:
    reset_settings()
    s = get_settings()
    setup_logging(s.log_level, json=False)
    repo = Repository(s.state_db_path)
    await repo.init()
    o = Orchestrator(s, repo=repo)

    msg = "Reply with exactly: heavy ok"
    print(f"---- HEAVY PATH SMOKE ----")
    print(f"msg: {msg!r}")
    try:
        r = await o.handle(msg, user_id="100816750945255424", heavy=True)
        print(f"OK handled_by={r.handled_by} tier={r.task.current_tier} "
              f"route={r.task.route} cloud_calls={r.task.cloud_call_count}")
        print(f"model={r.task.cloud_model_used}")
        print(f"response: {r.response[:240]}")
        return 0 if r.handled_by == "claude-max" else 1
    except Exception as e:  # noqa: BLE001
        print(f"FAIL {type(e).__name__}: {e}")
        return 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
