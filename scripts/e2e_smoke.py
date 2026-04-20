"""End-to-end smoke test: drives the real Orchestrator through 3 routing paths.

Exercises:
  L2 (local, 14B)    — short conversational
  L3 (worker, 32B)   — code task
  C1 (cloud, GPT-4o) — planning/URL task
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

    tests = [
        ("hi, how are you?",                                  "L2 (local)"),
        ("write a Python one-liner that returns 2+2",         "L3 (worker)"),
        ("이 URL을 분석하고 보고서 작성해: https://example.com", "C1 (cloud planning)"),
    ]
    failed = 0
    for msg, expect in tests:
        print(f"---- EXPECT {expect} ----")
        print(f"msg: {msg!r}")
        try:
            r = await o.handle(msg, user_id="100816750945255424")
            print(f"OK handled_by={r.handled_by} tier={r.task.current_tier} "
                  f"route={r.task.route} cloud_calls={r.task.cloud_call_count}")
            print(f"response: {r.response[:240]}")
        except Exception as e:  # noqa: BLE001
            print(f"FAIL {type(e).__name__}: {e}")
            failed += 1
        print()

    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
