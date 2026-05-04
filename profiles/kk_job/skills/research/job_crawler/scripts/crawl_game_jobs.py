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
    """game-job.co.kr — Korean game-industry-only board.

    Uses the duty=1 (programming) listing page; site is already game-only so
    we skip per-keyword searches. Company name is recovered from inline GA
    tracking calls (`GA_Application_Prdt(...,'GI_No',...,'CompanyName',...)`)
    and joined to anchors by GI_No.
    """
    out: list[Posting] = []
    url = "https://www.gamejob.co.kr/Recruit/Joblist?menucode=duty&duty=1"
    html = _http_get(url, timeout=timeout)
    if not html:
        return out

    ga_company: dict[str, str] = {}
    for args_str in re.findall(r"GA_Application_Prdt\(([^)]+)\)", html):
        parts = re.findall(r"'([^']*)'", args_str)
        if len(parts) >= 6 and parts[3].isdigit():
            gid = parts[3]
            company = re.sub(
                r"[\s ]*(채용|모집|공채)\s*$", "", parts[5].strip()
            ).strip()
            if gid not in ga_company and company:
                ga_company[gid] = company

    seen: set[str] = set()
    for href, gid, raw_title in re.findall(
        r'<a\s+href="(/Recruit/GI_Read/View\?GI_No=(\d+))"[^>]*>([^<]{2,200})</a>',
        html,
    ):
        if gid in seen:
            continue
        seen.add(gid)
        title = _collapse(raw_title)[:200]
        if not title:
            continue
        out.append(Posting(
            crawled_at=now_iso, source="gamejob",
            company=ga_company.get(gid, ""), title=title,
            url=_normalize_url("https://www.gamejob.co.kr" + href),
            raw_text=f"GI_No={gid}",
        ))
        if len(out) >= limit:
            break
    return out


def _fetch_jobkorea(keywords: list[str], *, limit: int, timeout: int,
                    now_iso: str) -> list[Posting]:
    """jobkorea.co.kr — Next.js/React page; postings live in CardJob blocks.

    Each result card is delimited by `data-sentry-component="CardJob"`. We
    split on those, then within each block grab the absolute GI_Read URL
    and recover company name from the company logo's `<img alt="회사 로고">`.
    Title is best-effort: text inside the GI_Read anchor with tags stripped.
    """
    out: list[Posting] = []
    seen: set[str] = set()
    for kw in keywords[:3]:
        if len(out) >= limit:
            break
        q = urllib.parse.quote(kw)
        url = f"https://www.jobkorea.co.kr/Search/?stext={q}&tabType=recruit"
        html = _http_get(url, timeout=timeout)
        if not html:
            continue
        blocks = re.findall(
            r'data-sentry-component="CardJob".*?'
            r'(?=data-sentry-component="CardJob"|</body)',
            html, flags=re.DOTALL,
        )
        for blk in blocks:
            if len(out) >= limit:
                break
            u = re.search(
                r'href="(https://www\.jobkorea\.co\.kr/Recruit/GI_Read/(\d+)\?[^"]+)"',
                blk,
            )
            if not u:
                continue
            full_url, gid = u.group(1).replace("&amp;", "&"), u.group(2)
            if gid in seen:
                continue
            seen.add(gid)
            co_m = re.search(r'<img\s+alt="([^"]{2,40}?)\s*로고"', blk)
            company = co_m.group(1).strip() if co_m else ""
            # Each CardJob has up to 3 anchors with the same GI_Read URL:
            # CompanyLogo (image-only), Title (job name), and a company-name
            # link. Pick the first anchor whose stripped inner text is real.
            title = ""
            for inner in re.findall(
                r'href="https://www\.jobkorea\.co\.kr/Recruit/GI_Read/' + gid +
                r'\?[^"]+"[^>]*>(.*?)</a>',
                blk, flags=re.DOTALL,
            ):
                candidate = _collapse(_strip_tags(inner))
                if len(candidate) >= 2 and candidate != company:
                    title = candidate[:200]
                    break
            out.append(Posting(
                crawled_at=now_iso, source="jobkorea",
                company=company, title=title,
                url=_normalize_url(full_url),
                raw_text=f"keyword={kw};GI_No={gid}",
            ))
        time.sleep(0.5)
    return out


def _fetch_nexon(_: list[str], *, limit: int, timeout: int,
                 now_iso: str) -> list[Posting]:
    """careers.nexon.com is a SPA (1KB shell HTML, client-rendered).

    The previous implementation hit `career.nexon.com` (singular) which is
    a dead host. The live site `careers.nexon.com` returns 403 to bots and
    its sitemap.xml only lists category landing pages, not individual
    postings. Static crawl is not viable without a headless browser.
    """
    print("[crawler] nexon: skipped (careers.nexon.com is a SPA — needs "
          "headless browser; no JSON endpoint exposed)", file=sys.stderr)
    return []


def _fetch_ncsoft(_: list[str], *, limit: int, timeout: int,
                  now_iso: str) -> list[Posting]:
    """careers.ncsoft.com renders job lists via jQuery template post-load.

    The page returns 48KB of layout HTML but the AJAX call that fills the
    list lives in a deferred chunk we couldn't isolate without a browser.
    """
    print("[crawler] ncsoft: skipped (careers.ncsoft.com renders job list "
          "via deferred jQuery template; no static endpoint)", file=sys.stderr)
    return []


def _fetch_netmarble(_: list[str], *, limit: int, timeout: int,
                     now_iso: str) -> list[Posting]:
    """`recruit.netmarble.com` is a dead host; `company.netmarble.com` is
    the surviving site but its recruit page is also client-rendered."""
    print("[crawler] netmarble: skipped (recruit.netmarble.com host is "
          "dead; company.netmarble.com is a SPA)", file=sys.stderr)
    return []


def _fetch_shiftup(_: list[str], *, limit: int, timeout: int,
                   now_iso: str) -> list[Posting]:
    """SHIFTUP — Stellar Blade studio (UE5).

    Single-page recruit list at /recruit/recruit.php; each posting is an
    inline block with `<span class='status ing|end'>...</span><h4>title</h4>
    <ul><li>title</li><li>experience</li><li>employment_type</li></ul>`.

    Active postings (status=ing) are ordered first so that --per-source-limit
    keeps the actionable ones when the cap is small.
    """
    out: list[Posting] = []
    html = _http_get("http://www.shiftup.co.kr/recruit/recruit.php",
                     timeout=timeout)
    if not html:
        return out

    blocks = re.findall(
        r"class=\"recruit_title\">[\s\S]{0,200}?"
        r"<span class='?status\s+(ing|end)'?>[^<]+</span>[\s\S]{0,200}?"
        r"<h4>([^<]+)</h4>[\s\S]{0,500}?"
        r"<ul>([\s\S]{0,500}?)</ul>",
        html,
    )
    parsed: list[tuple[str, str, str, str]] = []
    for status, title, ul_html in (
        (b[0], _collapse(b[1]), b[2]) for b in blocks
    ):
        lis = re.findall(r"<li>([^<]+)</li>", ul_html)
        exp = _collapse(lis[1]) if len(lis) >= 2 else ""
        emp = _collapse(lis[2]) if len(lis) >= 3 else ""
        parsed.append((status, title, exp, emp))

    # Active first, then expired — keeps actionable rows under tight limits.
    parsed.sort(key=lambda r: 0 if r[0] == "ing" else 1)

    for status, title, exp, emp in parsed:
        if len(out) >= limit:
            break
        if not title:
            continue
        out.append(Posting(
            crawled_at=now_iso, source="shiftup", company="시프트업",
            title=title[:200],
            seniority=exp, employment_type=emp,
            url="http://www.shiftup.co.kr/recruit/",
            expired=(status == "end"),
            raw_text=f"status={status}",
        ))
    return out


SOURCES = {
    "gamejob": _fetch_gamejob,
    "jobkorea": _fetch_jobkorea,
    "nexon": _fetch_nexon,
    "ncsoft": _fetch_ncsoft,
    "netmarble": _fetch_netmarble,
    "shiftup": _fetch_shiftup,
}


def _dedupe(items: Iterable[Posting]) -> list[Posting]:
    """Drop duplicates while preserving postings that share a URL.

    Single-page boards (e.g. SHIFTUP) emit many postings under the same
    landing URL — keying on URL alone collapses them to one. The triple
    (source, url, title) keeps those distinct while still deduplicating
    real cross-fetch duplicates (same posting picked up twice by retries
    or overlapping keyword searches inside a source).
    """
    seen: dict[tuple[str, str, str], Posting] = {}
    for p in items:
        key = (p.source, p.url, p.title)
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
