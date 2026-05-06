# hermes-hybrid

**NousResearch Hermes 위에 얹은 "성장하는 개인 에이전트".**
Discord 가 입구, 모든 작업이 자기 행동을 로그로 쌓고, 주말마다 자기반성하고, 반복 패턴을 sub-agent 위에 정리하는 자동화 어시스턴트.

단순히 "AI에게 물어보는" 봇이 아니라 — 작업 환경을 기억하고, 반복을 절차화하고, Discord 명령으로 계속 일하는 **persistent + curating** 에이전트로 설계됐다.

> **Phase 11 (2026-05-06)**: opencode 폐기 + master = **Claude CLI (Max OAuth)** 단일 lane. 모델 default = **opus**. heavy / c1 / opencode 일괄 제거. `ClaudeAgentDelegator` 가 Phase 10 병렬 fan-out 담당.
>
> **Phase 10 (2026-05-06)**: `master_parallel_agents=true` 옵트인 시 2개 이상의 `@handle` 멘션이 발견되면 각 agent 별로 독립 claude 호출을 `asyncio.gather` 로 동시 실행 후 결과 집계.
>
> **Phase 9 (2026-05-06)**: master 가 사용자 메시지의 `@coder` / `@reviewer` 같은 mention 을 자동 인식해 해당 sub-agent 의 SKILL.md frontmatter 를 system prompt 에 inject. `IntentRouter.agent_handles` + `TaskState.agent_handles` + `ExperienceRecord.agent_handles` 로 사용 통계도 자동 누적.
>
> **Phase 8 (2026-05-06)**: 6 profile + 27 잡 + profile-local 10 SKILL.md 를 폐기하고 17 sub-agent (`agents/{6 categories}/`) 단일 구조로 전환. 모든 자동화 cron / watcher 가 중단됐고, 이제 사용자 명시 요청을 master 가 받아 적절한 agent 에 위임한다.

---

## 1. 핵심 기능 (6 axes)

사용자 vision 6개 축 기준 현재 동작 상태:

| 축 | 상태 | 구현 |
|---|---|---|
| **Memory** — 세션을 넘는 개인화 | ✅ | `SqliteMemory` (`src/memory/sqlite.py`) + `/memo` 슬래시 명령 + **memory inject** 옵트인 (`HERMES_MEMORY_INJECT_ENABLED`). LIKE substring 검색 + 임베딩 검색 옵션 (`HERMES_MEMORY_SEARCH_BACKEND=embedding` + bge-m3). |
| **Skills** — 반복 작업 절차화 | ✅ | **17 sub-agent SKILL.md** (`agents/{research,planning,implementation,quality,documentation,infrastructure}/{name}/SKILL.md`) + slash skill 4개. `AgentRegistry` 가 인덱싱. Curator 가 promotion / archive 후보 markdown 자동 surface. |
| **Tools** — web/terminal/file/MCP | ✅ via **Hermes Master** | 모든 LLM 호출이 **Hermes Master (`claude` CLI / `opus`)** 통과 — Claude Max OAuth, $0 marginal. |
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
   │ on_demand    │      │ Intent Router      │    │  claude CLI          │
   │ (Discord/    │ ───► │ Policy Gate        │ ──►│  opus (Max OAuth)    │
   │  Telegram)   │      │ Agent Inventory    │    │  ($0 marginal)       │
   │              │      │ Session Importer   │    └──────────┬───────────┘
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
    └─ claude CLI 호출 (opus, Max OAuth) — @agent 멘션 시 SKILL.md inject (Phase 9)
    ↓
Critic.evaluate (Validator wrap + self_score 0~1 stamp)
    ↓
Response → Discord/Telegram
    ↓
ExperienceLogger.append → logs/experience/{date}.jsonl  (input/response 는 sha16+length 만)
    ↓
[hourly]      session importer ↑ — claude session JSON 도 통합
[Sun 22:00]   ReflectionJob   → logs/reflection/{ISO-WEEK}.md
[Sun 23:00]   CuratorJob      → logs/curator/{date}.md + handled_by_stats.json
                                + skill promotion / archive 후보
    ↓
사람 검토 → MEMORY.md 갱신 / Skill 승격 / 후속 작업
```

| 컴포넌트 | 위치 | 책임 |
|---|---|---|
| **HermesMasterOrchestrator** | [src/orchestrator/hermes_master.py](src/orchestrator/hermes_master.py) | All-via-master 엔트리. claude CLI / opus (Max OAuth) 단일 LLM lane. |
| **ClaudeCodeAdapter** | [src/claude_adapter/adapter.py](src/claude_adapter/adapter.py) | `claude -p --output-format json` subprocess wrapper. WSL/local backend. |
| **IntentRouter** | [src/integration/intent_router.py](src/integration/intent_router.py) | RuleLayer / 슬래시 skill 단락 + `@handle` mention 추출. |
| **PolicyGate** | [src/integration/policy_gate.py](src/integration/policy_gate.py) | allowlist / 일일 토큰 cap + Validator wrap. |
| **JobInventory** | [src/integration/job_inventory.py](src/integration/job_inventory.py) | `agents/` 17 sub-agent runtime 스캔 → master 가 `@coder` 등 핸들 lookup 에 사용. |
| **AgentRegistry** | [src/agents/__init__.py](src/agents/__init__.py) | `agents/{category}/{name}/SKILL.md` 인덱서. |
| **ExperienceLogger** | [src/core/experience_logger.py](src/core/experience_logger.py) | 모든 task 종료 시 JSONL 한 줄. privacy: 본문 저장 X, sha16 + length 만. |
| **Critic** | [src/core/critic.py](src/core/critic.py) | Validator wrap. retry/tier 정책 무영향, self_score 0~1 stamp. |
| **ReflectionJob** | [src/jobs/reflection_job.py](src/jobs/reflection_job.py) | 주간 통계 — 성공률, handler/tier 분포, p50/p95 latency, top failure buckets. |
| **CuratorJob** | [src/jobs/curator_job.py](src/jobs/curator_job.py) | handler/tool 별 success/failure rate + skill promotion / archive 후보 markdown. |
| **SessionImporter** | [src/core/session_importer.py](src/core/session_importer.py) | claude session JSON → ExperienceLog 통합 (hourly timer). |
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

- **OS**: Windows 10/11 + WSL2 Ubuntu (WSL 은 claude CLI subprocess 호출용)
- **Python**: 3.11+ (Windows host)
- **Claude Code CLI**: WSL 안 (`~/.local/bin/claude`). 1회 `claude /login` (Max 구독 OAuth — $0 marginal). **모든 master LLM 호출의 단일 lane**
- **Ollama**: optional — `HERMES_MEMORY_SEARCH_BACKEND=embedding` 일 때만 필요 (master 핫패스 X)

```bash
# Optional — memory embedding 사용 시
ollama pull bge-m3
```

> **폐기됨 (Phase 8/10 후 의존 X)**: Hermes CLI (`hermes`), profile cron, Hermes
> dashboard (`hermes-dashboard.service`), Hermes gateway 자동 등록. 봇은 Windows
> host 에서 단독으로 돌고 WSL 은 CLI subprocess 만 띄움.

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
notepad .env       # 최소 3 항목 (§6.1)

# 3. WSL 진입점 검증
wsl -d Ubuntu -- bash -lc "claude --version"

# 4. Claude CLI 인증 — WSL TUI 진입 후 /login 입력 (이미 Max OAuth 면 skip)
wsl -d Ubuntu -- bash -lc "claude"   # /login → Max 구독 인증 → /quit

# 5. 프리플라이트 (Discord allowlist + Ollama health 검증)
.\.venv\Scripts\Activate.ps1
python -c "import asyncio; from src.preflight import run_preflight; from src.config import Settings; r = asyncio.run(run_preflight(Settings(), require_gateway_stopped=False)); print('OK' if r.ok else 'FAIL', r.errors, r.warnings)"
```

---

## 6. 환경변수 (핵심)

> 변수 → agent 매핑은 [docs/AGENT_ENV.md](docs/AGENT_ENV.md) 의 카탈로그 참조.

### 6.1 필수 (3개)
```env
DISCORD_BOT_TOKEN=<your-bot-token>
DISCORD_ALLOWED_USER_IDS=<your-discord-user-id>     # 허용 user CSV (R12 fail-closed)
MASTER_ENABLED=true                                  # claude CLI 통과
```

### 6.2 Hermes Master (All-via-master)
```env
MASTER_MODEL=opus
MASTER_CLI_PATH=/home/kang/.local/bin/claude
MASTER_CLI_BACKEND=wsl_subprocess                    # wsl_subprocess | local_subprocess
MASTER_TIMEOUT_MS=120000
WSL_DISTRO=Ubuntu                                    # WSL 배포판 (subprocess 호출 대상)
```

> pydantic Settings 는 env var ↔ field 이름을 case-insensitive 로 매칭한다.
> `MASTER_ENABLED=true` 가 `Settings.master_enabled` 에 매핑. 기존 `.env` 의
> `HERMES_MASTER_*` 류 prefix 는 매칭 X (extra="ignore" 로 silently drop).

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
EXPERIENCE_LOG_ENABLED=true                          # 모든 task → JSONL
EXPERIENCE_LOG_ROOT=./logs/experience
```

### 6.5 Memory inject (default-off — privacy review 후 켜기)
```env
MEMORY_INJECT_ENABLED=false
MEMORY_INJECT_TOP_K=3
MEMORY_SEARCH_BACKEND=like                           # like | embedding (bge-m3)
MEMORY_EMBEDDING_MODEL=bge-m3
```

### 6.5a Phase 10 — Parallel @handle dispatch (default-off — opt-in)
```env
MASTER_PARALLEL_AGENTS=false                         # 2+ @handle 시 fan-out
MASTER_PARALLEL_MAX_CONCURRENCY=3                    # 동시 claude subprocess 수
```

> 켜면 N 개 멘션 → N 개 claude 호출이라 비용/지연/Max OAuth 한도가 N 배.
> 단일 호출 + SKILL.md inject (Phase 9 default) 이 대부분 충분 — 정말로
> 두 agent 의 독립 응답이 필요할 때만 켜라.

### 6.6 Telegram (선택)
```env
TELEGRAM_BOT_TOKEN=<token>
TELEGRAM_ALLOWED_USER_IDS=<your-id>
```

### 6.7 예산
```env
CLOUD_TOKEN_BUDGET_DAILY=100000                      # PolicyGate 일일 cap
PER_USER_IN_FLIGHT_MAX=1                             # R13 — per-user 동시 요청 cap
```

전체 변수는 [src/config.py](src/config.py) 의 `Settings` 클래스 + [.env.example](.env.example) 참조.

---

## 7. 실행

```powershell
# 원클릭 (Windows) — Ollama 워밍업 + WSL 키프얼라이브 + 봇 부팅
.\run_all.bat

# 단계별 디버그 (run_all.bat 의 [3/3] 만 직접)
.\.venv\Scripts\Activate.ps1
python scripts\run_bot.py
```

`run_all.bat` 의 3 단계 (Phase 10 기준):

| # | 단계 | 비고 |
|---|---|---|
| 1 | **Ollama 부팅 (선택)** | `HERMES_MEMORY_SEARCH_BACKEND=embedding` 일 때만 필요. 그 외 INFO 메시지로 skip. |
| 2 | **WSL 워밍업 + keepalive** | microsoft/WSL#10205 회피 — 마지막 로그인 세션이 사라져도 systemd-user 가 죽지 않도록 hidden bash loop |
| 3 | **Discord 봇 부팅** | `start.bat` → `start.ps1` → `python scripts/run_bot.py` |

> 기존 `run_all.bat` 의 `[2.5] gateway units / [3] hermes-dashboard / [4]
> register_cron_jobs` 단계는 Phase 8 polishing (commit `06319ce` /
> `2026-05-06-cleanup`) 으로 모두 제거됨. WSL 측 봇 디렉터리가 stale 이면
> `git pull` + `find . -name __pycache__ -exec rm -rf {} +` 후 재시작 필요.

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
@bot 안녕                              → master (claude/opus)
@bot @coder 피보나치 짜줘               → master prompt 에 coder SKILL.md inject (Phase 9)
@bot @coder 짜고 @reviewer 검토        → 두 agent 모두 inject (단일 호출, default)
                                        # MASTER_PARALLEL_AGENTS=true 시 fan-out 2 호출 → 집계 (Phase 10)
@bot /memo save 내일 회의 9시           → SqliteMemory 저장
@bot /memo list                        → 최근 메모 20개
@bot /hybrid-status                    → settings 요약
@bot /hybrid-budget                    → 일일 토큰 예산
@bot /kanban list                      → Kanban tasks
@bot 오늘 일정 알려줘                   → master → @researcher (캘린더 read MCP)
@bot 내일 14:00 회의 추가              → master → @devops (캘린더 write MCP, 사용자 확인 후)
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
│  │   └─ hermes_master.py       # claude/opus single-shot
│  ├─ integration/               # IntentRouter / PolicyGate / JobInventory / SessionImporter
│  ├─ agents/                    # AgentRegistry (`agents/` 스캔)
│  ├─ claude_adapter/            # Claude CLI subprocess (Hermes Master 단일 lane)
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
├─ tests/                        # pytest, 276 pass / 5 skip (Phase 10)
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
pytest -q                                 # 전체 (276 pass / 5 skip, Phase 10)
pytest tests/test_hermes_master.py        # 21 tests, master 분기 (Phase 9 inject + Phase 10 fan-out)
pytest tests/test_delegation.py           # 12 tests, OpenCodeAgentDelegator 병렬 + aggregate
pytest tests/test_integration_intent_router.py  # @handle 파싱
pytest tests/test_integration_policy_gate.py    # allowlist + budget
pytest tests/test_experience_logger.py    # JSONL contract + agent_handles
pytest tests/test_critic.py               # self_score lookup
pytest tests/test_agent_registry.py       # 17-agent 인덱싱
pytest tests/test_memory_search.py        # search + inject
pytest tests/test_preflight.py            # allowlist + Ollama warn + R6
```

---

## 12. 설계 불변식

1. **모든 LLM 호출은 Hermes Master (`claude` CLI / `opus`) entry point** 통과 — Phase 11 (2026-05-06).
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
- [x] **Diagram-aligned migration (2026-05-06)** — Hermes Master Orchestrator + Integration Layer 4 컴포넌트 + 레거시 dispatch 완전 삭제
- [x] **Phase 7** — 6 카테고리 / 17 sub-agent SKILL.md + `AgentRegistry` + `JobInventory.agents()` lookup
- [x] **Phase 8 (2026-05-06)** — 6 profile + 27 잡 + profile-local 10 SKILL.md 폐기. 17 agent 단일 구조. profile .env 변수 inventory 보존 (`_archive/profiles_envs/`, `docs/AGENT_ENV.md`).
- [x] **Phase 9 (2026-05-06)** — `@agent` 멘션 dispatch wiring. IntentRouter 가 `(?<![\w.])@(\w+)` 정규식으로 mention 추출 + AgentRegistry 검증, master `_compose_prompt` 가 SKILL.md frontmatter snippet 을 system prompt 에 inject. ExperienceLog 의 `agent_handles` 로 사용 통계 자동 누적.
- [x] **Phase 10 (2026-05-06)** — `ClaudeAgentDelegator` 진짜 병렬 실행. `master_parallel_agents=true` 옵트인 시 2+ handles → 각 agent 별 독립 claude 호출을 `asyncio.gather` + `Semaphore(max_concurrency)` 로 동시 실행 후 `aggregate_responses` 로 집계.
- [x] **Phase 11 Stage A (2026-05-06)** — heavy / c1 / heavy_session / `!heavy` prefix / smoke_heavy.py 일괄 폐기. master = single lane 전환 직전 정리. `TaskState.heavy` / `IntentRouter.heavy` 분기 / `Orchestrator.handle(heavy=...)` / `DiscordBot._HEAVY_PREFIX` / `Settings.c1_*` 4 dead 필드 / `ExperienceRecord.heavy` 모두 제거.
- [x] **Phase 11 Stage B (2026-05-06)** — opencode → Claude CLI (Max OAuth) master swap. Settings master_* 통합 (master_cli_path/backend/model/timeout). `OpenCodeAgentDelegator` → `ClaudeAgentDelegator`. src/opencode_adapter/ 통째 삭제.
- [ ] **Phase 12** — Slack gateway, Discord 슬래시 명령 확장

---

## 14. 문제 해결

### "claude: command not found" (WSL)
```bash
wsl -d Ubuntu -- bash -lc "which claude"
# 경로 다르면 .env 의 MASTER_CLI_PATH 수정
# 인증 필요 시: opencode auth login
```

### "Job Factory v2 init failed" / "ModuleNotFoundError" 류 봇 응답
**원인**: 봇이 stale 빌드 (Phase 8 이전 코드) 로 실행 중. `src/job_factory/`,
`src/hermes_adapter/` 등 폐기된 모듈을 import 시도 → 옛 fallback 메시지 노출.

**복구**:
```bash
# 1) 봇 종료 (WSL or Windows 어디서 띄우든)
systemctl --user stop hermes-gateway   # WSL 측 / 또는 taskkill /im python.exe
# 2) 코드 동기화
cd <봇-cwd> && git fetch && git reset --hard origin/main
# 3) stale .pyc 일괄 삭제 (중요)
find . -name __pycache__ -type d -exec rm -rf {} + 2>/dev/null
find . -name "*.pyc" -delete
# 4) 가상환경 재설치
.venv/bin/pip install -e . --upgrade
# 5) 부팅 검증
.venv/bin/python -c "from src.orchestrator.orchestrator import Orchestrator; from src.config import Settings; Orchestrator(Settings()); print('OK')"
# 6) 재시작
.\run_all.bat   # Windows / 또는 systemctl --user start <unit>
```

### Discord bot 이 메시지에 반응 안 함
- `DISCORD_ALLOWED_USER_IDS` 에 본인 ID 포함 확인
- Discord Developer Portal 에서 **MESSAGE CONTENT INTENT** 활성

### ExperienceLog 가 안 쌓인다
- `.env` 의 `EXPERIENCE_LOG_ENABLED=true` 확인
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

### Phase 8 마이그레이션 — profile/.env 통합 (이미 완료된 환경은 무시)
```bash
# 기존 profile/.env 의 실 값을 root .env 로 옮김 (P8 plan 의 §3.4)
# (이미 profiles/ 가 git rm 됐다면 _archive/profiles_envs/{profile}/.env.template 참조)
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
