---
name: sheets_append
description: Append job postings to the kk_job Google Sheet (raw + curated tabs) via Apps Script doPost.
version: 1.0.0
platforms: [linux, macos, windows]
metadata:
  hermes:
    tags: [storage, sheets, job]
    category: storage
    requires_toolsets: [terminal]
    config:
      - key: timeout_sec
        default: 15
    required_environment_variables:
      - name: JOB_SHEETS_WEBHOOK_URL
        description: "Apps Script web app URL for the kk_job spreadsheet (raw + curated tabs). Empty → script falls back to writing CSV to runtime/sheet_fallback/."
        optional: true
---

# When to Use
- `morning_game_jobs` cron의 2단계 (수집된 raw 데이터 시트 적재)
- `morning_game_jobs` cron의 4단계 (curated Top 5 시트 적재)

## 사용하지 말아야 할 때
- journal_ops 활동 로그 — `profiles/journal_ops/skills/storage/sheets_append/`를 쓸 것
- 단일 문자열 메모 — Discord webhook이 더 적합

# Procedure

## Quick recipe
JSON 한 건 또는 배열을 stdin으로 전달:

```bash
printf '%s' "$JSON_PAYLOAD" | python3 \
  ~/.hermes/profiles/kk_job/skills/storage/sheets_append/scripts/post_to_sheet.py \
  --tab raw
```

성공 시 exit 0 + stdout `OK rows=N tab=raw`. 실패 시 exit 1~3.

## 1. 입력 형식

### --tab raw (크롤링 원본)
컬럼 순서:
```
crawled_at | source | company | title | seniority | employment_type |
location | requirements | preferred | tech_stack | url | deadline |
raw_text | applied
```

### --tab curated (에이전트 산출 Top 5)
컬럼 순서:
```
date | company | title | match_score | match_reason | mismatch | url
```

## 2. 호출
- **stdin**: JSON 객체 또는 배열
- **--tab raw|curated**: 시트 탭 + 컬럼 순서 선택 (필수)
- **환경변수**: `JOB_SHEETS_WEBHOOK_URL` (옵션 — 미설정 시 CSV 폴백)
- **--dry-run**: 페이로드만 stdout 출력

## 3. 폴백 정책
`JOB_SHEETS_WEBHOOK_URL`이 비어있으면 시트 전송 대신
`runtime/sheet_fallback/<tab>_<yyyymmdd_HHMMSS>.csv`에 CSV로 저장하고
exit 0. 호출 측이 webhook 미설정 환경(첫 배포 전)에서도 잡 전체가
죽지 않도록 하는 graceful degradation.

## 4. 검증
- HTTP 200 + `{"ok": true, "rows": N, "tab": "raw"}` 응답
- 4xx/5xx → 즉시 실패 (exit 3) — Apps Script 권한/배포 문제
- 200이지만 `{"ok": false}` → exit 3

# Apps Script 계약 (사용자 측 배포 필요)

다음 시그니처의 Apps Script를 시트에 배포해야 한다:

```javascript
function doPost(e) {
  var body = JSON.parse(e.postData.contents);
  var ss = SpreadsheetApp.getActiveSpreadsheet();
  var sheet = ss.getSheetByName(body.tab);  // "raw" or "curated"
  if (!sheet) {
    return ContentService.createTextOutput(
      JSON.stringify({ok:false, error:"tab not found: "+body.tab})
    ).setMimeType(ContentService.MimeType.JSON);
  }
  var rows = body.rows;
  if (rows.length > 0) {
    var startRow = sheet.getLastRow() + 1;
    sheet.getRange(startRow, 1, rows.length, rows[0].length).setValues(rows);
  }
  return ContentService.createTextOutput(
    JSON.stringify({ok:true, rows:rows.length, tab:body.tab})
  ).setMimeType(ContentService.MimeType.JSON);
}
```

배포: 새 배포 → 웹 앱 → 액세스 권한 "본인" → URL 복사 →
`.env`의 `JOB_SHEETS_WEBHOOK_URL`에 붙여넣기.

# Pitfalls
- **journal_ops와 다른 webhook**: 둘 다 시트에 쓰지만 다른 시트. URL 혼용 금지.
- **헤더 행**: 사용자가 시트 1행에 컬럼 헤더를 미리 입력해야 함. 스크립트는 마지막 행 이후에만 append.
- **백업 탭**: 기존 데이터 있으면 사용자가 수동으로 `backup_YYYYMMDD` 탭 생성 후 재구성 권장.
- **rows 비었을 때**: HTTP 호출은 하지만 setValues 안 함 (exit 0).

# Verification
1. exit 0 + stdout에 `OK rows=N tab=<tab>` 또는 `FALLBACK csv=<path>`
2. 시트에 행 추가 확인 (수동)
3. webhook 미설정 모드: `runtime/sheet_fallback/` 디렉토리에 CSV 생성
