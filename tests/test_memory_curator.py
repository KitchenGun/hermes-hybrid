"""Phase 14 — Memory Curator tests.

Locks down:
  * read_prompt_prepend — empty when files absent + when disabled
  * read_prompt_prepend — formatted USER + MEMORY tail when present
  * maybe_curate_after_task — counter increments + N-th call triggers curation
  * curation appends 1 line to MEMORY.md (privacy: no raw text)
  * compaction triggers when MEMORY.md > max_chars*2
  * disabled=True → no-op
  * adapter exception → swallowed
"""
from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

from src.core.memory_curator import MemoryCurator


@dataclass
class _Result:
    text: str = "auto memo line"
    model_name: str = "opus"
    input_tokens: int = 10
    output_tokens: int = 5
    duration_ms: int = 50
    session_id: str = "s1"
    total_cost_usd: float = 0.0


class _StubAdapter:
    def __init__(self, *, raises: Exception | None = None, response: str = "auto memo line"):
        self.calls: list[dict[str, Any]] = []
        self._raises = raises
        self._response = response

    async def run(self, *, prompt: str, history=None, model=None, timeout_ms=None):
        self.calls.append({"prompt": prompt})
        if self._raises is not None:
            raise self._raises
        return _Result(text=self._response)


@dataclass
class _StubTask:
    task_id: str = "t1"


def _seed_log(root: Path, n: int) -> None:
    """Write N fake ExperienceLog rows to today.jsonl."""
    root.mkdir(parents=True, exist_ok=True)
    f = root / "2026-05-07.jsonl"
    rows = [
        {
            "ts": f"2026-05-07T0{i}:00:00+00:00",
            "task_id": f"t-{i}",
            "session_id": f"s-{i}",
            "user_id": "u1",
            "handled_by": "master:claude",
            "agent_handles": ["@coder"] if i % 2 == 0 else ["@reviewer"],
            "pipeline_id": None,
            "self_score": 0.8,
            "input_text_length": 30,
            "response_length": 200,
        }
        for i in range(n)
    ]
    f.write_text("\n".join(json.dumps(r) for r in rows) + "\n", encoding="utf-8")


# ---- read_prompt_prepend -------------------------------------------------


def test_prompt_prepend_empty_when_files_absent(tmp_path):
    cur = MemoryCurator(
        adapter=_StubAdapter(),
        memory_root=tmp_path / "mem",
        experience_log_root=tmp_path / "exp",
    )
    assert cur.read_prompt_prepend() == ""


def test_prompt_prepend_formatted_with_user_and_memory(tmp_path):
    mem_root = tmp_path / "mem"
    mem_root.mkdir(parents=True)
    (mem_root / "USER.md").write_text("자주 쓰는: @coder, @reviewer", encoding="utf-8")
    (mem_root / "MEMORY.md").write_text("- [2026-05-07T08:00] fizzbuzz 5회 작업.\n", encoding="utf-8")

    cur = MemoryCurator(
        adapter=_StubAdapter(),
        memory_root=mem_root,
        experience_log_root=tmp_path / "exp",
    )
    out = cur.read_prompt_prepend()
    assert "## User profile (auto-learned)" in out
    assert "@coder, @reviewer" in out
    assert "## Recent agent notes (auto-curated)" in out
    assert "fizzbuzz 5회 작업" in out


def test_prompt_prepend_returns_empty_when_disabled(tmp_path):
    mem_root = tmp_path / "mem"
    mem_root.mkdir(parents=True)
    (mem_root / "USER.md").write_text("data", encoding="utf-8")

    cur = MemoryCurator(
        adapter=_StubAdapter(),
        memory_root=mem_root,
        experience_log_root=tmp_path / "exp",
        enabled=False,
    )
    assert cur.read_prompt_prepend() == ""


# ---- maybe_curate_after_task --------------------------------------------


@pytest.mark.asyncio
async def test_curate_does_not_fire_until_n_tasks(tmp_path):
    _seed_log(tmp_path / "exp", n=5)
    cur = MemoryCurator(
        adapter=_StubAdapter(),
        memory_root=tmp_path / "mem",
        experience_log_root=tmp_path / "exp",
        every_n_tasks=5,
    )
    task = _StubTask()
    # 4 calls — not yet
    for _ in range(4):
        await cur.maybe_curate_after_task(task)
    assert not (tmp_path / "mem" / "MEMORY.md").exists()
    # 5th call — fires
    await cur.maybe_curate_after_task(task)
    assert (tmp_path / "mem" / "MEMORY.md").exists()


@pytest.mark.asyncio
async def test_curation_appends_one_line_with_timestamp(tmp_path):
    _seed_log(tmp_path / "exp", n=5)
    adapter = _StubAdapter(response="사용자가 @coder + @reviewer 자주 사용. 5회.")
    cur = MemoryCurator(
        adapter=adapter,
        memory_root=tmp_path / "mem",
        experience_log_root=tmp_path / "exp",
        every_n_tasks=5,
    )
    for _ in range(5):
        await cur.maybe_curate_after_task(_StubTask())

    mem_text = (tmp_path / "mem" / "MEMORY.md").read_text(encoding="utf-8")
    assert "사용자가 @coder + @reviewer 자주 사용" in mem_text
    # timestamp prefix [YYYY-MM-DDTHH:MM]
    assert mem_text.startswith("- [")


@pytest.mark.asyncio
async def test_curation_prompt_does_not_contain_raw_user_text(tmp_path):
    """Privacy: ExperienceLog metadata 만 — raw user_message X."""
    _seed_log(tmp_path / "exp", n=5)
    adapter = _StubAdapter()
    cur = MemoryCurator(
        adapter=adapter,
        memory_root=tmp_path / "mem",
        experience_log_root=tmp_path / "exp",
        every_n_tasks=5,
    )
    for _ in range(5):
        await cur.maybe_curate_after_task(_StubTask())

    assert len(adapter.calls) == 1
    prompt = adapter.calls[0]["prompt"]
    # privacy keywords
    assert "metadata" in prompt
    assert "privacy" in prompt
    # 시드된 row 의 input_text_length 등은 들어가 있음
    assert "input_len=30" in prompt


@pytest.mark.asyncio
async def test_compaction_triggers_when_oversized(tmp_path):
    """MEMORY.md 가 max_chars*2 초과 시 compaction 호출 — adapter 2회 호출 +
    compact 후 MEMORY.md 가 header 로 시작."""
    _seed_log(tmp_path / "exp", n=5)
    mem_root = tmp_path / "mem"
    mem_root.mkdir(parents=True)
    # max_chars=50, threshold=100. MEMORY.md = 500자 > 100 → compact 발동
    huge = "- old memo line\n" * 50            # ~800 chars
    (mem_root / "MEMORY.md").write_text(huge, encoding="utf-8")

    adapter = _StubAdapter(response="compacted summary")
    cur = MemoryCurator(
        adapter=adapter,
        memory_root=mem_root,
        experience_log_root=tmp_path / "exp",
        every_n_tasks=5,
        max_chars=50,
    )
    for _ in range(5):
        await cur.maybe_curate_after_task(_StubTask())

    # adapter 호출은 두 번 — curation + compaction
    assert len(adapter.calls) == 2
    # 두 번째 호출 prompt 가 compaction prompt
    compact_prompt = adapter.calls[1]["prompt"]
    assert "압축" in compact_prompt
    # 압축 후 MEMORY.md 가 header 로 시작
    final = (mem_root / "MEMORY.md").read_text(encoding="utf-8")
    assert "# Hermes Agent Memory" in final
    assert "compacted summary" in final


@pytest.mark.asyncio
async def test_disabled_curator_is_noop(tmp_path):
    cur = MemoryCurator(
        adapter=_StubAdapter(),
        memory_root=tmp_path / "mem",
        experience_log_root=tmp_path / "exp",
        every_n_tasks=1,
        enabled=False,
    )
    await cur.maybe_curate_after_task(_StubTask())
    assert not (tmp_path / "mem" / "MEMORY.md").exists()


@pytest.mark.asyncio
async def test_adapter_exception_is_swallowed(tmp_path):
    """LLM 호출 실패 시 봇이 죽지 않음."""
    _seed_log(tmp_path / "exp", n=5)
    adapter = _StubAdapter(raises=RuntimeError("LLM down"))
    cur = MemoryCurator(
        adapter=adapter,
        memory_root=tmp_path / "mem",
        experience_log_root=tmp_path / "exp",
        every_n_tasks=5,
    )
    for _ in range(5):
        await cur.maybe_curate_after_task(_StubTask())  # raises 안 함
    # MEMORY.md 도 안 만들어짐
    assert not (tmp_path / "mem" / "MEMORY.md").exists()


@pytest.mark.asyncio
async def test_update_user_profile_writes_user_md(tmp_path):
    _seed_log(tmp_path / "exp", n=10)
    adapter = _StubAdapter(response="# User Profile\n\n## 자주 쓰는\n- @coder")
    cur = MemoryCurator(
        adapter=adapter,
        memory_root=tmp_path / "mem",
        experience_log_root=tmp_path / "exp",
    )
    await cur.update_user_profile(days=7)
    user_md = (tmp_path / "mem" / "USER.md").read_text(encoding="utf-8")
    assert "User Profile" in user_md
