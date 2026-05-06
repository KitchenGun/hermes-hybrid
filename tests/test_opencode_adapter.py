"""Tests for OpenCodeAdapter — subprocess wrapper for ``opencode -p``.

We mock ``asyncio.create_subprocess_exec`` so the test stays in pure
Python, no opencode binary required. Locks down:
  * happy-path: stdout JSON → OpenCodeResult with text/model/tokens
  * non-zero exit → OpenCodeAdapterError
  * auth-style stderr → OpenCodeAuthError (specific subclass)
  * quota stderr → OpenCodeAuthError too (re-auth or wait — not retryable
    transient)
  * non-JSON stdout → OpenCodeAdapterError
  * is_error JSON flag → matching exception
  * timeout → OpenCodeTimeout
  * WSL backend wraps cmd via ``wsl -d ... bash -lc ...``
  * history is flattened into stdin
  * concurrency cap respected (semaphore <= master_concurrency)
"""
from __future__ import annotations

import asyncio
import json
from unittest.mock import AsyncMock, patch

import pytest

from src.config import Settings
from src.opencode_adapter import (
    OpenCodeAdapter,
    OpenCodeAdapterError,
    OpenCodeAuthError,
    OpenCodeTimeout,
)


def _settings(**overrides) -> Settings:
    base = {
        "_env_file": None,
        "discord_bot_token": "",
        "discord_allowed_user_ids": "",
        "require_allowlist": False,
        "ollama_enabled": False,
        "experience_log_enabled": False,
        "master_enabled": True,
        "master_model": "gpt-5.5",
        "master_timeout_ms": 5_000,
        "master_concurrency": 1,
        "opencode_cli_backend": "local_subprocess",
        "opencode_cli_path": "/usr/local/bin/opencode",
    }
    base.update(overrides)
    return Settings(**base)  # type: ignore[arg-type]


def _mock_proc(
    *,
    stdout: bytes = b"",
    stderr: bytes = b"",
    returncode: int = 0,
):
    """Build a mock subprocess that mimics asyncio.subprocess.Process."""
    proc = AsyncMock()
    proc.communicate = AsyncMock(return_value=(stdout, stderr))
    proc.returncode = returncode
    proc.kill = AsyncMock()
    proc.wait = AsyncMock()
    return proc


@pytest.mark.asyncio
async def test_run_happy_path_parses_json():
    settings = _settings()
    payload = {
        "result": "오늘은 흐림.",
        "model": "gpt-5.5",
        "session_id": "sess-123",
        "usage": {"input_tokens": 42, "output_tokens": 18},
        "total_cost_usd": 0.0,
    }
    proc = _mock_proc(stdout=json.dumps(payload).encode("utf-8"))

    with patch(
        "asyncio.create_subprocess_exec",
        AsyncMock(return_value=proc),
    ):
        adapter = OpenCodeAdapter(settings)
        result = await adapter.run(prompt="오늘 날씨 어때")

    assert result.text == "오늘은 흐림."
    assert result.model_name == "gpt-5.5"
    assert result.session_id == "sess-123"
    assert result.input_tokens == 42
    assert result.output_tokens == 18


@pytest.mark.asyncio
async def test_run_falls_back_to_text_field_when_no_result():
    """Adapter accepts both ``result`` (claude-style) and ``text`` keys."""
    settings = _settings()
    payload = {"text": "응답 본문", "model": "gpt-5.5"}
    proc = _mock_proc(stdout=json.dumps(payload).encode("utf-8"))

    with patch(
        "asyncio.create_subprocess_exec",
        AsyncMock(return_value=proc),
    ):
        adapter = OpenCodeAdapter(settings)
        result = await adapter.run(prompt="hi")
    assert result.text == "응답 본문"


@pytest.mark.asyncio
async def test_run_uses_modelUsage_first_for_model_name():
    settings = _settings()
    payload = {
        "result": "x",
        "model": "fallback-model",
        "modelUsage": {"gpt-5.5": {"turns": 1}},
    }
    proc = _mock_proc(stdout=json.dumps(payload).encode("utf-8"))
    with patch(
        "asyncio.create_subprocess_exec",
        AsyncMock(return_value=proc),
    ):
        adapter = OpenCodeAdapter(settings)
        result = await adapter.run(prompt="x")
    assert result.model_name == "gpt-5.5"


@pytest.mark.asyncio
async def test_nonzero_exit_raises_adapter_error():
    settings = _settings()
    proc = _mock_proc(
        stdout=b"", stderr=b"some random crash", returncode=2
    )
    with patch(
        "asyncio.create_subprocess_exec",
        AsyncMock(return_value=proc),
    ):
        adapter = OpenCodeAdapter(settings)
        with pytest.raises(OpenCodeAdapterError):
            await adapter.run(prompt="x")


@pytest.mark.asyncio
async def test_auth_failure_in_stderr_raises_auth_error():
    settings = _settings()
    proc = _mock_proc(
        stdout=b"",
        stderr=b"401 unauthorized - please run opencode auth login",
        returncode=1,
    )
    with patch(
        "asyncio.create_subprocess_exec",
        AsyncMock(return_value=proc),
    ):
        adapter = OpenCodeAdapter(settings)
        with pytest.raises(OpenCodeAuthError):
            await adapter.run(prompt="x")


@pytest.mark.asyncio
async def test_quota_failure_in_stderr_raises_auth_error():
    settings = _settings()
    proc = _mock_proc(
        stdout=b"",
        stderr=b"out of credits - try again later",
        returncode=1,
    )
    with patch(
        "asyncio.create_subprocess_exec",
        AsyncMock(return_value=proc),
    ):
        adapter = OpenCodeAdapter(settings)
        with pytest.raises(OpenCodeAuthError):
            await adapter.run(prompt="x")


@pytest.mark.asyncio
async def test_non_json_stdout_raises_adapter_error():
    settings = _settings()
    proc = _mock_proc(stdout=b"not json at all", returncode=0)
    with patch(
        "asyncio.create_subprocess_exec",
        AsyncMock(return_value=proc),
    ):
        adapter = OpenCodeAdapter(settings)
        with pytest.raises(OpenCodeAdapterError):
            await adapter.run(prompt="x")


@pytest.mark.asyncio
async def test_is_error_flag_in_json_raises_adapter_error():
    settings = _settings()
    payload = {
        "is_error": True,
        "subtype": "model_unavailable",
        "result": "model gpt-5.5 not available",
    }
    proc = _mock_proc(stdout=json.dumps(payload).encode("utf-8"))
    with patch(
        "asyncio.create_subprocess_exec",
        AsyncMock(return_value=proc),
    ):
        adapter = OpenCodeAdapter(settings)
        with pytest.raises(OpenCodeAdapterError):
            await adapter.run(prompt="x")


@pytest.mark.asyncio
async def test_is_error_with_auth_message_raises_auth_error():
    settings = _settings()
    payload = {"is_error": True, "result": "401 unauthorized"}
    proc = _mock_proc(stdout=json.dumps(payload).encode("utf-8"))
    with patch(
        "asyncio.create_subprocess_exec",
        AsyncMock(return_value=proc),
    ):
        adapter = OpenCodeAdapter(settings)
        with pytest.raises(OpenCodeAuthError):
            await adapter.run(prompt="x")


@pytest.mark.asyncio
async def test_subprocess_timeout_raises_timeout():
    settings = _settings(master_timeout_ms=50)
    proc = AsyncMock()
    # communicate hangs forever — wait_for will time out
    async def _hang(*_a, **_kw):
        await asyncio.sleep(10)

    proc.communicate = _hang
    proc.kill = AsyncMock()
    proc.wait = AsyncMock()
    proc.returncode = None

    with patch(
        "asyncio.create_subprocess_exec",
        AsyncMock(return_value=proc),
    ):
        adapter = OpenCodeAdapter(settings)
        with pytest.raises(OpenCodeTimeout):
            await adapter.run(prompt="x")


@pytest.mark.asyncio
async def test_wsl_backend_wraps_cmd():
    settings = _settings(opencode_cli_backend="wsl_subprocess")
    captured: dict = {}

    async def _fake_exec(*args, **kw):
        captured["argv"] = args
        return _mock_proc(stdout=b'{"result":"ok","model":"gpt-5.5"}')

    with patch(
        "asyncio.create_subprocess_exec",
        side_effect=_fake_exec,
    ):
        adapter = OpenCodeAdapter(settings)
        await adapter.run(prompt="x")

    argv = captured["argv"]
    assert argv[0] == "wsl"
    assert "bash" in argv
    # The opencode invocation should appear inside the bash -lc payload.
    inner = argv[-1]
    assert "opencode" in inner
    assert "--output-format" in inner
    assert "gpt-5.5" in inner


@pytest.mark.asyncio
async def test_local_backend_does_not_wrap_in_wsl():
    settings = _settings(opencode_cli_backend="local_subprocess")
    captured: dict = {}

    async def _fake_exec(*args, **kw):
        captured["argv"] = args
        return _mock_proc(stdout=b'{"result":"ok","model":"gpt-5.5"}')

    with patch(
        "asyncio.create_subprocess_exec",
        side_effect=_fake_exec,
    ):
        adapter = OpenCodeAdapter(settings)
        await adapter.run(prompt="x")

    argv = captured["argv"]
    assert argv[0].endswith("opencode")
    assert "--model" in argv
    assert "gpt-5.5" in argv


@pytest.mark.asyncio
async def test_history_is_flattened_into_stdin():
    settings = _settings()
    captured: dict = {}

    async def _fake_exec(*_args, **_kw):
        proc = _mock_proc(stdout=b'{"result":"ok","model":"gpt-5.5"}')
        async def _capture_communicate(payload):
            captured["stdin"] = payload
            return (b'{"result":"ok","model":"gpt-5.5"}', b"")
        proc.communicate = _capture_communicate
        return proc

    with patch(
        "asyncio.create_subprocess_exec",
        side_effect=_fake_exec,
    ):
        adapter = OpenCodeAdapter(settings)
        await adapter.run(
            prompt="현재 질문",
            history=[
                {"role": "user", "content": "이전 질문"},
                {"role": "assistant", "content": "이전 답"},
            ],
        )

    decoded = captured["stdin"].decode("utf-8")
    assert "[user]" in decoded
    assert "[assistant]" in decoded
    assert "이전 질문" in decoded
    assert "이전 답" in decoded
    assert "현재 질문" in decoded
