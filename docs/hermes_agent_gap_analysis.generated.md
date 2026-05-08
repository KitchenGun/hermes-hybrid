# Hermes Agent — Closed-Loop Gap Analysis (generated)

> P0a Step 8. Phase 21 snapshot + 본 마이그레이션의 W1-W12 wiring = "Phase 22 closed-loop".

## 현재 구조

- Phase 21 까지: memory_inject default ON, CuratorJob, SkillPromoter (auto-install + auto-revert), ABReportJob, Discord-feedback → 정책 갱신, cross-platform timer auto-registration, Discord #일기 → 24-필드 → Sheets pipeline (Phase 22).
- 본 마이그레이션 W1-W12 추가:
  - W1: memory candidate ingest + `source` 컬럼 + audit log
  - W2: skills/generated 10개 promote
  - W3: 5개 신규 cron + W3 marker block (timer_handlers)
  - W4: SOUL.generated.md + master prompt injection
  - W5: data/growth_metrics.generated.yaml baseline
  - W6: MCP `_TOOLS` 17개 추가 (read 5 + write 12)
  - W7: migration_self_review (Sun 21:00) + 3 종류 self-review artifact
  - W8: dialectic user modeling (Mon 06:00)
  - W9: skill self-modify (Sat 23:00)
  - W10: real-time recurring-request detector + 30분 drainer
  - W11: self-review artifact → CuratorJob / SkillPromoter scan extension
  - W12: delegation pattern extractor (Mon 12:00) + bias loader (observation only on day 0)

## 5개 Loop 활성 상태 (Day-0 honest assessment)

### Loop 1 — Memory: **Active Growing on day 0**
- W1 ingest 가 `data/state.db` (settings.state_db_path, 봇 `SqliteMemory`와 동일 파일) 의 `memos` 테이블에 `source='generated_candidates'` 로 적재
- 기존 `_maybe_inject_memory()` (`src/orchestrator/hermes_master.py:741-760`) 가 다음 요청에서 동일 DB로부터 recall (default ON)
- `data/memory/MEMORY.md` 의 CuratorJob 큐레이트 store 와는 **별도** — 두 store는 함께 운영되지만 recall 코퍼스는 `data/state.db`만 사용
- W7 SelfReview → memory candidate yaml → CuratorJob 가 이어서 ingest (P1.5: ingest path 도 `settings.state_db_path` 기준으로 정렬됨)

### Loop 2 — Skill: **mixed**
- 초기 W2 10 skills: ACTIVE (직접 `agents/{cat}/{name}/SKILL.md` 작성)
- 신규 draft 생성 (W9 / W10 / W11): Active for draft creation only
- auto-install: 현재 `skill_promoter_auto_install=False` (`src/jobs/skill_promoter.py:86` constructor default) 이므로 PR 흐름. P1 toggle flip 시 active.

### Loop 3 — User Model: **Active Growing on day 0**
- W2 user_profile + W4 SOUL injection 즉시 활성
- W8 dialectic 첫 실행은 다음 월요일 06:00 KST

### Loop 4 — Cron Reflection: **Bootstrap-Ready on day 0, Active by day 7**
- W7 첫 실행은 다음 일요일 21:00 KST
- W11 marker block 이 CuratorJob / SkillPromoter 에 self-review yaml/skills scan 추가

### Loop 5 — Delegation: **Observation-Only on day 0**
- W12 marker block은 `bot_stdout.log` 에 `w12.delegation_suggestion` 만 기록
- `task` mutation 없음. Active biasing 은 P2 (`task.suggested_handles` 필드 추가 + dispatch 훅 필요).
- Pattern extractor (Mon 12:00) 는 active, 출력은 다음 월요일에 생김.

## 여전히 부족한 점 (genuinely missing)

- Slack gateway 통합 ✗
- `logs/experience/` retention policy (90 일 + S3 cold storage) ✗
- Multi-lane provider routing (Phase 11 reversal) — `proposed_policy` 만 docs, 자동 적용 X
- `prompt_engineer` profile equivalent — generated skill 01 이 후보, 사람 review 후 결정
- Loop 5 auto-decomposition — master 가 큰 요청을 자동 분해해서 `@handle` 제안 → 사용자 승인 → 실제 dispatch 흐름

## 부분 커버

- `interview_preparation_skill` (07) — initial draft, 사용자 사례 보완 필요
- `github_repo_analysis_skill` (03) — initial draft + W10 detector 가 4회 이상 반복 시 추가 draft enqueue
- AI 트렌드 / 시장 / GitHub repo summary jobs — initial yaml 후보, W7 가 제안 yaml 추가

## 위험한 점

- `logs/experience/*.jsonl` 무제한 증가 — W7/W8/W12 lookback 쿼리 latency 증가 가능
- `feedback_keyword_match_enabled=false` 가 W7 self-review 신호 품질 낮춤 (P1 toggle 결정 사용자에게 위임)
- `skill_hot_reload_enabled=false` 이므로 P0c.4 후 W2 promote 된 skill 활성화 위해 봇 재시작 필요
- W10 detector 의 매 요청 FTS5 substring 쿼리 — latency 영향 (mitigation: 30일 TTL)

## 즉시 개선 P0 (이 마이그레이션이 적용)

- 8 generation + 12 wiring (W1-W12) + auto-apply (P0c.1-c.5) with gates
- Loop 1: Active Growing
- Loop 2 (P0): initial 10 active + W9/W10/W11 draft creation. (P1): `skill_promoter_auto_install=true` flip 으로 fully autonomous.
- Loop 3: Active Growing (SOUL injected, dialectic queued)
- Loop 4: Bootstrap-Ready (first SelfReview next Sunday)
- Loop 5: Observation-only (W12 logs `bot_stdout.log`; active biasing P2)

## 중장기 개선 P1 / P2

→ `docs/apply_plan.generated.md` 참조.

## 파일별 수정 제안

- 10 marker block insertion + 1 schema migration + 1 yaml block
  - `src/orchestrator/hermes_master.py` ×3 (W4, W10, W12)
  - `src/mcp/server.py` ×2 (W6a, W6b)
  - `src/jobs/curator_job.py` ×1 (W11)
  - `src/jobs/skill_promoter.py` ×1 (W11)
  - `src/cli/timer_handlers/{windows,linux,darwin}.py` ×1 each (W3)
  - `data/state.db` (bot SqliteMemory) schema migration on `memos` table
  - `config/job_factory.yaml` `# --- generated job candidates ---` block
- 모든 marker block 은 `HERMES_DISABLE_GROWTH_BLOCKS=true` short-circuit
- 다른 기존 파일 수정은 P1 (NOT auto-applied)
