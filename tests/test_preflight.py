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


# Phase 8/10 (2026-05-06) — hermes CLI 의존 폐기. master = opencode CLI 라
# preflight 가 hermes 존재를 검사하지 않음. test_hermes_cli_missing_is_hard_error
# 는 그래서 제거됨.


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
async def test_ollama_unreachable_warns_when_enabled(settings: Settings, monkeypatch):
    """Phase 8/10 후 Ollama 는 memory embedding fallback 용 — 핫패스 X.
    서버 다운이면 warning 만 (master 영향 없음)."""
    from src.llm.base import LLMConnectionError
    settings.ollama_enabled = True

    async def fake_wsl(_s, _cmd, timeout=10.0):
        return 0, "ok", ""

    async def fake_list(_url, timeout=5.0):
        raise LLMConnectionError("connection refused")

    monkeypatch.setattr(pf, "_wsl_run", fake_wsl)
    monkeypatch.setattr(pf, "list_ollama_models", fake_list)

    report = await pf.run_preflight(settings, require_gateway_stopped=False)
    assert report.ok is True   # 더 이상 hard error 가 아님
    assert any("ollama" in w.lower() for w in report.warnings)


@pytest.mark.asyncio
async def test_ollama_missing_embedding_model_warns_when_embedding_backend(
    settings: Settings, monkeypatch,
):
    """memory_search_backend=embedding + bge-m3 가 안 pulled → warn."""
    settings.ollama_enabled = True
    settings.memory_search_backend = "embedding"
    settings.memory_embedding_model = "bge-m3"

    async def fake_wsl(_s, _cmd, timeout=10.0):
        return 0, "ok", ""

    async def fake_list(_url, timeout=5.0):
        return ["llama3.2:3b"]   # bge-m3 미설치

    monkeypatch.setattr(pf, "_wsl_run", fake_wsl)
    monkeypatch.setattr(pf, "list_ollama_models", fake_list)

    report = await pf.run_preflight(settings, require_gateway_stopped=False)
    assert report.ok is True
    assert any("bge-m3" in w for w in report.warnings)


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
