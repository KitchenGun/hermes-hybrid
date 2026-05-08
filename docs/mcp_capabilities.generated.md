# MCP Capabilities (generated)

> W6 산출물. existing 1-tool MCP server + 17 growth-action 확장.

## 현재 (Phase 21 까지)
- `src/mcp/server.py` — hand-rolled JSON-RPC 2.0 (~120줄)
- `_TOOLS` 1개: `hybrid.handle` (orchestrator entry point)
- transport: stdio (line-delimited JSON), `run_stdio()`

## 확장 (W6)

`src/mcp/server_extensions_generated.py` 가 17개 tool 추가. 모두 `HERMES_DISABLE_GROWTH_BLOCKS=true` short-circuit.

### Read endpoints (5)

| name | purpose |
|---|---|
| `hermes_status` | memory_count / skill_count / job_count / 마지막 cron |
| `hermes_skills_list` | `agents/{cat}/{name}/SKILL.md` 일람 |
| `hermes_memory_search` | SqliteMemory.search wrapper (LIKE substring) |
| `hermes_recent_experience` | 최근 N개 ExperienceRecord summary (PII redacted) |
| `hermes_growth_metrics` | 현재 vs baseline delta |

### Write / Trigger endpoints (12)

| name | loop | description |
|---|---|---|
| `hermes_memory_add` | 1 | 외부에서 memo 1개 추가 (`source='mcp_external'`) |
| `hermes_memory_flip` | 1 | should_store toggle |
| `hermes_skill_draft` | 2 | SKILL.draft.md 작성 |
| `hermes_skill_promote` | 2 | manual promote (audit log) |
| `hermes_skill_revert` | 2 | explicit revert |
| `hermes_user_profile_patch` | 3 | profile claim 갱신 (confirm/weaken/retire/add) |
| `hermes_soul_regenerate` | 3 | SOUL.generated.md 재생성 |
| `hermes_trigger_self_review` | 4 | W7 즉시 호출 |
| `hermes_trigger_dialectic` | 4 | W8 즉시 호출 |
| `hermes_delegation_record` | 5 | manual pattern 기록 |
| `hermes_delegation_suggest` | 5 | bias loader 노출 |
| `hermes_capture_baseline` | cross | growth_metrics 재 snapshot |

### 공통

- 모든 write 는 `dry_run=true` 지원 (계획만 반환).
- `MCP_ALLOWED_USER_IDS` env (CSV) 가 비어있지 않으면 fail-closed.
- 모든 호출은 `ExperienceRecord` 에 `handled_by="mcp_external"` 로 stamp (Phase 1.5 routing context 활용).

## Gap vs Nous Hermes "68 built-in tools"

미커버:
- web fetch / search 통합 (현재는 sub-agent 가 처리)
- file read/write generic (보안 정책 부재 → 명시 신중)
- terminal 실행 (Bash via subprocess) — 위험 대비 부재
- direct Slack / Email / Notion 통합 — Phase 2

본 W6 은 "growth action" 만 cover. 일반 file/web/terminal 은 P2.

## Smoke 검증

`pytest tests/mcp/test_growth_actions.py -x -v`:
- 17 추가 tool register 확인
- 12 write endpoint dry_run 동작 확인
- `hermes_memory_add` 비-dry-run 으로 `source='mcp_external'` row 생성
