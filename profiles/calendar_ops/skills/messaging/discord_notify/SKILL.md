---
name: discord_notify
description: Send formatted message to a specific Discord channel via webhook URL.
version: 1.0.0
platforms: [linux, macos]
metadata:
  hermes:
    tags: [notification, discord, messaging]
    category: messaging
    requires_toolsets: [terminal]
    config:
      - key: default_color
        description: "Embed color (hex integer)"
        default: 5793266
        prompt: "기본 embed 색상 (Discord blurple: 0x5865F2)"
      - key: max_message_length
        default: 4000
    required_environment_variables:
      - name: DISCORD_BRIEFING_WEBHOOK_URL
        prompt: "Discord 대상 채널의 webhook URL"
---

# When to Use

- 스케줄 cron 잡이 완료되어 결과를 채널로 알려야 할 때
- 쓰기 작업 완료 후 사용자에게 결과 요약을 보낼 때
- 프롬프트에 "Discord로 전송", "채널에 알림", "notify" 등이 명시될 때

## 사용하지 말아야 할 때

- 개인 DM이 필요한 경우 (별도 `discord_dm` skill 사용 — 미구현)
- 실시간 대화 응답 (gateway/discord_bot이 담당, webhook 불필요)
- 대용량 파일 업로드 (webhook은 8MB 제한)

# Procedure

## 1. 입력 검증

```
required:
  title: string (1..256 chars)
  body: string (1..4000 chars)
optional:
  color: int (hex, 기본 0x5865F2)
  fields: [{name, value, inline?}]  # Discord embed field
  footer: string
  timestamp: ISO8601 (기본 현재)
```

## 2. Embed 구성

`scripts/post_webhook.py` 실행:
```bash
python scripts/post_webhook.py \
  --title "오늘 일정 브리핑" \
  --color 0x5865F2 \
  --footer "calendar_ops | trace_id=..."
# body는 stdin으로 전달
echo "..." | python scripts/post_webhook.py --title "..."
```

## 3. 전송 및 검증

- HTTP 204 응답 기대
- 429 (rate limit) 수신 시 `X-RateLimit-Reset-After` 초만큼 대기 후 1회 재시도
- 5xx 수신 시 1초 후 1회 재시도
- 2회 연속 실패 시 에러를 jobs 레이어로 전파

## 4. Ledger 기록

성공 시:
- `event_type: "discord_notify_sent"`
- `payload: {title, body_length, response_status, latency_ms}`

실패 시:
- `event_type: "discord_notify_failed"`
- `payload: {title, error_code, attempts}`

**body 원문은 Ledger에 저장하지 않음** (민감 일정 정보 보호).

# Pitfalls

- **Webhook URL 유출**: 환경변수로만 읽기. 로그·stdout에 출력 금지.
- **2000자 한계**: Discord 일반 메시지는 2000자, embed description은 4096자. 분할 전송 대신 truncate.
- **Rate Limit**: webhook당 분당 30회. 고빈도 잡은 배치 처리.
- **Markdown 충돌**: `_`, `*`, `` ` ``는 Discord에서 포맷 문자. 필요 시 백슬래시 이스케이프.
- **이모지 깨짐**: 기본 이모지(🌅 📅 ⚠️)는 안전. 커스텀 이모지(`<:name:id>`)는 guild 권한 필요.

# Verification

1. 응답 상태 204 확인
2. Ledger에 `discord_notify_sent` 이벤트 존재
3. 채널에 실제 메시지 도착 확인 (수동 검증 시)

# References

- Discord Webhook API: https://discord.com/developers/docs/resources/webhook#execute-webhook
- Embed 구조: https://discord.com/developers/docs/resources/channel#embed-object
