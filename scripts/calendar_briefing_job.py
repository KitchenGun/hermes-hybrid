#!/usr/bin/env python3
"""Daily calendar briefing — fetches today's events from Google Calendar
and posts a single Discord webhook message.

Reuses the OAuth client secret already on disk for the Gmail integration
(``secrets/google_oauth_client.json``); the Calendar token lives at a
separate path (``GOOGLE_CALENDAR_TOKEN_PATH``) because the scope differs
(``calendar.readonly`` vs. ``gmail.readonly``).

Multiple calendars are supported via ``GOOGLE_CALENDAR_IDS`` (comma-
separated). Defaults to ``primary``. Events are merged, deduped by
(start, summary), and sorted by start time.

Usage
-----
    # 1) bootstrap OAuth token (one-time, opens browser):
    python scripts/calendar_briefing_job.py --auth

    # 2) verify without sending:
    python scripts/calendar_briefing_job.py --dry-run -v

    # 3) send today's briefing to Discord:
    python scripts/calendar_briefing_job.py

    # offset: run for tomorrow / yesterday
    python scripts/calendar_briefing_job.py --day +1
    python scripts/calendar_briefing_job.py --day -1

Designed to be invoked by systemd-user timer at 07:30 KST daily
(scripts/install_calendar_briefing_timer.sh).
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import urllib.error
import urllib.request
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

try:
    from dotenv import load_dotenv  # type: ignore
    load_dotenv(PROJECT_ROOT / ".env")
except Exception:  # noqa: BLE001
    pass

CALENDAR_SCOPES = ["https://www.googleapis.com/auth/calendar.readonly"]
DEFAULT_TOKEN_PATH = PROJECT_ROOT / "secrets" / "google_calendar_token.json"
DEFAULT_CLIENT_SECRET = PROJECT_ROOT / "secrets" / "google_oauth_client.json"
WEBHOOK_ENV = "DISCORD_CALENDAR_WEBHOOK_URL"
FALLBACK_WEBHOOK_ENV = "DISCORD_BRIEFING_WEBHOOK_URL"
LOG = logging.getLogger("calendar_briefing_job")

WEEKDAY_KO = ["월", "화", "수", "목", "금", "토", "일"]


def _resolve_path(env_value: str | None, default: Path) -> Path:
    if not env_value:
        return default
    p = Path(env_value).expanduser()
    if not p.is_absolute():
        p = PROJECT_ROOT / p
    return p


def _calendar_ids() -> list[str]:
    raw = os.environ.get("GOOGLE_CALENDAR_IDS")
    if not raw or not raw.strip():
        raw = os.environ.get("GOOGLE_CALENDAR_ID") or "primary"
    return [c.strip() for c in raw.split(",") if c.strip()] or ["primary"]


def _load_credentials():  # type: ignore[no-untyped-def]
    try:
        from google.auth.transport.requests import Request  # type: ignore
        from google.oauth2.credentials import Credentials  # type: ignore
    except ImportError as e:  # pragma: no cover
        sys.exit(
            "google-auth is required. Install with: pip install -e .[mail]"
        )
    token_path = _resolve_path(os.environ.get("GOOGLE_CALENDAR_TOKEN_PATH"), DEFAULT_TOKEN_PATH)
    if not token_path.exists():
        sys.exit(
            f"Calendar token not found at {token_path}. "
            "Run: python scripts/calendar_briefing_job.py --auth"
        )
    creds = Credentials.from_authorized_user_file(str(token_path), CALENDAR_SCOPES)
    if not creds.valid:
        if creds.expired and creds.refresh_token:
            creds.refresh(Request())  # type: ignore[call-arg]
            token_path.write_text(creds.to_json(), encoding="utf-8")
        else:
            sys.exit(
                f"Calendar credentials invalid at {token_path}. Re-run --auth."
            )
    return creds


def _build_service():  # type: ignore[no-untyped-def]
    try:
        from googleapiclient.discovery import build  # type: ignore
    except ImportError:  # pragma: no cover
        sys.exit("google-api-python-client is required. Install with: pip install -e .[mail]")
    return build("calendar", "v3", credentials=_load_credentials(), cache_discovery=False)  # type: ignore[operator]


def _do_auth() -> int:
    try:
        from google_auth_oauthlib.flow import InstalledAppFlow  # type: ignore
    except ImportError:
        sys.exit(
            "google-auth-oauthlib is required for --auth. "
            "Install with: pip install -e .[mail]"
        )
    cred_path = _resolve_path(os.environ.get("GOOGLE_OAUTH_CREDENTIALS"), DEFAULT_CLIENT_SECRET)
    if not cred_path.exists():
        sys.exit(
            f"OAuth client secret not found: {cred_path}. "
            "Set GOOGLE_OAUTH_CREDENTIALS or place the JSON at the default path."
        )
    token_path = _resolve_path(os.environ.get("GOOGLE_CALENDAR_TOKEN_PATH"), DEFAULT_TOKEN_PATH)
    token_path.parent.mkdir(parents=True, exist_ok=True)
    print(
        "\n>>> Browser will open. Sign in with the Google account whose calendar "
        "you want to read.\n"
    )
    flow = InstalledAppFlow.from_client_secrets_file(str(cred_path), CALENDAR_SCOPES)
    creds = flow.run_local_server(port=0)
    token_path.write_text(creds.to_json(), encoding="utf-8")
    print(f"\n[OK] calendar token saved to {token_path}")
    return 0


def _target_window(day_offset: int) -> tuple[datetime, datetime, date]:
    """Return (timeMin, timeMax, target_date) for the given day offset.

    Uses the system local timezone so the briefing reflects "today" as the
    user experiences it. Window is [00:00 local, 24:00 local).
    """
    local_tz = datetime.now().astimezone().tzinfo
    today_local = datetime.now(tz=local_tz).date()
    target = today_local + timedelta(days=day_offset)
    start = datetime.combine(target, time(0, 0), tzinfo=local_tz)
    end = start + timedelta(days=1)
    return start, end, target


def _fetch_events(svc, calendar_id: str, time_min: datetime, time_max: datetime) -> list[dict]:  # type: ignore[no-untyped-def, type-arg]
    try:
        resp = svc.events().list(  # type: ignore[attr-defined]
            calendarId=calendar_id,
            timeMin=time_min.isoformat(),
            timeMax=time_max.isoformat(),
            singleEvents=True,
            orderBy="startTime",
            maxResults=50,
        ).execute()
    except Exception as e:  # noqa: BLE001
        LOG.warning("calendar_fetch_failed cal=%s err=%s", calendar_id, e)
        return []
    return resp.get("items", []) or []


def _event_start(ev: dict) -> tuple[datetime | None, bool]:  # type: ignore[type-arg]
    """Return (start_dt, is_all_day). For all-day events, dateTime is None
    in the API; we use the date as a midnight local datetime for sorting.
    """
    s = ev.get("start") or {}
    if "dateTime" in s:
        # RFC3339, may have offset.
        dt = datetime.fromisoformat(s["dateTime"].replace("Z", "+00:00"))
        return dt, False
    if "date" in s:
        d = date.fromisoformat(s["date"])
        local_tz = datetime.now().astimezone().tzinfo
        return datetime.combine(d, time(0, 0), tzinfo=local_tz), True
    return None, False


def _event_end(ev: dict) -> datetime | None:  # type: ignore[type-arg]
    e = ev.get("end") or {}
    if "dateTime" in e:
        return datetime.fromisoformat(e["dateTime"].replace("Z", "+00:00"))
    if "date" in e:
        local_tz = datetime.now().astimezone().tzinfo
        return datetime.combine(date.fromisoformat(e["date"]), time(0, 0), tzinfo=local_tz)
    return None


def _format_event_line(ev: dict) -> str:  # type: ignore[type-arg]
    summary = (ev.get("summary") or "(제목 없음)").strip()
    location = (ev.get("location") or "").strip()
    start, all_day = _event_start(ev)
    end = _event_end(ev)
    if all_day or start is None:
        time_part = "`종일`"
    else:
        local = start.astimezone()
        time_str = local.strftime("%H:%M")
        if end is not None and not all_day:
            local_end = end.astimezone()
            # Skip end if same as start (Google quirk for point-in-time events)
            if local_end != local:
                time_str = f"{time_str}-{local_end.strftime('%H:%M')}"
        time_part = f"`{time_str}`"
    parts = [time_part, summary]
    line = " ".join(parts)
    if location:
        line += f" · {location[:60]}"
    return line


def _format_briefing(events: list[dict], target: date) -> str:  # type: ignore[type-arg]
    weekday = WEEKDAY_KO[target.weekday()]
    header_date = f"{target.strftime('%Y-%m-%d')} ({weekday})"
    if not events:
        return f"📅 **오늘 일정** · 등록된 일정 없음 · {header_date}"
    lines = [f"📅 **오늘 일정** · {len(events)}건 · {header_date}", ""]
    for ev in events:
        lines.append(_format_event_line(ev))
    return "\n".join(lines)


def _dedupe_and_sort(events: list[dict]) -> list[dict]:  # type: ignore[type-arg]
    seen: set[tuple[str, str]] = set()
    out: list[dict] = []  # type: ignore[type-arg]
    for ev in events:
        start, _ = _event_start(ev)
        key = (start.isoformat() if start else "", (ev.get("summary") or "").strip())
        if key in seen:
            continue
        seen.add(key)
        out.append(ev)
    out.sort(key=lambda e: (_event_start(e)[0] or datetime.max.replace(tzinfo=timezone.utc)))
    return out


def _send_webhook(webhook_url: str, content: str) -> None:
    body = json.dumps({"content": content}).encode("utf-8")
    req = urllib.request.Request(
        webhook_url,
        data=body,
        headers={
            "Content-Type": "application/json",
            "User-Agent": "hermes-calendar-briefing/1.0 (+https://hermes.local)",
        },
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=15) as resp:  # noqa: S310
        if resp.status >= 300:
            raise RuntimeError(f"webhook status {resp.status}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--auth", action="store_true",
                        help="run interactive OAuth and write token, then exit")
    parser.add_argument("--dry-run", action="store_true",
                        help="fetch + format but do NOT post to Discord")
    parser.add_argument("--day", type=int, default=0,
                        help="day offset relative to today (0=today, +1=tomorrow, -1=yesterday)")
    parser.add_argument("--verbose", "-v", action="store_true")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )

    if args.auth:
        return _do_auth()

    cal_ids = _calendar_ids()
    LOG.info("calendars=%s", cal_ids)
    svc = _build_service()
    time_min, time_max, target = _target_window(args.day)
    LOG.info("window=%s..%s target=%s", time_min.isoformat(), time_max.isoformat(), target)

    events: list[dict] = []  # type: ignore[type-arg]
    for cid in cal_ids:
        items = _fetch_events(svc, cid, time_min, time_max)
        LOG.info("fetched cal=%s n=%d", cid, len(items))
        events.extend(items)

    events = _dedupe_and_sort(events)
    content = _format_briefing(events, target)

    if args.dry_run:
        print(content)
        LOG.info("dry_run total=%d", len(events))
        return 0

    webhook_url = (os.environ.get(WEBHOOK_ENV) or os.environ.get(FALLBACK_WEBHOOK_ENV) or "").strip()
    if not webhook_url:
        LOG.error("environment variable %s (or %s) is empty", WEBHOOK_ENV, FALLBACK_WEBHOOK_ENV)
        return 2
    try:
        _send_webhook(webhook_url, content)
    except (urllib.error.URLError, RuntimeError) as e:
        LOG.error("webhook_failed err=%s", e)
        return 1
    LOG.info("notified events=%d target=%s", len(events), target)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
