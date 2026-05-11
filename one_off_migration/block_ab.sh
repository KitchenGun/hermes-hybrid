#!/usr/bin/env bash
# Block A/B: advisor_ops + calendar_ops (running gateway, kk_job 패턴 동일)
set -euo pipefail

mkdir -p ~/.hermes/profiles_placeholder

echo "=== block A: advisor_ops ==="
echo "-- stop gateway --"
systemctl --user stop hermes-gateway-advisor_ops.service
echo "  stopped"
echo "-- move placeholder --"
mv ~/.hermes/profiles/advisor_ops ~/.hermes/profiles_placeholder/advisor_ops.placeholder
echo "  placeholder moved"
echo "-- restore real --"
mv ~/.hermes/profiles/profiles/advisor_ops ~/.hermes/profiles/advisor_ops
echo "  real advisor_ops restored"
echo "-- start gateway --"
systemctl --user start hermes-gateway-advisor_ops.service
echo "  started"
echo "-- verify kanban.db size --"
stat -c '%n %s' ~/.hermes/profiles/advisor_ops/kanban.db 2>&1 || echo "  (no kanban.db)"

echo
echo "=== block B: calendar_ops ==="
echo "-- stop gateway --"
systemctl --user stop hermes-gateway-calendar_ops.service
echo "  stopped"
echo "-- move placeholder --"
mv ~/.hermes/profiles/calendar_ops ~/.hermes/profiles_placeholder/calendar_ops.placeholder
echo "  placeholder moved"
echo "-- restore real --"
mv ~/.hermes/profiles/profiles/calendar_ops ~/.hermes/profiles/calendar_ops
echo "  real calendar_ops restored"
echo "-- start gateway --"
systemctl --user start hermes-gateway-calendar_ops.service
echo "  started"
echo "-- verify auth.json + config.yaml --"
test -f ~/.hermes/profiles/calendar_ops/auth.json && echo "  auth.json OK"
test -f ~/.hermes/profiles/calendar_ops/config.yaml && echo "  config.yaml OK"

echo
echo "=== final check ==="
echo "-- nested should be empty --"
ls ~/.hermes/profiles/profiles/ 2>&1 || true
echo "-- profile list --"
hermes profile list 2>&1 | head -25
