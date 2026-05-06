# Agent Environment Variables (Phase 8)

> Phase 8 (2026-05-06) 에서 6 profile 이 폐기되며, profile-local `.env`
> 가 사라졌다. 이 문서는 폐기된 profile `.env` 의 변수가 17 sub-agent
> 중 어느 것이 어떤 용도로 사용하는지 정리한 inventory + 보존 가이드.
>
> **목적**: 사용자가 root `.env` (또는 `~/.hermes/.env`) 에 한 번 채워두면
> agent 호출 시 별도 사용자 작업 없이 자동 사용되도록 변수명/위치 표준화.

## 1. 보존 위치 / 우선순위

```
1. process env (가장 우선)
2. ./.env  (repo root)
3. ~/.hermes/.env  (사용자 글로벌)
4. _archive/profiles_envs/{profile}/.env.template  (참조용 inventory, 값 X)
```

`src/config.py` 의 `Settings` 가 `pydantic-settings` 로 root `.env` 를
load. 추가 위치는 `python-dotenv` 의 `load_dotenv("~/.hermes/.env")` 로
보강 (Phase 8 후속에서 추가 예정).

## 2. 변수 카탈로그 (agent 별)

### @researcher (research)

| 변수 | 용도 | 흡수 출처 | 필수 |
|---|---|---|---|
| `BRAVE_SEARCH_API_KEY` | Brave Search API key (web_search 백엔드) | kk_job + advisor_ops | 선택 (Tavily/Exa fallback) |
| `GOOGLE_OAUTH_CREDENTIALS` | cocal MCP OAuth client_secret JSON 경로 | calendar_ops | 캘린더 read 시 필수 |
| `GOOGLE_CALENDAR_MCP_TOKEN_PATH` | refresh token 저장 경로 | calendar_ops | 선택 (default ./google_token.json) |
| `GOOGLE_CALENDAR_ID` | 기본 캘린더 ID | calendar_ops | 선택 (default `primary`) |

### @analyst (research)

| 변수 | 용도 | 흡수 출처 | 필수 |
|---|---|---|---|
| (없음) | 로컬 파일 시스템 read-only 만 | advisor_ops job_inventory | — |

### @documenter (documentation)

| 변수 | 용도 | 흡수 출처 | 필수 |
|---|---|---|---|
| (없음) | 로컬 파일 시스템 + 사용자 데이터만 | kk_job document_writer | — |

> docx 출력 시 `python-docx` 패키지 + 한글 폰트, pdf 시 `pandoc` /
> `weasyprint` 가 OS 레벨에 설치돼 있어야 함. 환경변수 X.

### @devops (infrastructure)

| 변수 | 용도 | 흡수 출처 | 필수 |
|---|---|---|---|
| `DISCORD_BRIEFING_WEBHOOK_URL` | 일반 브리핑 / 알림 webhook | calendar_ops + advisor_ops + kk_job + installer_ops | 알림 사용 시 필수 |
| `DISCORD_MAIL_WEBHOOK_URL` | 메일 알림 분리 webhook | mail_ops | 선택 |
| `DISCORD_WEATHER_WEBHOOK_URL` | 날씨 브리핑 분리 webhook | calendar_ops | 선택 |
| `DISCORD_KK_JOB_WEBHOOK_URL` | 잡 매칭 분리 webhook | kk_job | 선택 |
| `DISCORD_DM_USER_ID` | DM 대상 user id (현재 미구현) | calendar_ops | 선택 |
| `GOOGLE_SHEETS_WEBHOOK_URL` | journal_ops Apps Script doPost (24-필드 JSON) | journal_ops | sheets_append 사용 시 필수 |
| `JOB_SHEETS_WEBHOOK_URL` | kk_job Apps Script doPost (raw + curated) | kk_job | 선택 |
| `JOURNAL_ALERT_WEBHOOK_URL` | sheets_append 실패 시 운영 경보 (best-effort) | journal_ops | 선택 |
| `GOOGLE_OAUTH_CREDENTIALS` | cocal MCP OAuth client_secret JSON 경로 (write 공용) | calendar_ops | 캘린더 write 시 필수 |
| `GOOGLE_CALENDAR_MCP_TOKEN_PATH` | refresh token 저장 경로 (write 공용) | calendar_ops | 선택 |
| `GOOGLE_CALENDAR_ID` | 기본 캘린더 ID | calendar_ops | 선택 (default `primary`) |
| `HERMES_KANBAN_TASK` | dispatcher 가 주입하는 task id (auto_install) | installer_ops | Kanban worker spawn 시 필수 |
| `HERMES_KANBAN_WORKSPACE` | task workspace path | installer_ops | 선택 |
| `HERMES_TENANT` | `advisor` 만 처리 (다른 값이면 즉시 block) | installer_ops | Kanban worker spawn 시 필수 |
| `NAVER_APP_PASSWORD` | Naver 메일 IMAP App Password | mail_ops | 메일 polling 시 필수 |

### 공통 (모든 agent / master)

| 변수 | 용도 |
|---|---|
| `TIMEZONE` | 표시 타임존 (default `Asia/Seoul`) |
| `LOCALE` | 표시 로케일 (default `ko_KR`) |
| `DISCORD_BOT_TOKEN` | Discord gateway bot token |
| `DISCORD_ALLOWED_USER_IDS` | 응답 허용 사용자 id whitelist |
| `JOURNAL_CHANNEL_ID` | (legacy) #일기 채널 id — Phase 8 후 forced_profile 폐기로 의미 약화 |
| `OPENAI_BASE_URL` | Ollama base url (run_bot.py 가 부팅마다 갱신) |
| `OPENAI_API_KEY` | Ollama 는 무시하지만 OpenAI SDK 가 빈 값에 죽으므로 placeholder 필요 |

## 3. 사용자 마이그레이션 가이드

Phase 8 적용 시 한 번만 수행하면 이후 agent 호출에서 별도 작업 X:

1. **확인**: `cat profiles/calendar_ops/.env profiles/journal_ops/.env`
2. **통합**: 위 두 `.env` 의 비어있지 않은 값들을 root `.env` 에 복사 (변수명
   동일 — 그대로 line 추가). 충돌 시 root 가 이미 갖고 있으면 root 우선.
3. **확인**: `grep -E "^(DISCORD_|GOOGLE_|BRAVE_|NAVER_|HERMES_)" .env`
   로 위 카탈로그 변수 모두 존재 확인.
4. **삭제**: P8.4 에서 `profiles/` 디렉터리가 git rm 됨. profile-local
   `.env` 는 .gitignore 라 git 영향 없지만 working dir 의 디렉터리 자체가
   사라지므로 OS-level 에서 함께 삭제. 통합 작업이 끝난 뒤 진행.

## 4. 보존된 inventory

각 폐기 profile 의 변수명만 모은 template 은 git tracked:

- `_archive/profiles_envs/advisor_ops/.env.template`
- `_archive/profiles_envs/calendar_ops/.env.template`
- `_archive/profiles_envs/installer_ops/.env.template`
- `_archive/profiles_envs/journal_ops/.env.template`
- `_archive/profiles_envs/kk_job/.env.template`
- `_archive/profiles_envs/mail_ops/.env.template`

값은 모두 제거됨 — secret 노출 0. 향후 agent 갱신 시 어떤 변수가 어느
profile 에서 왔는지 추적용.

## 5. 호환 메모

- **`OPENAI_API_KEY` / `OPENAI_BASE_URL`**: Phase 8 후 master 는 opencode CLI
  (`gpt-5.5`) 만 호출 — 직호출 폐기. 단 Ollama base_url 갱신 / OpenAI SDK
  의 빈 값 거부 회피용으로 root `.env` 에는 placeholder 형태로 잔존 가능.
- **`USE_HERMES_FOR_*` / `USE_NEW_JOB_FACTORY` / `TRUST_HERMES_REFLECTION`**:
  legacy router/job_factory 의존. P8.5 에서 Settings 필드 정리 후 제거 예정.
- **`CALENDAR_SKILL_*` / `JOURNAL_OPS_CLAUDE_MODEL`**: profile 의존 slash
  skill 의존. P8.6 에서 정리.
