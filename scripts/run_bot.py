"""Entry point for the Discord gateway.

Performs preflight (R6+R15), initializes persistence (R4), starts the bot.
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# Populate os.environ from .env so dynamic-name env vars (e.g. NAVER_APP_PASSWORD,
# referenced by accounts.yaml's password_env) are visible. pydantic-settings
# only reads .env into declared Settings fields, not into os.environ, so any
# code path doing os.environ.get(...) directly needs this.
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


def _refresh_ollama_base_urls(settings) -> None:
    """Every Hermes profile that calls Ollama via WSL→Windows host shares the
    same OPENAI_BASE_URL. The host IP can shift across WSL reboots, so patch
    OPENAI_BASE_URL in each profile's .env every boot. Best-effort: never
    blocks startup. Generalized from the journal_ops-only refresh hook to
    cover all four profiles (journal/calendar/mail/kk_job) under the
    local-first migration."""
    import subprocess
    script_win = ROOT / "scripts" / "refresh_ollama_base_urls.sh"
    drive = script_win.drive[:-1].lower()  # 'E:' -> 'e'
    rest = str(script_win)[len(script_win.drive):].replace("\\", "/")
    script_wsl = f"/mnt/{drive}{rest}"
    try:
        subprocess.run(
            ["wsl", "-d", settings.hermes_wsl_distro, "bash", script_wsl],
            check=False, capture_output=True, text=True, timeout=15,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        pass


async def _startup() -> tuple[bool, Repository]:
    settings = get_settings()
    setup_logging(settings.log_level, json=settings.log_json)
    log = get_logger(__name__)

    _refresh_ollama_base_urls(settings)

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
