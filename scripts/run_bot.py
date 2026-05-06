"""Entry point for the Discord gateway.

Performs preflight (R6+R15), initializes persistence (R4), starts the bot.

Phase 8/10 (2026-05-06): master = opencode CLI / gpt-5.5. Hermes CLI 의존
없음. profile cron 폐기로 Ollama base_url WSL→Windows 갱신 hook 도 제거.
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# Populate os.environ from .env so dynamic-name env vars (NAVER_APP_PASSWORD,
# DISCORD_*_WEBHOOK_URL 등) 가 visible. pydantic-settings 는 .env 를 Settings
# 필드로만 읽지 os.environ 에 반영하지 않으므로, os.environ.get(...) 직호출
# 코드가 있는 한 이 hook 이 필요.
try:
    from dotenv import load_dotenv  # noqa: E402
    load_dotenv(ROOT / ".env")
except ImportError:  # pragma: no cover
    pass

from src.config import get_settings  # noqa: E402
from src.gateway import run_bot  # noqa: E402
from src.obs import get_logger, setup_logging  # noqa: E402
from src.preflight import run_preflight  # noqa: E402
from src.state import Repository  # noqa: E402


async def _startup() -> tuple[bool, Repository]:
    settings = get_settings()
    setup_logging(settings.log_level, json=settings.log_json)
    log = get_logger(__name__)

    report = await run_preflight(settings, require_gateway_stopped=True)
    for w in report.warnings:
        log.warning("preflight.warning", msg=w)
    for e in report.errors:
        log.error("preflight.error", msg=e)
    if not report.ok:
        return False, None  # type: ignore[return-value]

    repo = Repository(settings.state_db_path)
    await repo.init()
    log.info("repo.ready", path=str(settings.state_db_path))
    return True, repo


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    if hasattr(sys.stderr, "reconfigure"):
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    ok, repo = asyncio.run(_startup())
    if not ok:
        return 2
    settings = get_settings()
    run_bot(settings, repo)
    return 0


if __name__ == "__main__":
    sys.exit(main())
