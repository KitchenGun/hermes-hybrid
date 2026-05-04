"""Tests for src/job_factory/policy.py.

2026-05-04: OpenAI rate-cap tests removed when the API legacy was purged.
Claude CLI is the only cloud lane; rate caps are exercised against
claude_cli entries. Cost-cap tests use synthetic high prices on claude_cli
entries to exercise the daily_usd_cap logic (claude_cli is $0 in
production but the policy logic stays valid for any future paid arm).
"""
from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from src.job_factory.policy import (
    CloudPolicy,
    CloudPolicyConfig,
    PolicyVerdict,
    _SECONDS_PER_DAY,
    _SECONDS_PER_HOUR,
    _SlidingCounter,
)
from src.job_factory.registry import JobType, ModelEntry


# ---- Helpers --------------------------------------------------------------


class _FakeClock:
    """Manually-advanced clock for deterministic time-window tests."""

    def __init__(self, start: float = 1_700_000_000.0):
        self.now = start

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


def _job(
    name: str = "summarize",
    *,
    requires_user_approval: bool = False,
) -> JobType:
    return JobType(
        name=name,
        requires_user_approval=requires_user_approval,
        cloud_allowed=True,
        claude_allowed=True,
    )


def _entry(
    provider: str,
    name: str,
    *,
    cost_input: float = 0.0,
    cost_output: float = 0.0,
) -> ModelEntry:
    return ModelEntry(
        provider=provider, name=name,
        cost_input_per_1m=cost_input,
        cost_output_per_1m=cost_output,
    )


# ---- _SlidingCounter ------------------------------------------------------


def test_sliding_counter_records_and_counts():
    c = _SlidingCounter(window_seconds=60)
    c.record(ts=100.0)
    c.record(ts=110.0)
    c.record(ts=120.0)
    assert c.count(now=120.0) == 3


def test_sliding_counter_prunes_expired():
    c = _SlidingCounter(window_seconds=60)
    c.record(ts=100.0)
    c.record(ts=110.0)
    c.record(ts=120.0)
    assert c.count(now=200.0) == 0


def test_sliding_counter_partial_prune():
    c = _SlidingCounter(window_seconds=60)
    c.record(ts=100.0)
    c.record(ts=150.0)
    c.record(ts=170.0)
    assert c.count(now=180.0) == 2


# ---- CloudPolicyConfig YAML loading ---------------------------------------


def test_config_default_when_missing(tmp_path):
    cfg = CloudPolicyConfig.from_yaml(tmp_path / "nope.yaml")
    assert cfg.claude_auto_calls_per_hour == 10
    assert cfg.daily_usd_cap == 5.0


def test_config_loads_overrides(tmp_path):
    p = tmp_path / "policy.yaml"
    p.write_text(yaml.safe_dump({
        "claude_auto_calls_per_hour": 5,
        "claude_auto_calls_per_day": 100,
        "daily_usd_cap": 1.5,
    }), encoding="utf-8")
    cfg = CloudPolicyConfig.from_yaml(p)
    assert cfg.claude_auto_calls_per_hour == 5
    assert cfg.claude_auto_calls_per_day == 100
    assert cfg.daily_usd_cap == 1.5


def test_config_invalid_yaml_falls_back_to_defaults(tmp_path):
    p = tmp_path / "policy.yaml"
    p.write_text("[\nbroken yaml", encoding="utf-8")
    cfg = CloudPolicyConfig.from_yaml(p)
    assert cfg.claude_auto_calls_per_hour == 10  # default


def test_real_cloud_policy_yaml_loads():
    project_root = Path(__file__).resolve().parent.parent
    p = project_root / "config" / "cloud_policy.yaml"
    assert p.exists()
    cfg = CloudPolicyConfig.from_yaml(p)
    assert cfg.claude_auto_calls_per_hour > 0


# ---- Allow path -----------------------------------------------------------


def test_allow_when_all_caps_unset():
    """All caps = 0 → never deny."""
    cfg = CloudPolicyConfig(
        claude_auto_calls_per_hour=0,
        claude_auto_calls_per_day=0,
        daily_usd_cap=0,
        approval_estimated_tokens_above=0,
        approval_estimated_cost_above_usd=0,
    )
    p = CloudPolicy(config=cfg)
    v = p.evaluate(
        job=_job(),
        entry=_entry("claude_cli", "sonnet"),
        prompt_text="hello",
    )
    assert v.outcome == "allow"


def test_allow_local_provider_skips_rate_caps():
    """Ollama/local entries shouldn't even consult rate caps."""
    cfg = CloudPolicyConfig(
        claude_auto_calls_per_hour=0,
        claude_auto_calls_per_day=0,
    )
    p = CloudPolicy(config=cfg)
    v = p.evaluate(
        job=_job(),
        entry=_entry("ollama", "qwen2.5"),
        prompt_text="hi",
    )
    assert v.outcome == "allow"


# ---- Rate caps ------------------------------------------------------------


def test_claude_hourly_cap_denies_after_threshold():
    cfg = CloudPolicyConfig(
        claude_auto_calls_per_hour=3,
        claude_auto_calls_per_day=0,
        daily_usd_cap=0,
        approval_estimated_tokens_above=0,
        approval_estimated_cost_above_usd=0,
    )
    clock = _FakeClock()
    p = CloudPolicy(config=cfg, clock=clock)
    entry = _entry("claude_cli", "sonnet")

    for _ in range(3):
        v = p.evaluate(job=_job(), entry=entry)
        assert v.outcome == "allow"
        p.record_call(entry)

    v = p.evaluate(job=_job(), entry=entry)
    assert v.outcome == "deny"
    assert v.triggered_rule == "claude_hourly_cap"


def test_claude_hourly_cap_window_resets():
    cfg = CloudPolicyConfig(
        claude_auto_calls_per_hour=2,
        claude_auto_calls_per_day=0,
        daily_usd_cap=0,
        approval_estimated_tokens_above=0,
        approval_estimated_cost_above_usd=0,
    )
    clock = _FakeClock()
    p = CloudPolicy(config=cfg, clock=clock)
    entry = _entry("claude_cli", "haiku")

    p.record_call(entry)
    p.record_call(entry)
    assert p.evaluate(job=_job(), entry=entry).outcome == "deny"

    clock.advance(_SECONDS_PER_HOUR + 1)
    assert p.evaluate(job=_job(), entry=entry).outcome == "allow"


def test_claude_daily_cap_denies():
    cfg = CloudPolicyConfig(
        claude_auto_calls_per_hour=0,
        claude_auto_calls_per_day=2,
        daily_usd_cap=0,
        approval_estimated_tokens_above=0,
        approval_estimated_cost_above_usd=0,
    )
    p = CloudPolicy(config=cfg)
    entry = _entry("claude_cli", "haiku")
    p.record_call(entry)
    p.record_call(entry)
    v = p.evaluate(job=_job(), entry=entry)
    assert v.outcome == "deny"
    assert v.triggered_rule == "claude_daily_cap"


# ---- Cost cap -------------------------------------------------------------


def test_daily_usd_cap_denies_when_estimate_would_overrun():
    """Cost cap test — using arbitrary high prices on a claude_cli entry to
    exercise the cost-tracking logic. Production claude_cli has cost_*=0
    (Max OAuth) but the policy logic remains valid for any future paid arm."""
    cfg = CloudPolicyConfig(
        claude_auto_calls_per_hour=0,
        claude_auto_calls_per_day=0,
        daily_usd_cap=0.10,
        approval_estimated_tokens_above=0,
        approval_estimated_cost_above_usd=0,
    )
    p = CloudPolicy(config=cfg)
    expensive = _entry("claude_cli", "x", cost_input=2.5, cost_output=10)

    for _ in range(18):
        p.record_call(expensive, actual_cost_usd=0.0055)

    v = p.evaluate(job=_job(), entry=expensive)
    assert v.outcome == "deny"
    assert v.triggered_rule == "daily_usd_cap"


def test_daily_usd_cap_allows_when_within_budget():
    cfg = CloudPolicyConfig(
        claude_auto_calls_per_hour=0,
        claude_auto_calls_per_day=0,
        daily_usd_cap=10.0,           # plenty of headroom
        approval_estimated_tokens_above=0,
        approval_estimated_cost_above_usd=0,
    )
    p = CloudPolicy(config=cfg)
    entry = _entry("claude_cli", "haiku")
    v = p.evaluate(job=_job(), entry=entry)
    assert v.outcome == "allow"


def test_estimated_cost_field_populated():
    cfg = CloudPolicyConfig(
        claude_auto_calls_per_hour=0,
        claude_auto_calls_per_day=0,
        daily_usd_cap=0,
        approval_estimated_tokens_above=0,
        approval_estimated_cost_above_usd=0,
    )
    p = CloudPolicy(config=cfg)
    entry = _entry("claude_cli", "sonnet", cost_input=2.5, cost_output=10)
    v = p.evaluate(job=_job(), entry=entry)
    assert v.estimated_cost_usd > 0


# ---- Approval gate --------------------------------------------------------


def test_job_requires_approval_triggers_needs_approval():
    cfg = CloudPolicyConfig(
        claude_auto_calls_per_hour=0,
        claude_auto_calls_per_day=0,
        daily_usd_cap=0,
    )
    p = CloudPolicy(config=cfg)
    v = p.evaluate(
        job=_job(requires_user_approval=True),
        entry=_entry("claude_cli", "haiku"),
    )
    assert v.outcome == "needs_approval"
    assert v.triggered_rule == "job_requires_approval"


def test_estimated_tokens_threshold_triggers_approval():
    cfg = CloudPolicyConfig(
        claude_auto_calls_per_hour=0,
        claude_auto_calls_per_day=0,
        daily_usd_cap=0,
        approval_estimated_tokens_above=300,
        approval_estimated_cost_above_usd=0,
        estimated_output_tokens=200,
        minimum_estimated_input_tokens=200,
    )
    p = CloudPolicy(config=cfg)
    # 200 + 200 = 400 > 300 → approval.
    v = p.evaluate(
        job=_job(),
        entry=_entry("claude_cli", "haiku"),
    )
    assert v.outcome == "needs_approval"
    assert v.triggered_rule == "estimated_tokens_threshold"


def test_estimated_cost_threshold_triggers_approval():
    cfg = CloudPolicyConfig(
        claude_auto_calls_per_hour=0,
        claude_auto_calls_per_day=0,
        daily_usd_cap=0,
        approval_estimated_tokens_above=0,
        approval_estimated_cost_above_usd=0.001,
    )
    p = CloudPolicy(config=cfg)
    # ~$0.0055/call > $0.001 → approval.
    v = p.evaluate(
        job=_job(),
        entry=_entry("claude_cli", "sonnet", cost_input=2.5, cost_output=10),
    )
    assert v.outcome == "needs_approval"
    assert v.triggered_rule == "estimated_cost_threshold"


# ---- record_call ----------------------------------------------------------


def test_record_call_does_not_track_local():
    p = CloudPolicy()
    entry = _entry("ollama", "qwen2.5")
    p.record_call(entry)
    assert p.stats()["claude_hour"] == 0


def test_record_call_with_cost_advances_daily_usd():
    p = CloudPolicy()
    entry = _entry("claude_cli", "sonnet")
    p.record_call(entry, actual_cost_usd=0.15)
    p.record_call(entry, actual_cost_usd=0.10)
    assert p.stats()["daily_cost_usd"] == pytest.approx(0.25)


def test_record_call_cost_window_resets():
    clock = _FakeClock()
    p = CloudPolicy(clock=clock)
    entry = _entry("claude_cli", "sonnet")
    p.record_call(entry, actual_cost_usd=1.0)
    assert p.stats()["daily_cost_usd"] == pytest.approx(1.0)
    # Past the day window.
    clock.advance(_SECONDS_PER_DAY + 1)
    assert p.stats()["daily_cost_usd"] == pytest.approx(0.0)


# ---- stats() / observability ---------------------------------------------


def test_stats_counts_claude_calls():
    p = CloudPolicy()
    claude = _entry("claude_cli", "sonnet")
    p.record_call(claude)
    p.record_call(claude)
    p.record_call(claude)
    s = p.stats()
    assert s["claude_hour"] == 3
    assert s["claude_day"] == 3
