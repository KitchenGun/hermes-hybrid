# hermes-hybrid

**NousResearch Hermes 위에 얹은 "성장하는 개인 에이전트".**
Discord 가 입구, 모든 작업이 자기 행동을 로그로 쌓고, 주말마다 자기반성하고, 반복 패턴을 skill 후보로 올리는 자동화 어시스턴트.

단순히 "AI에게 물어보는" 봇이 아니라 — 작업 환경을 기억하고, 반복을 절차화하고, Discord 명령으로 계속 일하는 **persistent + curating** 에이전트로 설계됐다.

---

## 1. 핵심 기능 (6 axes)

사용자 vision 6개 축 기준 현재 동작 상태:

| 축 | 상태 | 구현 |
|---|---|---|
| **Memory** — 세션을 넘는 개인화 | ✅ | `SqliteMemory` (`src/memory/sqlite.py`) + `/memo` 슬래시 명령 + **memory inject** 옵트인 (P0-C, `HERMES_MEMORY_INJECT_ENABLED`). LIKE substring 검색으로 한국어 호환. |
| **Skills** — 반복 작업 절차화 | 🟡 부분 | profile-level `SKILL.md` 10개 (`profiles/*/skills/**`) + slash skill 4개 (`HybridMemoSkill`/`HybridStatusSkill`/`HybridBudgetSkill`/`CalendarSkill`). `SkillLibrary` 가 인덱싱 (`src/core/skill_library.py`). **자동 promotion 미구현** — Curator 의 다음 단계. |
| **Tools** — web/terminal/file/MCP | ✅ via Hermes | hermes 가 모든 tool 실행. profile 별 `disabled_toolsets` 로 제어. `calendar_ops` 는 google_calendar MCP, `kk_job` 은 web_search/job_crawler 등. |
| **Gateway** — Discord/Telegram/Slack | 🟡 부분 | Discord ✅ (`src/gateway/discord_bot.py`), MCP server PoC ✅ (`src/mcp/server.py`, Phase 3). **Telegram/Slack 미구현**. |
| **Cron Automation** — 정기 작업 | ✅ | hermes profile cron 10잡 + reflection timer (일요일 22:00 KST) + curator timer (일요일 23:00 KST). 사용자 일회 install 후 자동. |
| **Delegation** — sub-agent 병렬 | ❌ | hermes 위임 (heavy path 의 plan/act/reflect) 외 직접 구현 0. 미래 작업. |

---

## 2. 성장 루프 (P0/P1 산출)

```
Discord 메시지
    ↓
Orchestrator (forced_profile / slash skill / JobFactory v2 / router)
    ↓
LLM 호출 (Ollama L2/L3 → Claude CLI C1/C2 fallback)
    ↓
Critic.evaluate (Validator wrap + self_score 0~1 stamp)
    ↓
Response → Discord
    ↓
ExperienceLogger.append → logs/experience/{date}.jsonl  (input/response 는 sha16+length 만)
    ↓
[일요일 22:00 KST] ReflectionJob → logs/reflection/{ISO-WEEK}.md
[일요일 23:00 KST] CuratorJob   → logs/curator/{date}.md + handled_by_stats.json
    ↓
사람 검토 → MEMORY.md 후보 / Skill promotion 후보
```

| 컴포넌트 | 위치 | 책임 |
|---|---|---|
| **ExperienceLogger** | [src/core/experience_logger.py](src/core/experience_logger.py) | 모든 task 종료 시 JSONL 한 줄. privacy: 본문 저장 X, sha16 + length 만 |
| **Critic** | [src/core/critic.py](src/core/critic.py) | Validator wrap. retry/tier 정책은 Validator 가 결정, Critic 은 self_score 만 stamp |
| **ReflectionJob** | [src/jobs/reflection_job.py](src/jobs/reflection_job.py) | 주간 통계 — 성공률, profile/handler/tier 분포, p50/p95 latency, top failure buckets, top tools |
| **CuratorJob** | [src/jobs/curator_job.py](src/jobs/curator_job.py) | handler/tool 별 success/failure rate. failure_rate ≥ 30% & runs ≥ 5 인 핸들러 자동 flag |
| **SkillLibrary** | [src/core/skill_library.py](src/core/skill_library.py) | `profiles/*/skills/**/SKILL.md` 인덱스 → `skills/registry.yaml` |
| **Memory search** | [src/memory/sqlite.py](src/memory/sqlite.py) | LIKE-based search + 토큰 split OR 매칭. inject 는 `Orchestrator.handle()` 시작에서 옵트인 |

테스트: `pytest tests/test_{experience_logger,critic,reflection_job,curator_job,skill_library,memory_search}.py` — 56개 자체 테스트 + 전체 583 pass.

---

## 3. 아키텍처 흐름

```
Discord Bot
    │
    ├─ #일기 channel?  ──► forced_profile=journal_ops ──► hermes -p journal_ops chat
    │                                                       └─ log_activity (24-field JSON → Google Sheets)
    │
    ├─ /memo /hybrid-*  ──► slash skill (HybridMemoSkill etc.) ──► MemoryBackend
    │
    └─ 자유 텍스트
         │
         ├─ JobFactory v2 (default-on, 2026-05-06~)
         │    ├─ classifier (keyword → llm 4b → fallback) → JobType (10종)
         │    ├─ ScoreMatrix → arm 선택 (qwen / claude_cli haiku/sonnet/opus)
         │    └─ dispatcher → adapter 호출 → Critic 통과
         │
         └─ legacy router (v1 OFF default, HERMES_DISABLE_V1_JOBFACTORY 로 kill switch)

cron / watcher (hermes 가 직접 schedule)
    │
    └─ profiles/{profile}/cron/{category}/{job}.yaml prompt 직접 실행

ReflectionJob / CuratorJob (systemd-user timer, 일요일 22/23 KST)
    │
    └─ logs/experience/*.jsonl read → markdown + JSON 보고서
```

### Tier 매핑

| Tier | 모델 | 용도 | 비용 | 자동 |
|---|---|---|---|---|
| **L2** | `qwen2.5:14b-instruct` (Ollama) | 일반 응답 | $0 | ✓ |
| **L3** | `qwen2.5-coder:32b-instruct` (Ollama) | 코드/분석 | $0 | ✓ |
| **C1** | Claude CLI Haiku (Max OAuth) | 복잡 추론 — Validator 자동 escalation | $0 marginal | ✓ |
| **C2** | Claude CLI Sonnet/Opus (Max OAuth) | heavy 작업 — `!heavy` 명시 opt-in | $0 marginal | ✗ |

> **2026-05-04**: OpenAI gpt-4o 제거. C1/C2 모두 Claude CLI 경유 (Max OAuth = $0 marginal).
> **2026-05-06**: `system_mode` active/quiet 2-mode 폐기. JobFactory v2 default-on.

---

## 4. 프로필 6개

각 프로필은 자체 페르소나 (`SOUL.md`) + 모델 정책 (`config.yaml`) + 잡들 (`cron/`, `on_demand/`, `watchers/`) + 스킬들 (`skills/*/SKILL.md`) + 메모리 (`memories/MEMORY.md`).

| profile_id | 목적 | model | tier | 잡 수 | discord 출력 |
|---|---|---|---|---:|---|
| **advisor_ops** | 도구 어드바이저 (보고 전용) — 다른 프로필 스캔 → 추천 | `qwen2.5-coder:32b` | L3→C1 | cron 1 + on_demand 1 | `DISCORD_BRIEFING_WEBHOOK_URL` |
| **calendar_ops** | Google Calendar CRUD + 브리핑 + 충돌 감지 | `qwen2.5:14b` | L2→C1 | cron 4 + on_demand 5 + watcher 2 | briefing + weather webhook |
| **installer_ops** | Kanban worker (Phase 1 보류) | `qwen2.5-coder:32b` | L3→C1 | 0 (미구현) | kanban virtual channel |
| **journal_ops** | #일기 채널 → 24-필드 → Google Sheets | `qwen3:8b` | L2→C1 | on_demand 1 (forced_profile) | #일기 channel |
| **kk_job** | 게임 프로그래머 구인 리서칭 + 이력서/자소서 | `qwen2.5-coder:32b` | C1→C1 | cron 3 + on_demand 4 + watcher 1 | `DISCORD_KK_JOB_WEBHOOK_URL` |
| **mail_ops** | Gmail/Naver 받은편지함 알림 | `gpt-4o-mini` (OpenAI 직호출 — 정리 후보) | L2 only | watcher 1 | `DISCORD_MAIL_WEBHOOK_URL` |

자세한 분석은 `docs/PROFILE_INVENTORY.md` (Phase 1.5 산출물).

---

## 5. 요구사항

- **OS**: Windows 10/11 + WSL2 Ubuntu
- **Python**: 3.11+
- **Ollama**: Windows native, 모델 3종 (디스크 ~33GB, 32B 모델은 VRAM 20GB+ 권장)
- **Hermes CLI**: WSL 안 (`~/.local/bin/hermes`)
- **Claude Code CLI**: WSL 안 (`~/.local/bin/claude`), Max 구독 OAuth 인증

```bash
ollama pull qwen2.5:7b-instruct          # router 정제
ollama pull qwen2.5:14b-instruct         # L2 work
ollama pull qwen2.5-coder:32b-instruct   # L3 worker (코드/분석)
```

---

## 6. 설치

```powershell
# 1. Clone + 가상환경
cd E:\
git clone <your-repo-url> hermes-hybrid
cd hermes-hybrid
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -e ".[dev,ollama]"

# 2. .env 작성
copy .env.example .env
notepad .env       # 최소 4 항목 (§7)

# 3. WSL CLI 확인
wsl -d Ubuntu -- bash -lc "hermes --version && claude --version"

# 4. 프리플라이트
python -m src.preflight
```

---

## 7. 환경변수 (핵심)

### 7.1 필수 (4개)
```env
DISCORD_BOT_TOKEN=<your-bot-token>
DISCORD_ALLOWED_USER_IDS=<your-discord-user-id>     # 허용 user CSV (R12 fail-closed)
OLLAMA_ENABLED=true
HERMES_HOME=/home/kang/.hermes                       # WSL 경로
```

### 7.2 성장 루프 (P0/P1 산출, default-on)
```env
HERMES_EXPERIENCE_LOG_ENABLED=true                   # 모든 task → JSONL
HERMES_EXPERIENCE_LOG_ROOT=./logs/experience
HERMES_USE_NEW_JOB_FACTORY=true                      # v2 bandit dispatcher (2026-05-06 default-on)
```

### 7.3 Memory inject (P0-C, default-off — privacy review 후 켜기)
```env
HERMES_MEMORY_INJECT_ENABLED=false                   # /memo 내용을 prompt 에 자동 주입
HERMES_MEMORY_INJECT_TOP_K=3
```

### 7.4 Tier / 예산
```env
CLAUDE_CODE_MODEL=sonnet                             # C2 heavy path 모델
CLAUDE_CALL_BUDGET_SESSION=1                         # !heavy 세션당 1회
CLOUD_TOKEN_BUDGET_DAILY=100000
PER_USER_IN_FLIGHT_MAX=1
```

### 7.5 Deprecation kill switches
```env
HERMES_DISABLE_V1_JOBFACTORY=false                   # v1 keyword matcher 강제 끄기
```

전체 변수는 [src/config.py](src/config.py) 의 `Settings` 클래스 참조.

---

## 8. 실행

```powershell
# 원클릭 (Windows)
.\run_all.bat

# 단계별 디버그
ollama serve                              # 별도 터미널
.\.venv\Scripts\Activate.ps1
python scripts\run_bot.py
```

---

## 9. 성장 루프 활성화 (사용자 일회 작업)

WSL 안에서:

```bash
# 주간 통계 자동화 (일요일 22:00 KST)
bash /mnt/e/hermes-hybrid/scripts/install_reflection_timer.sh

# 핸들러/툴 stat 자동화 (일요일 23:00 KST)
bash /mnt/e/hermes-hybrid/scripts/install_curator_timer.sh

# 즉시 실행 (현재까지 누적된 데이터 보고서)
python /mnt/e/hermes-hybrid/scripts/reflection_job.py
python /mnt/e/hermes-hybrid/scripts/curator_job.py

# 결과
ls /mnt/e/hermes-hybrid/logs/reflection/    # YYYY-WW.md
ls /mnt/e/hermes-hybrid/logs/curator/       # YYYY-MM-DD.md
cat /mnt/e/hermes-hybrid/logs/curator/handled_by_stats.json
```

### 주간 검토 워크플로

1. **일요일 밤** — reflection.md 자동 생성 (성공률, top failure buckets, top tools)
2. **월요일 아침** — curator.md 의 "review or consider deactivating" flag 행동 결정
3. **격주** — MEMORY.md 후보를 사용자가 수동 merge (현재는 자동 갱신 안 함, 검토 필수)
4. **월간** — Skill promotion 후보 review (Phase 3 작업 후 자동화 예정)

---

## 10. Discord 사용

```
@bot 안녕                              → L2 (qwen2.5:14b) 자동 응답
@bot 파이썬으로 피보나치 짜줘            → JobFactory v2 → code_generation → arm 선택
@bot /memo save 내일 회의 9시           → SqliteMemory 저장
@bot /memo list                        → 최근 메모 20개
@bot /hybrid-status                    → settings 요약 (ollama on/off, 등록 skill 등)
@bot /hybrid-budget                    → 일일 토큰 예산
@bot 캘린더에 내일 미팅 추가             → CalendarSkill (HITL [확인]/[취소] 후 실행)
@bot !heavy 복잡한 아키텍처 분석        → C2 Claude CLI Opus, 명시 opt-in (예산 카운터)

# #일기 channel (forced_profile=journal_ops)
오늘 운동 30분 했어                     → 24-필드 JSON 추출 → Google Sheets append
```

---

## 11. 디렉터리

```
hermes-hybrid/
├─ scripts/                      # 진입점 + 운영 + cron timer
│  ├─ run_bot.py                 # Discord bot main
│  ├─ register_cron_jobs.py      # hermes profile cron 등록
│  ├─ validate_all_crons.py      # 모든 cron dry-run
│  ├─ install_reflection_timer.sh   # 일요일 22:00 KST
│  ├─ install_curator_timer.sh      # 일요일 23:00 KST
│  ├─ reflection_job.py / curator_job.py  # CLI 진입점
│  └─ build_skill_registry.py    # SKILL.md → registry.yaml
│
├─ src/
│  ├─ gateway/                   # Discord bot (forced_profile, allowlist)
│  ├─ orchestrator/              # 메인 흐름 + Critic 통합
│  ├─ router/                    # local/worker/cloud 결정
│  ├─ job_factory/               # v2 bandit dispatcher (default-on)
│  ├─ claude_adapter/            # Claude CLI subprocess
│  ├─ hermes_adapter/            # Hermes CLI subprocess
│  ├─ llm/                       # Ollama HTTP client
│  ├─ validator/                 # retry/tier 정책 (R10)
│  ├─ memory/                    # SqliteMemory + search (P0-C)
│  ├─ state/                     # TaskState + Repository
│  ├─ skills/                    # 슬래시 명령 (HybridMemoSkill 등)
│  ├─ mcp/                       # MCP server PoC (Phase 3)
│  │
│  ├─ core/                      # 🌱 성장 루프 핵심 (P0/P1)
│  │   ├─ experience_logger.py
│  │   ├─ critic.py
│  │   └─ skill_library.py
│  │
│  └─ jobs/                      # 🌱 cron 가능 자기개선 잡
│      ├─ base.py
│      ├─ reflection_job.py
│      └─ curator_job.py
│
├─ profiles/                     # 6 페르소나 — 자세한 inventory: docs/PROFILE_INVENTORY.md
│  ├─ advisor_ops/
│  ├─ calendar_ops/
│  ├─ installer_ops/             # Kanban Phase 1 보류
│  ├─ journal_ops/
│  ├─ kk_job/
│  └─ mail_ops/
│
├─ tests/                        # pytest, 583 pass
├─ logs/
│  ├─ experience/                # task append-only JSONL (P0-2)
│  ├─ reflection/                # 주간 markdown
│  └─ curator/                   # 주간 markdown + JSON
├─ data/                         # state.db (SQLite, gitignore)
└─ docs/
   ├─ architecture.md
   ├─ PROFILE_INVENTORY.md       # 6개 프로필 상세 (Phase 1.5)
   ├─ JOB_INVENTORY.md           # 26 jobs 상세 (Phase 1.5)
   └─ PROFILE_JOB_MAP.md         # 관계도 + ExperienceRecord 표준 필드 (Phase 1.5)
```

---

## 12. 테스트

```powershell
pytest -q                                 # 전체 (583 pass / 5 skip)
pytest tests/test_experience_logger.py    # 11 tests, JSONL contract
pytest tests/test_critic.py               # 11 tests, self_score lookup
pytest tests/test_reflection_job.py       # 9 tests, weekly stats
pytest tests/test_curator_job.py          # 9 tests, handler stat
pytest tests/test_skill_library.py        # 8 tests, SKILL.md scanner
pytest tests/test_memory_search.py        # 13 tests, search + inject
```

---

## 13. 설계 불변식

1. **Orchestrator 는 tool 을 직접 실행하지 않는다** — 실행은 Hermes 가 (R0).
2. **Router 는 `{route, confidence, reason, requires_planning}` 만 반환** (R11).
3. **LLM 은 실행 엔진**, JobFactory v2 가 어느 arm 을 쓸지 결정.
4. **C2 (Claude Sonnet/Opus) 는 사용자 명시 opt-in** (`!heavy`). 자동 escalation 은 C1 까지 (R2/R9).
5. **Validator 와 Critic 의 책임 분리** — Validator 가 retry/tier 결정, Critic 은 self_score 만 stamp. 점수가 retry 정책에 영향 X.
6. **모든 task 는 ExperienceLog 에 append-only** — 본문 X, sha16 + length 만 (privacy).
7. **Rule Layer 는 확정된 패턴만 응답** — LLM fallback 금지.
8. **Memory inject 는 default off** — 사용자 privacy 검토 후 명시 활성화.
9. **자동 행동 변경은 사람 검토 후** — Curator/Reflection 은 보고서만, MEMORY.md/SKILL 자동 수정 0.

자세한 설계 근거는 [docs/architecture.md](docs/architecture.md) + [memory/project_mode_system_deprecation.md](memory/project_mode_system_deprecation.md) 참조.

---

## 14. 로드맵

- [x] **Phase 1** — direct LLM client + shadow Hermes
- [x] **2026-05-04 정리** — OpenAI legacy 제거, dev/playtest/gaming 3-mode 폐기 (`runtime_mode`/`game_watcher`/`hotkey_daemon` 삭제)
- [x] **2026-05-06 정리** — `system_mode` active/quiet 2-mode 폐기, JobFactory v2 default-on
- [x] **P0 성장 루프 첫 벽돌** — ExperienceLogger / Critic / ReflectionJob / CuratorJob / SkillLibrary
- [x] **P0-C** — Memory search + opt-in inject
- [x] **P0-D** — Curator timer + Reflection timer
- [ ] **Phase 1.5** — Profile/Job inventory 문서화 + ExperienceRecord 스키마 확장 (`job_id`/`trigger_type`/`v2_job_type`/`skill_ids`/`model_provider`)
- [ ] **Phase 2** — cron/watcher 의 ExperienceLog 통합 (현재 Discord 경로만 흘러감 — 16잡 누락)
- [ ] **Phase 3** — Skill 자동 promotion (Curator → `skills/promoted/{candidate}/SKILL.md` 후보 markdown)
- [ ] **Phase 4** — Memory inject 의 임베딩 기반 (현재 LIKE substring; bge-m3 ollama embed 도입)
- [ ] **Phase 5** — Telegram/Slack gateway, sub-agent delegation
- [ ] **Phase 6** — installer_ops Kanban Phase 1 (보류 해제)

---

## 15. 문제 해결

### "hermes: command not found" (WSL)
```bash
wsl -d Ubuntu -- bash -lc "which hermes"
# 경로 다르면 .env 의 HERMES_CLI_PATH 수정
```

### Ollama 응답 없음
```powershell
ollama list
curl http://localhost:11434/api/tags
```

### Discord bot 이 메시지에 반응 안 함
- `DISCORD_ALLOWED_USER_IDS` 에 본인 ID 포함 확인
- Discord Developer Portal 에서 **MESSAGE CONTENT INTENT** 활성

### `!heavy` 승인 안 됨
- WSL 안 `claude --version` 직접 실행 확인
- Max OAuth 시간당 한도 도달 시 1시간 대기

### ExperienceLog 가 안 쌓인다
- `HERMES_EXPERIENCE_LOG_ENABLED=true` 확인
- `logs/experience/` 디렉터리 권한 (Windows host filesystem)
- 봇 재시작 후 한 메시지 보내고 `wc -l logs/experience/{date}.jsonl`

### `reflection.md` 가 안 만들어진다
- `bash scripts/install_reflection_timer.sh` 실행 확인
- `systemctl --user list-timers hermes-hybrid-*`
- 즉시 실행: `systemctl --user start hermes-hybrid-reflection.service`
- 로그: `journalctl --user -u hermes-hybrid-reflection.service -n 50`

### skill registry 가 비어있다
```bash
python scripts/build_skill_registry.py
cat skills/registry.yaml
```

### SQLite DB 잠금
```powershell
# bot 종료 후
del data\state.db-wal
del data\state.db-shm
```

---

## 16. 기여

- 브랜치: `feature/<area>` → PR → `main`
- 커밋 메시지: Conventional Commits (`feat:`, `fix:`, `refactor:` 등)
- 한국어 커밋 메시지 OK (이 저장소는 한국어 주석 + 한국어 commit 사용)
- 시크릿 절대 커밋 금지 — `.env` / `auth.json` / `*.bak.*` 는 `.gitignore` 됨

---

## 17. 라이선스

미정.

---

## 참고

- **Hermes Agent (NousResearch)**: 외부 의존 — 직접 실행 엔진
- **Claude Code CLI**: Max OAuth, $0 marginal
- **설계 스펙**: [docs/architecture.md](docs/architecture.md)
- **Phase 1.5 inventory** (Profile/Job 분석):
  - [docs/PROFILE_INVENTORY.md](docs/PROFILE_INVENTORY.md) (작성 예정)
  - [docs/JOB_INVENTORY.md](docs/JOB_INVENTORY.md) (작성 예정)
  - [docs/PROFILE_JOB_MAP.md](docs/PROFILE_JOB_MAP.md) (작성 예정)
- **합의 메모** (메모리 시스템 폐기): [memory/project_mode_system_deprecation.md](memory/project_mode_system_deprecation.md)
- **calendar_ops 프로파일**: [profiles/calendar_ops/README.md](profiles/calendar_ops/README.md)
