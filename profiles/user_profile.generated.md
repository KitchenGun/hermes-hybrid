# User Profile (generated)

> P0a Step 2 산출물 — Hermes Hybrid Growth-Agent Migration & Closed-Loop Wiring.
> 자동 생성. 사람 review 후 `profiles/user_profile.md` 또는 `data/memory/USER.md` 로 승격 검토.
> 출처는 모두 `path:line` 또는 `path` 단일 형태로 기록. 미확정 항목은 `NEEDS_REVIEW`.

## 사용자 목표

- **개인 자동화 + 성장형 에이전트 운영**: hermes-hybrid 봇 (Discord/Telegram + cron)을 통한 일일 브리핑, 메일/날씨/캘린더 알림, 일기 자동 적재 (`README.md`, `scripts/calendar_briefing_job.py`, `scripts/mail_alert_job.py`, `scripts/weather_alert.py`).
- **Nous Hermes Agent 모델 정렬**: 5-loop 닫힌 학습 시스템(memory/skill/user-model/cron-reflection/delegation)을 자기 코드베이스에 적용 (`docs/MASTER_ARCHITECTURE.md`, plan `refactored-inventing-pony.md`).
- **게임 엔진 개발자 일상 보조**: Unreal/Unity 작업 중 호출 가능한 보조 에이전트 (`C:\Users\kang9\.claude\projects\E--hermes-hybrid\memory\user_role.md`).

## 선호하는 에이전트 구조

- **Memory**: SQLite + LIKE substring search, Top-K=3 inject default ON (`src/memory/sqlite.py:38`, `src/config.py:182 memory_inject_enabled=True`).
- **Skills**: 6-카테고리 17 sub-agent SKILL.md 체계 (`src/agents/__init__.py:42 _CATEGORIES`). 자동 promotion via `SkillPromoter` (`src/jobs/skill_promoter.py:70`).
- **Tools**: MCP server (hand-rolled JSON-RPC 2.0) — `src/mcp/server.py:93`. 1-tool 부터 시작, growth-action 17개 확장 예정 (W6).
- **Gateway**: Discord (full bidi, allowlist fail-closed) + Telegram (long-poll, text-only). No Slack (`src/gateway/discord_bot.py`, `src/gateway/telegram_bot.py`).
- **Cron**: cross-platform timer auto-registration (Windows schtasks / Linux systemd-user / macOS launchctl) — `src/cli/timer_handlers/`.
- **Provider Routing**: Phase 11 단일 Claude CLI lane, Max OAuth $0 (`src/orchestrator/hermes_master.py:25`, commit cf98c65).
- **Delegation**: Phase 9 `@mention` 명시 dispatch + Phase 10 parallel agents + Phase 12 sequential pipeline (`src/state/task_state.py:154 agent_handles`).

## 선호하는 응답 스타일

- **한국어 우선** — code/identifiers 외 모든 explanation. (CLAUDE.md "Always respond in korean")
- **짧고 직접적** — "End-of-turn summary: one or two sentences" (CLAUDE.md `Tone and style`).
- **`path:line` 형식 인용** — markdown 링크보다 `path:line` 패턴이 일반적 (CLAUDE.md `When referencing specific functions or pieces of code include the pattern file_path:line_number`).
- **불필요한 docstring/주석 회피** — `Default to writing no comments` (CLAUDE.md).
- **"NEEDS_REVIEW" 표기를 환영** — 추측보다 명시적 미확정 표시 (plan Constraint #3).

## 자주 하는 작업

- **마이그레이션 / wiring 작업**: 큰 단위 plan 작성 후 단계별 marker block 삽입. plan 파일 자체를 99 KB 단위로 작성 (`refactored-inventing-pony.md`).
- **timer 등록 / 스크립트 추가**: `scripts/install_*_timer.sh` 패턴, `_TASKS` tuple 확장 (`src/cli/timer_handlers/windows.py:19`).
- **discord 일기 적재 / 24-필드 sheets**: `src/skills/journal/`, `src/skills/storage/sheets_append.py`, Phase 22.
- **메일 / 날씨 / 캘린더 알림 cron**: Phase 22 (commit 7c0dc40).
- **A/B 측정 + 정책 자동 갱신 + 피드백 루프**: Phase 17/18/20/21 (`src/jobs/ab_report.py`, `src/jobs/skill_promoter.py`, `src/gateway/feedback_router.py`).

## 개발 환경

- **OS**: Windows 11 Home 10.0.26200 (env current).
- **Shell**: PowerShell 5.1 (env current).
- **Python venv**: 자체 (현재 실행 인터프리터, `pyproject.toml`).
- **Claude Code**: Max OAuth, single-lane Claude CLI master (Phase 11, commit cf98c65).
- **WSL**: 사용 가능, Ollama 로컬 모델 검증용 (`scripts/check_wsl_ollama.sh`).
- **Git worktree**: `.claude/worktrees/<name>` 패턴 — 현재 `quirky-feistel-7f8dd3`.

## 주요 프로젝트

- **hermes-hybrid** (this repo) — Phase 22까지 진행. 
- **Unity / Unreal Engine 게임 개발** — 사용자 본업 (`C:\Users\kang9\.claude\projects\E--hermes-hybrid\memory\user_role.md`). GPU 휴리스틱 사용 금지 — GPU는 에디터·빌드·플레이테스트에 활용 중이라는 메모.
- **이전 시도들**: `Hermes-Agent-Setting`, `hermes-agent`, `hybrid-llm-orchestrator`, `harness-Auto-Create` (sibling 디렉토리 — `git worktree list` 외부에서 관찰).

## 사용 중인 외부 서비스 카탈로그

> 토큰 본문은 절대 인용하지 않음. 변수명·역할·통합 위치만 기록. 실제 값은 `.env` / `secrets/` 에 보관.

| 서비스 | env var | 통합 위치 | 용도 |
|---|---|---|---|
| Discord Bot | `DISCORD_BOT_TOKEN`, `DISCORD_ALLOWED_USER_IDS` | `src/gateway/discord_bot.py` | full bidi gateway, allowlist fail-closed |
| Discord Webhook (브리핑) | `DISCORD_BRIEFING_WEBHOOK_URL` | `scripts/calendar_briefing_job.py`, `scripts/weather_alert.py`, `scripts/mail_alert_job.py` | cron 산출물 push |
| Telegram Bot | `TELEGRAM_BOT_TOKEN`, `TELEGRAM_ALLOWED_USER_IDS` | `src/gateway/telegram_bot.py` | long-polling, text-only |
| Naver Mail (IMAP) | `NAVER_MAIL_USER`, `NAVER_MAIL_APP_PASSWORD` | `src/skills/mail/naver.py`, `scripts/mail_alert_job.py` | IMAP poll |
| Gmail (OAuth) | `GMAIL_OAUTH_CREDENTIALS`, `GMAIL_OAUTH_TOKEN_PATH` | `scripts/mail_alert_job.py` | OAuth poll (선택) |
| Google Calendar MCP | `GOOGLE_OAUTH_CREDENTIALS`, `GOOGLE_CALENDAR_MCP_TOKEN_PATH`, `GOOGLE_CALENDAR_ID` | `agents/research/researcher/SKILL.md`, `scripts/calendar_briefing_job.py` | 일정 조회 / 브리핑 |
| Google Sheets | `GOOGLE_SERVICE_ACCOUNT_JSON`, `JOURNAL_SHEET_ID` | `src/skills/storage/sheets_append.py` | 24-필드 일기 적재 |
| Brave Search | `BRAVE_SEARCH_API_KEY` | `agents/research/researcher/SKILL.md` | web search |
| OpenWeather / KMA | `KMA_API_KEY` (선택) | `scripts/weather_alert.py` | 날씨 |
| OpenAI | `OPENAI_API_KEY` | `src/llm/adapters/openai.py` (선택, 미사용 lane) | bench only |
| Ollama (local) | `OLLAMA_HOST` | `src/llm/adapters/ollama.py` | bench only |
| Claude Code (Max OAuth) | (CLI 자체 인증) | `src/claude_adapter/adapter.py` | master LLM lane |
| GitHub | `GITHUB_TOKEN` (선택) | `gh` CLI via `src/jobs/skill_promoter.py:600` | auto-PR |

## 자동화 후보

→ `jobs/generated/job_candidates.generated.yaml` 참조.

## 코딩 / 리서치 / 문서화 / 면접 / 게임 개발 패턴

- **코딩**: 작업 단위가 큼. plan 99 KB → marker block 단위 삽입 → migration script 일괄 실행. 단발 patch 보다 batch.
- **리서치**: 외부 docs를 인용하면서 본인 코드 line:number와 cross-link. 추측 금지 (CLAUDE.md `verify before recommending`).
- **문서화**: README + ARCHITECTURE + docs/MASTER_ARCHITECTURE 3-tier. Phase 마다 README §X 추가.
- **면접 준비**: NEEDS_REVIEW (transcript에 면접 직접 작업 명시 없음).
- **게임 개발**: Unity/Unreal 본업 — 봇은 보조 도구 역할. game-job 크롤러도 보유 (`agents/research/researcher/SKILL.md absorbed_from job_crawler`).

## 세션 transcript 기반 사용자 프롬프트 패턴 분석

> 직접 인용 없이 패턴만 요약. 시크릿 회피.

- **Top intents (관찰)**: (1) "이 plan을 따라 실행해라" 형 매우 상세한 다단계 마이그레이션 지시, (2) "검증해라" 형 read-only 평가 요청, (3) "1번/2번 진행" 형 옵션 선택, (4) cron / timer 추가 / 디버그.
- **선호 phrasing**: 한국어, 명령형 ("…해라"), 단계별 번호 매기기, "중요한 규칙:" 같은 가드 명시.
- **request shape**: 헤더("목표:", "검증 대상:", "검증 기준:") + 번호 리스트 + 최종 보고서 양식 명시.

## 장기 기억 후보

→ `memory/memory_candidates.generated.yaml` 참조.

## 저장 금지 민감 정보

> path 만, body 절대 금지. `should_store: false` 카테고리.

- `.env`, `.env.bak`, `.env.bak.20260501` — 모든 토큰 본문.
- `secrets/` 디렉토리 (있을 경우) — OAuth refresh token, JSON keys.
- `data/state.db`, `data/kanban.db` — 사용자 task / 메시지 본문.
- `data/memory/memos.db` — 메모 본문 (개인 노트).
- `logs/experience/*.jsonl` — `input_text_hash` 만 있고 본문은 없음 (`src/core/experience_logger.py:139`). 그러나 `tool_calls` 등에 우발적으로 토큰이 섞일 가능성 → grep 검사 필수.

## NEEDS_REVIEW

- `interview_preparation` 패턴 — transcript에 명시 부족.
- `career_tutor` 프로파일 — 사용자 의도 불명확.
- `unity_game_dev` 프로파일 — 게임 개발 컨텍스트는 메모에 있으나 봇 사용 사례 명시 부족.
- `prompt_engineer` 프로파일 — 직접 대응되는 sub-agent 없음.
