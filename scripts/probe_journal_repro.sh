#!/bin/bash
# Repro of the failure the user saw in #일기.
# Must run under a login shell so ~/.local/bin (where hermes lives) is on PATH.
export PATH="$HOME/.local/bin:$PATH"
QUERY='[현재 날짜: 2026-05-01 (금), 현재 시각: 12:09 KST] 아침에 일어남 컨디션 3 기분 피곤해 당시시간 오후 12시 09분'
echo "==> Query: $QUERY"
echo "==> Running hermes (max 90s) ..."
timeout 90 hermes -p journal_ops chat -q "$QUERY" -Q --max-turns 3 2>&1 | tail -50
