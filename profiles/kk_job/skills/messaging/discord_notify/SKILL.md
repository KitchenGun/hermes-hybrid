---
name: discord_notify
description: Send formatted job/resume notifications to Discord channel via webhook URL.
version: 1.0.0
platforms: [linux, macos]
metadata:
  hermes:
    tags: [notification, discord, messaging]
    category: messaging
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

- 주간 구인 다이제스트 cron 결과 전송
- 지원 마감 D-3 알림 (deadline_reminder)
- 새 공고 키워드 매칭 감지 (new_posting_alert watcher)
- 이력서 초안 생성 완료 알림

## 사용하지 말아야 할 때

- 개인 이력 데이터 (연봉, 전화번호) 평문 전송
- 대용량 문서 (webhook 8MB 제한 — 링크만 전송)
- 실시간 대화 (gateway/discord_bot 담당)

# Procedure

## 1. 입력 검증

```
required:
  title: string (1..256 chars)
  body: string (1..4000 chars)
optional:
  color: int (hex)
  fields: [{name, value, inline?}]
  footer: string
  timestamp: ISO8601
```

## 2. Embed 구성 및 전송

`scripts/post_webhook.py` 사용 (calendar_ops와 공유 패턴):
```bash
echo "..." | python scripts/post_webhook.py \
  --title "🔍 주간 구인 다이제스트 (4/20 주)" \
  --color 0x5865F2 \
  --footer "kk_job | trace_id=..."
```

## 3. 전송 및 검증

- HTTP 204 응답 기대
- 429 수신 시 `X-RateLimit-Reset-After` 대기 후 1회 재시도
- 5xx 수신 시 1초 후 1회 재시도
- 2회 연속 실패 시 에러 전파

## 4. Ledger 기록

성공 시:
- `event_type: "discord_notify_sent"`
- `payload: {title, body_length, response_status, latency_ms}`

**민감 정보 저장 금지**: 연봉, 개인 이메일은 body에 포함 시에도 Ledger에 원문 저장 금지.

# Pitfalls

- **Webhook URL 유출**: 환경변수로만. 로그 출력 금지.
- **Markdown 충돌**: `_`, `*`, `` ` ``는 Discord 포맷 문자.
- **이모지**: 기본 이모지(🔍 📄 💼 📊 ⚠️)만 안전.
- **링크 포함**: 공고 URL은 Discord가 자동 unfurl. trace_id 참조 유지.

# Verification

1. 응답 상태 204 확인
2. Ledger에 `discord_notify_sent` 이벤트 존재
3. 채널 실제 메시지 도착 (수동)

# References

- Discord Webhook API: https://discord.com/developers/docs/resources/webhook
