# Master Architecture (2026-05-06)

> Diagram-aligned all-via-master design. Supersedes the Tier ladder /
> JobFactory v2 / Router-based architecture documented in
> [`architecture.md`](architecture.md) (legacy reference).

## 1. 4-layer 다이어그램

```
                        ┌─────────────────────┐
                        │   Domain Profiles   │   advisor_ops / calendar_ops /
                        │   (6 페르소나)        │   installer_ops / journal_ops /
                        └──────────┬──────────┘   kk_job / mail_ops
                                   │ owns
                        ┌──────────▼──────────┐
                        │  Shared Skill Layer │   discord_notify / sheets_append /
                        │   (10 SKILL.md)      │   google_calendar / web_search /
                        └──────────┬──────────┘   job_crawler / document_writer ...
                                   │ uses
   Execution Modes ─────► Integration Layer ─────► Hermes Master Orchestrator
   ┌──────────────┐      ┌────────────────────┐    ┌──────────────────────┐
   │ on_demand 12 │      │ Intent Router      │    │  opencode CLI        │
   │ watcher 4    │ ───► │ Policy Gate        │ ──►│  gpt-5.5             │
   │ cron 11      │      │ Job Inventory      │    │  ($0 marginal)       │
   │ forced 1     │      │ Session Importer   │    └──────────┬───────────┘
   └──────────────┘      └────────────────────┘               │ emits
                                                              │
                        ┌────────────────────────────────────▼─────────────┐
                        │  Outputs and Feedback                            │
                        │   ExperienceLog / Discord DM / Telegram / Sheets │
                        │   Calendar / Docs / resume drafts / Kanban       │
                        └──────────────────────────────────────────────────┘
```

## 2. Integration Layer 4 컴포넌트

| 컴포넌트 | 위치 | 책임 |
|---|---|---|
| **Intent Router** | [src/integration/intent_router.py](../src/integration/intent_router.py) | 결정적 단락만 처리 — RuleLayer / 슬래시 skill / forced_profile / heavy. 자유 텍스트는 master 가 결정. |
| **Policy Gate** | [src/integration/policy_gate.py](../src/integration/policy_gate.py) | allowlist / 일일 토큰 cap / `requires_confirmation` 단일 contract. Validator wrap 으로 retry/tier post-validate. |
| **Job Inventory** | [src/integration/job_inventory.py](../src/integration/job_inventory.py) | profiles/ + skills/ runtime 스캔. master 가 prompt 구성에 사용. |
| **Session Importer** | [src/integration/session_importer.py](../src/integration/session_importer.py) | hermes 의 cron/watcher 세션 JSON → ExperienceLog 통합 (hourly systemd timer). |

## 3. Hermes Master Orchestrator

[src/orchestrator/hermes_master.py](../src/orchestrator/hermes_master.py) 의 `HermesMasterOrchestrator` 가 모든 진입의 단일 처리.

- **단일 LLM lane**: `opencode` CLI (`/home/kang/.local/bin/opencode`) 를 subprocess 로 호출, `gpt-5.5` 사용. Claude Code CLI Max OAuth 와 같은 $0 marginal 패턴 — API key 불필요.
- **흐름**:
  1. `_maybe_inject_memory` (옵트인)
  2. `IntentRouter.route` → IntentResult
  3. RuleLayer 매치 → instant 응답
  4. 슬래시 skill 매치 → skill.invoke
  5. `PolicyGate.pre_dispatch` → allowlist/budget/HITL 게이트
  6. `_dispatch_master` → opencode.run → `Critic.evaluate`
  7. `_finalize` → `ExperienceLogger.append` (`model_provider="opencode"`)

## 4. Outputs and Feedback

| 출력 | 트리거 | 위치 |
|---|---|---|
| **ExperienceLog** | 모든 task 종료 (master + cron sub-call import) | `logs/experience/{YYYY-MM-DD}.jsonl` |
| **ReflectionJob (markdown)** | systemd timer 일요일 22:00 KST | `logs/reflection/{ISO-WEEK}.md` |
| **CuratorJob (markdown + JSON)** | systemd timer 일요일 23:00 KST | `logs/curator/{date}.md` + `handled_by_stats.json` |
| **Discord/Telegram** | gateway 응답 | bot 채널 |
| **Google Sheets** | journal_ops / kk_job webhook | Apps Script doPost |
| **Google Calendar** | calendar_ops MCP | google_calendar API |
| **Docs / resume drafts** | kk_job document_writer skill | profile runtime/ |
| **Kanban tasks** | advisor_ops 발행 → installer_ops 처리 | `data/kanban.json` |

## 5. 레거시 정리 (2026-05-06)

다음 컴포넌트는 master 도입과 함께 완전 삭제됨:

- `src/job_factory/*` — JobFactory v1 + v2 dispatcher / classifier / policy / selector / runner / registry / score_matrix / bench/
- `src/router/router.py` — Router (Provider / Route / RouterDecision)
- Orchestrator 의 `_dispatch_with_retries` / `_execute_once` / `_run_local` / `_run_worker` / `_run_c1` / `_run_c2` / `_handle_via_job_factory_v2` / `HeavySessionRegistry` 사용
- Settings 의 `use_hermes_for_*` / `trust_hermes_reflection` / `router_conf_*` / `cloud_escalation_max` / `surrogate_max_tokens_*` / `claude_call_budget_session` / `effective_use_hermes_*` properties

레거시 의존 테스트 22 파일 삭제. 새 master 흐름은 `tests/test_hermes_master.py` + `tests/test_integration_*.py` + `tests/test_opencode_adapter.py` 가 검증.

## 6. Phase 7 — 6 카테고리 / 17 sub-agent (2026-05-06 완료)

`agents/{category}/{name}/SKILL.md` 글로벌 디렉터리. `AgentRegistry`
([src/agents/__init__.py](../src/agents/__init__.py)) 가 스캔, `JobInventory.agents()` 가 master 에 노출.

| 카테고리 | agents | 책임 요지 |
|---|---|---|
| **RESEARCH** | @finder · @analyst · @researcher | 위치 / 분석 / 외부 조사 |
| **PLANNING** | @architect · @planner | 시스템 설계 / 작업 분해 |
| **IMPLEMENTATION** | @coder · @editor · @fixer · @refactorer | 신규 작성 / 외과적 수정 / 버그 fix / 구조 개선 |
| **QUALITY** | @reviewer · @tester · @debugger · @security | 리뷰 / 테스트 / 진단 / 보안 |
| **DOCUMENTATION** | @documenter · @commenter | 외부 문서 / 인라인 주석 |
| **INFRASTRUCTURE** | @devops · @optimizer | 배포·운영 / 성능 |

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
```

`AgentRegistry.by_handle("@coder")` 또는 case-insensitive `"coder"` 로
조회. master 가 사용자 입력에서 `@coder` 같은 멘션을 발견하면 해당
SKILL.md 를 system prompt 에 inject.

## 7. 향후 (Phase 8+)

- **Phase 8** — Master 가 `@agent` 멘션 → 해당 agent SKILL.md 를 prompt
  에 inject 하는 dispatch wiring (현재는 인덱스만, master 호출 wiring X).
- **Phase 9** — `Delegator.delegate_many` 의 진짜 병렬 실행 + agent
  간 결과 집계.
- **Phase 10** — Slack gateway / Discord 슬래시 명령 확장.
