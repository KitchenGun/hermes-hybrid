#!/bin/bash
# One-shot: flip journal_ops profile in WSL to Ollama and verify.
# Idempotent — safe to re-run.
set -euo pipefail

PROFILE_DIR="$HOME/.hermes/profiles/journal_ops"
ENV_FILE="$PROFILE_DIR/.env"
REPO_PROFILE_DIR="/mnt/e/hermes-hybrid/profiles/journal_ops"

echo "==> Detect WSL gateway (Windows host)"
HOST_IP=$(ip route show default | awk '/default/{print $3; exit}')
if [ -z "$HOST_IP" ]; then
  echo "FATAL: could not detect default gateway IP"
  exit 1
fi
echo "  host_ip=$HOST_IP"

echo "==> Probe Ollama at http://$HOST_IP:11434"
if ! curl -s -m 5 "http://$HOST_IP:11434/api/tags" >/dev/null; then
  echo "FATAL: Ollama not reachable at http://$HOST_IP:11434"
  echo "       Make sure Ollama is running on Windows and listening on 0.0.0.0"
  exit 1
fi
echo "  OK"

echo "==> Update OPENAI_BASE_URL in $ENV_FILE"
NEW_URL="http://$HOST_IP:11434/v1"
if grep -q '^OPENAI_BASE_URL=' "$ENV_FILE"; then
  awk -v url="$NEW_URL" 'BEGIN{FS=OFS="="} /^OPENAI_BASE_URL=/{$0="OPENAI_BASE_URL=" url; print; next} {print}' \
    "$ENV_FILE" > "$ENV_FILE.tmp" && mv "$ENV_FILE.tmp" "$ENV_FILE"
else
  printf '\nOPENAI_BASE_URL=%s\n' "$NEW_URL" >> "$ENV_FILE"
fi
grep '^OPENAI_BASE_URL=' "$ENV_FILE"

echo "==> Sync config.yaml from repo to ~/.hermes"
cp "$REPO_PROFILE_DIR/config.yaml" "$PROFILE_DIR/config.yaml"
grep -A4 '^model:' "$PROFILE_DIR/config.yaml"

echo "==> Done."
