#!/usr/bin/env python3
"""crawl_game_jobs.py — Game programmer posting crawler for kk_job.

Collects postings from 5 Korean game-industry sources and writes a
normalized JSON array to --output. Per-source failures are logged to
stderr but never abort the whole run; an empty result still exits 0.

Usage:
    python3 crawl_game_jobs.py \
        --keywords "게임 프로그래머,클라이언트 프로그래머,Unreal,UE5" \
        --output /tmp/kk_job_raw.json

Exit codes:
    0  output file written (may contain 0 items)
    1  invalid arguments / cannot write output
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import time
import urllib.parse
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone, timedelta
from typing import Any, Iterable
from urllib import request as urlreq
from urllib.error import HTTPError, URLError

KST = timezone(timedelta(hours=9))
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0 Safari/537.36 hermes-hybrid-kk_job/1.0"
)
DEFAULT_KEYWORDS = "게임 프로그래머,클라이언트 프로그래머,Unreal,언리얼,UE5,Game Programmer"


@dataclass
class Posting:
    crawled_at: str
    source: str
    company: str
    title: str
    seniority: str = ""
    employment_type: str = ""
    location: str = ""
    requirements: str = ""
    preferred: str = ""
    tech_stack: str = ""
    url: str = ""
    deadline: str = ""
    raw_text: str = ""
    applied: bool = False
    expired: bool = False


def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Crawl game-industry job postings.")
    p.add_argument("--keywords", default=DEFAULT_KEYWORDS,
                   help="Comma-separated search keywords")
    p.add_argument("--output", required=True, help="Output JSON file path")
    p.add_argument("--per-source-limit", type=int, default=30,
                   help="Max postings per source (default 30)")
    p.add_argument("--timeout", type=int, default=12,
                   help="HTTP timeout per request, seconds (default 12)")
    p.add_argument("--source", action="append", default=None,
                   help="Limit to specific source (repeatable). Default: all 5.")
    return p.parse_args()


def _http_get(url: str, *, timeout: int) -> str | None:
    """Return decoded HTML text or None on failure (caller logs)."""
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
        print(f"[crawler] http_get failed for {url[:80]}: {e}", file=sys.stderr)
        return None
    except Exception as e:  # noqa: BLE001
        print(f"[crawler] http_get unexpected error for {url[:80]}: {e}",
              file=sys.stderr)
        return None
    for enc in (charset, "utf-8", "euc-kr", "cp949"):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", errors="replace")


def _normalize_url(u: str) -> str:
    """Drop tracking querystring; keep path-only identity."""
    if not u:
        return u
    parsed = urllib.parse.urlparse(u)
    drop = {"utm_source", "utm_medium", "utm_campaign", "utm_term",
            "utm_content", "fbclid", "gclid", "ref"}
    qs = [(k, v) for k, v in urllib.parse.parse_qsl(parsed.query)
          if k not in drop]
    new_q = urllib.parse.urlencode(qs)
    return urllib.parse.urlunparse(parsed._replace(query=new_q, fragment=""))


def _parse_deadline(text: str) -> tuple[str, bool]:
    """Return (deadline_str, expired_bool)."""
    if not text:
        return "", False
    t = text.strip()
    if any(k in t for k in ("상시", "수시", "채용시")):
        return "상시", False
    m = re.search(r"(\d{4})[.\-/](\d{1,2})[.\-/](\d{1,2})", t)
    if m:
        try:
            d = datetime(int(m.group(1)), int(m.group(2)), int(m.group(3)),
                         tzinfo=KST)
        except ValueError:
            return t[:32], False
        expired = d < datetime.now(KST).replace(hour=0, minute=0, second=0)
        return d.strftime("%Y-%m-%d"), expired
    m = re.search(r"~\s*(\d{1,2})[.\-/](\d{1,2})", t)
    if m:
        now = datetime.now(KST)
        try:
            d = datetime(now.year, int(m.group(1)), int(m.group(2)), tzinfo=KST)
            if d < now - timedelta(days=180):
                d = d.replace(year=now.year + 1)
        except ValueError:
            return t[:32], False
        return d.strftime("%Y-%m-%d"), d < now.replace(hour=0)
    if "오늘" in t:
        return datetime.now(KST).strftime("%Y-%m-%d"), False
    return t[:32], False


# ── HTML helpers (regex-based; kept dependency-free on purpose) ─────────
def _strip_tags(html: str) -> str:
    return re.sub(r"<[^>]+>", " ", html)


def _collapse(s: str) -> str:
    return re.sub(r"\s+", " ", s).strip()


def _extract_attr(html: str, attr: str) -> str:
    m = re.search(rf'{attr}\s*=\s*"([^"]*)"', html)
    return m.group(1) if m else ""


# ── Sources (each returns list[Posting]; failures raise) ────────────────
def _fetch_gamejob(keywords: list[str], *, limit: int, timeout: int,
                   now_iso: str) -> list[Posting]:
    out: list[Posting] = []
    for kw in keywords[:3]:  # 키워드 상위 3개만 (rate limit 절약)
        q = urllib.parse.quote(kw)
        url = f"https://www.gamejob.co.kr/Search/Recruit/Result?searchKey={q}"
        html = _http_get(url, timeout=timeout)
        if not html:
            continue
        for chunk in re.findall(
            r'<a[^>]+href="(/Recruit/[^"]+)"[^>]*>([^<]+)</a>', html
        ):
            href, title = chunk
            full = "https://www.gamejob.co.kr" + href
            out.append(Posting(
                crawled_at=now_iso, source="gamejob",
                company="", title=_collapse(title)[:200],
                url=_normalize_url(full),
                raw_text=f"keyword={kw}",
            ))
            if len(out) >= limit:
                return out
        time.sleep(0.5)
    return out


def _fetch_jobkorea(keywords: list[str], *, limit: int, timeout: int,
                    now_iso: str) -> list[Posting]:
    out: list[Posting] = []
    for kw in keywords[:3]:
        q = urllib.parse.quote(kw)
        url = f"https://www.jobkorea.co.kr/Search/?stext={q}&tabType=recruit"
        html = _http_get(url, timeout=timeout)
        if not html:
            continue
        for m in re.finditer(
            r'<a[^>]+class="title[^"]*"[^>]+href="([^"]+)"[^>]*>'
            r'([^<]+)</a>', html
        ):
            href = m.group(1)
            if href.startswith("/"):
                href = "https://www.jobkorea.co.kr" + href
            out.append(Posting(
                crawled_at=now_iso, source="jobkorea",
                company="", title=_collapse(m.group(2))[:200],
                url=_normalize_url(href),
                raw_text=f"keyword={kw}",
            ))
            if len(out) >= limit:
                return out
        time.sleep(0.5)
    return out


def _fetch_nexon(_: list[str], *, limit: int, timeout: int,
                 now_iso: str) -> list[Posting]:
    """Nexon careers — JSON endpoint preferred; fallback to HTML."""
    out: list[Posting] = []
    api = "https://career.nexon.com/api/recruits?kind=programming&page=1&size=50"
    try:
        req = urlreq.Request(api, headers={"User-Agent": USER_AGENT,
                                           "Accept": "application/json"})
        with urlreq.urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read().decode("utf-8", errors="replace"))
        items = data.get("items") or data.get("results") or data.get("data") or []
        for it in items:
            if not isinstance(it, dict):
                continue
            title = it.get("title") or it.get("name") or ""
            posting_id = it.get("id") or it.get("recruitId") or ""
            url = (f"https://career.nexon.com/recruit/{posting_id}"
                   if posting_id else "https://career.nexon.com/")
            deadline_raw = it.get("endDate") or it.get("closingDate") or ""
            dl, expired = _parse_deadline(str(deadline_raw))
            out.append(Posting(
                crawled_at=now_iso, source="nexon",
                company="넥슨", title=_collapse(str(title))[:200],
                url=_normalize_url(url),
                deadline=dl, expired=expired,
                employment_type=str(it.get("employmentType", "")),
                location=str(it.get("workLocation", "")),
                raw_text=json.dumps(it, ensure_ascii=False)[:1000],
            ))
            if len(out) >= limit:
                return out
        return out
    except Exception as e:  # noqa: BLE001
        print(f"[crawler] nexon api failed: {e}", file=sys.stderr)

    # HTML fallback
    html = _http_get("https://career.nexon.com/user/recruit/list?recruitJobsKind=10",
                     timeout=timeout)
    if not html:
        return out
    for m in re.finditer(
        r'<a[^>]+href="(/user/recruit/[^"]+)"[^>]*>(.*?)</a>',
        html, flags=re.DOTALL,
    ):
        href = "https://career.nexon.com" + m.group(1)
        title = _collapse(_strip_tags(m.group(2)))[:200]
        if not title:
            continue
        out.append(Posting(
            crawled_at=now_iso, source="nexon", company="넥슨",
            title=title, url=_normalize_url(href),
        ))
        if len(out) >= limit:
            break
    return out


def _fetch_ncsoft(_: list[str], *, limit: int, timeout: int,
                  now_iso: str) -> list[Posting]:
    out: list[Posting] = []
    url = "https://careers.ncsoft.com/career/list?searchType=programming"
    html = _http_get(url, timeout=timeout)
    if not html:
        return out
    for m in re.finditer(
        r'<a[^>]+href="([^"]*career[^"]*detail[^"]*)"[^>]*>(.*?)</a>',
        html, flags=re.DOTALL,
    ):
        href = m.group(1)
        if href.startswith("/"):
            href = "https://careers.ncsoft.com" + href
        title = _collapse(_strip_tags(m.group(2)))[:200]
        if not title:
            continue
        out.append(Posting(
            crawled_at=now_iso, source="ncsoft", company="NCSOFT",
            title=title, url=_normalize_url(href),
        ))
        if len(out) >= limit:
            break
    return out


def _fetch_netmarble(_: list[str], *, limit: int, timeout: int,
                     now_iso: str) -> list[Posting]:
    out: list[Posting] = []
    url = ("https://recruit.netmarble.com/main/recruitList.nm"
           "?categoryDevDuty=client")
    html = _http_get(url, timeout=timeout)
    if not html:
        return out
    for m in re.finditer(
        r'<a[^>]+href="([^"]*recruitDetail[^"]*)"[^>]*>(.*?)</a>',
        html, flags=re.DOTALL,
    ):
        href = m.group(1)
        if href.startswith("/"):
            href = "https://recruit.netmarble.com" + href
        title = _collapse(_strip_tags(m.group(2)))[:200]
        if not title:
            continue
        out.append(Posting(
            crawled_at=now_iso, source="netmarble", company="넷마블",
            title=title, url=_normalize_url(href),
        ))
        if len(out) >= limit:
            break
    return out


SOURCES = {
    "gamejob": _fetch_gamejob,
    "jobkorea": _fetch_jobkorea,
    "nexon": _fetch_nexon,
    "ncsoft": _fetch_ncsoft,
    "netmarble": _fetch_netmarble,
}


def _dedupe(items: Iterable[Posting]) -> list[Posting]:
    seen: dict[str, Posting] = {}
    for p in items:
        key = p.url or f"{p.source}::{p.title}"
        if key in seen:
            continue
        seen[key] = p
    return list(seen.values())


def main() -> int:
    args = _parse_args()
    keywords = [k.strip() for k in args.keywords.split(",") if k.strip()]
    if not keywords:
        print("[crawler] no keywords given", file=sys.stderr)
        return 1

    sources = args.source or list(SOURCES.keys())
    now_iso = datetime.now(KST).isoformat(timespec="seconds")

    all_postings: list[Posting] = []
    for src in sources:
        fn = SOURCES.get(src)
        if not fn:
            print(f"[crawler] unknown source skipped: {src}", file=sys.stderr)
            continue
        try:
            got = fn(keywords, limit=args.per_source_limit,
                     timeout=args.timeout, now_iso=now_iso)
            print(f"[crawler] {src}: collected {len(got)}", file=sys.stderr)
            all_postings.extend(got)
        except Exception as e:  # noqa: BLE001
            print(f"[crawler] {src} failed: {e}", file=sys.stderr)
        time.sleep(0.5)

    deduped = _dedupe(all_postings)
    print(f"[crawler] total after dedupe: {len(deduped)}", file=sys.stderr)

    try:
        with open(args.output, "w", encoding="utf-8") as f:
            json.dump([asdict(p) for p in deduped], f,
                      ensure_ascii=False, indent=2)
    except OSError as e:
        print(f"[crawler] cannot write output: {e}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
