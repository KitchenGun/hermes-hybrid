# hermes-hybrid

**NousResearch Hermes Agent를 heavy-lifting 런타임으로 감싸는 하이브리드 LLM 오케스트레이터.**
결정적 Rule Layer + 경량 Router + Validator(retry-budget) + Discord/MCP Gateway를 조합해
L2(로컬 Ollama) → C1(GPT-4o) → C2(Claude Code CLI, 승인형) tier를 안전하게 라우팅한다.

---

## 1. 구조

### 전체 아키텍처

```
Discord / MCP Client
        │
        ▼
   Gateway  ──►  Entry  ──►  Policy Gate  ──►  Router (L1)
                                                    │
                             ┌──────────────────────┼──────────────────────┐
                             ▼                      ▼                      ▼
                     HermesAdapter ─► L2      HermesAdapter ─► C1    C2 Heavy Path
                     (Ollama 7B/14B/32B)      (gpt-4o)         (wsl claude -p, 승인형)
                             │                      │                      │
                             └────────────┬─────────┴──────────────────────┘
                                          ▼
                                  Validator / Guardrail
                                  (accept | degrade | bump | suggest_heavy)
                                          │
                                          ▼
                          Repository / Ledger (SQLite, append-only)
                                          │
                                          ▼
                                  Discord webhook / 응답
```

### Tier 매핑

| Tier | 모델 | 용도 | 비용 | 자동 |
|---|---|---|---|---|
| **L2** | `qwen2.5:14b-instruct` (Ollama) | 일반 응답 | $0 (로컬) | ✓ |
| **L3** | `qwen2.5-coder:32b-instruct` (Ollama) | 코드 작업 | $0 (로컬) | ✓ |
| **C1** | `gpt-4o` (OpenAI) | 복잡 추론, L2 실패 시 bump | 종량제 | ✓ |
| **C2** | `sonnet` (Claude Code CLI via WSL) | 고난도, 승인형 | Max 구독 | ✗ (`!heavy` + 승인) |

Router는 L1 규칙(`qwen2.5:7b`)으로 **provider pin만 제안**하고, Policy Gate가 예산·승인을 판정한다.

---

## 2. 요구사항

### 시스템
- **OS**: Windows 10/11 + WSL2 Ubuntu (Hermes CLI는 WSL2에 설치)
- **Python**: 3.11 이상
- **Ollama**: Windows 네이티브, 모델 3종 pull 완료
- **Node.js** (선택): MCP 서버 실행용

### 외부 CLI
- `hermes` — NousResearch Hermes Agent CLI (WSL2 내부, `~/.local/bin/hermes`)
- `claude` — Claude Code CLI (WSL2 내부, `~/.local/bin/claude`)
- `ollama` — Windows 네이티브

### API 키
- **Discord Bot Token** — 필수
- **OpenAI API Key** — C1 tier 사용 시 필수
- **Anthropic API Key** — 비워두기 (C2는 Claude Code CLI 우회)
- **Google OAuth (선택)** — calendar_ops 프로파일 사용 시

### 로컬 모델 (Ollama)
```bash
ollama pull qwen2.5:7b-instruct          # Router (경량 라우팅)
ollama pull qwen2.5:14b-instruct         # L2 Work
ollama pull qwen2.5-coder:32b-instruct   # L3 Worker (코드)
```
총 디스크 사용량 ~33GB. 32B 모델은 VRAM 20GB+ 권장.

---

## 3. 디렉토리 레이아웃

```
hermes-hybrid/
├── README.md                         이 파일
├── pyproject.toml                    패키지 메타데이터
├── .env                              시크릿 (gitignore)
├── .env.example                      환경변수 템플릿
├── .gitignore
├── run_all.bat                       Windows 원클릭 부팅
├── start.bat / start.ps1             Discord bot 기동
│
├── docs/
│   └── architecture.md               설계 스펙
│
├── src/
│   ├── config.py                     pydantic-settings loader
│   ├── preflight.py                  기동 전 체크
│   ├── gateway/                      Discord bot
│   ├── mcp/                          MCP server
│   ├── router/                       L1 규칙 + 경량 분류
│   ├── orchestrator/                 흐름 제어, bump, heavy session
│   ├── hermes_adapter/               Hermes CLI subprocess wrapper
│   ├── claude_adapter/               Claude Code CLI wrapper
│   ├── llm/                          Ollama/OpenAI/Anthropic client
│   ├── validator/                    retry budget + escalation
│   ├── memory/                       SQLite session memory
│   ├── skills/                       hybrid skills (budget, memo, status)
│   ├── state/                        TaskState + Repository
│   └── obs/                          logging
│
├── scripts/
│   ├── run_bot.py                    Discord bot entry
│   ├── e2e_smoke.py                  end-to-end smoke test
│   ├── smoke_heavy.py                C2 경로 단독 테스트
│   ├── bench_latency.py              tier별 latency 측정
│   └── bootstrap_profile.sh          프로파일 WSL 연결
│
├── tests/                            pytest suites
│
├── profiles/                         Hermes 프로파일 (잡 + skill 번들)
│   └── calendar_ops/                 Google Calendar 관리
│       ├── config.yaml               프로파일 설정
│       ├── SOUL.md                   페르소나
│       ├── _shared/                  공통 규칙 (intent schema, safety)
│       ├── skills/                   google_calendar + discord_notify
│       ├── cron/read/                자동 브리핑 (4개 잡)
│       ├── cron/analyze/             주기 분석 (3개 잡)
│       ├── on_demand/                대화형 CRUD (4개 잡)
│       └── watchers/                 이벤트 감시 (2개 잡)
│
├── data/                             SQLite DB (gitignore)
└── logs/                             런타임 로그
```

---

## 4. 설치

### 4.1 리포 clone + Python 가상환경

```powershell
cd E:\
git clone <your-repo-url> hermes-hybrid
cd hermes-hybrid

python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -e ".[dev,ollama]"
```

### 4.2 `.env` 작성

```powershell
copy .env.example .env
notepad .env
```

주요 항목은 §5 참조. 최소 설정:
```env
DISCORD_BOT_TOKEN=<your-token>
OPENAI_API_KEY=sk-<your-key>
DISCORD_ALLOWED_USER_IDS=<your-discord-user-id>
OLLAMA_ENABLED=true
```

### 4.3 WSL2 + Hermes CLI 확인

```powershell
wsl -d Ubuntu -- bash -lc "hermes --version"
wsl -d Ubuntu -- bash -lc "claude --version"
```
둘 다 버전이 출력되어야 한다.

### 4.4 Ollama 모델 pull

```powershell
ollama pull qwen2.5:7b-instruct
ollama pull qwen2.5:14b-instruct
ollama pull qwen2.5-coder:32b-instruct
ollama list   # 3개 모두 표시 확인
```

### 4.5 프리플라이트

```powershell
python -m src.preflight
```
모든 항목이 `OK`로 표시되면 준비 완료.

---

## 5. 환경 변수 레퍼런스

### 5.1 필수

| 변수 | 설명 | 예시 |
|---|---|---|
| `DISCORD_BOT_TOKEN` | Discord 봇 토큰 | `MT...` |
| `DISCORD_ALLOWED_USER_IDS` | 허용 사용자 Discord ID (CSV) | `123456789012345678` |
| `OPENAI_API_KEY` | C1 tier 사용 시 필수 | `sk-proj-...` |
| `OPENAI_MODEL` | C1 모델명 | `gpt-4o` |

### 5.2 로컬 모델 (Ollama)

| 변수 | 기본값 | 설명 |
|---|---|---|
| `OLLAMA_ENABLED` | `true` | L2/L3 로컬 실행 여부 |
| `OLLAMA_BASE_URL` | `http://localhost:11434` | Ollama API |
| `OLLAMA_ROUTER_MODEL` | `qwen2.5:7b-instruct` | Router 판정용 |
| `OLLAMA_WORK_MODEL` | `qwen2.5:14b-instruct` | L2 Work |
| `OLLAMA_WORKER_MODEL` | `qwen2.5-coder:32b-instruct` | L3 Worker (코드) |

Ollama 비활성 시 `OPENAI_MODEL_LOCAL_SURROGATE` / `OPENAI_MODEL_WORKER_SURROGATE`로 대체.

### 5.3 Hermes CLI (WSL 연동)

| 변수 | 기본값 | 설명 |
|---|---|---|
| `HERMES_CLI_BACKEND` | `wsl_subprocess` | `wsl_subprocess` \| `local_subprocess` \| `mcp` |
| `HERMES_WSL_DISTRO` | `Ubuntu` | WSL 배포판 이름 |
| `HERMES_CLI_PATH` | `/home/kang/.local/bin/hermes` | WSL 내부 CLI 경로 |
| `HERMES_HOME` | `/home/kang/.hermes` | Hermes 홈 |
| `HERMES_TIMEOUT_MS` | `180000` | 실행 timeout (ms) |
| `HERMES_MAX_TURNS` | `20` | plan/act/observe/reflect 최대 턴 |
| `HERMES_CONCURRENCY` | `3` | 동시 실행 수 |

### 5.4 C2 Heavy Path (Claude Code CLI)

| 변수 | 기본값 | 설명 |
|---|---|---|
| `CLAUDE_CODE_CLI_PATH` | `/home/kang/.local/bin/claude` | WSL 내부 CLI |
| `CLAUDE_CODE_MODEL` | `sonnet` | `sonnet` \| `opus` \| 전체 모델명 |
| `CLAUDE_CODE_TIMEOUT_MS` | `300000` | 5분 (heavy 작업) |
| `CLAUDE_CODE_CONCURRENCY` | `1` | Max 시간당 한도 보호 |
| `CLAUDE_CALL_BUDGET_SESSION` | — | 세션당 C2 호출 상한 |

### 5.5 Feature Flags

| 변수 | 기본값 | 설명 |
|---|---|---|
| `USE_HERMES_FOR_LOCAL` | `false` | L2/L3을 Hermes 경유로 실행 (Phase 2) |
| `USE_HERMES_FOR_C1` | `false` | C1을 Hermes 경유로 실행 |
| `USE_HERMES_FOR_HEAVY` | — | C2를 Hermes 경유 (일반적으로 false) |
| `USE_HERMES_EVERYWHERE` | `false` | 전체를 Hermes로 강제 |
| `TRUST_HERMES_REFLECTION` | — | Hermes 자체 reflect 결과 신뢰 여부 |

### 5.6 Validator / Router

| 변수 | 기본값 | 설명 |
|---|---|---|
| `ROUTER_CONF_ACCEPT` | — | Router confidence 수용 임계 |
| `ROUTER_CONF_TIER_UP` | — | tier-up 트리거 confidence |
| `RETRY_BUDGET_DEFAULT` | — | 동일 tier 재시도 예산 |
| `SAME_TIER_RETRY_MAX` | — | 동일 tier 최대 재시도 |
| `TIER_UP_RETRY_MAX` | — | bump 최대 횟수 |
| `CLOUD_ESCALATION_MAX` | — | 클라우드 escalation 상한 |

### 5.7 예산 / 한도

| 변수 | 설명 |
|---|---|
| `CLOUD_TOKEN_BUDGET_DAILY` | 일일 클라우드 토큰 상한 |
| `CLOUD_TOKEN_BUDGET_SESSION` | 세션당 클라우드 토큰 상한 |
| `PER_USER_IN_FLIGHT_MAX` | 사용자당 동시 요청 제한 |

### 5.8 관찰성 / 상태

| 변수 | 기본값 | 설명 |
|---|---|---|
| `STATE_DB_PATH` | `./data/hermes_hybrid.db` | SQLite DB 경로 |
| `LOG_LEVEL` | `INFO` | `DEBUG` \| `INFO` \| `WARNING` |
| `LOG_JSON` | `false` | JSON 구조화 로그 |

---

## 6. 실행 방법

### 6.1 원클릭 기동 (Windows, 권장)

```powershell
.\run_all.bat
```

수행 순서:
1. Ollama 서버 기동 (이미 떠 있으면 스킵)
2. WSL2 warm-up (`hermes`/`claude` 접근 가능한지 확인)
3. Discord bot 기동 (`scripts/run_bot.py`)

로그는 같은 창에 출력. `Ctrl+C`로 종료.

### 6.2 단계별 기동 (디버그용)

```powershell
# 1. Ollama
ollama serve

# 2. Discord bot (별도 터미널)
.\.venv\Scripts\Activate.ps1
python scripts\run_bot.py
```

### 6.3 CLI Smoke 테스트 (Discord 없이)

```powershell
python -m src.orchestrator.cli "/ping"
python -m src.orchestrator.cli "안녕"
python -m src.orchestrator.cli "파이썬으로 피보나치 함수 짜줘"
python -m src.orchestrator.cli "!heavy 복잡한 분석 요청"
```

### 6.4 E2E Smoke

```powershell
python scripts\e2e_smoke.py
python scripts\smoke_heavy.py          # C2 경로만
python scripts\bench_latency.py        # tier별 응답 시간
```

### 6.5 MCP 서버로 기동

```powershell
python -m src.mcp.server
```
Hermes 측 MCP client 또는 Claude Desktop에서 연결.

---

## 7. Discord에서 사용

봇이 기동되면 허용된 사용자 ID에서 멘션으로 호출:

```
@Agent-Hermes 안녕
→ L2 (qwen2.5:14b) 자동 응답

@Agent-Hermes 파이썬 코드 리뷰해줘 <snippet>
→ Router가 코드성 판단 → L3 (qwen2.5-coder:32b)

@Agent-Hermes !heavy 복잡한 아키텍처 리팩터링 해줘
→ Policy Gate: "C2 실행 승인?" 버튼 메시지
→ 사용자 승인 → wsl claude -p 실행
→ 결과 + 비용 기록
```

---

## 8. Hermes 프로파일 사용 (calendar_ops 예시)

> **💡 CalendarSkill 통해 Discord에서 `이번주 일정 알려줘` 를 동작시키려면**
> Hermes-native 프로파일(`~/.hermes/profiles/calendar_ops/`) 쪽에도 4가지 수정이
> 필요합니다 (OAuth 배치, 심볼릭 링크, gapi wrapper, 툴셋 비활성화).
> 한 번에 자동화돼 있음: `bash scripts/provision_calendar_ops_native.sh`
> 근거와 트러블슈팅은 [`docs/calendar_ops_runbook.md`](docs/calendar_ops_runbook.md) 참조.

### 8.1 부트스트랩 (WSL)

```bash
wsl bash /mnt/e/hermes-hybrid/scripts/bootstrap_profile.sh calendar_ops
```

### 8.2 Discord Webhook 발급

대상 채널 설정 → 연동 → 웹후크 → **새 웹후크** → URL 복사
→ `profiles/calendar_ops/.env` 의 `DISCORD_BRIEFING_WEBHOOK_URL` 에 기입

### 8.3 Google OAuth 인증

```bash
wsl hermes -p calendar_ops auth google-calendar
```
또는 Google Cloud Console에서 OAuth 클라이언트 생성 후 `auth.json` 수동 배치.

### 8.4 동작 확인

```bash
wsl hermes -p calendar_ops chat -q "오늘 일정 알려줘"
```

### 8.5 크론 잡 등록

```bash
# 매일 아침 브리핑
wsl hermes -p calendar_ops cron create \
  --name morning_briefing \
  --schedule "0 8 * * *" \
  --skill google_calendar \
  --skill discord_notify \
  --prompt-file /mnt/e/hermes-hybrid/profiles/calendar_ops/cron/read/morning_briefing.yaml

wsl hermes -p calendar_ops cron list
wsl hermes -p calendar_ops cron status
```

자세한 잡 목록은 [`profiles/calendar_ops/README.md`](profiles/calendar_ops/README.md) 참조.

---

## 9. 테스트

```powershell
pytest -q                              # 전체
pytest tests/test_router.py -v         # 특정 모듈
pytest -k "heavy" -v                   # 키워드 매칭
pytest --tb=short -m "not slow"        # 느린 테스트 제외
```

커버리지:
```powershell
pytest --cov=src --cov-report=html
start htmlcov\index.html
```

---

## 10. 설계 불변식 (절대 위반 금지)

1. **Orchestrator는 tool을 직접 실행하지 않는다**. 실행은 Hermes가.
2. **Router는** `{route, confidence, reason, requires_planning}` 만 반환.
3. **LLM은 실행 엔진**, Hermes가 어느 LLM을 쓸지 결정.
4. **C2(Claude)는 최후 수단**, 세션당 예산이 있으며 사용자 승인 필수.
5. **Rule Layer는 확정된 패턴만 응답**, LLM fallback 금지.
6. **Validator는 판정만**. LLM 재호출이나 retry 루프를 돌리지 않는다.
7. **모든 단계 이벤트는 append-only ledger에 기록**.
8. **Router는 `claude-code`를 자동 선택할 수 없다**.

자세한 설계 근거는 [`docs/architecture.md`](docs/architecture.md) 참조.

---

## 11. 문제 해결

### "hermes: command not found" (WSL)
```bash
wsl -d Ubuntu -- bash -lc "which hermes"
# 경로가 다르면 .env의 HERMES_CLI_PATH 수정
```

### Ollama 응답 없음
```powershell
ollama list                             # 설치 확인
curl http://localhost:11434/api/tags    # 서버 응답 확인
```

### Discord bot이 메시지에 반응 안 함
- `DISCORD_ALLOWED_USER_IDS`에 본인 ID 포함 여부 확인
- 봇 권한: **MESSAGE CONTENT INTENT** 활성 필요 (Discord Developer Portal)

### C2(`!heavy`) 승인이 안 됨
- `CLAUDE_CODE_CLI_PATH`가 WSL 내부 경로인지 확인
- `wsl claude --version` 직접 실행 확인
- Max 시간당 한도 도달 가능성: 1시간 대기 후 재시도

### 프로파일 symlink 깨짐
```bash
ls -la ~/.hermes-calendar_ops
# 깨졌으면 bootstrap 재실행
wsl bash /mnt/e/hermes-hybrid/scripts/bootstrap_profile.sh calendar_ops
```

### SQLite DB 잠금
```powershell
# bot 종료 후
del data\hermes_hybrid.db-wal
del data\hermes_hybrid.db-shm
```

---

## 12. 로드맵

- [x] **Phase 1** — direct LLM client + shadow Hermes
- [ ] **Phase 2** — `USE_HERMES_FOR_LOCAL=true`, Policy Gate + Redis
- [ ] **Phase 3** — C2 CLI 3-stage kill + approval token + full Ledger
- [ ] **Phase 4** — Observability (OTel → Prometheus/Grafana)

---

## 13. 기여

- 브랜치 전략: `feature/<area>` → PR → `main`
- 커밋 메시지: Conventional Commits 권장 (`feat:`, `fix:`, `refactor:` 등)
- 시크릿은 절대 커밋 금지 — 커밋 전 `git status`로 `.env`, `auth.json` 제외 확인

---

## 14. 라이선스

미정 (추후 추가 예정)

---

## 참고 자료

- **Hermes Agent (NousResearch)**: https://hermes-agent.nousresearch.com/docs
- **설계 스펙**: [`docs/architecture.md`](docs/architecture.md)
- **calendar_ops 프로파일**: [`profiles/calendar_ops/README.md`](profiles/calendar_ops/README.md)
