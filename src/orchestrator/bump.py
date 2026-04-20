"""FIX#2: bump-input compression for retries.

When the validator says ``retry_same_tier`` or ``tier_up``, the orchestrator
feeds the model another attempt with a short reminder of what went wrong last
time. Without compression, these reminders stack across 3–4 retries and the
context balloons — blowing the token budget and drowning the real prompt.

The rule is simple and **non-cumulative**:

  ``bumped = summary(last_attempt) + "\\n\\n" + original_user_message``

Only the **last** model output is summarized, never the whole chain, and the
preview is hard-capped at 200 characters. This means the bumped prompt after
the 5th retry has the same shape and length bound as the bumped prompt after
the 1st retry. No growth.

The orchestrator stores the summary line on ``task.bump_prefix`` so
``_messages()`` can prepend it to the user content. A successful pass clears
it.
"""
from __future__ import annotations

from dataclasses import dataclass

from src.obs import get_logger
from src.state import TaskState
from src.validator import ValidationResult

log = get_logger(__name__)

_PREVIEW_MAX = 200
_REASON_MAX = 120


@dataclass(frozen=True)
class BumpPayload:
    """Result of compressing the last attempt into a single-line reminder.

    ``summary_line`` is the compact breadcrumb we inject; ``prompt`` is the
    full text the caller should send (summary + two newlines + original
    user_message). Callers who maintain their own prompt template (e.g.
    orchestrator's ``_messages()``) typically use ``summary_line`` alone.
    """

    prompt: str
    summary_line: str
    preview_len: int
    had_previous: bool


def compress_for_bump(task: TaskState, verdict: ValidationResult) -> BumpPayload:
    """Compress ``task.model_outputs[-1]`` into a bounded reminder.

    Contract:
      - If there is no previous model output, returns a pass-through payload
        (``prompt == task.user_message``, ``had_previous=False``). The caller
        still sets ``bump_prefix=""`` — no prefix is injected on the first try.
      - Otherwise emits a single-line summary citing the previous tier, model
        name, validator reason, and a ≤200-char preview of the previous reply.
      - Summary and preview are both hard-capped; the returned ``prompt`` is
        therefore O(1) in the number of retries.
    """
    if not task.model_outputs:
        return BumpPayload(
            prompt=task.user_message,
            summary_line="",
            preview_len=0,
            had_previous=False,
        )

    last = task.model_outputs[-1]
    preview = (last.text or "").strip().replace("\n", " ")
    if len(preview) > _PREVIEW_MAX:
        preview = preview[:_PREVIEW_MAX] + "..."

    reason = (verdict.reason or "no reason given")[:_REASON_MAX]

    summary = (
        f"[previous attempt via {last.tier}:{last.model_name or '?'} "
        f"was rejected ({reason}). excerpt: {preview!r}. "
        f"Address the issue without repeating the same mistake.]"
    )

    log.info(
        "bump.compressed",
        prev_tier=last.tier,
        prev_model=last.model_name,
        reason=reason,
        preview_len=len(preview),
        summary_len=len(summary),
        retry_count=task.retry_count,
    )

    return BumpPayload(
        prompt=f"{summary}\n\n{task.user_message}",
        summary_line=summary,
        preview_len=len(preview),
        had_previous=True,
    )
