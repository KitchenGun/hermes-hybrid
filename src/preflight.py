"""Preflight checks (R15) — run at startup; refuse to boot on hard failure.

Phase 8/10 (2026-05-06): master = opencode CLI / gpt-5.5. Hermes CLI
의존 없음 — 관련 check 제거. allowlist + Ollama health (memory embedding
용) + 공식 hermes-gateway 충돌 회피 (R6) 만 남김.
"""
from __future__ import annotations

import asyncio
import shlex
from dataclasses import dataclass

from src.config import Settings
from src.llm.base import LLMError
from src.llm.ollama_client import list_ollama_models
from src.obs import get_logger

log = get_logger(__name__)


@dataclass
class PreflightReport:
    ok: bool
    errors: list[str]
    warnings: list[str]


async def _wsl_run(settings: Settings, cmd: str, timeout: float = 10.0) -> tuple[int, str, str]:
    args = ["wsl", "-d", settings.wsl_distro, "bash", "-lc", cmd]
    proc = await asyncio.create_subprocess_exec(
        *args, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
    )
    try:
        out, err = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        proc.kill(); await proc.wait()
        return 124, "", "timeout"
    return proc.returncode or 0, out.decode("utf-8", "replace"), err.decode("utf-8", "replace")


async def gateway_is_running(settings: Settings) -> bool:
    """R6: 공식 hermes-gateway 가 같은 Discord token 으로 봇 실행 중인지 검사.
    설치만 돼있고 비활성이면 False."""
    rc, _out, _err = await _wsl_run(
        settings,
        f"systemctl --user is-active {shlex.quote(settings.gateway_service_name)} 2>/dev/null",
    )
    return rc == 0


async def stop_official_gateway(settings: Settings) -> tuple[bool, str]:
    """R6: stop (and disable) the official hermes-gateway so it doesn't race us."""
    rc, out, err = await _wsl_run(
        settings,
        f"systemctl --user stop {shlex.quote(settings.gateway_service_name)}; "
        f"systemctl --user disable {shlex.quote(settings.gateway_service_name)} 2>&1 || true",
    )
    msg = (out + err).strip().splitlines()[-1] if (out + err).strip() else ""
    return rc in (0, 1, 5), msg  # 'stop' exits 0; 'disable' may already be disabled


async def run_preflight(settings: Settings, *, require_gateway_stopped: bool) -> PreflightReport:
    errors: list[str] = []
    warnings: list[str] = []

    # R12: allowlist fail-closed
    if settings.require_allowlist and not settings.allowed_user_ids:
        errors.append(
            "REQUIRE_ALLOWLIST=true but DISCORD_ALLOWED_USER_IDS is empty. "
            "Set user IDs or explicitly set REQUIRE_ALLOWLIST=false to disable the gate."
        )

    # Ollama health (only when enabled — memory embedding 용 fallback).
    # Phase 8 후 master = opencode 라 본 메시지 처리에는 Ollama 불필요.
    # `memory_search_backend=embedding` + bge-m3 사용 시에만 의미.
    if settings.ollama_enabled:
        try:
            installed = await list_ollama_models(settings.ollama_base_url)
        except LLMError as e:
            warnings.append(
                f"OLLAMA_ENABLED=true but Ollama server unreachable "
                f"({settings.ollama_base_url}): {e}. Memory embedding fallback "
                "won't work; master path is unaffected."
            )
        else:
            # bge-m3 누락 시 warning (memory embedding 사용자만 영향)
            if (
                settings.memory_search_backend == "embedding"
                and settings.memory_embedding_model not in installed
            ):
                warnings.append(
                    f"memory_search_backend=embedding but "
                    f"{settings.memory_embedding_model!r} not pulled in Ollama. "
                    f"Run `ollama pull {settings.memory_embedding_model}` "
                    "or switch to memory_search_backend=like."
                )

    # R6: 공식 hermes-gateway 충돌 회피
    if require_gateway_stopped:
        if await gateway_is_running(settings):
            stopped, msg = await stop_official_gateway(settings)
            if not stopped:
                errors.append(
                    f"official hermes-gateway is running and could not be stopped: {msg}"
                )
            else:
                warnings.append(f"stopped official hermes-gateway: {msg}")

    return PreflightReport(ok=not errors, errors=errors, warnings=warnings)
