"""Import the user's Claude Code auto-memory into processed_memory (P1).

Reads markdown files under ``~/.claude/projects/<project>/memory/``
(or a path provided via ``--root``), snapshots them under
``data/external_memory/snapshots/`` (gitignored), runs the rule-based
extractor over each file, and feeds the candidates to
:class:`ProcessedMemoryWriter`. Conflicts are detected before writing
and routed straight to ``needs_review.md`` so the per-file writer
never sees an auto-merge case.

Claude Code auto-memory uses a YAML frontmatter convention
(``name:``, ``description:``, ``type: user|project|reference``) plus
free-form markdown body. The generic :class:`RuleExtractor` matches
explicit "# Decision:" / "# Failure pattern:" style headings and
won't see anything in those files. So this script also runs a
*frontmatter fallback*: every source file with a recognised
``type:`` field becomes one additional candidate (``type: user`` →
``user_preference``, ``type: project`` / ``type: reference`` →
``project_context``). RuleExtractor results, when present, are
additive — the two passes coexist.

Each candidate is scanned by :class:`PIIScanner` and
:class:`SecurityScanner` BEFORE writing. PII matches force
``pii_candidate=true`` (writer routes to ``needs_review.md``);
security findings at or above the threshold force a
``security_severity`` annotation that the writer's quarantine path
respects.

Default mode is dry-run: print what would be written and exit without
touching ``data/processed_memory/`` or the source manifests.
``--apply`` performs the actual writes. The external MEMORY.md is
NEVER modified — only read.
"""
from __future__ import annotations

import argparse
import json
import shutil
import sys
from datetime import datetime, timezone
from pathlib import Path

from src.memory.ingestion.conflict import detect_pairs
from src.memory.ingestion.extractor import Candidate, RuleExtractor
from src.memory.ingestion.manifest import ManifestStore, sha16
from src.memory.ingestion.pii import PIIScanner
from src.memory.ingestion.security_scan import SecurityScanner, SecuritySeverity
from src.memory.ingestion.sources import ClaudeSource, SourceItem
from src.memory.ingestion.writer import ProcessedMemoryWriter


_DEFAULT_ROOT = Path.home() / ".claude" / "projects" / "E--hermes-hybrid" / "memory"


def _utc_stamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


# --------------------------------------------------------------------------
# Frontmatter fallback
# --------------------------------------------------------------------------
_FRONTMATTER_TYPE_MAP = {
    "user": "user_preference",
    "project": "project_context",
    "reference": "project_context",
}


def _parse_frontmatter(text: str) -> tuple[dict[str, str], str]:
    """Tiny YAML-frontmatter parser. Returns (meta_dict, body).

    Only handles ``key: value`` pairs on individual lines — enough for
    the Claude Code auto-memory schema (``name``, ``description``,
    ``type``, ``originSessionId``). Multi-line values and lists fall
    out into the body, which is fine because the body is what we keep
    anyway.
    """
    if not text.startswith("---\n") and not text.startswith("---\r\n"):
        return {}, text
    sep = "\n---\n" if text.startswith("---\n") else "\r\n---\r\n"
    body_offset = len(sep)
    end = text.find(sep, 4)
    if end < 0:
        return {}, text
    raw_meta = text[4:end]
    body = text[end + body_offset:].strip()
    meta: dict[str, str] = {}
    for line in raw_meta.splitlines():
        if ":" not in line:
            continue
        key, _, value = line.partition(":")
        meta[key.strip()] = value.strip()
    return meta, body


def _frontmatter_candidate(item: SourceItem) -> Candidate | None:
    meta, body = _parse_frontmatter(item.content)
    fm_type = meta.get("type", "").strip().lower()
    target_type = _FRONTMATTER_TYPE_MAP.get(fm_type)
    if not target_type:
        return None
    title = meta.get("name") or Path(item.source_path).stem
    if not title.strip():
        return None
    description = meta.get("description", "").strip()
    composed_body = body.strip() if body.strip() else description
    if not composed_body:
        return None
    # Cap the body so a giant source file doesn't blow the token budget
    # downstream. Compile applies its own budget on top of this.
    if len(composed_body) > 4000:
        composed_body = composed_body[:4000].rstrip() + "\n\n[... truncated]"
    return Candidate(
        type=target_type,
        title=title.strip(),
        body=composed_body,
        confidence="medium",
        source="claude",
        source_sha16=sha16(item.content),
        source_path=item.source_path,
    )


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description=(
            "Snapshot the user's Claude Code auto-memory and ingest its "
            "rule-extracted candidates into data/processed_memory/. "
            "External MEMORY.md is read-only."
        ),
    )
    p.add_argument(
        "--root",
        type=Path,
        default=_DEFAULT_ROOT,
        help=f"Claude Code memory root (default: {_DEFAULT_ROOT})",
    )
    p.add_argument(
        "--processed-root",
        type=Path,
        default=Path("./data/processed_memory"),
        help="Hermes processed_memory root (writer target)",
    )
    p.add_argument(
        "--manifest-root",
        type=Path,
        default=Path("./data/source_manifests"),
        help="Source manifests root (sha16 jsonl)",
    )
    p.add_argument(
        "--snapshot-root",
        type=Path,
        default=Path("./data/external_memory/snapshots"),
        help="Where to copy a timestamped snapshot of the source files",
    )
    p.add_argument(
        "--apply",
        action="store_true",
        help="Actually write. Without it, runs as a dry-run.",
    )
    return p


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    root: Path = args.root
    if not root.exists():
        print(f"[import-claude-memory] root does not exist: {root}", file=sys.stderr)
        return 2
    dry = not args.apply
    mode = "DRY RUN" if dry else "APPLY"
    print(f"[import-claude-memory] mode={mode} root={root}")

    # --- snapshot ----------------------------------------------------
    snapshot_dir = args.snapshot_root / _utc_stamp()
    if not dry:
        snapshot_dir.mkdir(parents=True, exist_ok=True)
        for md in sorted(root.rglob("*.md")):
            rel = md.relative_to(root)
            dst = snapshot_dir / rel
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(md, dst)
        print(f"[import-claude-memory] snapshot: {snapshot_dir}")
    else:
        n = sum(1 for _ in root.rglob("*.md"))
        print(f"[import-claude-memory] would snapshot {n} markdown files to {snapshot_dir}")

    # --- iterate sources ---------------------------------------------
    src = ClaudeSource(root)
    extractor = RuleExtractor()
    all_items = list(src.iter_items())
    print(f"[import-claude-memory] source items: {len(all_items)}")

    candidates: list[Candidate] = []
    for item in all_items:
        rule_hits = extractor.extract(item)
        candidates.extend(rule_hits)
        # Frontmatter fallback always runs — Claude auto-memory files
        # rarely match RuleExtractor patterns but always carry a useful
        # frontmatter body.
        fm = _frontmatter_candidate(item)
        if fm is not None:
            candidates.append(fm)
    print(f"[import-claude-memory] extracted candidates: {len(candidates)}")

    # --- PII / Security scan -----------------------------------------
    pii_scanner = PIIScanner()
    sec_scanner = SecurityScanner()
    annotated: list[tuple[Candidate, bool, str]] = []
    pii_count = 0
    sec_high = 0
    sec_medium = 0
    for c in candidates:
        pii_findings = pii_scanner.scan(c.body)
        sec_findings = sec_scanner.scan(c.body, source_path=c.source_path)
        is_pii = bool(pii_findings)
        if is_pii:
            pii_count += 1
        if sec_findings:
            top = max(int(f.severity) for f in sec_findings)
        else:
            top = int(SecuritySeverity.NONE)
        sev_str = SecuritySeverity(top).name.lower() if top > 0 else "none"
        if sev_str == "high":
            sec_high += 1
        elif sev_str == "medium":
            sec_medium += 1
        annotated.append((c, is_pii, sev_str))
    print(
        f"[import-claude-memory] PII candidates: {pii_count} | "
        f"security medium: {sec_medium} | security high: {sec_high}"
    )

    # --- conflict pre-detection --------------------------------------
    pairs = detect_pairs(candidates)
    if pairs:
        print(
            f"[import-claude-memory] detected {len(pairs)} (type, slug) "
            "conflicts — routing both sides to needs_review."
        )
        skip_keys = {(p.type, p.slug) for p in pairs}
    else:
        skip_keys = set()

    # --- writer pass --------------------------------------------------
    if dry:
        report: dict[str, int] = {}
        for c in candidates:
            key = c.type
            report[key] = report.get(key, 0) + 1
        print(f"[import-claude-memory] would write per type: {report}")
        return 0

    writer = ProcessedMemoryWriter(args.processed_root)
    manifests: dict[str, ManifestStore] = {}
    actions: dict[str, int] = {}
    for c, is_pii, sev_str in annotated:
        from src.memory.ingestion.writer import slugify as _slug
        is_conflict = (c.type, _slug(c.title)) in skip_keys
        result = writer.write(
            type=c.type,
            title=c.title,
            body=c.body,
            source=c.source,
            source_sha16=c.source_sha16,
            confidence=c.confidence,
            tags=c.tags,
            needs_review=is_conflict,
            pii_candidate=is_pii,
            security_severity=sev_str,
        )
        actions[result.action] = actions.get(result.action, 0) + 1

    # --- per-source manifest -----------------------------------------
    args.manifest_root.mkdir(parents=True, exist_ok=True)
    by_source: dict[tuple[str, str], list[str]] = {}
    for c in candidates:
        by_source.setdefault((c.source, c.source_path), []).append(c.type)
    for (source, path), types in by_source.items():
        manifest_path = args.manifest_root / f"{source}.jsonl"
        store = manifests.setdefault(source, ManifestStore(manifest_path))
        store.ensure_schema_header()
        # The payload for sha16 is the raw file content; re-compute.
        try:
            payload = Path(path).read_text(encoding="utf-8")
        except OSError:
            continue
        store.append(
            payload=payload,
            source=source,
            source_path=path,
            derived_items=[f"{t}:{sha16(payload)}" for t in types],
        )

    print(f"[import-claude-memory] write actions: {actions}")
    print("[import-claude-memory] done.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
