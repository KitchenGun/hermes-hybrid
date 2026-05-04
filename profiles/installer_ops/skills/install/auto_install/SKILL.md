---
name: auto_install
description: Generate install plan markdown for an accepted advisor recommendation task. Phase 1 = plan-only (no actual installation).
version: 1.0.0
platforms: [linux, macos]
metadata:
  hermes:
    tags: [installer, kanban-worker, plan-generation]
    category: install
    config:
      - key: runtime_plan_dir
        description: "install plan 마크다운 출력 디렉토리"
        default: "${HERMES_PROFILE_HOME}/runtime"
    required_environment_variables:
      - name: HERMES_KANBAN_TASK
        prompt: "dispatcher가 주입하는 task id"
      - name: HERMES_TENANT
        prompt: "정상값: 'advisor' (advisor_ops가 발행한 task만 처리)"
---

# When to Use

- dispatcher가 installer_ops를 spawn했을 때만 (Kanban worker 컨텍스트)
- task의 tenant가 `advisor`이고 status가 `running`일 때
- task body에 advisor 표준 5필드(근거/출처/영향도/적용 대상/종류) 모두 존재 시

## 사용하지 말아야 할 때

- 사용자가 직접 chat으로 호출 (이 스킬은 Kanban worker 전용)
- task tenant가 `advisor` 외 — 즉시 `kanban_block(reason="unknown tenant")`
- task body의 5필드 중 하나라도 누락 — 즉시 `kanban_block(reason="missing field")`

# Procedure

## 1. 환경 검증

```
HERMES_KANBAN_TASK 존재 확인 → 없으면 즉시 exit 1 (worker 컨텍스트 아님)
HERMES_TENANT == "advisor" 확인 → 아니면 kanban_block(reason="unknown tenant")
```

## 2. Task 컨텍스트 로드

```python
task = kanban_show()
body = task["body"]  # 5필드 markdown
parents = task.get("parents", [])
prior_comments = task.get("comments", [])
```

`body` 파싱 (정규식 또는 구조화 prompt로 LLM에 위임):
- `근거: <1줄>`
- `출처: <URL>`
- `영향도: <낮음|중간|높음>`
- `적용 대상: <profile_id>`
- `종류: <skill|mcp|plugin|hook|model|custom_tool>`

5필드 중 누락 → `kanban_block(reason="missing field: <name>")`.

## 3. 적용 대상 프로파일 read-only 분석

```python
target_dir = Path(f"/home/kang/.hermes/profiles/{target_profile}")
```

- `config.yaml`, `cron/*.yaml`, `on_demand/*.yaml`, `skills/` 디렉토리 read
- 의도된 변경(kind에 따라 다름)이 기존 파일과 충돌하는지 검사
  - skill 추가: 같은 카테고리/이름 이미 있는지
  - mcp 추가: `mcp_servers` 키 충돌
  - cron 추가: 같은 schedule + name 중복
  - hook: 같은 trigger 중복

## 4. install plan 작성

SOUL.md "Output Format"의 형식으로 마크다운 작성. 필수 섹션:
- 표시명 + 적용 대상 + 종류 + 출처 + 영향도
- 예상 변경 (새 파일 / 기존 파일 수정 — 디프 또는 yaml 블록)
- 사용자 실행 명령 (수동 — 패키지 명령 + register 명령)
- 검증 방법
- 롤백 계획

## 5. 이중 기록

```python
# 짧은 요약은 task comment
kanban_comment(text=plan_summary[:1500])  # 첫 1500자

# 전체는 runtime 파일
plan_path = Path(f"/home/kang/.hermes/profiles/installer_ops/runtime/install_plan_{task_id}.md")
plan_path.parent.mkdir(parents=True, exist_ok=True)
plan_path.write_text(full_plan_md, encoding="utf-8")
```

## 6. 종료

```python
kanban_complete(
    summary=f"install plan ready ({kind}/{target_profile})",
    metadata={
        "plan_file": str(plan_path),
        "kind": kind,
        "target_profile": target_profile,
        "source_url": source,
    },
)
```

블록(중단) 사례:
- 정보 부족: `kanban_block(reason="missing source URL")`
- 충돌 신호: `kanban_block(reason="conflict with existing <type> in <profile>")`
- max_turns 도달: `kanban_block(reason="analysis exceeded budget")`

# Pitfalls

- **자동 등록 유혹**: plan 작성 후 `register_cron_jobs.py`를 직접 호출하고 싶어도
  Phase 1에서는 절대 금지. plan 안에 명령어로만 적는다 (사용자 manual 실행).
- **다른 프로파일 write**: 분석 시 read만. write는 `installer_ops/runtime/` 하위로만.
- **출처 미검증**: 출처 URL이 archived/dismissed task와 매칭되면 사용자가
  거절한 항목 → `kanban_block`. (advisor_ops Step 4 archived 사전 제외와 같은 정책.)
- **task body 형식 변경**: advisor_ops yaml의 body 5필드 형식이 바뀌면 이 스킬도 갱신 필요. body 구조 변경 시 advisor + installer 동기 PR.

# Verification

1. `hermes kanban show <task_id>`에 새 comment 한 건 + status `done` 확인
2. `profiles/installer_ops/runtime/install_plan_<task_id>.md` 파일 존재 + 5섹션 모두 채워짐
3. ledger에 `installer_ops` tag entry 1건

# References

- Hermes Kanban worker 표준 흐름: https://hermes-agent.nousresearch.com/docs/user-guide/features/kanban
- advisor_ops task body 형식: `profiles/advisor_ops/cron/weekly_advisor_scan.yaml` Step 7
- (Phase 1+) 자동 등록 검토 시 reference: `scripts/register_cron_jobs.py`
