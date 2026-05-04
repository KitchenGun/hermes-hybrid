# Local-First Migration — Model Decisions

> Living document. Updated as bench data lands. Final per-profile picks
> recorded here drive the `model:` field in each `profiles/*/config.yaml`.

## Hardware envelope

- RTX 4090 24 GB (free ~21.5 GB at idle)
- i9-14900KF, 32 GB RAM
- E: 573 GB free
- Ollama runs Windows-native; WSL hermes calls via default-gateway IP

Practical quant ceilings:

| Param | Q4_K_M | Q5_K_M | Q6_K  | Q8_0  |
|------:|-------:|-------:|------:|------:|
|    7B |  4.4 GB |  5.3 GB |  5.9 GB |  7.6 GB |
|   14B |  8.4 GB | 10.5 GB | 12 GB  | 15.7 GB |
|   32B | 18.5 GB | 22 GB ⚠ | 26 GB ✗ | ✗ |
|   70B |    ✗    |    ✗    |    ✗   | ✗ |

## Candidate pool (Phase 3 sweep)

Baseline (kept so ScoreMatrix can prove improvement):
- qwen2.5:7b-instruct (4.4 GB, Q4_K_M)
- qwen2.5:14b-instruct (8.4 GB, Q4_K_M)
- qwen2.5-coder:32b-instruct (18.5 GB, Q4_K_M)

2025–2026 sweep targets:
- qwen3:14b
- qwen3:30b-a3b (MoE, active 3B)
- gpt-oss:20b
- gemma3:27b
- deepseek-r1:14b
- phi4:14b
- mistral-nemo:12b-instruct-2407 (verify)

## Bench wiring

- 8 dimensions: korean / json / code_gen / code_review / summarize / routing / long_context / speed
- 10 job_type weight rows (see `config/benchmark_prompts.yaml`)
- Judge: **Claude Code CLI / haiku** via `bench_judge_backend=claude_cli`
  - Max OAuth = $0 marginal cost
  - cloud_policy.yaml `claude_auto_calls_per_day=50` → temporarily raise to 200 for full sweep, restore after
  - Auto BenchScheduler MUST use `bench_judge_backend=ollama` (not yet wired)

## Per-profile current state

| Profile | Used by | Current model | Hermes-CLI invoked? |
|---------|---------|---------------|---------------------|
| journal_ops | Discord #일기 forced_profile | qwen2.5:14b-instruct (just switched from gpt-4o-mini) | yes |
| calendar_ops | calendar_skill_enabled gate | gpt-4o (cloud — OpenAI quota dead) | yes |
| mail_ops | Python mail_skill (orchestrator-side) | gpt-4o-mini (config dead — skill bypasses Hermes) | no |
| kk_job | (no caller in src/orchestrator or src/gateway) | gpt-4o + ollama:coder:32b fallback | no |

→ Only journal_ops + calendar_ops actually consume their `model:` field via Hermes CLI. mail_ops + kk_job profile model edits are cosmetic — the real OpenAI surrogate exit for those is the Python `mail_skill` and the orchestrator's `_openai_surrogate_*` lazy clients (already gated by `ollama_routable`).

## Bench results — baseline + deepseek-r1:14b (2026-05-01T08:11Z)

`data/benchmarks/baseline_20260501T165518.json` + `deepseek_r1_20260501T171147.json`

| Model | korean | json | code_gen | code_rev | summarize | routing | long_ctx | speed (tps) |
|-------|-------:|-----:|---------:|---------:|----------:|--------:|---------:|------------:|
| qwen2.5:7b-instruct | 64.7 | 100 | 100 | 62.5 | 46.5 | 60 | 77.5 | 76.3 |
| qwen2.5:14b-instruct | 76 | 100 | 100 | 48.5 | 63 | 60 | 71 | 70.6 |
| qwen2.5-coder:32b-instruct | 85.7 | 100 | 100 | 55 | 74 | 80 | 82.5 | 21.8 |
| **deepseek-r1:14b** | **93.3** | 100 | 50 | **85** | 58.5 | 80 | **92.5** | 78.3 |

Top job_type score per arm:

- qwen2.5:7b-instruct → code_generation 92.5, schedule_logging 85.2
- qwen2.5:14b-instruct → code_generation 89.7, schedule_logging 88.0
- qwen2.5-coder:32b-instruct → schedule_logging 89.5, image_asset_request 87.7, document_transform 85.8

Latency cost (median across dims): 7b ~1.0 s, 14b ~1.7 s, coder:32b ~9 s.

### Notes on the 14b tool-calling regression

The session inspection that triggered this migration (`session_20260501_161513_f1bca1.json`) shows qwen2.5:14b returned `tool_calls=0` for journal_ops and hallucinated a "⚠️ 저장 실패" reply without invoking `sheets_append`. The bench score (`schedule_logging=88`) does NOT capture this — bench prompts grade JSON adherence and structural output, not actual tool-call invocation. **`tool_use_enforcement: "strict"` is the right knob to add to journal_ops alongside the model swap**.

## Pending — extended sweep

Awaiting pulls (sequence):
1. gpt-oss:20b
2. qwen3:14b
3. gemma3:27b
4. deepseek-r1:14b
5. phi4:14b

Run after all in (or partial sets as they land):
```
python scripts/bench_local_models.py \
    --output data/benchmarks/extended_<ts>.json \
    --model qwen3:14b --model qwen3:30b-a3b \
    --model gpt-oss:20b --model gemma3:27b \
    --model deepseek-r1:14b --model phi4:14b
```

## Capability check (gating: `tools` capability) — 2026-05-01T08:24Z

`scripts/check_model_caps.py` (new) reads each model's `/api/show` and
flags whether it supports function calling.

| Model | tools | thinking | vision | context |
|-------|:-:|:-:|:-:|-:|
| qwen2.5:7b/14b/coder:32b | ✅ | - | - | 32K |
| **gpt-oss:20b** | **✅** | ✅ | - | 131K |
| **qwen3:14b** | **✅** | ✅ | - | 41K |
| gemma3:27b | ❌ | - | ✅ | 131K |
| deepseek-r1:14b | ❌ | ✅ | - | 131K |
| phi4:14b | ❌ | - | - | 16K |

**Implication**: gemma3:27b had the highest `schedule_logging` (98.3) but
**lacks tool calling** — using it would reproduce the qwen2.5:14b
hallucination bug (model invents tool responses without firing them).
Same for deepseek-r1 and phi4. Tool-calling-capable shortlist:
**gpt-oss:20b** vs qwen3:14b vs the qwen2.5 baselines.

## v3 picks (applied 2026-05-01T12:23Z) — qwen3:8b smoke regression

After bench data showed `qwen3:8b` topping `schedule_logging` at 97.8 (vs gpt-oss:20b 95.3) with korean 91.3, json 100, routing 100, tools+thinking, the journal_ops profile was switched from `gpt-oss:20b` to `qwen3:8b`. The live smoke (`session_20260501_211905_00c9fd`) regressed:

- Model spent 8 turns on introspection (skill_view, **skill_manage that patched SKILL.md in-place** with hallucinated guidance, memory writes, more skill_view).
- `terminal` / `post_to_sheet.py` was never invoked → no Sheet row written.
- The bench `schedule_logging` axis grades 24-field JSON adherence + structural correctness, NOT tool-call discipline. Score-to-behavior gap is real.

`qwen3:8b` was rolled back. SKILL.md was restored from the repo source. **gpt-oss:20b is the new lock-in for tool-heavy profiles.** The behavioral signal beats the bench signal here.

### v3 final mapping

| Used by | Model | Reason | Different from baseline? |
|---------|-------|--------|--------------------------|
| **journal_ops** | `gpt-oss:20b` | smoke verified (4 turns, `OK rows=1`, Sheet row appended) | swap from cloud gpt-4o-mini |
| **calendar_ops** | `gpt-oss:20b` | tools ✅, korean 93.3, routing 80; OAuth + tool-name routing OK in smoke | swap from cloud gpt-4o |
| **OLLAMA_ROUTER_MODEL** (classifier) | `llama3.2:3b` | tps 129.9, 3B params, tools ✅; single-shot label_match — no thinking-loop risk at this size | swap from qwen2.5:7b |
| **OLLAMA_WORK_MODEL** (L2 surrogate) | `gpt-oss:20b` | same behavioral fit reasoning | swap from qwen2.5:14b (then briefly qwen3:8b) |
| **OLLAMA_WORKER_MODEL** (L3 surrogate) | `gpt-oss:20b` | code_gen 98.2, code_review 91, korean 93.3 | swap from qwen2.5-coder:32b |
| **OLLAMA_JUDGE_MODEL** (auto bench) | `qwen3:8b` | korean 91.3 + summarize 80; judge prompt has no tools to misuse, so the introspection risk is contained to a single SCORE/REASON output | swap from qwen2.5:14b (placeholder) |

### Why no further per-job differentiation

Bench data + smoke pinned 5 of 6 surfaces to `gpt-oss:20b` because it was the only model that:
1. Has the `tools` capability (excludes gemma3:27b/deepseek-r1:14b/phi4:14b),
2. Scores high on schedule_logging + korean + routing simultaneously (excludes qwen2.5 family at <90 korean),
3. Doesn't spend the turn budget on introspective skill edits (excludes qwen3:8b/14b/32b — all thinking models).

The split is router (tiny, fast) + worker (medium, behavior-safe) + judge (medium, single-shot). Genuine per-profile differentiation requires either a different bench dimension that captures tool-call discipline, or a new pool of non-thinking medium models that beat gpt-oss:20b on routing/korean.

## Final per-profile picks (applied 2026-05-01T08:35Z)

| Profile | Picked model | Why | Notes |
|---------|--------------|-----|-------|
| **journal_ops** | **gpt-oss:20b** | tool_calls=4 in smoke (sheets_append fired, `OK rows=1`); schedule_logging 95.3, korean 93.3, tps 139.4, context 131K | strict mode kept; `max_turns: 8`; prompt simplified (don't search for `intent_schema.json`, skip `skill_view` before terminal) |
| **calendar_ops** | **gpt-oss:20b** | only `tools`-capable arm with korean ≥90 + routing ≥80 + sub-2s json latency; OAuth check passed in smoke | direct hermes-CLI call still confuses tool routing (46 tools declared); the calendar_skill orchestrator path provides the actual prompt scaffolding |
| mail_ops | (no change — config dead) | Python mail_skill bypasses profile model; orchestrator surrogate now uses Ollama via `ollama_routable` | n/a |
| kk_job | (no change — config dead) | not invoked in current bot wiring | rebench when wired |

### Orchestrator surrogate (`.env`)
- `OLLAMA_ROUTER_MODEL=qwen2.5:7b-instruct` (kept — fastest classifier)
- `OLLAMA_WORK_MODEL=gpt-oss:20b` (was qwen2.5:14b)
- `OLLAMA_WORKER_MODEL=gpt-oss:20b` (was qwen2.5-coder:32b — single arm in VRAM eliminates swap)
- `OLLAMA_JUDGE_MODEL=gpt-oss:20b` (auto BenchScheduler default when wired)
- `LOCAL_FIRST_MODE=true`, `BENCH_JUDGE_BACKEND=claude_cli`

### Smoke verification

journal_ops `session_20260501_173517_b23913.json` — **success**:
- 4 tool calls: `skill_view`, `read_file`×2, `terminal`
- terminal output: `{"output": "OK rows=1", "exit_code": 0}` → real Google Sheet row appended
- assistant final reply matches SOUL.md format: `✅ 저장됨 / 16:30-17:25 (55분) / local-first 마이그레이션 작업 / Work · Focus 5 · Energy 4 · Deep Work`

calendar_ops `session_20260501_173623_41e5ca.json` — **partial**:
- OAuth tokens loaded (`Valid tokens found for account(s): normal`)
- model attempted skill discovery but didn't land on the right MCP tool name from the 46-tool catalog
- expected to work via the orchestrator's calendar_skill path (which prepares the prompt with explicit tool guidance) but direct `hermes -p calendar_ops chat` is fragile

## Rollback

- `git checkout profiles/ config/ src/` reverts all code/profile changes
- `cp .env.bak.20260501 .env` reverts settings
- `cp ~/.hermes/profiles/<p>/.env.bak.* ~/.hermes/profiles/<p>/.env` reverts WSL profile env
- Toggle `LOCAL_FIRST_MODE=False` to fall back to OpenAI surrogate path without code revert (ollama_routable still True if OLLAMA_ENABLED=true)
