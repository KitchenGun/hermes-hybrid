"""Tests for src.memory.discord_ingest (P4 dry-run scaffolding).

Covers explicit save-intent detection across Korean / English /
slash-command / mention triggers, confirms ordinary chatter does
not match, and verifies the function does not perform any side
effects regardless of the auto_ingest_enabled flag.
"""
from __future__ import annotations

import pytest

from src.memory.discord_ingest import (
    SaveIntent,
    SaveTrigger,
    describe_intent,
    try_extract_save_intent,
)


# ---------------------------------------------------------------------------
# Korean phrases
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "msg",
    [
        "이거 기억해",
        "기억해 다음에 참고해야 함",
        "이 결정 메모해줘",
        "프로젝트 컨텍스트 저장해주세요.",
        "기억해둬, 모드 폐기 합의는 2026-05-04",
    ],
)
def test_detects_korean_save_phrases(msg: str) -> None:
    intent = try_extract_save_intent(msg)
    assert intent is not None
    assert intent.trigger is SaveTrigger.KOREAN_PHRASE
    assert intent.body == msg.strip()


# ---------------------------------------------------------------------------
# English phrases
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "msg",
    [
        "remember this for next session",
        "Save This: kanban over scrum",
        "Note this — three loops are unfinished",
    ],
)
def test_detects_english_save_phrases(msg: str) -> None:
    intent = try_extract_save_intent(msg)
    assert intent is not None
    assert intent.trigger is SaveTrigger.ENGLISH_PHRASE


# ---------------------------------------------------------------------------
# Slash command
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "msg",
    [
        "/memo-save kanban over scrum",
        "/memo_save",
        "/remember this matters",
        "/save the discussion above",
    ],
)
def test_detects_slash_command(msg: str) -> None:
    intent = try_extract_save_intent(msg)
    assert intent is not None
    assert intent.trigger is SaveTrigger.SLASH_COMMAND


# ---------------------------------------------------------------------------
# Mention
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "msg",
    [
        "@hermes save the above",
        "<@123456789> remember the kanban decision",
        "<@!987654321> 기억",
    ],
)
def test_detects_mention(msg: str) -> None:
    intent = try_extract_save_intent(msg)
    assert intent is not None
    assert intent.trigger is SaveTrigger.MENTION


# ---------------------------------------------------------------------------
# No-match cases
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "msg",
    [
        "",
        "    ",
        "오늘 회의 어땠어?",
        "deploy the staging build please",
        "/help",
        "@someone unrelated mention",
        "I'll remember to bring lunch",   # 'remember to' is a different intent
        "save the date",                  # 'save the' alone is not 'save this/that'
    ],
)
def test_does_not_match_unrelated_messages(msg: str) -> None:
    assert try_extract_save_intent(msg) is None


# ---------------------------------------------------------------------------
# Side-effect contract
# ---------------------------------------------------------------------------
def test_dry_run_returns_intent_without_side_effects(tmp_path) -> None:
    """auto_ingest_enabled is plumbed through but the detector itself
    must never write anywhere — the gateway integration is responsible
    for that and isn't wired in this commit."""
    # Detector returns the same intent regardless of flag — the flag
    # only affects what the *caller* does next.
    msg = "이거 기억해"
    a = try_extract_save_intent(msg, auto_ingest_enabled=False)
    b = try_extract_save_intent(msg, auto_ingest_enabled=True)
    assert a is not None and b is not None
    assert a.trigger is b.trigger
    assert a.matched_text == b.matched_text
    # No new files should appear under tmp_path — the detector never
    # accepts a path in the first place.
    assert list(tmp_path.iterdir()) == []


# ---------------------------------------------------------------------------
# describe_intent
# ---------------------------------------------------------------------------
def test_describe_intent_handles_none() -> None:
    assert describe_intent(None) == "no-save-intent"


def test_describe_intent_renders_summary() -> None:
    intent = SaveIntent(
        trigger=SaveTrigger.SLASH_COMMAND,
        matched_text="/memo-save",
        body="/memo-save kanban discussion",
    )
    s = describe_intent(intent)
    assert "trigger=slash_command" in s
    assert "matched='/memo-save'" in s
    assert "body_len=" in s


# ---------------------------------------------------------------------------
# Slash command precedence over phrase match
# ---------------------------------------------------------------------------
def test_slash_command_takes_precedence_over_phrase() -> None:
    """A slash-command line that also contains a Korean save phrase
    should be classified as the slash command (more explicit)."""
    intent = try_extract_save_intent("/memo-save 이거 기억해")
    assert intent is not None
    assert intent.trigger is SaveTrigger.SLASH_COMMAND
