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
    # Repo root — Claude CLI subprocess 의 cwd 로 전달돼야 ``.claude/settings.json``
    # allow 패턴이 정상 적용된다. ``src/config.py`` 의 부모의 부모 = 리포 루트.
    # ``PROJECT_ROOT`` env 로 worktree override 가능 (pydantic-settings 자동 매핑).
    project_root: Path = Path(__file__).resolve().parents[1]

    # Phase 10 (2026-05-06): parallel @handle dispatch.
    # 사용자 메시지에 ``@coder`` / ``@reviewer`` 같은 mention 이 2개 이상
    # 있을 때 master 가 단일 호출에 모든 SKILL.md snippet 을 inject 하는
    # 대신 (Phase 9 default), 각 agent 별로 독립 claude 호출을 동시 실행
    # 후 결과를 집계하는 ``ClaudeAgentDelegator`` 경로로 라우팅.
    # 비용/지연이 N 배라 default off — 명시 opt-in.
    master_parallel_agents: bool = False
    master_parallel_max_concurrency: int = 3

    # Phase 15 (2026-05-07): SkillPromoter — auto SKILL.md draft + git PR.
    # Hermes Agent (Nous) 의 "creates skills from experience, improves them
    # during use" 흡수. CuratorJob 가 일요일 23:30 호출. ExperienceLog 의
    # 자주 등장 (handled_by, agent_handles, pipeline_id) cluster 추출 →
    # 기존 17 SKILL.md 어디에도 없는 새 패턴이면 SKILL.md draft + git PR.
    skill_promoter_enabled: bool = True
    skill_promoter_auto_pr: bool = True            # 사용자 결정: 자동 PR OK
    skill_promoter_min_evidence: int = 5           # 최소 N회 반복 패턴만
    skill_promoter_weak_score_threshold: float = 0.4
    skill_promoter_draft_dir: Path = Path("./logs/curator/auto_skills")

    # Phase 18 (2026-05-07): SKILL hot-reload + auto-promotion.
    # Hermes Agent 의 "skills self-improvement during use" 흡수.
    # critic 통과 draft 를 PR 안 거치고 agents/auto/<name>/SKILL.md 로 직접
    # 활성화. AgentRegistry 가 mtime 폴링으로 새 skill 자동 인식 — master 재시작 X.
    # 5회 사용 후 평균 self_score < threshold 면 다음 주 SkillPromoter 가
    # archived/ 로 이동 (자동 revert).
    # default OFF — 사용자 1주 관찰 후 명시 ON.
    skill_promoter_auto_install: bool = False
    skill_hot_reload_enabled: bool = False
    skill_hot_reload_poll_seconds: int = 30
    skill_promoter_critic_rerun: bool = True       # auto_install 시 형식 검증
    skill_auto_promotion_threshold: float = 0.85   # critic_rerun score ≥
    skill_auto_revert_min_uses: int = 5            # N회 사용 후 점수 평가
    skill_auto_revert_score_threshold: float = 0.3

    # Phase 19 (2026-05-07): cross-platform timer auto-registration.
    # `hermes-setup` 진입점 (pyproject.toml [project.scripts]) 가 호출되면
    # OS 별로 ReflectionJob / CuratorJob / SkillPromoter 의 weekly schedule
    # 을 등록. Windows = schtasks, Linux/WSL = systemd-user, macOS = launchd.
    # 사용자 동의 prompt 없이 silent 등록 금지 — auto_timer_ack 가 명시 True
    # 면 자동 진행, 아니면 first-run 시 [y/N] prompt.
    auto_timer_enabled: bool = True
    auto_timer_ack: bool = False
    # CI / Docker 회피용. HERMES_NO_AUTO_TIMER=1 시 setup 이 silent skip.

    # Phase 20 (2026-05-07): Discord 피드백 → 정책 자동 갱신.
    # 👍/👎 reaction + 텍스트 키워드 매칭으로 ExperienceLog 의 feedback 필드
    # patch. SkillPromoter.weak_agent_audit 가 negative_count ≥ threshold 인
    # 패턴을 보강 draft 트리거에 사용.
    # reaction listener 는 default ON, 키워드 매칭은 false-positive 우려로
    # default OFF (명시 opt-in).
    feedback_listener_enabled: bool = True
    feedback_keyword_match_enabled: bool = False
    feedback_negative_threshold: int = 3       # weak_agent_audit 보강 임계값
    feedback_lru_max: int = 1000               # in-memory message_id ↔ task_id
    feedback_lru_ttl_seconds: int = 86_400     # 24h

    # Phase 14 (2026-05-07): memory curator — auto MEMORY.md + USER.md.
    # 매 N task 후 master 가 metadata 보고 1-2줄 메모 append. 1500자 초과 시
    # 자동 LLM compaction. master prompt 에 자동 prepend (USER + MEMORY tail).
    # Privacy: raw user_message 는 LLM 에 안 넘김 — handled_by/agent_handles/
    # token count 등 metadata 만.
    memory_curator_enabled: bool = True
    memory_curator_every_n_tasks: int = 5
    memory_root: Path = Path("./data/memory")
    memory_max_chars: int = 1500

    # Phase 13 (2026-05-07): revision loop — plan/act/observe/reflect/retry.
    # Critic self_score < threshold 시 자동 retry. 3회까지, 모델 escalation
    # (haiku → sonnet → opus). default off — 사용자 opt-in 시 master 호출
    # 횟수가 늘어나 Max OAuth 한도 빨리 소진할 수 있음. 단순 응답엔 불필요.
    revision_loop_enabled: bool = False
    revision_loop_max_retries: int = 3
    revision_score_threshold: float = 0.5
    # 기본 escalation 순서. master_model 이 명시되면 그 위치부터 시작.
    revision_model_escalation: str = "haiku,sonnet,opus"

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

    # Kanban — Phase 2-A (Nous Hermes Agent 모델 정렬, 2026-05-07).
    # Phase 6 JSON stub 폐기. SQLite + dispatcher + master CLI worker spawn.
    kanban_db_path: Path = Path("./data/kanban.db")
    kanban_workspaces_root: Path = Path("./data/kanban/workspaces")
    kanban_dispatcher_enabled: bool = True
    kanban_dispatcher_poll_seconds: int = 60
    kanban_claim_ttl_seconds: int = 300
    kanban_spawn_failure_limit: int = 5
    kanban_notify_channel_id: int = 0  # 0 = silent, opt-in

    # Storage
    state_db_path: Path = Path("./data/state.db")
    log_level: str = "INFO"
    log_json: bool = False  # R14: structured JSON logs

    # Experience Logger — first brick of the growth loop.
    experience_log_enabled: bool = True
    experience_log_root: Path = Path("./logs/experience")

    # Memory inject — P0-C (2026-05-06). Phase 17 (2026-05-07): default ON.
    # _maybe_inject_memory() 검색 기반 top-k inject 가 master prompt 의 system
    # role 앞에 추가. memory_curator.read_prompt_prepend() 와는 별도 경로 —
    # 후자는 MEMORY.md tail 자동 prepend, 전자는 user_message 와 의미 유사한
    # 과거 메모리 검색.
    memory_inject_enabled: bool = True
    memory_inject_top_k: int = 3

    # Memory search backend — Phase 4 (2026-05-06).
    memory_search_backend: Literal["like", "embedding"] = "like"
    memory_embedding_model: str = "bge-m3"
    memory_embedding_base_url: str = "http://localhost:11434"
    memory_embedding_timeout_s: int = 10

    # Phase 22 (2026-05-07): journal pipeline reborn.
    # Phase 8 에서 폐기되었던 ``profiles/journal_ops`` 의 일기 흐름 (Discord
    # 자연어 → 24-필드 JSON → Apps Script append → 한국어 응답) 을 모듈로
    # 환생. ``JOURNAL_CHANNEL_ID`` 채널의 메시지만 ``JournalPipeline`` 으로
    # 라우팅 — 그 외 채널은 기존 ``Orchestrator.handle()`` 그대로.
    # default OFF (channel_id=0). 켜려면 ``JOURNAL_ENABLED=true`` +
    # ``JOURNAL_CHANNEL_ID``/``GOOGLE_SHEETS_WEBHOOK_URL`` 모두 채워야 함.
    journal_enabled: bool = False
    journal_channel_id: int = 0
    google_sheets_webhook_url: str = ""
    journal_alert_webhook_url: str = ""  # 시트 append 실패 시 빨간 embed (선택)

    # Phase 21 (2026-05-07): A/B experiment runner.
    # task_id hash 기반 결정론적 분기. control = inject skip, treatment = inject ON.
    # Phase 17 의 행동 변화 효과를 측정하기 위해 1주차 동시 도입. ratio 0.5
    # 면 50/50. ExperienceLog 의 experiment_arm / experiment_name 컬럼에 stamp.
    # 주간 ABReportJob (일요일 22:30) 가 self_score 평균 비교 + Welch's t.
    ab_experiment_enabled: bool = True
    ab_treatment_ratio: float = 0.5
    ab_experiment_name: str = "memory_inject"
    ab_report_root: Path = Path("./logs/ab")

    # ========================================================================
    # Growing Agent Memory Architecture — P0-A (2026-05-09).
    # 26 new settings (plan v4.2). The existing ``memory_inject_enabled`` and
    # ``memory_root`` above are preserved verbatim; ``compiled_memory_root``
    # below is the architecture-level alias for ``memory_root`` and points at
    # the same directory. Future refactor may collapse the two.
    # All retrieval / extraction / auto-ingest / auto-learning toggles ship
    # OFF by default — Phase 21 A/B for memory_inject must not be perturbed.
    # ------------------------------------------------------------------------

    # Retrieval gate (P2). default OFF; isolated under its own A/B key so the
    # legacy memory_inject experiment keeps a clean control arm.
    memory_retrieval_enabled: bool = False
    memory_retrieval_ab_key: str = "memory_retrieval_v1"

    # LLM-based extraction is a self-biasing risk; v1 is rule-based only.
    memory_llm_extraction_enabled: bool = False

    # Injection budget — keeps prompt cache hot by capping compiled context.
    memory_inject_token_budget: int = 2000
    memory_retriever_k: int = 5

    # Filesystem roots — see plan v4.2 § "디렉터리 구조 (v4.2)".
    processed_memory_root: Path = Path("./data/processed_memory")
    compiled_memory_root: Path = Path("./data/memory")
    ingest_staging_root: Path = Path("./data/ingest_staging")
    source_manifest_root: Path = Path("./data/source_manifests")
    external_memory_root: Path = Path("./data/external_memory")

    # Safety scanners.
    pii_detection_enabled: bool = True
    security_scan_enabled: bool = True
    security_scan_exclude_low_risk: bool = False
    security_scan_severity_threshold: str = "medium"   # low|medium|high

    # Ingest hygiene.
    ingest_auto_delete_staging: bool = True

    # Auto growth loops — both default OFF. Explicit user opt-in required.
    discord_auto_ingest_enabled: bool = False
    cron_auto_learning_enabled: bool = False

    # Profile isolation — P0-A ships the policy validator only. Setting
    # ``memory_profile_scoped=True`` triggers an experimental no-op warning
    # via :class:`src.memory.ingestion.profile_paths.ProfileScopedExperimentalWarning`.
    memory_profile_name: str = "default"
    memory_profile_scoped: bool = False

    # Skill storage — default ``hermes_profile`` matches the official
    # Hermes Skills source of truth (~/.hermes/skills/...). ``project_local``
    # is dev/test override; ``project_compat`` keeps the legacy agents/ layout.
    skill_storage_mode: str = "hermes_profile"          # hermes_profile|project_local|project_compat
    skill_registry_root: Path = Path("./data/profiles/default/skills")
    skill_shared_registry_root: Path = Path("./data/shared_skills")

    # Cron skill attach — default pinned for reproducibility.
    cron_skill_pin_mode: str = "pinned"                 # pinned|latest

    # Memory schema / audit.
    memory_schema_version: int = 1
    memory_hard_delete_enabled: bool = False
    memory_audit_root: Path = Path("./data/memory_audit")

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
