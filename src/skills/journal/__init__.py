"""Journal pipeline — Discord #일기 채널 자연어 → 24-필드 → Google Sheets.

Phase 22 (2026-05-07): legacy ``journal_ops`` profile (Phase 8 폐기) 의
핵심 흐름만 살린 minimal 재구현. forced_profile / SOUL.md / SKILL.md
infra 없이 단일 함수 ``handle_journal_message`` 가 추출-저장-응답을 끝낸다.
"""
from .pipeline import JournalPipeline, JournalPipelineResult, handle_journal_message

__all__ = ["JournalPipeline", "JournalPipelineResult", "handle_journal_message"]
