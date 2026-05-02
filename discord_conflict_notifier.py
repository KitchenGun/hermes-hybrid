#!/usr/bin/env python3
"""Discord conflict notifier for Google Calendar events.

Usage:
  python3 discord_conflict_notifier.py --event-id <EVENT_ID> [--window-delta MINUTES]

The script does the following:
  1. Fetch the target event by its ID.
  2. Determine the event's time window (start, end).
  3. List events in the same window using Google Calendar API (via MCP).
  4. Exclude the event itself and any private events.
  5. If there are conflicts, print a formatted message.
  6. If no conflicts, print exactly `NO_NOTIFICATION`.

This script intentionally does **not** send any messages.  The output can be piped to a
Discord integration or an email, depending on your automation.
"""

import sys
import argparse
import os
from datetime import datetime, timezone, timedelta
from hermes_tools import mcp_google_calendar_get_event, mcp_google_calendar_list_events

API_TIME_FORMAT = "%Y-%m-%dT%H:%M:%S%z"

# Helper: parse ISO 8601 strings to datetime objects

def parse_iso(iso_str: str) -> datetime:
    return datetime.fromisoformat(iso_str)

# Helper: format datetime to Korean-style date/time

def fmt_datetime(dt: datetime) -> str:
    # Assume local timezone is Asia/Seoul
    local_tz = timezone(timedelta(hours=9))
    dt_local = dt.astimezone(local_tz)
    return dt_local.strftime("%m월 %d일 (%a) %H:%M")


# Helper: parse event time (DateTime or Date)

def get_event_datetime(event: dict, key: str) -> datetime:
    value = event.get(key)
    if not value:
        return None
    if isinstance(value, dict):
        # could be {"date": "2024-05-01"}
        # or {"dateTime": "2024-05-01T10:00:00+09:00"}
        if "dateTime" in value:
            return parse_iso(value["dateTime"])
        if "date" in value:
            # All day, assume start at 00:00
            dt = datetime.strptime(value["date"], "%Y-%m-%d")
            return dt.replace(tzinfo=timezone.utc)
    return None


# Determine if two time windows overlap

def times_overlap(start1, end1, start2, end2) -> bool:
    return max(start1, start2) < min(end1, end2)


def main():
    parser = argparse.ArgumentParser(description="Check for calendar event conflicts.")
    parser.add_argument("--event-id", required=True, help="Event ID of the newly created/updated event")
    parser.add_argument("--window-delta", type=int, default=None, help="Minutes after event start to check for conflicts")
    args = parser.parse_args()

    # Get event details
    try:
        event_resp = mcp_google_calendar_get_event(
            account="", calendarId="", eventId=args.event_id, fields=None
        )
    except Exception as e:
        print(f"ERROR fetching event: {e}")
        sys.exit(1)

    event = event_resp.get("data", {})
    if not event:
        print("ERROR: no event data returned")
        sys.exit(1)

    title = event.get("summary", "[제목 없음]")
    start = get_event_datetime(event, "start")
    end = get_event_datetime(event, "end")
    if not start or not end:
        print("ERROR: cannot parse start or end time")
        sys.exit(1)

    # Determine window: if window_delta provided, adjust end to start + delta
    if args.window_delta:
        end = start + timedelta(minutes=args.window_delta)

    # List events in window
    try:
        list_resp = mcp_google_calendar_list_events(
            account="", calendarId="", timeMin=start.isoformat(), timeMax=end.isoformat(), timeZone="Asia/Seoul", max_results=50, fields=None
        )
    except Exception as e:
        print(f"ERROR listing events: {e}")
        sys.exit(1)

    events = list_resp.get("data", {}).get("events", [])
    conflicts = []
    for e in events:
        if e.get("id") == args.event_id:
            continue
        # Skip private events
        if e.get("visibility") == "private":
            continue
        s = get_event_datetime(e, "start")
        en = get_event_datetime(e, "end")
        if times_overlap(start, end, s, en):
            conflicts.append(e)

    if not conflicts:
        print("NO_NOTIFICATION")
        sys.exit(0)

    # Build notification message
    msg_lines = []
    msg_lines.append(f"⚠️ 일정 충돌 감지")
    msg_lines.append(f"새 이벤트: {title} ({fmt_datetime(start)[:5]}-{fmt_datetime(end)[:5]})")
    msg_lines.append("겹치는 기존 이벤트:")
    for c in conflicts:
        cs = get_event_datetime(c, "start")
        ce = get_event_datetime(c, "end")
        msg_lines.append(f"  • {fmt_datetime(cs)[5:10]} {cs.strftime('%H:%M')}–{ce.strftime('%H:%M')} {c.get('summary', '')}")
    msg_lines.append("선택지: [새 이벤트 시간 변경] [기존 이벤트 조정] [그대로 유지]")
    print("\n".join(msg_lines))


if __name__ == "__main__":
    main()
