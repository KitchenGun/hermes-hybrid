# calendar_ops 프로파일 런북 (Hermes-native)

> ⚠️ **LEGACY: pre-Phase-8 reference (deprecated 2026-05-06)** ⚠️
>
> 이 런북은 폐기된 calendar_ops profile + CalendarSkill 의 운영 방법을
> 설명한다. Phase 8 에서 calendar_ops profile 과 CalendarSkill 모두 폐기.
> 캘린더 read/CRUD 는 master 가 @researcher / @devops 에 위임하는 구조로
> 전환됐다. 현행 환경변수는 [`AGENT_ENV.md`](AGENT_ENV.md) 의
> `GOOGLE_OAUTH_CREDENTIALS` / `GOOGLE_CALENDAR_MCP_TOKEN_PATH` /
> `GOOGLE_CALENDAR_ID` 항목 참조.

**대상 독자**: hermes-hybrid를 새 머신에 배포하면서 CalendarSkill을 실제로 작동시키려는 사람.

**전제**: 리포지토리 코드는 이미 `CALENDAR_SKILL_ENABLED=true` + `HermesAdapter`의 `-p <profile>` / `-s <skill>` 지원을 포함하고 있음 (커밋 `982fa2f`). 그걸 딛고 **프로파일 쪽**에서 추가로 필요한 4가지를 이 문서가 설명.

**한 줄 요약**: `bash scripts/provision_calendar_ops_native.sh` 로 전부 자동화돼 있음. 아래는 그 스크립트가 왜 이렇게 생겼는지에 대한 근거.

---

## 전체 데이터 흐름

```
Discord 메시지
  └─ Orchestrator.handle()
       └─ SkillRegistry.match()  ← CalendarSkill 정규식 히트 (일정/캘린더/calendar/...)
            └─ orch.hermes.run(profile="calendar_ops",
                              preload_skills=["productivity/google-workspace"])
                 └─ 서브프로세스: hermes -p calendar_ops chat -q "..." -Q -s productivity/google-workspace
                      └─ gpt-4o-mini (프로파일 config.yaml 기준)
                           └─ SKILL.md 읽고 bash 툴로 `gapi calendar list` 실행
                                └─ google_api.py → Google Calendar API → 실제 이벤트
```

L2/L3/C1 라우터는 전혀 안 탐. `handled_by=skill:calendar`, `cloud_calls=0`, OpenAI TPM 소비 없음.

---

## 발견한 4가지 gotcha

배포 중 마주친 네 가지 함정. 프로비저닝 스크립트가 각각에 대해 수정을 적용함.

### 1. OAuth 토큰 위치

**증상**: Hermes가 `"현재 Google Calendar에 로그인되어 있지 않아…"` 라고 답함. 실제로는 `~/.hermes/google_token.json` 에 유효한 토큰이 있음.

**원인**: `-p calendar_ops` 플래그는 Hermes의 `HERMES_HOME` 환경변수를 `~/.hermes/profiles/calendar_ops/` 로 프로파일-스코프로 리라이트함. 그러면 `google-workspace` 스킬의 `TOKEN_PATH = HERMES_HOME / "google_token.json"` 은 프로파일 디렉터리를 뒤지지 글로벌 `~/.hermes/` 를 뒤지지 않음.

**수정**: 토큰 2개 파일을 프로파일 디렉터리로 복사:
```bash
cp ~/.hermes/google_token.json          ~/.hermes/profiles/calendar_ops/
cp ~/.hermes/google_client_secret.json  ~/.hermes/profiles/calendar_ops/
```

### 2. `hermes_constants.py` import 실패

**증상**: 프로파일 내의 `setup.py --check` 실행 시 `ModuleNotFoundError: No module named 'hermes_constants'`.

**원인**: 스킬 setup.py 맨 위에 다음 fallback이 있음:
```python
try:
    from hermes_constants import display_hermes_home, get_hermes_home
except ModuleNotFoundError:
    HERMES_AGENT_ROOT = Path(__file__).resolve().parents[4]
    if HERMES_AGENT_ROOT.exists():
        sys.path.insert(0, str(HERMES_AGENT_ROOT))
    from hermes_constants import ...
```

프로파일-로컬 경로에서 `parents[4]` 는 `~/.hermes/profiles/calendar_ops/` (프로파일 루트) 를 가리킴 — 거기엔 `hermes_constants.py` 가 없음. 글로벌 스킬 경로에서는 `parents[4]` 가 `~/` 이고, hermes-agent 경로에서는 `~/.hermes/hermes-agent/` 라 그건 있음.

**수정**: `hermes_constants.py` 를 프로파일 루트에 심볼릭 링크:
```bash
ln -sf ~/.hermes/hermes-agent/hermes_constants.py ~/.hermes/profiles/calendar_ops/hermes_constants.py
```

### 3. Hermes bash 툴이 Python user-site를 제거함 + `$HOME` 리라이트

**증상**: 모델이 `python3 google_api.py calendar list` 를 bash 툴로 실행하면 `ModuleNotFoundError: No module named 'googleapiclient'`. 같은 명령을 일반 WSL 셸에서는 문제없이 실행됨.

**원인 1 — PYTHONNOUSERSITE**: Hermes의 bash 툴은 `python3` 를 샌드박스 환경변수로 실행함. 보통 사용자는 `~/.local/lib/python3.12/site-packages` 에 googleapiclient 를 pip install --user 했을 텐데, 이 위치는 기본적으로 파이썬이 `sys.path` 에 자동 포함하는 user-site 경로. Hermes bash 툴은 그 포함을 차단하는 듯 (정확한 메커니즘은 `PYTHONNOUSERSITE=1` 설정 추정).

**원인 2 — $HOME 리라이트**: 동일 bash 툴에서 `$HOME` 변수가 `/home/kang/.hermes/profiles/calendar_ops` 로 재설정돼 있음. 세션 덤프에서 확인:
```
"File not found: /home/kang/.hermes/profiles/calendar_ops/home/.hermes/skills/..."
```
→ `$HOME/.hermes/skills/...` 를 평가한 결과가 이상하게 복제된 경로. `$HOME` 가 원래 유저 홈이 아님.

**원인 3 — shell 변수 persistence 없음**: SKILL.md 의 `GAPI="python $HOME/..."` 같은 shorthand 스타일은 Hermes bash 툴이 매 호출마다 새 서브셸을 spawn 하기 때문에 **턴 사이 변수가 증발**함. 모델이 turn N 에서 `GAPI=...` 를 정의해도 turn N+1 에서는 `GAPI: command not found`.

**수정**: 3개 원인을 한 번에 우회하는 **절대경로 wrapper 스크립트** 를 설치:

```bash
# /home/kang/.hermes/profiles/calendar_ops/gapi
#!/bin/bash
export PYTHONPATH="/home/kang/.local/lib/python3.12/site-packages${PYTHONPATH:+:${PYTHONPATH}}"
exec python3 "/home/kang/.hermes/profiles/calendar_ops/skills/productivity/google-workspace/scripts/google_api.py" "$@"
```

하드코드된 경로 때문에 `$HOME` 이 뭐든 상관없고, PYTHONPATH 를 매 호출 세팅하므로 user-site 증발 문제도 해결. 모델은 그냥 `/home/kang/.hermes/profiles/calendar_ops/gapi calendar list --max 10` 한 줄만 치면 됨 — 변수 정의 필요 없음.

SKILL.md 의 해당 Usage 블록도 프로비저닝 시 이 wrapper 경로로 교체됨.

### 4. 작은 모델이 엉뚱한 도구를 고름

**증상**: `browser: navigate calendar.google.com` 을 10번 반복 호출. 스킬은 로드됐는데 모델이 아예 안 씀.

**원인**: gpt-4o-mini 가 프로파일에 등록된 28개 툴셋 중 `browser` 에 먼저 손이 감 — "calendar" 라는 단어 보고 반사적으로 "브라우저로 그 사이트 가보자" 판단. SKILL.md 의 Usage 섹션에 있는 `$GAPI calendar list` 는 무시.

**수정**: 프로파일 단위로 방해되는 툴셋을 비활성화:
```bash
HERMES_HOME=~/.hermes/profiles/calendar_ops hermes tools disable browser web --platform cli
```

다른 프로파일에는 영향 없음 (툴 enable/disable 은 프로파일 스코프).

---

## 검증 커맨드

프로비저닝 후 3단계로 확인:

**1) Hermes CLI 레벨** — 순수 Hermes 서브프로세스만 테스트:
```bash
hermes -p calendar_ops chat -q '이번주 일정 알려줘' -Q \
  --max-turns 6 -s productivity/google-workspace --yolo
```
기대 출력: `🌅 이번주 (…) • 4월 22일 (토) — 개인프로젝트 회의 …` 같은 실제 이벤트 리스트.

**2) 오케스트레이터 CLI 레벨** — hermes-hybrid 래퍼 포함:
```bash
PYTHONIOENCODING=utf-8 python -m src.orchestrator.cli "이번주 일정 알려줘" --user cli-kang
```
기대 로그:
```
task.end  handled_by=skill:calendar  tier=L2  route=local  cloud_calls=0  latency_ms=~30000
```

**3) Discord 레벨** — 실제 봇:
```
@Agent-Hermes 이번주 일정 알려줘
```
기대 응답: CLI 결과와 동일한 일정 포맷.

---

## 주요 함정 및 재발 방지

### 봇 프로세스 재시작 잊음
**증상**: `.env` 에 `CALENDAR_SKILL_ENABLED=true` 를 더했는데 봇이 일반 L2/L3 응답만 보냄.

**원인**: `pydantic-settings` 의 `BaseSettings` 는 **프로세스 시작 시 한 번만** `.env` 를 읽음. 봇이 플래그 반영 전에 떠 있었다면 플래그 없는 상태를 고수함.

**처방**:
```powershell
Get-CimInstance Win32_Process -Filter "name='python.exe'" | Select ProcessId, CommandLine
Stop-Process -Id <PID> -Force
Start-Process python -ArgumentList "scripts\run_bot.py" -WorkingDirectory "E:\hermes-hybrid"
```

### 프로파일 `config.yaml` 의 model 이 너무 약함
`gpt-4o-mini` 는 gotcha #4 가 필요할 정도로 도구 선택 능력이 약함. 여유가 있으면 `gpt-4o` 로 승격 (TPM 여유 확인). 프로파일 `config.yaml` 의 `model.default` 만 바꾸면 됨.

### 프로파일 `config.yaml` 의 providers 가 비어있음
`ollama-local` 같은 custom_provider 는 `--provider` argparse choice 에 안 들어있음 (`auto/openrouter/nous/…` 만 허용). `HermesAdapter` 는 `model`/`provider` 가 빈 문자열일 때 CLI 플래그를 생략하도록 설계돼 있으므로, `.env` 에서 `CALENDAR_SKILL_MODEL=` 와 `CALENDAR_SKILL_PROVIDER=` 둘 다 **공백으로** 두고, 프로파일 `config.yaml` 이 선택을 주도하게 함.

---

## 리포지토리 구조 참고

리포에 있는 `profiles/calendar_ops/config.yaml` 은 Ollama + MCP 서버 기반의 **별개 아키텍처 초안** — 현재 작동하는 Hermes-native 프로파일(`~/.hermes/profiles/calendar_ops/`) 과는 다름. 혼동 주의.

현재 실제로 사용하는 경로:
- 코드: 이 리포지토리 (`src/skills/calendar.py`, `src/hermes_adapter/adapter.py`)
- 프로파일: `~/.hermes/profiles/calendar_ops/` (WSL 안, git 추적 밖)
- 프로비저닝: `scripts/provision_calendar_ops_native.sh` (이 리포지토리 안)
- OAuth: `~/.hermes/profiles/calendar_ops/google_token.json` (수동 / 프로비저닝 스크립트가 복사)
