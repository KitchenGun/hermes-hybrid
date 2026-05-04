# installer_ops

hermes-hybrid의 **Hermes Kanban worker 프로파일**. advisor_ops가 발행한 추천 task를
사용자가 accept(=todo/ready로 promote)하면 dispatcher가 이 프로파일을 spawn하고,
워커는 task body를 읽어 사용자가 직접 실행할 **install plan**(예상 변경 디프 +
명령어 후보)을 생성한다.

**Phase 1 minimum**: plan generation만. 실제 cron/skill/mcp 등록은 사용자 manual 실행.
Phase 1+에서 사용자 명시적 동의 시 자동 등록 로직 추가 검토.

## 트리거

| 종류 | 시점 |
|---|---|
| Kanban dispatcher | advisor_ops task가 todo/ready 상태가 되면 자동 spawn (gateway 활성 + dispatcher tick 60s) |

cron/on_demand 파일 없음 — 외부 trigger 미사용.

## 출력

1. **Kanban task comment** (1-2 KB 요약)
2. **상세 install plan**: `runtime/install_plan_<task_id>.md` (전체 디프 + 명령 + 검증 + 롤백)
3. **task status**: `done` (정상) 또는 `blocked` (정보 부족 / 위험 신호)

## 안전 규칙 (요약 — 전체는 [SOUL.md](SOUL.md))

- 다른 프로파일 file write 금지 (read-only 분석)
- 패키지 매니저 / 외부 명령 직접 실행 금지
- 자동 cron 등록 금지 (Phase 1)
- tenant 검증: `advisor` 외 task는 즉시 block

## 의존성

- `hermes kanban` CLI + active gateway (`systemctl --user start hermes-gateway`)
- advisor_ops가 발행한 task (tenant=`advisor`)
- 환경변수: `HERMES_KANBAN_TASK`, `HERMES_TENANT`, `HERMES_KANBAN_WORKSPACE` (dispatcher가 주입)

## 디렉토리

```
installer_ops/
├── config.yaml                          # Hermes 공식 + x-hermes-hybrid 확장
├── SOUL.md                              # 시스템 프롬프트 슬롯 #1
├── skills/install/auto_install/
│   ├── SKILL.md                         # plan 생성 절차
│   └── scripts/                         # (Phase 1+ 자동 등록 시 추가 예정)
├── memories/
│   ├── USER.md                          # 사용자 환경/선호
│   └── MEMORY.md                        # 운영 메모 (이력은 Kanban이 store)
└── runtime/                             # install_plan_<task_id>.md
```

## 등록 (cron 등록 불필요)

installer_ops는 cron 잡이 없으므로 `register_cron_jobs.py` 등록 대상이 아니다.
Kanban dispatcher가 자동으로 인식 (프로파일 디렉토리 존재만으로 assignee 후보).

검증 명령:
```bash
# Discovered 후보에 installer_ops가 보여야 정상
hermes kanban init  # idempotent
```

## Phase 1+ 후속 작업 (메모만)

- **자동 등록 로직**: plan 안의 명령을 사용자 동의 후 직접 실행하는 모드
  - 옵션 A: plan 검토 후 사용자가 task에 `accept` comment 추가 → 추가 spawn
  - 옵션 B: 처음부터 dry-run + 자동 실행 (위험 — 신중히)
- **충돌 사전 검사 강화**: 의존성 그래프 분석 (지금은 단순 키 매칭)
- **자동 등록 후 self-test 호출**: 등록한 잡이 실제 동작하는지 smoke test
