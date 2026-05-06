#!/bin/bash
# Refresh OPENAI_BASE_URL in every profile's .env so it points at the
# current WSL→Windows-host gateway IP. WSL2's NAT IP can change across
# reboots; called by scripts/run_bot.py at every boot so the bot never
# spawns a Hermes process pointing at a stale Ollama address.
#
# Idempotent. Profiles whose .env doesn't exist (not provisioned) are
# skipped silently. Fails open (exit 0) if Ollama isn't reachable, so a
# missing local model never blocks the gateway from starting.
#
# Replaces the journal_ops-only refresh_journal_ops_ollama_url.sh —
# generalized for the local-first migration where calendar_ops, mail_ops,
# and kk_job also point at WSL→Windows Ollama.
set -u

PROFILE_ROOT="$HOME/.hermes/profiles"
PROFILES=(journal_ops calendar_ops mail_ops kk_job advisor_ops)

HOST_IP=$(ip route show default | awk '/default/{print $3; exit}')
if [ -z "$HOST_IP" ]; then
  echo "[refresh-ollama-url] could not detect WSL gateway IP; leaving .envs unchanged" >&2
  exit 0
fi
NEW_URL="http://$HOST_IP:11434/v1"

# Optional reachability probe — warn but don't block.
if ! curl -s -m 3 "http://$HOST_IP:11434/api/tags" >/dev/null; then
  echo "[refresh-ollama-url] WARNING: Ollama not reachable at $HOST_IP:11434" >&2
fi

for prof in "${PROFILES[@]}"; do
  ENV_FILE="$PROFILE_ROOT/$prof/.env"
  if [ ! -f "$ENV_FILE" ]; then
    continue  # profile not provisioned in WSL; skip silently
  fi

  CURRENT=$(grep -E '^OPENAI_BASE_URL=' "$ENV_FILE" | head -1 | cut -d= -f2-)
  if [ "$CURRENT" = "$NEW_URL" ]; then
    continue  # already correct; skip silently to keep boot output clean
  fi

  if grep -q '^OPENAI_BASE_URL=' "$ENV_FILE"; then
    awk -v url="$NEW_URL" 'BEGIN{FS=OFS="="} /^OPENAI_BASE_URL=/{$0="OPENAI_BASE_URL=" url; print; next} {print}' \
      "$ENV_FILE" > "$ENV_FILE.tmp" && mv "$ENV_FILE.tmp" "$ENV_FILE"
  else
    printf '\nOPENAI_BASE_URL=%s\n' "$NEW_URL" >> "$ENV_FILE"
  fi
  echo "[refresh-ollama-url] $prof: OPENAI_BASE_URL -> $NEW_URL"
done
