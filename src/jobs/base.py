"""Tiny base class for growth-loop jobs (Reflection, Curator, Benchmark…).

Jobs in this package are intentionally **non-LLM**: they reduce already-
collected data (ExperienceLog, profile metadata, skill registry) into
markdown/yaml that a human can review. LLM-driven analysis belongs to
P2 (Skill auto-promotion / curator's deeper passes), not here.

Why a class instead of a free function:
  * cron scheduling lives in metadata (``schedule`` attribute) so the
    register_cron_jobs.py wiring can pick it up later
  * tests can construct a job with mocked dependencies (logger, output
    dir) without touching globals
  * subclasses share the standard ``run() -> JobResult`` contract
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field


class JobResult(BaseModel):
    """Outcome of a single job run.

    ``ok`` reflects "did the job complete its intended work?", not "did
    every observation succeed". A reflection that finds zero data still
    returns ``ok=True`` — the run succeeded, the input was just empty.
    """

    ok: bool
    summary: str
    output_path: Path | None = None
    metrics: dict[str, Any] = Field(default_factory=dict)


class BaseJob:
    """Marker base class. Subclasses set ``name`` / ``schedule`` and
    implement ``run``."""

    #: Stable identifier — used in cron registration and logs.
    name: str = ""
    #: Cron expression in KST. Empty = on-demand only (no cron).
    schedule: str = ""
    #: Human-readable description for the curator's index.
    description: str = ""

    def run(self, **kwargs: Any) -> JobResult:  # pragma: no cover - abstract
        raise NotImplementedError
