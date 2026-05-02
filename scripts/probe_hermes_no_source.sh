#!/bin/bash
# Mimic exactly what HermesAdapter spawns: bash -lc with no .env source.
# This shows whether Hermes auto-loads the profile/.env or whether we need
# to source it ourselves before calling.
echo "OPENAI_BASE_URL (parent shell) = ${OPENAI_BASE_URL:-<unset>}"
timeout 60 hermes -p journal_ops chat -q "ping just answer ok" -Q --max-turns 1 2>&1 | tail -10
