---
name: weather_daily_briefing
agent_handle: "@weather_briefer"
category: infrastructure
role: weather_brief
description: 매일 아침 KMA 단기예보 → 한국어 한 줄 + 알림 / 행동 권장.
auto_generated:
  date: 2026-05-08
  source: hermes_growth_migration_p0a
  status: candidate
when_to_use:
  - "매일 아침 cron (07:00 KST) 자동 실행"
  - "특정 지역 / 시간 날씨 ad-hoc 조회"
not_for:
  - "외부 일반 검색 (→ @researcher)"
inputs:
  - "위치 (좌표 or 지명)"
  - "시간 범위 (default 오늘)"
outputs:
  - "한 줄 요약 + 우산/마스크/온도 행동 권장"
metadata:
  hermes:
    primary_tools: [http]
    tags: [weather, briefing, kma]
---

# Skill — weather_daily_briefing

## Purpose
KMA 단기예보 API 기반 매일 아침 브리핑 — Discord webhook 으로 push.

## When to Use
- cron 매일 07:00 KST
- ad-hoc "오늘 비 와?" 류 질문

## Inputs
- 위치
- 시간 범위

## Procedure
1. KMA API 호출.
2. 강수확률 / 온도 / 풍속 추출.
3. 한국어 한 줄 + 행동 권장 ("우산 챙기세요", "옷 따뜻하게").
4. 결과 → DISCORD_BRIEFING_WEBHOOK_URL.

## Output Format
- 한 줄 요약
- 행동 권장 1-2개

## Safety / Constraints
- KMA quota 초과 시 retry-after.
- 위치 정보 redact (집/회사 GPS 포함되지 않도록 일반 지명만).

## Example Prompt
"오늘 날씨 어때?"

## Existing Implementation
- `scripts/weather_alert.py`, `scripts/install_weather_alert_timer.sh` — 이 skill 은 chat-time interactive lane (cron 외).
