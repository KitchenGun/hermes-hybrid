# calendar_ops — Google Calendar 관리 프로파일

Hermes Agent 프로파일로, Google Calendar CRUD + Discord 채널 브리핑을 담당한다.

## 역할

- **Read** (자동 실행, L2 로컬): 매일 아침/저녁 브리핑, 주간 미리보기, 회의 15분 전 알림
- **Write** (사용자 승인 필수, L2→C1): 일정 추가/수정/삭제
- **Analyze** (주기적, L2→C1): 주간 회고, 집중 시간 리포트, 월간 패턴 분석
- **Watcher** (이벤트 기반): 새 초대 수신, 충돌 감지

## 제약

- **C2(Claude Code CLI) 사용 금지**: `heavy_policy: never`
- **Tier 상한: C1(gpt-4o)**: 분석 잡에서만 bump 허용
- **일일 예산: $0.20**: Read 잡은 비용 0, Analyze 잡이 소량 소비
- **쓰기는 반드시 사용자 확인**: 자동 실행 금지

## 디렉토리

```
calendar_ops/
├── config.yaml                  # 프로파일 메인 설정 (공식 Hermes 스키마)
├── SOUL.md                      # 에이전트 페르소나
├── .env.example                 # 환경변수 템플릿 (커밋 O)
├── .env                         # 실제 시크릿 (커밋 X)
├── .gitignore
├── auth.json                    # Google OAuth (커밋 X)
│
├── _shared/
│   ├── intent_schema.json       # 쓰기 잡 입력 JSON schema
│   ├── persona.md               # 공통 응답 톤·포맷
│   └── safety_rules.md          # 쓰기 안전 규칙
│
├── skills/
│   ├── google_calendar/
│   │   └── SKILL.md             # MCP wrapper
│   └── discord_notify/
│       ├── SKILL.md
│       └── scripts/
│           └── post_webhook.py  # webhook POST 헬퍼
│
├── cron/
│   ├── read/
│   │   ├── morning_briefing.yaml    (매일 08:00)
│   │   ├── weekly_preview.yaml      (일요일 20:00)
│   │   ├── pre_meeting_reminder.yaml (15분마다)
│   │   └── daily_wrap.yaml          (매일 22:00)
│   └── analyze/
│       ├── weekly_retrospective.yaml (금요일 19:00)
│       ├── focus_time_report.yaml    (월요일 09:00)
│       └── monthly_pattern.yaml      (매월 1일 09:00)
│
├── on_demand/
│   ├── add_event.yaml           (키워드: "추가해줘", "잡아줘")
│   ├── update_event.yaml        (키워드: "바꿔줘", "시간 옮겨")
│   ├── delete_event.yaml        (키워드: "삭제해줘", "취소")
│   └── quick_block.yaml         (키워드: "집중", "블록")
│
├── watchers/
│   ├── new_invitation_handler.yaml
│   └── conflict_detector.yaml
│
├── memories/                    # 런타임 (gitignore)
├── sessions/                    # 런타임 (gitignore)
└── logs/                        # 런타임 (gitignore)
```

## 초기 설정

### 1. 디렉토리 연결 (WSL)

```bash
wsl bash /mnt/e/hermes-hybrid/scripts/bootstrap_profile.sh calendar_ops
```

이 스크립트는:
- `~/.hermes-calendar_ops` → `/mnt/e/hermes-hybrid/profiles/calendar_ops` symlink 생성
- 런타임 디렉토리(`~/.hermes-calendar_ops-runtime`)를 WSL 로컬에 생성
- `.env` 파일 존재 확인 및 템플릿 복사

### 2. Discord Webhook 발급

대상 채널 → 연동 → 웹후크 → **새 웹후크** → URL 복사
→ `profiles/calendar_ops/.env`의 `DISCORD_BRIEFING_WEBHOOK_URL`에 기입

### 3. Google OAuth 인증

```bash
wsl hermes -p calendar_ops auth google-calendar
```

또는 Google Cloud Console에서 OAuth 클라이언트를 생성하여 `auth.json`을 직접 배치.

Scopes 필요:
- `https://www.googleapis.com/auth/calendar.readonly`
- `https://www.googleapis.com/auth/calendar.events`

### 4. 동작 확인

```bash
wsl hermes -p calendar_ops chat -q "오늘 일정 알려줘"
```

### 5. 크론 등록

각 cron 잡 YAML을 Hermes CLI로 등록:

```bash
wsl hermes -p calendar_ops cron create \
  --name morning_briefing \
  --schedule "0 8 * * *" \
  --skill google_calendar \
  --skill discord_notify \
  --prompt-file /mnt/e/hermes-hybrid/profiles/calendar_ops/cron/read/morning_briefing.yaml

wsl hermes -p calendar_ops cron list
```

## 잡 분류 요약

| 카테고리 | 트리거 | Tier | 확인 필요 | 재시도 |
|---|---|---|---|---|
| Read | cron | L2 | ✗ | 2회 |
| Write | on_demand | L2→C1 | ✓ | 0회 |
| Analyze | cron | L2→C1 | ✗ | 1회 |
| Watcher | event | L2 | ✗ | 0회 |

## 비용 예측

| 잡 | 빈도 | tier | 예상 일일 비용 |
|---|---|---|---|
| morning_briefing | 1/day | L2 | $0.00 |
| weekly_preview | 1/week | L2 | $0.00 |
| pre_meeting_reminder | 96/day | L2 | $0.00 |
| daily_wrap | 1/day | L2 | $0.00 |
| weekly_retrospective | 1/week | C1 | $0.01 |
| focus_time_report | 1/week | C1 | $0.01 |
| monthly_pattern | 1/month | C1 | $0.003 |
| add_event (평균) | 3/day | L2 | $0.00 |
| **합계 (일일)** | — | — | **≈ $0.003** |

월간 약 $0.10 ~ $0.20. `config.yaml`의 `budget.cap_usd_per_day: 0.20`으로 상한.

## 보안

- `.env`, `auth.json`은 **절대 커밋하지 않는다** (`.gitignore` 확인)
- OAuth refresh token 유출 시: Google Cloud Console → 해당 OAuth 클라이언트 **재발급**
- Discord webhook URL 유출 시: Discord 채널 설정 → 해당 webhook **재생성**

## 문제 해결

| 증상 | 원인 | 대응 |
|---|---|---|
| "재인증 필요" 메시지 | OAuth token 만료 | `hermes -p calendar_ops auth google-calendar` 재실행 |
| webhook 204 아닌 응답 | URL 오타 또는 채널 삭제 | `.env`의 URL 재확인 |
| 크론 잡이 실행 안 됨 | `hermes cron status` 가 not running | `hermes cron start` 로 스케줄러 기동 |
| 중복 알림 | `memories/reminder_sent_event_ids.json` 불일치 | 파일 삭제 후 재생성 대기 |
| 비용 초과 알림 | `budget.cap_usd_per_day` 접근 | Analyze 잡 일시 중단 (`hermes -p calendar_ops cron pause weekly_retrospective`) |
