"""Ingest a Claude / ChatGPT / Discord export file (P1).

Generic counterpart to ``import_claude_memory.py``. Picks the right
source adapter based on ``--source``, runs the rule extractor, and
appends to ``data/processed_memory/`` plus ``data/source_manifests/``.

Default mode is ``--dry-run``: no files are mutated. ``--apply``
performs writes. The original input file is never modified; raw
payloads are not persisted beyond the sha16 manifest entry.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

from src.memory.ingestion.conflict import detect_pairs
from src.memory.ingestion.extractor import RuleExtractor
from src.memory.ingestion.manifest import ManifestStore
from src.memory.ingestion.sources import (
    ChatGPTSource,
    ClaudeSource,
    DiscordSource,
    SourceItem,
)
from src.memory.ingestion.writer import ProcessedMemoryWriter, slugify

_SOURCES = {
    "claude": ClaudeSource,
    "chatgpt": ChatGPTSource,
    "discord": DiscordSource,
}


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description=(
            "Ingest a Claude / ChatGPT / Discord export into Hermes "
            "processed_memory. Default is dry-run."
        ),
    )
    p.add_argument("--source", choices=sorted(_SOURCES), required=True)
    p.add_argument("--input", type=Path, required=True, help="File or root path")
    p.add_argument(
        "--processed-root",
        type=Path,
        default=Path("./data/processed_memory"),
    )
    p.add_argument(
        "--manifest-root",
        type=Path,
        default=Path("./data/source_manifests"),
    )
    p.add_argument(
        "--apply",
        action="store_true",
        help="Actually write. Without it, runs as a dry-run.",
    )
    return p


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    if not args.input.exists():
        print(f"[ingest-conversations] input does not exist: {args.input}", file=sys.stderr)
        return 2
    src_cls = _SOURCES[args.source]
    src = src_cls(args.input)
    items: list[SourceItem] = list(src.iter_items())
    print(f"[ingest-conversations] source={args.source} items={len(items)}")

    extractor = RuleExtractor()
    candidates: list = []
    for it in items:
        candidates.extend(extractor.extract(it))
    print(f"[ingest-conversations] candidates={len(candidates)}")

    pairs = detect_pairs(candidates)
    skip_keys = {(p.type, p.slug) for p in pairs}
    if pairs:
        print(f"[ingest-conversations] {len(pairs)} (type, slug) conflicts → needs_review")

    if not args.apply:
        per_type: dict[str, int] = {}
        for c in candidates:
            per_type[c.type] = per_type.get(c.type, 0) + 1
        print(f"[ingest-conversations] DRY RUN per-type: {per_type}")
        return 0

    writer = ProcessedMemoryWriter(args.processed_root)
    actions: dict[str, int] = {}
    for c in candidates:
        is_conflict = (c.type, slugify(c.title)) in skip_keys
        result = writer.write(
            type=c.type,
            title=c.title,
            body=c.body,
            source=c.source,
            source_sha16=c.source_sha16,
            confidence=c.confidence,
            tags=c.tags,
            needs_review=is_conflict,
        )
        actions[result.action] = actions.get(result.action, 0) + 1

    args.manifest_root.mkdir(parents=True, exist_ok=True)
    store = ManifestStore(args.manifest_root / f"{args.source}.jsonl")
    store.ensure_schema_header()
    for it in items:
        store.append(
            payload=it.content,
            source=args.source,
            source_path=it.source_path,
        )
    print(f"[ingest-conversations] write actions: {actions}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
