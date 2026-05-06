# hermes-hybrid

**NousResearch Hermes 위에 얹은 "성장하는 개인 에이전트".**
Discord 가 입구, 모든 작업이 자기 행동을 로그로 쌓고, 주말마다 자기반성하고, 반복 패턴을 sub-agent 위에 정리하는 자동화 어시스턴트.

단순히 "AI에게 물어보는" 봇이 아니라 — 작업 환경을 기억하고, 반복을 절차화하고, Discord 명령으로 계속 일하는 **persistent + curating** 에이전트로 설계됐다.

> **Phase 8 (2026-05-06)**: 6 profile + 27 잡 + profile-local 10 SKILL.md 를 폐기하고 17 sub-agent (`agents/{6 categories}/`) 단일 구조로 전환. 모든 자동화 cron / watcher 가 중단됐고, 이제 사용자 명시 요청을 master 가 받아 적절한 agent 에 위임한다.

---

## 1. 핵심 기능 (6 axes)

사용자 vision 6개 축 기준 현재 동작 상태:

| 축 | 상태 | 구현 |
|---|---|---|
| **Memory** — 세션을 넘는 개인화 | ✅ | `SqliteMemory` (`src/memory/sqlite.py`) + `/memo` 슬래시 명령 + **memory inject** 옵트인 (`HERMES_MEMORY_INJECT_ENABLED`). LIKE substring 검색 + 임베딩 검색 옵션 (`HERMES_MEMORY_SEARCH_BACKEND=embedding` + bge-m3). |
| **Skills** — 반복 작업 절차화 | ✅ | **17 sub-agent SKILL.md** (`agents/{research,planning,implementation,quality,documentation,infrastructure}/{name}/SKILL.md`) + slash skill 4개. `AgentRegistry` 가 인덱싱. Curator 가 promotion / archive 후보 markdown 자동 surface. |
| **Tools** — web/terminal/file/MCP | ✅ via **Hermes Master** | 모든 LLM 호출이 **Hermes Master (`opencode` CLI / `gpt-5.5`)** 통과 — Claude Max OAuth 와 같은 $0 marginal 패턴. |
| **Gateway** — Discord/Telegram/Slack | 🟡 부분 | Discord ✅ + Telegram ✅ (long-poll, stdlib only) + MCP server PoC ✅. **Slack 미구현**. |
| **Cron Automation** — 정기 작업 | ❌ Phase 8 폐기 | reflection timer (일요일 22:00 KST) + curator timer (일요일 23:00 KST) + session importer (매시간) + Kanban CLI 만 잔존. profile cron 27 잡 모두 폐기. |
| **Delegation** — sub-agent 병렬 | 🟡 인터페이스 + 17 agent 정의 | `Delegator` Protocol + `SequentialHermesDelegator` + Phase 7 `agents/` 6 카테고리 / 17 SKILL.md (`AgentRegistry`). 진짜 병렬 실행 + master `@agent` 멘션 dispatch wiring 은 Phase 9. |

---

## 2. 성장 루프 + 4-layer 다이어그램 (Phase 8)

```
                        ┌─────────────────────┐
                        │   Agent Layer       │   research / planning /
                        │   (17 sub-agents)    │   implementation / quality /
                        └──────────┬──────────┘   documentation / infrastructure
                                   │ exposed by
                        ┌──────────▼──────────┐
                        │  Slash Skills       │   /memo · /kanban · /hybrid-status
                        │   (4 deterministic)  │   /hybrid-budget
                        └──────────┬──────────┘
                                   │ uses
   Execution Modes ─────► Integration Layer ─────► Hermes Master Orchestrator
   ┌──────────────┐      ┌────────────────────┐    ┌──────────────────────┐
   │ on_demand    │      │ Intent Router      │    │  opencode CLI        │
   │ (Discord/    │ ───► │ Policy Gate        │ ──►│  gpt-5.5             │
   │  Telegram)   │      │ Agent Inventory    │    │  ($0 marginal)       │
   │ heavy (!)    │      │ Session Importer   │    └──────────┬───────────┘
   └──────────────┘      └────────────────────┘               │ emits
                                                              │
                        ┌────────────────────────────────────▼─────────────┐
                        │  Outputs and Feedback                            │
                        │   ExperienceLog / Discord DM / Telegram          │
                        │   Sheets / Calendar / Docs / Kanban (via @devops)│
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
PolicyGate — allowlist / 일일 토큰 cap 검증
    ↓
Hermes Master Orchestrator
    ├─ memory inject (옵트인)
    └─ opencode CLI 호출 (gpt-5.5) — 향후 @agent 멘션 시 SKILL.md inject (Phase 9)
    ↓
Critic.evaluate (Validator wrap + self_score 0~1 stamp)
    ↓
Response → Discord/Telegram
    ↓
ExperienceLogger.append → logs/experience/{date}.jsonl  (input/response 는 sha16+length 만)
    ↓
[hourly]      session importer ↑ — opencode session JSON 도 통합
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
| **IntentRouter** | [src/integration/intent_router.py](src/integration/intent_router.py) | RuleLayer / 슬래시 skill / heavy 분기. |
| **PolicyGate** | [src/integration/policy_gate.py](src/integration/policy_gate.py) | allowlist / 일일 토큰 cap + Validator wrap. |
| **JobInventory** | [src/integration/job_inventory.py](src/integration/job_inventory.py) | `agents/` 17 sub-agent runtime 스캔 → master 가 `@coder` 등 핸들 lookup 에 사용. |
| **AgentRegistry** | [src/agents/__init__.py](src/agents/__init__.py) | `agents/{category}/{name}/SKILL.md` 인덱서. |
| **ExperienceLogger** | [src/core/experience_logger.py](src/core/experience_logger.py) | 모든 task 종료 시 JSONL 한 줄. privacy: 본문 저장 X, sha16 + length 만. |
| **Critic** | [src/core/critic.py](src/core/critic.py) | Validator wrap. retry/tier 정책 무영향, self_score 0~1 stamp. |
| **ReflectionJob** | [src/jobs/reflection_job.py](src/jobs/reflection_job.py) | 주간 통계 — 성공률, handler/tier 분포, p50/p95 latency, top failure buckets. |
| **CuratorJob** | [src/jobs/curator_job.py](src/jobs/curator_job.py) | handler/tool 별 success/failure rate + skill promotion / archive 후보 markdown. |
| **SessionImporter** | [src/core/session_importer.py](src/core/session_importer.py) | opencode/hermes 의 session JSON → ExperienceLog 통합 (hourly timer). |
| **KanbanStore** | [src/core/kanban.py](src/core/kanban.py) | agent 간 hand-off 채널 (single-file JSON). |
| **Memory search** | [src/memory/sqlite.py](src/memory/sqlite.py) + [src/memory/embedding.py](src/memory/embedding.py) | LIKE substring + 옵션 임베딩 검색 (bge-m3). |
| **Telegram gateway** | [src/gateway/telegram_bot.py](src/gateway/telegram_bot.py) | long-poll, stdlib only. allowlist 동일 적용. |

---

## 3. Agent Layer (17 sub-agents, 6 categories)

각 agent 는 `agents/{category}/{name}/SKILL.md` frontmatter + 본문으로 정의.
`AgentRegistry` 가 핸들 (`@coder` / `@reviewer` 등) 으로 lookup.

| 카테고리 | agents | 책임 요지 |
|---|---|---|
| **RESEARCH** | [@finder](agents/research/finder/SKILL.md) · [@analyst](agents/research/analyst/SKILL.md) · [@researcher](agents/research/researcher/SKILL.md) | 위치 / 분석 / 외부 조사 (web_search · job_crawler · 캘린더 read) |
| **PLANNING** | [@architect](agents/planning/architect/SKILL.md) · [@planner](agents/planning/planner/SKILL.md) | 시스템 설계 / 작업 분해 |
| **IMPLEMENTATION** | [@coder](agents/implementation/coder/SKILL.md) · [@editor](agents/implementation/editor/SKILL.md) · [@fixer](agents/implementation/fixer/SKILL.md) · [@refactorer](agents/implementation/refactorer/SKILL.md) | 신규 작성 / 외과적 수정 / 버그 fix / 구조 개선 |
| **QUALITY** | [@reviewer](agents/quality/reviewer/SKILL.md) · [@tester](agents/quality/tester/SKILL.md) · [@debugger](agents/quality/debugger/SKILL.md) · [@security](agents/quality/security/SKILL.md) | 리뷰 / 테스트 / 진단 / 보안 |
| **DOCUMENTATION** | [@documenter](agents/documentation/documenter/SKILL.md) · [@commenter](agents/documentation/commenter/SKILL.md) | 외부 문서 (README/runbook/이력서/자소서) / 인라인 주석 |
| **INFRASTRUCTURE** | [@devops](agents/infrastructure/devops/SKILL.md) · [@optimizer](agents/infrastructure/optimizer/SKILL.md) | 배포·운영 + Discord/Sheets/Calendar 발송 + install plan / 성능 |

각 SKILL.md frontmatter 는 `when_to_use` / `not_for` / `inputs` / `outputs` / `metadata.hermes` 표준. 자세한 매핑은 [docs/AGENT_INVENTORY.md](docs/AGENT_INVENTORY.md) 참조.

---

## 4. 요구사항

- **OS**: Windows 10/11 + WSL2 Ubuntu
- **Python**: 3.11+
- **opencode CLI**: WSL 안 (`~/.local/bin/opencode`), 1회 `opencode auth login`
- **Claude Code CLI**: WSL 안 (`~/.local/bin/claude`), Max 구독 OAuth (heavy 경로용)
- **Ollama**: optional — 로컬 fallback / memory embedding 시 필요

```bash
# Optional Ollama models (memory embedding 사용 시)
ollama pull bge-m3                       # memory embedding (선택)
```

---

## 5. 설치

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
notepad .env       # 최소 4 항목 (§6)

# 3. WSL CLI 확인
wsl -d Ubuntu -- bash -lc "opencode --version && claude --version"

# 4. 프리플라이트
python -m src.preflight
```

---

## 6. 환경변수 (핵심)

> 변수 → agent 매핑은 [docs/AGENT_ENV.md](docs/AGENT_ENV.md) 의 카탈로그 참조.

### 6.1 필수 (4개)
```env
DISCORD_BOT_TOKEN=<your-bot-token>
DISCORD_ALLOWED_USER_IDS=<your-discord-user-id>     # 허용 user CSV (R12 fail-closed)
HERMES_MASTER_ENABLED=true                           # opencode CLI 통과
HERMES_HOME=/home/kang/.hermes                       # WSL 경로
```

### 6.2 Hermes Master (All-via-master)
```env
HERMES_MASTER_MODEL=gpt-5.5
HERMES_OPENCODE_CLI_PATH=/home/kang/.local/bin/opencode
HERMES_OPENCODE_CLI_BACKEND=wsl_subprocess           # wsl_subprocess | local_subprocess
HERMES_MASTER_TIMEOUT_MS=120000
```

### 6.3 Agent webhooks (@devops 가 사용)
```env
DISCORD_BRIEFING_WEBHOOK_URL=https://discord.com/api/webhooks/...
GOOGLE_SHEETS_WEBHOOK_URL=https://script.google.com/macros/s/.../exec
GOOGLE_OAUTH_CREDENTIALS=/home/kang/.hermes/google_client_secret.json
GOOGLE_CALENDAR_MCP_TOKEN_PATH=/home/kang/.hermes/google_token.json
GOOGLE_CALENDAR_ID=primary
BRAVE_SEARCH_API_KEY=<for-@researcher-web_search>
```

### 6.4 성장 루프 (default-on)
```env
HERMES_EXPERIENCE_LOG_ENABLED=true                   # 모든 task → JSONL
HERMES_EXPERIENCE_LOG_ROOT=./logs/experience
```

### 6.5 Memory inject (default-off — privacy review 후 켜기)
```env
HERMES_MEMORY_INJECT_ENABLED=false
HERMES_MEMORY_INJECT_TOP_K=3
HERMES_MEMORY_SEARCH_BACKEND=like                    # like | embedding (bge-m3)
HERMES_MEMORY_EMBEDDING_MODEL=bge-m3
```

### 6.6 Telegram (선택)
```env
TELEGRAM_BOT_TOKEN=<token>
TELEGRAM_ALLOWED_USER_IDS=<your-id>
```

### 6.7 예산
```env
CLOUD_TOKEN_BUDGET_DAILY=100000
PER_USER_IN_FLIGHT_MAX=1
```

전체 변수는 [src/config.py](src/config.py) 의 `Settings` 클래스 + [.env.example](.env.example) 참조.

---

## 7. 실행

```powershell
# 원클릭 (Windows)
.\run_all.bat

# 단계별 디버그
.\.venv\Scripts\Activate.ps1
python scripts\run_bot.py
```

---

## 8. 성장 루프 활성화 (사용자 일회 작업)

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
4. **월간** — Skill promotion 후보 review (Phase 9 작업 후 자동화 예정)

---

## 9. Discord 사용

```
@bot 안녕                              → master (opencode/gpt-5.5)
@bot 파이썬으로 피보나치 짜줘            → master → (Phase 9 후 @coder 자동 위임)
@bot /memo save 내일 회의 9시           → SqliteMemory 저장
@bot /memo list                        → 최근 메모 20개
@bot /hybrid-status                    → settings 요약
@bot /hybrid-budget                    → 일일 토큰 예산
@bot /kanban list                      → Kanban tasks
@bot 오늘 일정 알려줘                   → master → @researcher (캘린더 read MCP)
@bot 내일 14:00 회의 추가              → master → @devops (캘린더 write MCP, 사용자 확인 후)
@bot !heavy 복잡한 아키텍처 분석        → Claude CLI Sonnet/Opus, 명시 opt-in
```

> Phase 8 후 자동화는 모두 폐기됐다. 매일 아침 브리핑 / #일기 자동 추출 /
> 잡 매칭 폴링 등은 더 이상 자동 실행되지 않는다. 동일 결과를 원하면
> master 한테 명시 요청 (예: `@bot 오늘 일정 + 일기 가능한 항목 정리해줘`).

---

## 10. 디렉터리

```
hermes-hybrid/
├─ scripts/                      # 진입점 + 운영 + cron timer
│  ├─ run_bot.py                 # Discord bot main
│  ├─ run_telegram_bot.py        # Telegram MVP gateway
│  ├─ install_reflection_timer.sh   # 일요일 22:00 KST
│  ├─ install_curator_timer.sh      # 일요일 23:00 KST
│  ├─ install_session_importer_timer.sh  # 매시간
│  ├─ reflection_job.py / curator_job.py  # CLI 진입점
│  ├─ kanban_cli.py
│  └─ build_skill_registry.py
│
├─ src/
│  ├─ gateway/                   # Discord bot (allowlist) + Telegram
│  ├─ orchestrator/              # 메인 흐름 (HermesMaster 위임)
│  │   ├─ orchestrator.py        # thin wrapper
│  │   └─ hermes_master.py       # opencode/gpt-5.5 single-shot
│  ├─ integration/               # IntentRouter / PolicyGate / JobInventory / SessionImporter
│  ├─ agents/                    # AgentRegistry (`agents/` 스캔)
│  ├─ opencode_adapter/          # opencode CLI subprocess
│  ├─ claude_adapter/            # Claude CLI subprocess (heavy 경로)
│  ├─ memory/                    # SqliteMemory + search (LIKE / bge-m3)
│  ├─ state/                     # TaskState + Repository
│  ├─ skills/                    # 슬래시 명령 (HybridMemo / Status / Budget / Kanban)
│  ├─ mcp/                       # MCP server PoC
│  │
│  ├─ core/                      # 🌱 성장 루프 핵심
│  │   ├─ experience_logger.py
│  │   ├─ critic.py
│  │   ├─ kanban.py
│  │   ├─ session_importer.py
│  │   └─ skill_library.py       # SKILL.md 인덱서 (legacy)
│  │
│  └─ jobs/                      # 🌱 cron 가능 자기개선 잡
│      ├─ base.py
│      ├─ reflection_job.py
│      └─ curator_job.py
│
├─ agents/                       # 🌟 17 sub-agent SKILL.md (Phase 7 산출 + Phase 8 흡수)
│  ├─ research/{finder,analyst,researcher}/SKILL.md
│  ├─ planning/{architect,planner}/SKILL.md
│  ├─ implementation/{coder,editor,fixer,refactorer}/SKILL.md
│  ├─ quality/{reviewer,tester,debugger,security}/SKILL.md
│  ├─ documentation/{documenter,commenter}/SKILL.md
│  └─ infrastructure/{devops,optimizer}/SKILL.md
│
├─ tests/                        # pytest, 269 pass / 5 skip
├─ logs/
│  ├─ experience/                # task append-only JSONL
│  ├─ reflection/                # 주간 markdown
│  └─ curator/                   # 주간 markdown + JSON
├─ data/                         # state.db (SQLite, gitignore) + kanban.json
├─ _archive/                     # Phase 8 이전 자료 보존 (history)
│  ├─ profiles_envs/{profile}/.env.template     # 변수명 inventory
│  └─ profiles_memories/{advisor_ops,installer_ops}/   # 메타 컨텍스트
└─ docs/
   ├─ AGENT_ENV.md               # agent 별 환경변수 카탈로그
   ├─ AGENT_INVENTORY.md         # 17 agent 표 (Phase 8 산출)
   ├─ MASTER_ARCHITECTURE.md     # all-via-master 다이어그램
   ├─ architecture.md            # 레거시 — pre-Phase-8 reference
   ├─ JOB_INVENTORY.md           # legacy — pre-Phase-8 reference
   ├─ PROFILE_INVENTORY.md       # legacy — pre-Phase-8 reference
   └─ PROFILE_JOB_MAP.md         # legacy — pre-Phase-8 reference
```

---

## 11. 테스트

```powershell
pytest -q                                 # 전체 (269 pass / 5 skip)
pytest tests/test_experience_logger.py    # 11 tests, JSONL contract
pytest tests/test_critic.py               # 11 tests, self_score lookup
pytest tests/test_reflection_job.py       # 9 tests, weekly stats
pytest tests/test_curator_job.py          # 9 tests, handler stat
pytest tests/test_skill_library.py        # 8 tests, SKILL.md scanner
pytest tests/test_memory_search.py        # 13 tests, search + inject
pytest tests/test_agent_registry.py       # 17-agent 인덱싱
```

---

## 12. 설계 불변식

1. **모든 LLM 호출은 Hermes Master (`opencode` / `gpt-5.5`) entry point** 통과 — 2026-05-06 ~.
2. **Orchestrator 는 tool 을 직접 실행하지 않는다** — master 가.
3. **IntentRouter 는 결정적 단락만** — RuleLayer / 슬래시 skill / heavy. 자유 텍스트는 master 에 위임.
4. **PolicyGate 가 단일 contract** — allowlist + 일일 토큰 cap. (HITL `requires_confirmation` 은 Phase 8 에서 폐기.)
5. **Validator 와 Critic 의 책임 분리** — Validator 가 retry/tier 결정, Critic 은 self_score 만 stamp.
6. **모든 task 는 ExperienceLog 에 append-only** — 본문 X, sha16 + length 만 (privacy).
7. **Memory inject 는 default off** — 사용자 privacy 검토 후 명시 활성화.
8. **자동 행동 변경은 사람 검토 후** — Curator/Reflection 은 보고서만, MEMORY.md/SKILL 자동 수정 0.
9. **Profile 단위 자동화 0** — Phase 8 에서 6 profile + 27 잡 모두 폐기. 매 요청은 사용자 명시 + master 위임.

자세한 설계 근거는 [docs/MASTER_ARCHITECTURE.md](docs/MASTER_ARCHITECTURE.md) 참조.

---

## 13. 로드맵

- [x] **2026-05-04 정리** — OpenAI legacy 제거, dev/playtest/gaming 3-mode 폐기
- [x] **2026-05-06 정리** — `system_mode` active/quiet 2-mode 폐기
- [x] **P0 성장 루프 첫 벽돌** — ExperienceLogger / Critic / ReflectionJob / CuratorJob / SkillLibrary
- [x] **Memory search + 임베딩 옵션** (LIKE + bge-m3)
- [x] **Phase 1.5** — Profile/Job inventory + ExperienceRecord routing 필드
- [x] **Phase 2** — cron/watcher → ExperienceLog 통합 (hourly timer)
- [x] **Phase 3** — Curator skill promotion / archive 후보 markdown
- [x] **Phase 5** — Telegram MVP gateway + Delegator interface
- [x] **Phase 6** — Kanban store + `/kanban` slash + advisor → installer hand-off
- [x] **Diagram-aligned migration (2026-05-06)** — Hermes Master Orchestrator (opencode/gpt-5.5) + Integration Layer 4 컴포넌트 + 레거시 dispatch 완전 삭제
- [x] **Phase 7** — 6 카테고리 / 17 sub-agent SKILL.md + `AgentRegistry` + `JobInventory.agents()` lookup
- [x] **Phase 8 (2026-05-06)** — 6 profile + 27 잡 + profile-local 10 SKILL.md 폐기. 17 agent 단일 구조. profile .env 변수 inventory 보존 (`_archive/profiles_envs/`, `docs/AGENT_ENV.md`).
- [ ] **Phase 9** — `@agent` 멘션 → master 가 해당 agent SKILL.md 를 prompt 에 inject 하는 dispatch wiring
- [ ] **Phase 10** — `Delegator.delegate_many` 진짜 병렬 실행 + agent 간 결과 집계
- [ ] **Phase 11** — Slack gateway, Discord 슬래시 명령 확장

---

## 14. 문제 해결

### "opencode: command not found" (WSL)
```bash
wsl -d Ubuntu -- bash -lc "which opencode"
# 경로 다르면 .env 의 HERMES_OPENCODE_CLI_PATH 수정
# 인증 필요 시: opencode auth login
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

### SQLite DB 잠금
```powershell
# bot 종료 후
del data\state.db-wal
del data\state.db-shm
```

### Phase 8 마이그레이션 — profile/.env 통합
```bash
# 기존 profile/.env 의 실 값을 root .env 로 옮김 (P8 plan 의 §3.4)
cat profiles/calendar_ops/.env profiles/journal_ops/.env  # backup 시 한 번만 확인
# 변수 카탈로그: docs/AGENT_ENV.md
```

---

## 15. 기여

- 브랜치: `feature/<area>` → PR → `main`
- 커밋 메시지: Conventional Commits (`feat:`, `fix:`, `refactor:` 등)
- 한국어 커밋 메시지 OK (이 저장소는 한국어 주석 + 한국어 commit 사용)
- 시크릿 절대 커밋 금지 — `.env` / `auth.json` / `*.bak.*` 는 `.gitignore` 됨

---

## 16. 라이선스

미정.

---

## 참고

- **opencode CLI**: [opencode docs](https://opencode.ai/) — Claude CLI 패턴의 OAuth 기반 LLM gateway
- **Claude Code CLI**: Max OAuth, $0 marginal
- **설계 스펙**: [docs/MASTER_ARCHITECTURE.md](docs/MASTER_ARCHITECTURE.md)
- **17 sub-agent**: [docs/AGENT_INVENTORY.md](docs/AGENT_INVENTORY.md)
- **agent 별 환경변수**: [docs/AGENT_ENV.md](docs/AGENT_ENV.md)
- **Pre-Phase-8 reference** (legacy 자료):
  - [docs/PROFILE_INVENTORY.md](docs/PROFILE_INVENTORY.md)
  - [docs/JOB_INVENTORY.md](docs/JOB_INVENTORY.md)
  - [docs/PROFILE_JOB_MAP.md](docs/PROFILE_JOB_MAP.md)
- **합의 메모** (메모리 시스템 폐기): [memory/project_mode_system_deprecation.md](memory/project_mode_system_deprecation.md)
