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

    # journal_ops: channel-pinned forced profile routing.
    # When a message arrives on the channel matching ``journal_channel_id``,
    # the bot bypasses the rule/skill/factory pipeline and forces the
    # orchestrator into the journal_ops profile (24-field activity logger
    # → Google Sheets via Apps Script). 0 (default) = feature disabled.
    journal_channel_id: int = 0
    # Apps Script doPost endpoint that journal_ops's sheets_append skill
    # POSTs activity rows to. Empty = skill will fail with exit 2.
    google_sheets_webhook_url: str = ""
    # Optional Discord webhook for journal_ops operational alerts. When the
    # Apps Script endpoint fails (HTTP 4xx/5xx, {"ok": false, ...}, or
    # network error), post_to_sheet.py best-effort posts a red embed here.
    # Empty = no alert fired (only stderr + LLM-driven Discord reply).
    journal_alert_webhook_url: str = ""

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

    # Calendar skill — routes calendar/schedule queries to the calendar_ops
    # Hermes profile so the google-workspace skill (with OAuth) is active.
    # Disabled by default; flip on once the calendar_ops profile is
    # configured and OAuth is authenticated (setup.py --check returns OK).
    calendar_skill_enabled: bool = False
    calendar_skill_profile: str = "calendar_ops"
    # Empty string = defer to the profile's own config.yaml. Custom
    # providers defined in the profile (e.g. ``ollama-local``) are NOT
    # valid ``--provider`` argparse choices on the Hermes CLI, so the
    # documented way to opt into a profile-defined backend is to leave
    # these blank. Set them only if you want to override what the
    # profile config says on a per-turn basis.
    calendar_skill_model: str = ""
    calendar_skill_provider: str = ""
    # Preload the google-workspace skill so it's available for the turn
    # without the model needing to pick it from the global catalog.
    calendar_skill_preload: str = ""  # 프로파일 config.yaml의 auto_load로 충분, 별도 preload 불필요
    calendar_skill_timeout_ms: int = 180_000
    calendar_skill_max_turns: int = 5        # write/복잡 쿼리용
    calendar_skill_read_max_turns: int = 3   # read 전용: plan + API + respond

    # Ollama (optional) — when disabled, R3 surrogate path is used.
    ollama_enabled: bool = False
    ollama_base_url: str = "http://localhost:11434"
    ollama_router_model: str = "qwen2.5:7b-instruct"
    ollama_work_model: str = "qwen2.5:14b-instruct"
    ollama_worker_model: str = "qwen2.5-coder:32b-instruct"
    # Bench harness LLMJudge backend. The judge grades model outputs on
    # llm_judge dimensions (korean / code_review / summarize / long_context).
    # Options:
    #   "claude_cli" — uses ClaudeCLIAdapter (Max OAuth, $0). Cap-aware:
    #                  cloud_policy.yaml's claude_auto_calls_per_day still
    #                  applies, so big sweeps may need a temporary cap raise.
    #   "ollama"    — uses ``ollama_judge_model`` locally. Free + unlimited
    #                 but lower-quality grading. Default for BenchScheduler
    #                 (the auto-loop) so we never burn Claude quota silently.
    #   "openai"    — legacy; uses gpt-4o-mini via openai_api_key. Costs a
    #                 few cents per sweep; disabled when key is empty.
    bench_judge_backend: Literal["claude_cli", "ollama", "openai"] = "claude_cli"
    ollama_judge_model: str = "qwen2.5:14b-instruct"  # used when bench_judge_backend="ollama"

    # Local-first migration master flag. When True, the orchestrator's L0
    # (router) / L2 (work) / L3 (worker) surrogate paths prefer local Ollama
    # over the OpenAI surrogate even if openai_api_key is set. The OpenAI
    # lazy clients stay registered as fallback so flipping this to False
    # restores the legacy behavior without code changes. Off by default
    # so this commit is reversible. Flip via env: LOCAL_FIRST_MODE=True.
    local_first_mode: bool = False

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

    # Profiles directory (for Refiner / Job_Factory / HITL profile_loader)
    profiles_dir: Path = Path("./profiles")

    # JobFactory v1 — 두 단계 게이트 (legacy: profile keyword matching).
    # 1) job_factory_enabled=True : _handle_locked()에서 factory.decide() 호출 시작.
    #    no_match 시 degraded 응답에 힌트 메시지 추가.
    # 2) allow_profile_creation=True : final_failure 때 프로필 스켈레톤 자동 생성.
    #    템플릿 출력 검증 후에만 활성화할 것.
    job_factory_enabled: bool = False
    allow_profile_creation: bool = False

    # JobFactory v2 — empirical bandit routing (Phase 1~6 산출물).
    # When true, _handle_locked() routes (after the heavy / forced_profile /
    # rule / skill gates) to JobFactoryDispatcher instead of the legacy
    # JobFactory v1 → Router → tier ladder. Off by default; flip per-user
    # in settings.local.json or globally once Phase 8 rollout is complete.
    # Legacy paths stay alive in code for 1-2 weeks of co-existence.
    use_new_job_factory: bool = False
    # Optional override path for the v2 score matrix (rarely changed).
    # Default = data/job_factory/score_matrix.json relative to repo root.
    score_matrix_path: Path = Path("./data/job_factory/score_matrix.json")

    # Watcher runtime — event/poll 기반 watcher YAML 실행 엔진.
    # 비활성화 시 watchers/*.yaml은 디스크에만 존재하고 폴링되지 않음.
    watcher_enabled: bool = False
    watcher_default_interval_seconds: int = 300  # YAML이 interval_seconds 미지정 시 기본값
    watcher_max_concurrency: int = 4              # 동시 실행 watcher 수 cap

    # Mail skill — Gmail/Naver 등 멀티 프로바이더 메일 조회.
    # accounts.yaml(profile별)이 계정·자격을 들고 있으며, 이 플래그는 단지
    # MailSkill을 SkillRegistry에 등록할지를 결정한다.
    mail_skill_enabled: bool = False

    # Human-in-the-loop (HITL) — confirmation gates for writes declared
    # with ``safety.requires_confirmation: true`` in profile job YAMLs.
    hitl_enabled: bool = True
    hitl_timeout_seconds: int = 600  # 10 min — matches discord.ui.View default sentiment
    hitl_fallback_to_text_command: bool = True  # allow `/confirm <id> yes|no`

    # OpenCode agent specialization (Plan→Build/High→Reviewer pipeline).
    # Off by default; `!opencode` prefix is a no-op until flipped on.
    opencode_enabled: bool = False
    opencode_plan_model: str = "gpt-4o"
    opencode_build_model: str = "qwen2.5-coder:14b-instruct"
    opencode_high_model: str = "sonnet"       # Claude Code CLI alias
    opencode_reviewer_model: str = "haiku"    # Claude Code CLI alias
    opencode_risk_threshold: Literal["low", "medium", "high"] = "medium"
    opencode_reviewer_enabled: bool = True    # skip Reviewer for cost

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
    def effective_ollama_enabled(self) -> bool:
        """True when Ollama should be used for local-tier work.

        Local-first mode implies Ollama is enabled — flipping
        ``LOCAL_FIRST_MODE=True`` alone is sufficient to reroute the L0/L2/L3
        surrogate paths through Ollama without also setting
        ``OLLAMA_ENABLED=True`` (single-knob ergonomics).

        Runtime-mode override: when ``runtime_mode.get().mode`` is
        ``playtest`` or ``gaming``, Ollama is forced OFF so the GPU/VRAM
        is free for the game / editor PIE. Flipping back to ``dev`` (via
        CLI / hotkey / watcher / PIE webhook) restores routing without a
        process restart. The static flags above remain unchanged.
        """
        if not (self.ollama_enabled or self.local_first_mode):
            return False
        # Lazy import: runtime_mode has no Settings dependency, but
        # importing at module top would create a tight coupling that
        # makes Settings non-importable in early-init paths.
        from src.runtime_mode import get as _runtime_mode_get
        return _runtime_mode_get().local_llm_should_run

    @property
    def effective_c1_backend(self) -> Literal["openai", "claude_cli"]:
        """C1 backend with runtime-mode override.

        In ``playtest`` / ``gaming`` mode, force ``claude_cli`` (Max OAuth,
        $0 marginal cost) so a long game session can't silently burn
        OpenAI tokens. In ``dev`` mode, defer to the configured
        :attr:`c1_backend`. The orchestrator reads this property instead
        of ``c1_backend`` directly so the override applies everywhere.
        """
        from src.runtime_mode import get as _runtime_mode_get
        if not _runtime_mode_get().local_llm_should_run:
            return "claude_cli"
        return self.c1_backend

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
