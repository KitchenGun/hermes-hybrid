"""Mail provider abstraction (Gmail REST + Naver IMAP).

This package is structured so the watcher runtime can poll multiple mail
providers behind a single interface (``MailProvider``). Add new providers
by registering them in ``PROVIDERS``.
"""
from __future__ import annotations

from src.skills.mail.base import MailMessage, MailProvider, MailProviderError
from src.skills.mail.gmail import GmailProvider
from src.skills.mail.naver import NaverProvider

PROVIDERS: dict[str, type[MailProvider]] = {
    "gmail": GmailProvider,
    "naver": NaverProvider,
}

__all__ = [
    "MailMessage",
    "MailProvider",
    "MailProviderError",
    "GmailProvider",
    "NaverProvider",
    "PROVIDERS",
]
