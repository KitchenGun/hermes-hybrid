#!/usr/bin/env python3
"""
fetch_weather.py — 서울 청량리(동대문구 전농동) 날씨 본문 생성기.

기상청 위치 코드 1123056000(서울 동대문구 전농동) 좌표를 open-meteo의
무료 forecast API로 조회하여, post_webhook.py 가 그대로 받아쓸 수 있는
Discord embed 본문 텍스트를 stdout 으로 출력한다.

키 발급 불필요 — open-meteo 는 비상업·저빈도 호출에 한해 무인증을 허용.

Usage:
    python3 fetch_weather.py                     # stdout 본문
    python3 fetch_weather.py --out body.txt      # 파일로 저장
    python3 fetch_weather.py --json              # 디버그용 raw JSON 일부

Exit codes:
    0  성공
    2  네트워크/파싱 실패 (재시도 후)
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import datetime
from typing import Any
from urllib import request as urlreq
from urllib.error import HTTPError, URLError

# 청량리역 좌표 — weather.go.kr URL 의 dong/1123056000 기준.
LAT, LON = 37.57997010526071, 127.04773632414528
LOCATION_LABEL = "서울 청량리 (동대문구 전농동)"
TZ = "Asia/Seoul"

API = (
    "https://api.open-meteo.com/v1/forecast"
    f"?latitude={LAT}&longitude={LON}"
    "&current=temperature_2m,apparent_temperature,relative_humidity_2m,"
    "weather_code,precipitation,wind_speed_10m"
    "&hourly=temperature_2m,weather_code,precipitation_probability,precipitation"
    "&daily=weather_code,temperature_2m_max,temperature_2m_min,"
    "sunrise,sunset,precipitation_sum,precipitation_probability_max"
    f"&timezone={TZ}&forecast_days=2"
)

# WMO weather interpretation code → 한국어 라벨 (open-meteo 표 기반).
WMO = {
    0: "맑음",
    1: "대체로 맑음",
    2: "부분 흐림",
    3: "흐림",
    45: "안개", 48: "짙은 안개",
    51: "약한 이슬비", 53: "이슬비", 55: "강한 이슬비",
    56: "약한 어는비", 57: "강한 어는비",
    61: "약한 비", 63: "비", 65: "강한 비",
    66: "약한 어는비", 67: "강한 어는비",
    71: "약한 눈", 73: "눈", 75: "강한 눈",
    77: "싸락눈",
    80: "약한 소나기", 81: "소나기", 82: "강한 소나기",
    85: "약한 눈 소나기", 86: "강한 눈 소나기",
    95: "뇌우", 96: "뇌우 + 약한 우박", 99: "뇌우 + 강한 우박",
}

WEEKDAY = ["월", "화", "수", "목", "금", "토", "일"]

USER_AGENT = "hermes-hybrid-cron/0.1 (weather-fetch)"
TIMEOUT_SEC = 10
MAX_RETRIES = 2


def _http_get(url: str) -> dict[str, Any]:
    last: Exception | None = None
    for attempt in range(MAX_RETRIES + 1):
        try:
            req = urlreq.Request(url, headers={"User-Agent": USER_AGENT})
            with urlreq.urlopen(req, timeout=TIMEOUT_SEC) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except (HTTPError, URLError, TimeoutError, json.JSONDecodeError) as e:
            last = e
            time.sleep(1.0 * (attempt + 1))
    raise RuntimeError(f"open-meteo 호출 실패: {last}") from last


def _label(code: int) -> str:
    return WMO.get(int(code), f"코드 {code}")


def _fmt_date(iso_date: str) -> str:
    """'2026-05-02' → '5월 2일 토'."""
    d = datetime.strptime(iso_date, "%Y-%m-%d").date()
    return f"{d.month}월 {d.day}일 {WEEKDAY[d.weekday()]}"


def _fmt_time(iso_dt: str) -> str:
    """'2026-05-02T05:34' → '05:34'."""
    return iso_dt.split("T", 1)[1][:5] if "T" in iso_dt else iso_dt


def _hour_index(iso_now_hour: str, hourly_times: list[str]) -> int:
    """현재 시각에 해당하는 hourly 인덱스. 못 찾으면 0."""
    for i, t in enumerate(hourly_times):
        if t.startswith(iso_now_hour):
            return i
    return 0


def _slot_avg_pop(times: list[str], pops: list[int], date_iso: str,
                  start_h: int, end_h: int) -> int | None:
    """해당 날짜의 [start_h, end_h) 시간대 평균 강수확률 (0~100, 정수)."""
    vals: list[int] = []
    for t, p in zip(times, pops):
        if not t.startswith(date_iso):
            continue
        h = int(t[11:13])
        if start_h <= h < end_h and p is not None:
            vals.append(int(p))
    if not vals:
        return None
    return round(sum(vals) / len(vals))


def build_body(data: dict[str, Any]) -> str:
    cur = data["current"]
    hourly = data["hourly"]
    daily = data["daily"]

    today_iso = daily["time"][0]
    tomorrow_iso = daily["time"][1] if len(daily["time"]) > 1 else None

    cur_temp = cur["temperature_2m"]
    feels = cur["apparent_temperature"]
    humidity = cur["relative_humidity_2m"]
    wind = cur["wind_speed_10m"]
    cur_code = cur["weather_code"]
    cur_precip = cur["precipitation"]

    cur_iso_hour = cur["time"][:13]  # 'YYYY-MM-DDTHH'
    h_idx = _hour_index(cur_iso_hour, hourly["time"])
    cur_pop = hourly["precipitation_probability"][h_idx]

    pop_morning = _slot_avg_pop(
        hourly["time"], hourly["precipitation_probability"],
        today_iso, 6, 12,
    )
    pop_afternoon = _slot_avg_pop(
        hourly["time"], hourly["precipitation_probability"],
        today_iso, 12, 18,
    )
    pop_evening = _slot_avg_pop(
        hourly["time"], hourly["precipitation_probability"],
        today_iso, 18, 24,
    )

    today_max = daily["temperature_2m_max"][0]
    today_min = daily["temperature_2m_min"][0]
    today_code = daily["weather_code"][0]
    today_precip = daily["precipitation_sum"][0]
    today_sunrise = _fmt_time(daily["sunrise"][0])
    today_sunset = _fmt_time(daily["sunset"][0])

    lines: list[str] = []
    lines.append(f"📍 {LOCATION_LABEL} · {_fmt_date(today_iso)}")
    lines.append("")
    lines.append(
        f"🌡️ 현재: {cur_temp:.0f}°C "
        f"(체감 {feels:.0f}°C · {_label(cur_code)})"
    )
    lines.append(
        f"💧 습도 {humidity}% · 🌬️ 바람 {wind:.1f} m/s"
        + (f" · ☔ 강수 {cur_precip:.1f}mm" if cur_precip and cur_precip > 0 else "")
    )

    pop_now_str = f"{cur_pop}%" if cur_pop is not None else "—"
    lines.append(f"☔ 지금 강수확률: {pop_now_str}")

    parts = []
    if pop_morning is not None:
        parts.append(f"오전 {pop_morning}%")
    if pop_afternoon is not None:
        parts.append(f"오후 {pop_afternoon}%")
    if pop_evening is not None:
        parts.append(f"저녁 {pop_evening}%")
    if parts:
        lines.append("   시간대: " + " · ".join(parts))

    lines.append("")
    lines.append(f"📈 오늘 ({_label(today_code)})")
    lines.append(f"  • 최고/최저: {today_max:.0f}°C / {today_min:.0f}°C")
    lines.append(f"  • 일출/일몰: {today_sunrise} / {today_sunset}")
    lines.append(f"  • 누적 강수: {today_precip:.1f}mm")

    if tomorrow_iso:
        tmr_max = daily["temperature_2m_max"][1]
        tmr_min = daily["temperature_2m_min"][1]
        tmr_code = daily["weather_code"][1]
        tmr_pop_max = daily["precipitation_probability_max"][1]
        lines.append("")
        lines.append(f"📅 내일 ({_fmt_date(tomorrow_iso)} · {_label(tmr_code)})")
        lines.append(
            f"  • 최고/최저: {tmr_max:.0f}°C / {tmr_min:.0f}°C"
            + (f" · 최대 강수확률 {tmr_pop_max}%" if tmr_pop_max is not None else "")
        )

    return "\n".join(lines)


def main() -> int:
    p = argparse.ArgumentParser(description="청량리 날씨 본문 생성기")
    p.add_argument("--out", help="결과를 저장할 파일 경로 (없으면 stdout)")
    p.add_argument("--json", action="store_true", help="raw JSON 응답 출력 (디버그)")
    args = p.parse_args()

    try:
        data = _http_get(API)
    except RuntimeError as e:
        print(str(e), file=sys.stderr)
        return 2

    if args.json:
        print(json.dumps(data, ensure_ascii=False, indent=2))
        return 0

    body = build_body(data)

    if args.out:
        with open(args.out, "w", encoding="utf-8") as f:
            f.write(body)
        print(f"[fetch_weather] wrote {len(body)} chars → {args.out}")
    else:
        print(body)
    return 0


if __name__ == "__main__":
    sys.exit(main())
