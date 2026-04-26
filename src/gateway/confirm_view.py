"""HITL confirmation UI — discord.ui.View with [확인]/[취소] buttons.

Paired with :meth:`src.orchestrator.Orchestrator.enter_confirmation_gate`:
the orchestrator suspends a task in ``awaiting_confirmation`` and hands the
caller a :class:`~src.state.ConfirmationContext`; the gateway turns that
context into an embed + this view.

Responsibilities split:
  - View handles *interaction* (click, auth check, disabling buttons,
    timeout) and calls ``orchestrator.resume_after_confirmation``.
  - View does NOT execute the approved job itself — that belongs to the
    caller that originally set up the gate. We return the resume result
    by storing it on the view so the bot can act on it after
    :meth:`discord.ui.View.wait`, or the caller can pass an ``on_approve``
    / ``on_decline`` coroutine for fire-and-forget flows.

Security: the allowlist is already enforced at ``on_message`` time, but we
defensively check ``interaction.user.id == owner_user_id`` on every button
click so a different allowlisted user can't confirm on the owner's behalf.
"""
from __future__ import annotations

from typing import Any, Awaitable, Callable, Optional

import discord

from src.obs import get_logger

log = get_logger(__name__)

# Type for the optional callback the bot passes so it can resume the
# real execution pipeline once confirmation is granted.
ApproveCallback = Callable[[str, dict[str, Any]], Awaitable[None]]
DeclineCallback = Callable[[str, str], Awaitable[None]]


class ConfirmView(discord.ui.View):
    """Two-button confirmation gate rendered under an HITL preview embed."""

    def __init__(
        self,
        *,
        task_id: str,
        owner_user_id: int,
        orchestrator: Any,  # src.orchestrator.Orchestrator — forward-ref to avoid import cycle
        timeout_seconds: float = 600.0,
        on_approve: Optional[ApproveCallback] = None,
        on_decline: Optional[DeclineCallback] = None,
    ) -> None:
        super().__init__(timeout=timeout_seconds)
        self.task_id = task_id
        self.owner_user_id = int(owner_user_id)
        self.orchestrator = orchestrator
        self._on_approve = on_approve
        self._on_decline = on_decline
        self._message: discord.Message | None = None
        # Populated after wait() returns; lets callers inspect the outcome
        # without registering callbacks.
        self.approved: bool | None = None
        self.decision: str = "pending"

    def bind_message(self, message: discord.Message) -> None:
        """Let the view disable its own buttons on timeout."""
        self._message = message

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.owner_user_id:
            await interaction.response.send_message(
                "⚠️ 본인의 확인 요청이 아닙니다.", ephemeral=True
            )
            log.info(
                "hitl.view.actor_mismatch",
                task_id=self.task_id,
                owner=self.owner_user_id,
                actor=interaction.user.id,
            )
            return False
        return True

    @discord.ui.button(label="확인", style=discord.ButtonStyle.success, emoji="✅")
    async def confirm(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:
        await self._resolve(interaction, decision="confirm")

    @discord.ui.button(label="취소", style=discord.ButtonStyle.danger, emoji="❌")
    async def cancel(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:
        await self._resolve(interaction, decision="cancel")

    async def _resolve(
        self, interaction: discord.Interaction, *, decision: str
    ) -> None:
        # Defer first so we can edit the original message even if the
        # resume path takes a beat (sqlite write).
        await interaction.response.defer()

        result = await self.orchestrator.resume_after_confirmation(
            self.task_id,
            decision=decision,
            actor_user_id=str(interaction.user.id),
        )
        self.decision = decision

        if result is None:
            # State drift: task gone / not actually awaiting. Surface it
            # softly — don't re-disable buttons, the task's gone anyway.
            await interaction.followup.send(
                "⚠️ 해당 작업을 찾을 수 없거나 이미 처리되었습니다.",
                ephemeral=True,
            )
            self.approved = False
            self.stop()
            return

        task, approved = result
        self.approved = approved
        self._disable_all()
        await self._update_message_after(interaction, task, decision)

        if approved and self._on_approve is not None:
            payload = (
                task.confirmation_context.pending_payload
                if task.confirmation_context is not None
                else {}
            )
            try:
                await self._on_approve(task.task_id, payload)
            except Exception as e:  # noqa: BLE001
                log.warning(
                    "hitl.view.on_approve_failed",
                    task_id=self.task_id,
                    err=str(e),
                )
        elif not approved and self._on_decline is not None:
            try:
                await self._on_decline(task.task_id, decision)
            except Exception as e:  # noqa: BLE001
                log.warning(
                    "hitl.view.on_decline_failed",
                    task_id=self.task_id,
                    err=str(e),
                )

        self.stop()

    async def on_timeout(self) -> None:
        # Fires when discord.ui.View.timeout elapses without a click.
        # Transition the task to failed via the same resume API so state
        # is consistent with the cancel path.
        result = await self.orchestrator.resume_after_confirmation(
            self.task_id,
            decision="timeout",
            actor_user_id=str(self.owner_user_id),
        )
        self.decision = "timeout"
        self.approved = False
        self._disable_all()
        if self._message is not None:
            try:
                await self._message.edit(
                    content="⏱️ 확인 시간이 초과되어 실행을 건너뜁니다.",
                    view=self,
                )
            except discord.HTTPException:
                pass
        if result is not None and self._on_decline is not None:
            try:
                await self._on_decline(self.task_id, "timeout")
            except Exception as e:  # noqa: BLE001
                log.warning(
                    "hitl.view.on_decline_failed_timeout",
                    task_id=self.task_id,
                    err=str(e),
                )

    def _disable_all(self) -> None:
        for child in self.children:
            if isinstance(child, discord.ui.Button):
                child.disabled = True

    async def _update_message_after(
        self,
        interaction: discord.Interaction,
        task: Any,
        decision: str,
    ) -> None:
        if decision == "confirm":
            note = "✅ 확인됨 — 실행을 이어갑니다."
        elif decision == "cancel":
            note = "❌ 취소됨 — 실행을 중단합니다."
        else:
            note = "⏱️ 시간 초과."
        try:
            await interaction.edit_original_response(content=note, view=self)
        except discord.HTTPException as e:
            log.info(
                "hitl.view.edit_failed",
                task_id=self.task_id,
                err=str(e),
            )


def build_preview_embed(
    *,
    title: str,
    body: str,
    color: int,
    task_id: str,
) -> discord.Embed:
    """Render the yellow confirmation embed. Kept here so the view module
    owns the visual contract — color/footer — end-to-end.
    """
    embed = discord.Embed(title=title, description=body, color=color)
    embed.set_footer(text=f"task `{task_id}` · [확인]/[취소] 버튼을 눌러주세요")
    return embed
