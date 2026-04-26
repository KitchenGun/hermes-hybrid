"""Mail provider contract.

A ``MailProvider`` exposes the minimum surface the watcher runtime needs:
list new INBOX messages since a checkpoint. Concrete providers may
expose more (search, send, label management) via the ``MailSkill`` on
the on_demand path, but the watcher only relies on this protocol.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Protocol, runtime_checkable


class MailProviderError(RuntimeError):
    """Raised when a mail provider cannot complete a request.

    Watcher runtime catches this per-account so one bad mailbox does not
    take down the whole watcher.
    """


@dataclass(frozen=True)
class MailMessage:
    provider: str       # "gmail" | "naver" | ...
    account: str        # account name from accounts.yaml
    address: str        # the mailbox address (you@gmail.com)
    message_id: str     # provider-stable id used as dedup key
    subject: str
    sender: str
    snippet: str
    received_at: datetime


@runtime_checkable
class MailProvider(Protocol):
    """Polling surface for the watcher runtime.

    Implementations must be safe to instantiate per poll (cheap), and
    ``list_new_since`` must be safe to call concurrently with other
    providers (no shared mutable state).
    """

    name: str
    account: str
    address: str

    def list_new_since(
        self,
        last_message_id: str | None,
        *,
        limit: int = 20,
    ) -> list[MailMessage]:
        """Return INBOX messages newer than ``last_message_id``.

        - INBOX only — spam folders are excluded by querying the inbox
          label / folder directly (no programmatic spam classification).
        - Newest first. ``limit`` caps the result to avoid flooding the
          notification channel after long downtime.
        - When ``last_message_id`` is None, return at most ``limit`` of
          the most recent messages — the runtime treats this first
          batch as "the high-water mark" and does NOT notify on it.
        """
        ...
