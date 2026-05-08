"""End-to-end tests for scripts.import_claude_memory (P1).

The script does three things: snapshot, extract, and write. Tests
exercise the in-process pipeline (ClaudeSource → RuleExtractor →
ProcessedMemoryWriter + ManifestStore) so we cover the script's
behaviour without spawning a subprocess. The CLI parser is exercised
once via runpy-style invocation to catch arg-parsing regressions.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from src.memory.ingestion.extractor import RuleExtractor
from src.memory.ingestion.manifest import ManifestStore
from src.memory.ingestion.sources import ClaudeSource
from src.memory.ingestion.writer import ProcessedMemoryWriter, parse_processed_file


@pytest.fixture
def fake_claude_root(tmp_path: Path) -> Path:
    root = tmp_path / "claude_memory"
    root.mkdir()
    (root / "MEMORY.md").write_text(
        "# Failure pattern: acceptEdits hardcoded confusion\n"
        "Repeated misuse of acceptEdits.\n",
        encoding="utf-8",
    )
    (root / "user_role.md").write_text(
        "Preferred: short Korean answers\n"
        "Style: terse, no summary\n",
        encoding="utf-8",
    )
    return root


def test_in_process_pipeline_writes_processed_memory(
    fake_claude_root: Path, tmp_path: Path
) -> None:
    proc = tmp_path / "processed_memory"
    src = ClaudeSource(fake_claude_root)
    extractor = RuleExtractor()
    writer = ProcessedMemoryWriter(proc)

    candidates = []
    items = list(src.iter_items())
    assert len(items) == 2
    for it in items:
        candidates.extend(extractor.extract(it))
    assert candidates

    for c in candidates:
        writer.write(
            type=c.type,
            title=c.title,
            body=c.body,
            source=c.source,
            source_sha16=c.source_sha16,
            confidence=c.confidence,
            tags=c.tags,
        )

    fp_md = proc / "failure_patterns.md"
    rs_md = proc / "response_style.md"
    up_md = proc / "user_profile.md"
    assert fp_md.exists()
    assert rs_md.exists() or up_md.exists()  # either inline pattern hits

    items_in_fp = parse_processed_file(fp_md.read_text(encoding="utf-8"))
    assert any("acceptEdits" in i.title for i in items_in_fp)


def test_manifest_records_processed_files(
    fake_claude_root: Path, tmp_path: Path
) -> None:
    manifest_path = tmp_path / "source_manifests" / "claude.jsonl"
    store = ManifestStore(manifest_path)
    store.ensure_schema_header()
    src = ClaudeSource(fake_claude_root)
    for it in src.iter_items():
        store.append(
            payload=it.content,
            source="claude",
            source_path=it.source_path,
        )
    entries = list(store.iter_manifests())
    assert len(entries) == 2
    assert all(e.source == "claude" for e in entries)
    assert all(e.retention == "manifest_only" for e in entries)


def test_dry_run_does_not_write(
    fake_claude_root: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Invoke main(['--root', ...]) without --apply: nothing should be written."""
    from scripts import import_claude_memory as mod

    proc = tmp_path / "processed_memory"
    snap = tmp_path / "snapshots"
    manifests = tmp_path / "source_manifests"
    rc = mod.main([
        "--root", str(fake_claude_root),
        "--processed-root", str(proc),
        "--snapshot-root", str(snap),
        "--manifest-root", str(manifests),
    ])
    assert rc == 0
    # No write side effects in dry-run.
    assert not proc.exists() or not any(proc.iterdir())
    assert not snap.exists() or not any(snap.iterdir())
    assert not manifests.exists() or not any(manifests.iterdir())
