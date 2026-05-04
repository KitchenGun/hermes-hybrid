#!/usr/bin/env bash
# Ensure each profile .env has a non-empty OPENAI_API_KEY value.
# Ollama ignores the key but Hermes' OpenAI SDK aborts when it's empty
# (the chat-completions client validates the header before reading the
# response). A literal "ollama-ignored" sentinel keeps the SDK happy
# without misleading anyone reviewing .env files later.
set -u

PROFILE_ROOT="$HOME/.hermes/profiles"
PROFILES=(calendar_ops kk_job journal_ops)
SENTINEL="ollama-ignored"

for prof in "${PROFILES[@]}"; do
    ENV_FILE="$PROFILE_ROOT/$prof/.env"
    [ -f "$ENV_FILE" ] || continue
    if grep -q '^OPENAI_API_KEY=' "$ENV_FILE"; then
        continue
    fi
    printf '\nOPENAI_API_KEY=%s\n' "$SENTINEL" >> "$ENV_FILE"
    echo "[ensure-api-key] $prof: appended sentinel"
done
