"""Source manifest store (sha16-only, raw text never persisted).

Each conversation source (Claude / ChatGPT / Discord) gets its own jsonl
file under ``data/source_manifests/``. Lines come in two flavours:

- ``{"record_type": "schema", "schema_version": N}`` — first line, written
  on file creation. Future migrations branch on ``schema_version``.
- ``{"record_type": "manifest", ...}`` — one per ingested artifact.

We never store raw conversation text here. The manifest carries:

- ``sha16`` — first 16 hex chars of SHA-256 of the raw payload (matches
  :func:`src.core.experience_logger._sha16`).
- ``content_sha256`` — full hash, kept for future provenance checks.
- ``source`` — provider tag, validated against :data:`ALLOWED_SOURCES`.
- ``retention`` — currently always ``"manifest_only"``.
- ``derived_items`` — list of ``item_id`` strings the writer (P0-B) created
  from this artifact.

Rationale: raw conversation text is only kept in ``data/ingest_staging/``
(gitignored) and deleted after ingest. Audit trails rely on these
manifests plus ``data/processed_memory/*.md`` meta blocks.
"""
from __future__ import annotations

import hashlib
import json
import logging
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable, Iterator, Literal

# Avoid a hard import cycle with src.core; re-derive sha16 with the same
# semantics as :func:`src.core.experience_logger._sha16`.
_log = logging.getLogger(__name__)

SCHEMA_VERSION = 1

ALLOWED_SOURCES: frozenset[str] = frozenset({"claude", "chatgpt", "discord"})
ALLOWED_RETENTIONS: frozenset[str] = frozenset({"manifest_only"})
RecordType = Literal["schema", "manifest"]


def sha16(payload: str | bytes) -> str:
    """Return the first 16 hex chars of SHA-256(payload).

    Mirrors :func:`src.core.experience_logger._sha16` so manifest sha16 and
    ExperienceLog sha16 match for the same input. Empty input → ``""``.
    """
    if not payload:
        return ""
    if isinstance(payload, str):
        data = payload.encode("utf-8", errors="replace")
    else:
        data = payload
    return hashlib.sha256(data).hexdigest()[:16]


def content_sha256(payload: str | bytes) -> str:
    """Full SHA-256 hex of payload. Empty input → ``""``."""
    if not payload:
        return ""
    if isinstance(payload, str):
        data = payload.encode("utf-8", errors="replace")
    else:
        data = payload
    return hashlib.sha256(data).hexdigest()


@dataclass(frozen=True, slots=True)
class ManifestEntry:
    """One processed artifact, identified by its sha16."""

    sha16: str
    content_sha256: str
    source: str
    source_path: str
    imported_at: str
    status: str = "processed"
    retention: str = "manifest_only"
    profile: str = "default"
    derived_items: tuple[str, ...] = field(default_factory=tuple)
    schema_version: int = SCHEMA_VERSION

    def to_record(self) -> dict:
        d = asdict(self)
        d["record_type"] = "manifest"
        # tuple → list for JSON
        d["derived_items"] = list(d["derived_items"])
        return d

    @classmethod
    def from_record(cls, record: dict) -> "ManifestEntry":
        if record.get("record_type") != "manifest":
            raise ValueError(
                f"expected record_type=manifest, got {record.get('record_type')!r}"
            )
        derived = tuple(record.get("derived_items") or ())
        return cls(
            sha16=record["sha16"],
            content_sha256=record["content_sha256"],
            source=record["source"],
            source_path=record["source_path"],
            imported_at=record["imported_at"],
            status=record.get("status", "processed"),
            retention=record.get("retention", "manifest_only"),
            profile=record.get("profile", "default"),
            derived_items=derived,
            schema_version=record.get("schema_version", SCHEMA_VERSION),
        )


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S+00:00")


def validate_source(source: str) -> None:
    if source not in ALLOWED_SOURCES:
        raise ValueError(
            f"unknown source {source!r}; allowed: {sorted(ALLOWED_SOURCES)}"
        )


def validate_retention(retention: str) -> None:
    if retention not in ALLOWED_RETENTIONS:
        raise ValueError(
            f"unknown retention {retention!r}; allowed: {sorted(ALLOWED_RETENTIONS)}"
        )


class ManifestStore:
    """Read/write a single ``source_manifests/*.jsonl`` file.

    Behaviour:
    - Reading skips the leading schema record and any malformed lines (the
      latter are logged + reported via :meth:`malformed_lines`).
    - :meth:`append` is sha16-deduplicated. Duplicates return ``False``.
    - :meth:`ensure_schema_header` writes ``record_type=schema`` if the
      file is missing or empty.
    """

    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self._malformed: list[tuple[int, str, str]] = []  # (lineno, raw, reason)

    # ------------------------------------------------------------------
    # Schema header
    # ------------------------------------------------------------------
    def ensure_schema_header(self) -> bool:
        """Write the schema header if the file is empty or missing.

        Returns True iff the header was newly written. Existing schema
        headers are validated; mismatched versions log a warning but do
        not raise (migrations are P-x territory).
        """
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if self.path.exists() and self.path.stat().st_size > 0:
            # Validate first line
            with self.path.open("r", encoding="utf-8") as fh:
                first = fh.readline()
            try:
                rec = json.loads(first)
            except json.JSONDecodeError:
                _log.warning(
                    "manifest %s first line is malformed JSON; leaving as-is",
                    self.path,
                )
                return False
            if rec.get("record_type") != "schema":
                _log.warning(
                    "manifest %s first line is not a schema record; "
                    "expected record_type=schema, got %r",
                    self.path,
                    rec.get("record_type"),
                )
            elif rec.get("schema_version") != SCHEMA_VERSION:
                _log.warning(
                    "manifest %s schema_version=%r, expected %d; "
                    "migration not yet implemented (P-x)",
                    self.path,
                    rec.get("schema_version"),
                    SCHEMA_VERSION,
                )
            return False
        with self.path.open("w", encoding="utf-8") as fh:
            fh.write(json.dumps({
                "record_type": "schema",
                "schema_version": SCHEMA_VERSION,
            }, ensure_ascii=False))
            fh.write("\n")
        return True

    # ------------------------------------------------------------------
    # Reading
    # ------------------------------------------------------------------
    def iter_manifests(self) -> Iterator[ManifestEntry]:
        """Yield ManifestEntry rows. Schema header and malformed lines are
        skipped (the latter recorded for :meth:`malformed_lines`).
        """
        self._malformed.clear()
        if not self.path.exists():
            return
        with self.path.open("r", encoding="utf-8") as fh:
            for lineno, raw in enumerate(fh, start=1):
                line = raw.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError as exc:
                    self._malformed.append((lineno, raw.rstrip("\n"), str(exc)))
                    _log.warning(
                        "manifest %s line %d: malformed JSON (%s); skipping",
                        self.path, lineno, exc,
                    )
                    continue
                rtype = rec.get("record_type")
                if rtype == "schema":
                    continue
                if rtype != "manifest":
                    self._malformed.append(
                        (lineno, raw.rstrip("\n"), f"unknown record_type={rtype!r}")
                    )
                    _log.warning(
                        "manifest %s line %d: unknown record_type=%r; skipping",
                        self.path, lineno, rtype,
                    )
                    continue
                try:
                    yield ManifestEntry.from_record(rec)
                except (KeyError, ValueError) as exc:
                    self._malformed.append((lineno, raw.rstrip("\n"), str(exc)))
                    _log.warning(
                        "manifest %s line %d: invalid manifest (%s); skipping",
                        self.path, lineno, exc,
                    )

    def known_sha16(self) -> set[str]:
        """Set of sha16 values already recorded in the file."""
        return {entry.sha16 for entry in self.iter_manifests()}

    def malformed_lines(self) -> list[tuple[int, str, str]]:
        """Last call to :meth:`iter_manifests` reported these as malformed.

        Each tuple is ``(lineno, raw_line, reason)``. Empty until
        :meth:`iter_manifests` (or anything that calls it) runs.
        """
        return list(self._malformed)

    # ------------------------------------------------------------------
    # Writing
    # ------------------------------------------------------------------
    def append(
        self,
        *,
        payload: str,
        source: str,
        source_path: str,
        profile: str = "default",
        derived_items: Iterable[str] = (),
        imported_at: str | None = None,
    ) -> ManifestEntry | None:
        """Compute sha16/content_sha256 and append a manifest record.

        Returns the appended :class:`ManifestEntry` or ``None`` if the
        sha16 was already present (idempotent dedup).
        """
        validate_source(source)
        sha = sha16(payload)
        if not sha:
            raise ValueError("cannot ingest empty payload")
        if sha in self.known_sha16():
            return None
        self.ensure_schema_header()
        entry = ManifestEntry(
            sha16=sha,
            content_sha256=content_sha256(payload),
            source=source,
            source_path=source_path,
            imported_at=imported_at or _utc_now_iso(),
            status="processed",
            retention="manifest_only",
            profile=profile,
            derived_items=tuple(derived_items),
            schema_version=SCHEMA_VERSION,
        )
        validate_retention(entry.retention)
        with self.path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(entry.to_record(), ensure_ascii=False))
            fh.write("\n")
        return entry
