# MEMORY — advisor_ops

이 파일은 advisor 자체 운영 메모용이다. **추천 이력 추적은 Hermes Kanban이 담당** —
`~/.hermes/kanban.db`에 영구 보존, `tenant=advisor`로 필터.

## 추천 이력 조회

```bash
# 활성/대기 추천 (triage/todo/ready/blocked)
hermes kanban list --tenant advisor

# 거절(archived) 추천
hermes kanban list --tenant advisor --archived

# 특정 task 상세
hermes kanban show <task_id>
```

## 중복 추천 정책 (idempotency-key 기반)

`weekly_advisor_scan` / `advise_now`는 추천 발행 시 다음 키 형식을 사용:

```
advisor-<profile>-<kind>-<slug>-<ISO_week>
예: advisor-journal_ops-mcp-notion-2026-W19
```

- 같은 주에 동일 (profile, kind, slug) 추천이 **active 상태**로 존재하면 기존 task id 반환 (자동 dedup)
- 사용자가 거절(archived)한 추천은 dedup 대상이 아니므로, cron yaml Step 4의 `hermes kanban list --tenant advisor --archived` 조회로 사전 제외
- 사용자 accept(=todo로 promote)는 별도 워커가 처리 (Phase 1+에서 추가)

## advisor 자체 운영 메모 (필요 시 append)

(2026-05-04 Phase 1 마이그레이션 — MEMORY 기반 추천 이력 추적은 폐지, Kanban으로 이전.)
