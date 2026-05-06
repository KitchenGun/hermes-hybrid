"""Memory Curator — Phase 14 (2026-05-07).

Hermes Agent 의 "Agent-curated memory with periodic nudges" + "FTS5
cross-session recall with LLM summarization" 패턴 흡수.

목적: 사용자 manual `/memo save` 의존 폐지. 봇이 사용 패턴을 자동 학습해
다음 호출에 반영.

산출물:
  - data/memory/MEMORY.md — agent notes (auto-curated). 매 N task 끝나면
    최근 N task 의 요약 1-2줄 append.
  - data/memory/USER.md — user profile (auto-learned). 일요일 22시
    (Reflection 이후) 갱신. 자주 쓰는 agent / 시간대 / 주제 cluster.

매 호출 시 master prompt 에 자동 prepend:
  [system prompt]

  ## User profile (auto-learned)
  {USER.md}

  ## Recent agent notes (auto-curated)
  {MEMORY.md tail 1500 chars}

  ## @handle SKILL.md inject (Phase 9)
  ...

  ## User
  {user message}

크기 제어: MEMORY.md 가 max_chars 초과 시 LLM 자동 요약 (compaction).
USER.md 는 짧게 유지 (200자 이내).

Privacy: ExperienceLog 가 sha16+length 만 가지고 있어서, MEMORY 큐레이션
prompt 도 raw user_message 를 직접 보지 못함. 대신 handled_by / agent_handles
/ pipeline_id / model_outputs 같은 metadata 만 LLM 에 넘김 — privacy 보호.
"""
from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from src.obs import get_logger

log = get_logger(__name__)


_MEMORY_FNAME = "MEMORY.md"
_USER_FNAME = "USER.md"


class MemoryCurator:
    """Auto-curate MEMORY.md + USER.md from ExperienceLog metadata."""

    def __init__(
        self,
        adapter: Any,                          # ClaudeCodeAdapter-like
        memory_root: Path,
        experience_log_root: Path,
        *,
        every_n_tasks: int = 5,
        max_chars: int = 1500,
        enabled: bool = True,
    ):
        self.adapter = adapter
        self.memory_root = Path(memory_root)
        self.experience_log_root = Path(experience_log_root)
        self.every_n_tasks = max(1, every_n_tasks)
        self.max_chars = max(200, max_chars)
        self.enabled = enabled

        # task counter — increments on every recorded task. trigger curation
        # when counter % every_n_tasks == 0.
        self._counter: int = 0

    # ---- prompt prepend (synchronous, fast path) ---------------------

    def read_prompt_prepend(self) -> str:
        """Return USER.md + MEMORY.md tail formatted for master prompt
        prepend. Empty string if both absent or disabled."""
        if not self.enabled:
            return ""
        parts: list[str] = []
        user_md = self._read(self.memory_root / _USER_FNAME)
        if user_md:
            parts.append("## User profile (auto-learned)\n" + user_md.strip())
        mem_md = self._read(self.memory_root / _MEMORY_FNAME)
        if mem_md:
            tail = mem_md[-self.max_chars:]
            parts.append("## Recent agent notes (auto-curated)\n" + tail.strip())
        return "\n\n".join(parts)

    # ---- post-task hook (async) --------------------------------------

    async def maybe_curate_after_task(self, task: Any) -> None:
        """Called from HermesMaster._finalize. Best-effort (failures swallowed)."""
        if not self.enabled:
            return
        try:
            self._counter += 1
            if self._counter % self.every_n_tasks != 0:
                return
            await self._curate_recent(self.every_n_tasks)
            await self._maybe_compact()
        except Exception as e:  # noqa: BLE001
            log.warning("memory.curate_failed", err=str(e))

    async def _curate_recent(self, n: int) -> None:
        """Read last N ExperienceLog rows, ask master for a 1-2 line summary,
        append to MEMORY.md."""
        rows = list(self._tail_experience_log(n))
        if not rows:
            return

        # privacy-safe summary input — no raw text, just metadata
        meta_summary = "\n".join(
            f"- ts={r.get('ts','')} handled_by={r.get('handled_by','')} "
            f"agents={','.join(r.get('agent_handles') or [])} "
            f"pipeline={r.get('pipeline_id') or '-'} "
            f"score={r.get('self_score', 0):.2f} "
            f"input_len={r.get('input_text_length', 0)} "
            f"resp_len={r.get('response_length', 0)}"
            for r in rows
        )
        prompt = (
            "다음은 최근 사용자 task N 개의 metadata. "
            "raw 본문은 포함되지 않음 (privacy). "
            "이 metadata 를 보고 다음 호출에 도움될 짧은 한국어 메모 1-2 줄 작성. "
            "예: '사용자가 @coder + @reviewer 페어 자주 사용. fizzbuzz 류 작은 작업 5회.'\n\n"
            f"{meta_summary}\n\n"
            "응답: 메모만, 마크다운 헤더 X, 1-2줄."
        )

        try:
            result = await self.adapter.run(prompt=prompt, history=[])
            note = (getattr(result, "text", "") or "").strip()
        except Exception as e:  # noqa: BLE001
            log.warning("memory.curate_adapter_failed", err=str(e))
            return

        if not note:
            return

        # Append to MEMORY.md
        ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M")
        line = f"- [{ts}] {note}\n"
        self._append(self.memory_root / _MEMORY_FNAME, line)
        log.info("memory.curated", note_chars=len(note))

    async def _maybe_compact(self) -> None:
        """If MEMORY.md > max_chars*2, ask master to compress to max_chars."""
        path = self.memory_root / _MEMORY_FNAME
        text = self._read(path)
        if not text or len(text) < self.max_chars * 2:
            return

        prompt = (
            "다음은 누적된 agent 메모. 핵심 패턴/사실만 남기고 한국어로 압축. "
            f"목표 길이 ≤{self.max_chars}자. 글머리 형식 유지.\n\n"
            f"{text}"
        )
        try:
            result = await self.adapter.run(prompt=prompt, history=[])
            compacted = (getattr(result, "text", "") or "").strip()
        except Exception as e:  # noqa: BLE001
            log.warning("memory.compact_failed", err=str(e))
            return

        if not compacted:
            return
        # Replace MEMORY.md with compacted version + timestamp marker
        ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M")
        header = f"# Hermes Agent Memory — auto-curated\n# Last compacted: {ts}\n\n"
        self._write(path, header + compacted + "\n")
        log.info("memory.compacted", from_chars=len(text), to_chars=len(compacted))

    # ---- USER.md (less frequent, explicit) ---------------------------

    async def update_user_profile(self, days: int = 7) -> None:
        """일요일 22시 (Reflection 후) 호출. ExperienceLog 의 N일치를 보고
        USER.md 후보를 갱신.

        구조:
          # User Profile (auto-learned)

          ## 자주 쓰는 agent
          - @coder (32%) · @researcher (18%) · @reviewer (14%)

          ## 활동 시간대
          - 평일 21–23시 / 주말 14–17시

          ## 주제 cluster
          - 게임 엔진 개발 (Unreal/Unity)
          - hermes-hybrid 자체 개선
        """
        if not self.enabled:
            return

        rows = list(self._tail_experience_log(500))
        if len(rows) < 5:
            return

        meta = self._aggregate(rows)
        prompt = (
            "다음은 사용자의 최근 활동 통계. 한국어 USER.md 작성. "
            "섹션 3개 — '자주 쓰는 agent' / '활동 시간대' / '주제 cluster'. "
            "각 섹션 2-3 줄 이내. 추측 금지 — 통계에 근거.\n\n"
            f"{meta}"
        )

        try:
            result = await self.adapter.run(prompt=prompt, history=[])
            text = (getattr(result, "text", "") or "").strip()
        except Exception as e:  # noqa: BLE001
            log.warning("memory.user_profile_failed", err=str(e))
            return

        if not text:
            return
        path = self.memory_root / _USER_FNAME
        self._write(path, text + "\n")
        log.info("memory.user_profile_updated", chars=len(text))

    # ---- helpers -----------------------------------------------------

    def _tail_experience_log(self, n: int) -> Iterable[dict[str, Any]]:
        """Yield the last N rows from ExperienceLog JSONL files. Newest first.

        We walk the date-sharded files in reverse chronological order and
        stream lines. Stop once we have n rows.
        """
        if not self.experience_log_root.exists():
            return
        files = sorted(self.experience_log_root.glob("*.jsonl"), reverse=True)
        rows: list[dict[str, Any]] = []
        for f in files:
            try:
                lines = f.read_text(encoding="utf-8").splitlines()
            except OSError:
                continue
            for raw in reversed(lines):
                raw = raw.strip()
                if not raw:
                    continue
                try:
                    rows.append(json.loads(raw))
                except (ValueError, TypeError):
                    continue
                if len(rows) >= n:
                    break
            if len(rows) >= n:
                break
        return rows

    @staticmethod
    def _aggregate(rows: list[dict[str, Any]]) -> str:
        """Summarize rows into agent/time/topic stats string."""
        if not rows:
            return ""
        # agent freq
        agent_count: dict[str, int] = {}
        hour_count: dict[int, int] = {}
        handler_count: dict[str, int] = {}
        for r in rows:
            for h in r.get("agent_handles") or []:
                agent_count[h] = agent_count.get(h, 0) + 1
            ts = r.get("ts", "")
            try:
                dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
                hour_count[dt.hour] = hour_count.get(dt.hour, 0) + 1
            except (ValueError, TypeError):
                pass
            hb = r.get("handled_by", "")
            handler_count[hb] = handler_count.get(hb, 0) + 1

        agent_pct = {
            a: 100 * c / max(1, sum(agent_count.values()))
            for a, c in agent_count.items()
        }
        top_agents = sorted(agent_pct.items(), key=lambda x: -x[1])[:5]
        top_hours = sorted(hour_count.items(), key=lambda x: -x[1])[:5]
        top_handlers = sorted(handler_count.items(), key=lambda x: -x[1])[:5]

        return (
            f"총 task: {len(rows)}\n"
            f"top agents: {', '.join(f'{a}({p:.0f}%)' for a,p in top_agents)}\n"
            f"top hours: {', '.join(f'{h}시({c})' for h,c in top_hours)}\n"
            f"top handled_by: {', '.join(f'{h}({c})' for h,c in top_handlers)}"
        )

    def _read(self, path: Path) -> str:
        if not path.exists():
            return ""
        try:
            return path.read_text(encoding="utf-8")
        except OSError:
            return ""

    def _append(self, path: Path, text: str) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        try:
            with path.open("a", encoding="utf-8") as f:
                f.write(text)
        except OSError as e:
            log.warning("memory.append_failed", path=str(path), err=str(e))

    def _write(self, path: Path, text: str) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        try:
            path.write_text(text, encoding="utf-8")
        except OSError as e:
            log.warning("memory.write_failed", path=str(path), err=str(e))


__all__ = ["MemoryCurator"]
