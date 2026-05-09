"""Discord save-intent detection (P4 scaffolding, dry-run only).

Detects explicit save commands in user messages so the gateway can
later forward them to the ingestion pipeline. **No actual ingestion
happens here.** This module is a pure detector — it does not import
``discord.py``, does not touch ``data/`` directly, and does not
modify ``src/gateway/discord_bot.py`` (that file carries the W11
marker block on main and stays untouched until P4 is fully approved).

Scope of P4 dry-run:

- Only **explicit save markers** are detected. Implicit / pattern-
  based capture (e.g. "auto-save anything that mentions a decision")
  is deliberately out of scope until ``discord_auto_ingest_enabled``
  is reviewed and turned on.
- The default policy is dry-run: :func:`try_extract_save_intent`
  returns the intent object so the caller can log / preview, but
  does not write to ``data/ingest_staging/``. Writing is gated by
  the flag below and is not implemented in this commit.
- Detection covers both Korean and English explicit phrases, plus
  a slash-command form (``/memo-save``) and a mention form
  (``@hermes save``). The patterns are intentionally narrow — false
  positives end up in NEEDS_REVIEW once active, and we'd rather
  miss-detect than spam quarantine.

Activation contract (out of scope here, captured for the eventual
gateway integration):

1. ``discord_auto_ingest_enabled=True`` (config flag, default False).
2. Caller resolves the SaveIntent against the actual ``discord.Message``
   object (author allowlist, channel allowlist, attachment policy).
3. Caller writes raw payload to ``data/ingest_staging/`` (P0-A
   policy: gitignored, sha16-only retention) and appends a
   ``data/source_manifests/discord.jsonl`` entry.
4. Caller invokes the rule extractor / writer pipeline as in
   ``scripts/ingest_conversations.py --source discord``.

None of those steps are wired up here. This module exists so the
detector can be unit-tested without standing up the discord bot,
and so step (2)-(4) is a small focused PR rather than a sweeping
gateway change.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from enum import Enum
from typing import Iterable


class SaveTrigger(str, Enum):
    """Why the detector flagged the message."""

    KOREAN_PHRASE = "ko_phrase"      # "기억해" 등
    ENGLISH_PHRASE = "en_phrase"     # "save this" 등
    SLASH_COMMAND = "slash_command"  # "/memo-save"
    MENTION = "mention"              # "@hermes save"


# ---------------------------------------------------------------------------
# Patterns
# ---------------------------------------------------------------------------
# Korean save phrases. ``\b`` doesn't work for Hangul so we anchor on
# whitespace / punctuation / end-of-string instead.
_KO_SAVE_PHRASES: tuple[str, ...] = (
    "기억해",
    "기억해둬",
    "기억해줘",
    "기억해주세요",
    "메모해",
    "메모해줘",
    "메모해주세요",
    "저장해",
    "저장해줘",
    "저장해주세요",
)
_KO_RE = re.compile(
    r"(?:^|[\s.,!?])(" + "|".join(map(re.escape, _KO_SAVE_PHRASES)) + r")(?:$|[\s.,!?])",
)

_EN_SAVE_PHRASES: tuple[str, ...] = (
    "remember this",
    "remember that",
    "save this",
    "save that",
    "memorize this",
    "memorize that",
    "note this",
)
_EN_RE = re.compile(
    r"\b(" + "|".join(map(re.escape, _EN_SAVE_PHRASES)) + r")\b",
    re.IGNORECASE,
)

# Slash-command form (``/memo-save`` or ``/memo save``). Matches at the
# start of the message only — mid-message slashes can be path fragments.
_SLASH_RE = re.compile(
    r"^\s*/(?:memo[-_]save|remember|save)(?:\s|$)",
    re.IGNORECASE,
)

# Mention form. The actual bot mention id is supplied per-deployment;
# the detector accepts either ``@hermes save``-style or the literal
# Discord ``<@123>`` form followed by ``save`` / ``remember``.
_MENTION_RE = re.compile(
    r"(?:@hermes|<@!?\d+>)\s+(?:save|remember|memo|기억|메모|저장)",
    re.IGNORECASE,
)


# ---------------------------------------------------------------------------
# Result
# ---------------------------------------------------------------------------
@dataclass(frozen=True, slots=True)
class SaveIntent:
    """One detected save command in a Discord message."""

    trigger: SaveTrigger
    matched_text: str          # the trigger phrase / command itself
    body: str                  # full message body the caller will save
    note: str = ""             # optional human note (caller-supplied)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------
def try_extract_save_intent(
    message_text: str,
    *,
    auto_ingest_enabled: bool = False,
) -> SaveIntent | None:
    """Return a SaveIntent if the message contains an explicit save marker.

    Parameters
    ----------
    message_text:
        The raw Discord message body. The detector does not need the
        full ``discord.Message`` object — that lookup belongs in the
        gateway.
    auto_ingest_enabled:
        Mirrors ``Settings.discord_auto_ingest_enabled``. Default
        False. Even when True, this function only **detects** —
        actual staging / manifest writing is the caller's job.

    Returns
    -------
    SaveIntent | None
        ``None`` if no marker matched. The presence of a SaveIntent
        does not imply anything was written; this is a dry-run
        detector.

    Notes
    -----
    The auto_ingest_enabled flag is accepted (rather than read from
    config inside) so callers can run the detector in test contexts
    without instantiating Settings, and so the policy decision lives
    at the call site where logging context is available.
    """
    if not message_text or not message_text.strip():
        return None

    body = message_text.strip()

    # Slash command takes precedence — it's the most explicit and
    # least likely to be a false positive.
    m = _SLASH_RE.match(body)
    if m is not None:
        return SaveIntent(
            trigger=SaveTrigger.SLASH_COMMAND,
            matched_text=m.group(0).strip(),
            body=body,
        )

    m = _MENTION_RE.search(body)
    if m is not None:
        return SaveIntent(
            trigger=SaveTrigger.MENTION,
            matched_text=m.group(0),
            body=body,
        )

    m = _KO_RE.search(body)
    if m is not None:
        return SaveIntent(
            trigger=SaveTrigger.KOREAN_PHRASE,
            matched_text=m.group(1),
            body=body,
        )

    m = _EN_RE.search(body)
    if m is not None:
        return SaveIntent(
            trigger=SaveTrigger.ENGLISH_PHRASE,
            matched_text=m.group(1),
            body=body,
        )

    return None


def describe_intent(intent: SaveIntent | None) -> str:
    """One-line human description for log lines / dry-run output."""
    if intent is None:
        return "no-save-intent"
    return (
        f"trigger={intent.trigger.value} "
        f"matched={intent.matched_text!r} "
        f"body_len={len(intent.body)}"
    )


__all__ = [
    "SaveIntent",
    "SaveTrigger",
    "describe_intent",
    "try_extract_save_intent",
]
