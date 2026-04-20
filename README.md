# hermes-hybrid

Hybrid LLM orchestrator that wraps the **official NousResearch Hermes Agent** as its heavy-lifting runtime, adds a deterministic **Rule Layer**, a lightweight **Router**, a **Validator** with retry-budget logic, and a **Discord gateway**.

```
Discord ─► Orchestrator ─► Rule ─► Router ─┬─► Local (Ollama 14B) [optional]
                                            ├─► Worker (Ollama 32B) [optional]
                                            └─► Hermes Agent (official)
                                                    └─► Claude Opus / GPT-4o
                              ▼
                          Validator ─► pass | retry | tier-up | escalate
                              ▼
                          Discord
```

See `docs/architecture.md` (the design spec) for the full contract.

---

## Requirements

- Windows + WSL2 Ubuntu (the official Hermes Agent lives in WSL2)
- Python 3.11+
- Official Hermes Agent installed at `~/.hermes/` in WSL2 (`hermes doctor` must pass)
- Discord bot token
- Anthropic API key (Claude Opus) — used via Hermes Agent
- (Optional) OpenAI API key for GPT-4o buffer layer
- (Optional) Ollama on Windows with 7B/14B/32B models pulled

## Install

```powershell
cd E:\hermes-hybrid
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -e ".[dev]"
copy .env.example .env
# edit .env and set DISCORD_BOT_TOKEN, OPENAI_API_KEY, ANTHROPIC_API_KEY
```

## Verify Hermes backend is reachable

```powershell
wsl -d Ubuntu -- bash -lc "hermes --version"
```

## Smoke test (CLI, no Discord)

```powershell
python -m src.orchestrator.cli "/ping"
python -m src.orchestrator.cli "hello"
python -m src.orchestrator.cli "analyze this and write a report: https://example.com"
```

## Run Discord gateway

**Stop the official Hermes gateway first** so both bots don't contend for the same token:

```powershell
wsl -d Ubuntu -- bash -lc "systemctl --user stop hermes-gateway"
```

Then:

```powershell
python scripts/run_bot.py
```

## Tests

```powershell
pytest -q
```

## Project layout

```
src/
├─ config.py              # pydantic-settings loader
├─ state/task_state.py    # TaskState per design §9
├─ router/
│  ├─ rule_layer.py       # exact commands
│  └─ router.py           # heuristic + optional Ollama 7B refinement
├─ hermes_adapter/        # subprocess wrapper for `hermes chat -q -Q`
├─ llm/                   # OpenAI, Anthropic, Ollama clients
├─ validator/             # error classification + retry budget
├─ orchestrator/          # main loop + CLI entry
└─ gateway/               # discord.py bot
tests/                    # pytest smoke tests
scripts/run_bot.py        # Discord entry point
```

## Design invariants (do NOT violate)

1. Orchestrator does not execute tools. Hermes does.
2. Router only returns `{route, confidence, reason, requires_planning}`.
3. LLMs are execution engines; Hermes selects them.
4. Claude is the last-resort fallback, budgeted per session.
5. Rule Layer answers only confirmed patterns (no LLM fallback).
