"""Preflight checks (R15) — run at startup; refuse to boot on hard failure.

Also provides a helper to stop the conflicting official Hermes Discord
gateway service (R6) so we don't end up with two bots on the same token.
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
    args = ["wsl", "-d", settings.hermes_wsl_distro, "bash", "-lc", cmd]
    proc = await asyncio.create_subprocess_exec(
        *args, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
    )
    try:
        out, err = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        proc.kill(); await proc.wait()
        return 124, "", "timeout"
    return proc.returncode or 0, out.decode("utf-8", "replace"), err.decode("utf-8", "replace")


async def check_hermes_available(settings: Settings) -> tuple[bool, str]:
    rc, out, err = await _wsl_run(settings, f"test -x {shlex.quote(settings.hermes_cli_path)} && echo ok")
    if rc == 0 and "ok" in out:
        return True, ""
    return False, f"hermes CLI not executable at {settings.hermes_cli_path}: {err or out}"


async def check_hermes_doctor(settings: Settings) -> tuple[bool, str]:
    rc, out, err = await _wsl_run(
        settings, f"{shlex.quote(settings.hermes_cli_path)} doctor 2>&1 | tail -5", timeout=30.0
    )
    if rc != 0:
        return False, f"hermes doctor failed: {(err or out)[-200:]}"
    return True, ""


async def gateway_is_running(settings: Settings) -> bool:
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

    # API keys
    if not settings.openai_api_key:
        warnings.append("OPENAI_API_KEY not set — C1 + L2/L3 surrogates unavailable")
    if not settings.anthropic_api_key:
        warnings.append("ANTHROPIC_API_KEY not set — C2 unavailable")

    # Hermes CLI reachability
    ok, reason = await check_hermes_available(settings)
    if not ok:
        errors.append(reason)

    # Ollama health (only when enabled)
    if settings.ollama_enabled:
        try:
            installed = await list_ollama_models(settings.ollama_base_url)
        except LLMError as e:
            errors.append(
                f"OLLAMA_ENABLED=true but Ollama server is unreachable "
                f"({settings.ollama_base_url}): {e}"
            )
        else:
            required = [
                settings.ollama_router_model,
                settings.ollama_work_model,
                settings.ollama_worker_model,
            ]
            # Ollama reports names as "family:tag". We compare exact strings.
            missing = [m for m in required if m not in installed]
            if missing:
                warnings.append(
                    f"Ollama running but model(s) not pulled: {missing}. "
                    f"Run `ollama pull <name>`. Missing models will fall back to cloud."
                )

    # Gateway conflict
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
