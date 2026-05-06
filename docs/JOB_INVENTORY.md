# Job Inventory

> `profiles/*/{cron,on_demand,watchers}/**.yaml` 26개 + `src/job_factory/` v2 분류 + `src/skills/` 슬래시 스킬을 실 파일 기준으로 정리한 인벤토리.
> Phase 1.5-B 산출물 — ExperienceRecord 의 routing 필드 확장 근거.
>
> 분석일: 2026-05-06 (HEAD: 470fb7a)

---

## 1. 전체 26개 job

| job_id | profile_id | trigger_type | category | tier(max/prefer) | schedule/source | budget | skills | delivery | requires_confirm |
|---|---|---|---|---|---|---:|---|---|---|
| weekly_advisor_scan | advisor_ops | cron | analyze | C1/L3 | `0 7 * * 0` KST | 0.15 | analysis/job_inventory | webhook | true |
| advise_now | advisor_ops | on_demand | analyze | C1/L3 | intent patterns | 0.10 | analysis/job_inventory | webhook | true |
| morning_briefing | calendar_ops | cron | read | L2/L2 | `0 8 * * *` | 0.00 | google_calendar, discord_notify | webhook | false |
| weather_briefing | calendar_ops | cron | read | L2/L2 | `30 7 * * *` | 0.00 | discord_notify | webhook | false |
| daily_wrap | calendar_ops | cron | read | L2/L2 | `0 22 * * *` | 0.00 | google_calendar, discord_notify | webhook | false |
| weekly_preview | calendar_ops | cron | read | L2/L2 | `0 20 * * 0` | 0.00 | google_calendar, discord_notify | webhook | false |
| focus_time_report | calendar_ops | cron | analyze | C1/L2 | `0 9 * * 1` | 0.05 | google_calendar, discord_notify | webhook | false |
| monthly_pattern | calendar_ops | cron | analyze | C1/L2 | `0 9 1 * *` | 0.10 | google_calendar, discord_notify | webhook | false |
| weekly_retrospective | calendar_ops | cron | analyze | C1/L2 | `0 19 * * 5` | 0.08 | google_calendar, discord_notify | webhook | false |
| add_event | calendar_ops | on_demand | write | C1/L2 | intent patterns | 0.02 | google_calendar, discord_notify | webhook | **true** |
| update_event | calendar_ops | on_demand | write | C1/L2 | intent patterns | 0.03 | google_calendar, discord_notify | webhook | **true** |
| delete_event | calendar_ops | on_demand | write | L2/L2 | intent patterns | 0.01 | google_calendar, discord_notify | webhook | **true** |
| find_event | calendar_ops | on_demand | read | L2/L2 | intent patterns | 0.01 | google_calendar, discord_notify | webhook | false |
| quick_block | calendar_ops | on_demand | write | L2/L2 | intent patterns | 0.00 | google_calendar | **silent** | false |
| conflict_detector | calendar_ops | watcher_event | watcher | L2/L2 | `internal.calendar_write_completed` | 0.00 | google_calendar, discord_notify | dm | false |
| new_invitation_handler | calendar_ops | watcher_event | watcher | L2/L2 | `google_calendar.push_notification` | 0.00 | google_calendar, discord_notify | dm | false |
| log_activity | journal_ops | forced_profile | write | C1/L2 | #일기 채널 | 0.01 | sheets_append | webhook | false |
| morning_game_jobs | kk_job | cron | read | C1/C1 | `10 7 * * *` | 0.10 | job_crawler, sheets_append, discord_notify | webhook | false |
| weekly_job_digest | kk_job | cron | read | C1/L2 | `0 9 * * 1` | 0.05 | web_search, discord_notify | webhook | false |
| deadline_reminder | kk_job | cron | read | L2/L2 | `0 8 * * *` | 0.00 | discord_notify | webhook | false |
| analyze_posting | kk_job | on_demand | analyze | C1/C1 | intent patterns | 0.08 | web_search, discord_notify | webhook | false |
| draft_resume | kk_job | on_demand | write | C1/C1 | intent patterns | 0.10 | document_writer, discord_notify | webhook | true |
| search_jobs | kk_job | on_demand | read | C1/L2 | intent patterns | 0.05 | web_search, discord_notify | webhook | false |
| update_career | kk_job | on_demand | write | L2/L2 | intent patterns | 0.01 | discord_notify | webhook | true |
| new_posting_alert | kk_job | watcher_poll | watcher | L2/L2 | `rss_poll` 3600s | 0.00 | discord_notify | webhook | false |
| new_mail_alert | mail_ops | watcher_poll | watcher | L2/L2 | `mail_poll` 300s | 0.00 | mail (Python), discord_notify | webhook | false |

**총 26**: cron 10 / on_demand 13 / watcher_event 2 / watcher_poll 2 / forced_profile 1.

## 2. Trigger 타입 5종

| trigger_type | 카운트 | 식별 방식 |
|---|---:|---|
| `cron` | 10 | `trigger.type: cron` + cron expression |
| `on_demand` | 13 | `trigger.type: on_demand` + intent patterns |
| `watcher_event` | 2 | `trigger.type: watcher` + `source: internal.{event}` 또는 `{tool}.push_notification` |
| `watcher_poll` | 2 | `trigger.type: watcher` + `source.type: rss_poll/mail_poll` + `interval_seconds` |
| `forced_profile` | 1 | journal_ops `log_activity` (Discord #일기 채널) |

## 3. ID 중복 / orphan 분석

- **job_id 중복 0건** — 26개 unique (cross-profile 포함)
- **orphan 0건** — yaml 의 `name:` 필드가 register_cron_jobs.py 와 매칭
- **단**: SKILL.md 의 `sheets_append` 는 journal_ops + kk_job 둘 다 존재. SkillEntry id 는 `{profile}/{category}/{name}` 이라 충돌 없음 — 단 사용자 mental model 에서 "sheets_append 가 두 개" 라는 점 인지 필요.

## 4. SKILL.md 10개 (profile-level) vs 슬래시 skill 4개 (별개 시스템)

**Profile-level SKILL.md** (`profiles/*/skills/**/SKILL.md`):

| SkillEntry id |
|---|
| advisor_ops/analysis/job_inventory |
| calendar_ops/messaging/discord_notify |
| calendar_ops/productivity/google_calendar |
| installer_ops/install/auto_install |
| journal_ops/storage/sheets_append |
| kk_job/messaging/discord_notify |
| kk_job/productivity/document_writer |
| kk_job/research/job_crawler |
| kk_job/research/web_search |
| kk_job/storage/sheets_append |

**슬래시 skill** (`src/skills/__init__.py:default_registry`):

| name | match | 조건 |
|---|---|---|
| `calendar` | regex (일정/캘린더/미팅/약속) | `calendar_skill_enabled=True` |
| `hybrid-status` | `^/hybrid-status$` | always |
| `hybrid-budget` | `^/hybrid-budget$` | always |
| `hybrid-memo` | `/memo (save\|list\|clear)...` | always |

→ **두 시스템이 별개**. ExperienceRecord 의 `handled_by="skill:hybrid-memo"` 는 슬래시. `tool_calls.tool="discord_notify"` 는 SKILL.md 일 수 있음. **명명 정리 필요** (예: `slash_skill` vs `skill_ids` 별도 필드).

## 5. JobFactory v2 의 JobType 10개 (`config/job_factory.yaml`)

`profiles/*/jobs` 와 별개 분류. v2 dispatcher 가 자유 텍스트 메시지를 분류:

`simple_chat`, `summarize`, `code_review`, `code_generation`, `architecture_design`, `web_research`, `document_transform`, `schedule_logging`, `image_asset_request`, `heavy_project_task`

→ **cron 잡은 v2 를 거치지 않음** (cron 은 hermes 가 직접 schedule).
→ ExperienceLog 는 두 차원의 분류를 모두 stamp 해야 함:
  - `job_id` (yaml 의 26개 또는 슬래시 skill name)
  - `v2_job_type` (10개 중 하나, 자유 텍스트일 때만)

## 6. ExperienceLog 가 기록할 추가 routing 필드 (제안)

기존 `profile` / `forced_profile` / `heavy` / `route` / `tier` / `handled_by` 외에 추가:

| 필드 | 타입 | 의도 |
|---|---|---|
| `job_id` | str \| None | yaml 의 `name:` (e.g. "morning_briefing") 또는 슬래시 name 또는 None (자유 텍스트) |
| `job_category` | str | "read" / "write" / "analyze" / "monitor" / "watcher" / "chat" |
| `trigger_type` | str | "discord_message" / "cron" / "watcher_event" / "watcher_poll" / "forced_profile" / "manual" |
| `trigger_source` | str \| None | cron expr 또는 `internal.{event}` 또는 `rss_poll://...` 또는 user_id |
| `v2_job_type` | str \| None | v2 분류 결과 (10종 중 하나) |
| `v2_classification_method` | str \| None | "keyword" / "llm" / "fallback" |
| `skill_ids` | list[str] | SKILL.md SkillEntry id 들 |
| `slash_skill` | str \| None | 슬래시 skill name |
| `model_provider` | str \| None | "ollama" / "custom" / "claude_cli" |
| `model_name` | str \| None | 실제 호출 모델 (local 도 포함) |
| `memory_inject_count` | int | P0-C inject 에서 들어간 메모 수 |
