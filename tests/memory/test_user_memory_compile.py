"""USER.md compile tests (P0-B).

Targets ``MemoryCurator.compile_split_memory`` for the USER side:
priority order, lazy regenerate (no-op when source unchanged),
regenerate on source change, token budget pruning low-priority items.
"""
from __future__ import annotations

import json
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


def test_user_compile_emits_auto_generated_header(
    curator: MemoryCurator, proc_root: Path, writer: ProcessedMemoryWriter
) -> None:
    writer.write(
        type="user_preference",
        title="terse responses",
        body="user prefers short answers",
        source="claude",
        source_sha16="aa" * 8,
    )
    result = curator.compile_split_memory(processed_memory_root=proc_root)
    assert result["user_changed"] is True
    user_md = (curator.memory_root / "USER.md").read_text(encoding="utf-8")
    assert "AUTO-GENERATED" in user_md
    assert "DO NOT EDIT DIRECTLY" in user_md
    assert "terse responses" in user_md


def test_user_compile_priority_user_correction_high_first(
    curator: MemoryCurator, proc_root: Path, writer: ProcessedMemoryWriter
) -> None:
    writer.write(
        type="response_style",
        title="formality",
        body="default formal tone",
        source="claude",
        source_sha16="aa" * 8,
        confidence="medium",
    )
    writer.write(
        type="user_preference",
        title="explicit override",
        body="user explicitly said: keep it casual",
        source="user_correction",
        source_sha16="bb" * 8,
        confidence="high",
    )
    curator.compile_split_memory(processed_memory_root=proc_root)
    user_md = (curator.memory_root / "USER.md").read_text(encoding="utf-8")
    # explicit override (rank 0) appears before formality (rank 1)
    pos_override = user_md.find("explicit override")
    pos_formality = user_md.find("formality")
    assert pos_override >= 0 and pos_formality >= 0
    assert pos_override < pos_formality


def test_user_compile_lazy_regenerate_no_op(
    curator: MemoryCurator, proc_root: Path, writer: ProcessedMemoryWriter
) -> None:
    writer.write(
        type="user_preference",
        title="terse",
        body="short answers",
        source="claude",
        source_sha16="aa" * 8,
    )
    first = curator.compile_split_memory(processed_memory_root=proc_root)
    assert first["user_changed"] is True

    # Run again with no source change → no-op.
    second = curator.compile_split_memory(processed_memory_root=proc_root)
    assert second["user_changed"] is False
    assert second["user_manifest"]["compile_reason"] in (
        "first_run", "source_changed",  # whatever the prior was, the noop path
    )


def test_user_compile_regenerates_when_source_changes(
    curator: MemoryCurator, proc_root: Path, writer: ProcessedMemoryWriter
) -> None:
    writer.write(
        type="user_preference",
        title="terse",
        body="short answers",
        source="claude",
        source_sha16="aa" * 8,
    )
    curator.compile_split_memory(processed_memory_root=proc_root)
    # Add a second item — this changes the source hash.
    writer.write(
        type="response_style",
        title="lang",
        body="korean",
        source="claude",
        source_sha16="bb" * 8,
    )
    result = curator.compile_split_memory(processed_memory_root=proc_root)
    assert result["user_changed"] is True
    assert result["user_manifest"]["compile_reason"] == "source_changed"


def test_user_compile_token_budget_excludes_low_priority(
    curator: MemoryCurator, proc_root: Path, writer: ProcessedMemoryWriter
) -> None:
    # Tiny budget so the low-confidence catch-all gets pruned.
    writer.write(
        type="user_preference",
        title="primary preference",
        body="x" * 200,
        source="user_correction",
        source_sha16="aa" * 8,
        confidence="high",
    )
    writer.write(
        type="user_preference",
        title="low confidence noise",
        body="y" * 200,
        source="claude",
        source_sha16="bb" * 8,
        confidence="low",
    )
    result = curator.compile_split_memory(
        processed_memory_root=proc_root, token_budget=60
    )
    excluded = result["user_manifest"]["excluded_count"]
    assert excluded["budget"] >= 1
    user_md = (curator.memory_root / "USER.md").read_text(encoding="utf-8")
    assert "primary preference" in user_md
    assert "low confidence noise" not in user_md


def test_user_manifest_records_source_hashes(
    curator: MemoryCurator, proc_root: Path, writer: ProcessedMemoryWriter
) -> None:
    writer.write(
        type="user_preference",
        title="t",
        body="b",
        source="claude",
        source_sha16="aa" * 8,
    )
    curator.compile_split_memory(processed_memory_root=proc_root)
    manifest_path = curator.memory_root / "USER.manifest.json"
    assert manifest_path.exists()
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert "user_profile.md" in manifest["source_hashes"]
    assert "response_style.md" in manifest["source_hashes"]
