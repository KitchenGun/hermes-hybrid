# MEMORY — installer_ops

이 파일은 installer_ops 자체 운영 메모용이다. 처리한 task 이력은 **Kanban이
영구 store** — `hermes kanban list --tenant advisor --status done`로 조회.

## 작업 이력 조회

```bash
# 완료한 install plan task
hermes kanban list --tenant advisor --status done

# 차단(블록)된 task
hermes kanban list --tenant advisor --status blocked

# 특정 task 상세 (comment + run history)
hermes kanban show <task_id>
```

## 운영 메모 (필요 시 append)

(2026-05-04 Phase 1 minimum 스캐폴드 — auto-install 로직 미구현, plan 생성만.)
