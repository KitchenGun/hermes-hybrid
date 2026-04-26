"""Load mail account configuration from ``profiles/<id>/accounts.yaml``.

YAML schema:

    accounts:
      - name: personal_gmail
        provider: gmail
        address: you@gmail.com
        token_file: ./secrets/gmail_personal_token.json
        credentials_file: ./secrets/google_oauth_client.json
      - name: personal_naver
        provider: naver
        address: you@naver.com
        password_env: NAVER_APP_PASSWORD

Secrets are NEVER stored inline. Naver passwords are referenced by
environment variable name (``password_env``), Google tokens are
referenced by file path (the JSON file holds the token).
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path

import yaml

from src.skills.mail import PROVIDERS
from src.skills.mail.base import MailProvider, MailProviderError

log = logging.getLogger(__name__)


class AccountConfigError(ValueError):
    """Raised when accounts.yaml is malformed or references unknown providers."""


@dataclass(frozen=True)
class AccountConfig:
    name: str
    provider: str
    address: str
    raw: dict


class AccountLoader:
    """Read accounts.yaml for a profile and instantiate ``MailProvider``s on demand."""

    def __init__(self, profile_dir: Path):
        self.profile_dir = Path(profile_dir)
        self.accounts_file = self.profile_dir / "accounts.yaml"

    def load(self) -> dict[str, AccountConfig]:
        if not self.accounts_file.exists():
            return {}
        try:
            data = yaml.safe_load(self.accounts_file.read_text(encoding="utf-8")) or {}
        except yaml.YAMLError as e:
            raise AccountConfigError(f"failed to parse {self.accounts_file}: {e}") from e
        accounts = data.get("accounts") or []
        if not isinstance(accounts, list):
            raise AccountConfigError(
                f"{self.accounts_file}: 'accounts' must be a list"
            )
        out: dict[str, AccountConfig] = {}
        for item in accounts:
            if not isinstance(item, dict):
                raise AccountConfigError(
                    f"{self.accounts_file}: each account must be a mapping"
                )
            name = str(item.get("name") or "").strip()
            provider = str(item.get("provider") or "").strip().lower()
            address = str(item.get("address") or "").strip()
            if not name or not provider or not address:
                raise AccountConfigError(
                    f"{self.accounts_file}: account missing name/provider/address"
                )
            if provider not in PROVIDERS:
                raise AccountConfigError(
                    f"{self.accounts_file}: unknown provider '{provider}' for '{name}'"
                )
            if name in out:
                raise AccountConfigError(
                    f"{self.accounts_file}: duplicate account name '{name}'"
                )
            out[name] = AccountConfig(
                name=name, provider=provider, address=address, raw=item
            )
        return out

    def build(self, account: AccountConfig) -> MailProvider:
        cls = PROVIDERS[account.provider]
        kwargs = {"account": account.name, "address": account.address}
        if account.provider == "gmail":
            token_file = account.raw.get("token_file")
            if not token_file:
                raise MailProviderError(
                    f"gmail account '{account.name}' missing 'token_file'"
                )
            kwargs["token_file"] = str(self._resolve(token_file))
            cred = account.raw.get("credentials_file")
            if cred:
                kwargs["credentials_file"] = str(self._resolve(cred))
        elif account.provider == "naver":
            pw_env = account.raw.get("password_env")
            if not pw_env:
                raise MailProviderError(
                    f"naver account '{account.name}' missing 'password_env'"
                )
            kwargs["password_env"] = pw_env
            host = account.raw.get("host")
            if host:
                kwargs["host"] = host
            port = account.raw.get("port")
            if port:
                kwargs["port"] = int(port)
        return cls(**kwargs)  # type: ignore[arg-type]

    def _resolve(self, path_str: str) -> Path:
        p = Path(path_str)
        if p.is_absolute():
            return p
        # Relative paths are interpreted relative to the project root
        # (cwd when the bot runs), matching how Settings paths behave.
        return Path.cwd() / p
