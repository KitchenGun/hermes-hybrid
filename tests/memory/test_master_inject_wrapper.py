"""Tests for HermesMasterOrchestrator._maybe_inject_memory wrapper (P2 follow-up).

The wrapper does two additive things:
1. Run the legacy Phase-21 sqlite memory.search path (unchanged).
2. Optionally append a P2 retrieval supplement when
   memory_retrieval_enabled=True.

Compiled USER.md / MEMORY.md is not re-injected in the wrapper because
_compose_prompt already prepends them via
MemoryCurator.read_prompt_prepend. The last test in this module
verifies that path stays functional after P0-B's
compile_split_memory writes the artifacts.
"""
from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from src.config import Settings
from src.core.memory_curator import MemoryCurator
from src.memory.base import Memo
from src.memory.ingestion.writer import ProcessedMemoryWriter
from src.orchestrator.hermes_master import HermesMasterOrchestrator


# ---------------------------------------------------------------------------
# Stubs — directly bind the unbound method onto a tiny stub class so we
# don't have to construct the full orchestrator (heavy dependency tree).
# ---------------------------------------------------------------------------
class _StubMemory:
    def __init__(self, results: list[Memo] | None = None) -> None:
        self.results = list(results or [])
        self.calls: list[tuple] = []

    async def search(self, user_id: str, query: str, k: int = 3):
        self.calls.append((user_id, query, k))
        return list(self.results)


class _StubOrch:
    """Just enough surface to call ``_maybe_inject_memory``."""
    _maybe_inject_memory = HermesMasterOrchestrator._maybe_inject_memory

    def __init__(self, settings: Settings, memory: _StubMemory) -> None:
        self.settings = settings
        self.memory = memory


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------
def _settings(
    tmp_path: Path,
    *,
    inject_enabled: bool = True,
    retrieval_enabled: bool = False,
) -> Settings:
    return Settings(
        _env_file=None,  # type: ignore[call-arg]
        memory_inject_enabled=inject_enabled,
        memory_retrieval_enabled=retrieval_enabled,
        compiled_memory_root=tmp_path / "memory",
        processed_memory_root=tmp_path / "processed_memory",
        ingest_staging_root=tmp_path / "ingest_staging",
        source_manifest_root=tmp_path / "source_manifests",
        external_memory_root=tmp_path / "external_memory",
        memory_audit_root=tmp_path / "memory_audit",
        memory_inject_token_budget=2000,
        memory_retriever_k=5,
        # baseline isolation
        master_enabled=False,
        experience_log_enabled=False,
        experience_log_root=tmp_path / "experience",
    )


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------
def test_memory_inject_disabled_short_circuits_everything(
    tmp_path: Path,
) -> None:
    s = _settings(tmp_path, inject_enabled=False, retrieval_enabled=True)
    mem = _StubMemory(results=[Memo(user_id="u", text="should not appear")])
    orch = _StubOrch(s, mem)
    history: list[dict[str, str]] = []
    count = asyncio.run(orch._maybe_inject_memory("u", "hello", history))
    assert count == 0
    assert history == []
    # Legacy memory.search must NOT be called when injection is disabled —
    # the early return is what preserves the Phase-21 control-arm semantics.
    assert mem.calls == []


def test_legacy_layer_runs_when_inject_enabled(tmp_path: Path) -> None:
    s = _settings(tmp_path, inject_enabled=True, retrieval_enabled=False)
    mem = _StubMemory(
        results=[Memo(user_id="u", text="legacy hit body")]
    )
    orch = _StubOrch(s, mem)
    history: list[dict[str, str]] = []
    count = asyncio.run(orch._maybe_inject_memory("u", "hello", history))
    assert count == 1
    assert len(history) == 1
    assert history[0]["role"] == "system"
    assert "legacy hit body" in history[0]["content"]
    # Legacy memory.search hit once.
    assert len(mem.calls) == 1


def test_retrieval_layer_skipped_when_flag_false(tmp_path: Path) -> None:
    """memory_retrieval_enabled=False → retriever NOT consulted even if
    processed_memory has matches."""
    proc = tmp_path / "processed_memory"
    w = ProcessedMemoryWriter(proc)
    w.write(
        type="failure_pattern",
        title="acceptEdits hardcoded",
        body="recurring acceptEdits permission issue",
        source="claude",
        source_sha16="aa" * 8,
        confidence="medium",
    )
    s = _settings(tmp_path, inject_enabled=True, retrieval_enabled=False)
    orch = _StubOrch(s, _StubMemory())
    history: list[dict[str, str]] = []
    count = asyncio.run(orch._maybe_inject_memory("u", "acceptEdits permission", history))
    # Legacy returned 0 hits, retrieval is gated off → no inserts.
    assert count == 0
    assert history == []


def test_retrieval_layer_runs_when_flag_true(tmp_path: Path) -> None:
    proc = tmp_path / "processed_memory"
    w = ProcessedMemoryWriter(proc)
    w.write(
        type="failure_pattern",
        title="acceptEdits hardcoded",
        body="recurring acceptEdits permission issue with hardcoded mode",
        source="claude",
        source_sha16="aa" * 8,
        confidence="medium",
    )
    s = _settings(tmp_path, inject_enabled=True, retrieval_enabled=True)
    orch = _StubOrch(s, _StubMemory())
    history: list[dict[str, str]] = []
    count = asyncio.run(orch._maybe_inject_memory(
        "u", "acceptEdits hardcoded permission help", history,
    ))
    assert count >= 1
    assert any("acceptEdits hardcoded" in h["content"] for h in history)


def test_retrieval_does_not_double_inject_compiled_artifacts(tmp_path: Path) -> None:
    """Wrapper passes include_compiled=False so compiled USER.md / MEMORY.md
    text is NOT in the retrieval supplement (they're prepended elsewhere)."""
    mem_root = tmp_path / "memory"
    proc = tmp_path / "processed_memory"
    proc.mkdir()
    mem_root.mkdir()
    w = ProcessedMemoryWriter(proc)
    w.write(
        type="user_preference",
        title="prefers terse",
        body="user wants short responses always",
        source="user_correction",
        source_sha16="aa" * 8,
        confidence="high",
    )
    w.write(
        type="failure_pattern",
        title="acceptEdits hardcoded",
        body="recurring acceptEdits permission issue with hardcoded mode",
        source="claude",
        source_sha16="bb" * 8,
        confidence="medium",
    )
    # Compile USER.md / MEMORY.md so they would be re-pickable.
    curator = MemoryCurator(
        adapter=None,
        memory_root=mem_root,
        experience_log_root=tmp_path / "experience",
    )
    curator.compile_split_memory(processed_memory_root=proc)
    assert (mem_root / "USER.md").exists()

    s = _settings(tmp_path, inject_enabled=True, retrieval_enabled=True)
    orch = _StubOrch(s, _StubMemory())
    history: list[dict[str, str]] = []
    asyncio.run(orch._maybe_inject_memory(
        "u", "acceptEdits hardcoded permission help", history,
    ))
    combined = "\n".join(h["content"] for h in history)
    # Retrieval surface is "### relevant: ..." — no "## USER (compiled)" or
    # "## MEMORY (compiled)" blocks should appear.
    assert "USER (compiled)" not in combined
    assert "MEMORY (compiled)" not in combined
    # But the matched failure_pattern content should still surface.
    assert "acceptEdits hardcoded" in combined


def test_compiled_user_md_surfaces_via_compose_prompt_path(tmp_path: Path) -> None:
    """The compiled USER.md is reachable through MemoryCurator.read_prompt_prepend,
    which is the path _compose_prompt uses. The wrapper does not duplicate
    this — verifying the existing prepend keeps working after P0-B compile."""
    mem_root = tmp_path / "memory"
    proc = tmp_path / "processed_memory"
    proc.mkdir()
    mem_root.mkdir()

    w = ProcessedMemoryWriter(proc)
    w.write(
        type="user_preference",
        title="prefers terse",
        body="user wants short replies",
        source="user_correction",
        source_sha16="aa" * 8,
        confidence="high",
    )
    curator = MemoryCurator(
        adapter=None,
        memory_root=mem_root,
        experience_log_root=tmp_path / "experience",
    )
    curator.compile_split_memory(processed_memory_root=proc)
    text = curator.read_prompt_prepend()
    assert text.strip()  # non-empty
    assert "prefers terse" in text or "prefers" in text or "USER" in text
