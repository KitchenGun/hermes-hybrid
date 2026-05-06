# Profile Inventory

> ⚠️ **LEGACY: pre-Phase-8 reference (deprecated 2026-05-06)** ⚠️
>
> 이 문서는 폐기된 6 profile 구조를 설명하는 역사 자료다. Phase 8
> (2026-05-06) 에서 profile 구조 자체가 폐기되고 17 sub-agent 단일 구조로
> 전환됐다. 현행 자료는 [`AGENT_INVENTORY.md`](AGENT_INVENTORY.md) 와
> [`MASTER_ARCHITECTURE.md`](MASTER_ARCHITECTURE.md) 참조.
>
> 아래 내용은 변경 없이 보존 — git history 와 별개로 사람이 읽기 쉬운
> 형태의 사전 상태 스냅샷.

> 6개 프로필을 실제 파일(`config.yaml` + `SOUL.md`) 기준으로 정리한 인벤토리.
> Phase 1.5-A 산출물 — ExperienceRecord 스키마 확장(`profile_id` / `model_provider` / `memory_policy_id`)의 근거 문서.
>
> 분석일: 2026-05-06 (HEAD: 470fb7a)

---

## 1. 프로필 6개 메타데이터

| profile_id | 목적 | model | tier_policy | budget/day | approvals | memory | disabled_toolsets | 주요 trigger | discord 출력 | 판정 |
|---|---|---|---|---:|---|---|---:|---|---|---|
| **advisor_ops** | 도구 어드바이저(보고 전용) — 프로필 yaml + skills 트리 스캔 → 누락/개선 추천 | ollama / `qwen2.5-coder:32b-instruct` | prefer L3, max C1, 2 bump | 0.30 | manual | enabled | 7 | cron 1 + on_demand 1 | `DISCORD_BRIEFING_WEBHOOK_URL` | **유지** |
| **calendar_ops** | Google Calendar CRUD + 일정 브리핑 + 충돌 감지 + 패턴 분석 | custom / `qwen2.5:14b-instruct` | prefer L2, max C1, heavy=never, 2 bump | 0.20 | manual | enabled | 15 | cron 4 + on_demand 5 + watcher 2 | briefing + weather webhook | **유지** |
| **installer_ops** | Kanban worker — advisor 추천 task → install plan 생성 → comment 첨부 | ollama / `qwen2.5-coder:32b-instruct` | prefer L3, max C1, 2 bump | 0.20 | manual | enabled | 7 | **잡 0개** | kanban virtual channel | **보류** (Phase 6) |
| **journal_ops** | Discord #일기 채널 자연어 → 24-필드 JSON → Google Sheets | custom / `qwen3:8b` | prefer L2, max C1, heavy=never, 2 bump | 0.05 | **off** (HITL 없음) | enabled | 16 | on_demand 1 (forced_profile) | #일기 + alert webhook | **유지** |
| **kk_job** | 커리어 코치 + 구인 리서처 — 공고 크롤링 + 매칭 + 이력서 작성 | custom / `qwen2.5-coder:32b-instruct` | prefer **C1**, max C1, 2 bump | 0.50 | manual | enabled | **0** (전체 활성) | cron 3 + on_demand 4 + watcher 1 | `DISCORD_KK_JOB_WEBHOOK_URL` | **유지** |
| **mail_ops** | Gmail/Naver 받은편지함 모니터링 → Discord 알림 | custom / **`gpt-4o-mini`** (api.openai.com) | prefer L2, max **L2** (cloud 차단) | 0.05 | off (읽기 전용) | enabled | 0 | watcher 1 | `DISCORD_MAIL_WEBHOOK_URL` | **유지하되 OpenAI 직호출 검토** |

**근거 파일**: `profiles/{profile_id}/config.yaml` + `profiles/{profile_id}/SOUL.md` (모두 6/6 존재).

## 2. 디렉터리 빈 상태

| profile | cron/ | on_demand/ | watchers/ | skills/ | 비고 |
|---|---:|---:|---:|---:|---|
| advisor_ops | 1 | 1 | **0** | 1 | watchers 디렉터리 자체 없음 |
| calendar_ops | 4 | 5 | 2 | 2 | 완비 |
| installer_ops | **0** | **0** | **0** | 1 | **잡 시스템 미구현** — Phase 6 보류 |
| journal_ops | 0 (.gitkeep) | 1 | 0 (.gitkeep) | 1 | forced_profile 1잡만 (의도된 단순화) |
| kk_job | 3 | 4 | 1 | 5 | 완비 |
| mail_ops | 0 | 0 | 1 | **0** | watcher 만으로 작동, SKILL.md 없음 |

## 3. 모델 분포

| 모델 | 사용 프로필 수 | 프로필 |
|---|---:|---|
| `qwen2.5-coder:32b-instruct` (ollama) | 3 | advisor_ops, installer_ops, kk_job |
| `qwen2.5:14b-instruct` (custom→ollama) | 1 | calendar_ops |
| `qwen3:8b` (custom→ollama) | 1 | journal_ops |
| `gpt-4o-mini` (api.openai.com) | 1 | **mail_ops** |

**`mail_ops` 가 유일한 OpenAI 직호출 프로필** — 2026-05-04 OpenAI legacy 제거 후에도 살아있음. 의도성 검토 필요.

## 4. Discord 채널 / Webhook 매핑

| profile | 사용 webhook ENV | channel binding |
|---|---|---|
| advisor_ops | `DISCORD_BRIEFING_WEBHOOK_URL` | webhook |
| calendar_ops | `DISCORD_BRIEFING_WEBHOOK_URL`, `DISCORD_WEATHER_WEBHOOK_URL` | webhook |
| installer_ops | `DISCORD_BRIEFING_WEBHOOK_URL` (kanban virtual) | kanban (현재 미구현) |
| journal_ops | `JOURNAL_ALERT_WEBHOOK_URL` | **forced_profile = #일기 채널** |
| kk_job | `DISCORD_BRIEFING_WEBHOOK_URL`, `DISCORD_KK_JOB_WEBHOOK_URL` | webhook |
| mail_ops | `DISCORD_BRIEFING_WEBHOOK_URL`, `DISCORD_MAIL_WEBHOOK_URL` | webhook |

**`DISCORD_BRIEFING_WEBHOOK_URL` 가 5개 프로필에서 공유** — 실 Discord 채널 1개에 5개 페르소나가 글을 쓰는 형태. 사용자 mental model 확인 필요.

`journal_ops` 만 channel-pinned forced_profile (`#일기`) — 그 외는 webhook out-bound only.

## 5. ExperienceLog 가 기록할 profile 메타 (제안)

ExperienceRecord 에 추가 권장:

| 필드 | 도메인 | 의도 |
|---|---|---|
| `profile_id` | "advisor_ops"/"calendar_ops"/.../"mail_ops"/None | 어떤 경로로 결정됐든 최종 profile |
| `model_provider` | "ollama" / "custom" / "claude_cli" / None | 실제 사용된 backend |
| `memory_policy_id` | "enabled" / "disabled" / "inject" | P0-C inject 여부 포함 |

기존 `profile` 필드(JobFactory v1 매칭) 와 `forced_profile`(채널 고정)을 통합하는 것이 더 합리적.
