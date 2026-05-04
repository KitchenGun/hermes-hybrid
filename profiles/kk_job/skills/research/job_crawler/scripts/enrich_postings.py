#!/usr/bin/env python3
"""enrich_postings.py — Fill in seniority/deadline by fetching each detail page.

Crawler list-page extraction misses seniority because gamejob/jobkorea only
expose minimum experience on the detail page. Without this, LLM matching
guesses and recommends mismatched roles (e.g. NC AION2 requires 5+ years
but user is 3y). This pass fetches each unique URL once, parses
"경력 N년 이상", "경력무관", and the deadline, then writes back the
enriched JSON.

Skips entries whose `seniority` is already populated (shiftup case).

Usage:
    python3 enrich_postings.py \
        --input  /tmp/kk_job_raw_marked.json \
        --output /tmp/kk_job_raw_enriched.json \
        [--throttle 0.4]

Exit codes:
    0  output written (some entries may have empty seniority)
    1  invalid args / cannot read input
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import time
from datetime import datetime, timezone, timedelta
from typing import Any
from urllib import request as urlreq
from urllib.error import HTTPError, URLError

KST = timezone(timedelta(hours=9))
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0 Safari/537.36 hermes-hybrid-kk_job/1.0"
)

EXP_MIN_RE = re.compile(r"경력\s*(\d+)\s*년\s*이상")
EXP_RANGE_RE = re.compile(r"경력\s*(\d+)\s*[~∼\-]\s*(\d+)\s*년")
EXP_OPEN_RE = re.compile(r"(신입|경력무관|경력 무관|경력없음|무관)")
DEADLINE_DOT_RE = re.compile(r"마감일\s*[:：]?\s*(\d{4}[.\-/]\d{1,2}[.\-/]\d{1,2})")
DEADLINE_BRACKET_RE = re.compile(r"\[(\d{1,2})[.\-/](\d{1,2})\]")
DEADLINE_OPEN_RE = re.compile(r"\[(상시채용|상시|수시채용)\]")
# Posted-date patterns:
#   gamejob: '...2026-04-28 16:11 등록 ...' inline
#   jobkorea: inline JSON `"firstPostedAt":"2026-04-30T18:55:55+09:00"`
POSTED_GAMEJOB_RE = re.compile(r"(\d{4}-\d{2}-\d{2})[^<\n]{0,40}?등록")
# jobkorea inlines RSC payload where each quote is backslash-escaped:
#   \"firstPostedAt\":\"2026-04-30T14:40:59+09:00\"
# Allow optional preceding/following backslashes around quotes.
POSTED_JOBKOREA_RE = re.compile(
    r'\\?"firstPostedAt\\?"\s*:\s*\\?"(\d{4}-\d{2}-\d{2})'
)


def _http_get(url: str, *, timeout: int) -> str | None:
    req = urlreq.Request(url, headers={
        "User-Agent": USER_AGENT,
        "Accept": "text/html,application/xhtml+xml",
        "Accept-Language": "ko,en;q=0.9",
    })
    try:
        with urlreq.urlopen(req, timeout=timeout) as resp:
            raw = resp.read()
            charset = resp.headers.get_content_charset() or "utf-8"
    except (HTTPError, URLError, TimeoutError) as e:
        print(f"[enrich] http_get fail {url[:80]}: {e}", file=sys.stderr)
        return None
    except Exception as e:  # noqa: BLE001
        print(f"[enrich] http_get unexpected {url[:80]}: {e}", file=sys.stderr)
        return None
    for enc in (charset, "utf-8", "euc-kr", "cp949"):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", errors="replace")


def _parse_seniority(text: str) -> str:
    """Return a normalized seniority string or empty if not found."""
    m = EXP_MIN_RE.search(text)
    if m:
        return f"경력 {m.group(1)}년 이상"
    m = EXP_RANGE_RE.search(text)
    if m:
        return f"경력 {m.group(1)}~{m.group(2)}년"
    m = EXP_OPEN_RE.search(text)
    if m:
        return m.group(1)
    return ""


def _parse_deadline(text: str) -> tuple[str, bool]:
    """Return (deadline_str, expired)."""
    m = DEADLINE_DOT_RE.search(text)
    if m:
        raw = m.group(1).replace("/", "-").replace(".", "-")
        try:
            d = datetime.strptime(raw, "%Y-%m-%d").replace(tzinfo=KST)
        except ValueError:
            return raw, False
        return d.strftime("%Y-%m-%d"), d < datetime.now(KST).replace(hour=0)
    m = DEADLINE_OPEN_RE.search(text)
    if m:
        return "상시", False
    m = DEADLINE_BRACKET_RE.search(text)
    if m:
        now = datetime.now(KST)
        try:
            d = datetime(now.year, int(m.group(1)), int(m.group(2)), tzinfo=KST)
            if d < now - timedelta(days=180):
                d = d.replace(year=now.year + 1)
        except ValueError:
            return "", False
        return d.strftime("%Y-%m-%d"), d < now.replace(hour=0)
    return "", False


def _parse_posted_at(text: str) -> str:
    """Return first-posted ISO date (YYYY-MM-DD) or empty if not found."""
    m = POSTED_JOBKOREA_RE.search(text)
    if m:
        return m.group(1)
    m = POSTED_GAMEJOB_RE.search(text)
    if m:
        return m.group(1)
    return ""


def _enrich_one(url: str, *, timeout: int) -> dict[str, Any]:
    html = _http_get(url, timeout=timeout)
    if not html:
        return {}
    out: dict[str, Any] = {}
    sen = _parse_seniority(html)
    if sen:
        out["seniority"] = sen
    dl, expired = _parse_deadline(html)
    if dl:
        out["deadline"] = dl
        out["expired"] = expired
    posted = _parse_posted_at(html)
    if posted:
        out["posted_at"] = posted
    return out


def main() -> int:
    p = argparse.ArgumentParser(description="Enrich raw postings with seniority/deadline.")
    p.add_argument("--input", required=True, help="Input raw JSON (marked or unmarked)")
    p.add_argument("--output", required=True, help="Output enriched JSON")
    p.add_argument("--throttle", type=float, default=0.4,
                   help="Seconds to sleep between requests (default 0.4)")
    p.add_argument("--timeout", type=int, default=10,
                   help="HTTP timeout per request (default 10)")
    args = p.parse_args()

    try:
        with open(args.input, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError) as e:
        print(f"[enrich] cannot read input: {e}", file=sys.stderr)
        return 1

    if not isinstance(data, list):
        print(f"[enrich] expected JSON array, got {type(data).__name__}", file=sys.stderr)
        return 1

    enriched_count = 0
    skipped_existing = 0
    skipped_no_url = 0
    by_source: dict[str, int] = {}
    for i, item in enumerate(data):
        if not isinstance(item, dict):
            continue
        # shiftup already has seniority from list-page parser
        if (item.get("seniority") or "").strip():
            skipped_existing += 1
            continue
        url = item.get("url", "")
        if not url or url.startswith("javascript:") or "shiftup.co.kr" in url:
            skipped_no_url += 1
            continue
        info = _enrich_one(url, timeout=args.timeout)
        if info:
            item.update(info)
            enriched_count += 1
            by_source[item.get("source", "?")] = by_source.get(item.get("source", "?"), 0) + 1
        if i % 10 == 9:
            print(f"[enrich] progress {i+1}/{len(data)} (enriched={enriched_count})",
                  file=sys.stderr)
        time.sleep(args.throttle)

    print(f"[enrich] enriched={enriched_count}  skipped_existing={skipped_existing}  "
          f"skipped_no_url={skipped_no_url}", file=sys.stderr)
    print(f"[enrich] by source: {by_source}", file=sys.stderr)

    try:
        with open(args.output, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
    except OSError as e:
        print(f"[enrich] cannot write output: {e}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
