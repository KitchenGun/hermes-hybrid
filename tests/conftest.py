"""Shared pytest fixtures for hermes-hybrid tests."""
from __future__ import annotations

import pytest

from src.config import Settings, reset_settings


@pytest.fixture(autouse=True)
def _isolate_experience_log(tmp_path, monkeypatch):
    """Block tests from leaking into the production experience log.

    The default ``Settings()`` has ``experience_log_enabled=True`` and
    ``experience_log_root=./logs/experience`` — running pytest from the
    repo root with that default would have any test that builds an
    ``Orchestrator(settings)`` write JSONL lines into the operator's
    real log file. We caught this on 2026-05-05 after finding 4 stale
    ``skill:boom`` entries in the production log that originated from
    ``tests/test_skills.py``'s _BoomSkill case.

    Setting the env vars here propagates through pydantic-settings to
    every ``Settings()`` instance the test creates, regardless of
    whether it goes through the ``settings`` fixture below.
    """
    monkeypatch.setenv("HERMES_EXPERIENCE_LOG_ENABLED", "false")
    monkeypatch.setenv(
        "HERMES_EXPERIENCE_LOG_ROOT", str(tmp_path / "experience")
    )
    # 2026-05-06: production default master_enabled=True. Tests that build
    # ``Settings(_env_file=None, ...)`` without passing master_enabled
    # would inherit the True default and try to route through the
    # opencode-backed master path, which the rest of the test setup
    # doesn't expect. Force OFF via env so any Settings() built without
    # an explicit override stays on the legacy path.
    monkeypatch.setenv("HERMES_MASTER_ENABLED", "false")


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
        # Defense in depth — even if the autouse env-var fixture is
        # ever bypassed, the explicit settings here keep tests off the
        # production log directory.
        experience_log_enabled=False,
        experience_log_root=tmp_path / "experience",
        # 2026-05-06: production default flipped to True (use_new_job_factory).
        # Most existing orchestrator tests were written against the v1 /
        # router-direct codepath; pin v2 OFF here so they keep exercising
        # that path. Tests that specifically need v2 should either build
        # their own Settings or override this fixture's value.
        use_new_job_factory=False,
        # 2026-05-06: production default flipped to True (master_enabled).
        # Same reasoning — tests written before the master path exists
        # need the legacy dispatch. Master-specific tests build their own
        # Settings (test_hermes_master.py) so they aren't affected.
        master_enabled=False,
    )
    return s
