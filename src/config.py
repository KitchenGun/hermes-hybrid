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

    # Telegram — Phase 5 MVP gateway (2026-05-06).
    # Off by default. When token is set, scripts/run_telegram_bot.py spins
    # up a long-polling client that hands messages to the same Orchestrator
    # used by Discord. allowlist enforced same as Discord (R12 fail-closed).
    telegram_bot_token: str = ""
    telegram_allowed_user_ids: str = ""  # comma-separated

    # WSL subprocess defaults — Claude CLI subprocess 호출에 사용.
    # Phase 8/10/11 후 hermes/opencode CLI 의존 폐기. master = Claude CLI 단일.
    wsl_distro: str = "Ubuntu"
    gateway_service_name: str = "hermes-gateway"  # R6 — 공식 Hermes gateway 충돌 회피

    # Hermes Master Orchestrator — Phase 11 (2026-05-06).
    # All-via-master 설계: 모든 LLM 호출이 ``claude`` CLI (Max OAuth) 의
    # master 모델 (default opus) 을 통과한다. Max 구독료만 — API key 별도
    # 비용 X ($0 marginal). Production default = True. ``MASTER_ENABLED=false``
    # 로 끄면 disabled 안내 메시지로 응답.
    master_enabled: bool = True
    master_model: str = "opus"                    # opus | sonnet | haiku — Claude alias
    master_timeout_ms: int = 300_000              # 5min — Claude CLI cold start 고려
    master_concurrency: int = 1                   # Max OAuth 시간당 한도 보호
    master_cli_backend: Literal["wsl_subprocess", "local_subprocess"] = "wsl_subprocess"
    master_cli_path: str = "/home/kang/.local/bin/claude"

    # Phase 10 (2026-05-06): parallel @handle dispatch.
    # 사용자 메시지에 ``@coder`` / ``@reviewer`` 같은 mention 이 2개 이상
    # 있을 때 master 가 단일 호출에 모든 SKILL.md snippet 을 inject 하는
    # 대신 (Phase 9 default), 각 agent 별로 독립 claude 호출을 동시 실행
    # 후 결과를 집계하는 ``ClaudeAgentDelegator`` 경로로 라우팅.
    # 비용/지연이 N 배라 default off — 명시 opt-in.
    master_parallel_agents: bool = False
    master_parallel_max_concurrency: int = 3

    # Ollama (optional)
    ollama_enabled: bool = False
    ollama_base_url: str = "http://localhost:11434"
    ollama_router_model: str = "qwen2.5:7b-instruct"
    ollama_work_model: str = "qwen2.5:14b-instruct"
    ollama_worker_model: str = "qwen2.5-coder:32b-instruct"
    bench_judge_backend: Literal["claude_cli", "ollama"] = "claude_cli"
    ollama_judge_model: str = "qwen2.5:14b-instruct"

    # Local-first lane signal — kept for downstream code that still
    # checks ``ollama_routable``.
    local_first_mode: bool = False

    # Validator retry budget (PolicyGate.post_validate's retry policy).
    # Master path is single-shot today, so these only apply when callers
    # use the Validator directly.
    retry_budget_default: int = 4
    same_tier_retry_max: int = 2
    tier_up_retry_max: int = 2

    # Daily cloud token cap. PolicyGate.pre_dispatch consults
    # ``Repository.used_tokens_today`` against this number. The master
    # path inherits the same gate.
    cloud_token_budget_daily: int = 100_000

    # Session-scoped token cap, kept for downstream rate-limit shims.
    cloud_token_budget_session: int = 20_000

    # Per-user
    per_user_in_flight_max: int = 1             # R13

    # Kanban store — Phase 6 (2026-05-06). agent 간 hand-off 채널.
    kanban_store_path: Path = Path("./data/kanban.json")

    # Storage
    state_db_path: Path = Path("./data/state.db")
    log_level: str = "INFO"
    log_json: bool = False  # R14: structured JSON logs

    # Experience Logger — first brick of the growth loop.
    experience_log_enabled: bool = True
    experience_log_root: Path = Path("./logs/experience")

    # Memory inject — P0-C (2026-05-06).
    memory_inject_enabled: bool = False
    memory_inject_top_k: int = 3

    # Memory search backend — Phase 4 (2026-05-06).
    memory_search_backend: Literal["like", "embedding"] = "like"
    memory_embedding_model: str = "bge-m3"
    memory_embedding_base_url: str = "http://localhost:11434"
    memory_embedding_timeout_s: int = 10

    @property
    def allowed_user_ids(self) -> set[int]:
        if not self.discord_allowed_user_ids.strip():
            return set()
        return {int(x) for x in self.discord_allowed_user_ids.split(",") if x.strip()}

    @property
    def telegram_allowlist(self) -> set[int]:
        if not self.telegram_allowed_user_ids.strip():
            return set()
        return {
            int(x)
            for x in self.telegram_allowed_user_ids.split(",")
            if x.strip()
        }

    @property
    def ollama_routable(self) -> bool:
        """True when Ollama is the routing target for local-tier work."""
        return self.ollama_enabled or self.local_first_mode

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
