# Architecture — Hermes Hybrid

> ⚠️ **LEGACY: pre-Phase-8 design spec (deprecated 2026-05-06)** ⚠️
>
> 이 문서는 Phase 8 이전의 설계 스펙. JobFactory v1/v2 + Router + tier
> ladder 시대의 다이어그램과 결정 근거를 담고 있다. Phase 8 (2026-05-06)
> 에서 master (opencode/gpt-5.5) + 17 sub-agent 단일 구조로 전환된 후
> 자료는 [`MASTER_ARCHITECTURE.md`](MASTER_ARCHITECTURE.md) 참조.

Authoritative design spec. Code in `src/` implements this contract; keep them in sync.

```
Discord User
  ↓
Orchestrator
  ├─ Rule Layer (exact patterns)
  ├─ Router (local 7B, Ollama — optional)
  ↓
Hermes (Agent Runtime: plan → act → observe → reflect → retry)
  ↓
LLM Layer
  ├─ Ollama (Local)     L2: 14B (Work) | L3: 32B (Worker)
  ├─ GPT-4o (Cloud)     C1: primary cloud / buffer
  └─ Claude Opus 4.7    C2: last-resort heavy reasoning
  ↓
Tools / Skills / Memory (external Runner only)
  ↓
Validator
  ↓
Discord Response
```

## Invariants

1. Hermes is an **Agent Runtime**, not an LLM wrapper. It owns plan/act/reflect and produces ActionJSON.
2. Orchestrator does **routing + policy** only. It never calls tools.
3. LLMs are execution engines. Hermes selects the model.
4. **Tools run in an external Runner.** LLMs emit ActionJSON; the Runner executes.
5. Local-first: Ollama → GPT → Claude.
6. Claude is the final fallback, budgeted per session.

## Router contract

```json
{
  "route": "local | worker | cloud",
  "confidence": 0.0,
  "reason": "string",
  "requires_planning": false
}
```

Thresholds:
- `confidence >= 0.75` → accept.
- `0.50 <= confidence < 0.75` → bump one tier.
- `confidence < 0.50` → force `cloud` + `requires_planning=true`.

## Hermes loop

`PLAN → ACT(ActionJSON) → Runner → OBSERVE → REFLECT → (success|retry|escalate)`

Same-tier retry ≤ 2, tier-up ≤ 2, C2 escalation ≤ 1 (overridden by Validator).

## LLM tiers

| Tier | Model | Purpose |
|---|---|---|
| L2 | Ollama 14B | conversational, summarization |
| L3 | Ollama 32B (Coder) | code / structured JSON |
| C1 | GPT-4o | cloud buffer, JSON repair, planning |
| C2 | Claude Opus 4.7 | final fallback, hard reasoning |

## Cloud fallback table

See main design spec §6 — encoded in `src/validator/validator.py` (`_tier_up` + `_classify`).

## Validator error classes

- `malformed_output` — JSON parse / schema fail → same-tier repair retry.
- `low_quality` — empty / repetitive / self-score < 0.6 → tier-up.
- `timeout` → tier-up with 2× timeout.
- `tool_error` → swap/drop tool, same-tier retry.

## Budgets (defaults, overridable via env)

| Budget | Default |
|---|---|
| `retry_budget` per task | 4 |
| `same_tier_retry_max` | 2 |
| `tier_up_retry_max` | 2 |
| `cloud_escalation_max` | 1 |
| `cloud_token_budget_session` | 20,000 |
| `claude_call_budget_session` | 1 |
| `cloud_token_budget_daily` | 100,000 |

## TaskState

See `src/state/task_state.py`. Mirrors design spec §9 field-for-field.

## Discord response policy

- Rule Layer: plain text.
- >3s jobs: placeholder "⏳ processing…" then edit.
- Final text > 2000 chars: split on newlines or attach as file.
- `degraded=True`: ⚠️ + `/retry <task_id>` hint.
- Cloud escalations are invisible to the user (log only).

## Anti-patterns (never do)

1. Hermes as a bare LLM wrapper (no plan/reflect).
2. Orchestrator executing tools directly.
3. Router that generates plans.
4. Claude as the primary model.
5. LLM invoking tools in-process (function calling runtime).

---

# Phase 1 — Hermes-centric transition

The project is moving from an **Orchestrator-centric** shape (Hermes was a
dead-ish adapter, most work ran through direct LLM clients) to a
**Hermes-centric** shape where Hermes owns plan/act/reflect and the
Orchestrator is routing + policy + budget only.

Phase 1 lays the groundwork — five pre-work fixes and a reversible feature
flag. No call sites are migrated yet; the toggle is opt-in.

## The five FIXes

Each fix is in place as of Phase 1 Day 5 and has tests that lock the
invariant so regressions surface immediately.

| # | Fix | Where | Invariant locked |
|---|-----|-------|------------------|
| 1 | Router excludes `claude-code` | `src/router/router.py` | `Provider = Literal["ollama","openai"]`; `tests/test_router.py::test_provider_literal_excludes_claude_code` |
| 2 | Bump input compression | `src/orchestrator/bump.py` | Non-cumulative, ≤200-char preview; `tests/test_bump.py::test_non_cumulative_across_retries` |
| 3 | Hermes local tier selection log | `src/hermes_adapter/adapter.py` | `hermes.model_selected` structlog event on every run with `primary_model` / `turns_used` |
| 4 | Heavy session reuse | `src/orchestrator/heavy_session.py` + `ClaudeCodeResumeFailed` | 10-min TTL, fresh-fallback on resume failure; `tests/test_heavy_session.py::test_heavy_resume_failure_falls_back_to_fresh` |
| 5 | HermesAdapter v2 output contract | `src/hermes_adapter/adapter.py` | `HermesProviderMismatch` / `HermesBudgetExceeded` / `HermesMalformedResult` + `provider_actual` / `turns_used` / `models_used` fields |

## Feature flag: `USE_HERMES_FOR_LOCAL`

Default: **false**. Controls whether L2/L3 traffic routes through
HermesAdapter v2 or the legacy direct-OpenAI/Ollama client path.

- **false**: current behavior — Ollama (if enabled) else OpenAI surrogate.
  Hermes isn't touched for non-heavy traffic.
- **true**: L2/L3 runs through `HermesAdapter.run(...)` with `provider`
  pinned via `RouterDecision.provider` (never `claude-code`) and
  `max_turns` ≤ 5 for snappy response.

Phase 2+ removes the flag once the Hermes lane is the default. Until then
a single env flip reverts to the legacy path.

## TaskState additions

- `bump_prefix: str = ""` — FIX#2 breadcrumb. Cleared on pass, overwritten
  on each retry. Never grows cumulatively.
- `heavy: bool = False` — opt-in heavy path flag, set by Discord gateway
  when `!heavy ` prefix is detected.

## Observability — Phase 1 events

| Event | When | Key fields |
|-------|------|------------|
| `router.decision` | every auto-path turn | `route`, `provider`, `confidence`, `reason` |
| `bump.compressed` | every retry after a failed attempt | `prev_tier`, `prev_model`, `reason`, `preview_len`, `summary_len` |
| `hermes.model_selected` | every HermesAdapter run | `primary_model`, `models_used`, `provider_actual`, `turns_used` |
| `hermes.provider_mismatch` (via exception) | R1 violation | `requested`, `actual` |
| `hermes.budget_exceeded` (via exception) | R2 violation | `kind`, `used`, `cap` |
| `heavy.session_choice` | every heavy path turn | `choice=reused|fresh`, `age_sec`, `session_id` |
| `heavy.session_recorded` | after heavy turn succeeds | `user_id`, `session_id` |
| `heavy.resume_failed` | CLI rejected `--resume` | `old_session_id`, `reason` |
| `heavy.session_invalidated` | after resume failure | `had_entry`, `reason` |

## Phase 1 exit gate

Flipping `USE_HERMES_FOR_LOCAL=true` globally is allowed only after **all**
of the following hold:

- [ ] Full test suite green: **≥ 108 passing** (current count after Day 4).
- [ ] Latency: `scripts/bench_latency.py` reports p95 ratio **≤ 2×** vs.
      baseline across 30+ samples.
- [ ] 24h Discord live run with flag on for self only, zero
      `hermes.provider_mismatch` / `hermes.budget_exceeded` events.
- [ ] Router `provider=claude-code` count = 0 over 24h (structurally
      impossible, verified in logs).
- [ ] Bump payload size bounded: no log line with `summary_len > 600`.
- [ ] Heavy session reuse observable: at least one `choice=reused` in
      `heavy.session_choice` across real-user traffic.

---

# Phase 2 — Skills surface + C1 Hermes lane

Phase 2 lands the first half of the "make Hermes the default reasoner"
transition. Two things ship together:

1. A **Python-side skills surface** that owns slash commands end-to-end
   (between RuleLayer and the Router). This is the shape Phase 3 will
   port to Hermes' native skill surface — the contract is deliberately
   mechanical to migrate.
2. A **C1 Hermes lane** behind `USE_HERMES_FOR_C1`, same pattern as the
   Phase 1 flag for L2/L3. C1 earns plan/act/reflect cost (unlike L2/L3
   where the Hermes lane is mostly a smoke test).

## Skills surface

Layer order inside `Orchestrator._handle_locked`:

```
rule layer  ──▶  skill registry  ──▶  daily budget  ──▶  router  ──▶  tier dispatch
   (#1)          (#1.5, Phase 2)         (R4)           (LLMs)
```

A skill hit returns immediately with `handled_by="skill:<name>"` —
**no router, no LLM, no cloud tokens**. Skills are side-effect-light by
contract (doc: `src/skills/base.py`); anything that needs real reasoning
belongs in the router path.

### Built-in skills (Phase 2)

| Skill | Pattern | Does |
|-------|---------|------|
| `hybrid-status` | `/hybrid-status` | Dumps flag state, skill count, heavy-session size — one-stop dev-facing health view |
| `hybrid-budget` | `/hybrid-budget` | Reads `Repository.used_tokens_today`; reports used / cap / remaining (R4 ledger) |
| `hybrid-memo`   | `/memo save\|list\|clear [...]` | Per-user short notes via `MemoryBackend` |

### Contract pieces

- `Skill` ABC — `match()` (sync, regex/prefix) + `invoke()` (async).
- `SkillContext` — bag of handles (`settings`, `repo`, `memory`,
  `orchestrator`, `user_id`, `session_id`). Permissive on purpose so
  new skills don't churn the signature.
- `SkillRegistry` — ordered list; first match wins. Registration order
  is cosmetic for disjoint skills, load-bearing for overlapping ones.
- `default_registry()` — production factory (the three skills above,
  in that order).

### Memory backend (`src/memory/`)

- `MemoryBackend` Protocol: `save`, `list_memos`, `clear`.
- `InMemoryMemory` — default, process-local, asyncio-locked. Cleared on
  bot restart; fine for Phase 2.
- Hard cap: 2000 chars per note (`MemoryTooLarge` on overflow).
- Phase 3 will swap in a Hermes-native memory backend without changing
  the `hybrid-memo` skill.

### Skill exception policy

If `Skill.invoke` raises, the Orchestrator renders
`⚠️ skill \`<name>\` failed: \`<ExceptionType>\`` and marks the task
`degraded=True`. Skills should handle their own expected errors and
return a user-readable string (e.g. `MemoryTooLarge` → "⚠️ memo too
large"); unexpected exceptions degrade rather than crash the turn.
Regression-tested via `tests/test_skills.py::test_skill_exception_renders_warning`.

## C1 Hermes lane (`USE_HERMES_FOR_C1`)

Default: **false**. Controls whether C1 traffic routes through
HermesAdapter v2 or the legacy direct-OpenAI path.

- **false**: C1 calls `OpenAIClient.generate(...)` directly with
  `openai_model` (gpt-4o). Current behavior, unchanged.
- **true**: C1 runs through `HermesAdapter.run(...)` with:
  - `provider="openai"` pinned (FIX#1 type-level + FIX#5 runtime guarantee)
  - `model=openai_model` (**not** the surrogate — C1 is planning tier)
  - `max_turns=hermes_max_turns` (full budget; plan/act/reflect earns
    its keep here unlike L2/L3 which clamp at 5)
  - `handled_by="cloud-gpt-hermes"` in the task record

Critical invariant: **Claude is structurally unreachable from C1.** The
Router never emits `provider="claude-code"` (FIX#1 Literal type), and
the adapter raises `HermesProviderMismatch` if Hermes drifts to anything
other than the requested provider (FIX#5). Claude remains gated behind
the `!heavy` opt-in.

Bump prefix (FIX#2 breadcrumb) is prepended to the Hermes query on
retries, same as Phase 1 — feature parity across the two flags.

## Observability — Phase 2 events

| Event | When | Key fields |
|-------|------|------------|
| `task.end` (extended) | every turn | new `handled_by` values: `skill:<name>`, `cloud-gpt-hermes` |
| `skill.error` | skill raised | `skill`, `err` |

## Phase 2 exit gate

Flipping `USE_HERMES_FOR_C1=true` globally is allowed only after:

- [ ] Phase 1 exit gate already met (Hermes lane for L2/L3 stable).
- [ ] Full test suite green: **≥ 133 passing** (current count after Phase 2).
- [ ] Latency: p95 for C1 turns ≤ 2× direct-OpenAI baseline over 30+ samples.
- [ ] 24h live run with flag on for self only, zero
      `hermes.provider_mismatch` / `hermes.budget_exceeded` events on C1.
- [ ] No `provider=claude-code` events recorded on any auto-escalation path.

---

# Phase 2b — Heavy through Hermes + persistent memos

Phase 2b extends the reversible-flag migration into the heavy lane and
closes a UX gap Phase 2 explicitly called out: memos don't survive
restart. Two things land together:

1. **`USE_HERMES_FOR_HEAVY`** — heavy (`!heavy`) routes through Hermes
   with `provider="claude-code"` pinned. Same Phase 1/2 flag pattern.
2. **`SqliteMemory`** — durable `MemoryBackend` backed by SQLite,
   drop-in compatible with `InMemoryMemory`.

## Heavy-via-Hermes lane (`USE_HERMES_FOR_HEAVY`)

Default: **false**. Controls whether the heavy path runs through
HermesAdapter v2 or the legacy `ClaudeCodeAdapter`.

- **false**: `_handle_heavy` → `_run_c2` → `ClaudeCodeAdapter.run(...)`
  with the FIX#4 session-reuse registry. Unchanged legacy path.
- **true**: `_handle_heavy` → `_run_c2` → `_run_c2_via_hermes` →
  `HermesAdapter.run(..., provider="claude-code", ...)`. Hermes owns
  plan/act/reflect; Claude is the reasoning step.

Session reuse (FIX#4) works symmetrically in both paths — the
`HeavySessionRegistry` is shared. The Hermes lane passes prior session
ids via `resume_session=`; on any Hermes error during a resume, we
invalidate the registry entry and retry fresh once (same
fail-closed-then-retry shape as the legacy `ClaudeCodeResumeFailed`
handler). First-turn (no prior sid) errors propagate to
`_handle_heavy`'s error-rendering block instead of silently retrying.

Error-to-tag mapping on the heavy path:

| Exception | handled_by | User message |
|-----------|------------|--------------|
| `ClaudeCodeAuthError` | `claude-auth` | quota/OAuth hint |
| `ClaudeCodeTimeout`   | `claude-timeout` | timeout (legacy path) |
| `ClaudeCodeAdapterError` | `claude-error` | generic failure |
| `HermesAuthError` (Phase 2b) | `hermes-auth` | OAuth hint, same remediation |
| `HermesTimeout` (Phase 2b) | `claude-timeout` | timeout (via Hermes) |
| `HermesAdapterError` (Phase 2b) | `claude-error` | generic failure (via Hermes) |

Critical invariant: **Claude is still reached only via `!heavy`.** The
Router's `Provider = Literal["ollama","openai"]` (FIX#1) excludes
`claude-code` from auto-escalation, so even with the heavy flag on,
there's no path from L2/L3/C1 retries into Claude.

## Persistent memos (`SqliteMemory`)

`src/memory/sqlite.py` — durable `MemoryBackend` backed by aiosqlite.
Same interface as `InMemoryMemory`, same validation (2k-char cap, empty
rejection), same insertion-order `list_memos` semantics. Notes survive
bot restarts.

Storage layout: shares the Repository's SQLite file by default
(`settings.state_db_path`) with a dedicated `memos` table. Schema is
self-contained (`CREATE TABLE IF NOT EXISTS` + auto-schema on every
write) so the backend is drop-in and co-exists cleanly with the existing
`tasks` / `budget_daily` tables. To activate, swap the Orchestrator's
default `memory=InMemoryMemory()` for `memory=SqliteMemory(settings.state_db_path)`
at the Discord entry point — the `MemoryBackend` Protocol makes this
a one-line change.

The existing `/memo save|list|clear` skill works against either backend
unchanged (tested against both in `tests/test_memory*`).

## Observability — Phase 2b events

| Event | When | Key fields |
|-------|------|------------|
| `heavy.hermes_resume_failed` | Hermes errored on a resume-session call | `user_id`, `old_session_id`, `reason` |
| `hermes.auth_error_on_heavy` | `HermesAuthError` on the heavy lane | `err` |
| `hermes.timeout_on_heavy` | `HermesTimeout` on the heavy lane | `err` |
| `hermes.error_on_heavy` | other `HermesAdapterError` on heavy | `err` |
| `task.end` (extended) | every turn | new `handled_by` values: `claude-max-hermes`, `hermes-auth` (heavy) |

## Phase 2b exit gate

Flipping `USE_HERMES_FOR_HEAVY=true` globally is allowed only after:

- [ ] Phase 2 exit gate already met (C1 Hermes lane stable).
- [ ] Full test suite green: **≥ 147 passing** (current count after Phase 2b).
- [ ] Hermes CLI in the deployed build verified to support
      `--provider claude-code` end-to-end (single manual smoke run —
      gives output and a session_id).
- [ ] 24h live run with flag on for self only, zero
      `hermes.provider_mismatch` / `hermes.budget_exceeded` events on
      the heavy lane.
- [ ] At least one `heavy.session_choice=reused` observed after flipping
      the flag (confirms FIX#4 still works through the Hermes path).
- [ ] `SqliteMemory` wired into Discord entry point and confirmed to
      round-trip notes across a restart.

---

# Phase 3 — Master switch, validator trust, MCP surface

Phase 3 lands the operational consolidation pieces. Three shippable
items plus one deferred SDK migration:

1. **`USE_HERMES_EVERYWHERE`** — master switch that implies all three
   per-phase flags (Phase 1/2/2b) via `effective_*` properties.
2. **`TRUST_HERMES_REFLECTION`** — validator short-circuit for
   multi-turn Hermes outputs (turns_used ≥ 2).
3. **`src/mcp/`** — SDK-free JSON-RPC 2.0 MCP server that exposes
   `hybrid.handle` to external clients.
4. **Discord gateway uses `SqliteMemory`** — memos now survive restart
   in production (Phase 2b deferred item closed).

## The master switch

`Settings.use_hermes_everywhere` is OR-ed into each per-phase flag via
properties the orchestrator reads:

```python
effective_use_hermes_for_local = use_hermes_for_local or use_hermes_everywhere
effective_use_hermes_for_c1    = use_hermes_for_c1    or use_hermes_everywhere
effective_use_hermes_for_heavy = use_hermes_for_heavy or use_hermes_everywhere
```

Rollout story: operators stage the transition with per-phase flags
during each phase's exit gate; when all three hold, flip
`USE_HERMES_EVERYWHERE=true` as the single operational lever.
Per-phase flags continue to work as overrides while the master is off.
`/hybrid-status` now shows both raw and effective flag values so
operators can verify overrides at a glance.

## Validator reflection trust

`Settings.trust_hermes_reflection` gates a short-circuit in
`Validator.validate(...)`:

| Pre-condition | Validator result |
|---------------|------------------|
| flag off | unchanged (all existing checks run) |
| flag on & `turns_used < 2` | unchanged (single-turn gets full checks) |
| flag on & timeout/tool_error/empty | **still fails** |
| flag on & `expected_schema="json"` & malformed JSON | **still fails** |
| flag on & `turns_used ≥ 2` & text non-empty & JSON valid (if asked) | **pass** (`reason="hermes reflection trusted (turns=N)"`) |

Rationale: once Hermes has completed ≥ 2 plan/act/reflect cycles and
produced non-empty output that satisfies the structural contract,
second-guessing it on subjective quality markers (refusal-looking
phrases, short length, suspected repetition) is counter-productive —
Hermes' own reflection already saw and kept the output. We keep the
hard contracts (no timeouts, no empty, no JSON violations) because
those aren't subjective.

This knob is strictly off by default. Exit gate: measure reflection
quality against the current validator in live traffic first (Phase 3b
gate).

## MCP server (`src/mcp/`)

SDK-free JSON-RPC 2.0 implementation with three methods
(`initialize`, `tools/list`, `tools/call`) and one tool (`hybrid.handle`).

Design principles:

- **No new dependency.** Official MCP Python SDK is ~300 KB of transitive
  deps; we use the stdlib `json` module and a hand-rolled dispatch.
  Swap to the SDK in Phase 4 once the surface grows past three methods.
- **Transport-agnostic core.** `HybridMCPServer.handle_request` takes
  and returns dicts — unit tests round-trip requests with zero stdio
  plumbing. `run_stdio()` wraps it with line-delimited JSON framing
  (the canonical MCP stdio transport).
- **One-tool surface.** `hybrid.handle` is enough to prove the wiring.
  Phase 4 adds `hybrid.memo.*`, `hybrid.status`, and possibly
  `hybrid.retry` once the shape is verified.

Error mapping:

| Case | JSON-RPC code |
|------|---------------|
| invalid `jsonrpc` version | -32600 (Invalid request) |
| unknown method | -32601 (Method not found) |
| unknown tool / bad args | -32602 (Invalid params) |
| malformed JSON line (stdio) | -32700 (Parse error) |
| unexpected server exception | -32000 (Implementation-defined) |

The orchestrator's `degraded` flag surfaces to MCP clients as
`result.isError = true` alongside the response text.

## Persistent memos in production

`src/gateway/discord_bot.py` now instantiates
`SqliteMemory(settings.state_db_path)` and passes it to the
Orchestrator. The `/memo` skill gets durable storage for free — the
`MemoryBackend` Protocol makes this a one-line swap.

## Observability — Phase 3 events

| Event | When | Key fields |
|-------|------|------------|
| `mcp.error` | any handled MCP error response | `method`, `code`, `err` |
| `mcp.unhandled` | unexpected server exception | `method` |
| `mcp.stdio.start` | stdio transport comes up | — |
| `task.end` (extended) | validator reflection trust took effect | `verdict.reason="hermes reflection trusted (turns=N)"` |

## Phase 3 exit gate

Flipping `USE_HERMES_EVERYWHERE=true` and `TRUST_HERMES_REFLECTION=true`
globally is allowed only after:

- [ ] Phase 2b exit gate already met (all per-phase Hermes lanes stable).
- [ ] Full test suite green: **≥ 169 passing** (current count after Phase 3).
- [ ] 48h live run with `USE_HERMES_EVERYWHERE=true` for self only,
      zero auth / budget / provider-mismatch events on any lane.
- [ ] Reflection-trust measurement: over 100+ samples on Hermes lanes,
      the "trust" path pass rate matches or beats the legacy validator
      within 2% (no silent quality regression).
- [ ] MCP server reachable from at least one external client
      (Claude Desktop / inspector / equivalent) end-to-end.

## Phase 4 sketch (not started)

- Swap hand-rolled MCP for the official Python SDK once the surface
  grows past the three-method Phase 3 set.
- Expose `hybrid.memo.save`, `hybrid.memo.list`, `hybrid.status` as
  separate MCP tools alongside `hybrid.handle`.
- Stream orchestrator progress events over MCP's subscription channel
  so clients see tier escalations / retries in flight.
- Remove `OpenAIClient` / `AnthropicClient` / `OllamaClient` imports
  from the Orchestrator once `USE_HERMES_EVERYWHERE` has been on for
  a stable window. At that point the direct-client lanes are dead code.

