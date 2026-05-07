"""Journal pipeline orchestrator — extract → append → format.

The Discord gateway calls :func:`handle_journal_message` for messages on the
``JOURNAL_CHANNEL_ID`` channel. One Claude call to extract, one HTTP POST to
append, one formatted reply. No retries (legacy: 시트 중복 행 방지).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from src.claude_adapter import ClaudeCodeAdapter, ClaudeCodeAdapterError
from src.config import Settings
from src.obs import get_logger
from src.skills.storage.sheets_append import (
    SheetsAppendError,
    append_rows,
)

from .extractor import (
    DEFAULT_TZ,
    ExtractionError,
    extract_activities,
)
from .format import (
    format_extraction_failure,
    format_failure,
    format_success,
)

log = get_logger(__name__)


@dataclass(frozen=True)
class JournalPipelineResult:
    response: str
    ok: bool
    rows_written: int = 0
    rows_extracted: int = 0
    extraction_ms: int = 0
    error: str = ""


@dataclass
class JournalPipeline:
    """Wires settings + adapter + handler. Created once per gateway."""

    settings: Settings
    adapter: ClaudeCodeAdapter
    extra: dict[str, Any] = field(default_factory=dict)

    async def handle(
        self,
        user_message: str,
        *,
        now: datetime | None = None,
    ) -> JournalPipelineResult:
        return await handle_journal_message(
            user_message,
            settings=self.settings,
            adapter=self.adapter,
            now=now,
        )


async def handle_journal_message(
    user_message: str,
    *,
    settings: Settings,
    adapter: ClaudeCodeAdapter,
    now: datetime | None = None,
) -> JournalPipelineResult:
    """End-to-end: 자연어 → 24필드 JSON → Apps Script append → 한국어 응답."""
    msg = (user_message or "").strip()
    if not msg:
        return JournalPipelineResult(
            response=format_extraction_failure("빈 메시지"), ok=False, error="empty",
        )

    if not settings.google_sheets_webhook_url:
        return JournalPipelineResult(
            response=format_failure(reason="GOOGLE_SHEETS_WEBHOOK_URL 미설정"),
            ok=False,
            error="webhook_unset",
        )

    now = now or datetime.now(DEFAULT_TZ)

    try:
        extraction = await extract_activities(
            msg, adapter=adapter, now=now,
        )
    except ExtractionError as e:
        log.warning("journal.extract_failed", err=str(e))
        return JournalPipelineResult(
            response=format_extraction_failure(str(e)),
            ok=False,
            error=f"extract:{e}",
        )
    except ClaudeCodeAdapterError as e:
        log.warning("journal.llm_failed", err=str(e))
        return JournalPipelineResult(
            response=format_failure(reason=f"LLM 호출 실패: {type(e).__name__}"),
            ok=False,
            error=f"llm:{type(e).__name__}",
        )

    try:
        result = append_rows(
            extraction.rows,
            webhook_url=settings.google_sheets_webhook_url,
            alert_url=settings.journal_alert_webhook_url,
            alert_title="journal sheets_append failed",
        )
    except SheetsAppendError as e:
        log.warning("journal.append_failed", err=str(e), status=e.status)
        return JournalPipelineResult(
            response=format_failure(reason=str(e), status=e.status),
            ok=False,
            rows_extracted=len(extraction.rows),
            extraction_ms=extraction.duration_ms,
            error=f"append:{e.status}",
        )

    response = format_success(extraction.rows)
    log.info(
        "journal.ok",
        rows=result.rows_written,
        extraction_ms=extraction.duration_ms,
    )
    return JournalPipelineResult(
        response=response,
        ok=True,
        rows_written=result.rows_written,
        rows_extracted=len(extraction.rows),
        extraction_ms=extraction.duration_ms,
    )


__all__ = [
    "JournalPipeline",
    "JournalPipelineResult",
    "handle_journal_message",
]
