"""Source adapters for the import pipeline (P1).

Each adapter takes whatever the upstream provider hands us — markdown
files for Claude Code auto-memory, the OpenAI conversation export
JSON, the Discord JSON dumps the bot produces — and yields uniform
:class:`SourceItem` objects for the extractor to chew on.

Two design rules:

1. **No raw text leaves this module untouched.** Every yielded
   ``SourceItem`` carries the raw payload, but the rest of the pipeline
   only persists the sha16 manifest entry plus rule-extracted
   candidates. Adapters never write back to disk.

2. **Source-shape errors are recoverable.** An unreadable file or a
   single malformed JSON object yields nothing for that input but does
   not abort the iterator — the next file/object is still processed.
   This matches the writer's dedup-by-sha16 behaviour: re-running an
   import only adds new content.
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterator, Mapping

_log = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class SourceItem:
    """One unit of conversation content seen by an extractor.

    ``content`` is the raw payload used to compute sha16 and to drive
    rule extraction. ``metadata`` carries provider-specific fields
    (timestamps, message ids, channel names) so future provenance
    plumbing in P4/P5 has a place to look.
    """

    source: str             # claude | chatgpt | discord
    source_path: str        # canonical reference (file path, conversation id, etc.)
    content: str            # raw text payload
    metadata: Mapping[str, Any] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Claude Code auto-memory
# ---------------------------------------------------------------------------
class ClaudeSource:
    """Read every ``.md`` under a Claude Code memory root.

    ``~/.claude/projects/<project>/memory/`` is the typical layout.
    The MEMORY.md index is treated like any other file — the index
    structure is honoured by the extractor, not by the source.
    """

    def __init__(self, root: Path | str) -> None:
        self.root = Path(root)

    def iter_items(self) -> Iterator[SourceItem]:
        if not self.root.exists():
            _log.warning("ClaudeSource root not found: %s", self.root)
            return
        for md in sorted(self.root.rglob("*.md")):
            try:
                content = md.read_text(encoding="utf-8")
            except OSError as exc:
                _log.warning("ClaudeSource skip %s: %s", md, exc)
                continue
            if not content.strip():
                continue
            yield SourceItem(
                source="claude",
                source_path=str(md),
                content=content,
                metadata={"file_name": md.name},
            )


# ---------------------------------------------------------------------------
# ChatGPT conversation export
# ---------------------------------------------------------------------------
class ChatGPTSource:
    """Read OpenAI's ``conversations.json`` export.

    The official export is a JSON array; each conversation has a
    ``mapping`` dict from message id to message node, plus a top-level
    ``title``, ``create_time`` etc. We treat each conversation as one
    SourceItem with the concatenated user-message text as ``content``
    — the extractor focuses on user-side patterns (preferences, prompt
    templates, decisions), so assistant turns are dropped.
    """

    def __init__(self, json_path: Path | str) -> None:
        self.json_path = Path(json_path)

    def iter_items(self) -> Iterator[SourceItem]:
        if not self.json_path.exists():
            _log.warning("ChatGPTSource path not found: %s", self.json_path)
            return
        try:
            data = json.loads(self.json_path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            _log.warning("ChatGPTSource parse failed %s: %s", self.json_path, exc)
            return
        if isinstance(data, dict):
            data = [data]
        if not isinstance(data, list):
            _log.warning("ChatGPTSource expected list/dict at top level")
            return
        for conv in data:
            if not isinstance(conv, dict):
                continue
            try:
                user_text = self._collect_user_text(conv)
            except Exception as exc:  # noqa: BLE001
                _log.warning("ChatGPTSource conv parse skip: %s", exc)
                continue
            if not user_text.strip():
                continue
            conv_id = str(conv.get("id") or conv.get("conversation_id") or "")
            title = str(conv.get("title") or "")
            yield SourceItem(
                source="chatgpt",
                source_path=conv_id or self.json_path.name,
                content=user_text,
                metadata={
                    "title": title,
                    "conversation_id": conv_id,
                    "create_time": conv.get("create_time"),
                },
            )

    @staticmethod
    def _collect_user_text(conv: Mapping[str, Any]) -> str:
        mapping = conv.get("mapping") or {}
        if isinstance(mapping, Mapping):
            chunks: list[str] = []
            for node in mapping.values():
                msg = (node or {}).get("message") if isinstance(node, Mapping) else None
                if not isinstance(msg, Mapping):
                    continue
                author = (msg.get("author") or {}).get("role") if isinstance(msg.get("author"), Mapping) else None
                if author != "user":
                    continue
                content = msg.get("content") or {}
                parts = content.get("parts") if isinstance(content, Mapping) else None
                if not isinstance(parts, list):
                    continue
                for p in parts:
                    if isinstance(p, str) and p.strip():
                        chunks.append(p)
            return "\n\n".join(chunks)
        # Fallback shape: a flat ``messages`` list of {role, content}.
        messages = conv.get("messages")
        if isinstance(messages, list):
            return "\n\n".join(
                m.get("content", "")
                for m in messages
                if isinstance(m, Mapping) and m.get("role") == "user"
            )
        return ""


# ---------------------------------------------------------------------------
# Discord
# ---------------------------------------------------------------------------
class DiscordSource:
    """Read a Discord export file.

    The Hermes Discord bot writes a JSON array of message objects with
    ``content``, ``author``, ``timestamp``, ``channel`` and so on. Each
    message becomes a SourceItem. ``content`` is the raw text — bot
    self-mentions and command invocations are kept; the extractor will
    drop anything that doesn't match a rule.
    """

    def __init__(self, json_path: Path | str) -> None:
        self.json_path = Path(json_path)

    def iter_items(self) -> Iterator[SourceItem]:
        if not self.json_path.exists():
            _log.warning("DiscordSource path not found: %s", self.json_path)
            return
        try:
            data = json.loads(self.json_path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            _log.warning("DiscordSource parse failed %s: %s", self.json_path, exc)
            return
        if isinstance(data, dict):
            data = [data]
        if not isinstance(data, list):
            return
        for msg in data:
            if not isinstance(msg, Mapping):
                continue
            content = str(msg.get("content") or "")
            if not content.strip():
                continue
            yield SourceItem(
                source="discord",
                source_path=str(msg.get("id") or self.json_path.name),
                content=content,
                metadata={
                    "channel": msg.get("channel"),
                    "author": (msg.get("author") or {}).get("name") if isinstance(msg.get("author"), Mapping) else msg.get("author"),
                    "timestamp": msg.get("timestamp"),
                },
            )


__all__ = [
    "SourceItem",
    "ClaudeSource",
    "ChatGPTSource",
    "DiscordSource",
]
