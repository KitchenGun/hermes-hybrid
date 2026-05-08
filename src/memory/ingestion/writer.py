"""Append / update / supersede items in ``data/processed_memory/*.md`` (P0-B).

Each processed_memory file is a flat markdown list of ``## {title}`` sections
followed by an HTML comment that carries metadata. The writer is the only
component that should mutate these files in production — manual edits work
but they must preserve the meta block.

Item identity
-------------
``item_id = f"{type}:{source_sha16}:{slug(title)}"``

The slug is ASCII-only (lowercase, hyphens), with a sha16(title) fallback
when the title produces no ASCII characters. ``item_id`` is stable across
edits to the body, which is why update can be idempotent on the same
sha16 input even when the body wording changes slightly after normalisation.

Update semantics
----------------
The branching is exhaustive:

1. **Same item_id** (= same type + sha16 + slug): idempotent update;
   ``updated_at`` advances, the body is replaced. If the new body equals
   the existing body, the file is not rewritten.

2. **Same (type, slug) + different sha16 + bodies normalise to equal**:
   idempotent merge; the existing item's ``source_sha16`` is updated to
   point at the new payload. Status stays ``active``.

3. **Same (type, slug) + different sha16 + bodies differ**: conflict.
   Both the old and the proposed item are flipped to ``status=needs_review``
   and a row is appended to ``data/processed_memory/needs_review.md`` so
   the human reviewer can see the timestamp comparison and pick a winner.
   Auto-merge is forbidden.

4. **source = "user_correction"**: the new item is written ``status=active``,
   the existing item is flipped to ``status=superseded``, and the new
   item's meta carries ``supersedes={old_item_id}`` so rollback can
   reconstruct lineage without raw text.

5. **needs_review / pii_candidate / security_severity >= threshold**:
   item is written with ``status=needs_review`` regardless of branch
   above. Compile excludes it.

Type → file mapping
-------------------
The 7 ``type`` values map to the 7 topic-specific markdown files. The
extractor (P1) emits one of these tags per candidate:

- ``user_preference``  → ``user_profile.md``
- ``response_style``   → ``response_style.md``
- ``project_context``  → ``project_context.md``
- ``decision``         → ``decision_log.md``
- ``prompt_template``  → ``prompt_library.md``
- ``failure_pattern``  → ``failure_patterns.md``
- ``reusable_skill``   → ``skills_index.md``

``needs_review`` is not a real type; it's the quarantine destination
for any item with ``status=needs_review``.
"""
from __future__ import annotations

import hashlib
import logging
import re
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable, Literal, Mapping

from .security_scan import SecuritySeverity

_log = logging.getLogger(__name__)


SCHEMA_VERSION = 1

# ----------------------------------------------------------------------
# Type → file mapping (closed set; extractor must emit one of these)
# ----------------------------------------------------------------------
TYPE_TO_FILE: Mapping[str, str] = {
    "user_preference": "user_profile.md",
    "response_style": "response_style.md",
    "project_context": "project_context.md",
    "decision": "decision_log.md",
    "prompt_template": "prompt_library.md",
    "failure_pattern": "failure_patterns.md",
    "reusable_skill": "skills_index.md",
}
ALLOWED_TYPES = frozenset(TYPE_TO_FILE.keys())
NEEDS_REVIEW_FILE = "needs_review.md"

ALLOWED_SOURCES = frozenset({
    "claude", "chatgpt", "discord", "hermes_session", "user_correction",
})
ALLOWED_STATUS = frozenset({"active", "superseded", "needs_review"})
ALLOWED_CONFIDENCE = frozenset({"low", "medium", "high"})
SECURITY_SEVERITIES = frozenset({"none", "low", "medium", "high"})


# ----------------------------------------------------------------------
# Slug + item_id
# ----------------------------------------------------------------------
def _sha16(text: str) -> str:
    if not text:
        return ""
    return hashlib.sha256(text.encode("utf-8", errors="replace")).hexdigest()[:16]


_NON_ALNUM_RE = re.compile(r"[^a-z0-9]+")
_HYPHEN_COLLAPSE = re.compile(r"-+")


def slugify(title: str, *, max_len: int = 60) -> str:
    """ASCII slug. Empty result falls back to sha16(title)."""
    base = title.casefold()
    base = _NON_ALNUM_RE.sub("-", base)
    base = _HYPHEN_COLLAPSE.sub("-", base).strip("-")
    if not base:
        base = _sha16(title) or "untitled"
    return base[:max_len]


def make_item_id(*, type: str, source_sha16: str, title: str) -> str:
    if type not in ALLOWED_TYPES:
        raise ValueError(
            f"unknown type {type!r}; expected one of {sorted(ALLOWED_TYPES)}"
        )
    return f"{type}:{source_sha16}:{slugify(title)}"


# ----------------------------------------------------------------------
# Item dataclass
# ----------------------------------------------------------------------
@dataclass(frozen=True, slots=True)
class MemoryItem:
    """One ``## {title}`` section + meta block in a processed_memory file."""

    item_id: str
    type: str
    title: str
    body: str
    source: str
    source_sha16: str
    created_at: str
    updated_at: str
    confidence: str = "medium"
    tags: tuple[str, ...] = ()
    status: str = "active"
    needs_review: bool = False
    pii_candidate: bool = False
    security_risk: bool = False
    security_severity: str = "none"
    supersedes: str = ""
    profile: str = "default"
    schema_version: int = SCHEMA_VERSION
    # provenance fields — P0-B leaves them empty; P4/P5 populate.
    origin: str = ""
    origin_session_id: str = ""
    origin_message_id: str = ""
    origin_thread_id: str = ""
    cron_job_id: str = ""
    cron_run_id: str = ""
    delivery_target: str = ""

    # ------------------------------------------------------------------
    # Validation
    # ------------------------------------------------------------------
    def __post_init__(self) -> None:
        if self.type not in ALLOWED_TYPES:
            raise ValueError(f"type={self.type!r} not in {sorted(ALLOWED_TYPES)}")
        if self.source not in ALLOWED_SOURCES:
            raise ValueError(f"source={self.source!r} not in {sorted(ALLOWED_SOURCES)}")
        if self.status not in ALLOWED_STATUS:
            raise ValueError(f"status={self.status!r} not in {sorted(ALLOWED_STATUS)}")
        if self.confidence not in ALLOWED_CONFIDENCE:
            raise ValueError(
                f"confidence={self.confidence!r} not in {sorted(ALLOWED_CONFIDENCE)}"
            )
        if self.security_severity not in SECURITY_SEVERITIES:
            raise ValueError(
                f"security_severity={self.security_severity!r} "
                f"not in {sorted(SECURITY_SEVERITIES)}"
            )

    # ------------------------------------------------------------------
    # Markdown I/O
    # ------------------------------------------------------------------
    def to_markdown(self) -> str:
        meta_lines = [
            f"  schema_version={self.schema_version}",
            f"  item_id={self.item_id}",
            f"  type={self.type}",
            f"  source={self.source}",
            f"  source_sha16={self.source_sha16}",
            f"  created_at={self.created_at}",
            f"  updated_at={self.updated_at}",
            f"  confidence={self.confidence}",
            f"  tags={','.join(self.tags)}",
            f"  status={self.status}",
            f"  needs_review={'true' if self.needs_review else 'false'}",
            f"  pii_candidate={'true' if self.pii_candidate else 'false'}",
            f"  security_risk={'true' if self.security_risk else 'false'}",
            f"  security_severity={self.security_severity}",
            f"  supersedes={self.supersedes}",
            f"  profile={self.profile}",
            f"  origin={self.origin}",
            f"  origin_session_id={self.origin_session_id}",
            f"  origin_message_id={self.origin_message_id}",
            f"  origin_thread_id={self.origin_thread_id}",
            f"  cron_job_id={self.cron_job_id}",
            f"  cron_run_id={self.cron_run_id}",
            f"  delivery_target={self.delivery_target}",
        ]
        meta_block = "<!-- meta:\n" + "\n".join(meta_lines) + "\n-->"
        return f"## {self.title}\n\n{self.body.rstrip()}\n\n{meta_block}\n"


_META_RE = re.compile(
    r"<!--\s*meta:\s*(.*?)\s*-->",
    re.DOTALL,
)
_SECTION_RE = re.compile(
    r"(?ms)^##\s+(?P<title>.+?)\s*\n(?P<rest>.*?)(?=^##\s|\Z)",
)


def _parse_meta_kv(meta_text: str) -> dict[str, str]:
    out: dict[str, str] = {}
    for raw in meta_text.splitlines():
        line = raw.strip()
        if not line or "=" not in line:
            continue
        key, _, val = line.partition("=")
        out[key.strip()] = val.strip()
    return out


def parse_processed_file(text: str) -> list[MemoryItem]:
    """Parse a processed_memory markdown file into items.

    Sections without a meta block are skipped (they're considered free-form
    intro text — the file header). Sections whose meta block is missing
    required fields are also skipped, with a warning.
    """
    items: list[MemoryItem] = []
    for match in _SECTION_RE.finditer(text):
        title = match.group("title").strip()
        rest = match.group("rest")
        meta_match = _META_RE.search(rest)
        if not meta_match:
            continue
        body = rest[:meta_match.start()].strip()
        meta = _parse_meta_kv(meta_match.group(1))
        try:
            items.append(MemoryItem(
                item_id=meta["item_id"],
                type=meta["type"],
                title=title,
                body=body,
                source=meta.get("source", "hermes_session"),
                source_sha16=meta.get("source_sha16", ""),
                created_at=meta.get("created_at", ""),
                updated_at=meta.get("updated_at", meta.get("created_at", "")),
                confidence=meta.get("confidence", "medium"),
                tags=tuple(t for t in meta.get("tags", "").split(",") if t),
                status=meta.get("status", "active"),
                needs_review=meta.get("needs_review", "false") == "true",
                pii_candidate=meta.get("pii_candidate", "false") == "true",
                security_risk=meta.get("security_risk", "false") == "true",
                security_severity=meta.get("security_severity", "none"),
                supersedes=meta.get("supersedes", ""),
                profile=meta.get("profile", "default"),
                schema_version=int(meta.get("schema_version", SCHEMA_VERSION)),
                origin=meta.get("origin", ""),
                origin_session_id=meta.get("origin_session_id", ""),
                origin_message_id=meta.get("origin_message_id", ""),
                origin_thread_id=meta.get("origin_thread_id", ""),
                cron_job_id=meta.get("cron_job_id", ""),
                cron_run_id=meta.get("cron_run_id", ""),
                delivery_target=meta.get("delivery_target", ""),
            ))
        except (KeyError, ValueError) as exc:
            _log.warning("processed_memory parse skip: %s in %r", exc, title)
    return items


# ----------------------------------------------------------------------
# Writer
# ----------------------------------------------------------------------
@dataclass(frozen=True, slots=True)
class WriteResult:
    action: Literal["append", "update", "merge", "conflict", "supersede", "noop"]
    item: MemoryItem | None = None
    superseded_id: str = ""
    conflict_existing_id: str = ""


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S+00:00")


_WS_COMPARE_RE = re.compile(r"\s+")
_PUNCT_COMPARE_RE = re.compile(r"[^\w\s]", re.UNICODE)


def _normalize_for_compare(text: str) -> str:
    """Body-equality comparison helper.

    Strips all punctuation (so ``"we use kanban."`` and
    ``"we use kanban"`` compare equal), collapses whitespace, casefolds.
    Mirrors :func:`src.memory.ingestion.normalizer.normalize_body_for_compare`
    — the two functions intentionally produce the same output so the
    writer's idempotent-merge branch and the normalizer's external
    helper agree on what counts as the "same body."
    """
    if not text:
        return ""
    base = text.casefold()
    base = _PUNCT_COMPARE_RE.sub(" ", base)
    base = _WS_COMPARE_RE.sub(" ", base).strip()
    return base


class ProcessedMemoryWriter:
    """Append / update / supersede items across processed_memory files."""

    def __init__(
        self,
        root: Path,
        *,
        security_threshold: SecuritySeverity = SecuritySeverity.MEDIUM,
    ) -> None:
        self.root = Path(root)
        self.security_threshold = security_threshold

    # ------------------------------------------------------------------
    # File helpers
    # ------------------------------------------------------------------
    def _file_for_type(self, type: str) -> Path:
        if type not in TYPE_TO_FILE:
            raise ValueError(f"unknown type {type!r}")
        return self.root / TYPE_TO_FILE[type]

    def _needs_review_file(self) -> Path:
        return self.root / NEEDS_REVIEW_FILE

    def _read_items(self, path: Path) -> list[MemoryItem]:
        if not path.exists():
            return []
        return parse_processed_file(path.read_text(encoding="utf-8"))

    def _write_items(self, path: Path, header: str, items: Iterable[MemoryItem]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        chunks = [header.rstrip() + "\n"]
        for it in items:
            chunks.append("\n" + it.to_markdown())
        path.write_text("".join(chunks), encoding="utf-8")

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------
    def write(
        self,
        *,
        type: str,
        title: str,
        body: str,
        source: str,
        source_sha16: str,
        confidence: str = "medium",
        tags: Iterable[str] = (),
        needs_review: bool = False,
        pii_candidate: bool = False,
        security_severity: str = "none",
        profile: str = "default",
        provenance: Mapping[str, str] | None = None,
    ) -> WriteResult:
        """Write a candidate. Branches on the update policy described above."""
        if type not in ALLOWED_TYPES:
            raise ValueError(f"unknown type {type!r}")
        if source not in ALLOWED_SOURCES:
            raise ValueError(f"unknown source {source!r}")

        sev = SecuritySeverity.from_str(security_severity) if security_severity != "none" else SecuritySeverity.NONE
        force_quarantine = (
            needs_review
            or pii_candidate
            or sev >= self.security_threshold
        )
        item_status = "needs_review" if force_quarantine else "active"

        item_id = make_item_id(type=type, source_sha16=source_sha16, title=title)
        now = _utc_now_iso()
        provenance = dict(provenance or {})
        new_item = MemoryItem(
            item_id=item_id,
            type=type,
            title=title,
            body=body.rstrip(),
            source=source,
            source_sha16=source_sha16,
            created_at=now,
            updated_at=now,
            confidence=confidence,
            tags=tuple(tags),
            status=item_status,
            needs_review=force_quarantine,
            pii_candidate=pii_candidate,
            security_risk=sev >= SecuritySeverity.HIGH,
            security_severity=security_severity,
            profile=profile,
            origin=provenance.get("origin", ""),
            origin_session_id=provenance.get("origin_session_id", ""),
            origin_message_id=provenance.get("origin_message_id", ""),
            origin_thread_id=provenance.get("origin_thread_id", ""),
            cron_job_id=provenance.get("cron_job_id", ""),
            cron_run_id=provenance.get("cron_run_id", ""),
            delivery_target=provenance.get("delivery_target", ""),
        )

        # Quarantine path skips the topic-specific file and goes straight
        # to needs_review.md.
        if force_quarantine:
            return self._append_needs_review(new_item, reason=self._quarantine_reason(
                needs_review, pii_candidate, sev,
            ))

        # source=user_correction always supersedes any prior active item
        # with the same (type, slug).
        target = self._file_for_type(type)
        existing = self._read_items(target)
        slug = slugify(title)

        if source == "user_correction":
            return self._handle_user_correction(target, existing, new_item, slug)

        # Look for matching items by item_id, then by (type, slug).
        for idx, ex in enumerate(existing):
            if ex.item_id == new_item.item_id:
                return self._handle_same_item_id(target, existing, idx, ex, new_item)
        for idx, ex in enumerate(existing):
            if ex.type == type and slugify(ex.title) == slug and ex.status == "active":
                if _normalize_for_compare(ex.body) == _normalize_for_compare(new_item.body):
                    return self._handle_idempotent_merge(target, existing, idx, ex, new_item)
                return self._handle_conflict(target, existing, idx, ex, new_item)

        # Brand new candidate → append.
        existing_active = [it for it in existing]
        existing_active.append(new_item)
        self._write_items(target, _topic_header(type), existing_active)
        return WriteResult(action="append", item=new_item)

    # ------------------------------------------------------------------
    # Branch implementations
    # ------------------------------------------------------------------
    def _handle_same_item_id(
        self,
        path: Path,
        items: list[MemoryItem],
        idx: int,
        existing: MemoryItem,
        new: MemoryItem,
    ) -> WriteResult:
        if existing.body.rstrip() == new.body.rstrip():
            return WriteResult(action="noop", item=existing)
        updated = replace(
            existing,
            body=new.body,
            updated_at=new.updated_at,
            tags=new.tags or existing.tags,
            confidence=new.confidence,
        )
        items[idx] = updated
        self._write_items(path, _topic_header(existing.type), items)
        return WriteResult(action="update", item=updated)

    def _handle_idempotent_merge(
        self,
        path: Path,
        items: list[MemoryItem],
        idx: int,
        existing: MemoryItem,
        new: MemoryItem,
    ) -> WriteResult:
        merged = replace(
            existing,
            source_sha16=new.source_sha16,
            updated_at=new.updated_at,
            tags=tuple(sorted(set(existing.tags) | set(new.tags))),
        )
        # item_id intentionally unchanged — old sha16 portion of item_id
        # would otherwise rewrite history. Only meta source_sha16 advances.
        items[idx] = merged
        self._write_items(path, _topic_header(existing.type), items)
        return WriteResult(action="merge", item=merged)

    def _handle_conflict(
        self,
        path: Path,
        items: list[MemoryItem],
        idx: int,
        existing: MemoryItem,
        new: MemoryItem,
    ) -> WriteResult:
        # Both flip to needs_review; existing stays in its file (so manual
        # editing still finds it), new entry is recorded in needs_review.md.
        flagged_existing = replace(
            existing,
            status="needs_review",
            needs_review=True,
            updated_at=new.updated_at,
        )
        items[idx] = flagged_existing
        self._write_items(path, _topic_header(existing.type), items)
        flagged_new = replace(
            new,
            status="needs_review",
            needs_review=True,
        )
        self._append_needs_review(
            flagged_new,
            reason=f"conflict_with={existing.item_id}",
        )
        return WriteResult(
            action="conflict",
            item=flagged_new,
            conflict_existing_id=existing.item_id,
        )

    def _handle_user_correction(
        self,
        path: Path,
        items: list[MemoryItem],
        new: MemoryItem,
        slug: str,
    ) -> WriteResult:
        superseded_id = ""
        for idx, ex in enumerate(items):
            if ex.type == new.type and slugify(ex.title) == slug and ex.status == "active":
                items[idx] = replace(
                    ex,
                    status="superseded",
                    updated_at=new.updated_at,
                )
                superseded_id = ex.item_id
                break
        active_new = replace(new, supersedes=superseded_id)
        items.append(active_new)
        self._write_items(path, _topic_header(new.type), items)
        return WriteResult(
            action="supersede",
            item=active_new,
            superseded_id=superseded_id,
        )

    def _append_needs_review(self, item: MemoryItem, *, reason: str) -> WriteResult:
        existing = self._read_items(self._needs_review_file())
        # dedup by item_id within the quarantine file as well
        for ex in existing:
            if ex.item_id == item.item_id:
                return WriteResult(action="noop", item=ex)
        tagged = replace(item, tags=tuple(sorted(set(item.tags) | {f"reason:{reason}"})))
        existing.append(tagged)
        self._write_items(self._needs_review_file(), _NEEDS_REVIEW_HEADER, existing)
        return WriteResult(action="append", item=tagged)

    @staticmethod
    def _quarantine_reason(
        needs_review: bool, pii: bool, severity: SecuritySeverity
    ) -> str:
        if pii:
            return "pii"
        if severity >= SecuritySeverity.HIGH:
            return "security_high"
        if severity >= SecuritySeverity.MEDIUM:
            return "security_medium"
        if severity >= SecuritySeverity.LOW:
            return "security_low"
        if needs_review:
            return "low_confidence"
        return "unknown"


# ----------------------------------------------------------------------
# File headers
# ----------------------------------------------------------------------
_TOPIC_HEADERS: Mapping[str, str] = {
    "user_preference": (
        "<!-- Hermes processed memory: user_profile -->\n"
        "# User Profile\n"
    ),
    "response_style": (
        "<!-- Hermes processed memory: response_style -->\n"
        "# Response Style\n"
    ),
    "project_context": (
        "<!-- Hermes processed memory: project_context -->\n"
        "# Project Context\n"
    ),
    "decision": (
        "<!-- Hermes processed memory: decision_log -->\n"
        "# Decision Log\n"
    ),
    "prompt_template": (
        "<!-- Hermes processed memory: prompt_library -->\n"
        "# Prompt Library\n"
    ),
    "failure_pattern": (
        "<!-- Hermes processed memory: failure_patterns -->\n"
        "# Failure Patterns\n"
    ),
    "reusable_skill": (
        "<!-- Hermes processed memory: skills_index -->\n"
        "# Skills Index\n"
    ),
}
_NEEDS_REVIEW_HEADER = (
    "<!-- Hermes processed memory: needs_review -->\n"
    "# NEEDS_REVIEW\n"
)


def _topic_header(type: str) -> str:
    return _TOPIC_HEADERS.get(type, f"# {type}\n")


__all__ = [
    "SCHEMA_VERSION",
    "TYPE_TO_FILE",
    "ALLOWED_TYPES",
    "ALLOWED_SOURCES",
    "ALLOWED_STATUS",
    "MemoryItem",
    "ProcessedMemoryWriter",
    "WriteResult",
    "make_item_id",
    "parse_processed_file",
    "slugify",
]
