#!/usr/bin/env python3
"""validate_all_crons.py — Sequentially trigger every cron job, capture
result quality, write a markdown report to the user's desktop, and finally
shut down the machine.

Designed to run unattended (user goes to sleep). Each job is given a wait
window long enough for the slowest path (cold model load + multi-turn
inference + tool execution + webhook). The script does NOT retry — one
clean shot per job. Failures are recorded in the report so the operator
can re-tune model/prompt next morning.
"""
from __future__ import annotations

import datetime
import glob
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

# ── Config ──────────────────────────────────────────────────────────
HERMES_BIN = "/home/kang/.local/bin/hermes"
HERMES_HOME = Path("/home/kang/.hermes")

# (profile, job_id, job_name, default_model_to_record_if_success)
JOBS: list[tuple[str, str, str]] = [
    ("calendar_ops", "c95e88d45d32", "weather_briefing"),
    ("calendar_ops", "35c68c3d75cb", "morning_briefing"),
    ("calendar_ops", "7c1427a4fa4b", "daily_wrap"),
    ("calendar_ops", "4699d3e7ce51", "weekly_preview"),
    ("calendar_ops", "2300603cb5cc", "focus_time_report"),
    ("calendar_ops", "5c40c2a58244", "monthly_pattern"),
    ("calendar_ops", "a33fd9accb18", "weekly_retrospective"),
    ("kk_job",       "6ae38dd59b05", "deadline_reminder"),
    ("kk_job",       "86ebdd0d559e", "morning_game_jobs"),
    ("kk_job",       "7829c0b88c27", "weekly_job_digest"),
    ("advisor_ops",  "dfe064ffb074", "weekly_advisor_scan"),
]

# 가벼운 잡(format only)은 3분, 무거운 잡(analyze + Kanban + crawl)은 7분.
WAIT_SEC = {
    "weather_briefing":     180,
    "deadline_reminder":    180,
    "morning_briefing":     360,
    "daily_wrap":           360,
    "weekly_preview":       360,
    "focus_time_report":    420,
    "monthly_pattern":      420,
    "weekly_retrospective": 420,
    "morning_game_jobs":    540,
    "weekly_job_digest":    420,
    "weekly_advisor_scan":  540,
}

DESKTOP = Path("/mnt/c/Users/kang9/Desktop")
REPORT_PATH = DESKTOP / f"cron_validation_{datetime.datetime.now():%Y%m%d_%H%M}.md"
LOG_PATH = HERMES_HOME / "validate_all_crons.log"


def log(msg: str) -> None:
    line = f"[{datetime.datetime.now():%H:%M:%S}] {msg}"
    print(line, flush=True)
    with open(LOG_PATH, "a", encoding="utf-8") as f:
        f.write(line + "\n")


def trigger_job(profile: str, job_id: str) -> bool:
    """Fire the cron job. Returns True when hermes accepted the trigger."""
    r = subprocess.run(
        [HERMES_BIN, "-p", profile, "cron", "run", job_id],
        capture_output=True,
        text=True,
        timeout=30,
    )
    ok = "Triggered job" in (r.stdout or "")
    if not ok:
        log(f"  trigger failed: stdout={r.stdout[:200]} stderr={r.stderr[:200]}")
    return ok


def newest_session(profile: str, job_id: str, since: float) -> dict | None:
    """Find the newest session file produced after ``since`` (epoch)."""
    pattern = str(
        HERMES_HOME / "profiles" / profile / "sessions" / f"session_cron_{job_id}_*.json"
    )
    files = [f for f in glob.glob(pattern) if os.path.getmtime(f) >= since]
    if not files:
        return None
    files.sort(key=os.path.getmtime, reverse=True)
    return {"path": files[0], **json.loads(Path(files[0]).read_text())}


def assess(session: dict | None) -> tuple[str, str]:
    """Return (status, detail) — status one of: ok | incomplete | failed | no_session."""
    if session is None:
        return "no_session", "세션 파일 미생성 (cron tick 안 함 또는 trigger 미반영)"
    msgs = session.get("messages", [])
    model = session.get("model", "?")
    if not msgs:
        return "failed", "messages 0개"
    # Look at the last 5 tool-role messages — webhook이 성공하면 200/204/OK 표식
    tail_tools = [m for m in msgs if m.get("role") == "tool"][-6:]
    has_webhook_success = False
    has_error = False
    for m in tail_tools:
        c = str(m.get("content", ""))
        if '"error"' in c and ("MCP error" in c or "exit_code" in c and '"exit_code": 0' not in c):
            has_error = True
        if "post_webhook" in c.lower() or "sheets_append" in c.lower():
            if '"exit_code": 0' in c and '"error": null' in c:
                has_webhook_success = True
    # 마지막 assistant 응답에 "✅ 전송 완료" 같은 한국어 표시
    last_assistant = next(
        (m for m in reversed(msgs) if m.get("role") == "assistant"),
        None,
    )
    final_text = ""
    if last_assistant:
        c = last_assistant.get("content")
        if isinstance(c, str):
            final_text = c
        elif isinstance(c, list):
            final_text = " ".join(
                str(x.get("text") or x.get("type") or x)[:200] for x in c if x
            )
    delivered = (
        "✅ 전송 완료" in final_text
        or "전송 성공" in final_text
        or "204" in final_text
        or "Successfully sent" in final_text
        or has_webhook_success
    )
    if delivered:
        return "ok", f"webhook 전송 성공, model={model}"
    if has_error:
        return "incomplete", f"tool error 있음, model={model}, msgs={len(msgs)}"
    return "incomplete", f"webhook 호출 흔적 없음, model={model}, msgs={len(msgs)}"


def write_report(results: list[dict]) -> None:
    md_lines: list[str] = []
    now = datetime.datetime.now()
    md_lines.append(f"# Cron Job 자동 검증 보고서")
    md_lines.append("")
    md_lines.append(f"- 생성 시각: **{now:%Y-%m-%d %H:%M:%S}** (KST)")
    md_lines.append(f"- 총 잡 수: **{len(results)}**")
    md_lines.append(f"- 성공(ok): **{sum(1 for r in results if r['status']=='ok')}**")
    md_lines.append(f"- 부분 동작(incomplete): **{sum(1 for r in results if r['status']=='incomplete')}**")
    md_lines.append(f"- 실패(failed/no_session): **{sum(1 for r in results if r['status'] in ('failed','no_session'))}**")
    md_lines.append("")

    md_lines.append("## 결과 요약")
    md_lines.append("")
    md_lines.append("| 잡 | profile | 모델 | 상태 | duration | msg | 비고 |")
    md_lines.append("|---|---|---|---|---|---|---|")
    for r in results:
        md_lines.append(
            f"| `{r['name']}` | {r['profile']} | `{r['model']}` | "
            f"{r['status_emoji']} {r['status']} | {r.get('duration','-')} | "
            f"{r.get('msg_count','-')} | {r['detail']} |"
        )
    md_lines.append("")

    # 재실행 가이드
    md_lines.append("## 다음에 같은 검증을 다시 돌리려면")
    md_lines.append("")
    md_lines.append("```bash")
    md_lines.append("# WSL 안에서:")
    md_lines.append("python3 /mnt/e/hermes-hybrid/scripts/validate_all_crons.py")
    md_lines.append("```")
    md_lines.append("")
    md_lines.append("개별 잡만 trigger 하려면:")
    md_lines.append("")
    md_lines.append("```bash")
    for r in results:
        md_lines.append(
            f"hermes -p {r['profile']} cron run {r['job_id']}    # {r['name']}"
        )
    md_lines.append("```")
    md_lines.append("")

    # 최종 모델 매핑 (검증 결과 기반)
    md_lines.append("## 최종 모델 매핑 (검증 결과 반영)")
    md_lines.append("")
    md_lines.append("| 잡 | 채택 모델 | 근거 |")
    md_lines.append("|---|---|---|")
    for r in results:
        if r['status'] == 'ok':
            md_lines.append(
                f"| `{r['name']}` | `{r['model']}` | 검증 통과 |"
            )
        else:
            md_lines.append(
                f"| `{r['name']}` | `{r['model']}` (잠정) | "
                f"{r['status']} — 추후 재조정 필요: {r['detail']} |"
            )
    md_lines.append("")

    # 향후 수정 권장
    bad = [r for r in results if r['status'] != 'ok']
    if bad:
        md_lines.append("## 다음 라운드에 시도할 것")
        md_lines.append("")
        for r in bad:
            md_lines.append(f"### `{r['name']}` ({r['status']})")
            md_lines.append(f"- 현재 모델: `{r['model']}`")
            md_lines.append(f"- 원인 추정: {r['detail']}")
            md_lines.append(f"- 권장 시도:")
            md_lines.append(f"  - 더 큰 instruction-following 모델 (`qwen2.5-coder:32b-instruct`, `qwen3:30b-a3b`)")
            md_lines.append(f"  - prompt 단순화 — 잡 yaml 의 step 수 줄이기")
            md_lines.append(f"  - tool fields 축소 (cocal MCP 결과 길면 응답 생성 어려움)")
            md_lines.append(f"  - hermes timeout 증가 (`agent.max_turns` / cron timeout)")
            md_lines.append("")

    REPORT_PATH.write_text("\n".join(md_lines), encoding="utf-8")
    log(f"report written: {REPORT_PATH}")


def commit_final_models(results: list[dict]) -> None:
    """검증 통과한 잡들에 한해서 jobs.json 의 model 을 최종 채택값으로 박아둔다.
    실패한 잡은 그대로 둔다 (잠정 모델 유지)."""
    # 잡별 model 은 trigger 시점의 jobs.json 값. 검증 ok 면 그 값이 best.
    # 우리는 모두 이미 qwen2.5:14b-instruct 로 통합됐으니 별도 변경 없음.
    # (다른 round에서 잡별 차등할 때 이 함수가 의미가 살아남.)
    log("commit_final_models: 모든 잡 이미 qwen2.5:14b-instruct 로 통합 — no-op")


def shutdown_machine(delay_sec: int = 60) -> None:
    """Windows shutdown via interop (wsl 에서 windows 바이너리 직접 호출)."""
    log(f"scheduling shutdown in {delay_sec}s")
    try:
        subprocess.run(
            [
                "/mnt/c/Windows/System32/shutdown.exe",
                "/s",
                "/t",
                str(delay_sec),
                "/c",
                "cron 자동 검증 완료 — 머신 종료",
            ],
            check=False,
            timeout=10,
        )
    except Exception as e:
        log(f"shutdown.exe 호출 실패: {e}")


def main() -> int:
    log("=== validate_all_crons.py 시작 ===")
    LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    overall_start = time.time()
    results: list[dict] = []

    for profile, job_id, name in JOBS:
        log(f"\n--- {name} ({profile}) ---")
        before = time.time()
        # gateway 가 죽었으면 활성 시도 (재시작은 매번 안 함)
        ok = trigger_job(profile, job_id)
        if not ok:
            results.append(
                {
                    "name": name, "profile": profile, "job_id": job_id,
                    "model": "?", "status": "failed",
                    "status_emoji": "❌", "detail": "trigger 거부됨",
                    "duration": "-", "msg_count": 0,
                }
            )
            continue

        wait = WAIT_SEC.get(name, 360)
        log(f"  triggered, waiting {wait}s for completion")
        time.sleep(wait)

        sess = newest_session(profile, job_id, before)
        status, detail = assess(sess)
        emoji = {"ok": "✅", "incomplete": "⚠️", "failed": "❌", "no_session": "❌"}[status]
        rec = {
            "name": name, "profile": profile, "job_id": job_id,
            "model": (sess or {}).get("model", "?"),
            "status": status, "status_emoji": emoji, "detail": detail,
            "msg_count": len((sess or {}).get("messages", [])),
        }
        if sess:
            try:
                start = datetime.datetime.fromisoformat(sess["session_start"])
                end = datetime.datetime.fromisoformat(sess["last_updated"])
                rec["duration"] = f"{(end-start).total_seconds():.0f}s"
            except Exception:
                rec["duration"] = "?"
        else:
            rec["duration"] = "-"
        results.append(rec)
        log(f"  result: {emoji} {status} — {detail}")

    log(f"\n=== 모든 잡 검증 완료 ({time.time()-overall_start:.0f}s) ===")
    write_report(results)
    commit_final_models(results)
    log("shutdown 예약")
    shutdown_machine(delay_sec=120)
    log("=== 종료 ===")
    return 0


if __name__ == "__main__":
    sys.exit(main())
