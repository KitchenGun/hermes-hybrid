"""Phase 2 Skill abstraction.

A **Skill** is a small, named handler that owns a particular slash
command (or pattern) end-to-end — from matching the user's message to
producing the final text response. Skills run *after* the existing
RuleLayer and *before* the Router, in a single pass inside the
Orchestrator.

Why skills instead of piling more regexes into ``RuleLayer``?

- Skills can be **stateful**: a memo skill needs a ``MemoryBackend``, a
  budget skill needs the ``Repository``. RuleLayer's current ``RuleMatch``
  DTO just names a handler string; the Orchestrator then does a big
  if-chain to actually run the handler. Skills own their own invocation
  logic, so adding a skill = adding one file, not one regex and one
  elif-branch.
- Phase 3 plans to migrate skills onto Hermes' native skill surface; a
  clean Python-side contract makes that port mechanical.
- Skills can read ``SkillContext`` freely — no dependency injection
  through a trail of method signatures.

Skills must be **fast and side-effect-light** — they short-circuit the
LLM path, so a slow skill blocks the user's response. Anything that
needs real reasoning should go through the Router/Hermes path.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any


@dataclass
class SkillContext:
    """Bag of handles a Skill needs to do its job.

    Deliberately permissive — skills may need various Orchestrator-owned
    resources (Repository, MemoryBackend, heavy-session registry, etc.)
    and we don't want to keep growing method signatures as we add skills.
    """

    settings: Any                # src.config.Settings (avoid circular import)
    repo: Any | None             # Repository or None (tests / CLI)
    memory: Any                  # MemoryBackend
    user_id: str
    session_id: str
    # Optional back-reference to the Orchestrator itself so skills that
    # need to inspect live state (tasks, budgets) can reach it. Phase 2
    # skills use this sparingly.
    orchestrator: Any = None


@dataclass(frozen=True)
class SkillMatch:
    """A Skill's answer when asked 'does this message belong to me?'"""

    skill_name: str
    args: dict[str, str]


class Skill(ABC):
    """Base class for Skills.

    Concrete skills override :meth:`match` and :meth:`invoke`. The
    ``name`` attribute is used for logging / ``handled_by`` tagging and
    should be lowercase-hyphen (e.g. ``"hybrid-status"``).
    """

    name: str = "unnamed-skill"

    @abstractmethod
    def match(self, message: str) -> SkillMatch | None:
        """Return a ``SkillMatch`` if the message belongs to this skill,
        else ``None``. Must be synchronous and fast (regex / prefix
        check only) — it runs against every incoming message.
        """

    @abstractmethod
    async def invoke(self, match: SkillMatch, ctx: SkillContext) -> str:
        """Produce the final response string for the matched message.

        Exceptions propagate to the Orchestrator, which renders them
        as a degraded response; skills should handle their own expected
        error conditions and return a user-readable message instead.
        """
