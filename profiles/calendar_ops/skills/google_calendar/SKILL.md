---
name: google_calendar
description: Google Calendar CRUD via MCP server. Read/create/update/delete events with OAuth 2.0.
version: 1.0.0
platforms: [linux]
metadata:
  hermes:
    tags: [calendar, google, productivity]
    category: productivity
    requires_toolsets: [mcp]
    config:
      - key: calendar_id
        description: "Default Google Calendar ID"
        default: "primary"
        prompt: "조회/기록 대상 캘린더 ID (기본 primary)"
      - key: timezone
        description: "Display timezone"
        default: "Asia/Seoul"
    required_environment_variables:
      - name: GOOGLE_APPLICATION_CREDENTIALS
        prompt: "OAuth 자격증명 JSON 경로"
      - name: GOOGLE_CALENDAR_ID
        prompt: "기본 캘린더 ID"
      - name: TIMEZONE
        prompt: "표시 타임존"
---

# When to Use

- 사용자가 "일정", "캘린더", "schedule", "meeting", "회의", "약속" 등을 언급
- 브리핑/리마인더/회고 계열 자동 잡
- 일정 조회 결과를 다른 skill(예: discord_notify)에 넘겨야 할 때

## 사용하지 말아야 할 때

- 이메일·메신저 기반 일정 (이건 별도 skill 필요)
- 타인의 캘린더 조작 (권한 미부여)
- "일정 관리 팁" 같은 일반 상담 (캘린더 접근 불필요)

# Procedure

## 1. 조회 (read)

사용 MCP tool: `list_events`
```
input:
  calendar_id: string = "primary"
  time_min: ISO8601
  time_max: ISO8601
  max_results: int = 50
output:
  events: [{id, title, start, end, location, attendees, description}]
```

표시는 반드시 `TIMEZONE` 로컬 변환. 0건이면 "일정 없음" 명시.

## 2. 생성 (create)

사용 MCP tool: `create_event`

**반드시 확인 게이트 거칠 것** (`_shared/safety_rules.md` §1).

```
input (intent_schema.json 따름):
  calendar_id: string
  title: string
  start: ISO8601
  end: ISO8601 | duration_minutes 로부터 계산
  location?: string
  attendees?: [email]
  description?: string
output:
  event_id: string
  html_link: string
```

성공 시 event_id를 Ledger `task_events.payload`에 기록.
중복 감지: 같은 (title, start, end, calendar_id) 존재 시 기존 ID 반환, 생성 스킵.

## 3. 수정 (update)

사용 MCP tool: `update_event`

반드시 `event_id` 기반. 제목·시간 기반 매칭 금지.
변경 전/후 스냅샷을 Ledger에 기록 (rollback 근거).

## 4. 삭제 (delete)

사용 MCP tool: `delete_event`

삭제 전 이벤트 전체 내용을 Ledger에 스냅샷 저장 (`_shared/safety_rules.md` §5).
반복 이벤트의 경우 "이번 인스턴스만 / 앞으로 / 전체" 사용자 선택 필수.

## 5. 충돌 감지 (conflict_check)

사용 MCP tool: `list_events` (time_min/max로 범위 지정)

새 이벤트의 (start, end) 범위에 겹치는 기존 이벤트를 찾아 경고만 제시.
자동 수정 금지.

# Pitfalls

- **타임존 혼선**: Google Calendar API는 UTC 반환. 표시는 반드시 `TIMEZONE` 변환.
- **반복 이벤트**: `recurringEventId` 확인. 단일 인스턴스 수정과 전체 수정 구분.
- **All-day 이벤트**: `start.date` (date only) vs `start.dateTime` 구분. 시간 포함 여부로 판단.
- **OAuth 만료**: 401/403 수신 즉시 중단, 재시도 금지.
- **participants 이메일 PII**: 로그·Ledger에 마스킹 옵션 적용.
- **Rate Limit**: Google Calendar API 분당 600회 제한. 고빈도 잡(`pre_meeting_reminder`)은 캐시 활용.

# Verification

실행 후 다음을 확인:

1. Ledger `task_events`에 `google_calendar.<action>` 이벤트 기록됨
2. 쓰기 작업: `event_id`가 응답에 포함됨
3. 조회: 응답 `events` 배열 존재 (0건이어도 필드 자체는 존재)
4. 타임존: 모든 시각이 `TIMEZONE` 기준으로 표시됨
5. 실패 시: Ledger에 원인 코드(401/403/5xx) 기록됨

# References

- Google Calendar API: https://developers.google.com/calendar/api/v3/reference
- OAuth 2.0 scopes: `calendar.readonly`, `calendar.events`
- 이 스킬은 외부 MCP 서버(`@google/calendar-mcp`)를 wrapper합니다. MCP 서버가 없으면 로드 실패.
