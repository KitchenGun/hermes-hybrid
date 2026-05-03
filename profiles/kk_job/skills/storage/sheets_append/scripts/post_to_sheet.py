#!/usr/bin/env python3
"""post_to_sheet.py — kk_job Google Sheet appender (raw + curated tabs).

Forked from journal_ops post_to_sheet.py with two key differences:
  1. --tab argument selects column schema (raw 14-col vs curated 7-col)
  2. JOB_SHEETS_WEBHOOK_URL env (separate from journal_ops webhook).
     If unset, the script writes CSV to runtime/sheet_fallback/ instead
     of failing — lets the cron run end-to-end before Apps Script deploy.

Usage:
    printf '%s' '[{...}]' | python3 post_to_sheet.py --tab raw
    printf '%s' '[{...}]' | python3 post_to_sheet.py --tab curated
    printf '%s' '[{...}]' | python3 post_to_sheet.py --tab raw --dry-run

Exit codes:
    0  appended successfully (or CSV fallback written)
    1  invalid input
    2  webhook URL missing AND fallback path also unwritable
    3  HTTP failure / Apps Script returned ok=false
"""
from __future__ import annotations

import argparse
import csv
import json
import os
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib import request as urlreq
from urllib.error import HTTPError, URLError

COLUMNS_RAW: list[str] = [
    "crawled_at", "source", "company", "title", "seniority",
    "employment_type", "location", "requirements", "preferred",
    "tech_stack", "url", "deadline", "raw_text", "applied",
]
COLUMNS_CURATED: list[str] = [
    "date", "company", "title", "match_score", "match_reason",
    "mismatch", "url",
]
TAB_SCHEMAS = {"raw": COLUMNS_RAW, "curated": COLUMNS_CURATED}

MAX_RETRIES = 1
TIMEOUT_SEC = 15
USER_AGENT = "hermes-hybrid-kk_job/1.0 (+https://github.com/anthropics/hermes-hybrid)"

# Default fallback dir lives next to the script's profile root.
# script path: <root>/skills/storage/sheets_append/scripts/post_to_sheet.py
# → fallback: <root>/runtime/sheet_fallback/
DEFAULT_FALLBACK_DIR = (
    Path(__file__).resolve().parents[3] / "runtime" / "sheet_fallback"
)


def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Append job rows to kk_job sheet (raw or curated tab)."
    )
    p.add_argument("--tab", required=True, choices=["raw", "curated"],
                   help="Target tab and column schema")
    p.add_argument("--dry-run", action="store_true",
                   help="Render payload only, no HTTP / no CSV.")
    p.add_argument("--fallback-dir",
                   default=str(DEFAULT_FALLBACK_DIR),
                   help="CSV fallback dir when JOB_SHEETS_WEBHOOK_URL is unset")
    return p.parse_args()


def _load_input() -> list[dict[str, Any]]:
    raw = sys.stdin.read().strip()
    if not raw:
        print("[post_to_sheet] stdin empty", file=sys.stderr)
        sys.exit(1)
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as e:
        print(f"[post_to_sheet] invalid JSON: {e}", file=sys.stderr)
        sys.exit(1)
    if isinstance(data, dict):
        return [data]
    if isinstance(data, list):
        if not data:
            print("[post_to_sheet] empty array — nothing to append",
                  file=sys.stderr)
            return []
        return data
    print(f"[post_to_sheet] expected object or array, got {type(data).__name__}",
          file=sys.stderr)
    sys.exit(1)


def _normalize(rows: list[dict[str, Any]], cols: list[str]) -> list[list[Any]]:
    out: list[list[Any]] = []
    for r in rows:
        if not isinstance(r, dict):
            continue
        row: list[Any] = []
        for col in cols:
            val = r.get(col)
            if val is None:
                val = ""
            elif isinstance(val, bool):
                val = "TRUE" if val else "FALSE"
            row.append(val)
        out.append(row)
    return out


def _send(url: str, payload: dict[str, Any]) -> tuple[int, dict[str, Any] | None]:
    data = json.dumps(payload).encode("utf-8")
    req = urlreq.Request(
        url, data=data, method="POST",
        headers={"Content-Type": "application/json", "User-Agent": USER_AGENT},
    )
    last_err: Exception | None = None
    for attempt in range(MAX_RETRIES + 1):
        try:
            with urlreq.urlopen(req, timeout=TIMEOUT_SEC) as resp:
                body_raw = resp.read().decode("utf-8", errors="replace")
                try:
                    body = json.loads(body_raw) if body_raw else None
                except json.JSONDecodeError:
                    body = None
                return resp.status, body
        except HTTPError as e:
            last_err = e
            if 500 <= e.code < 600 and attempt < MAX_RETRIES:
                time.sleep(1.0)
                continue
            try:
                body_raw = e.read().decode("utf-8", errors="replace")
                body = json.loads(body_raw) if body_raw else None
            except (json.JSONDecodeError, AttributeError):
                body = None
            print(f"[post_to_sheet] HTTP {e.code}: {e.reason}", file=sys.stderr)
            return e.code, body
        except URLError as e:
            last_err = e
            if attempt < MAX_RETRIES:
                time.sleep(1.0)
                continue
            break
        except Exception as e:  # noqa: BLE001
            last_err = e
            break
    print(f"[post_to_sheet] failed after retries: {last_err}", file=sys.stderr)
    return -1, None


def _csv_fallback(rows: list[list[Any]], *, tab: str, cols: list[str],
                  fallback_dir: str) -> int:
    """Write a CSV file when the sheets webhook is unconfigured."""
    out_dir = Path(fallback_dir)
    try:
        out_dir.mkdir(parents=True, exist_ok=True)
    except OSError as e:
        print(f"[post_to_sheet] fallback dir unwritable: {e}", file=sys.stderr)
        return 2
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    path = out_dir / f"{tab}_{ts}.csv"
    # errors="replace" guards against surrogates that can leak in when this
    # script is invoked from a Windows shell with a non-utf-8 stdin codec
    # (cp949 in particular). On WSL the stdin path is utf-8 and this is a
    # no-op.
    try:
        with path.open("w", encoding="utf-8", newline="", errors="replace") as f:
            w = csv.writer(f)
            w.writerow(cols)
            w.writerows(rows)
    except OSError as e:
        print(f"[post_to_sheet] csv write failed: {e}", file=sys.stderr)
        return 2
    print(f"FALLBACK csv={path} rows={len(rows)} tab={tab}")
    return 0


def main() -> int:
    args = _parse_args()
    cols = TAB_SCHEMAS[args.tab]
    rows_in = _load_input()
    if not rows_in:
        print(f"OK rows=0 tab={args.tab}")
        return 0
    rows_norm = _normalize(rows_in, cols)
    if not rows_norm:
        print("[post_to_sheet] no valid rows after normalization", file=sys.stderr)
        return 1

    payload = {"tab": args.tab, "rows": rows_norm, "columns": cols}

    if args.dry_run:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return 0

    webhook_url = os.environ.get("JOB_SHEETS_WEBHOOK_URL", "").strip()
    if not webhook_url:
        print(
            "[post_to_sheet] JOB_SHEETS_WEBHOOK_URL not set — using CSV fallback",
            file=sys.stderr,
        )
        return _csv_fallback(rows_norm, tab=args.tab, cols=cols,
                             fallback_dir=args.fallback_dir)

    status, body = _send(webhook_url, payload)
    if status == 200 and isinstance(body, dict) and body.get("ok") is True:
        n = body.get("rows", len(rows_norm))
        print(f"OK rows={n} tab={args.tab}")
        return 0

    err_msg = ""
    if isinstance(body, dict):
        err_msg = str(body.get("error", ""))[:200]
    print(f"[post_to_sheet] sheets_append_failed: status={status}, "
          f"error={err_msg}", file=sys.stderr)
    return 3


if __name__ == "__main__":
    sys.exit(main())
