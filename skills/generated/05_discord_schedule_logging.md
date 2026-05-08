---
name: discord_schedule_logging
agent_handle: "@discord_schedule_logger"
category: documentation
role: schedule_capture
description: Discord 일기 채널 → 24-필드 schedule 추출 → Sheets 적재.
auto_generated:
  date: 2026-05-08
  source: hermes_growth_migration_p0a
  status: candidate
when_to_use:
  - "Discord #일기 메시지 자동 적재"
  - "JOURNAL_SHEET_ID 시트 row append"
not_for:
  - "Discord 메시지 작성 (→ @discord_ops)"
  - "이미지 첨부 (→ @discord_ops)"
inputs:
  - "Discord message text"
  - "user / channel meta"
outputs:
  - "24-필드 normalized JSON + Sheets append result"
metadata:
  hermes:
    primary_tools: [google_sheets, discord]
    tags: [journal, sheets, schedule]
---

# Skill — discord_schedule_logging

## Purpose
Discord #일기 채널 메시지를 24-필드 normalized schedule 로 분해 후 Google Sheets append.

## When to Use
- 일일 / 주간 활동 기록 자동화
- Phase 22 journal pipeline 의 사용자 인터페이스 추출 단계

## Inputs
- raw Discord message
- user_id, channel_id, ts

## Procedure
1. text → 24-필드 schema 매핑 (`profiles/journal_ops/intent_schema.json` 참조).
2. 누락 필드 → 기본값 또는 NEEDS_REVIEW.
3. `src/skills/storage/sheets_append.py` 호출 → Sheets append.
4. 성공 시 Discord 반응 (✅) 추가, 실패 시 (⚠️ + reason).

## Output Format
- normalized JSON (24 필드)
- Sheets append 응답 (row_index)
- Discord reaction 결과

## Safety / Constraints
- 메시지에 secret / token 형태 ([A-Za-z0-9]{30+}) 발견 시 redact.
- Sheets quota 초과 시 retry-after 백오프.

## Example Prompt
(자동 — Discord 봇이 #일기 채널 메시지 받을 때 호출)

## Existing Implementation
- `src/skills/journal/__init__.py`, `src/skills/journal/extractor.py`, `src/skills/journal/format.py`, `src/skills/journal/pipeline.py`
- `src/skills/storage/sheets_append.py`
- Phase 22 (commit 9fa368a)
