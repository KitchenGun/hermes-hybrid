"""ScoreMatrix — empirical (job_type × model) → ScoreStats.

This is the **single source of truth** for routing decisions in the
Job Factory v2 design. No prior matrix — every routing choice is driven
by Welford-accumulated validator scores from real production traffic
(plus offline benchmark seeds).

Design notes:
  * Welford's online algorithm gives numerically stable mean + variance
    in a single pass with O(1) memory per cell. See Knuth TAOCP Vol 2
    §4.2.2 or https://en.wikipedia.org/wiki/Algorithms_for_calculating_variance.
  * asyncio.Lock around mutations because multiple Discord messages can
    update the same cell concurrently (per-user lock isn't enough — a
    single user can also have a background bench writing same cell).
  * Atomic persist via tmp-file + rename, so a crash mid-write never
    leaves a half-written JSON. On load failure, return empty matrix
    (cold-start auto-recovery).
  * Cell key is the tuple (job_type, model) but JSON serializes them as
    "{job_type}::{model}" strings so the file is human-readable.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import tempfile
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

CELL_KEY_SEPARATOR = "::"
SCHEMA_VERSION = 1

# Number of writes that trigger a flush to disk. Lower = safer (less data
# loss on crash) but more I/O. 50 is a reasonable balance for ~1 message/s.
DEFAULT_FLUSH_THRESHOLD = 50


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _iso(dt: datetime | None) -> str | None:
    return dt.isoformat() if dt else None


def _parse_iso(s: str | None) -> datetime | None:
    return datetime.fromisoformat(s) if s else None


@dataclass
class ScoreStats:
    """Welford-accumulated stats for a single (job_type, model) cell.

    Attributes:
        n: Number of samples observed.
        mean: Running mean (0–100 score scale).
        m2: Sum of squared deviations from the running mean.
            ``variance = m2 / n`` (population variance — sufficient for
            ranking; sample variance would divide by n-1 but for n>>1
            the difference is negligible and we always have n>=1 when
            asked).
        last_updated: UTC timestamp of the most recent update.

    The ``update`` method implements Welford's online algorithm. After
    K calls with values x_1..x_K, ``mean`` and ``m2`` exactly match what
    you'd get from a one-shot computation over the same values, free of
    catastrophic cancellation.
    """

    n: int = 0
    mean: float = 0.0
    m2: float = 0.0
    last_updated: datetime | None = None

    def update(self, value: float) -> None:
        """Welford online update — O(1), numerically stable."""
        self.n += 1
        delta = value - self.mean
        self.mean += delta / self.n
        delta2 = value - self.mean
        self.m2 += delta * delta2
        self.last_updated = _utcnow()

    @property
    def variance(self) -> float:
        """Population variance. Returns 0.0 for n<=1 (no spread possible)."""
        if self.n <= 1:
            return 0.0
        return self.m2 / self.n

    @property
    def stddev(self) -> float:
        """Standard deviation (sqrt of variance)."""
        return self.variance**0.5

    def to_dict(self) -> dict[str, Any]:
        return {
            "n": self.n,
            "mean": self.mean,
            "m2": self.m2,
            "last_updated": _iso(self.last_updated),
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "ScoreStats":
        return cls(
            n=int(d.get("n", 0)),
            mean=float(d.get("mean", 0.0)),
            m2=float(d.get("m2", 0.0)),
            last_updated=_parse_iso(d.get("last_updated")),
        )


@dataclass
class ScoreMatrix:
    """Persisted (job_type, model) → ScoreStats table.

    Use :meth:`load` / :meth:`update` / :meth:`persist`.
    Concurrency: all mutating methods take ``self._lock`` so concurrent
    Discord handlers + background bench tasks don't corrupt cells.
    Persistence: atomic — writes to ``{path}.tmp`` then renames.
    """

    path: Path
    cells: dict[tuple[str, str], ScoreStats] = field(default_factory=dict)
    flush_threshold: int = DEFAULT_FLUSH_THRESHOLD
    _dirty: int = 0
    _lock: asyncio.Lock = field(default_factory=asyncio.Lock, repr=False)

    # ---- access ----

    def get(self, job_type: str, model: str) -> ScoreStats:
        """Read-only fetch. Returns a fresh empty ScoreStats if cell absent.

        Note: returns the live object if present — callers must not mutate.
        For mutation use :meth:`update`.
        """
        return self.cells.get((job_type, model)) or ScoreStats()

    def has(self, job_type: str, model: str) -> bool:
        return (job_type, model) in self.cells

    def models_for(self, job_type: str) -> list[str]:
        """All models with at least one observation for this job_type."""
        return [m for (j, m) in self.cells if j == job_type]

    def all_models(self) -> set[str]:
        """All models seen anywhere in the matrix."""
        return {m for (_, m) in self.cells}

    def all_job_types(self) -> set[str]:
        """All job_types seen anywhere in the matrix."""
        return {j for (j, _) in self.cells}

    # ---- mutation (async — concurrent-safe) ----

    async def update(
        self, job_type: str, model: str, score: float
    ) -> ScoreStats:
        """Append one observation. Auto-flushes after ``flush_threshold`` writes.

        Returns the updated ScoreStats (defensive copy not made — for
        observability only, do not mutate).
        """
        if not 0.0 <= score <= 100.0:
            raise ValueError(f"score must be in [0, 100], got {score}")
        async with self._lock:
            cell = self.cells.setdefault((job_type, model), ScoreStats())
            cell.update(score)
            self._dirty += 1
            if self._dirty >= self.flush_threshold:
                self._dirty = 0
                self._persist_unlocked()
            return cell

    async def persist(self) -> None:
        """Force-write to disk (e.g., on shutdown)."""
        async with self._lock:
            self._dirty = 0
            self._persist_unlocked()

    # ---- persistence (sync; called inside lock) ----

    def _persist_unlocked(self) -> None:
        """Atomic JSON write. Caller must hold self._lock."""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "version": SCHEMA_VERSION,
            "updated_at": _iso(_utcnow()),
            "cells": {
                f"{j}{CELL_KEY_SEPARATOR}{m}": cell.to_dict()
                for (j, m), cell in self.cells.items()
            },
        }
        # tempfile.mkstemp guarantees uniqueness even with concurrent writers.
        # We then os.replace which is atomic on the same filesystem (POSIX
        # and Win32 ReplaceFileW). Crashes leave either old or new — never
        # half-written.
        fd, tmp_path = tempfile.mkstemp(
            prefix=self.path.name + ".",
            suffix=".tmp",
            dir=str(self.path.parent),
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(payload, f, ensure_ascii=False, indent=2)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp_path, self.path)
        except Exception:
            # Best-effort cleanup of the tmp file; don't mask the original.
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
            raise

    @classmethod
    def load(
        cls,
        path: Path,
        flush_threshold: int = DEFAULT_FLUSH_THRESHOLD,
    ) -> "ScoreMatrix":
        """Load from disk; on any failure return an empty matrix.

        Cold-start auto-recovery: a corrupt or missing file is not an
        error — the bandit selector will fall back to round-robin until
        cells fill up. We log a warning so operators notice.
        """
        if not path.exists():
            log.info("score_matrix.load.missing", extra={"path": str(path)})
            return cls(path=path, flush_threshold=flush_threshold)

        try:
            with path.open("r", encoding="utf-8") as f:
                payload = json.load(f)
        except (OSError, json.JSONDecodeError) as e:
            log.warning(
                "score_matrix.load.failed",
                extra={"path": str(path), "err": str(e)},
            )
            return cls(path=path, flush_threshold=flush_threshold)

        version = payload.get("version", 0)
        if version != SCHEMA_VERSION:
            log.warning(
                "score_matrix.load.version_mismatch",
                extra={"path": str(path), "got": version, "want": SCHEMA_VERSION},
            )
            # Version mismatch → start clean rather than risk silent corruption.
            return cls(path=path, flush_threshold=flush_threshold)

        cells: dict[tuple[str, str], ScoreStats] = {}
        for raw_key, raw_stats in payload.get("cells", {}).items():
            if CELL_KEY_SEPARATOR not in raw_key:
                log.warning(
                    "score_matrix.load.bad_key",
                    extra={"key": raw_key},
                )
                continue
            job_type, model = raw_key.split(CELL_KEY_SEPARATOR, 1)
            try:
                cells[(job_type, model)] = ScoreStats.from_dict(raw_stats)
            except (TypeError, ValueError) as e:
                log.warning(
                    "score_matrix.load.bad_cell",
                    extra={"key": raw_key, "err": str(e)},
                )
                continue

        log.info(
            "score_matrix.load.ok",
            extra={"path": str(path), "cells": len(cells)},
        )
        return cls(path=path, cells=cells, flush_threshold=flush_threshold)
