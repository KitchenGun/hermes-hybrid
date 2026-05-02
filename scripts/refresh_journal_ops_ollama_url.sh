#!/bin/bash
# Refresh OPENAI_BASE_URL in ~/.hermes/profiles/journal_ops/.env so it points
# at the current WSL→Windows-host gateway IP. WSL2's NAT IP can change across
# reboots; called by scripts/run_bot.py at every boot so the bot never spawns
# a Hermes process pointing at a stale Ollama address.
#
# Idempotent. No-op if .env is missing (profile not provisioned). Fails open
# (exit 0 with stderr note) if Ollama isn't reachable, so a missing local
# model never blocks the gateway from starting.
set -u

ENV_FILE="$HOME/.hermes/profiles/journal_ops/.env"
if [ ! -f "$ENV_FILE" ]; then
  echo "[refresh-ollama-url] $ENV_FILE not found; skipping" >&2
  exit 0
fi

HOST_IP=$(ip route show default | awk '/default/{print $3; exit}')
if [ -z "$HOST_IP" ]; then
  echo "[refresh-ollama-url] could not detect WSL gateway IP; leaving .env unchanged" >&2
  exit 0
fi

NEW_URL="http://$HOST_IP:11434/v1"
CURRENT=$(grep -E '^OPENAI_BASE_URL=' "$ENV_FILE" | head -1 | cut -d= -f2-)
if [ "$CURRENT" = "$NEW_URL" ]; then
  echo "[refresh-ollama-url] OPENAI_BASE_URL already $NEW_URL; no change"
  exit 0
fi

if grep -q '^OPENAI_BASE_URL=' "$ENV_FILE"; then
  awk -v url="$NEW_URL" 'BEGIN{FS=OFS="="} /^OPENAI_BASE_URL=/{$0="OPENAI_BASE_URL=" url; print; next} {print}' \
    "$ENV_FILE" > "$ENV_FILE.tmp" && mv "$ENV_FILE.tmp" "$ENV_FILE"
else
  printf '\nOPENAI_BASE_URL=%s\n' "$NEW_URL" >> "$ENV_FILE"
fi
echo "[refresh-ollama-url] OPENAI_BASE_URL -> $NEW_URL"
