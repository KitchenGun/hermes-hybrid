"""inject_strict_rules.py — Inject the "엄격한 도구 사용 규칙" block at the
start of every cron job's ``prompt:`` body that doesn't already have it.

Why this script exists:

  qwen2.5:14b-instruct (calendar_ops/kk_job main model) systematically (1) uses
  ISO8601 short form `2026-05-05T00:00` for google_calendar list-events
  timeMin/timeMax — which the MCP server reject-validates with `"Must be ISO
  8601 format: '2026-01-01T00:00:00'"`, and the model then aborts the job
  rather than retrying with the full form; (2) emits tool calls as plain text
  (`terminal(command="…")`) instead of real tool_use blocks under heavier
  prompts; (3) hallucinates Windows-style paths
  `/mnt/c/Users/User/.hermes/...` for post_webhook.py instead of the actual
  WSL path. The advisor_ops yaml already had a similar guard block and that
  jobs's MCP calls succeed, so the fix is to copy the same guardrails to
  every calendar/kk_job prompt.

Idempotent: skips files that already contain the marker comment.

Run from WSL:
  python3 /mnt/e/hermes-hybrid/scripts/inject_strict_rules.py
  python3 /mnt/e/hermes-hybrid/scripts/inject_strict_rules.py --profile kk_job
  python3 /mnt/e/hermes-hybrid/scripts/inject_strict_rules.py --dry-run
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

REPO_PROFILES = Path(__file__).resolve().parent.parent / "profiles"
MARKER = "엄격한 도구 사용 규칙"

STRICT_RULES_BLOCK = """  ⚠️  엄격한 도구 사용 규칙 (위반시 잡 fail) ⚠️
  1. datetime 인자는 반드시 ISO 8601 full format: "2026-05-05T00:00:00+09:00"
     (초 ":00" 포함, 시간대 "+09:00" 포함). 절대 "2026-05-05T00:00" 같은
     단축형 사용 금지. mcp_google_calendar_list_events 의 timeMin/timeMax 가
     단축형이면 MCP 가 reject 하고 잡이 죽는다.
  2. 학습 데이터의 옛 날짜(2023, 2024) 절대 사용 금지. user prompt 의 [현재 날짜]
     기준으로만 계산.
  3. timeMin/timeMax 는 한 호출에 하루 범위만 사용 (예: 오늘=00:00:00+09:00 ~
     다음날 00:00:00+09:00).
  4. mcp_google_calendar_list_events 호출 시 fields 인자는
     ["id","summary","start","end","location","attendees"] 만 사용 (htmlLink,
     transparency 등 빼서 응답 길이 축소 — model context 절약).
  5. 도구 호출은 plain text 로 emit 하지 말 것. write_file/terminal/MCP 모두 실제
     tool_use 로만. 만약 텍스트로 "terminal(command=...)" 같은 형식 출력 시 잡
     실패로 간주.
  6. 파일 경로는 이 prompt 본문에 적힌 절대경로 그대로 복사. 절대 추측 금지.
     이 잡은 WSL Linux 에서 도는데 학습 데이터 영향으로 모델이 자꾸
     "/mnt/c/Users/User/.hermes/..." 같은 Windows 경로를 환각하는데 그런 경로는
     존재하지 않는다.

"""


def inject_into_yaml(p: Path, dry_run: bool = False) -> str:
    """Returns one of: 'skipped' | 'injected' | 'no-prompt' | 'has-marker'."""
    text = p.read_text(encoding="utf-8")
    if MARKER in text:
        return "has-marker"
    if "\nprompt: |" not in text and not text.startswith("prompt: |"):
        return "no-prompt"

    # Find the "prompt: |" line and the start of the indented body.
    lines = text.splitlines(keepends=True)
    out: list[str] = []
    i = 0
    injected = False
    while i < len(lines):
        out.append(lines[i])
        # Match the prompt: | header (allow trailing whitespace)
        if lines[i].rstrip("\n").rstrip() == "prompt: |" and not injected:
            # Inject the strict rules block immediately as the first content
            # of the prompt body. The body's natural indentation is two spaces
            # (matching STRICT_RULES_BLOCK).
            out.append(STRICT_RULES_BLOCK)
            injected = True
        i += 1

    if not injected:
        return "no-prompt"

    if not dry_run:
        p.write_text("".join(out), encoding="utf-8")
    return "injected"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--profile", default="calendar_ops",
                    help="Target profile (calendar_ops | kk_job | all). Default calendar_ops.")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    profiles = ["calendar_ops", "kk_job"] if args.profile == "all" else [args.profile]
    summary: dict[str, int] = {}
    for prof in profiles:
        cron_dir = REPO_PROFILES / prof / "cron"
        if not cron_dir.exists():
            print(f"[skip] {prof}: no cron dir at {cron_dir}")
            continue
        yamls = sorted(cron_dir.rglob("*.yaml"))
        print(f"\n[{prof}] {len(yamls)} yaml(s)")
        for y in yamls:
            status = inject_into_yaml(y, dry_run=args.dry_run)
            summary[status] = summary.get(status, 0) + 1
            print(f"  {status:<14} {y.relative_to(REPO_PROFILES)}")
    print()
    print(f"summary: {summary}")
    if args.dry_run:
        print("(dry-run — no files changed)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
