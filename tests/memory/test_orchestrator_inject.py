"""Tests for src.memory.injection MemoryInjectionService (P2).

Note: hermes_master.py is intentionally untouched in this commit. The
service is exercised directly so the live orchestrator request path
stays unchanged. A follow-up PR will collapse
HermesMasterOrchestrator._maybe_inject_memory into a thin wrapper
around MemoryInjectionService.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from src.core.memory_curator import MemoryCurator
from src.memory.injection import MemoryInjectionService
from src.memory.ingestion.writer import ProcessedMemoryWriter


@pytest.fixture
def populated_roots(tmp_path: Path) -> tuple[Path, Path, MemoryCurator]:
    proc = tmp_path / "processed_memory"
    mem = tmp_path / "memory"
    proc.mkdir()
    mem.mkdir()
    w = ProcessedMemoryWriter(proc)
    w.write(
        type="user_preference",
        title="terse responses",
        body="user prefers short replies",
        source="user_correction",
        source_sha16="aa" * 8,
        confidence="high",
    )
    w.write(
        type="failure_pattern",
        title="acceptEdits hardcoded confusion",
        body="recurring confusion around acceptEdits permission mode",
        source="claude",
        source_sha16="bb" * 8,
        confidence="medium",
    )
    curator = MemoryCurator(
        adapter=None,
        memory_root=mem,
        experience_log_root=tmp_path / "experience",
    )
    curator.compile_split_memory(processed_memory_root=proc)
    return proc, mem, curator


def test_compose_uses_compiled_user_and_memory(
    populated_roots: tuple[Path, Path, MemoryCurator]
) -> None:
    proc, mem, _ = populated_roots
    svc = MemoryInjectionService(
        compiled_memory_root=mem,
        processed_memory_root=proc,
    )
    result = svc.compose(query="anything", ab_arm="control")
    assert result.used_compiled_user is True
    assert result.used_compiled_memory is True
    assert "USER (compiled)" in result.text
    assert "MEMORY (compiled)" in result.text
    assert "terse responses" in result.text
    assert "acceptEdits hardcoded confusion" in result.text


def test_compose_skips_retrieval_when_disabled(
    populated_roots: tuple[Path, Path, MemoryCurator]
) -> None:
    proc, mem, _ = populated_roots
    svc = MemoryInjectionService(
        compiled_memory_root=mem,
        processed_memory_root=proc,
        retrieval_enabled=False,
    )
    result = svc.compose(query="acceptEdits permission", ab_arm="treatment")
    # retrieval gated off → 0 hits, but compiled context still present
    assert result.retrieval_hits == 0
    assert result.used_compiled_user and result.used_compiled_memory


def test_compose_uses_retrieval_when_enabled_and_treatment(
    populated_roots: tuple[Path, Path, MemoryCurator]
) -> None:
    proc, mem, _ = populated_roots
    svc = MemoryInjectionService(
        compiled_memory_root=mem,
        processed_memory_root=proc,
        retrieval_enabled=True,
    )
    result = svc.compose(query="acceptEdits permission mode", ab_arm="treatment")
    assert result.retrieval_hits >= 1
    assert "relevant:" in result.text


def test_compose_token_budget_drops_low_priority(
    populated_roots: tuple[Path, Path, MemoryCurator]
) -> None:
    proc, mem, _ = populated_roots
    svc = MemoryInjectionService(
        compiled_memory_root=mem,
        processed_memory_root=proc,
        retrieval_enabled=True,
        token_budget=20,  # very tight
    )
    result = svc.compose(query="acceptEdits", ab_arm="treatment")
    # At least one item dropped due to budget.
    assert result.over_budget_dropped >= 1


def test_compose_empty_when_no_compiled_files(tmp_path: Path) -> None:
    svc = MemoryInjectionService(
        compiled_memory_root=tmp_path / "memory",
        processed_memory_root=tmp_path / "processed_memory",
    )
    result = svc.compose(query="anything", ab_arm="control")
    assert result.text == ""
    assert result.used_compiled_user is False
    assert result.used_compiled_memory is False
