"""Phase 1 latency benchmark.

Runs a fixed prompt set against the orchestrator in both flag modes:
  1. USE_HERMES_FOR_LOCAL=false (baseline, direct Ollama/OpenAI)
  2. USE_HERMES_FOR_LOCAL=true  (Phase 1, Hermes-driven L2/L3)

Reports p50 / p95 for each and prints the ratio so we can check the
Phase 1 exit gate: ``hermes_p95 <= 2 * baseline_p95``.

Usage::

    python scripts/bench_latency.py            # default 10 prompts × 2 modes
    python scripts/bench_latency.py --n 30     # more samples

Expects ``.env`` to point at real Ollama / OpenAI. Run Ollama locally
first, or the baseline path will fall through to the OpenAI surrogate.
"""
from __future__ import annotations

import argparse
import asyncio
import statistics
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.config import Settings, get_settings, reset_settings  # noqa: E402
from src.obs import setup_logging  # noqa: E402
from src.orchestrator import Orchestrator  # noqa: E402
from src.state import Repository  # noqa: E402

# Small, realistic prompts that should route to L2 (local). We deliberately
# avoid code / structured-output signals so the router stays in local lane,
# which is where Phase 1 wiring lives.
PROMPTS = [
    "Summarize what the GIL is in one sentence.",
    "Give me a Korean idiom that means 'seize the moment'.",
    "What's a good breakfast if I'm cutting sugar?",
    "One-line joke about databases.",
    "Translate 'good night, sleep well' to French.",
    "Which is colder, -30°C or -30°F?",
    "Name three movies with existential themes.",
    "What does HTTP 418 mean?",
    "Suggest a 3-word product name for a study app.",
    "What's the capital of Mongolia?",
]


async def _one_run(o: Orchestrator, prompt: str, user_id: str) -> tuple[float, str]:
    t0 = time.perf_counter()
    result = await o.handle(prompt, user_id=user_id)
    elapsed = time.perf_counter() - t0
    return elapsed, result.handled_by


async def _bench(flag: bool, n: int, settings: Settings, repo: Repository) -> dict:
    settings.use_hermes_for_local = flag
    o = Orchestrator(settings, repo=repo)
    samples: list[float] = []
    handlers: dict[str, int] = {}

    for i in range(n):
        prompt = PROMPTS[i % len(PROMPTS)]
        try:
            elapsed, handled_by = await _one_run(o, prompt, user_id="bench")
        except Exception as e:  # noqa: BLE001
            print(f"  [{i:02d}] EXC {type(e).__name__}: {e}")
            continue
        samples.append(elapsed)
        handlers[handled_by] = handlers.get(handled_by, 0) + 1
        print(f"  [{i:02d}] {elapsed*1000:6.0f}ms  via {handled_by}")

    if not samples:
        return {"flag": flag, "n": 0, "p50": None, "p95": None, "handlers": {}}

    samples.sort()
    return {
        "flag": flag,
        "n": len(samples),
        "p50": statistics.median(samples),
        "p95": samples[int(len(samples) * 0.95) - 1] if len(samples) >= 20
               else samples[-1],  # small-N: use max as p95
        "mean": statistics.mean(samples),
        "handlers": handlers,
    }


def _fmt(s: dict) -> str:
    if s["p50"] is None:
        return f"n={s['n']} (no samples)"
    return (
        f"n={s['n']}  p50={s['p50']*1000:.0f}ms  "
        f"p95={s['p95']*1000:.0f}ms  mean={s['mean']*1000:.0f}ms  "
        f"handlers={s['handlers']}"
    )


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--n", type=int, default=10, help="samples per mode")
    ap.add_argument("--skip-baseline", action="store_true")
    ap.add_argument("--skip-hermes", action="store_true")
    args = ap.parse_args()

    reset_settings()
    settings = get_settings()
    setup_logging(settings.log_level, json=False)
    repo = Repository(settings.state_db_path)
    await repo.init()

    print("=" * 72)
    print(f"Phase 1 latency bench  (N={args.n} per mode)")
    print(f"  ollama_enabled        = {settings.ollama_enabled}")
    print(f"  use_hermes_for_local  = (toggled per mode)")
    print("=" * 72)

    results: dict[str, dict] = {}

    if not args.skip_baseline:
        print("\n--- baseline (USE_HERMES_FOR_LOCAL=false) ---")
        results["baseline"] = await _bench(False, args.n, settings, repo)
        print("  ", _fmt(results["baseline"]))

    if not args.skip_hermes:
        print("\n--- phase1 (USE_HERMES_FOR_LOCAL=true) ---")
        results["phase1"] = await _bench(True, args.n, settings, repo)
        print("  ", _fmt(results["phase1"]))

    print("\n" + "=" * 72)
    if "baseline" in results and "phase1" in results:
        b = results["baseline"]
        p = results["phase1"]
        if b["p95"] and p["p95"]:
            ratio = p["p95"] / b["p95"]
            print(f"  p95 ratio (phase1 / baseline) = {ratio:.2f}×")
            gate = "PASS" if ratio <= 2.0 else "FAIL"
            print(f"  Phase 1 gate (≤ 2×) .......... {gate}")
            return 0 if ratio <= 2.0 else 1
    print("  (need both modes to compute ratio)")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
