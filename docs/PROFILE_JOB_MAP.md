# Profile ↔ Job ↔ Skill ↔ Memory 관계도

> 현재 vs 목표 구조 + ExperienceRecord 가 기록해야 할 표준 routing 필드 + Migration 순서.
> Phase 1.5-C 산출물.
>
> 분석일: 2026-05-06 (HEAD: 470fb7a)

---

## 1. 현재 관계도 (실 코드 기준)

```
Discord Message
    │
    ├─ #일기 channel? ──────────► forced_profile=journal_ops ──► hermes -p journal_ops
    │                                                              └─ log_activity → sheets_append
    │
    ├─ /memo /hybrid-* ?  ─────► slash skill ──► MemoryBackend.save/list/clear
    │
    └─ 자유 텍스트
         │
         ├─ JobFactory v2 (default-on, 2026-05-06~)
         │    ├─ classifier (keyword/llm/fallback) → JobType (10종)
         │    ├─ ScoreMatrix → arm 선택
         │    └─ dispatcher → adapter 호출 → Critic → ExperienceLog
         │
         └─ legacy router (v1 OFF, kill switch)

cron / watcher (hermes 가 직접 schedule)
    │
    └─ profile cron yaml prompt 직접 실행
         ↑
         └─ ❌ ExperienceLog 미기록 (Orchestrator 우회)
```

## 2. 결정적 누락: cron/watcher 의 ExperienceLog 미기록

`Orchestrator._log_task_end` 만 logger 호출. 그러나 cron/watcher 16잡은:
- WSL 안에서 `hermes -p {profile} cron run {job_id}` 가 직접 실행
- hermes 자체 세션 → JSON 파일 출력
- **hermes-hybrid 의 Orchestrator 를 통과하지 않음**

→ **26 잡 중 16잡 (cron 10 + watcher 4 + 일부 on_demand) 가 ExperienceLog 에 안 들어감**.

→ Phase 2 의 가장 큰 작업: cron/watcher 통합.

## 3. 목표 관계도

```
Profile
  ├─ owns Jobs (1:N)
  │    ├─ cron jobs    (self-driving by hermes scheduler)
  │    ├─ on_demand    (Discord/intent triggered)
  │    ├─ watchers     (event/poll triggered)
  │    └─ forced       (channel-pinned)
  │
  ├─ enables Skills (M:N — skills.auto_load)
  ├─ defines Model Policy (config.yaml)
  ├─ defines Memory Policy (memories/MEMORY.md+USER.md, inject on/off)
  └─ emits ExperienceLog (모든 trigger)

Job
  ├─ belongs to Profile (1:1)
  ├─ has Trigger
  ├─ uses Skills (Profile skills 의 subset)
  ├─ uses Model Policy (inherits, optional override)
  ├─ writes ExperienceLog ← 매 실행마다
  └─ may create ReflectionCandidate

Skill
  ├─ profile-level (SKILL.md, hand-written or auto-promoted)
  │    └─ has runs/successes/failure_rate (Curator 가 갱신)
  └─ slash (src/skills/*.py — 내부 명령)

Memory
  ├─ per-user (SqliteMemory): /memo CRUD + P0-C search/inject
  ├─ per-profile (MEMORY.md, USER.md): SOUL.md 와 합쳐 prompt prefix
  └─ Curator 가 reflection 결과 → MEMORY.md 후보 (검토 후 merge)
```

## 4. 통합 ID 체계 (ExperienceRecord 표준 필드)

| 필드 | 도메인 | 비고 |
|---|---|---|
| `profile_id` | "advisor_ops"/.../"mail_ops"/None | forced_profile 또는 cron_owner 또는 v2 매칭 결과 |
| `job_id` | yaml 의 `name:` (26개) 또는 슬래시 name 또는 None | 자유 텍스트면 None |
| `job_category` | "read"/"write"/"analyze"/"monitor"/"watcher"/"chat" | yaml `category:` |
| `trigger_type` | "discord_message"/"cron"/"watcher_event"/"watcher_poll"/"forced_profile"/"manual" | 5종 |
| `trigger_source` | cron expr / `internal.{event}` / `rss_poll://...` / "discord:user_id" | 디버깅용 |
| `v2_job_type` | "simple_chat"/"code_review"/... 10종 | v2 통과시만 |
| `v2_classification_method` | "keyword"/"llm"/"fallback" | v2 통과시만 |
| `skill_ids` | list[str], SkillEntry id ("calendar_ops/messaging/discord_notify") | 호출된 모든 |
| `slash_skill` | str, "hybrid-memo"/etc | 슬래시 처리시만 |
| `model_provider` | "ollama"/"custom"/"claude_cli" | 실제 사용된 |
| `model_name` | "qwen2.5:14b-instruct"/"haiku"/... | local 도 포함 |
| `memory_inject_count` | int | P0-C inject 수 |

기존 보존: `ts`, `task_id`, `session_id`, `user_id`, `route`, `tier`, `handled_by`, `status`, `outcome`, `degraded`, `latency_ms`, `cloud_calls`, `cloud_models`, `prompt_tokens`, `completion_tokens`, `retries`, `tier_ups`, `error_types`, `last_error_message`, `tool_calls`, `hermes_turns`, `hermes_reflection_count`, `self_score`, `input_text_hash/length`, `response_hash/length`.

## 5. ReflectionJob 그룹핑 키 (예시)

```python
# Q1: 어느 profile 이 가장 비싼가?
GROUP BY profile_id → SUM(cloud_calls), AVG(latency_ms)

# Q2: 어느 cron job 이 자주 실패하는가?
WHERE trigger_type='cron' GROUP BY job_id → fail_rate, last_run

# Q3: forced_profile vs JobFactory v2 의 success_rate 차이?
GROUP BY trigger_type → success_rate

# Q4: 어떤 skill 이 가장 많이 호출되는가?
GROUP BY tool_calls.tool → COUNT, fail_rate

# Q5: v2 의 어떤 분류가 자주 misroute 되는가?
WHERE v2_classification_method='llm' AND outcome='failed'
GROUP BY v2_job_type
```

## 6. SkillPromotion 패턴 (Phase 3)

**프로필-레벨 SKILL.md 자동 승격**:
```python
# 같은 (profile_id, job_id) 가 동일 skill_ids 시퀀스로 5회 이상 success
WHERE outcome='succeeded'
GROUP BY profile_id, job_id, skill_ids
HAVING COUNT(*) >= 5 AND failure_rate <= 0.20

# Curator 가 발견하면:
#   → skills/promoted/{profile_id}__{job_id}/SKILL.md 후보 markdown
#   → 사람 review → 통과시 profiles/{profile_id}/skills/{category}/{name}/ 로 승격
```

**비활성화 후보**:
```python
WHERE skill_id ?
GROUP BY skill_id → failure_rate >= 0.30 AND runs >= 10
# → skills/archived/ 후보
```

## 7. Migration 순서

| Phase | 작업 | 산출물 | 회귀 위험 |
|---|---|---|---|
| **1.5 (P0)** | Inventory 문서화 (이 3개 docs/) | docs/ 3개 | 0 |
| **1.5 (P1)** | ExperienceRecord 스키마 확장 | new fields + tests | 낮음 (default 값) |
| **2** | cron/watcher → ExperienceLog 통합 | `scripts/hermes_session_to_experience.py` 폴링 | 중간 |
| **3** | Skill 자동 promotion | Curator 가 `skills/promoted/` 후보 markdown | 낮음 (사람 review 단계) |
| **4** | Memory 임베딩 검색 | bge-m3 ollama embed + 코사인 | 중간 (default off 유지) |
| **5** | Telegram gateway + sub-agent delegation | 새 gateway/delegation 모듈 | 높음 (별 PR 권장) |
| **6** | installer_ops Kanban Phase 1 | Kanban 시스템 신설 | 높음 |
