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

    # Claude Code CLI (heavy path — uses Max subscription OAuth, zero API cost)
    # 2026-05-04: OpenAI/Anthropic API legacy fully removed — Claude CLI is the
    # only cloud lane. ollama is the only local lane.
    claude_code_cli_backend: Literal["wsl_subprocess", "local_subprocess"] = "wsl_subprocess"
    claude_code_cli_path: str = "/home/kang/.local/bin/claude"
    claude_code_model: str = "sonnet"  # alias; "opus" / full name also accepted
    claude_code_timeout_ms: int = 300_000  # heavy tasks can take minutes
    claude_code_concurrency: int = 1  # Max session has per-hour limits; don't stampede

    # C1 backend — fixed to claude_cli (Max OAuth, $0 marginal). 2026-05-04:
    # OpenAI legacy removed. Precedence:
    #   1. ``use_hermes_for_c1=True``  → Hermes-driven C1 (Phase 2 path)
    #   2. else                         → direct Claude CLI with Haiku
    # The Haiku C1 instance has its own concurrency pool so it does NOT
    # share the heavy-path cap of 1 (which would serialize C1 behind C2).
    c1_backend: Literal["claude_cli"] = "claude_cli"
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
    # 2026-05-04: Claude CLI fallback 경로 설정 (강제 플래그 아님).
    # ``CalendarSkill.invoke`` 가 항상 Hermes 부터 시도하고, ollama 가 죽거나
    # (게임모드 quiet) timeout 일 때만 이 값들을 가지고 ``claude -p`` 로
    # fallback. 강제 사용을 토글하는 use_claude_cli 같은 플래그는 사용자
    # 의도에 따라 폐기.
    calendar_skill_claude_model: str = "haiku"  # Max OAuth 호환 alias
    # WSL 측 절대경로. Repo 안 git-tracked 파일을 가리킨다 (/mnt/<drive>/...).
    calendar_skill_mcp_config_path: str = (
        "/mnt/e/hermes-hybrid/profiles/calendar_ops/claude_mcp.json"
    )

    # forced_profile (channel-pinned) 경로의 Claude CLI fallback 설정 —
    # 위 calendar 와 동일 정책. journal_ops 가 일기 채널 forced_profile 에
    # 진입하면 1차로 Hermes via Ollama, 실패시 Claude CLI 로 fallback.
    # ``journal_ops_use_claude_cli`` 같은 강제 플래그는 사용자 의도에 따라 폐기.
    journal_ops_claude_model: str = "haiku"
    # log_activity.yaml의 prompt를 system prompt로 합쳐 보낸다. job 이름이
    # 다른 잡으로 바뀌면 이 값을 바꾼다 (단일 on_demand 잡이라 현재는 고정).
    journal_ops_job_name: str = "log_activity"
    # WSL 측 .env 절대경로. 봇 subprocess가 이 파일을 source해서
    # GOOGLE_SHEETS_WEBHOOK_URL 같은 시크릿을 Claude CLI에 노출시킨다.
    journal_ops_env_source_path: str = (
        "/home/kang/.hermes/profiles/journal_ops/.env"
    )
    # ``-p`` (print) 모드의 Claude CLI 는 도구 사용 시 권한 prompt 를 띄울 수
    # 없으니, sheets_append 가 의존하는 ``Bash`` 같은 기본 도구를 명시적으로
    # 허용 목록에 올려야 한다. 안 그러면 봇 응답이 "permission prompt 를
    # 수락하면 자동 저장됩니다" 같은 안내 텍스트로 끝나고 시트엔 한 줄도
    # 안 들어간다. 콤마 분리 — 다른 도구가 필요하면 ``Bash,Read`` 형태로 추가.
    journal_ops_allowed_tools_csv: str = "Bash"

    @property
    def journal_ops_allowed_tools(self) -> list[str]:
        return [
            t.strip()
            for t in self.journal_ops_allowed_tools_csv.split(",")
            if t.strip()
        ]

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
    # 2026-05-04: "openai" backend removed when API legacy was purged.
    bench_judge_backend: Literal["claude_cli", "ollama"] = "claude_cli"
    ollama_judge_model: str = "qwen2.5:14b-instruct"  # used when bench_judge_backend="ollama"

    # Local-first master flag. Historical: routed L0/L2/L3 to Ollama instead
    # of the OpenAI surrogate. 2026-05-04: OpenAI surrogate removed entirely,
    # so this flag now just signals "prefer local ollama over Hermes-routed
    # paths" for the L2/L3 lane. Kept for backward compat with .env settings;
    # may be folded into ``ollama_enabled`` in a future cleanup.
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

    # Experience Logger — first brick of the growth loop. Every task that
    # reaches Orchestrator._log_task_end appends one JSONL line to
    # ``{experience_log_root}/{YYYY-MM-DD}.jsonl``. Reflection / Curator
    # jobs read these to extract patterns. Privacy: user_message is NOT
    # stored, only sha256-prefix + length, so the file is safe to share.
    # Set ``experience_log_enabled=false`` to silence (tests, dry-runs).
    experience_log_enabled: bool = True
    experience_log_root: Path = Path("./logs/experience")

    # Memory inject — P0-C (2026-05-06). When true, each Orchestrator.handle()
    # call runs ``memory.search(user_id, user_message, k=memory_inject_top_k)``
    # before dispatch and prepends the matches as a system-role entry on
    # ``task.history_window``. Off by default — flip it on per-deployment
    # only after confirming memory contents don't leak inappropriate context
    # (e.g. /memo entries from a past channel into a new conversation).
    memory_inject_enabled: bool = False
    memory_inject_top_k: int = 3

    # Memory search backend — Phase 4 (2026-05-06).
    # ``like``: SQL LIKE substring match (default, zero deps, fine for
    #          small corpora and Korean which doesn't tokenize on spaces).
    # ``embedding``: ollama embed API + cosine similarity. Requires the
    #          embedding model to be pulled (``ollama pull bge-m3``).
    #          Falls back to ``like`` automatically when the embed call
    #          fails (e.g. ollama down, model missing) so a backend swap
    #          can never silently disable memory search.
    memory_search_backend: Literal["like", "embedding"] = "like"
    memory_embedding_model: str = "bge-m3"
    memory_embedding_base_url: str = "http://localhost:11434"
    memory_embedding_timeout_s: int = 10

    # JobFactory v1 — 두 단계 게이트 (legacy: profile keyword matching).
    # 1) job_factory_enabled=True : _handle_locked()에서 factory.decide() 호출 시작.
    #    no_match 시 degraded 응답에 힌트 메시지 추가.
    # 2) allow_profile_creation=True : final_failure 때 프로필 스켈레톤 자동 생성.
    #    템플릿 출력 검증 후에만 활성화할 것.
    #
    # P0-3 (2026-05-05): v1 is in deprecation. ``use_new_job_factory`` (v2,
    # bandit) is the forward path. When v1 is active *and*
    # ``disable_v1_jobfactory`` is false, the orchestrator emits a
    # ``jobfactory.v1_deprecated`` warning at startup so operators see the
    # migration cue. ``disable_v1_jobfactory=true`` is the kill switch:
    # the v1 codepath in ``_handle_locked`` is skipped regardless of
    # ``job_factory_enabled``. Roll the default to true once production
    # has run a week without surprises, then delete v1.
    job_factory_enabled: bool = False
    allow_profile_creation: bool = False
    disable_v1_jobfactory: bool = False

    # JobFactory v2 — empirical bandit routing (Phase 1~6 산출물).
    # When true, _handle_locked() routes (after the heavy / forced_profile /
    # rule / skill gates) to JobFactoryDispatcher instead of the legacy
    # JobFactory v1 → Router → tier ladder.
    # 2026-05-06: default → True. v1 (job_factory_enabled) is in deprecation
    # (see disable_v1_jobfactory above). Operators who need to fall back can
    # set ``HERMES_USE_NEW_JOB_FACTORY=false`` in .env, but that path is no
    # longer exercised in CI.
    use_new_job_factory: bool = True
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

    @property
    def ollama_routable(self) -> bool:
        """True when Ollama is the routing target for local-tier work.

        Either flag enables the lane: ``ollama_enabled`` is the explicit
        switch, ``local_first_mode`` is the migration master that implies
        Ollama-as-default. No runtime-mode override — game/editor sessions
        manage their own ollama process state out-of-band if needed.
        """
        return self.ollama_enabled or self.local_first_mode

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
