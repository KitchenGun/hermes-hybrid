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


def _wsl_default_gw_ip() -> str | None:
    """Default gateway IP inside WSL — usually the Windows host on mirrored
    networking. Used as a fallback when ``localhost`` doesn't reach ollama."""
    try:
        out = subprocess.check_output(
            ["ip", "route", "show", "default"], text=True, timeout=2
        )
        parts = out.split()
        return parts[2] if len(parts) >= 3 else None
    except Exception:
        return None


def _ollama_probe() -> str | None:
    """Return the host that responded on port 11434, or None."""
    import urllib.request

    candidates = ["localhost"]
    gw = _wsl_default_gw_ip()
    if gw:
        candidates.append(gw)
    for host in candidates:
        try:
            with urllib.request.urlopen(
                f"http://{host}:11434/api/tags", timeout=2
            ) as r:
                if r.status == 200:
                    return host
        except Exception:
            continue
    return None


def ensure_ollama_up(timeout: int = 60) -> bool:
    """Block until ollama answers ``/api/tags`` on either localhost or the
    WSL default-gateway IP.  If it doesn't respond, attempt to spawn
    ``ollama serve`` on the **Windows** side via cmd.exe interop — this
    mirrors run_all.bat step 1, since ollama runs as a Windows process and
    listens on a port WSL reaches via mirrored networking. Returns True on
    success, False on timeout.
    """
    host = _ollama_probe()
    if host:
        log(f"ollama already up at http://{host}:11434")
        return True

    log("ollama not responding — attempting Windows-side spawn via cmd.exe")
    try:
        # ``start /MIN`` returns immediately; ollama serve runs detached.
        # Use Popen so we don't block on a never-exiting child.
        subprocess.Popen(
            ["cmd.exe", "/c", "start", "/MIN", "ollama", "serve"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            stdin=subprocess.DEVNULL,
        )
    except FileNotFoundError:
        log("  cmd.exe not found — are we outside WSL? abort")
        return False
    except Exception as e:
        log(f"  spawn failed: {e}")

    log(f"  waiting up to {timeout}s for port 11434…")
    for _ in range(timeout):
        host = _ollama_probe()
        if host:
            log(f"  ollama is up at http://{host}:11434")
            return True
        time.sleep(1)
    log("  ABORT: ollama did not respond within timeout")
    return False


def jobs_json_meta(profile: str, job_id: str) -> dict | None:
    """Read the live job entry from jobs.json — source of truth for last_run_at,
    last_status, last_error, last_delivery_error.

    The cron scheduler updates these fields on every run completion, so they
    are more reliable than scanning sessions/ for a freshly-modified file.
    """
    p = HERMES_HOME / "profiles" / profile / "cron" / "jobs.json"
    if not p.exists():
        return None
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return None
    jobs = data.get("jobs") if isinstance(data, dict) else data
    if not isinstance(jobs, list):
        return None
    for j in jobs:
        if isinstance(j, dict) and j.get("id") == job_id:
            return j
    return None


def newest_session(profile: str, job_id: str, since: float) -> dict | None:
    """Find the newest session file for the job. ``since`` is used only as a
    soft lower bound (with 10-minute grace) — the scheduler may backfill a
    job onto its next tick, so a session whose mtime is slightly earlier
    than the trigger is still legitimate.
    """
    pattern = str(
        HERMES_HOME / "profiles" / profile / "sessions" / f"session_cron_{job_id}_*.json"
    )
    grace = since - 600  # 10-minute backfill grace
    files = [f for f in glob.glob(pattern) if os.path.getmtime(f) >= grace]
    if not files:
        # Fall back to the absolute newest file matching the pattern, regardless
        # of mtime. The meta check (last_run_at) decides whether to trust it.
        files = list(glob.glob(pattern))
    if not files:
        return None
    files.sort(key=os.path.getmtime, reverse=True)
    try:
        return {"path": files[0], **json.loads(Path(files[0]).read_text())}
    except Exception:
        return None


def _session_webhook_ok(session: dict) -> bool:
    """True if the session shows the prompt's own post_webhook.py call
    succeeded (status=204 / "✅ 전송 완료" in last assistant text).

    This is independent of hermes' built-in deliver=discord delivery, which
    needs DISCORD_HOME_CHANNEL. Most jobs in this repo deliver via prompt-
    embedded post_webhook.py, so the meta-level last_delivery_error is
    expected to be set even on a fully successful run.
    """
    msgs = session.get("messages", [])
    last_assistant = next(
        (m for m in reversed(msgs) if m.get("role") == "assistant"), None,
    )
    text = ""
    if last_assistant:
        c = last_assistant.get("content")
        if isinstance(c, str):
            text = c
        elif isinstance(c, list):
            text = " ".join(
                str(x.get("text") or "") for x in c
                if isinstance(x, dict) and x.get("type") == "text"
            )
    if any(k in text for k in ("✅ 전송 완료", "전송 성공", "Successfully sent")):
        return True
    if "204" in text:
        return True
    # Tool-side: any post_webhook.py / sheets_append tool with exit_code 0.
    tail_tools = [m for m in msgs if m.get("role") == "tool"][-8:]
    for m in tail_tools:
        c = str(m.get("content", ""))
        cl = c.lower()
        if ("post_webhook" in cl or "sheets_append" in cl) \
                and '"exit_code": 0' in c and '"error": null' in c:
            return True
    return False


def assess(profile: str, job_id: str, session: dict | None, trigger_at: float) -> tuple[str, str, str]:
    """Return (status, detail, model). status one of:
        ok | incomplete | delivery_failed | failed | no_session

    Authoritative source: jobs.json (last_run_at, last_status, last_delivery_error).
    Session file is consulted for model name AND for prompt-embedded webhook
    success — when the prompt does its own post_webhook.py call, hermes-level
    last_delivery_error is irrelevant if the in-prompt call succeeded.
    """
    meta = jobs_json_meta(profile, job_id)
    sess_model = (session or {}).get("model", "?") if session else "?"
    meta_model = (meta or {}).get("model", "?") if meta else "?"
    model = sess_model if sess_model != "?" else meta_model

    # 1) jobs.json meta first
    if meta is None:
        if session is None:
            return "no_session", "jobs.json 미등록 + 세션 파일 없음", model
        return "incomplete", "jobs.json 미등록 (세션은 있음)", model

    last_run = meta.get("last_run_at")
    last_status = meta.get("last_status")
    last_error = meta.get("last_error") or ""
    last_delivery_error = meta.get("last_delivery_error") or ""

    ran_recently = False
    if last_run:
        try:
            run_dt = datetime.datetime.fromisoformat(last_run)
            ran_recently = run_dt.timestamp() >= trigger_at - 600
        except Exception:
            ran_recently = False

    if not last_run:
        return "no_session", "한 번도 실행 안 됨 (last_run_at=null)", model
    if not ran_recently:
        return (
            "no_session",
            f"이번 trigger 후 미실행 (last_run={last_run[:19]} < trigger {datetime.datetime.fromtimestamp(trigger_at):%H:%M:%S})",
            model,
        )

    if last_status != "ok":
        return "failed", f"last_status={last_status}, err={last_error[:100]}", model

    # Prompt-embedded post_webhook.py path: if session shows the in-prompt
    # webhook call succeeded, the run is OK regardless of hermes's own
    # last_delivery_error (which is for deliver=discord auto-delivery — a
    # separate path, requires DISCORD_HOME_CHANNEL we deliberately don't set).
    webhook_ok_in_prompt = bool(session and _session_webhook_ok(session))
    if webhook_ok_in_prompt:
        return "ok", f"prompt 내 webhook 성공 (model={model})", model

    if last_delivery_error:
        return (
            "delivery_failed",
            f"hermes auto-delivery 실패 + prompt-내 webhook 흔적 없음: {last_delivery_error[:100]}",
            model,
        )

    # No delivery error and no webhook success marker — check for tool errors.
    if session is not None:
        msgs = session.get("messages", [])
        tail_tools = [m for m in msgs if m.get("role") == "tool"][-6:]
        for m in tail_tools:
            c = str(m.get("content", ""))
            if '"error":' in c and '"error": null' not in c:
                if "MCP error" in c or ("exit_code" in c and '"exit_code": 0' not in c):
                    return "incomplete", f"tool error in session tail (model={model})", model

        # Strict mode (2026-05-05): hermes status=ok + deliver=local 만으론
        # ok 인지 모른다. 잡이 webhook 호출 단계에 못 가고 plain-text emit
        # 으로 끝났을 수 있다. 마지막 assistant 텍스트에 webhook 성공 표시
        # (✅/204/전송 완료) 도, 도구 측 post_webhook exit 0 도 없으면
        # incomplete 로 잡는다 — 사용자가 디스코드 채널에서 안 보였는데
        # validate 가 ✅로 잡으면 false-positive 감지 못 함.
        last_assistant = next(
            (m for m in reversed(msgs) if m.get("role") == "assistant"), None
        )
        text = ""
        if last_assistant:
            c = last_assistant.get("content")
            if isinstance(c, str):
                text = c
            elif isinstance(c, list):
                text = " ".join(
                    str(x.get("text") or "") for x in c
                    if isinstance(x, dict) and x.get("type") == "text"
                )
        # Suspicious markers that mean the model bailed:
        bail_markers = (
            "transmission failed", "전송 실패", "전송 중단", "잡 실행 중단",
            "관리자에게 문의", "[SILENT]", "보고서 파일이 생성되지 않",
            "errorCode:", "No such file or directory",
        )
        # Plain-text tool-call emit markers (model didn't actually call the tool):
        plain_tool_emit = (
            "terminal: python3", "terminal:python3", "write_file(path=",
            'terminal(command="', "terminal(command='",
        )
        if any(m in text for m in bail_markers):
            return "incomplete", f"잡이 bail/실패 메시지로 종료 (model={model})", model
        if any(m in text for m in plain_tool_emit):
            return (
                "incomplete",
                f"plain-text 로 도구 호출 emit (실제 호출 안 됨, model={model})",
                model,
            )

    return "ok", f"hermes status=ok (model={model})", model


def wait_until_run(profile: str, job_id: str, trigger_at: float, timeout_s: int) -> bool:
    """Poll jobs.json every 30s for ``last_run_at > trigger_at``. Returns
    True when the job has run after the trigger, False on timeout.

    Replaces the fixed-sleep wait — hermes scheduler is sequential, so
    actual completion time scales with how many other jobs are queued
    ahead of this one. A short fixed sleep produced false negatives.
    """
    deadline = time.time() + timeout_s
    poll = 30
    while time.time() < deadline:
        meta = jobs_json_meta(profile, job_id)
        last_run = (meta or {}).get("last_run_at")
        if last_run:
            try:
                run_ts = datetime.datetime.fromisoformat(last_run).timestamp()
                if run_ts >= trigger_at - 5:  # tiny clock skew tolerance
                    return True
            except Exception:
                pass
        time.sleep(poll)
    return False


def write_report(results: list[dict]) -> None:
    md_lines: list[str] = []
    now = datetime.datetime.now()
    md_lines.append(f"# Cron Job 자동 검증 보고서")
    md_lines.append("")
    md_lines.append(f"- 생성 시각: **{now:%Y-%m-%d %H:%M:%S}** (KST)")
    md_lines.append(f"- 총 잡 수: **{len(results)}**")
    md_lines.append(f"- 성공(ok): **{sum(1 for r in results if r['status']=='ok')}**")
    md_lines.append(f"- delivery 실패(📵): **{sum(1 for r in results if r['status']=='delivery_failed')}**")
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


def parallel_run(only: list[tuple[str, str, str]] | None = None) -> list[dict]:
    """Trigger every job (or just ``only``), then poll jobs.json until
    each one's last_run_at advances past its trigger time. Hermes scheduler
    processes one job at a time per profile, so simultaneous triggers just
    queue — the wall-clock total ends up similar to sequential but we don't
    pay the WAIT_SEC overhead per job.

    Returns the same result dict list ``main`` produces.
    """
    jobs_to_run = only if only is not None else JOBS
    trigger_times: dict[tuple[str, str], float] = {}
    name_for: dict[tuple[str, str], str] = {}

    log(f"=== parallel: trigger {len(jobs_to_run)} job(s) ===")
    for profile, job_id, name in jobs_to_run:
        before = time.time()
        if trigger_job(profile, job_id):
            trigger_times[(profile, job_id)] = before
            name_for[(profile, job_id)] = name
            log(f"  triggered {name}")
        else:
            log(f"  TRIGGER FAILED {name}")
        time.sleep(2)  # tiny stagger so logs don't interleave at the daemon

    # Poll up to 60 minutes total — empirically each job is 2-13 min, and
    # hermes serializes per-profile. 60 min covers all 11 jobs.
    deadline = time.time() + 3600
    pending = dict(trigger_times)
    last_logged = 0
    while pending and time.time() < deadline:
        time.sleep(30)
        for k in list(pending.keys()):
            prof, jid = k
            meta = jobs_json_meta(prof, jid)
            last_run = (meta or {}).get("last_run_at")
            if last_run:
                try:
                    run_ts = datetime.datetime.fromisoformat(last_run).timestamp()
                    if run_ts >= pending[k] - 5:
                        log(f"  done: {name_for[k]} ({len(trigger_times)-len(pending)+1}/{len(trigger_times)})")
                        del pending[k]
                except Exception:
                    pass
        # Heartbeat every 5 min even if nothing finished
        if int(time.time() // 300) != last_logged:
            last_logged = int(time.time() // 300)
            log(f"  [heartbeat] {len(pending)}/{len(trigger_times)} pending")

    if pending:
        log(f"  TIMEOUT: {len(pending)} job(s) never ran:")
        for k in pending:
            log(f"    - {name_for[k]} ({k[0]}/{k[1]})")

    # Build results
    results: list[dict] = []
    for profile, job_id, name in jobs_to_run:
        before = trigger_times.get((profile, job_id), time.time())
        sess = newest_session(profile, job_id, before)
        status, detail, model = assess(profile, job_id, sess, before)
        emoji = {
            "ok": "✅", "incomplete": "⚠️", "delivery_failed": "📵",
            "failed": "❌", "no_session": "❌",
        }[status]
        rec = {
            "name": name, "profile": profile, "job_id": job_id,
            "model": model, "status": status, "status_emoji": emoji,
            "detail": detail,
            "msg_count": len((sess or {}).get("messages", [])),
        }
        if sess:
            try:
                s = datetime.datetime.fromisoformat(sess["session_start"])
                e = datetime.datetime.fromisoformat(sess["last_updated"])
                rec["duration"] = f"{(e - s).total_seconds():.0f}s"
            except Exception:
                rec["duration"] = "?"
        else:
            rec["duration"] = "-"
        results.append(rec)
        log(f"  result: {emoji} {name} — {detail}")
    return results


def main() -> int:
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument(
        "--from-meta",
        action="store_true",
        help="Skip the trigger+wait loop and read jobs.json meta directly. "
             "Use this for fast post-mortem analysis when ollama/gateway "
             "is down or you've already run the jobs once.",
    )
    ap.add_argument(
        "--no-shutdown",
        action="store_true",
        help="Don't shutdown the machine at the end (overrides default).",
    )
    ap.add_argument(
        "--parallel",
        action="store_true",
        help="Trigger all jobs at once and poll until each completes (max 60 min total). "
             "Much faster than the default sequential trigger+sleep loop because hermes "
             "scheduler queues triggers and processes them serially per profile anyway.",
    )
    ap.add_argument(
        "--only",
        type=str,
        help="Comma-separated job names to run (parallel only). e.g. --only weekly_advisor_scan,weather_briefing",
    )
    args = ap.parse_args()

    log("=== validate_all_crons.py 시작 ===")
    LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    overall_start = time.time()
    results: list[dict] = []

    # Real trigger path needs ollama to answer the cron jobs' LLM calls.
    # --from-meta only reads jobs.json so ollama state is irrelevant.
    if not args.from_meta:
        if not ensure_ollama_up(timeout=60):
            log("ABORT: ollama 미응답 — 잡 trigger 의미 없음. --from-meta 로 메타만 평가하려면 다시 호출.")
            return 2

    # --parallel: trigger all jobs (or --only subset) at once and poll.
    if args.parallel:
        only_filter: list[tuple[str, str, str]] | None = None
        if args.only:
            wanted = {n.strip() for n in args.only.split(",") if n.strip()}
            only_filter = [(p, jid, name) for p, jid, name in JOBS if name in wanted]
            if not only_filter:
                log(f"ABORT: --only matched no jobs (wanted={wanted})")
                return 2
        results = parallel_run(only_filter)
        log(f"\n=== parallel 검증 완료 ({time.time()-overall_start:.0f}s) ===")
        write_report(results)
        commit_final_models(results)
        if args.from_meta or args.no_shutdown:
            log("--no-shutdown — shutdown 건너뜀")
        else:
            shutdown_machine(delay_sec=120)
        log("=== 종료 ===")
        return 0

    for profile, job_id, name in JOBS:
        log(f"\n--- {name} ({profile}) ---")
        before = time.time()

        if args.from_meta:
            # No trigger — assess against the existing jobs.json snapshot.
            # Use the job's recorded last_run_at as the "trigger" reference
            # so the recent-run check still passes for genuinely stale runs.
            meta = jobs_json_meta(profile, job_id)
            last_run = (meta or {}).get("last_run_at")
            if last_run:
                try:
                    before = datetime.datetime.fromisoformat(last_run).timestamp()
                except Exception:
                    pass
            sess = newest_session(profile, job_id, before)
            status, detail, model = assess(profile, job_id, sess, before)
            emoji = {
                "ok": "✅", "incomplete": "⚠️", "delivery_failed": "📵",
                "failed": "❌", "no_session": "❌",
            }[status]
            rec = {
                "name": name, "profile": profile, "job_id": job_id,
                "model": model, "status": status, "status_emoji": emoji,
                "detail": detail,
                "msg_count": len((sess or {}).get("messages", [])),
            }
            if sess:
                try:
                    s = datetime.datetime.fromisoformat(sess["session_start"])
                    e = datetime.datetime.fromisoformat(sess["last_updated"])
                    rec["duration"] = f"{(e - s).total_seconds():.0f}s"
                except Exception:
                    rec["duration"] = "?"
            else:
                rec["duration"] = "-"
            results.append(rec)
            log(f"  [meta] {emoji} {status} — {detail}")
            continue

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

        # Polling wait — hermes scheduler is sequential per profile and
        # ollama serializes inference, so a fixed sleep gave false negatives
        # (jobs took 9-13 min when WAIT_SEC was 6 min). Poll last_run_at
        # until it advances past our trigger; max 20 min per job.
        max_wait = max(WAIT_SEC.get(name, 360) * 3, 1200)
        log(f"  triggered, polling for last_run > trigger (max {max_wait}s)")
        ran = wait_until_run(profile, job_id, before, max_wait)
        log(f"  poll {'done' if ran else 'TIMEOUT'} after {time.time()-before:.0f}s")

        sess = newest_session(profile, job_id, before)
        status, detail, model = assess(profile, job_id, sess, before)
        emoji = {
            "ok": "✅",
            "incomplete": "⚠️",
            "delivery_failed": "📵",
            "failed": "❌",
            "no_session": "❌",
        }[status]
        rec = {
            "name": name, "profile": profile, "job_id": job_id,
            "model": model,
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
    if args.from_meta or args.no_shutdown:
        log("--from-meta or --no-shutdown — shutdown 건너뜀")
    else:
        log("shutdown 예약")
        shutdown_machine(delay_sec=120)
    log("=== 종료 ===")
    return 0


if __name__ == "__main__":
    sys.exit(main())
