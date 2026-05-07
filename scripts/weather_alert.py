#!/usr/bin/env python3
"""KMA apihub 단기예보(getVilageFcst) -> Discord webhook 매일 아침 알림.

기상청 API Hub(apihub.kma.go.kr)의 동네예보를 호출해 오늘의 최저/최고 기온,
시간대별 강수확률·하늘상태를 정리한 뒤 Discord webhook 으로 전송한다.

운영 위치는 `~/.hermes/scripts/weather_alert.py` (secrets 도 같은 폴더 `.env`).
이 파일은 repo 에 보존되는 원본이며, 설치 시 그대로 복사해 사용한다.

Env (`~/.hermes/scripts/.env`):
    KMA_APIHUB_KEY=<apihub auth key>
    DISCORD_WEATHER_WEBHOOK_URL=https://discord.com/api/webhooks/...
    KMA_NX=61                     # optional, default 61 (청량리동)
    KMA_NY=127                    # optional, default 127 (청량리동)
    KMA_LOCATION_LABEL=청량리동   # optional, 메시지 헤더에 사용
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")


def _load_env_file(path: Path) -> None:
    """Tiny .env loader (no python-dotenv dependency)."""
    if not path.exists():
        return
    try:
        for raw in path.read_text(encoding="utf-8").splitlines():
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, _, v = line.partition("=")
            k = k.strip()
            v = v.strip().strip('"').strip("'")
            if k and k not in os.environ:
                os.environ[k] = v
    except OSError:
        pass


_load_env_file(Path(__file__).resolve().parent / ".env")

LOG = logging.getLogger("weather_alert")
API_URL = (
    "https://apihub.kma.go.kr/api/typ02/openApi/"
    "VilageFcstInfoService_2.0/getVilageFcst"
)

SKY_LABEL = {"1": "맑음", "3": "구름많음", "4": "흐림"}
PTY_LABEL = {
    "0": "", "1": "비", "2": "비/눈", "3": "눈", "4": "소나기",
    "5": "빗방울", "6": "빗방울/눈날림", "7": "눈날림",
}


def _base_datetime(now: datetime) -> tuple[str, str]:
    # 02:00 발표분 사용 — TMN/TMX 포함, 02:15 이후 안정 노출.
    # 07:10 cron 호출 시 항상 안전.
    target = now.replace(hour=2, minute=0, second=0, microsecond=0)
    if now < target + timedelta(minutes=15):
        target -= timedelta(days=1)
    return target.strftime("%Y%m%d"), "0200"


def _fetch_items(auth: str, base_date: str, base_time: str,
                 nx: int, ny: int) -> list[dict[str, Any]]:
    qs = urllib.parse.urlencode({
        "authKey": auth,
        "pageNo": "1",
        "numOfRows": "1000",
        "dataType": "JSON",
        "base_date": base_date,
        "base_time": base_time,
        "nx": nx,
        "ny": ny,
    })
    req = urllib.request.Request(
        f"{API_URL}?{qs}",
        headers={"User-Agent": "hermes-weather-alert/1.0"},
    )
    with urllib.request.urlopen(req, timeout=20) as resp:  # noqa: S310
        body = resp.read().decode("utf-8")
    try:
        payload = json.loads(body)
    except json.JSONDecodeError as e:
        raise RuntimeError(f"non-JSON response: {body[:200]}") from e
    items = (
        payload.get("response", {})
        .get("body", {})
        .get("items", {})
        .get("item", [])
    )
    if not items:
        header = payload.get("response", {}).get("header", {})
        raise RuntimeError(f"empty items header={header}")
    return items


def _summarize_today(items: list[dict[str, Any]], today: str) -> dict[str, Any]:
    summary: dict[str, Any] = {"tmn": None, "tmx": None, "slots": []}
    by_time: dict[str, dict[str, str]] = {}
    for it in items:
        if it.get("fcstDate") != today:
            continue
        cat = it["category"]
        val = str(it["fcstValue"])
        if cat == "TMN":
            summary["tmn"] = val
            continue
        if cat == "TMX":
            summary["tmx"] = val
            continue
        by_time.setdefault(it["fcstTime"], {})[cat] = val

    slot_times = ("0600", "0900", "1200", "1500", "1800", "2100")
    for t in slot_times:
        b = by_time.get(t)
        if not b:
            continue
        summary["slots"].append({
            "time": t,
            "tmp": b.get("TMP"),
            "pop": b.get("POP"),
            "sky": b.get("SKY"),
            "pty": b.get("PTY"),
        })
    return summary


def _format_message(summary: dict[str, Any], date_str: str, label: str) -> str:
    weekday = ["월", "화", "수", "목", "금", "토", "일"][
        datetime.strptime(date_str, "%Y%m%d").weekday()
    ]
    pretty = f"{date_str[:4]}-{date_str[4:6]}-{date_str[6:]} ({weekday})"
    tmn = summary["tmn"] or "?"
    tmx = summary["tmx"] or "?"

    pops = [int(s["pop"]) for s in summary["slots"]
            if s.get("pop") not in (None, "")]
    rain = ""
    if pops and max(pops) >= 60:
        rain = "\n☔ 우산 챙기세요 (강수확률 ≥ 60%)"
    elif pops and max(pops) >= 30:
        rain = "\n🌂 비 가능성 있음"

    rows = []
    for s in summary["slots"]:
        hh = s["time"][:2]
        sky = SKY_LABEL.get(s.get("sky") or "", "?")
        pty = PTY_LABEL.get(s.get("pty") or "0", "")
        cond = pty if pty else sky
        rows.append(f"`{hh}시` {s.get('tmp') or '?'}° · {cond} · 강수 {s.get('pop') or '?'}%")
    detail = "\n".join(rows) if rows else "(시간대 데이터 없음)"

    return (
        f"☀️ **{pretty}** · {label}\n"
        f"최저 **{tmn}°** / 최고 **{tmx}°**"
        f"{rain}\n```\n{detail}\n```"
    )


def _send_webhook(url: str, content: str) -> None:
    body = json.dumps({"content": content}).encode("utf-8")
    req = urllib.request.Request(
        url, data=body,
        headers={
            "Content-Type": "application/json",
            "User-Agent": "hermes-weather-alert/1.0 (+https://hermes.local)",
        },
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=15) as resp:  # noqa: S310
        if resp.status >= 300:
            raise RuntimeError(f"webhook status {resp.status}")


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--dry-run", action="store_true",
                   help="전송하지 않고 메시지만 출력")
    p.add_argument("-v", "--verbose", action="store_true")
    args = p.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )

    auth = (os.environ.get("KMA_APIHUB_KEY") or "").strip()
    webhook = (os.environ.get("DISCORD_WEATHER_WEBHOOK_URL") or "").strip()
    nx = int(os.environ.get("KMA_NX", "61"))
    ny = int(os.environ.get("KMA_NY", "127"))
    label = os.environ.get("KMA_LOCATION_LABEL", "청량리동")

    if not auth:
        LOG.error("KMA_APIHUB_KEY missing in environment")
        return 2
    if not args.dry_run and not webhook:
        LOG.error("DISCORD_WEATHER_WEBHOOK_URL missing in environment")
        return 2

    now = datetime.now()
    base_date, base_time = _base_datetime(now)
    today = now.strftime("%Y%m%d")

    try:
        items = _fetch_items(auth, base_date, base_time, nx, ny)
    except (urllib.error.URLError, RuntimeError, json.JSONDecodeError) as e:
        LOG.error("fetch_failed err=%s", e)
        return 1

    summary = _summarize_today(items, today)
    msg = _format_message(summary, today, label)

    if args.dry_run:
        print(msg)
        return 0
    try:
        _send_webhook(webhook, msg)
    except (urllib.error.URLError, RuntimeError) as e:
        LOG.error("webhook_failed err=%s", e)
        return 1
    LOG.info("sent ok base=%s/%s nx=%s ny=%s", base_date, base_time, nx, ny)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
