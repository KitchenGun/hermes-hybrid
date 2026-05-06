"""Tests for the Phase 2 skill surface.

Covers:
  - Registry ordering and empty-registry fallthrough.
  - ``/hybrid-status`` reflects flag state and includes skill count.
  - ``/hybrid-budget`` handles the no-repo path + a repo with used tokens.
  - ``/memo save|list|clear`` round-trips through InMemoryMemory.
  - Orchestrator integration: a slash command is answered by the skill
    and tagged ``handled_by="skill:<name>"`` without touching Router/LLMs.
  - Skills run *after* RuleLayer — if a rule matches first, the skill is
    never consulted. (`/status <id>` is a RuleLayer handler.)
  - Skill exceptions degrade gracefully (no crash, user sees a warning).
"""
from __future__ import annotations

import pytest

from src.config import Settings
from src.memory import InMemoryMemory
from src.orchestrator import Orchestrator
from src.skills import (
    HybridBudgetSkill,
    HybridMemoSkill,
    HybridStatusSkill,
    Skill,
    SkillContext,
    SkillMatch,
    SkillRegistry,
    default_registry,
)


# ---- registry ---------------------------------------------------------------


def test_registry_ordering_first_match_wins():
    calls: list[str] = []

    class _SkillA(Skill):
        name = "a"

        def match(self, message: str) -> SkillMatch | None:
            calls.append("a")
            return SkillMatch(self.name, {}) if message.startswith("/x") else None

        async def invoke(self, match, ctx):
            return "a-reply"

    class _SkillB(Skill):
        name = "b"

        def match(self, message: str) -> SkillMatch | None:
            calls.append("b")
            return SkillMatch(self.name, {}) if message.startswith("/x") else None

        async def invoke(self, match, ctx):
            return "b-reply"

    reg = SkillRegistry([_SkillA(), _SkillB()])
    hit = reg.match("/x go")
    assert hit is not None
    skill, _ = hit
    assert skill.name == "a"
    assert calls == ["a"]  # short-circuits; B never consulted


def test_registry_empty_and_no_match():
    assert SkillRegistry().match("hi") is None
    reg = SkillRegistry([HybridStatusSkill()])
    assert reg.match("regular chat, not a slash command") is None


def test_default_registry_contents():
    reg = default_registry()
    assert reg.names() == ["hybrid-status", "hybrid-budget", "hybrid-memo", "kanban"]


# ---- hybrid-status ----------------------------------------------------------


@pytest.mark.asyncio
async def test_hybrid_status_shows_flags(settings: Settings):
    settings.ollama_enabled = True

    o = Orchestrator(settings)
    result = await o.handle("/hybrid-status", user_id="u1")
    assert result.handled_by == "skill:hybrid-status"
    body = result.response
    assert "ollama_enabled        : True" in body
    assert "master_enabled        :" in body
    assert "memory_inject_enabled :" in body
    assert "require_allowlist     :" in body
    # Orchestrator injected → skill count is rendered.
    assert "skills registered     :" in body


# ---- hybrid-budget ----------------------------------------------------------


@pytest.mark.asyncio
async def test_hybrid_budget_no_repo(settings: Settings):
    o = Orchestrator(settings, repo=None)
    result = await o.handle("/hybrid-budget", user_id="u1")
    assert result.handled_by == "skill:hybrid-budget"
    assert "repository not configured" in result.response
    assert f"{settings.cloud_token_budget_daily:,}" in result.response


@pytest.mark.asyncio
async def test_hybrid_budget_with_repo(settings: Settings):
    class _FakeRepo:
        async def used_tokens_today(self, user_id: str) -> int:
            return 12345

        # `save_task` is called by Orchestrator for any task.
        async def save_task(self, task):
            return None

        async def add_tokens(self, user_id, n):
            return None

    o = Orchestrator(settings, repo=_FakeRepo())  # type: ignore[arg-type]
    result = await o.handle("/hybrid-budget", user_id="u1")
    assert result.handled_by == "skill:hybrid-budget"
    assert "12,345" in result.response
    assert f"{settings.cloud_token_budget_daily:,}" in result.response


# ---- hybrid-memo ------------------------------------------------------------


@pytest.mark.asyncio
async def test_memo_save_list_clear_via_orchestrator(settings: Settings):
    o = Orchestrator(settings)

    # Empty list initially
    r = await o.handle("/memo list", user_id="u1")
    assert "no memos yet" in r.response

    # Save two
    r = await o.handle("/memo save buy milk", user_id="u1")
    assert r.handled_by == "skill:hybrid-memo"
    assert "buy milk" in r.response
    await o.handle("/memo save call mom", user_id="u1")

    # List shows both
    r = await o.handle("/memo list", user_id="u1")
    assert "buy milk" in r.response
    assert "call mom" in r.response

    # Other user can't see u1's notes
    r = await o.handle("/memo list", user_id="u2")
    assert "no memos yet" in r.response

    # Clear
    r = await o.handle("/memo clear", user_id="u1")
    assert "cleared 2" in r.response
    r = await o.handle("/memo list", user_id="u1")
    assert "no memos yet" in r.response


@pytest.mark.asyncio
async def test_memo_save_rejects_empty(settings: Settings):
    o = Orchestrator(settings)
    r = await o.handle("/memo save    ", user_id="u1")
    assert "Usage" in r.response or "⚠️" in r.response


@pytest.mark.asyncio
async def test_memo_oversize_rendered_not_crashed(settings: Settings):
    """An oversize payload should produce a user-readable warning and
    NOT crash the orchestrator (skill handles MemoryTooLarge internally)."""
    o = Orchestrator(settings)
    big = "x" * 2500
    r = await o.handle(f"/memo save {big}", user_id="u1")
    assert r.task.status == "succeeded"
    assert "too large" in r.response


# ---- orchestrator integration ----------------------------------------------


@pytest.mark.asyncio
async def test_skill_short_circuits_master_dispatch(settings: Settings):
    """Skill hit must return without consulting the master LLM. We
    can't easily spy on the master adapter (it's lazy-built only when
    needed); instead we rely on master_enabled=False (the fixture
    default) so any downstream call would fail with master:disabled.
    A skill hit producing a normal slash response proves the
    short-circuit fired."""
    o = Orchestrator(settings)
    r = await o.handle("/hybrid-status", user_id="u1")
    assert r.handled_by.startswith("skill:")
    assert "master:disabled" not in r.response


@pytest.mark.asyncio
async def test_rule_layer_wins_over_skill_when_both_match(settings: Settings):
    """RuleLayer runs before skills; its matches aren't stolen by a skill
    with an overlapping prefix. (Regression guard for handled_by ordering.)

    Use ``/help`` (a static RuleLayer rule with a pre-baked response) so
    the assertion doesn't depend on dynamic ``/status`` handler wiring."""
    o = Orchestrator(settings)
    r = await o.handle("/help", user_id="u1")
    assert r.handled_by == "rule"


@pytest.mark.asyncio
async def test_skill_exception_renders_warning(settings: Settings):
    """If a skill's invoke() raises, orchestrator degrades with a warning
    instead of bubbling the traceback up to Discord."""

    class _BoomSkill(Skill):
        name = "boom"

        def match(self, message: str) -> SkillMatch | None:
            if message.startswith("/boom"):
                return SkillMatch(self.name, {})
            return None

        async def invoke(self, match, ctx):
            raise RuntimeError("kaboom")

    o = Orchestrator(settings, skills=SkillRegistry([_BoomSkill()]))
    r = await o.handle("/boom", user_id="u1")
    assert r.handled_by == "skill:boom"
    assert r.task.degraded is True
    assert "RuntimeError" in r.response


# ---- match edge cases -------------------------------------------------------


def test_hybrid_status_pattern_strict():
    s = HybridStatusSkill()
    assert s.match("/hybrid-status") is not None
    assert s.match("/HYBRID-STATUS") is not None  # case-insensitive
    assert s.match("/hybrid-status extra") is None  # strict end-of-line
    assert s.match("  /hybrid-status   ") is not None  # whitespace tolerated


def test_hybrid_budget_pattern_strict():
    s = HybridBudgetSkill()
    assert s.match("/hybrid-budget") is not None
    assert s.match("/hybrid-budget now") is None


def test_hybrid_memo_pattern_parses_verb_and_text():
    s = HybridMemoSkill()
    m = s.match("/memo save hello world")
    assert m is not None
    assert m.args == {"verb": "save", "text": "hello world"}

    m = s.match("/memo list")
    assert m is not None and m.args == {"verb": "list"}

    m = s.match("/memo clear")
    assert m is not None and m.args == {"verb": "clear"}

    assert s.match("/memo") is None
    assert s.match("/memo delete foo") is None
