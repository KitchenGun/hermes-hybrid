"""Integration Layer — diagram-aligned 4-component façade.

The diagram puts four sibling components between the Execution Modes and
the Hermes Master Orchestrator:

    Intent Router   — Discord slash / natural language / scheduler / watcher
    Session Importer — ExperienceLog unification (cron/watcher hand-off)
    Policy Gate     — confirm / budget / tier / safety
    Job Inventory   — runtime view of profiles / jobs / skills

This package is the single entry point for the Master to consult those
four. Existing implementations live in other modules (`router/`,
`validator/`, `core/session_importer.py`, profile yaml scanning); the
classes here re-package them behind small, testable interfaces.

Why a façade rather than moving the source files:
  * keeps Phase 4 (legacy deletion) reversible — if an integration
    decision needs to be reverted, the underlying primitives stay
  * makes it obvious to a future reader where to look for cross-cutting
    routing concerns
"""
from .intent_router import IntentResult, IntentRouter
from .job_inventory import JobInventory, JobSpec, ProfileSpec
from .policy_gate import PolicyDecision, PolicyGate
from .session_importer import import_sessions, session_to_record

__all__ = [
    "IntentResult",
    "IntentRouter",
    "JobInventory",
    "JobSpec",
    "PolicyDecision",
    "PolicyGate",
    "ProfileSpec",
    "import_sessions",
    "session_to_record",
]
