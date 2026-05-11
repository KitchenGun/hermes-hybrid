#!/usr/bin/env bash
# Check missing keys in ~/.hermes/.env
set -uo pipefail

echo "=== ~/.hermes/.env key check ==="
for key in DISCORD_ALLOWED_USER_IDS DISCORD_MAIL_WEBHOOK_URL DISCORD_CALENDAR_WEBHOOK_URL TELEGRAM_BOT_TOKEN TELEGRAM_ALLOWED_USERS GATEWAY_ALLOW_ALL_USERS; do
    line=$(grep -E "^${key}=" ~/.hermes/.env 2>/dev/null || true)
    if [ -n "$line" ]; then
        echo "  [exists] $line"
    else
        echo "  [MISSING] $key"
    fi
done

echo
echo "=== hermes-hybrid /.env values for those keys ==="
for key in DISCORD_ALLOWED_USER_IDS DISCORD_MAIL_WEBHOOK_URL DISCORD_CALENDAR_WEBHOOK_URL TELEGRAM_BOT_TOKEN TELEGRAM_ALLOWED_USERS GATEWAY_ALLOW_ALL_USERS; do
    line=$(grep -E "^${key}=" /mnt/e/hermes-hybrid/.env 2>/dev/null || true)
    if [ -n "$line" ]; then
        echo "  hh: $line"
    else
        echo "  hh: (no $key)"
    fi
done
