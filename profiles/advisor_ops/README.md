# advisor_ops

hermes-hybrid의 **메타 프로파일**. 다른 프로파일들의 잡 yaml과 skills 트리를
스캔해, 누락·개선 가능한 도구(Skills / MCP / Plugins / Hooks / Custom Tools / Model)를
리서치하고, 사용자가 Claude Code에 그대로 붙여넣을 수 있는 **자연어 추천 프롬프트**를
생성한다.

**자동 설치는 절대 하지 않는다.** 출력은 보고서뿐이며, 설치 판단·실행은 사용자 몫.

## 트리거

| 종류 | 시점 | 파일 |
|---|---|---|
| cron | 일요일 04:00 KST | `cron/weekly_advisor_scan.yaml` |
| on_demand | "어드바이저 분석" / "도구 점검" 등 | `on_demand/advise_now.yaml` |

## 출력

1. **Hermes Kanban tasks** (`tenant=advisor`, status=`triage`)
   - 추천 항목당 task 1개. 사용자가 dashboard / `hermes kanban list --tenant advisor`로 검토 후 promote / archive.
   - idempotency-key로 중복 자동 방지 (`advisor-<profile>-<kind>-<slug>-<ISO_week>`)
2. **Discord 요약 임베드** (상위 3건 — task id 포함)
   - webhook: `DISCORD_BRIEFING_WEBHOOK_URL`
3. **상세 보고서 마크다운**
   - `runtime/install_prompt_YYYY-MM-DD.md`
   - 발행된 task id 목록 + 추천 디테일

## 안전 규칙 (요약 — 전체는 [SOUL.md](SOUL.md))

- 자동 설치/패키지 추가 금지
- 다른 프로파일 파일 수정 금지 (read-only)
- 모든 추천에 출처 URL + 영향도 명시 필수

## 의존성

- `hermes kanban` CLI (Kanban DB가 `~/.hermes/kanban.db`에 초기화되어 있어야 함 — `hermes kanban init`은 idempotent)
- `kk_job/skills/messaging/discord_notify/scripts/post_webhook.py` — Discord 송신용 (advisor_ops가 절대경로로 호출)
- 환경변수: `DISCORD_BRIEFING_WEBHOOK_URL`, `BRAVE_SEARCH_API_KEY`

## 등록

cron 등록은 다른 프로파일과 동일하게 [`scripts/register_cron_jobs.py`](../../scripts/register_cron_jobs.py) 사용:

```bash
python3 /mnt/e/hermes-hybrid/scripts/register_cron_jobs.py --profile advisor_ops
```

`scripts/register_cron_jobs.py`의 `CRON_PROFILES` 목록에 `"advisor_ops"`를 추가해야
`--profile all` 모드에서도 함께 등록된다.

## 디렉토리

```
advisor_ops/
├── config.yaml                          # Hermes 공식 + x-hermes-hybrid 확장
├── SOUL.md                              # 시스템 프롬프트 슬롯 #1
├── intent_schema.json                   # AdvisorIntent JSON Schema
├── cron/weekly_advisor_scan.yaml
├── on_demand/advise_now.yaml
├── skills/analysis/job_inventory/
│   ├── SKILL.md
│   └── scripts/scan_jobs.py             # 풀스캔 → JSON 인벤토리
├── memories/
│   ├── USER.md                          # 사용자 환경/선호
│   └── MEMORY.md                        # advisor 운영 메모 (추천 이력은 Kanban이 store)
└── runtime/                             # install_prompt_*.md, 임시 산출물
```
