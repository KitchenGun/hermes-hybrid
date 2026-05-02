#!/bin/bash
# Quick audit of WSL profile .env files for the local-first migration.
# Reports presence of OPENAI_BASE_URL key (refresh_ollama_base_urls.sh
# needs to know which profiles to update).
PROFILES=(journal_ops calendar_ops mail_ops kk_job)
for p in "${PROFILES[@]}"; do
  f="$HOME/.hermes/profiles/$p/.env"
  if [ ! -f "$f" ]; then
    printf "%-15s : <env file missing>\n" "$p"
    continue
  fi
  url=$(grep -E '^OPENAI_BASE_URL=' "$f" | head -1 | cut -d= -f2-)
  if [ -z "$url" ]; then
    printf "%-15s : OPENAI_BASE_URL <not set>\n" "$p"
  else
    printf "%-15s : OPENAI_BASE_URL=%s\n" "$p" "$url"
  fi
done
