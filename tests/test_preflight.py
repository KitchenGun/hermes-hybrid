"""Preflight tests (R6 + R12 + R15).

We patch `_wsl_run` so tests don't actually shell out to WSL.
"""
from __future__ import annotations

import pytest

from src.config import Settings
from src import preflight as pf


@pytest.mark.asyncio
async def test_fail_closed_allowlist_blocks_boot(settings: Settings, monkeypatch):
    """R12: require_allowlist=true + empty allowlist → hard error."""
    settings.require_allowlist = True
    settings.discord_allowed_user_ids = ""

    async def fake_wsl(_s, _cmd, timeout=10.0):
        return 0, "ok", ""  # pretend Hermes CLI is there

    monkeypatch.setattr(pf, "_wsl_run", fake_wsl)

    report = await pf.run_preflight(settings, require_gateway_stopped=False)
    assert report.ok is False
    assert any("ALLOWLIST" in e or "allowlist" in e.lower() for e in report.errors)


@pytest.mark.asyncio
async def test_allowlist_with_ids_passes(settings: Settings, monkeypatch):
    settings.require_allowlist = True
    settings.discord_allowed_user_ids = "12345"

    async def fake_wsl(_s, _cmd, timeout=10.0):
        return 0, "ok", ""

    monkeypatch.setattr(pf, "_wsl_run", fake_wsl)

    report = await pf.run_preflight(settings, require_gateway_stopped=False)
    assert report.ok is True


@pytest.mark.asyncio
async def test_hermes_cli_missing_is_hard_error(settings: Settings, monkeypatch):
    async def fake_wsl(_s, cmd, timeout=10.0):
        if "test -x" in cmd:
            return 1, "", "not found"
        return 0, "", ""

    monkeypatch.setattr(pf, "_wsl_run", fake_wsl)

    report = await pf.run_preflight(settings, require_gateway_stopped=False)
    assert report.ok is False
    assert any("hermes" in e.lower() for e in report.errors)


# 2026-05-04: test_missing_api_keys_warn_not_fail removed when OpenAI/Anthropic
# preflight warnings were dropped (API legacy purged — Claude CLI uses Max OAuth
# checked via Hermes CLI reachability, not API keys).


@pytest.mark.asyncio
async def test_gateway_running_gets_stopped(settings: Settings, monkeypatch):
    """R6: if the official hermes-gateway is alive, we stop+disable it."""
    stopped_calls: list[str] = []

    async def fake_wsl(_s, cmd, timeout=10.0):
        if "test -x" in cmd:
            return 0, "ok", ""
        if "is-active" in cmd:
            return 0, "active", ""  # running
        if "stop" in cmd and "disable" in cmd:
            stopped_calls.append(cmd)
            return 0, "", ""
        return 0, "", ""

    monkeypatch.setattr(pf, "_wsl_run", fake_wsl)

    report = await pf.run_preflight(settings, require_gateway_stopped=True)
    assert report.ok is True
    assert stopped_calls  # stop was attempted
    assert any("stopped" in w.lower() for w in report.warnings)


@pytest.mark.asyncio
async def test_ollama_unreachable_is_hard_error_when_enabled(settings: Settings, monkeypatch):
    """OLLAMA_ENABLED=true + server down → refuse to boot (we'd otherwise
    silently fall through to cloud for every request)."""
    from src.llm.base import LLMConnectionError
    settings.ollama_enabled = True

    async def fake_wsl(_s, _cmd, timeout=10.0):
        return 0, "ok", ""

    async def fake_list(_url, timeout=5.0):
        raise LLMConnectionError("connection refused")

    monkeypatch.setattr(pf, "_wsl_run", fake_wsl)
    monkeypatch.setattr(pf, "list_ollama_models", fake_list)

    report = await pf.run_preflight(settings, require_gateway_stopped=False)
    assert report.ok is False
    assert any("ollama" in e.lower() for e in report.errors)


@pytest.mark.asyncio
async def test_ollama_missing_models_warns_but_passes(settings: Settings, monkeypatch):
    """Server up but a model isn't pulled → warn so user sees, but don't block boot
    (orchestrator will either get a 404 on that model and tier-up, or user will
    fix it). Don't refuse to boot for something recoverable."""
    settings.ollama_enabled = True

    async def fake_wsl(_s, _cmd, timeout=10.0):
        return 0, "ok", ""

    async def fake_list(_url, timeout=5.0):
        # Only the 7B is pulled; 14B and 32B are missing.
        return [settings.ollama_router_model]

    monkeypatch.setattr(pf, "_wsl_run", fake_wsl)
    monkeypatch.setattr(pf, "list_ollama_models", fake_list)

    report = await pf.run_preflight(settings, require_gateway_stopped=False)
    assert report.ok is True
    assert any("not pulled" in w for w in report.warnings)


@pytest.mark.asyncio
async def test_ollama_not_checked_when_disabled(settings: Settings, monkeypatch):
    """OLLAMA_ENABLED=false → we must not probe Ollama at all (the user may not
    even have it installed yet)."""
    settings.ollama_enabled = False
    called = {"n": 0}

    async def fake_wsl(_s, _cmd, timeout=10.0):
        return 0, "ok", ""

    async def fake_list(_url, timeout=5.0):
        called["n"] += 1
        return []

    monkeypatch.setattr(pf, "_wsl_run", fake_wsl)
    monkeypatch.setattr(pf, "list_ollama_models", fake_list)

    report = await pf.run_preflight(settings, require_gateway_stopped=False)
    assert report.ok is True
    assert called["n"] == 0


@pytest.mark.asyncio
async def test_gateway_not_running_no_stop_attempt(settings: Settings, monkeypatch):
    async def fake_wsl(_s, cmd, timeout=10.0):
        if "test -x" in cmd:
            return 0, "ok", ""
        if "is-active" in cmd:
            return 3, "inactive", ""  # not running
        if "stop" in cmd:
            raise AssertionError("should not attempt stop when gateway is inactive")
        return 0, "", ""

    monkeypatch.setattr(pf, "_wsl_run", fake_wsl)

    report = await pf.run_preflight(settings, require_gateway_stopped=True)
    assert report.ok is True
