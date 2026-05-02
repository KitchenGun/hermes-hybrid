#!/bin/bash
# Smoke test: drive calendar_ops with a list-events prompt and check
# whether the google_calendar MCP server tool was actually invoked.
# READ-ONLY — no events created.
export PATH="$HOME/.local/bin:$PATH"
QUERY='[현재 날짜: 2026-05-01 (금), 현재 시각: 17:36 KST] 오늘 남은 일정 한 줄 요약해줘.'
echo "==> Query: $QUERY"
echo "==> Running hermes -p calendar_ops (max 240s) ..."
timeout 240 hermes -p calendar_ops chat -q "$QUERY" -Q --max-turns 6 2>&1 | tail -40
echo
echo "==> Inspecting last session for tool_calls ..."
python3 /mnt/e/hermes-hybrid/scripts/inspect_last_session.py
