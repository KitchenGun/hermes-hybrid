"""Job_Factory — profile discovery, matching, and dynamic creation.

Sits between :class:`Refiner` and :class:`Orchestrator`. Given a raw
user request, the Factory:

  1. Scans ``profiles/`` for available profiles (cached with TTL).
  2. Scores each against the request using keyword/semantic hints.
  3. If a confident match exists → returns that profile_id.
  4. Otherwise → synthesizes a new profile from a template and registers
     it on disk, using the standard directory layout.

LangChain analogy:
  A ``Runnable`` that maps ``user_message → profile_id`` with a fallback
  branch (profile creation) when no mapping exists.
"""
from src.job_factory.factory import (
    JobFactory,
    JobFactoryError,
    ProfileMatch,
)
from src.job_factory.profile_template import render_new_profile

__all__ = [
    "JobFactory",
    "JobFactoryError",
    "ProfileMatch",
    "render_new_profile",
]
