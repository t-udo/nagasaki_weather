#!/usr/bin/env python3
"""3サイトの天気予報を取得し、forecast.json と index.html を再生成する。"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import time
from copy import deepcopy
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import requests
from bs4 import BeautifulSoup, Tag
from jinja2 import Environment, FileSystemLoader, select_autoescape

ROOT = Path(__file__).resolve().parent
DATA_DIR = ROOT / "data"
JSON_PATH = DATA_DIR / "forecast.json"
HTML_PATH = ROOT / "index.html"
TEMPLATE_NAME = "template.html"
LOCK_PATH = DATA_DIR / "update.lock"

JST = timezone(timedelta(hours=9))
DEFAULT_DATES = ("2026-09-20", "2026-09-21", "2026-09-22")
DOW_FULL = ["月曜日", "火曜日", "水曜日", "木曜日", "金曜日", "土曜日", "日曜日"]
DOW_SHORT = ["月", "火", "水", "木", "金", "土", "日"]

URLS = {
    "weathernews": "https://weathernews.jp/onebox/tenki/nagasaki/42201/week.html?tab=4",
    "yahoo": "https://weather.yahoo.co.jp/weather/jp/42/8410/42201.html",
    "tenki": "https://tenki.jp/leisure/9/45/268/2256/10days.html",
}

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/128.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "ja,en-US;q=0.8,en;q=0.7",
}

WNI_WX = {
    100: "晴れ",
    101: "晴れ時々くもり",
    102: "晴れ一時雨",
    103: "晴れ時々雪",
    104: "晴れ一時雪",
    110: "晴れのちくもり",
    111: "晴れのち雨",
    112: "晴れのち雪",
    200: "くもり",
    201: "くもり時々晴れ",
    202: "くもり一時雨",
    203: "くもり時々雨",
    204: "くもり一時雪",
    210: "くもりのち晴れ",
    211: "くもりのち雨",
    212: "くもりのち雪",
    300: "雨",
    301: "雨時々晴れ",
    302: "雨時々くもり",
    303: "雨一時雪",
    311: "雨のち晴れ",
    313: "雨のちくもり",
    400: "雪",
    401: "雪時々晴れ",
    402: "雪時々くもり",
    411: "雪のち晴れ",
    413: "雪のちくもり",
    500: "快晴",
    600: "雷雨",
}


class FetchError(Exception):
    pass


def now_jst() -> datetime:
    return datetime.now(JST)


def parse_dates(values: list[str]) -> list[date]:
    out: list[date] = []
    for raw in values:
        out.append(datetime.strptime(raw.strip(), "%Y-%m-%d").date())
    if not out:
        raise ValueError("対象日が空です")
    return out


def day_meta(d: date) -> dict[str, Any]:
    return {
        "iso": d.isoformat(),
        "label": f"{d.month}月{d.day}日",
        "dow": DOW_FULL[d.weekday()],
        "short": f"{d.day}日({DOW_SHORT[d.weekday()]})",
    }


def title_range(days: list[date]) -> str:
    first, last = days[0], days[-1]
    if first.month == last.month:
        return f"{first.month}月{first.day}日〜{last.day}日"
    return f"{first.month}月{first.day}日〜{last.month}月{last.day}日"


def classify_icon(wx: str | None) -> str:
    if not wx:
        return "unknown"
    if any(token in wx for token in ("雨", "雪", "雷")):
        return "rain"
    if any(token in wx for token in ("時々曇", "のち曇", "一時曇", "時々くもり", "のちくもり")):
        return "sun_cloud"
    if any(token in wx for token in ("時々晴", "のち晴", "一時晴")):
        return "cloud_sun"
    if "くもり" in wx or "曇" in wx:
        return "cloudy"
    if "晴" in wx:
        return "sunny"
    return "unknown"


def wni_wx_name(code: int) -> str:
    if code in WNI_WX:
        return WNI_WX[code]
    base = {1: "晴れ", 2: "くもり", 3: "雨", 4: "雪"}.get(code // 100, "不明")
    mid, ones = (code // 10) % 10, code % 10
    second = {1: "晴れ", 2: "くもり", 3: "雨", 4: "雪"}.get(ones, "")
    if not second or second == base:
        return base
    if mid == 1:
        return f"{base}のち{second}"
    return f"{base}時々{second}"


def to_int(text: str | None) -> int | None:
    if not text:
        return None
    m = re.search(r"-?\d+", text.replace(",", ""))
    return int(m.group()) if m else None


def text_of(el: Tag | None) -> str:
    return re.sub(r"\s+", " ", el.get_text(" ", strip=True)) if el else ""


def fetch_html(url: str, timeout: int = 30) -> str:
    resp = requests.get(url, headers=HEADERS, timeout=timeout)
    if resp.status_code != 200:
        raise FetchError(f"HTTP {resp.status_code} for {url}")
    resp.encoding = resp.apparent_encoding or "utf-8"
    return resp.text


SOURCE_NAMES = {
    "weathernews": "ウェザーニュース",
    "yahoo": "Yahoo!天気",
    "tenki": "tenki.jp",
}


def empty_source(key: str, place: str, error: str | None = None, ok: bool = False) -> dict[str, Any]:
    return {
        "key": key,
        "name": SOURCE_NAMES[key],
        "place": place,
        "url": URLS[key],
        "ok": ok,
        "stale": False,
        "error": error,
        "fetched_at": None,
        "published": None,
        "comment": None,
        "days": {},
    }


def parse_weathernews(html: str, targets: list[date]) -> dict[str, Any]:
    soup = BeautifulSoup(html, "lxml")
    week = soup.select_one("#flick_list_week")
    if not week:
        raise FetchError("ウェザーニュースの2週間表が見つかりません")

    items = week.select("ul.wxweek_content")
    if not items:
        raise FetchError("ウェザーニュースの日別行が見つかりません")

    today = now_jst().date()
    today_idx = None
    for i, ul in enumerate(items):
        classes = ul.get("class") or []
        day_n = to_int(text_of(ul.select_one(".day")))
        if day_n == today.day and "past" not in classes:
            today_idx = i
            break
    if today_idx is None:
        today_idx = next((i for i, ul in enumerate(items) if ul.get("id") == "wx__week0"), 0)

    parsed: dict[str, dict[str, Any]] = {}
    for i, ul in enumerate(items):
        d = today + timedelta(days=i - today_idx)
        img = ul.select_one("li.weather img.wx__icon")
        src = img.get("src", "") if img else ""
        m = re.search(r"wxicon/(\d+)", src)
        code = int(m.group(1)) if m else 0
        wx = wni_wx_name(code) if code else None
        rain_txt = text_of(ul.select_one("li.rain"))
        pop = to_int(rain_txt) if "%" in rain_txt else None
        entry = {
            "wx": wx,
            "icon": classify_icon(wx),
            "high": to_int(text_of(ul.select_one("li.high"))),
            "low": to_int(text_of(ul.select_one("li.low"))),
            "pop": pop,
        }
        if d in targets and wx and entry["high"] is not None:
            parsed[d.isoformat()] = entry

    if not parsed:
        raise FetchError("対象日のウェザーニュース予報を抽出できませんでした")

    heading = text_of(soup.select_one("#overview .opinion .heading"))
    body = text_of(soup.select_one("#overview .opinion .body"))
    comment = "。".join(p for p in (heading, body) if p) or None

    src = empty_source("weathernews", "長崎市", ok=True)
    src["fetched_at"] = now_jst().isoformat(timespec="seconds")
    src["comment"] = comment
    src["days"] = parsed
    return src


def parse_yahoo(html: str, targets: list[date]) -> dict[str, Any]:
    soup = BeautifulSoup(html, "lxml")
    week = soup.select_one("#yjw_week table.yjw_table")
    if not week:
        raise FetchError("Yahoo!天気の週間表が見つかりません")

    rows = week.select("tr")
    if len(rows) < 4:
        raise FetchError("Yahoo!天気の週間表の行が不足しています")

    year = now_jst().year
    pub = text_of(soup.select_one("#yjw_week .yjw_note_h2"))

    date_cells = rows[0].select("td")[1:]
    wx_cells = rows[1].select("td")[1:]
    temp_cells = rows[2].select("td")[1:]
    pop_cells = rows[3].select("td")[1:]

    parsed: dict[str, dict[str, Any]] = {}
    for i, cell in enumerate(date_cells):
        m = re.search(r"(\d{1,2})月(\d{1,2})日", text_of(cell))
        if not m:
            continue
        d = date(year, int(m.group(1)), int(m.group(2)))
        if d not in targets:
            continue
        wx = text_of(wx_cells[i]) if i < len(wx_cells) else ""
        wx = wx.replace("天気", "").strip()
        img = wx_cells[i].find("img") if i < len(wx_cells) else None
        if img and img.get("alt"):
            wx = str(img["alt"]).strip()
        temps = [int(n) for n in re.findall(r"-?\d+", text_of(temp_cells[i] if i < len(temp_cells) else None))]
        parsed[d.isoformat()] = {
            "wx": wx or None,
            "icon": classify_icon(wx),
            "high": temps[0] if temps else None,
            "low": temps[1] if len(temps) > 1 else None,
            "pop": to_int(text_of(pop_cells[i] if i < len(pop_cells) else None)),
        }

    if not parsed:
        raise FetchError("対象日のYahoo!天気予報を抽出できませんでした")

    src = empty_source("yahoo", "長崎市", ok=True)
    src["fetched_at"] = now_jst().isoformat(timespec="seconds")
    src["published"] = pub or None
    src["days"] = parsed
    return src


def parse_tenki(html: str, targets: list[date]) -> dict[str, Any]:
    soup = BeautifulSoup(html, "lxml")
    tables = soup.select("table.forecast-point-10days")
    if not tables:
        raise FetchError("tenki.jp の10日間表が見つかりません")

    year = now_jst().year
    parsed: dict[str, dict[str, Any]] = {}
    current: dict[str, Any] | None = None
    current_iso: str | None = None

    for table in tables:
        for tr in table.select("tr"):
            ths = tr.find_all("th", recursive=False)
            tds = tr.find_all("td", recursive=False)
            if ths and re.search(r"\d{1,2}月\d{1,2}日", text_of(ths[0])):
                m = re.search(r"(\d{1,2})月(\d{1,2})日", text_of(ths[0]))
                if not m:
                    continue
                d = date(year, int(m.group(1)), int(m.group(2)))
                current_iso = d.isoformat()
                if d not in targets:
                    current = None
                    continue
                wx = text_of(ths[1].select_one(".forecast-telop")) if len(ths) > 1 else None
                high = to_int(text_of(ths[2].select_one(".high-temp")) if len(ths) > 2 else None)
                low = to_int(text_of(ths[2].select_one(".low-temp")) if len(ths) > 2 else None)
                pop = to_int(text_of(ths[3]) if len(ths) > 3 else None)
                indexes = [text_of(span) for span in tr.select(".index-telop")]
                uv = indexes[0] if indexes else None
                heat = indexes[1] if len(indexes) > 1 else None
                current = {
                    "wx": wx or None,
                    "icon": classify_icon(wx),
                    "high": high,
                    "low": low,
                    "pop": pop,
                    "uv": uv,
                    "heat": heat,
                    "hours": [],
                }
                parsed[current_iso] = current
                continue

            if current is None or current_iso is None or len(tds) < 6:
                continue
            if "time" not in (tds[0].get("class") or []):
                continue
            wind_dir = tds[6].find("img")["alt"] if len(tds) > 6 and tds[6].find("img") else ""
            wind_spd = to_int(text_of(tds[6].select_one(".wind-speed") if len(tds) > 6 else None))
            wind = " ".join(x for x in (wind_dir, f"{wind_spd}m/s" if wind_spd is not None else "") if x)
            precip = text_of(tds[4]).replace(" ", "") or "0㎜"
            current["hours"].append(
                {
                    "span": text_of(tds[0]),
                    "wx": text_of(tds[1].select_one(".forecast-telop")) or text_of(tds[1]),
                    "temp": to_int(text_of(tds[2])),
                    "pop": to_int(text_of(tds[3])),
                    "precip": precip,
                    "humidity": to_int(text_of(tds[5])),
                    "wind": wind,
                }
            )

    if not parsed:
        raise FetchError("対象日の tenki.jp 予報を抽出できませんでした")

    published = None
    m = re.search(r"announce_datetime:(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})", html)
    if m:
        dt = datetime.strptime(m.group(1), "%Y-%m-%d %H:%M:%S")
        published = f"{dt.year}年{dt.month}月{dt.day}日 {dt.hour:02d}時{dt.minute:02d}分発表"
    else:
        head = text_of(soup.select_one("h2, .forecast-point-announce, .announce"))
        if head:
            published = head

    src = empty_source("tenki", "ハウステンボス", ok=True)
    src["fetched_at"] = now_jst().isoformat(timespec="seconds")
    src["published"] = published
    src["days"] = parsed
    return src


PARSERS = {
    "weathernews": ("長崎市", parse_weathernews),
    "yahoo": ("長崎市", parse_yahoo),
    "tenki": ("ハウステンボス", parse_tenki),
}


def fetch_source(key: str, targets: list[date]) -> dict[str, Any]:
    place, parser = PARSERS[key]
    try:
        html = fetch_html(URLS[key])
        return parser(html, targets)
    except Exception as exc:  # noqa: BLE001 - サイト差分をログして継続したい
        return empty_source(key, place, error=str(exc), ok=False)


def load_previous() -> dict[str, Any] | None:
    if not JSON_PATH.exists():
        return None
    try:
        return json.loads(JSON_PATH.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return None


def merge_sources(fresh: dict[str, dict[str, Any]], previous: dict[str, Any] | None) -> dict[str, dict[str, Any]]:
    merged: dict[str, dict[str, Any]] = {}
    prev_sources = (previous or {}).get("sources") or {}
    for key in PARSERS:
        current = fresh[key]
        old = deepcopy(prev_sources.get(key) or {})
        if current.get("ok"):
            if old.get("days"):
                for iso, day in old["days"].items():
                    current["days"].setdefault(iso, day)
            merged[key] = current
            continue
        if old.get("ok") and old.get("days"):
            old["stale"] = True
            old["error"] = current.get("error")
            merged[key] = old
        else:
            merged[key] = current
    return merged


def payload_for(days: list[date], sources: dict[str, dict[str, Any]]) -> dict[str, Any]:
    fetched_at = now_jst()
    return {
        "fetched_at": fetched_at.isoformat(timespec="seconds"),
        "fetched_at_label": f"{fetched_at.year}年{fetched_at.month}月{fetched_at.day}日 {fetched_at.strftime('%H:%M')}",
        "title_range": title_range(days),
        "days": [day_meta(d) for d in days],
        "sources": sources,
        "urls": URLS,
    }


def has_any_day(sources: dict[str, dict[str, Any]]) -> bool:
    return any(src.get("days") for src in sources.values())


def render_html(data: dict[str, Any]) -> str:
    env = Environment(
        loader=FileSystemLoader(str(ROOT)),
        autoescape=select_autoescape(["html", "xml"]),
        trim_blocks=True,
        lstrip_blocks=True,
    )
    tmpl = env.get_template(TEMPLATE_NAME)
    return tmpl.render(**data)


def acquire_lock() -> int:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    flags = os.O_CREAT | os.O_EXCL | os.O_WRONLY
    try:
        fd = os.open(LOCK_PATH, flags, 0o644)
    except FileExistsError:
        age = time.time() - LOCK_PATH.stat().st_mtime
        if age < 600:
            raise SystemExit("別の update.py が実行中です") from None
        LOCK_PATH.unlink(missing_ok=True)
        fd = os.open(LOCK_PATH, flags, 0o644)
    os.write(fd, str(os.getpid()).encode())
    return fd


def release_lock(fd: int) -> None:
    os.close(fd)
    LOCK_PATH.unlink(missing_ok=True)


def install_cron() -> None:
    script = ROOT / "update.sh"
    script.chmod(0o755)
    line = f"10 6,12,18,21 * * * {script}"
    try:
        current = subprocess.check_output(["crontab", "-l"], text=True, stderr=subprocess.DEVNULL)
    except subprocess.CalledProcessError:
        current = ""
    if str(script) in current:
        print("crontab に既に登録されています")
        print(line)
        return
    new = current.rstrip() + ("\n" if current.strip() else "") + f"# weather forecast updater\n{line}\n"
    subprocess.run(["crontab", "-"], input=new, text=True, check=True)
    print("crontab に登録しました:")
    print(line)
    print("WSL では `sudo service cron start` が必要な場合があります。")


def run(dates: list[date], render_only: bool) -> int:
    previous = load_previous()
    if render_only:
        if not previous:
            print("forecast.json がありません", file=sys.stderr)
            return 1
        HTML_PATH.write_text(render_html(previous), encoding="utf-8")
        print(f"rendered {HTML_PATH}")
        return 0

    fresh: dict[str, dict[str, Any]] = {}
    for i, key in enumerate(PARSERS):
        print(f"fetch {key} ...", flush=True)
        fresh[key] = fetch_source(key, dates)
        status = "ok" if fresh[key].get("ok") else f"FAIL {fresh[key].get('error')}"
        print(f"  {status}", flush=True)
        if i < len(PARSERS) - 1:
            time.sleep(1.5)

    sources = merge_sources(fresh, previous)
    if not has_any_day(sources):
        print("有効な予報を取得できなかったため HTML は更新しません", file=sys.stderr)
        return 2

    data = payload_for(dates, sources)
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    JSON_PATH.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    HTML_PATH.write_text(render_html(data), encoding="utf-8")
    print(f"wrote {JSON_PATH}")
    print(f"wrote {HTML_PATH}")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="天気予報を再取得して index.html を再生成する")
    parser.add_argument(
        "--dates",
        default=",".join(DEFAULT_DATES),
        help="対象日 (YYYY-MM-DD をカンマ区切り)。省略時は 2026-09-20,21,22",
    )
    parser.add_argument("--render-only", action="store_true", help="既存 JSON から HTML だけ再生成")
    parser.add_argument("--install-cron", action="store_true", help="6/12/18/21時台に実行する crontab を登録")
    args = parser.parse_args()

    if args.install_cron:
        install_cron()
        return 0

    dates = parse_dates(args.dates.split(","))
    fd = acquire_lock()
    try:
        return run(dates, args.render_only)
    finally:
        release_lock(fd)


if __name__ == "__main__":
    sys.exit(main())
