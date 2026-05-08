# Apply Plan (generated)

> P0a Step 9. 비-trivial 변경 1건당 target / reason / predicted diff / risk / rollback / apply command / verification.

## P0 (이 마이그레이션이 자동 적용)

| target | reason | apply command | verification |
|---|---|---|---|
| `data/growth_metrics.generated.yaml` (W5) | Loop 4 baseline | `python scripts/capture_growth_metrics.py --output data/growth_metrics.generated.yaml` | 6 fields + timestamp + git_sha 존재 |
| `data/state.db` (bot SqliteMemory) schema (W1) | source 컬럼 추가 | `python scripts/migrate_memos_add_source.py --apply` (default `settings.state_db_path` = `data/state.db`) | `PRAGMA table_info(memos)` 에 source 포함 |
| memory ingest (W1) | Loop 1 활성 | `python scripts/ingest_memory_candidates.py --apply --user-id $(detect)` | `SELECT COUNT(*) WHERE source='generated_candidates'` ≥ 80% |
| 10 marker blocks (P0c.3) | W4/W6a/W6b/W10/W11×2/W12 | `python scripts/run_p0c.py --phases c3` | python imports succeed + pytest -x 통과 |
| skill promote (W2) | Loop 2 bootstrap | `python scripts/promote_generated_skills.py --apply --score-threshold 0.85` | `agents/<cat>/<name>/SKILL.md` count 증가 |
| job_factory.yaml block (P0c.5a) | W3 jobs 등록 | `python scripts/register_generated_jobs.py --apply --candidates-only --validate` | yaml 에 `# --- generated job candidates ---` block 존재 |
| timer_handlers _TASKS extension (P0c.5b) | W3 cron 등록 | `hermes-setup --non-interactive` (after marker block insert) | `schtasks /Query` 등 9 entries |

## P1 (사용자 결정 필요, 자동 적용 X)

### `.gitignore` 추가 후보
- `*.generated.tmp`, `generated/.cache/` (도입 시)
- **risk**: low. **rollback**: line 제거.

### `docs/PROFILE_INVENTORY.md` 갱신
- generated 프로파일 yaml 링크 추가
- **risk**: low. 

### `docs/JOB_INVENTORY.md` 갱신
- generated 잡 후보 yaml 링크 추가
- **risk**: low.

### `skill_hot_reload_enabled=true` 활성화
- effect: AgentRegistry 가 `agents/*/SKILL.md` 폴링 → 봇 재시작 없이 새 skill 활성
- **risk**: medium. 폴링 30s overhead.
- **rollback**: env / settings false 로 되돌리기.
- **apply command**: `.env` 또는 `src/config.py` 의 default 변경 (사용자 결정).

### `feedback_keyword_match_enabled=true` 활성화
- effect: Phase 20 reaction-based feedback 에 keyword matching 추가
- **risk**: high. FP rate untested.
- **decision**: 30일 모니터링 후 결정.

### `skill_promoter_auto_install=true` 활성화
- effect: SkillPromoter 가 W9/W10/W11 draft 를 PR 대신 직접 install
- **risk**: high. critic_rerun=True + threshold 0.85 가 게이트지만 auto-revert 도 함께 켜야 안전.
- **decision**: 14일 동안 PR 흐름 관찰 후 결정.

## P2 (장기, 제안만)

### Phase 11 single-lane → multi-lane 복귀
- `config/provider_routing.generated.yaml` `proposed_policy` 참조
- **risk**: high. ADR cf98c65 반전.
- **trigger**: Max OAuth 비용 모델 변화 / OpenAI 또는 Ollama 가 Claude 대비 우월한 case.

### `logs/experience/` retention policy
- 90 일 rolling + S3 cold storage
- **risk**: medium. 기존 검색 path 영향.

### Slack gateway 추가
- `src/gateway/slack_bot.py` (NEW)
- **risk**: medium. Discord 와 동일 pattern.

### Loop 5 active dispatch biasing
- `src/state/task_state.py` `task.suggested_handles: list[str] = Field(default_factory=list)` 추가 (1줄)
- `_dispatch_master()` 에 `task.agent_handles` 비어있을 때 `task.suggested_handles` 채택 로직
- W12 marker block 을 log-only → mutating 으로 전환
- **risk**: high. Dispatch chain audit 필요.

### Career-tutor / interview-prep skill 활성화
- 사용자 의도 명시 후 `interview_preparation` (generated 07) → `agents/documentation/interview_prep/SKILL.md` 승격

## 모든 변경의 공통 rollback

- 모든 W marker block: `HERMES_DISABLE_GROWTH_BLOCKS=true` env flag 즉시 short-circuit
- 또는 `scripts/rollback_marked_blocks.sh --names <list>`
- memory ingest: `DELETE FROM memos WHERE source='generated_candidates'`
- skill promote: `git restore agents/`
- job_factory.yaml: `# --- generated job candidates ---` 블록 제거
- timer install: 플랫폼 별 native uninstall 명령 (gap_analysis 참조)
