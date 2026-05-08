"""Shared fixtures for the Hermes Growing Agent Memory test suite.

The repo-root :mod:`tests.conftest` already isolates ExperienceLog and
forces ``master_enabled=false``. This conftest only adds memory-specific
helpers — namely an isolated tmp ``data/`` root for tests that exercise
manifest read/write so they don't touch the real ``data/source_manifests/``
files.
"""
from __future__ import annotations

from pathlib import Path

import pytest


@pytest.fixture
def tmp_manifest_path(tmp_path: Path) -> Path:
    """Return a not-yet-created ``claude.jsonl`` path under tmp."""
    return tmp_path / "source_manifests" / "claude.jsonl"


@pytest.fixture
def tmp_data_root(tmp_path: Path) -> Path:
    """Return a tmp ``data`` root with the P0-A subdirectories created."""
    root = tmp_path / "data"
    for sub in (
        "ingest_staging",
        "source_manifests",
        "external_memory/snapshots",
        "external_memory/sanitized",
        "external_memory/examples",
        "processed_memory",
        "memory",
    ):
        (root / sub).mkdir(parents=True, exist_ok=True)
    return root
