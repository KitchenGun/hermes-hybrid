"""Tests for src.memory.ingestion.manifest (P0-A).

Coverage: sha16/content_sha256 derivation; schema header round-trip;
record_type discriminator; sha16 dedup; malformed line skip + report;
source/retention whitelist validation.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from src.core.experience_logger import _sha16 as core_sha16
from src.memory.ingestion.manifest import (
    ALLOWED_RETENTIONS,
    ALLOWED_SOURCES,
    SCHEMA_VERSION,
    ManifestEntry,
    ManifestStore,
    content_sha256,
    sha16,
    validate_retention,
    validate_source,
)


def test_sha16_matches_experience_logger_sha16() -> None:
    """sha16() must match _sha16() so manifest and ExperienceLog cross-ref."""
    payload = "hello memory"
    assert sha16(payload) == core_sha16(payload)
    # Empty maps to "" in both.
    assert sha16("") == core_sha16("") == ""


def test_sha16_handles_bytes_and_unicode() -> None:
    assert sha16("abc") == sha16(b"abc")
    # 비-ASCII (UTF-8 with replace) — 그래도 deterministic.
    assert sha16("한국어") == sha16("한국어".encode())


def test_content_sha256_full_length_and_empty() -> None:
    full = content_sha256("abc")
    assert len(full) == 64
    # sha16 should be the prefix.
    assert full.startswith(sha16("abc"))
    assert content_sha256("") == ""


def test_validate_source_accepts_whitelist() -> None:
    for s in ALLOWED_SOURCES:
        validate_source(s)
    with pytest.raises(ValueError):
        validate_source("slack")
    with pytest.raises(ValueError):
        validate_source("")


def test_validate_retention_whitelist() -> None:
    for r in ALLOWED_RETENTIONS:
        validate_retention(r)
    with pytest.raises(ValueError):
        validate_retention("permanent")


def test_ensure_schema_header_creates_file(tmp_manifest_path: Path) -> None:
    store = ManifestStore(tmp_manifest_path)
    assert not tmp_manifest_path.exists()
    assert store.ensure_schema_header() is True
    assert tmp_manifest_path.exists()
    first = tmp_manifest_path.read_text(encoding="utf-8").splitlines()[0]
    rec = json.loads(first)
    assert rec == {"record_type": "schema", "schema_version": SCHEMA_VERSION}
    # Calling again is a no-op (already present).
    assert store.ensure_schema_header() is False


def test_iter_manifests_skips_schema_and_yields_entries(
    tmp_manifest_path: Path,
) -> None:
    store = ManifestStore(tmp_manifest_path)
    e1 = store.append(
        payload="first",
        source="claude",
        source_path="~/x.md",
    )
    e2 = store.append(
        payload="second",
        source="chatgpt",
        source_path="~/y.json",
    )
    assert e1 is not None and e2 is not None
    entries = list(store.iter_manifests())
    assert [entry.sha16 for entry in entries] == [e1.sha16, e2.sha16]
    # Schema header was skipped, malformed list is empty.
    assert store.malformed_lines() == []


def test_append_dedup_returns_none_for_known_sha16(
    tmp_manifest_path: Path,
) -> None:
    store = ManifestStore(tmp_manifest_path)
    first = store.append(
        payload="hello",
        source="claude",
        source_path="~/a.md",
    )
    assert first is not None
    second = store.append(
        payload="hello",
        source="claude",
        source_path="~/b.md",  # different source_path, same sha16
    )
    assert second is None
    # File still has only one manifest record.
    entries = list(store.iter_manifests())
    assert len(entries) == 1
    assert entries[0].sha16 == first.sha16


def test_iter_manifests_skips_malformed_and_unknown_record_type(
    tmp_manifest_path: Path,
) -> None:
    store = ManifestStore(tmp_manifest_path)
    valid = store.append(
        payload="ok",
        source="discord",
        source_path="#general/123",
    )
    assert valid is not None
    # Inject a malformed JSON line + an unknown record_type line.
    with tmp_manifest_path.open("a", encoding="utf-8") as fh:
        fh.write("{not json\n")
        fh.write(json.dumps({"record_type": "garbage", "x": 1}) + "\n")
    entries = list(store.iter_manifests())
    assert len(entries) == 1
    malformed = store.malformed_lines()
    assert len(malformed) == 2
    reasons = [reason for _lineno, _raw, reason in malformed]
    assert any("unknown record_type" in r for r in reasons)


def test_append_rejects_empty_payload(tmp_manifest_path: Path) -> None:
    store = ManifestStore(tmp_manifest_path)
    with pytest.raises(ValueError):
        store.append(payload="", source="claude", source_path="~/x")


def test_manifest_entry_round_trip() -> None:
    entry = ManifestEntry(
        sha16="ab" * 8,
        content_sha256="cd" * 32,
        source="claude",
        source_path="~/z.md",
        imported_at="2026-05-09T00:00:00+00:00",
        derived_items=("response_style:abababababab:hello",),
    )
    rec = entry.to_record()
    assert rec["record_type"] == "manifest"
    assert rec["derived_items"] == ["response_style:abababababab:hello"]
    back = ManifestEntry.from_record(rec)
    assert back == entry


def test_manifest_entry_from_record_rejects_wrong_type() -> None:
    with pytest.raises(ValueError):
        ManifestEntry.from_record({"record_type": "schema", "schema_version": 1})
