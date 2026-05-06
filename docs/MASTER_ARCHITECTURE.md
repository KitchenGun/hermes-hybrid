# Master Architecture (Phase 11, 2026-05-06)

> Diagram-aligned all-via-master design. Phase 8 에서 6 profile + 27 잡 +
> profile-local 10 SKILL.md 가 폐기되며 **17 sub-agent (`agents/`) 단일
> 구조** 로 수렴. Phase 9 에서 master 가 사용자 메시지의 ``@handle``
> mention 을 인식해 해당 agent 의 SKILL.md frontmatter 를 system prompt
> 에 inject. Phase 10 에서 ``master_parallel_agents=true`` 옵트인 시
> ``ClaudeAgentDelegator`` 가 각 ``@handle`` 별로 독립 claude 호출
> 을 ``asyncio.gather`` 로 동시 실행 후 결과 집계. **Phase 11 (2026-05-06):
> opencode CLI 폐기 + master = Claude CLI (Max OAuth) 단일 lane.** 모델
> default = opus. heavy / c1 / opencode 일괄 제거. Pre-Phase-8 reference
> 는 [`architecture.md`](architecture.md) (legacy).

## 1. 4-layer 다이어그램

```
                        ┌─────────────────────┐
                        │   Agent Layer       │   research / planning /
                        │   (17 sub-agents)    │   implementation / quality /
                        └──────────┬──────────┘   documentation / infrastructure
                                   │ exposed by
                        ┌──────────▼──────────┐
                        │  Slash Skills       │   /memo · /kanban · /hybrid-status
                        │   (4 deterministic)  │   /hybrid-budget
                        └──────────┬──────────┘
                                   │ uses
   Execution Modes ─────► Integration Layer ─────► Hermes Master Orchestrator
   ┌──────────────┐      ┌────────────────────┐    ┌──────────────────────┐
   │ on_demand    │      │ Intent Router      │    │  claude CLI          │
   │ (Discord/    │ ───► │ Policy Gate        │ ──►│  opus (Max OAuth)    │
   │  Telegram)   │      │ Agent Inventory    │    │  ($0 marginal)       │
   │              │      │ Session Importer   │    └──────────┬───────────┘
   └──────────────┘      └────────────────────┘               │ emits
                                                              │
                        ┌────────────────────────────────────▼─────────────┐
                        │  Outputs and Feedback                            │
                        │   ExperienceLog / Discord DM / Telegram          │
                        │   Sheets / Calendar / Docs / Kanban (via @devops)│
                        └──────────────────────────────────────────────────┘
```

Pre-Phase-8 의 "Domain Profiles (6)" + "Shared Skill Layer (10 SKILL.md)" 두
레이어는 폐기됐다. 그 자리를 17 sub-agent 가 직접 차지하며, 각 agent 는
자체 SKILL.md frontmatter 로 master 가 dispatch 시 필요한 정보 (when_to_use
/ inputs / outputs / 환경변수) 를 모두 담는다.

## 2. Integration Layer 4 컴포넌트

| 컴포넌트 | 위치 | 책임 |
|---|---|---|
| **Intent Router** | [src/integration/intent_router.py](../src/integration/intent_router.py) | 결정적 단락만 처리 — RuleLayer / 슬래시 skill + `@handle` mention 추출. 자유 텍스트는 master 가 결정. (Phase 8: forced_profile 폐기. Phase 11: heavy 폐기) |
| **Policy Gate** | [src/integration/policy_gate.py](../src/integration/policy_gate.py) | allowlist / 일일 토큰 cap. (Phase 8: HITL `requires_confirmation` 분기 폐기 — profile yaml 의존이었음) Validator wrap 으로 retry/tier post-validate. |
| **Agent Inventory** | [src/integration/job_inventory.py](../src/integration/job_inventory.py) | 클래스 이름 호환을 위해 `JobInventory` 그대로. 책임은 `agents/` 17 sub-agent runtime 스캔만. master 가 `@coder` 등 핸들 lookup 에 사용. |
| **Session Importer** | [src/integration/session_importer.py](../src/integration/session_importer.py) | claude/hermes 의 session JSON → ExperienceLog 통합 (hourly systemd timer). |

## 3. Hermes Master Orchestrator

[src/orchestrator/hermes_master.py](../src/orchestrator/hermes_master.py) 의 `HermesMasterOrchestrator` 가 모든 진입의 단일 처리.

- **단일 LLM lane**: `claude` CLI (`/home/kang/.local/bin/claude`) 를 subprocess 로 호출, `opus` 사용. Claude Max 구독 OAuth 사용 — API key 불필요, $0 marginal.
- **흐름**:
  1. `_maybe_inject_memory` (옵트인)
  2. `IntentRouter.route` → IntentResult (RuleLayer / 슬래시 skill / fallthrough + `@handle` mention 추출)
  3. RuleLayer 매치 → instant 응답
  4. 슬래시 skill 매치 → skill.invoke
  5. `PolicyGate.pre_dispatch` → allowlist/budget 게이트
  6. `_compose_prompt` → system prompt + (Phase 9) ``@handle`` SKILL.md frontmatter snippet inject + user message
  7. `_dispatch_master` → adapter.run → `Critic.evaluate`
  8. `_finalize` → `ExperienceLogger.append` (`model_provider="claude_cli"`, `agent_handles=[...]`)

### Phase 9 — `@handle` mention dispatch (default)

IntentRouter 가 정규식 `(?<![\w.])@(\w+)` 으로 사용자 메시지에서 mention
후보를 추출하고, AgentRegistry 로 검증해 등록된 핸들만 `IntentResult.
agent_handles` 에 stamp. master 의 `_compose_prompt` 가 각 핸들에 대해
SKILL.md frontmatter (role / description / when_to_use / not_for /
inputs / outputs / primary_tools) 를 짧은 snippet 으로 변환해 system
prompt 에 prepend. 이로써 master 가 sub-agent 의 책임/경계/입출력을
따라 응답하도록 유도.

inject 형식:
```
## Active sub-agent: @coder (role: write_new_code)
description: ...
when_to_use:
  - ...
not_for:
  - ...
inputs: ...
outputs: ...
primary_tools: ...
```

ExperienceLog 의 `agent_handles` 필드로 향후 Curator 가 agent 별 사용
빈도 / 성공률 통계 산출 가능.

### Phase 10 — Parallel `@handle` dispatch (opt-in)

`settings.master_parallel_agents=true` 일 때 + `len(task.agent_handles)
>= 2` 이면, master 가 **단일 호출 대신** [`ClaudeAgentDelegator`](../src/core/delegation.py)
로 라우팅:

```
@coder + @reviewer + @tester
        ↓ delegate_many (asyncio.gather)
    sem = Semaphore(master_parallel_max_concurrency)
        ↓ 동시 claude subprocess 호출 (각각 자체 SKILL.md system prompt)
    [SubAgentResult, SubAgentResult, SubAgentResult]
        ↓ aggregate_responses
    "### @coder\n...\n\n### @reviewer\n...\n\n### @tester\n..."
        ↓ HermesMaster._dispatch_parallel_agents
    각 sub-call 의 prompt/completion tokens 가 model_outputs 에
    substage='parallel:@<handle>' 로 기록
```

handled_by 값:
- 모두 성공 → `master:parallel`
- 일부 성공 → `master:parallel_partial` (degraded=True)
- 모두 실패 → `master:parallel_failed`

비용/지연이 N 배 — 명시 opt-in 만. 사용 사례: `@coder` 가 코드 작성하고
`@tester` 가 동시에 테스트 시안 만드는 식의 **독립 작업 동시 실행**. 단순
연속 (`@coder` 결과를 `@reviewer` 가 받아서 검토) 은 단일 호출 (Phase 9)
이 더 효율적.

## 4. Agent Layer (17 sub-agents)

`agents/{category}/{name}/SKILL.md` 글로벌 디렉터리. `AgentRegistry`
([src/agents/__init__.py](../src/agents/__init__.py)) 가 스캔, `JobInventory.agents()` 가 master 에 노출.

| 카테고리 | agents | 책임 요지 |
|---|---|---|
| **RESEARCH** | @finder · @analyst · @researcher | 위치 / 분석 / 외부 조사 (web_search · job_crawler · 캘린더 read) |
| **PLANNING** | @architect · @planner | 시스템 설계 / 작업 분해 |
| **IMPLEMENTATION** | @coder · @editor · @fixer · @refactorer | 신규 작성 / 외과적 수정 / 버그 fix / 구조 개선 |
| **QUALITY** | @reviewer · @tester · @debugger · @security | 리뷰 / 테스트 / 진단 / 보안 |
| **DOCUMENTATION** | @documenter · @commenter | 외부 문서 (README/이력서/자소서) / 인라인 주석 |
| **INFRASTRUCTURE** | @devops · @optimizer | 배포·운영 + Discord/Sheets/Calendar 발송 + install plan / 성능 |

각 SKILL.md 의 frontmatter 표준:
```yaml
name: coder
agent_handle: "@coder"
category: implementation
role: write_new_code
description: ...
when_to_use: [...]
not_for: [...]
inputs: [...]
outputs: [...]
metadata:
  hermes:
    primary_tools: [write, edit]
    tags: [...]
absorbed_from:        # Phase 8 흡수 매핑 (optional)
  - profiles/<...>/skills/<...>
```

`AgentRegistry.by_handle("@coder")` 또는 case-insensitive `"coder"` 로
조회. master 가 사용자 입력에서 `@coder` 같은 멘션을 발견하면 해당
SKILL.md 를 system prompt 에 inject (Phase 9 wiring).

## 5. Outputs and Feedback

| 출력 | 트리거 | 위치 |
|---|---|---|
| **ExperienceLog** | 모든 task 종료 (master + claude session import) | `logs/experience/{YYYY-MM-DD}.jsonl` |
| **ReflectionJob (markdown)** | systemd timer 일요일 22:00 KST | `logs/reflection/{ISO-WEEK}.md` |
| **CuratorJob (markdown + JSON)** | systemd timer 일요일 23:00 KST | `logs/curator/{date}.md` + `handled_by_stats.json` |
| **Discord/Telegram** | gateway 응답 | bot 채널 |
| **Google Sheets** | @devops sheets_append (사용자 명시 요청 시) | Apps Script doPost |
| **Google Calendar** | @devops cocal MCP (write) / @researcher (read) | Google Calendar API |
| **Docs / 이력서·자소서** | @documenter document_writer | `runtime/documents/` |
| **Kanban tasks** | agent 간 hand-off 채널 | `data/kanban.json` |

## 6. Phase 8 폐기 정리 (2026-05-06)

다음 컴포넌트는 Phase 8 도입과 함께 완전 삭제됨:

**디렉터리 / 자산**:
- `profiles/` 전체 (6 디렉터리, 27 잡 yaml, profile-local 10 SKILL.md, sessions/runtime/probes)
- `src/orchestrator/profile_loader.py`
- `src/watcher/` (cron/poll watcher 인프라)
- `src/gateway/dm_dispatcher.py`, `src/gateway/confirm_view.py` (HITL view)
- `src/skills/calendar.py` (CalendarSkill)

**Settings 죽은 필드**:
- `profiles_dir` / `journal_channel_id` / `google_sheets_webhook_url` / `journal_alert_webhook_url`
- `journal_ops_*` / `calendar_skill_*` / `mail_skill_enabled`
- `hitl_*` / `watcher_*` / `claude_enabled` / `job_factory_*` / `use_new_job_factory`
- `score_matrix_path` / `allow_profile_creation` / `disable_v1_jobfactory`

**Orchestrator HITL 메서드 6개**:
- `requires_confirmation` / `enter_confirmation_gate` / `record_confirmation_message` / `resume_after_confirmation` / `list_pending_confirmations` / `build_preview`

**자동화 (사용자 인지 필수)**:
- morning_briefing / weather_briefing / daily_wrap / weekly_preview
- focus_time_report / monthly_pattern / weekly_retrospective
- morning_game_jobs / deadline_reminder / weekly_job_digest
- weekly_advisor_scan
- log_activity (#일기 forced_profile)
- new_mail_alert / new_posting_alert
- conflict_detector / new_invitation_handler
- add/update/delete/find/quick_block_event (on_demand)

→ 사용자가 master 한테 매번 명시 요청. 예: "오늘 일정 알려줘" → master
가 @researcher 에게 캘린더 read 위임 → 응답.

**스크립트 19개**:
- register_cron_jobs.py / validate_all_crons.py / check_jobs_meta.py
- inject_strict_rules.py / install_gateway_units.sh / refresh_ollama_base_urls.sh
- bootstrap_profile.sh / patch_jobs_baseurl.py / check_profile_envs.sh
- 그 외 calendar_ops / journal_ops / kk_job 운영 보조 shell

**테스트 4개**:
- test_calendar_skill / test_calendar_watcher / test_mail_watcher / test_profile_loader (이미 Phase 6 에서 제거됨)

검증: `pytest -q` → **269 passed / 5 skipped / 0 failed**.

## 7. 변수 마이그레이션

profile 폐기 후에도 사용자가 별도 작업 없이 agent 호출에 동일 환경변수를
사용할 수 있도록 inventory 보존:

- `.env.example` (root) — 모든 agent 변수 stub
- `_archive/profiles_envs/{profile}/.env.template` — 폐기 profile 별 변수
  명만 (값 없음, git tracked)
- `docs/AGENT_ENV.md` — agent ↔ 변수 매핑 카탈로그

사용자 마이그레이션 워크플로:
1. `cat profiles/calendar_ops/.env profiles/journal_ops/.env` (P8.4 직전)
2. 비어있지 않은 값들을 root `.env` 로 통합
3. P8.4 commit (`profiles/` git rm) 후 봇 재시작

## 8. 향후 (Phase 11+)

- ✅ **Phase 9 (2026-05-06)** — Master 가 IntentRouter 결과의 `@coder`
  같은 mention 인식 → AgentRegistry.by_handle → agent SKILL.md 를 system
  prompt 에 inject. ExperienceLog 에 `agent_handles` 기록.
- ✅ **Phase 10 (2026-05-06)** — `ClaudeAgentDelegator.delegate_many` 진짜
  병렬 실행 (`asyncio.gather` + `Semaphore(max_concurrency)`) + `aggregate_
  responses` 로 agent 간 결과 집계. opt-in: `master_parallel_agents=true`.
- **Phase 11** — Slack gateway / Discord 슬래시 명령 확장.
- **Phase 12** — Master meta-aggregator (Phase 10 의 단순 concat 대신 LLM
  round-trip 으로 sub-agent 결과들을 종합 응답으로 변환).
- **Phase 13** — Curator 의 agent_handles 기반 통계 자동 surface (어느
  agent 가 자주 호출되고 어느 agent 가 자주 실패하는지).
