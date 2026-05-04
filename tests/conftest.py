"""Shared pytest fixtures for hermes-hybrid tests."""
from __future__ import annotations

import pytest

from src.config import Settings, reset_settings


@pytest.fixture
def settings(tmp_path) -> Settings:
    """Hermetic settings — no env file, DB in tmp."""
    reset_settings()
    s = Settings(
        _env_file=None,  # type: ignore[call-arg]
        discord_bot_token="",
        discord_allowed_user_ids="",
        require_allowlist=False,
        ollama_enabled=False,
        state_db_path=tmp_path / "test.db",
    )
    return s
