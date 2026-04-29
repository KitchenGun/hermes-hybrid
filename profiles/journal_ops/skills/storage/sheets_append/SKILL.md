---
name: sheets_append
description: Append activity rows to a Google Sheet via Apps Script doPost webhook.
version: 1.0.0
platforms: [linux, macos]
metadata:
  hermes:
    tags: [storage, sheets, journal]
    category: storage
    requires_toolsets: [terminal]
    config:
      - key: timeout_sec
        description: "HTTP timeout per attempt (seconds)"
        default: 15
    required_environment_variables:
      - name: GOOGLE_SHEETS_WEBHOOK_URL
        description: "Apps Script web app URL (Deploy → 새 배포 → 웹앱 → /exec)"
      - name: JOURNAL_ALERT_WEBHOOK_URL
        description: "Optional Discord webhook for failure alerts (red embed). Empty = no alert."
        optional: true
---

# When to Use
- 사용자 활동 발화를 24-필드 JSON으로 파싱한 후 시트에 append
- `log_activity` 잡에서만 호출 (다른 잡은 금지)

# Procedure

## Quick recipe
JSON 한 건 또는 배열을 stdin으로 흘려보낸다:

```bash
printf '%s' "$JSON_PAYLOAD" | python3 \
  ~/.hermes/profiles/journal_ops/skills/storage/sheets_append/scripts/post_to_sheet.py
```

성공 시 exit 0 + stdout `OK rows=N`. 실패 시 exit 1~3 (스크립트 docstring 참조).

## 1. 입력 형식
- **단일 활동**: `{"Date": "2026-04-29", "Activity": "운동", ...}`
- **복수 활동**: `[{...}, {...}]`
- 24-필드 스키마(`profiles/journal_ops/intent_schema.json`) 준수
- 누락 필드는 `null` 또는 생략 (스크립트가 빈 문자열로 정규화)

## 2. 호출
- **stdin**: JSON (한 줄 또는 멀티라인 OK — `json.load`로 파싱)
- **환경변수**: `GOOGLE_SHEETS_WEBHOOK_URL` (필수, terminal env_passthrough로 주입됨)
- **옵션**:
  - `--dry-run`: 페이로드만 출력 (HTTP 호출 안 함, exit 0)

## 3. 검증
- HTTP 200 + 응답 본문에 `{"ok": true, "rows": N}` 기대
- HTTP 5xx → 1초 대기 후 1회만 재시도
- HTTP 4xx (401/403 포함) → Apps Script 권한 만료 — 즉시 실패, 재시도 금지
- HTTP 200이지만 `{"ok": false, ...}` → 부분 실패로 간주, exit 3

## 4. Ledger 기록
- 성공: `event_type: "sheets_append_ok", rows: N`
- 실패: `event_type: "sheets_append_failed", http_status: N, exit_code: N`

## 5. 실패 알림 (자동, best-effort)
`JOURNAL_ALERT_WEBHOOK_URL` 환경변수가 설정돼 있으면 시트 append 실패 시
스크립트가 Discord에 빨간 embed로 운영 경보를 자동으로 발사한다.
- 발사 조건: HTTP 4xx/5xx, `{"ok": false, ...}` 응답, 네트워크 오류
- 발사 안 함: 입력 JSON invalid (exit 1), webhook URL 미설정 (exit 2)
- 알림 자체가 실패해도 메인 exit code에는 영향 없음 (best-effort)
- 사용 목적: #일기 채널에 섞이지 않는 별도 운영 채널에서 통합 헬스 모니터링

# Pitfalls
- **Webhook URL 유출**: 로그/stdout/응답에 절대 출력하지 말 것 (URL이 OAuth 권한이라 leak = 시트 쓰기 권한 leak)
- **Apps Script CORS/권한**: doPost 함수가 반드시 `ContentService.createTextOutput(JSON.stringify(o)).setMimeType(JSON)` 형식으로 응답해야 함
- **배포 URL 차이**: Deploy → "새 배포" → 웹 앱의 URL 사용 (`/exec`로 끝남). 개발 모드 URL(`/dev`)은 인증 필요해서 동작 안 함
- **시트 헤더 변경 시**: post_to_sheet.py의 `COLUMNS` 상수도 같이 갱신 필요. 헤더 행과 컬럼 순서가 반드시 1:1 매칭
- **Tags가 list인 경우**: 스크립트가 자동으로 `", "` join하여 시트에 문자열로 저장

# Verification
1. `exit 0` + stdout에 `OK rows=N` 확인
2. 시트에 실제 행 추가 확인 (수동 검증)
3. Ledger에 `sheets_append_ok` 이벤트 기록
4. 실패 시 stderr에 명확한 에러 메시지 + exit 1/2/3
