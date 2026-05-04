"""CloudPolicy — gate every cloud / Claude call before it fires.

The dispatcher consults :meth:`CloudPolicy.evaluate` BEFORE invoking
any cloud-side adapter. The policy returns a :class:`PolicyVerdict`
that says either:

  * **allow** — fire the call (and update counters).
  * **deny**  — skip this step (e.g., hourly cap reached); dispatcher
                tries the next candidate.
  * **needs_approval** — pause for the user (Discord button); the
                dispatcher hands the verdict + step description back
                up to the orchestrator, which uses the existing HITL
                infrastructure.

What the policy enforces:

  1. **Rate caps** — per-hour and per-day call counters per provider.
     Sliding-window via timestamp deque; reset is implicit.
  2. **Cost estimation** — uses ``ModelEntry.cost_input_per_1m`` /
     ``cost_output_per_1m`` against estimated tokens; checks against
     a daily USD cap.
  3. **Approval threshold** — `JobType.requires_user_approval` flag,
     plus optional auto-approval-needed conditions (e.g., estimated
     cost above $X, large prompt).

The policy keeps state in-process (no DB persistence in v1). On bot
restart, hourly/daily counters reset — that's a known limitation but
acceptable because the orchestrator already enforces a separate global
daily token budget at the SQLite level (`Repository.add_tokens` /
`used_tokens_today`).
"""
from __future__ import annotations

import logging
import time
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal

import yaml

from src.job_factory.registry import JobType, ModelEntry

log = logging.getLogger(__name__)


PolicyOutcome = Literal["allow", "deny", "needs_approval"]


@dataclass(frozen=True)
class PolicyVerdict:
    """Result of :meth:`CloudPolicy.evaluate`.

    Attributes:
        outcome: ``allow`` / ``deny`` / ``needs_approval``.
        reason: One-line human-readable explanation. Always populated.
        estimated_cost_usd: Best-effort cost estimate for this call.
            0.0 for free providers (Claude CLI Max OAuth, local).
        triggered_rule: Which rule decided (e.g., ``"hourly_cap"``,
            ``"daily_cost"``, ``"requires_user_approval"``). Empty on
            allow.
    """

    outcome: PolicyOutcome
    reason: str
    estimated_cost_usd: float = 0.0
    triggered_rule: str = ""


# ---- counters -------------------------------------------------------------


_SECONDS_PER_HOUR = 3600
_SECONDS_PER_DAY = 86400


class _SlidingCounter:
    """Tracks events with timestamps in a deque.

    O(1) amortized add (timestamps accumulate), O(k) prune where k is
    expired entries — much smaller than total in steady-state.
    """

    def __init__(self, window_seconds: int):
        self._window = window_seconds
        self._events: deque[float] = deque()

    def record(self, ts: float | None = None) -> None:
        self._events.append(ts if ts is not None else time.time())

    def count(self, *, now: float | None = None) -> int:
        cutoff = (now if now is not None else time.time()) - self._window
        while self._events and self._events[0] < cutoff:
            self._events.popleft()
        return len(self._events)


# ---- config ---------------------------------------------------------------


@dataclass
class CloudPolicyConfig:
    """Knobs loaded from ``config/cloud_policy.yaml``.

    All caps are inclusive: e.g., ``claude_auto_calls_per_hour=10`` means
    the 11th call is denied. Setting any cap to 0 disables that check.

    2026-05-04: OpenAI rate caps removed when API legacy was purged.
    Claude CLI is the only cloud lane.
    """

    # Claude CLI rate caps. Auto-triggered Claude (Job Factory escalation,
    # cron prompt wrap, bench LLMJudge) differs from manual `!heavy`
    # (separate counters in the legacy orchestrator).
    claude_auto_calls_per_hour: int = 10
    claude_auto_calls_per_day: int = 50

    # Per-process cap on simultaneous Claude CLI sessions (Max session
    # contention guard).
    claude_cli_concurrent_max: int = 3

    # USD caps (input + output combined, estimated from cost_per_1m).
    daily_usd_cap: float = 5.0

    # Approval thresholds — over these triggers needs_approval regardless
    # of JobType.requires_user_approval.
    approval_estimated_tokens_above: int = 10_000
    approval_estimated_cost_above_usd: float = 0.50

    # Token-count heuristics for estimation when adapter doesn't report
    # actual tokens upfront. ~4 chars per token is the OpenAI ballpark.
    estimated_chars_per_token: int = 4
    # If a request body has no message, assume 200 tokens minimum so
    # we don't under-count.
    minimum_estimated_input_tokens: int = 200
    estimated_output_tokens: int = 500

    @classmethod
    def from_yaml(cls, path: Path) -> "CloudPolicyConfig":
        if not path.exists():
            return cls()  # default config — file is optional
        try:
            data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        except yaml.YAMLError as e:
            log.warning("policy.yaml.parse_failed", extra={"err": str(e)})
            return cls()
        if not isinstance(data, dict):
            return cls()

        def _i(key: str, default: int) -> int:
            v = data.get(key, default)
            return int(v) if isinstance(v, (int, float)) else default

        def _f(key: str, default: float) -> float:
            v = data.get(key, default)
            return float(v) if isinstance(v, (int, float)) else default

        return cls(
            claude_auto_calls_per_hour=_i("claude_auto_calls_per_hour", 10),
            claude_auto_calls_per_day=_i("claude_auto_calls_per_day", 50),
            claude_cli_concurrent_max=_i("claude_cli_concurrent_max", 3),
            daily_usd_cap=_f("daily_usd_cap", 5.0),
            approval_estimated_tokens_above=_i(
                "approval_estimated_tokens_above", 10_000
            ),
            approval_estimated_cost_above_usd=_f(
                "approval_estimated_cost_above_usd", 0.50
            ),
            estimated_chars_per_token=_i("estimated_chars_per_token", 4),
            minimum_estimated_input_tokens=_i(
                "minimum_estimated_input_tokens", 200
            ),
            estimated_output_tokens=_i("estimated_output_tokens", 500),
        )


# ---- CloudPolicy ----------------------------------------------------------


class CloudPolicy:
    """Stateful gate for cloud / Claude calls.

    Construct one per dispatcher. The policy holds in-process counters,
    so concurrent dispatchers in the same process must share the same
    instance for caps to be effective.

    Args:
        config: Configuration. Defaults if not supplied.
        clock: Optional clock function (returns float seconds). Useful
            for tests; defaults to ``time.time``.
    """

    def __init__(
        self,
        *,
        config: CloudPolicyConfig | None = None,
        clock=time.time,
    ):
        self._cfg = config or CloudPolicyConfig()
        self._clock = clock

        self._claude_hour = _SlidingCounter(_SECONDS_PER_HOUR)
        self._claude_day = _SlidingCounter(_SECONDS_PER_DAY)

        # Cost ledger — list of (ts, usd) tuples. We only need a 24h
        # rolling sum so a deque suffices.
        self._cost_day: deque[tuple[float, float]] = deque()

    # ---- core API --------------------------------------------------------

    def evaluate(
        self,
        *,
        job: JobType,
        entry: ModelEntry,
        prompt_text: str = "",
    ) -> PolicyVerdict:
        """Should this call go through?

        Args:
            job: The active JobType (drives ``requires_user_approval``).
            entry: The candidate model arm (drives provider routing +
                cost estimation).
            prompt_text: Concatenated prompt for token estimation. Empty
                string → falls back to ``minimum_estimated_input_tokens``.
        """
        provider = entry.provider

        # 1. Rate caps (only for cloud providers).
        rate_verdict = self._check_rate_caps(provider)
        if rate_verdict is not None:
            return rate_verdict

        # 2. Cost estimation + daily USD cap.
        est_in = self._estimate_input_tokens(prompt_text)
        est_out = self._cfg.estimated_output_tokens
        est_cost = self._estimate_cost(entry, est_in, est_out)

        if self._cfg.daily_usd_cap > 0:
            daily_usd = self._daily_cost_usd()
            if daily_usd + est_cost > self._cfg.daily_usd_cap:
                return PolicyVerdict(
                    outcome="deny",
                    reason=(
                        f"daily USD cap reached "
                        f"({daily_usd:.4f} + {est_cost:.4f} "
                        f"> {self._cfg.daily_usd_cap})"
                    ),
                    estimated_cost_usd=est_cost,
                    triggered_rule="daily_usd_cap",
                )

        # 3. Approval thresholds.
        if job.requires_user_approval:
            return PolicyVerdict(
                outcome="needs_approval",
                reason=f"job_type {job.name!r} requires_user_approval=True",
                estimated_cost_usd=est_cost,
                triggered_rule="job_requires_approval",
            )
        if (
            self._cfg.approval_estimated_tokens_above > 0
            and est_in + est_out > self._cfg.approval_estimated_tokens_above
        ):
            return PolicyVerdict(
                outcome="needs_approval",
                reason=(
                    f"estimated tokens "
                    f"{est_in + est_out} > "
                    f"{self._cfg.approval_estimated_tokens_above}"
                ),
                estimated_cost_usd=est_cost,
                triggered_rule="estimated_tokens_threshold",
            )
        if (
            self._cfg.approval_estimated_cost_above_usd > 0
            and est_cost > self._cfg.approval_estimated_cost_above_usd
        ):
            return PolicyVerdict(
                outcome="needs_approval",
                reason=(
                    f"estimated cost ${est_cost:.4f} > "
                    f"${self._cfg.approval_estimated_cost_above_usd}"
                ),
                estimated_cost_usd=est_cost,
                triggered_rule="estimated_cost_threshold",
            )

        return PolicyVerdict(
            outcome="allow",
            reason=f"within all caps ({provider})",
            estimated_cost_usd=est_cost,
        )

    def record_call(
        self,
        entry: ModelEntry,
        *,
        actual_cost_usd: float | None = None,
    ) -> None:
        """Mark a call as actually fired.

        Pass ``actual_cost_usd`` if known (post-call from token usage);
        otherwise the policy uses the pre-call estimate from a recent
        ``evaluate`` (best-effort — not perfectly accurate)."""
        now = self._clock()
        provider = entry.provider
        if provider == "claude_cli":
            self._claude_hour.record(now)
            self._claude_day.record(now)
        else:
            # Local (ollama) or hermes_profile — no rate counters.
            return
        if actual_cost_usd is not None and actual_cost_usd > 0:
            self._cost_day.append((now, actual_cost_usd))
            self._prune_costs(now)

    # ---- introspection --------------------------------------------------

    def stats(self) -> dict[str, Any]:
        """Snapshot of current counters (for ledger / observability)."""
        now = self._clock()
        return {
            "claude_hour": self._claude_hour.count(now=now),
            "claude_day": self._claude_day.count(now=now),
            "daily_cost_usd": self._daily_cost_usd(now=now),
        }

    # ---- helpers --------------------------------------------------------

    def _check_rate_caps(self, provider: str) -> PolicyVerdict | None:
        now = self._clock()
        if provider == "claude_cli":
            if (
                self._cfg.claude_auto_calls_per_hour > 0
                and self._claude_hour.count(now=now)
                >= self._cfg.claude_auto_calls_per_hour
            ):
                return PolicyVerdict(
                    outcome="deny",
                    reason=(
                        f"Claude hourly cap reached "
                        f"({self._cfg.claude_auto_calls_per_hour}/hour)"
                    ),
                    triggered_rule="claude_hourly_cap",
                )
            if (
                self._cfg.claude_auto_calls_per_day > 0
                and self._claude_day.count(now=now)
                >= self._cfg.claude_auto_calls_per_day
            ):
                return PolicyVerdict(
                    outcome="deny",
                    reason=(
                        f"Claude daily cap reached "
                        f"({self._cfg.claude_auto_calls_per_day}/day)"
                    ),
                    triggered_rule="claude_daily_cap",
                )
        # Local / hermes_profile have no rate caps.
        return None

    def _estimate_input_tokens(self, prompt_text: str) -> int:
        if not prompt_text:
            return self._cfg.minimum_estimated_input_tokens
        chars = len(prompt_text)
        est = chars // max(1, self._cfg.estimated_chars_per_token)
        return max(est, self._cfg.minimum_estimated_input_tokens)

    @staticmethod
    def _estimate_cost(
        entry: ModelEntry, in_tokens: int, out_tokens: int,
    ) -> float:
        if entry.cost_input_per_1m == 0 and entry.cost_output_per_1m == 0:
            return 0.0
        return (
            entry.cost_input_per_1m * in_tokens / 1_000_000
            + entry.cost_output_per_1m * out_tokens / 1_000_000
        )

    def _daily_cost_usd(self, *, now: float | None = None) -> float:
        n = now if now is not None else self._clock()
        self._prune_costs(n)
        return sum(c for _, c in self._cost_day)

    def _prune_costs(self, now: float) -> None:
        cutoff = now - _SECONDS_PER_DAY
        while self._cost_day and self._cost_day[0][0] < cutoff:
            self._cost_day.popleft()
