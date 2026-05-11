#!/usr/bin/env bash
# Phase B.1: append 3 missing Discord keys to ~/.hermes/.env
# - Token/auth/provider 변경 없음, Discord allowlist + webhook URL만 추가
set -euo pipefail

ENV=~/.hermes/.env

# backup
if [ ! -f "$ENV.backup-pre-r0-b1" ]; then
    cp -p "$ENV" "$ENV.backup-pre-r0-b1"
    echo "  backed up $ENV → $ENV.backup-pre-r0-b1"
else
    echo "  backup already exists (skip)"
fi

# append 3 keys (각각 검사 후, 중복 방지)
echo "" >> "$ENV"
echo "# === Phase B.1 import from hermes-hybrid (2026-05-11) ===" >> "$ENV"

grep -q '^DISCORD_ALLOWED_USER_IDS=' "$ENV" || \
    echo "DISCORD_ALLOWED_USER_IDS=100816750945255424" >> "$ENV"
echo "  added DISCORD_ALLOWED_USER_IDS"

grep -q '^DISCORD_MAIL_WEBHOOK_URL=' "$ENV" || \
    echo "DISCORD_MAIL_WEBHOOK_URL=https://discord.com/api/webhooks/1497951563241488476/Og-sUFkCcA5dxyjVv4ZBcSOtkJvqSEUhnXg0aYUPWtF_Oy4Wm26zkHf-hG4K30TV7mWa" >> "$ENV"
echo "  added DISCORD_MAIL_WEBHOOK_URL"

grep -q '^DISCORD_CALENDAR_WEBHOOK_URL=' "$ENV" || \
    echo "DISCORD_CALENDAR_WEBHOOK_URL=https://discord.com/api/webhooks/1496878034832068809/rzceJ2wizilgexvy_0nBHz2GjQTG9_ox6lPLvO9tQkJdqf3TPbZLXk_-NnUJ1FCInfye" >> "$ENV"
echo "  added DISCORD_CALENDAR_WEBHOOK_URL"

echo
echo "=== verify ==="
echo "-- 3 keys added --"
grep -E "^(DISCORD_ALLOWED_USER_IDS|DISCORD_MAIL_WEBHOOK_URL|DISCORD_CALENDAR_WEBHOOK_URL)=" "$ENV"
echo
echo "-- backup safe --"
ls -la "$ENV.backup-pre-r0-b1"
echo
echo "-- provider/auth keys untouched (sanity) --"
grep -E "^(OPENAI_|ANTHROPIC_API_KEY)" "$ENV" | head -5
