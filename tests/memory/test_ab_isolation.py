"""A/B isolation tests (P2).

The retrieval enrichment must NOT perturb the legacy Phase-21
``memory_inject`` experiment. Rules:

1. Compiled USER.md / MEMORY.md is shown to every arm — that path is
   the legacy experiment's signal and we never want it to vary
   between control and treatment of the *new* retrieval experiment.
2. Retrieval enrichment is gated by:
     - ``memory_retrieval_enabled`` (config flag, default False), AND
     - the caller passing ``ab_arm="treatment"`` (the new
       ``memory_retrieval_v1`` experiment's treatment arm).
3. Disabling either gate produces identical compiled-only output for
   the same query.

These tests don't run the actual experiment_runner — they verify the
service-level contract that downstream A/B plumbing relies on.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from src.config import Settings
from src.core.memory_curator import MemoryCurator
from src.memory.injection import MemoryInjectionService
from src.memory.ingestion.writer import ProcessedMemoryWriter


@pytest.fixture
def populated(tmp_path: Path) -> tuple[Path, Path, MemoryCurator]:
    proc = tmp_path / "processed_memory"
    mem = tmp_path / "memory"
    proc.mkdir()
    mem.mkdir()
    w = ProcessedMemoryWriter(proc)
    w.write(
        type="user_preference",
        title="korean responses",
        body="user prefers Korean",
        source="user_correction",
        source_sha16="aa" * 8,
        confidence="high",
    )
    w.write(
        type="failure_pattern",
        title="acceptEdits hardcoded",
        body="recurring acceptEdits permission mode confusion",
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


def test_default_settings_keep_retrieval_off() -> None:
    s = Settings(_env_file=None)
    assert s.memory_inject_enabled is True  # legacy experiment preserved
    assert s.memory_retrieval_enabled is False
    assert s.memory_retrieval_ab_key == "memory_retrieval_v1"


def test_control_arm_does_not_run_retrieval(
    populated: tuple[Path, Path, MemoryCurator]
) -> None:
    proc, mem, _ = populated
    svc = MemoryInjectionService(
        compiled_memory_root=mem,
        processed_memory_root=proc,
        retrieval_enabled=True,  # flag ON
    )
    result = svc.compose(query="acceptEdits", ab_arm="control")
    # Treatment arm gate not reached → 0 retrieval hits.
    assert result.retrieval_hits == 0
    assert result.used_compiled_user and result.used_compiled_memory


def test_treatment_arm_with_flag_off_does_not_run_retrieval(
    populated: tuple[Path, Path, MemoryCurator]
) -> None:
    proc, mem, _ = populated
    svc = MemoryInjectionService(
        compiled_memory_root=mem,
        processed_memory_root=proc,
        retrieval_enabled=False,  # flag OFF
    )
    result = svc.compose(query="acceptEdits", ab_arm="treatment")
    assert result.retrieval_hits == 0


def test_compiled_context_identical_across_arms(
    populated: tuple[Path, Path, MemoryCurator]
) -> None:
    proc, mem, _ = populated
    svc_off = MemoryInjectionService(
        compiled_memory_root=mem,
        processed_memory_root=proc,
        retrieval_enabled=False,
    )
    a = svc_off.compose(query="x", ab_arm="control")
    b = svc_off.compose(query="x", ab_arm="treatment")
    # Without retrieval, the two arms must produce identical output —
    # otherwise the new path is leaking into the legacy experiment's
    # control variance.
    assert a.text == b.text


def test_retrieval_only_shows_for_treatment_with_flag_on(
    populated: tuple[Path, Path, MemoryCurator]
) -> None:
    proc, mem, _ = populated
    svc = MemoryInjectionService(
        compiled_memory_root=mem,
        processed_memory_root=proc,
        retrieval_enabled=True,
    )
    control = svc.compose(query="acceptEdits permission", ab_arm="control")
    treatment = svc.compose(query="acceptEdits permission", ab_arm="treatment")
    assert control.retrieval_hits == 0
    assert treatment.retrieval_hits >= 1
