# hermes-hybrid

**NousResearch Hermes 위에 얹은 "성장하는 개인 에이전트".**
Discord 가 입구, 모든 작업이 자기 행동을 로그로 쌓고, 주말마다 자기반성하고, 반복 패턴을 skill 후보로 올리는 자동화 어시스턴트.

단순히 "AI에게 물어보는" 봇이 아니라 — 작업 환경을 기억하고, 반복을 절차화하고, Discord 명령으로 계속 일하는 **persistent + curating** 에이전트로 설계됐다.

---

## 1. 핵심 기능 (6 axes)

사용자 vision 6개 축 기준 현재 동작 상태:

| 축 | 상태 | 구현 |
|---|---|---|
| **Memory** — 세션을 넘는 개인화 | ✅ | `SqliteMemory` (`src/memory/sqlite.py`) + `/memo` 슬래시 명령 + **memory inject** 옵트인 (`HERMES_MEMORY_INJECT_ENABLED`). LIKE substring 검색 + 임베딩 검색 옵션 (`HERMES_MEMORY_SEARCH_BACKEND=embedding` + bge-m3). |
| **Skills** — 반복 작업 절차화 | ✅ | profile-level `SKILL.md` 10개 (`profiles/*/skills/**`) + slash skill 5개 + **글로벌 sub-agent 17개** (`agents/{6 categories}/{name}/SKILL.md`). `SkillLibrary` + `AgentRegistry` 가 인덱싱. Curator 가 promotion / archive 후보 markdown 자동 surface. |
| **Tools** — web/terminal/file/MCP | ✅ via **Hermes Master** | 모든 LLM 호출이 **Hermes Master (`opencode` CLI / `gpt-5.5`)** 통과 — Claude Max OAuth 와 같은 $0 marginal 패턴. cron/watcher sub-call 의 ollama 라인은 보조. profile 별 `disabled_toolsets` 로 제어. |
| **Gateway** — Discord/Telegram/Slack | 🟡 부분 | Discord ✅ + Telegram ✅ (long-poll, stdlib only) + MCP server PoC ✅. **Slack 미구현**. |
| **Cron Automation** — 정기 작업 | ✅ | hermes profile cron 11잡 + reflection timer (일요일 22:00 KST) + curator timer (일요일 23:00 KST) + session importer timer (매시간) + Kanban CLI. 사용자 일회 install 후 자동. |
| **Delegation** — sub-agent 병렬 | 🟡 인터페이스 + 17 agent 정의 | `Delegator` Protocol + `SequentialHermesDelegator` + Phase 7 `agents/` 6 카테고리 / 17 SKILL.md (`AgentRegistry`). 진짜 병렬 실행은 Phase 8. |

---

## 2. 성장 루프 + 4-layer 다이어그램

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

### 데이터 흐름 (Discord → Master → Outputs)

```
Discord/Telegram 메시지
    ↓
Orchestrator (thin wrapper, master 위임)
    ↓
IntentRouter — RuleLayer / 슬래시 skill 단락 우선
    ↓ (자유 텍스트일 때)
PolicyGate — allowlist / 일일 토큰 cap / requires_confirmation 검증
    ↓
Hermes Master Orchestrator
    ├─ memory inject (옵트인)
    ├─ JobInventory snippet — profile/job 컨텍스트
    └─ opencode CLI 호출 (gpt-5.5)
    ↓
Critic.evaluate (Validator wrap + self_score 0~1 stamp)
    ↓
Response → Discord/Telegram
    ↓
ExperienceLogger.append → logs/experience/{date}.jsonl  (input/response 는 sha16+length 만)
    ↓
[hourly]      session importer ↑ — cron/watcher 결과도 통합
[Sun 22:00]   ReflectionJob   → logs/reflection/{ISO-WEEK}.md
[Sun 23:00]   CuratorJob      → logs/curator/{date}.md + handled_by_stats.json
                                + skill promotion / archive 후보
    ↓
사람 검토 → MEMORY.md 갱신 / Skill 승격 / 후속 작업
```

| 컴포넌트 | 위치 | 책임 |
|---|---|---|
| **HermesMasterOrchestrator** | [src/orchestrator/hermes_master.py](src/orchestrator/hermes_master.py) | All-via-master 엔트리. opencode CLI / gpt-5.5 단일 LLM lane. |
| **OpenCodeAdapter** | [src/opencode_adapter/adapter.py](src/opencode_adapter/adapter.py) | `opencode -p` subprocess wrapper. WSL/local backend. |
| **IntentRouter** | [src/integration/intent_router.py](src/integration/intent_router.py) | RuleLayer / 슬래시 skill / forced_profile / heavy 분기. |
| **PolicyGate** | [src/integration/policy_gate.py](src/integration/policy_gate.py) | allowlist / 일일 토큰 cap / requires_confirmation 검증 + Validator wrap. |
| **JobInventory** | [src/integration/job_inventory.py](src/integration/job_inventory.py) | profiles/ + skills/ runtime 스캔. master 가 prompt 구성에 사용. |
| **ExperienceLogger** | [src/core/experience_logger.py](src/core/experience_logger.py) | 모든 task 종료 시 JSONL 한 줄. privacy: 본문 저장 X, sha16 + length 만. |
| **Critic** | [src/core/critic.py](src/core/critic.py) | Validator wrap. retry/tier 정책 무영향, self_score 0~1 stamp. |
| **ReflectionJob** | [src/jobs/reflection_job.py](src/jobs/reflection_job.py) | 주간 통계 — 성공률, profile/handler/tier 분포, p50/p95 latency, top failure buckets. |
| **CuratorJob** | [src/jobs/curator_job.py](src/jobs/curator_job.py) | handler/tool 별 success/failure rate + skill promotion / archive 후보 markdown. |
| **SkillLibrary** | [src/core/skill_library.py](src/core/skill_library.py) | `profiles/*/skills/**/SKILL.md` 인덱스 → `skills/registry.yaml`. |
| **SessionImporter** | [src/core/session_importer.py](src/core/session_importer.py) | hermes 의 cron/watcher 세션 JSON → ExperienceLog 통합 (hourly timer). |
| **KanbanStore** | [src/core/kanban.py](src/core/kanban.py) | advisor_ops → installer_ops 핸드오프 채널 (single-file JSON). |
| **Memory search** | [src/memory/sqlite.py](src/memory/sqlite.py) + [src/memory/embedding.py](src/memory/embedding.py) | LIKE substring + 옵션 임베딩 검색 (bge-m3). |
| **Telegram gateway** | [src/gateway/telegram_bot.py](src/gateway/telegram_bot.py) | long-poll, stdlib only. allowlist 동일 적용. |

---

## 3. 아키텍처 흐름

```
Discord / Telegram
    │
    ├─ #일기 channel?     ──► IntentRouter: trigger_type=forced_profile
    │                          → master (journal_ops 페르소나)
    │
    ├─ /memo / /kanban /  ──► IntentRouter: 슬래시 skill 단락 (LLM 호출 X)
    │  /hybrid-*
    │
    └─ 자유 텍스트         ──► IntentRouter → PolicyGate → Hermes Master
                                                             (opencode/gpt-5.5)

cron / watcher (hermes WSL scheduler 가 직접 실행)
    │
    └─ profiles/{p}/cron/{category}/{job}.yaml prompt 실행
       (hermes profile 의 ollama 가 sub-call — master 와 별도 라인)
       │
       └─► [hourly] session importer 가 결과를 ExperienceLog 로 통합

ReflectionJob / CuratorJob (systemd-user timer, 일요일 22/23 KST)
    │
    └─ logs/experience/*.jsonl read → markdown + JSON 보고서
       + skill promotion / archive 후보 surfacing (사람 review)
```

### LLM lane (단일)

| Lane | 모델 | 호출 경로 | 비용 |
|---|---|---|---|
| **Master** | `gpt-5.5` (via `opencode` CLI) | 모든 Discord/Telegram 진입 → opencode subprocess | $0 marginal (Max-OAuth 류 패턴) |
| **Cron sub-call** (legacy 보존) | `qwen2.5:14b-instruct` / `qwen2.5-coder:32b` (ollama) | hermes profile cron 이 자체 schedule, master 우회 | $0 (로컬) |

> **2026-05-04**: OpenAI gpt-4o 제거 (legacy).
> **2026-05-06**: `system_mode` active/quiet 2-mode 폐기. JobFactory v1/v2 + Router + tier ladder 모두 제거. **All-via-master** (opencode/gpt-5.5) 전환.
> **Phase 7 (예정)**: 6 카테고리 / 17 agent (RESEARCH/PLANNING/IMPLEMENTATION/QUALITY/DOCUMENTATION/INFRASTRUCTURE) 통합 — `@coder`, `@reviewer` 등 sub-agent dispatch 인터페이스 도입.

---

## 4. 프로필 6개

각 프로필은 자체 페르소나 (`SOUL.md`) + 모델 정책 (`config.yaml`) + 잡들 (`cron/`, `on_demand/`, `watchers/`) + 스킬들 (`skills/*/SKILL.md`) + 메모리 (`memories/MEMORY.md`).

**모든 사용자 진입은 Hermes Master (`opencode` / `gpt-5.5`)** 통과. 아래 model 컬럼은 hermes profile cron sub-call 이 사용하는 fallback (legacy 호환).

| profile_id | 목적 | cron sub-call model | 잡 수 | discord 출력 |
|---|---|---|---:|---|
| **advisor_ops** | 도구 어드바이저 (보고 전용) — 다른 프로필 스캔 → 추천 + Kanban triage 발행 | `qwen2.5-coder:32b` (ollama) | cron 1 + on_demand 1 | `DISCORD_BRIEFING_WEBHOOK_URL` |
| **calendar_ops** | Google Calendar CRUD + 브리핑 + 충돌 감지 | `qwen2.5:14b` (ollama) | cron 7 + on_demand 5 + watcher 2 | briefing + weather webhook |
| **installer_ops** | Kanban worker — advisor 추천 task → install plan 첨부 | `qwen2.5-coder:32b` (ollama) | on_demand 1 (process_kanban_tasks) | kanban virtual channel |
| **journal_ops** | #일기 채널 → 24-필드 → Google Sheets | `qwen3:8b` (ollama) | on_demand 1 (forced_profile) | #일기 channel |
| **kk_job** | 게임 프로그래머 구인 리서칭 + 이력서/자소서 | `qwen2.5-coder:32b` (ollama) | cron 3 + on_demand 4 + watcher 1 | `DISCORD_KK_JOB_WEBHOOK_URL` |
| **mail_ops** | Gmail/Naver 받은편지함 알림 | `qwen2.5:14b` (ollama, 2026-05-06 gpt-4o-mini 폐기) | watcher 1 | `DISCORD_MAIL_WEBHOOK_URL` |

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
OLLAMA_ENABLED=true                                  # cron sub-call 용 (master 와 별개)
HERMES_HOME=/home/kang/.hermes                       # WSL 경로
```

### 7.2 Hermes Master (All-via-master, 2026-05-06~)
```env
HERMES_MASTER_ENABLED=true                           # production default — opencode CLI 통과
HERMES_MASTER_MODEL=gpt-5.5
HERMES_OPENCODE_CLI_PATH=/home/kang/.local/bin/opencode
HERMES_OPENCODE_CLI_BACKEND=wsl_subprocess           # wsl_subprocess | local_subprocess
HERMES_MASTER_TIMEOUT_MS=120000
```

### 7.3 성장 루프 (default-on)
```env
HERMES_EXPERIENCE_LOG_ENABLED=true                   # 모든 task → JSONL
HERMES_EXPERIENCE_LOG_ROOT=./logs/experience
```

### 7.4 Memory inject (default-off — privacy review 후 켜기)
```env
HERMES_MEMORY_INJECT_ENABLED=false
HERMES_MEMORY_INJECT_TOP_K=3
HERMES_MEMORY_SEARCH_BACKEND=like                    # like | embedding (bge-m3)
HERMES_MEMORY_EMBEDDING_MODEL=bge-m3
```

### 7.5 Telegram (선택)
```env
TELEGRAM_BOT_TOKEN=<token>
TELEGRAM_ALLOWED_USER_IDS=<your-id>
```

### 7.6 예산
```env
CLOUD_TOKEN_BUDGET_DAILY=100000
PER_USER_IN_FLIGHT_MAX=1
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

1. **모든 LLM 호출은 Hermes Master (`opencode` / `gpt-5.5`) entry point** 통과 — 2026-05-06 ~.
2. **Orchestrator 는 tool 을 직접 실행하지 않는다** — master 와 hermes 가 (R0).
3. **IntentRouter 는 결정적 단락만** — RuleLayer / 슬래시 skill / forced_profile / heavy. 자유 텍스트는 master 에 위임.
4. **PolicyGate 가 단일 contract** — allowlist / 일일 토큰 cap / `requires_confirmation` 통합.
5. **Validator 와 Critic 의 책임 분리** — Validator 가 retry/tier 결정, Critic 은 self_score 만 stamp. 점수가 retry 정책에 영향 X.
6. **모든 task 는 ExperienceLog 에 append-only** — 본문 X, sha16 + length 만 (privacy).
7. **Memory inject 는 default off** — 사용자 privacy 검토 후 명시 활성화.
8. **자동 행동 변경은 사람 검토 후** — Curator/Reflection 은 보고서만, MEMORY.md/SKILL 자동 수정 0.
9. **Profile 의 ollama 모델은 cron sub-call 전용** (legacy 호환) — Phase 7 의 6 카테고리 / 17 agent 통합으로 교체 예정.

자세한 설계 근거는 [docs/architecture.md](docs/architecture.md) + [memory/project_mode_system_deprecation.md](memory/project_mode_system_deprecation.md) 참조.

---

## 14. 로드맵

- [x] **2026-05-04 정리** — OpenAI legacy 제거, dev/playtest/gaming 3-mode 폐기
- [x] **2026-05-06 정리** — `system_mode` active/quiet 2-mode 폐기
- [x] **P0 성장 루프 첫 벽돌** — ExperienceLogger / Critic / ReflectionJob / CuratorJob / SkillLibrary
- [x] **Memory search + 임베딩 옵션** (LIKE + bge-m3)
- [x] **Phase 1.5** — Profile/Job inventory + ExperienceRecord routing 필드 (`job_id`/`trigger_type`/`v2_job_type`/`skill_ids`/`model_provider`)
- [x] **Phase 2** — cron/watcher → ExperienceLog 통합 (`scripts/import_hermes_sessions.py` hourly timer)
- [x] **Phase 3** — Curator skill promotion / archive 후보 markdown
- [x] **Phase 5 (부분)** — Telegram MVP gateway + Delegator interface (sub-agent stub)
- [x] **Phase 6** — Kanban store + `/kanban` slash + installer_ops first job
- [x] **Diagram-aligned migration (2026-05-06)** — Hermes Master Orchestrator (opencode/gpt-5.5) + Integration Layer 4 컴포넌트 + 레거시 dispatch (JobFactory v1/v2 / Router / tier ladder) 완전 삭제
- [x] **Phase 7** — 6 카테고리 / 17 sub-agent SKILL.md (RESEARCH 3 / PLANNING 2 / IMPLEMENTATION 4 / QUALITY 4 / DOCUMENTATION 2 / INFRASTRUCTURE 2) + `AgentRegistry` + `JobInventory.agents()` lookup
- [ ] **Phase 8** — `@agent` 멘션 → master 가 해당 agent SKILL.md 를 prompt 에 inject 하는 dispatch wiring
- [ ] **Phase 9** — `Delegator.delegate_many` 진짜 병렬 실행 (Phase 5b)
- [ ] **Phase 10** — Slack gateway, Discord 슬래시 명령 확장

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
