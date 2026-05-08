"""Cross-output split tests (P0-B).

USER.md and MEMORY.md must be compiled from disjoint source sets and
must regenerate independently — modifying ``response_style.md`` should
NOT cause MEMORY.md to be rewritten.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from src.core.memory_curator import MemoryCurator
from src.memory.ingestion.writer import ProcessedMemoryWriter


@pytest.fixture
def proc_root(tmp_path: Path) -> Path:
    p = tmp_path / "processed_memory"
    p.mkdir(parents=True, exist_ok=True)
    return p


@pytest.fixture
def memory_root(tmp_path: Path) -> Path:
    p = tmp_path / "memory"
    p.mkdir(parents=True, exist_ok=True)
    return p


@pytest.fixture
def curator(memory_root: Path, tmp_path: Path) -> MemoryCurator:
    return MemoryCurator(
        adapter=None,
        memory_root=memory_root,
        experience_log_root=tmp_path / "experience",
    )


@pytest.fixture
def writer(proc_root: Path) -> ProcessedMemoryWriter:
    return ProcessedMemoryWriter(proc_root)


def test_split_compile_writes_two_distinct_files(
    curator: MemoryCurator, proc_root: Path, writer: ProcessedMemoryWriter
) -> None:
    writer.write(
        type="user_preference",
        title="prefers korean",
        body="user_pref body",
        source="claude",
        source_sha16="aa" * 8,
    )
    writer.write(
        type="failure_pattern",
        title="acceptEdits hardcoded",
        body="repeats this mistake",
        source="claude",
        source_sha16="bb" * 8,
        confidence="high",
    )
    result = curator.compile_split_memory(processed_memory_root=proc_root)
    assert result["user_changed"] and result["memory_changed"]

    user_md = (curator.memory_root / "USER.md").read_text(encoding="utf-8")
    memory_md = (curator.memory_root / "MEMORY.md").read_text(encoding="utf-8")

    # Each artifact only references items from its own source set.
    assert "prefers korean" in user_md
    assert "prefers korean" not in memory_md
    assert "acceptEdits hardcoded" in memory_md
    assert "acceptEdits hardcoded" not in user_md


def test_split_compile_memory_no_op_when_only_user_changes(
    curator: MemoryCurator, proc_root: Path, writer: ProcessedMemoryWriter
) -> None:
    writer.write(
        type="user_preference",
        title="initial",
        body="b",
        source="claude",
        source_sha16="aa" * 8,
    )
    writer.write(
        type="decision",
        title="initial decision",
        body="d",
        source="claude",
        source_sha16="cc" * 8,
    )
    curator.compile_split_memory(processed_memory_root=proc_root)
    # Now mutate ONLY the user side.
    writer.write(
        type="response_style",
        title="newly added style",
        body="korean",
        source="claude",
        source_sha16="bb" * 8,
    )
    result = curator.compile_split_memory(processed_memory_root=proc_root)
    assert result["user_changed"] is True
    # MEMORY side sources unchanged → no rewrite.
    assert result["memory_changed"] is False


def test_split_compile_user_no_op_when_only_memory_changes(
    curator: MemoryCurator, proc_root: Path, writer: ProcessedMemoryWriter
) -> None:
    writer.write(
        type="response_style",
        title="initial style",
        body="korean",
        source="claude",
        source_sha16="aa" * 8,
    )
    writer.write(
        type="decision",
        title="initial decision",
        body="d",
        source="claude",
        source_sha16="cc" * 8,
    )
    curator.compile_split_memory(processed_memory_root=proc_root)
    # Mutate only MEMORY side.
    writer.write(
        type="prompt_template",
        title="new prompt",
        body="prompt body",
        source="claude",
        source_sha16="dd" * 8,
    )
    result = curator.compile_split_memory(processed_memory_root=proc_root)
    assert result["memory_changed"] is True
    assert result["user_changed"] is False
