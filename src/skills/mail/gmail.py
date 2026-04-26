"""Gmail provider via google-api-python-client.

OAuth tokens live per-account on disk (path declared in accounts.yaml).
The OAuth client secret (``credentials_file``) is a GCP project artifact
and may be shared across multiple Gmail accounts.

Read-only: scope is ``gmail.readonly`` only; the provider never modifies
mail state. Spam/Trash are excluded explicitly via the search query.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from pathlib import Path

from src.skills.mail.base import MailMessage, MailProviderError

log = logging.getLogger(__name__)

GMAIL_SCOPES = ["https://www.googleapis.com/auth/gmail.readonly"]
_INBOX_QUERY = "in:inbox -in:spam -in:trash -category:promotions"


def _lazy_imports() -> tuple[object, object, object]:
    """Import google-api packages lazily so the rest of the codebase
    doesn't pay an import cost when mail support isn't in use.
    """
    try:
        from google.auth.transport.requests import Request  # type: ignore
        from google.oauth2.credentials import Credentials  # type: ignore
        from googleapiclient.discovery import build  # type: ignore
    except ImportError as e:  # pragma: no cover
        raise MailProviderError(
            "Gmail provider requires the 'mail' extras. "
            "Install with: pip install -e .[mail]"
        ) from e
    return Request, Credentials, build


class GmailProvider:
    name = "gmail"

    def __init__(
        self,
        *,
        account: str,
        address: str,
        token_file: str,
        credentials_file: str = "",
    ):
        self.account = account
        self.address = address
        self.token_file = Path(token_file)
        self.credentials_file = Path(credentials_file) if credentials_file else None

    def _service(self):  # type: ignore[no-untyped-def]
        Request, Credentials, build = _lazy_imports()
        if not self.token_file.exists():
            raise MailProviderError(
                f"Gmail token not found for account '{self.account}': {self.token_file}. "
                "Run scripts/setup_mail_accounts.py --account "
                f"{self.account} --auth"
            )
        creds = Credentials.from_authorized_user_file(  # type: ignore[attr-defined]
            str(self.token_file), GMAIL_SCOPES
        )
        if not creds.valid:
            if creds.expired and creds.refresh_token:
                creds.refresh(Request())  # type: ignore[call-arg]
                self.token_file.write_text(creds.to_json(), encoding="utf-8")
            else:
                raise MailProviderError(
                    f"Gmail credentials invalid for '{self.account}'. Re-run --auth."
                )
        return build("gmail", "v1", credentials=creds, cache_discovery=False)  # type: ignore[operator]

    def list_new_since(
        self,
        last_message_id: str | None,
        *,
        limit: int = 20,
    ) -> list[MailMessage]:
        try:
            svc = self._service()
            resp = svc.users().messages().list(  # type: ignore[attr-defined]
                userId="me",
                q=_INBOX_QUERY,
                maxResults=limit,
            ).execute()
        except MailProviderError:
            raise
        except Exception as e:  # noqa: BLE001
            raise MailProviderError(f"Gmail list failed for '{self.account}': {e}") from e

        ids = [m["id"] for m in resp.get("messages") or []]
        if not ids:
            return []

        if last_message_id and last_message_id in ids:
            cutoff = ids.index(last_message_id)
            ids = ids[:cutoff]
        if not ids:
            return []

        messages: list[MailMessage] = []
        for mid in ids:
            try:
                m = svc.users().messages().get(  # type: ignore[attr-defined]
                    userId="me",
                    id=mid,
                    format="metadata",
                    metadataHeaders=["Subject", "From", "Date"],
                ).execute()
            except Exception as e:  # noqa: BLE001
                log.warning("gmail.fetch_failed", extra={"account": self.account, "mid": mid, "err": str(e)})
                continue
            messages.append(self._to_message(m))
        return messages

    def _to_message(self, raw: dict) -> MailMessage:  # type: ignore[type-arg]
        headers = {h["name"].lower(): h["value"] for h in raw.get("payload", {}).get("headers", [])}
        ts_ms = int(raw.get("internalDate", "0"))
        received = datetime.fromtimestamp(ts_ms / 1000, tz=timezone.utc) if ts_ms else datetime.now(tz=timezone.utc)
        return MailMessage(
            provider=self.name,
            account=self.account,
            address=self.address,
            message_id=raw["id"],
            subject=headers.get("subject", "(no subject)"),
            sender=headers.get("from", ""),
            snippet=raw.get("snippet", ""),
            received_at=received,
        )
