# journal_ops — Discord 활동 로거

Discord `#일기` 채널에 자연어로 활동을 기록하면, 24-필드 스키마로 파싱하여
Google Sheets에 즉시 행을 추가하는 Hermes 프로파일.

## 흐름

```
사용자: "방금 9시 20분부터 10시 30분까지 운동했어. Focus 4, deep work."
         ↓ Discord #일기 채널
[discord_bot] channel_id == JOURNAL_CHANNEL_ID → forced_profile="journal_ops"
         ↓
[Orchestrator._handle_forced_profile] hermes -p journal_ops
         ↓
[on_demand/log_activity.yaml] LLM이 24-필드 JSON 추출
         ↓
[skills/storage/sheets_append/scripts/post_to_sheet.py] Apps Script webhook POST
         ↓
[Google Sheet] 행 1개 append
         ↓
사용자: "✅ 저장됨 — 09:20~10:30 / 운동 / 70분 — Category: Health, Focus 4"
```

## 1. Google Sheet 준비

1. 새 스프레드시트 생성 (예: "Activity Log")
2. 첫 번째 시트의 1행에 다음 21개 헤더를 **이 순서대로** 작성:

| Date | Weekday | Start Time | End Time | Duration | Activity | Category | Subcategory | Tags | Priority | Focus Score | Energy Score | Difficulty | Deep Work | Planned/Unplanned | Outcome | Notes | Location | Device | Interruptions | Mood |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|

⚠️ 헤더 순서가 [`post_to_sheet.py`의 `COLUMNS` 상수](skills/storage/sheets_append/scripts/post_to_sheet.py)와
정확히 일치해야 한다. 시트 헤더를 바꾸려면 `COLUMNS`도 함께 갱신.

## 2. Apps Script 배포

확장프로그램 → **Apps Script** 클릭 → 다음 코드 붙여넣기:

```javascript
/**
 * journal_ops doPost handler.
 * Receives {"rows": [[col1, col2, ...], ...]} and appends to Sheet1.
 */
function doPost(e) {
  try {
    const payload = JSON.parse(e.postData.contents);
    const rows = payload.rows || [];
    if (!Array.isArray(rows) || rows.length === 0) {
      return _json({ ok: false, error: "no rows" });
    }
    // Sanity: every row must be a non-empty array of equal length.
    const width = rows[0].length;
    for (const r of rows) {
      if (!Array.isArray(r) || r.length !== width) {
        return _json({ ok: false, error: "ragged rows" });
      }
    }
    const sh = SpreadsheetApp.getActiveSpreadsheet().getSheets()[0];
    sh.getRange(sh.getLastRow() + 1, 1, rows.length, width).setValues(rows);
    return _json({ ok: true, rows: rows.length });
  } catch (err) {
    return _json({ ok: false, error: String(err) });
  }
}

function _json(o) {
  return ContentService
    .createTextOutput(JSON.stringify(o))
    .setMimeType(ContentService.MimeType.JSON);
}
```

**저장** → **배포 → 새 배포** → 종류: **웹 앱**
- 실행: 본인 계정
- 액세스 권한: **모든 사용자** (또는 본인만 — 그 경우 OAuth 헤더 필요)
- **배포** 클릭 → URL 복사 (`https://script.google.com/macros/s/.../exec` 형식)

⚠️ 개발 모드 URL(`/dev`)이 아니라 **배포된 웹앱 URL(`/exec`)** 을 사용해야 한다.

## 3. 환경변수 설정

`E:/hermes-hybrid/.env`에 다음 두 값을 채운다:

```env
# Discord #일기 채널의 ID
# Discord 설정 → 고급 → "개발자 모드" ON → 채널 우클릭 → "ID 복사"
JOURNAL_CHANNEL_ID=1234567890123456789

# 위에서 배포한 Apps Script 웹앱 URL
GOOGLE_SHEETS_WEBHOOK_URL=https://script.google.com/macros/s/AKfycb.../exec
```

## 4. WSL ~/.hermes로 sync

Hermes CLI는 WSL 안에서 `~/.hermes/profiles/...`를 읽으므로, 프로파일 디렉토리를
WSL 경로로 복사/싱크해야 한다:

```bash
# Windows PowerShell (이 프로젝트 루트에서):
wsl rsync -av profiles/journal_ops/ ~/.hermes/profiles/journal_ops/
```

또는 기존 sync 스크립트가 있다면 그걸 재실행한다 (`scripts/sync_profiles.sh` 등).

## 5. 봇 재시작

`.env`를 변경했으니 봇을 재시작해야 한다.

```powershell
# 기존 프로세스 종료 후
python scripts/run_bot.py
```

## 6. 검증 (수동)

| # | 입력 (#일기 채널에) | 기대 |
|---|--------------------|------|
| 1 | `방금 9시 20분부터 10시 30분까지 운동했어. Focus 4, Energy 5, deep work` | ✅ 저장됨 + 시트에 1행 (Date=오늘, Start=09:20, End=10:30, Duration=70, Focus=4, Deep Work=true) |
| 2 | `오전 9~10시 코딩, 10~11시 회의, 12~13시 점심` | ✅ 3건 저장됨 + 시트 3행 (Category: Work/Work/Life) |
| 3 | `그냥 좀 산책했어` | Date+Activity만 채워진 1행, 시각/Score는 빈 값 |
| 4 | `JOURNAL_CHANNEL_ID`를 임시로 잘못된 값으로 → "운동 30분" | journal_ops 호출 안 됨 (다른 라우팅 흐름) |
| 5 | `#일기` 채널에 `!heavy 어제 활동 분석해줘` | Claude C2 응답, 시트 변경 없음 (heavy가 forced 우선) |

## 트러블슈팅

### `OK rows=N`이 안 뜨고 exit 3
- Apps Script 배포 URL이 `/dev`로 끝나는지 확인 (`/exec`이어야 함)
- Apps Script 콘솔의 **실행** 탭에서 `doPost` 에러 로그 확인
- 시트 컬럼 수가 21개와 일치하는지

### `GOOGLE_SHEETS_WEBHOOK_URL env var not set` (exit 2)
- `.env`에 값이 있는지
- `config.yaml`의 `terminal.env_passthrough`에 `GOOGLE_SHEETS_WEBHOOK_URL`이 포함됐는지
- 봇 재시작했는지

### LLM이 JSON이 아닌 텍스트 반환
- `prompt`의 "코드펜스 없이 순수 JSON" 강조가 약할 수 있음
- L2(Ollama) 모델이 너무 작으면 → `tier_policy.bump_rules`가 자동으로 C1으로 escalate
- 또는 `.env`에 `OLLAMA_ENABLED=false` 설정 → 처음부터 GPT-4o-mini 사용

### `forced_profile`이 적용 안 됨
- `JOURNAL_CHANNEL_ID`가 정확한 숫자인지 (Discord 채널 ID 18~19자리 정수)
- `discord_bot.on_message`의 채널 매칭 로그 확인
- 봇 재시작 필수

## 관련 파일

- [config.yaml](config.yaml) — Hermes 프로파일 설정
- [SOUL.md](SOUL.md) — 에이전트 행동 규범 (시스템 프롬프트)
- [intent_schema.json](intent_schema.json) — 24-필드 ActivityIntent 스키마
- [on_demand/log_activity.yaml](on_demand/log_activity.yaml) — 메인 잡 (4-Step prompt)
- [skills/storage/sheets_append/SKILL.md](skills/storage/sheets_append/SKILL.md) — append 절차
- [skills/storage/sheets_append/scripts/post_to_sheet.py](skills/storage/sheets_append/scripts/post_to_sheet.py) — Apps Script 클라이언트

## 참고: 코어 변경 사항

이 프로파일을 활성화하기 위해 다음 코어 코드가 변경됐다:

- `src/config.py`: `Settings.journal_channel_id`, `Settings.google_sheets_webhook_url`
- `src/state/task_state.py`: `TaskState.forced_profile`
- `src/orchestrator/orchestrator.py`: `handle(forced_profile=...)`, `_handle_forced_profile()`
- `src/gateway/discord_bot.py`: `on_message`에서 채널 ID 매칭 → forced_profile 전달
