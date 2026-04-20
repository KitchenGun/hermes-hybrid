"""FIX#4: HeavySessionRegistry + ClaudeCodeResumeFailed integration tests.

Covers:
  - Registry pick/record/invalidate and the 10-min TTL.
  - Adapter builds ``--resume`` correctly and drops ``--no-session-persistence``.
  - Resume-failed stderr patterns surface as :class:`ClaudeCodeResumeFailed`.
  - Orchestrator's heavy path reuses prior session on 2nd turn and falls back
    to fresh on resume failure (no user-visible error).
"""
from __future__ import annotations

from typing import Any

import pytest

from src.claude_adapter import ClaudeCodeResult, ClaudeCodeResumeFailed
from src.claude_adapter.adapter import ClaudeCodeAdapter
from src.config import Settings
from src.orchestrator.heavy_session import HeavySessionRegistry


# ----------------------------------------------------------------------
# Registry unit tests
# ----------------------------------------------------------------------


def test_registry_empty_returns_none():
    reg = HeavySessionRegistry()
    assert reg.pick("u1") is None
    assert reg.size() == 0


def test_registry_record_then_pick_returns_session():
    reg = HeavySessionRegistry()
    reg.record("u1", "sid-abc", now=1000.0)
    assert reg.pick("u1", now=1005.0) == "sid-abc"


def test_registry_expired_returns_none():
    """Beyond the 10-min window, stored sessions are ignored."""
    reg = HeavySessionRegistry(window_sec=600)
    reg.record("u1", "sid-abc", now=1000.0)
    # 601 seconds later — just past the window
    assert reg.pick("u1", now=1601.0) is None


def test_registry_is_per_user():
    reg = HeavySessionRegistry()
    reg.record("u1", "sid-one", now=1000.0)
    reg.record("u2", "sid-two", now=1000.0)
    assert reg.pick("u1", now=1001.0) == "sid-one"
    assert reg.pick("u2", now=1001.0) == "sid-two"


def test_registry_invalidate_drops_entry():
    reg = HeavySessionRegistry()
    reg.record("u1", "sid-old", now=1000.0)
    reg.invalidate("u1", reason="test")
    assert reg.pick("u1", now=1005.0) is None
    assert reg.size() == 0


def test_registry_record_overwrites_prior():
    """A new session for the same user replaces the old one — we never keep
    stale ids around once the user's started a new thread."""
    reg = HeavySessionRegistry()
    reg.record("u1", "sid-old", now=1000.0)
    reg.record("u1", "sid-new", now=2000.0)
    assert reg.pick("u1", now=2005.0) == "sid-new"
    assert reg.size() == 1


# ----------------------------------------------------------------------
# Adapter builds --resume correctly
# ----------------------------------------------------------------------


def test_build_cmd_adds_resume_and_drops_no_persistence(settings: Settings):
    a = ClaudeCodeAdapter(settings)
    cmd = a._build_cmd(model="sonnet", resume_session_id="sid-abc", persist_session=False)
    joined = " ".join(cmd)
    assert "--resume sid-abc" in joined
    assert "--no-session-persistence" not in joined  # critical for resume


def test_build_cmd_persist_session_drops_no_persistence(settings: Settings):
    """Even without a resume id, asking for persistence drops the
    no-persistence flag so the new session sticks around for reuse."""
    a = ClaudeCodeAdapter(settings)
    cmd = a._build_cmd(model="sonnet", resume_session_id=None, persist_session=True)
    joined = " ".join(cmd)
    assert "--no-session-persistence" not in joined
    assert "--resume" not in joined


def test_build_cmd_stateless_default_keeps_no_persistence(settings: Settings):
    a = ClaudeCodeAdapter(settings)
    cmd = a._build_cmd(model="sonnet")
    assert "--no-session-persistence" in " ".join(cmd)


# ----------------------------------------------------------------------
# Adapter raises ClaudeCodeResumeFailed on session-missing patterns
# ----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_run_raises_resume_failed_on_session_not_found(
    settings: Settings, monkeypatch
):
    a = ClaudeCodeAdapter(settings)

    async def fake_exec(*cmd, **kwargs):
        class _Proc:
            returncode = 1
            async def communicate(self, _input=None):
                return (b"", b"Error: session not found: sid-abc\n")
        return _Proc()

    monkeypatch.setattr("asyncio.create_subprocess_exec", fake_exec)

    with pytest.raises(ClaudeCodeResumeFailed):
        await a.run(prompt="hi", resume_session_id="sid-abc", persist_session=True)


@pytest.mark.asyncio
async def test_run_raises_resume_failed_on_is_error_payload(
    settings: Settings, monkeypatch
):
    """Some CLI builds return JSON with ``is_error=true`` + a missing-session
    message instead of non-zero exit; must still raise ResumeFailed."""
    a = ClaudeCodeAdapter(settings)

    async def fake_exec(*cmd, **kwargs):
        class _Proc:
            returncode = 0
            async def communicate(self, _input=None):
                payload = (
                    b'{"is_error": true, "result": "No such session: sid-abc", '
                    b'"subtype": "resume_error"}'
                )
                return (payload, b"")
        return _Proc()

    monkeypatch.setattr("asyncio.create_subprocess_exec", fake_exec)

    with pytest.raises(ClaudeCodeResumeFailed):
        await a.run(prompt="hi", resume_session_id="sid-abc", persist_session=True)


# ----------------------------------------------------------------------
# Orchestrator heavy path: reuse + fresh-fallback
# ----------------------------------------------------------------------


class _ScriptedClaude:
    """Thin recorder compatible with ``ClaudeCodeAdapter.run`` signature."""

    def __init__(self, scripts: list[ClaudeCodeResult | Exception]):
        self._scripts = list(scripts)
        self.calls: list[dict[str, Any]] = []

    async def run(
        self,
        *,
        prompt: str,
        history: list[dict[str, str]] | None = None,
        model: str | None = None,
        timeout_ms: int | None = None,
        resume_session_id: str | None = None,
        persist_session: bool = False,
    ) -> ClaudeCodeResult:
        self.calls.append({
            "prompt": prompt,
            "resume_session_id": resume_session_id,
            "persist_session": persist_session,
        })
        r = self._scripts.pop(0)
        if isinstance(r, Exception):
            raise r
        return r


def _cr(text: str, sid: str) -> ClaudeCodeResult:
    return ClaudeCodeResult(
        text=text, model_name="claude-sonnet-4-6", session_id=sid,
        duration_ms=1, input_tokens=5, output_tokens=10,
    )


@pytest.mark.asyncio
async def test_heavy_second_turn_reuses_session(settings: Settings):
    """Second !heavy within the window → adapter called with prior session_id."""
    from src.orchestrator import Orchestrator

    o = Orchestrator(settings)
    fake = _ScriptedClaude([_cr("first", "sid-1"), _cr("second", "sid-1")])
    o.claude_code = fake  # type: ignore[assignment]

    await o.handle("first prompt", user_id="u1", heavy=True)
    assert fake.calls[0]["resume_session_id"] is None  # first turn → fresh
    assert fake.calls[0]["persist_session"] is True

    await o.handle("second prompt", user_id="u1", heavy=True)
    assert fake.calls[1]["resume_session_id"] == "sid-1"  # reused!
    assert fake.calls[1]["persist_session"] is True


@pytest.mark.asyncio
async def test_heavy_resume_failure_falls_back_to_fresh(settings: Settings):
    """When --resume fails, orchestrator invalidates the registry and retries
    without --resume. The user sees a normal response, not an error."""
    from src.orchestrator import Orchestrator

    o = Orchestrator(settings)
    # Pre-seed the registry so the next call tries to resume.
    o.heavy_sessions.record("u1", "sid-stale", now=0.0)
    # Monkey-patch pick() to always hand out the stale id.
    o.heavy_sessions.pick = lambda user_id, now=None: "sid-stale"  # type: ignore[assignment]

    fake = _ScriptedClaude([
        ClaudeCodeResumeFailed("session not found: sid-stale"),
        _cr("fresh reply", "sid-new"),
    ])
    o.claude_code = fake  # type: ignore[assignment]

    result = await o.handle("go deep", user_id="u1", heavy=True)
    assert result.handled_by == "claude-max"
    assert result.response == "fresh reply"
    # Two calls: resume attempt (failed), then fresh retry.
    assert len(fake.calls) == 2
    assert fake.calls[0]["resume_session_id"] == "sid-stale"
    assert fake.calls[1]["resume_session_id"] is None
    # Registry now points at the new session.
    assert o.heavy_sessions.peek("u1") == "sid-new"
