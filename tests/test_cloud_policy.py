"""Tests for src/job_factory/policy.py."""
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
    # All within window from t=120 (window is ts > t-60 = 60).
    assert c.count(now=120.0) == 3


def test_sliding_counter_prunes_expired():
    c = _SlidingCounter(window_seconds=60)
    c.record(ts=100.0)
    c.record(ts=110.0)
    c.record(ts=120.0)
    # At t=200, only entries > 140 are in window. None of these qualify.
    assert c.count(now=200.0) == 0


def test_sliding_counter_partial_prune():
    c = _SlidingCounter(window_seconds=60)
    c.record(ts=100.0)
    c.record(ts=150.0)
    c.record(ts=170.0)
    # At t=180, window is > 120 → 150 and 170 qualify.
    assert c.count(now=180.0) == 2


# ---- CloudPolicyConfig YAML loading ---------------------------------------


def test_config_default_when_missing(tmp_path):
    cfg = CloudPolicyConfig.from_yaml(tmp_path / "nope.yaml")
    # Defaults present.
    assert cfg.openai_calls_per_hour == 60
    assert cfg.daily_usd_cap == 5.0


def test_config_loads_overrides(tmp_path):
    p = tmp_path / "policy.yaml"
    p.write_text(yaml.safe_dump({
        "openai_calls_per_hour": 5,
        "claude_auto_calls_per_day": 100,
        "daily_usd_cap": 1.5,
    }), encoding="utf-8")
    cfg = CloudPolicyConfig.from_yaml(p)
    assert cfg.openai_calls_per_hour == 5
    assert cfg.claude_auto_calls_per_day == 100
    assert cfg.daily_usd_cap == 1.5
    # Untouched fields keep defaults.
    assert cfg.openai_calls_per_day == 1000


def test_config_invalid_yaml_falls_back_to_defaults(tmp_path):
    p = tmp_path / "policy.yaml"
    p.write_text("[\nbroken yaml", encoding="utf-8")
    cfg = CloudPolicyConfig.from_yaml(p)
    assert cfg.openai_calls_per_hour == 60  # default


def test_real_cloud_policy_yaml_loads():
    project_root = Path(__file__).resolve().parent.parent
    p = project_root / "config" / "cloud_policy.yaml"
    assert p.exists()
    cfg = CloudPolicyConfig.from_yaml(p)
    # Sanity: caps reasonable.
    assert cfg.openai_calls_per_hour > 0
    assert cfg.claude_auto_calls_per_hour > 0


# ---- Allow path -----------------------------------------------------------


def test_allow_when_all_caps_unset():
    """All caps = 0 → never deny."""
    cfg = CloudPolicyConfig(
        openai_calls_per_hour=0,
        openai_calls_per_day=0,
        daily_usd_cap=0,
        approval_estimated_tokens_above=0,
        approval_estimated_cost_above_usd=0,
    )
    p = CloudPolicy(config=cfg)
    v = p.evaluate(
        job=_job(),
        entry=_entry("openai", "gpt-4o", cost_input=2.5, cost_output=10),
        prompt_text="hello",
    )
    assert v.outcome == "allow"


def test_allow_local_provider_skips_rate_caps():
    """Ollama/local entries shouldn't even consult rate caps."""
    cfg = CloudPolicyConfig(
        openai_calls_per_hour=0,  # would deny if applied to ollama
        openai_calls_per_day=0,
    )
    p = CloudPolicy(config=cfg)
    v = p.evaluate(
        job=_job(),
        entry=_entry("ollama", "qwen2.5"),
        prompt_text="hi",
    )
    assert v.outcome == "allow"


# ---- Rate caps ------------------------------------------------------------


def test_openai_hourly_cap_denies_after_threshold():
    cfg = CloudPolicyConfig(
        openai_calls_per_hour=3,
        openai_calls_per_day=0,
        daily_usd_cap=0,
        approval_estimated_tokens_above=0,
        approval_estimated_cost_above_usd=0,
    )
    clock = _FakeClock()
    p = CloudPolicy(config=cfg, clock=clock)
    entry = _entry("openai", "gpt-4o-mini", cost_input=0.15, cost_output=0.60)

    # 3 allowed.
    for _ in range(3):
        v = p.evaluate(job=_job(), entry=entry)
        assert v.outcome == "allow"
        p.record_call(entry)

    # 4th denied.
    v = p.evaluate(job=_job(), entry=entry)
    assert v.outcome == "deny"
    assert v.triggered_rule == "openai_hourly_cap"


def test_openai_hourly_cap_window_resets():
    cfg = CloudPolicyConfig(
        openai_calls_per_hour=2,
        openai_calls_per_day=0,
        daily_usd_cap=0,
        approval_estimated_tokens_above=0,
        approval_estimated_cost_above_usd=0,
    )
    clock = _FakeClock()
    p = CloudPolicy(config=cfg, clock=clock)
    entry = _entry("openai", "gpt-4o-mini")

    p.record_call(entry)
    p.record_call(entry)
    # Cap reached.
    assert p.evaluate(job=_job(), entry=entry).outcome == "deny"

    # Advance past the hour window.
    clock.advance(_SECONDS_PER_HOUR + 1)
    assert p.evaluate(job=_job(), entry=entry).outcome == "allow"


def test_claude_caps_independent_of_openai():
    cfg = CloudPolicyConfig(
        openai_calls_per_hour=2,
        claude_auto_calls_per_hour=2,
        openai_calls_per_day=0,
        claude_auto_calls_per_day=0,
        daily_usd_cap=0,
        approval_estimated_tokens_above=0,
        approval_estimated_cost_above_usd=0,
    )
    p = CloudPolicy(config=cfg)
    openai = _entry("openai", "x")
    claude = _entry("claude_cli", "sonnet")

    # Burn OpenAI cap.
    p.record_call(openai)
    p.record_call(openai)
    assert p.evaluate(job=_job(), entry=openai).outcome == "deny"
    # Claude still ok.
    assert p.evaluate(job=_job(), entry=claude).outcome == "allow"


def test_daily_cap_denies():
    cfg = CloudPolicyConfig(
        openai_calls_per_hour=0,
        openai_calls_per_day=2,
        daily_usd_cap=0,
        approval_estimated_tokens_above=0,
        approval_estimated_cost_above_usd=0,
    )
    p = CloudPolicy(config=cfg)
    entry = _entry("openai", "x")
    p.record_call(entry)
    p.record_call(entry)
    v = p.evaluate(job=_job(), entry=entry)
    assert v.outcome == "deny"
    assert v.triggered_rule == "openai_daily_cap"


# ---- Cost cap -------------------------------------------------------------


def test_daily_usd_cap_denies_when_estimate_would_overrun():
    cfg = CloudPolicyConfig(
        openai_calls_per_hour=0,
        openai_calls_per_day=0,
        daily_usd_cap=0.10,
        approval_estimated_tokens_above=0,
        approval_estimated_cost_above_usd=0,
        # Estimation defaults: 200 in + 500 out = 700 tokens.
    )
    p = CloudPolicy(config=cfg)
    # gpt-4o pricing: $2.50/1M in, $10/1M out.
    # 200/1M * $2.50 + 500/1M * $10 = $0.0005 + $0.005 = $0.0055 per call.
    expensive = _entry("openai", "gpt-4o", cost_input=2.5, cost_output=10)

    # Pre-load $0.099 of fake spend → next call's $0.0055 would push to $0.1045.
    # But 0.099 + 0.0055 < 0.10 + 0.0055 = 0.1055, > 0.10, so denied.
    # Easiest setup: record 18 calls @ 0.0055 = $0.099.
    for _ in range(18):
        p.record_call(expensive, actual_cost_usd=0.0055)

    v = p.evaluate(job=_job(), entry=expensive)
    assert v.outcome == "deny"
    assert v.triggered_rule == "daily_usd_cap"


def test_daily_usd_cap_allows_when_within_budget():
    cfg = CloudPolicyConfig(
        openai_calls_per_hour=0,
        openai_calls_per_day=0,
        daily_usd_cap=10.0,           # plenty of headroom
        approval_estimated_tokens_above=0,
        approval_estimated_cost_above_usd=0,
    )
    p = CloudPolicy(config=cfg)
    entry = _entry("openai", "gpt-4o-mini", cost_input=0.15, cost_output=0.60)
    v = p.evaluate(job=_job(), entry=entry)
    assert v.outcome == "allow"


def test_estimated_cost_field_populated():
    cfg = CloudPolicyConfig(
        openai_calls_per_hour=0,
        openai_calls_per_day=0,
        daily_usd_cap=0,
        approval_estimated_tokens_above=0,
        approval_estimated_cost_above_usd=0,
    )
    p = CloudPolicy(config=cfg)
    entry = _entry("openai", "gpt-4o", cost_input=2.5, cost_output=10)
    v = p.evaluate(job=_job(), entry=entry)
    assert v.estimated_cost_usd > 0


# ---- Approval gate --------------------------------------------------------


def test_job_requires_approval_triggers_needs_approval():
    cfg = CloudPolicyConfig(
        openai_calls_per_hour=0,
        openai_calls_per_day=0,
        daily_usd_cap=0,
    )
    p = CloudPolicy(config=cfg)
    v = p.evaluate(
        job=_job(requires_user_approval=True),
        entry=_entry("openai", "gpt-4o-mini"),
    )
    assert v.outcome == "needs_approval"
    assert v.triggered_rule == "job_requires_approval"


def test_estimated_tokens_threshold_triggers_approval():
    cfg = CloudPolicyConfig(
        openai_calls_per_hour=0,
        openai_calls_per_day=0,
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
        entry=_entry("openai", "gpt-4o-mini"),
    )
    assert v.outcome == "needs_approval"
    assert v.triggered_rule == "estimated_tokens_threshold"


def test_estimated_cost_threshold_triggers_approval():
    cfg = CloudPolicyConfig(
        openai_calls_per_hour=0,
        openai_calls_per_day=0,
        daily_usd_cap=0,
        approval_estimated_tokens_above=0,
        approval_estimated_cost_above_usd=0.001,
    )
    p = CloudPolicy(config=cfg)
    # gpt-4o: ~$0.0055/call > $0.001 → approval.
    v = p.evaluate(
        job=_job(),
        entry=_entry("openai", "gpt-4o", cost_input=2.5, cost_output=10),
    )
    assert v.outcome == "needs_approval"
    assert v.triggered_rule == "estimated_cost_threshold"


# ---- record_call ----------------------------------------------------------


def test_record_call_does_not_track_local():
    p = CloudPolicy()
    entry = _entry("ollama", "qwen2.5")
    p.record_call(entry)
    assert p.stats()["openai_hour"] == 0
    assert p.stats()["claude_hour"] == 0


def test_record_call_with_cost_advances_daily_usd():
    p = CloudPolicy()
    entry = _entry("openai", "gpt-4o-mini")
    p.record_call(entry, actual_cost_usd=0.15)
    p.record_call(entry, actual_cost_usd=0.10)
    assert p.stats()["daily_cost_usd"] == pytest.approx(0.25)


def test_record_call_cost_window_resets():
    clock = _FakeClock()
    p = CloudPolicy(clock=clock)
    entry = _entry("openai", "gpt-4o")
    p.record_call(entry, actual_cost_usd=1.0)
    assert p.stats()["daily_cost_usd"] == pytest.approx(1.0)
    # Past the day window.
    clock.advance(_SECONDS_PER_DAY + 1)
    assert p.stats()["daily_cost_usd"] == pytest.approx(0.0)


# ---- stats() / observability ---------------------------------------------


def test_stats_counts_per_provider():
    p = CloudPolicy()
    openai = _entry("openai", "x")
    claude = _entry("claude_cli", "sonnet")
    p.record_call(openai)
    p.record_call(openai)
    p.record_call(claude)
    s = p.stats()
    assert s["openai_hour"] == 2
    assert s["openai_day"] == 2
    assert s["claude_hour"] == 1
    assert s["claude_day"] == 1
