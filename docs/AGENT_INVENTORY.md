# Agent Inventory (Phase 8, 2026-05-06)

> 17 sub-agent 의 핸들 / 책임 / 흡수된 profile-local 자산 / 환경변수 매핑.
> Phase 8 산출물. Phase 7 의 [`MASTER_ARCHITECTURE.md`](MASTER_ARCHITECTURE.md) §6 표를 확장.

## 1. 6 카테고리 / 17 agent 일람

각 SKILL.md 는 `agents/{category}/{name}/SKILL.md` 에 위치. 클릭 가능한
링크는 SKILL.md 본문.

### RESEARCH (3)

| 핸들 | role | 책임 |
|---|---|---|
| [@finder](../agents/research/finder/SKILL.md) | locate | 코드/파일 위치 빠른 검색 (Grep/Glob 1회) |
| [@analyst](../agents/research/analyst/SKILL.md) | analyze | 코드/데이터 정적 분석 + codebase inventory scan |
| [@researcher](../agents/research/researcher/SKILL.md) | external_research | 외부 웹/문서/패키지/공고 조사 + 캘린더 read + 잡 크롤링 |

### PLANNING (2)

| 핸들 | role | 책임 |
|---|---|---|
| [@architect](../agents/planning/architect/SKILL.md) | system_design | 시스템 다이어그램 / 모듈 경계 / 트레이드오프 |
| [@planner](../agents/planning/planner/SKILL.md) | task_breakdown | 단일 작업의 단계 분해 + 의존성 |

### IMPLEMENTATION (4)

| 핸들 | role | 책임 |
|---|---|---|
| [@coder](../agents/implementation/coder/SKILL.md) | write_new_code | 새 모듈/기능 작성 (greenfield) |
| [@editor](../agents/implementation/editor/SKILL.md) | surgical_edit | 외과적 수정 (1~10줄 패치) |
| [@fixer](../agents/implementation/fixer/SKILL.md) | bug_fix | 버그 진단 + 수정 |
| [@refactorer](../agents/implementation/refactorer/SKILL.md) | structure_change | 동작 보존하면서 구조 개선 |

### QUALITY (4)

| 핸들 | role | 책임 |
|---|---|---|
| [@reviewer](../agents/quality/reviewer/SKILL.md) | code_review | 변경 PR 코드 리뷰 |
| [@tester](../agents/quality/tester/SKILL.md) | write_tests | 단위/통합 테스트 작성 |
| [@debugger](../agents/quality/debugger/SKILL.md) | diagnose | 런타임 에러 / 회귀 진단 |
| [@security](../agents/quality/security/SKILL.md) | security_audit | 보안 결함 / 시크릿 노출 탐지 |

### DOCUMENTATION (2)

| 핸들 | role | 책임 |
|---|---|---|
| [@documenter](../agents/documentation/documenter/SKILL.md) | write_docs | README/runbook/이력서/자소서 작성 (md/docx/pdf) |
| [@commenter](../agents/documentation/commenter/SKILL.md) | inline_comments | 인라인 docstring/주석 추가 |

### INFRASTRUCTURE (2)

| 핸들 | role | 책임 |
|---|---|---|
| [@devops](../agents/infrastructure/devops/SKILL.md) | deploy_and_ops | systemd unit / .env / CI / Discord webhook / Sheets append / Calendar CRUD / install plan |
| [@optimizer](../agents/infrastructure/optimizer/SKILL.md) | performance | 핫스팟 분석 + 최적화 |

## 2. Phase 8 흡수 매핑

폐기된 profile-local 10 SKILL.md 의 운영 정보는 4 agent 에 통합:

| 폐기된 profile-local SKILL.md | 흡수 agent | 추가 책임 |
|---|---|---|
| `advisor_ops/skills/analysis/job_inventory` | **@analyst** | codebase / yaml inventory 스캔 + 추천 |
| `calendar_ops/skills/productivity/google_calendar` (read) | **@researcher** | list-events / get-event MCP |
| `calendar_ops/skills/productivity/google_calendar` (write) | **@devops** | create/update/delete-event MCP |
| `calendar_ops/skills/messaging/discord_notify` | **@devops** | Discord webhook 발송 |
| `installer_ops/skills/install/auto_install` | **@devops** | install plan 작성 + Kanban comment 첨부 |
| `journal_ops/skills/storage/sheets_append` | **@devops** | Google Sheets webhook append (24-필드 JSON) |
| `kk_job/skills/research/web_search` | **@researcher** | Brave/Tavily/Exa 검색 |
| `kk_job/skills/research/job_crawler` | **@researcher** | gamejob/jobkorea/Nexon/NC/Netmarble 크롤링 |
| `kk_job/skills/productivity/document_writer` | **@documenter** | 이력서/자소서 (md/docx/pdf) |
| `kk_job/skills/messaging/discord_notify` | **@devops** | (calendar_ops 와 통합) |
| `kk_job/skills/storage/sheets_append` | **@devops** | (journal_ops 와 통합) |

각 흡수처 SKILL.md frontmatter 에 `absorbed_from:` 필드로 출처 표시.

## 3. 환경변수 → agent 매핑

자세한 카탈로그는 [`AGENT_ENV.md`](AGENT_ENV.md) 참조. 요약:

| 변수 | 사용 agent |
|---|---|
| `BRAVE_SEARCH_API_KEY` | @researcher |
| `GOOGLE_OAUTH_CREDENTIALS` | @researcher (read) + @devops (write) |
| `GOOGLE_CALENDAR_MCP_TOKEN_PATH` | @researcher + @devops |
| `GOOGLE_CALENDAR_ID` | @researcher + @devops |
| `DISCORD_*_WEBHOOK_URL` (5종) | @devops |
| `GOOGLE_SHEETS_WEBHOOK_URL` | @devops |
| `JOB_SHEETS_WEBHOOK_URL` | @devops |
| `JOURNAL_ALERT_WEBHOOK_URL` | @devops |
| `HERMES_KANBAN_TASK` / `HERMES_TENANT` | @devops (auto_install) |
| `NAVER_APP_PASSWORD` | master (mail polling) |
| `TIMEZONE` / `LOCALE` | 공통 |

## 4. 호출 방법 (Phase 9 시점)

**Phase 9 (2026-05-06) 부터 자동 wiring 활성화**:

- 사용자가 메시지에 `@coder` / `@reviewer` 같은 mention 을 포함하면
  IntentRouter (`src/integration/intent_router.py`) 가 정규식
  `(?<![\w.])@(\w+)` 으로 후보를 추출하고 AgentRegistry 로 검증.
- 알려진 핸들만 IntentResult.agent_handles 에 stamp (e.g. `["@coder",
  "@reviewer"]`). email-ish text (`user@example.com`) / 미등록 핸들은
  자동 필터링.
- HermesMaster 가 system prompt 에 각 agent 의 SKILL.md frontmatter
  snippet 을 inject:
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
- TaskState.agent_handles + ExperienceRecord.agent_handles 로 ExperienceLog
  에 기록 → 향후 Curator 가 agent 별 사용 빈도 통계 가능.
- mention 없이 자유 텍스트 → master 가 자체 추론으로 적절한 도구 호출
  (전과 동일 동작).
- RuleLayer / 슬래시 skill 단락 시에도 mention 은 stamp 되지만 master 가
  호출되지 않으므로 prompt inject 는 건너뜀.

## 5. AgentRegistry API

```python
from src.integration import JobInventory
from src.agents import AgentRegistry

# JobInventory 가 가장 일반적인 진입
inv = JobInventory()  # repo_root 자동 감지
agents = inv.agents()              # list[AgentEntry] (17)
coder = inv.agent_by_handle("@coder")
research = inv.agents_by_category("research")  # @finder, @analyst, @researcher

# 직접 AgentRegistry 사용도 가능
reg = AgentRegistry(repo_root=Path.cwd())
print(reg.summary())  # {"implementation": 4, "quality": 4, ...}
```

`AgentEntry` 필드:
- `handle: str` — `"@coder"` 형식
- `name: str` — `"coder"` (no @)
- `category: str` — `"implementation"` 등 6개
- `role: str` — `"write_new_code"` 등
- `description: str`
- `when_to_use: list[str]` / `not_for: list[str]`
- `inputs: list[str]` / `outputs: list[str]`
- `metadata: dict[str, Any]` — frontmatter `metadata.hermes` 통째로
- `version: str` / `absorbed_from: list[str]` (optional)
- `skill_md_path: Path` — 원본 파일 경로

## 6. 향후

- ✅ **Phase 9 (2026-05-06)** — `@agent` 멘션 dispatch wiring 완료
  (IntentRouter 파싱 → master prompt 에 SKILL.md frontmatter inject →
  ExperienceLog 기록).
- ✅ **Phase 10 (2026-05-06)** — `OpenCodeAgentDelegator.delegate_many`
  진짜 병렬 실행 (`asyncio.gather` + `Semaphore(max_concurrency)`)
  + `aggregate_responses` 로 결과 집계. opt-in `master_parallel_agents=true`.
  - `@coder + @tester` 동시 호출 (독립 작업) 패턴 활용.
  - 단순 연속 (`@coder → @reviewer`) 은 Phase 9 단일 호출이 더 효율적.
- Phase 11 — Slack gateway / Discord 슬래시 `/agent <handle> <task>` 추가.
- Phase 12 — Master meta-aggregator (LLM round-trip 으로 sub-agent 결과
  종합).
- Phase 13 — Curator 의 agent_handles 통계 자동 surface.
