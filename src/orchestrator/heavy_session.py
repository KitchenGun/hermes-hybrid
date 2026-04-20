"""FIX#4: heavy-path session reuse registry.

When the user types ``!heavy`` multiple times in quick succession they're
typically in a thread of thought — binding the second turn to the first
turn's Claude Code session lets the model pick up the context for free
(no re-sending history tokens, no re-priming system prompts).

But: sessions older than ``_REUSE_WINDOW_SEC`` stop being useful. The
user has moved on, the semantic link is gone, and the Max CLI might
have evicted the session file anyway. Beyond the window we fall back
to a fresh session.

Policy:
  - In-memory only (bot restart clears, which is fine — Max OAuth
    already requires re-login in that case).
  - Per-user; no cross-user session sharing.
  - On resume failure (:class:`ClaudeCodeResumeFailed`) the orchestrator
    calls :meth:`invalidate` and retries fresh.
  - Every ``pick`` / ``record`` / ``invalidate`` emits a structured log
    event so we can audit reuse behavior in production.
"""
from __future__ import annotations

import time
from dataclasses import dataclass

from src.obs import get_logger

log = get_logger(__name__)

# 10 minutes: long enough to cover a Discord back-and-forth, short enough
# that a stale session can't accidentally contaminate a new train of
# thought hours later. Tunable if field data says otherwise.
_REUSE_WINDOW_SEC = 600


@dataclass
class _HeavyState:
    session_id: str
    last_ts: float


class HeavySessionRegistry:
    """Tracks the last Claude Code session_id per user with a TTL window."""

    def __init__(self, window_sec: int = _REUSE_WINDOW_SEC):
        self._by_user: dict[str, _HeavyState] = {}
        self._window_sec = window_sec

    def pick(self, user_id: str, *, now: float | None = None) -> str | None:
        """Return the user's previous session_id if it's within the reuse
        window, else ``None``. Never removes the entry — we keep it in case
        a stale-but-still-valid session is worth probing (the CLI itself
        will tell us via :class:`ClaudeCodeResumeFailed` if it's gone).
        """
        current = now if now is not None else time.time()
        state = self._by_user.get(user_id)
        if state is None:
            log.info("heavy.session_choice", user_id=user_id, choice="fresh",
                     reason="no prior session")
            return None
        age = current - state.last_ts
        if age > self._window_sec:
            log.info(
                "heavy.session_choice",
                user_id=user_id, choice="fresh",
                reason="prior session expired",
                age_sec=int(age),
            )
            return None
        log.info(
            "heavy.session_choice",
            user_id=user_id, choice="reused",
            session_id=state.session_id,
            age_sec=int(age),
        )
        return state.session_id

    def record(self, user_id: str, session_id: str, *, now: float | None = None) -> None:
        """Stamp a session as the user's newest heavy turn."""
        current = now if now is not None else time.time()
        self._by_user[user_id] = _HeavyState(session_id=session_id, last_ts=current)
        log.info("heavy.session_recorded", user_id=user_id, session_id=session_id)

    def invalidate(self, user_id: str, *, reason: str = "") -> None:
        """Drop the user's entry — called after resume failure, session
        deletion, or any time the stored id is known to be useless."""
        state = self._by_user.pop(user_id, None)
        log.info(
            "heavy.session_invalidated",
            user_id=user_id,
            had_entry=state is not None,
            old_session_id=state.session_id if state else None,
            reason=reason,
        )

    # ---- introspection (for tests and debug slash commands) ----

    def peek(self, user_id: str) -> str | None:
        state = self._by_user.get(user_id)
        return state.session_id if state else None

    def size(self) -> int:
        return len(self._by_user)
