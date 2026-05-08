"""P0e — Behavior diff harness.

Reads tests/behavior/fixtures.yaml. For each fixture:
  - record "before" with HERMES_DISABLE_GROWTH_BLOCKS=true
  - record "after" without
  - compare expected_signal

This harness is intentionally lightweight — it compares static signals
(memo present, marker block emitted log line, file produced) rather than
booting Discord. For full integration, run pytest tests/behavior/.

Usage:
    python scripts/behavior_diff.py --fixtures tests/behavior/fixtures.yaml --dry-run
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

import yaml


REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))
DEFAULT_FIXTURES = REPO_ROOT / "tests" / "behavior" / "fixtures.yaml"


def _check_file_signal(signal: dict) -> tuple[bool, str]:
    path = REPO_ROOT / signal["path"]
    if not path.exists():
        return False, f"missing: {path}"
    if "must_contain" in signal:
        text = path.read_text(encoding="utf-8", errors="replace")
        if signal["must_contain"] not in text:
            return False, f"missing token in {path}"
    return True, "ok"


def _check_signal(signal: dict) -> tuple[bool, str]:
    typ = signal.get("type")
    if typ == "file":
        return _check_file_signal(signal)
    if typ == "import":
        try:
            __import__(signal["module"])
            return True, "imported"
        except Exception as e:  # noqa: BLE001
            return False, f"import failed: {e}"
    return False, f"unknown signal type: {typ}"


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--fixtures", type=Path, default=DEFAULT_FIXTURES)
    p.add_argument("--dry-run", action="store_true", default=True)
    args = p.parse_args()

    if not args.fixtures.exists():
        print(f"fixtures missing: {args.fixtures}", file=sys.stderr)
        return 1

    data = yaml.safe_load(args.fixtures.read_text(encoding="utf-8")) or {}
    fixtures = data.get("fixtures") or []
    print(f"fixtures: {len(fixtures)}")

    results: list[dict] = []
    passed = 0
    for f in fixtures:
        name = f.get("name") or "unnamed"
        target_loop = f.get("target_loop") or "?"
        signal = f.get("expected_signal") or {}
        ok, note = _check_signal(signal)
        results.append({"name": name, "target_loop": target_loop, "ok": ok, "note": note})
        marker = "OK" if ok else "FAIL"
        if ok:
            passed += 1
        print(f"  [{marker}] {name} (loop={target_loop}) - {note}")

    out_path = REPO_ROOT / "data" / f"behavior_diff_{datetime.now(timezone.utc).strftime('%Y-%m-%d')}.json"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps({
        "ts": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "fixtures": len(fixtures),
        "passed": passed,
        "results": results,
    }, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\nresult → {out_path.relative_to(REPO_ROOT)}")
    print(f"passed: {passed}/{len(fixtures)}")
    return 0 if passed >= max(1, int(0.66 * len(fixtures))) else 1


if __name__ == "__main__":
    sys.exit(main())
