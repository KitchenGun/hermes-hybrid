"""Centralized settings loaded from environment / .env."""
from __future__ import annotations

from pathlib import Path
from typing import Literal

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # Discord
    discord_bot_token: str = ""
    discord_allowed_user_ids: str = ""  # comma-separated
    require_allowlist: bool = True  # R12: fail-closed by default

    # Hermes adapter
    hermes_cli_backend: Literal["wsl_subprocess", "local_subprocess", "mcp"] = "wsl_subprocess"
    hermes_wsl_distro: str = "Ubuntu"
    hermes_cli_path: str = "/home/kang/.local/bin/hermes"
    hermes_home: str = "/home/kang/.hermes"  # used for reading session JSON (R1)
    hermes_timeout_ms: int = 180_000
    hermes_max_turns: int = 20
    hermes_concurrency: int = 3  # R13: cap parallel subprocess calls
    gateway_service_name: str = "hermes-gateway"  # for R6 pre-check

    # Cloud LLMs
    openai_api_key: str = ""
    openai_model: str = "gpt-4o"
    openai_model_local_surrogate: str = "gpt-4o-mini"   # R3: local tier surrogate (Ollama off)
    openai_model_worker_surrogate: str = "gpt-4o"        # R3: worker tier surrogate
    anthropic_api_key: str = ""
    anthropic_model: str = "claude-opus-4-7"

    # Claude Code CLI (heavy path — uses Max subscription OAuth, zero API cost)
    claude_code_cli_backend: Literal["wsl_subprocess", "local_subprocess"] = "wsl_subprocess"
    claude_code_cli_path: str = "/home/kang/.local/bin/claude"
    claude_code_model: str = "sonnet"  # alias; "opus" / full name also accepted
    claude_code_timeout_ms: int = 300_000  # heavy tasks can take minutes
    claude_code_concurrency: int = 1  # Max session has per-hour limits; don't stampede

    # C1 backend selector. When OpenAI TPM/quota limits make the legacy
    # gpt-4o path unusable (see incident 2026-04-21), flip this to
    # ``claude_cli`` and C1 runs through the Claude Code CLI with the
    # Haiku model — still Max OAuth, zero per-token cost, but a lighter
    # model than C2/heavy. Precedence:
    #   1. ``use_hermes_for_c1=True``  → Hermes-driven C1 (Phase 2 path)
    #   2. ``c1_backend="claude_cli"`` → direct Claude CLI with Haiku
    #   3. else                         → legacy direct gpt-4o
    # The Haiku C1 instance has its own concurrency pool so it does NOT
    # share the heavy-path cap of 1 (which would serialize C1 behind C2).
    c1_backend: Literal["openai", "claude_cli"] = "openai"
    c1_claude_code_model: str = "haiku"           # alias; full name also accepted
    c1_claude_code_timeout_ms: int = 120_000      # C1 is per-turn planning, not deep
    c1_claude_code_concurrency: int = 3           # Haiku is light; allow real parallelism

    # Ollama (optional) — when disabled, R3 surrogate path is used.
    ollama_enabled: bool = False
    ollama_base_url: str = "http://localhost:11434"
    ollama_router_model: str = "qwen2.5:7b-instruct"
    ollama_work_model: str = "qwen2.5:14b-instruct"
    ollama_worker_model: str = "qwen2.5-coder:32b-instruct"

    # Phase 1 rollout flag (off by default). When true, L2/L3 requests are
    # executed through HermesAdapter v2 — the plan/act/reflect engine — with
    # provider pinned via the RouterDecision.provider field (FIX#1). When
    # false, the current direct-OpenAI/Ollama client path is used verbatim,
    # so Phase 1 stays reversible. Flip to true only after the Phase 1 exit
    # criteria are met (see ARCHITECTURE.md §"Phase 1").
    use_hermes_for_local: bool = False

    # Phase 2 rollout flag. When true, C1 (cloud-planning tier) runs through
    # Hermes with provider='openai' pinned, same pattern as Phase 1. Off by
    # default; flip only after Phase 1 is stable and the latency gate holds
    # for C1 as well.
    use_hermes_for_c1: bool = False

    # Phase 2b rollout flag. When true, the heavy path (`!heavy`) is driven
    # by HermesAdapter with provider='claude-code' pinned — Hermes owns
    # plan/act/reflect and Claude is the reasoning step. When false, the
    # existing direct ClaudeCodeAdapter path is used (unchanged). Off by
    # default; flip only after verifying the Hermes CLI in the deployed
    # build supports --provider claude-code end-to-end.
    use_hermes_for_heavy: bool = False

    # Phase 3 master switch. When true, implies every per-phase flag above
    # (use_hermes_for_local / _for_c1 / _for_heavy) — the orchestrator is
    # fully hermes-centric. Kept separate so operators can stage the
    # transition with per-phase flags during rollout and flip this one
    # once all exit gates are met. The per-phase flags still work as
    # overrides when this is false.
    use_hermes_everywhere: bool = False

    # Phase 3 validator simplification. When true, Hermes-lane outputs that
    # show evidence of multi-turn reflection (turns_used >= 2) are trusted
    # by the Python validator — we skip the low-quality pattern checks and
    # pass-through the text. Timeouts / tool errors / empty outputs still
    # fail the validator. Off by default; flip only after the Hermes
    # reflection quality has been measured in real traffic.
    trust_hermes_reflection: bool = False

    # Router thresholds
    router_conf_accept: float = 0.75
    router_conf_tier_up: float = 0.50

    # Budgets
    retry_budget_default: int = 4
    same_tier_retry_max: int = 2
    tier_up_retry_max: int = 2
    cloud_escalation_max: int = 1
    cloud_token_budget_session: int = 20_000
    claude_call_budget_session: int = 1         # R2: enforced by Orchestrator
    cloud_token_budget_daily: int = 100_000     # R4: enforced by BudgetTracker
    surrogate_max_tokens_local: int = 512       # R3: hard cap for surrogate lane
    surrogate_max_tokens_worker: int = 1024     # R3

    # Per-user
    per_user_in_flight_max: int = 1             # R13

    # Storage
    state_db_path: Path = Path("./data/state.db")
    log_level: str = "INFO"
    log_json: bool = False  # R14: structured JSON logs

    @property
    def allowed_user_ids(self) -> set[int]:
        if not self.discord_allowed_user_ids.strip():
            return set()
        return {int(x) for x in self.discord_allowed_user_ids.split(",") if x.strip()}

    # Phase 3: the per-phase flags should answer "are we routing through
    # Hermes for this lane?" after factoring in the master switch. Callers
    # in the orchestrator read these via ``effective_*`` so the rollout
    # story stays: "flip use_hermes_everywhere once all per-lane gates hold."
    @property
    def effective_use_hermes_for_local(self) -> bool:
        return self.use_hermes_for_local or self.use_hermes_everywhere

    @property
    def effective_use_hermes_for_c1(self) -> bool:
        return self.use_hermes_for_c1 or self.use_hermes_everywhere

    @property
    def effective_use_hermes_for_heavy(self) -> bool:
        return self.use_hermes_for_heavy or self.use_hermes_everywhere


_settings: Settings | None = None


def get_settings() -> Settings:
    global _settings
    if _settings is None:
        _settings = Settings()
    return _settings


def reset_settings() -> None:
    """Test helper."""
    global _settings
    _settings = None
