"""Naver mail provider via IMAP (imap_tools wrapper).

Naver does not expose a public REST API for personal @naver.com mailboxes
(developer center: search/login/maps only; Cloud Outbound Mailer is
send-only; Works Mail API is for enterprise domains). IMAP is therefore
the only ingestion path.

Auth model: Naver app password (8 chars), generated under
Naver-account-security → Application Passwords AFTER turning on
IMAP/SMTP under Naver Mail settings AND enabling 2FA on the account.
The regular login password will not work.

Connection model: connection-per-poll. We do not hold an IMAP IDLE
session — Naver IMAP is well-behaved for short transactions but flakey
for long-lived ones, so each ``list_new_since`` opens, queries, and
closes its own connection. The ``imap_tools.MailBox`` context manager
handles cleanup automatically.

Why imap_tools over stdlib imaplib: structured ``MailMessage`` attributes
(subject/from_/date already RFC 2047 decoded), UID-based query builder,
charset-aware search. Strips ~70 lines of manual MIME decoding our
previous implementation maintained.
"""
from __future__ import annotations

import logging
import os
from datetime import datetime, timezone

from src.skills.mail.base import MailMessage, MailProviderError

log = logging.getLogger(__name__)

DEFAULT_HOST = "imap.naver.com"
DEFAULT_PORT = 993


def _lazy_imap_tools():  # type: ignore[no-untyped-def]
    """Defer the imap_tools import so the rest of the codebase doesn't
    pay an import cost when mail support isn't installed.
    """
    try:
        from imap_tools import MailBox, AND  # type: ignore
    except ImportError as e:  # pragma: no cover
        raise MailProviderError(
            "Naver provider requires the 'mail' extras. "
            "Install with: pip install -e .[mail]"
        ) from e
    return MailBox, AND


class NaverProvider:
    name = "naver"

    def __init__(
        self,
        *,
        account: str,
        address: str,
        password_env: str,
        host: str = DEFAULT_HOST,
        port: int = DEFAULT_PORT,
    ):
        self.account = account
        self.address = address
        self.password_env = password_env
        self.host = host
        self.port = port

    def _password(self) -> str:
        pw = os.environ.get(self.password_env, "").strip()
        if not pw:
            raise MailProviderError(
                f"Naver app password not set for account '{self.account}': "
                f"environment variable {self.password_env} is empty. "
                "Issue an app password under Naver account security."
            )
        return pw

    def list_new_since(
        self,
        last_message_id: str | None,
        *,
        limit: int = 20,
    ) -> list[MailMessage]:
        MailBox, _AND = _lazy_imap_tools()

        try:
            with MailBox(self.host, self.port).login(
                self.address, self._password(), initial_folder="INBOX"
            ) as mb:
                criteria = self._criteria(last_message_id)
                msgs = list(
                    mb.fetch(
                        criteria=criteria,
                        limit=limit,
                        reverse=True,           # newest first
                        headers_only=True,      # avoid downloading bodies
                        mark_seen=False,        # read-only — never STORE \\Seen
                        bulk=False,
                        charset="UTF-8",
                    )
                )
        except MailProviderError:
            raise
        except Exception as e:  # noqa: BLE001
            # imap_tools raises subclasses of imaplib.IMAP4.error and OSError;
            # surface them as MailProviderError so the watcher loop logs and
            # continues for other accounts.
            raise MailProviderError(
                f"Naver IMAP failed for '{self.account}' ({self.address}): {e}"
            ) from e

        out: list[MailMessage] = []
        for m in msgs:
            uid = (m.uid or "").strip()
            if not uid:
                continue
            received = m.date or datetime.now(tz=timezone.utc)
            if received.tzinfo is None:
                received = received.replace(tzinfo=timezone.utc)
            out.append(
                MailMessage(
                    provider=self.name,
                    account=self.account,
                    address=self.address,
                    message_id=uid,
                    subject=(m.subject or "(no subject)").strip(),
                    sender=_format_sender(m.from_, m.from_values),
                    snippet="",  # headers_only=True → body not fetched
                    received_at=received,
                )
            )
        return out

    def _criteria(self, last_message_id: str | None) -> str:
        """Build IMAP criteria string.

        Naver IMAP supports raw UID range syntax which gives a tight
        "everything strictly newer than the last seen UID" query without
        re-listing the whole INBOX.
        """
        if last_message_id and last_message_id.isdigit():
            return f"UID {int(last_message_id) + 1}:*"
        return "ALL"


def _format_sender(from_str: str | None, from_values) -> str:
    """Pretty-format the ``From`` header.

    ``imap_tools`` already RFC 2047-decodes the raw header. ``from_values``
    is an ``EmailAddress`` (name + email) when parseable. Prefer the
    structured form, fall back to the raw decoded string.
    """
    if from_values is not None:
        name = (getattr(from_values, "name", "") or "").strip()
        email_addr = (getattr(from_values, "email", "") or "").strip()
        if name and email_addr:
            return f"{name} <{email_addr}>"
        if email_addr:
            return email_addr
        if name:
            return name
    return (from_str or "").strip()
