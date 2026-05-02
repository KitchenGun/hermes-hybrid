#!/bin/bash
# Smoke test: drive journal_ops with a real activity prompt and inspect
# whether sheets_append actually got invoked (tool_calls > 0). Note this
# WILL append a row to the configured Google Sheet — that's the point.
export PATH="$HOME/.local/bin:$PATH"
QUERY='[현재 날짜: 2026-05-01 (금), 현재 시각: 17:25 KST] 방금 16시 30분부터 17시 25분까지 local-first 마이그레이션 작업했어. Focus 5, Deep Work, 컨디션 4.'
echo "==> Query: $QUERY"
echo "==> Running hermes -p journal_ops (max 180s) ..."
timeout 240 hermes -p journal_ops chat -q "$QUERY" -Q --max-turns 8 2>&1 | tail -40
echo
echo "==> Inspecting last session for tool_calls ..."
python3 /mnt/e/hermes-hybrid/scripts/inspect_last_session.py
