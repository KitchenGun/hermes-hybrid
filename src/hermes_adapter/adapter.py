"""Adapter that wraps the official NousResearch Hermes Agent.

Backend: WSL2 subprocess invocation of `hermes chat -q "..." -Q`.
The agent runs its own plan/act/reflect loop inside and writes a session
JSON file to ~/.hermes/sessions/session_<id>.json which we read back to
populate the hermes_trace (R1).

Concurrency is capped by a module-level asyncio.Semaphore (R13) so even
high Discord load can't fork unbounded WSL processes.

## v2 contract (FIX#5)

v2 strengthens the adapter output so the orchestrator can enforce R1
(no silent provider fallback) and R2 (no turn-budget bypass) at runtime:

- ``provider_requested`` / ``provider_actual`` — the caller's asked-for
  provider vs. what Hermes actually used. Mismatch raises
  :class:`HermesProviderMismatch` so Max can never be reached via a
  sneaky Hermes fallback.
- ``turns_used`` — how many plan/act/reflect iterations Hermes consumed,
  surfaced for caller-side budget accounting.
- ``models_used`` / ``primary_model`` — which local/cloud models Hermes
  picked internally (FIX#3). ``primary_model`` is logged via
  ``hermes.model_selected`` so Hermes' internal routing is observable.

All v2 fields default to sensible values (provider_requested, no
modelUsage rows, etc.) so pre-v2 Hermes builds that don't populate
these in the session JSON still work — we just can't verify R1 when
the ``provider`` field is missing from the session. We trust in that
case and rely on the ``--no-fallback`` CLI flag to enforce the
invariant upstream.
"""
from __future__ import annotations

import asyncio
import json
import re
import shlex
from dataclasses import dataclass, field
from typing import Any, Literal

from src.config import Settings
from src.obs import get_logger

log = get_logger(__name__)


class HermesAdapterError(RuntimeError):
    pass


class HermesTimeout(HermesAdapterError):
    pass


class HermesAuthError(HermesAdapterError):
    pass


class HermesProviderMismatch(HermesAdapterError):
    """R1: requested provider ≠ actual provider used by Hermes.

    Raised when the Hermes session JSON reports a different provider
    than the one we pinned via ``--provider``. This is a fail-closed
    guardrail: if Hermes ever silently falls back to a cheaper/more
    expensive provider we do NOT want to return the response — the
    orchestrator should surface the error and halt this route.
    """

    def __init__(self, requested: str, actual: str):
        self.requested = requested
        self.actual = actual
        super().__init__(f"Hermes provider mismatch: requested={requested!r} actual={actual!r}")


class HermesBudgetExceeded(HermesAdapterError):
    """R2: Hermes consumed more turns / budget than the caller allowed.

    Signals that the ``--max-turns`` / ``--max-budget-usd`` guardrail
    was breached. Since Hermes is the execution engine that drives
    Claude, budget overruns here are the primary defense against
    runaway Max usage.
    """

    def __init__(self, kind: Literal["turns", "usd"], used: float, cap: float):
        self.kind = kind
        self.used = used
        self.cap = cap
        super().__init__(f"Hermes budget exceeded: {kind}={used} > cap={cap}")


class HermesMalformedResult(HermesAdapterError):
    """Hermes output violated the expected JSON contract.

    Raised when the session JSON is present but is missing required
    fields, or when stdout claims success but no text came back. The
    orchestrator should treat this like a ``tool_error`` and retry
    per the validator ladder.
    """


@dataclass
class HermesResult:
    """v2 output contract (FIX#5).

    Backward-compat fields (used by existing call sites):
      text, session_id, tier_used, model_name, provider, duration_ms,
      stdout_raw, stderr_raw, prompt_tokens, completion_tokens, trace

    v2 fields (new, populated from session JSON when available):
      provider_requested, provider_actual, models_used, primary_model,
      turns_used, skills_invoked, mcp_tools_invoked, total_cost_usd,
      raw_json
    """

    text: str
    session_id: str | None
    tier_used: Literal["C1", "C2"]
    model_name: str
    provider: str
    duration_ms: int
    stdout_raw: str
    stderr_raw: str
    prompt_tokens: int = 0
    completion_tokens: int = 0
    # Parsed session trace (R1): best-effort plan/act/reflect reconstruction.
    trace: dict[str, Any] = field(default_factory=dict)

    # ---- v2 contract fields (FIX#5) ----
    provider_requested: str = ""
    provider_actual: str = ""
    models_used: list[str] = field(default_factory=list)
    primary_model: str = ""
    turns_used: int = 0
    skills_invoked: list[str] = field(default_factory=list)
    mcp_tools_invoked: list[str] = field(default_factory=list)
    total_cost_usd: float = 0.0
    raw_json: dict[str, Any] = field(default_factory=dict)


_STDOUT_SESSION_RE = re.compile(r"session[_\s-]*id[:=]\s*([\w\-]+)", re.IGNORECASE)
_AUTH_ERROR_RE = re.compile(r"(401|unauthorized|authentication failed)", re.IGNORECASE)


class HermesAdapter:
    def __init__(self, settings: Settings):
        self.settings = settings
        self._sem = asyncio.Semaphore(max(1, settings.hermes_concurrency))

    # ---- public ----

    async def run(
        self,
        query: str,
        *,
        model: str | None,
        provider: str | None,
        resume_session: str | None = None,
        max_turns: int | None = None,
        extra_args: list[str] | None = None,
        timeout_ms: int | None = None,
        profile: str | None = None,
        preload_skills: list[str] | None = None,
    ) -> HermesResult:
        """Execute one Hermes turn. Never silently re-maps model/provider.

        Callers (orchestrator) are responsible for correct tier → (model,
        provider) mapping. We do NOT fall back to default model when something
        looks off — that was the R2/R9 bug.

        v2: after the subprocess returns, verify ``provider_actual`` against
        the requested provider and raise :class:`HermesProviderMismatch` on
        divergence. Also surface ``turns_used`` / ``models_used`` /
        ``primary_model`` so the caller can enforce R2 and log FIX#3.
        """
        max_turns = max_turns or self.settings.hermes_max_turns
        timeout_ms = timeout_ms or self.settings.hermes_timeout_ms
        cmd = self._build_cmd(
            query=query,
            model=model,
            provider=provider,
            resume_session=resume_session,
            max_turns=max_turns,
            extra_args=extra_args or [],
            profile=profile,
            preload_skills=preload_skills or [],
        )

        async with self._sem:
            start = asyncio.get_event_loop().time()
            try:
                proc = await asyncio.create_subprocess_exec(
                    *cmd,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                )
                try:
                    stdout_b, stderr_b = await asyncio.wait_for(
                        proc.communicate(), timeout=timeout_ms / 1000.0
                    )
                except asyncio.TimeoutError:
                    proc.kill()
                    await proc.wait()
                    raise HermesTimeout(f"Hermes timed out after {timeout_ms}ms")
            except FileNotFoundError as e:
                raise HermesAdapterError(f"Hermes backend not available: {e}") from e

        duration_ms = int((asyncio.get_event_loop().time() - start) * 1000)
        stdout = stdout_b.decode("utf-8", errors="replace")
        stderr = stderr_b.decode("utf-8", errors="replace")

        if proc.returncode != 0:
            if _AUTH_ERROR_RE.search(stdout + stderr):
                raise HermesAuthError(f"Hermes auth failed (model={model}, provider={provider})")
            raise HermesAdapterError(
                f"Hermes exited {proc.returncode} (model={model}, provider={provider})\n"
                f"stderr: {stderr[-500:]}"
            )

        text, session_id = self._parse_stdout(stdout)
        trace: dict[str, Any] = {}
        raw_json: dict[str, Any] = {}
        if session_id:
            raw_json = await self._load_session_json(session_id)
            trace = self._project_trace(raw_json) if raw_json else {}

        model_for_tier = (model or "").lower()
        tier: Literal["C1", "C2"] = "C2" if "claude" in model_for_tier or "opus" in model_for_tier else "C1"

        # ---- v2: extract and verify ----
        v2 = self._extract_v2(
            raw_json,
            requested_provider=provider or "",
            requested_model=model or "",
        )

        # R1 enforcement: if Hermes reported an actual provider and it's
        # different from what we pinned, fail-closed. When ``provider`` is
        # None (caller deferred to the profile's config.yaml) OR
        # ``provider=="auto"`` (Hermes-side auto-selection), we skip this
        # check — by definition the caller accepted whatever Hermes chose.
        if (
            provider
            and provider != "auto"
            and v2["provider_actual"]
            and v2["provider_actual"] != provider
            # Accept provider aliases that Hermes might normalize (e.g., "openai" vs "openai-chat")
            and not _providers_compatible(provider, v2["provider_actual"])
        ):
            raise HermesProviderMismatch(requested=provider, actual=v2["provider_actual"])

        # R2 enforcement: if Hermes exposed turns_used and it exceeded the
        # cap we passed in, fail-closed (this catches --max-turns bypass
        # regressions in the Hermes CLI).
        if v2["turns_used"] > max_turns:
            raise HermesBudgetExceeded(kind="turns", used=v2["turns_used"], cap=max_turns)

        # FIX#3: log Hermes' internal model choice so we can see whether
        # Hermes escalated to a bigger local model (qwen 7b → 14b → 32b)
        # or used a cloud surrogate under the hood.
        log.info(
            "hermes.model_selected",
            primary_model=v2["primary_model"] or (model or ""),
            models_used=v2["models_used"],
            provider_requested=provider or "",
            provider_actual=v2["provider_actual"] or (provider or ""),
            turns_used=v2["turns_used"],
            session_id=session_id,
        )

        return HermesResult(
            text=text,
            session_id=session_id,
            tier_used=tier,
            model_name=model or (v2["primary_model"] or ""),
            provider=provider or (v2["provider_actual"] or ""),
            duration_ms=duration_ms,
            stdout_raw=stdout,
            stderr_raw=stderr,
            trace=trace,
            prompt_tokens=v2["prompt_tokens"],
            completion_tokens=v2["completion_tokens"],
            # v2 fields
            provider_requested=provider or "",
            provider_actual=v2["provider_actual"] or (provider or ""),
            models_used=v2["models_used"],
            primary_model=v2["primary_model"] or (model or ""),
            turns_used=v2["turns_used"],
            skills_invoked=v2["skills_invoked"],
            mcp_tools_invoked=v2["mcp_tools_invoked"],
            total_cost_usd=v2["total_cost_usd"],
            raw_json=raw_json,
        )

    # ---- internals ----

    def _build_cmd(
        self,
        *,
        query: str,
        model: str | None,
        provider: str | None,
        resume_session: str | None,
        max_turns: int,
        extra_args: list[str],
        profile: str | None = None,
        preload_skills: list[str] | None = None,
    ) -> list[str]:
        # Per the official Hermes CLI reference: ``-p/--profile <name>`` is
        # a top-level flag that precedes the subcommand, so we need to
        # slot it in BEFORE ``chat`` — not after it like --provider etc.
        # See https://hermes-agent.nousresearch.com/docs/reference/cli-commands
        args: list[str] = [self.settings.hermes_cli_path]
        if profile:
            args += ["-p", profile]
        args += [
            "chat",
            "-q", query,
            "-Q",
            "--max-turns", str(max_turns),
        ]
        # ``-m`` and ``--provider`` are optional: when the caller passes
        # ``None`` we let the profile's own ``config.yaml`` drive model
        # and provider selection. Custom providers defined in the profile
        # (e.g. ``ollama-local``) are NOT valid ``--provider`` argparse
        # choices, so leaving the flag off is the documented way to opt
        # into a profile-defined backend.
        if model:
            args += ["-m", model]
        if provider:
            args += ["--provider", provider]
        # ``-s/--skills`` preloads a skill for the session. Repeatable, so
        # we emit one flag per entry for unambiguous parsing.
        for skill_name in (preload_skills or []):
            args += ["-s", skill_name]
        if resume_session:
            args += ["--resume", resume_session]
        args += extra_args

        if self.settings.hermes_cli_backend == "wsl_subprocess":
            inner = " ".join(shlex.quote(a) for a in args)
            return ["wsl", "-d", self.settings.hermes_wsl_distro, "bash", "-lc", inner]
        if self.settings.hermes_cli_backend == "local_subprocess":
            return args
        raise HermesAdapterError(f"Unsupported backend: {self.settings.hermes_cli_backend}")

    def _parse_stdout(self, stdout: str) -> tuple[str, str | None]:
        """Pull session_id (first line `session_id: xxx`) and the final text."""
        session_id: str | None = None
        lines = stdout.splitlines()
        kept: list[str] = []
        for ln in lines:
            m = _STDOUT_SESSION_RE.search(ln)
            if m and session_id is None:
                session_id = m.group(1)
                continue
            kept.append(ln)
        text = "\n".join(kept).strip()

        # Future-proofing: detect JSON wrapper
        if text.startswith("{") and text.endswith("}"):
            try:
                obj = json.loads(text)
                if isinstance(obj, dict) and "response" in obj:
                    return str(obj["response"]), session_id or obj.get("session_id")
            except json.JSONDecodeError:
                pass
        return text, session_id

    async def _load_session_json(self, session_id: str) -> dict[str, Any]:
        """Read the raw session JSON so v1 trace projection and v2 field
        extraction can share one file fetch."""
        session_path = f"{self.settings.hermes_home}/sessions/session_{session_id}.json"
        if self.settings.hermes_cli_backend == "wsl_subprocess":
            cmd = ["wsl", "-d", self.settings.hermes_wsl_distro, "bash", "-lc", f"cat {shlex.quote(session_path)}"]
        else:
            cmd = ["cat", session_path]

        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
            )
            out, _err = await asyncio.wait_for(proc.communicate(), timeout=10.0)
        except (asyncio.TimeoutError, FileNotFoundError):
            return {}
        if proc.returncode != 0:
            return {}
        try:
            return json.loads(out.decode("utf-8", errors="replace"))
        except json.JSONDecodeError:
            return {}

    # Kept as a thin alias for tests that predate v2 (test_hermes_adapter).
    async def _load_trace(self, session_id: str) -> dict[str, Any]:
        raw = await self._load_session_json(session_id)
        return self._project_trace(raw) if raw else {}

    @staticmethod
    def _project_trace(session_json: dict[str, Any]) -> dict[str, Any]:
        messages = session_json.get("messages", []) or []
        actions: list[dict[str, Any]] = []
        observations: list[dict[str, Any]] = []
        reflections: list[dict[str, Any]] = []
        goal = ""

        for m in messages:
            role = m.get("role")
            content = m.get("content")
            if role == "user" and isinstance(content, str) and not goal:
                goal = content[:300]
            if role == "assistant":
                tool_calls = m.get("tool_calls") or []
                for tc in tool_calls:
                    fn = (tc.get("function") or {}) if isinstance(tc, dict) else {}
                    actions.append({"tool": fn.get("name", "?"), "args_preview": str(fn.get("arguments", ""))[:200]})
                if isinstance(content, str) and content.strip():
                    reflections.append({"text": content.strip()[:300]})
            if role == "tool":
                preview = ""
                if isinstance(content, str):
                    preview = content[:200]
                elif isinstance(content, list):
                    preview = json.dumps(content)[:200]
                observations.append({
                    "tool": m.get("name", "?"),
                    "ok": True,  # Hermes doesn't mark failures in-band; best-effort
                    "preview": preview,
                })

        return {
            "session_id": session_json.get("session_id"),
            "model": session_json.get("model"),
            "message_count": session_json.get("message_count", len(messages)),
            "plan": {"goal": goal, "tools_declared": len(session_json.get("tools") or [])},
            "actions": actions,
            "observations": observations,
            "reflections": reflections,
        }

    @staticmethod
    def _extract_v2(
        session_json: dict[str, Any],
        *,
        requested_provider: str,
        requested_model: str,
    ) -> dict[str, Any]:
        """FIX#5 + FIX#3: pull v2 contract fields out of the Hermes session JSON.

        Expected (but all-optional) schema:

        .. code-block:: json

            {
              "provider": "ollama",
              "modelUsage": [
                {"model": "qwen2.5:7b-instruct", "turns": 2,
                 "prompt_tokens": 120, "completion_tokens": 80,
                 "cost_usd": 0.0}
              ],
              "turns_used": 2,
              "skills_invoked": ["hybrid-status"],
              "mcp_tools_invoked": ["fetch.get"],
              "total_cost_usd": 0.0
            }

        Missing fields are filled with safe defaults so pre-v2 Hermes builds
        don't explode — they just can't be R1-verified.
        """
        if not session_json:
            return {
                "provider_actual": "",
                "models_used": [],
                "primary_model": "",
                "turns_used": 0,
                "skills_invoked": [],
                "mcp_tools_invoked": [],
                "total_cost_usd": 0.0,
                "prompt_tokens": 0,
                "completion_tokens": 0,
            }

        provider_actual = str(session_json.get("provider", "") or "")

        model_usage = session_json.get("modelUsage") or []
        if not isinstance(model_usage, list):
            model_usage = []

        models_used: list[str] = []
        primary_model = ""
        primary_turns = -1
        total_prompt = 0
        total_completion = 0
        total_cost = float(session_json.get("total_cost_usd", 0.0) or 0.0)
        total_turns_from_usage = 0
        for row in model_usage:
            if not isinstance(row, dict):
                continue
            name = str(row.get("model", "") or "")
            if not name:
                continue
            if name not in models_used:
                models_used.append(name)
            turns = int(row.get("turns", 0) or 0)
            total_turns_from_usage += turns
            if turns > primary_turns:
                primary_turns = turns
                primary_model = name
            total_prompt += int(row.get("prompt_tokens", 0) or 0)
            total_completion += int(row.get("completion_tokens", 0) or 0)
            if "cost_usd" in row:
                try:
                    total_cost += float(row["cost_usd"] or 0.0)
                except (TypeError, ValueError):
                    pass

        # Prefer an explicit turns_used field, else sum of modelUsage rows,
        # else fall back to message count as a crude upper bound.
        turns_used = int(
            session_json.get("turns_used")
            or total_turns_from_usage
            or _assistant_turn_count(session_json)
        )

        skills = session_json.get("skills_invoked") or []
        mcp_tools = session_json.get("mcp_tools_invoked") or []

        return {
            "provider_actual": provider_actual,
            "models_used": models_used,
            "primary_model": primary_model,
            "turns_used": turns_used,
            "skills_invoked": [str(s) for s in skills if s],
            "mcp_tools_invoked": [str(t) for t in mcp_tools if t],
            "total_cost_usd": total_cost,
            "prompt_tokens": total_prompt,
            "completion_tokens": total_completion,
        }


def _assistant_turn_count(session_json: dict[str, Any]) -> int:
    msgs = session_json.get("messages") or []
    return sum(1 for m in msgs if isinstance(m, dict) and m.get("role") == "assistant")


# Providers Hermes might spell differently than we pin. Add pairs that we
# want to treat as equivalent so we don't raise HermesProviderMismatch on
# harmless string normalization.
_PROVIDER_ALIASES: dict[str, set[str]] = {
    "openai": {"openai", "openai-chat", "openai_chat"},
    "ollama": {"ollama", "ollama-chat", "ollama_local"},
}


def _providers_compatible(requested: str, actual: str) -> bool:
    req = requested.lower().strip()
    act = actual.lower().strip()
    if req == act:
        return True
    for canonical, aliases in _PROVIDER_ALIASES.items():
        if req in aliases and act in aliases:
            return True
    return False
