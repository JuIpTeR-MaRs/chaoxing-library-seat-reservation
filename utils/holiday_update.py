"""Fetch the Chinese holiday arrangement into ``holidays.json``.

Two public sources are used:

``timor.tech`` (preferred)
    One request per year returns every arranged day with ``wage``: 3 = legal
    holiday, 2 = rest day inside an arrangement, 1 = make-up working day.  That
    split is exactly what ``holidays.json`` stores.  It lags behind the State
    Council announcement.
``NateScarlet/holiday-cn`` (fallback)
    Community-maintained JSON generated from the State Council papers, usually
    available within days of the announcement.  It only marks days as off/work,
    so legal days can be derived for the fixed-date holidays (元旦/劳动节/国庆节);
    the lunar ones (春节/清明节/端午节/中秋节) are recorded as ``pending`` ranges
    until timor.tech catches up.

Nothing here may break a reservation run: a failed fetch, an unexpected payload
or a write error is reported and the old file is left untouched.
"""

import datetime as dt
import json
import os
from pathlib import Path

import requests

from . import holidays

TIMOR_URL = "https://timor.tech/api/holiday/year/{year}"
HOLIDAY_CN_URLS = (
    "https://cdn.jsdelivr.net/gh/NateScarlet/holiday-cn@master/{year}.json",
    "https://fastly.jsdelivr.net/gh/NateScarlet/holiday-cn@master/{year}.json",
    "https://raw.githubusercontent.com/NateScarlet/holiday-cn/master/{year}.json",
)
# timor.tech answers 403 to the default python-requests user agent.
USER_AGENT = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36")
# Legal days of the holidays whose dates are fixed on the solar calendar.
FIXED_LEGAL_DAYS = {"元旦": ("01-01",),
                    "劳动节": ("05-01", "05-02"),
                    "国庆节": ("10-01", "10-02", "10-03")}
# Lunar / solar-term holidays: holiday-cn cannot tell which day is the legal one.
LUNAR_HOLIDAY_NAMES = ("春节", "清明节", "端午节", "中秋节")
MIN_ROWS = 15
AUTO_NOTE = ("holidays：法定节假日（任务默认跳过）。adjustments：调休日（只用于界面提醒，"
             "不会自动跳过）。pending：已放假但法定日期尚未确认的区间，日志和界面会提示。"
             "用界面“更新节假日表”或 --update-holidays 从官方安排自动更新；"
             "也可以手工按通知修改本文件。")
LEGAL, REST, WORK = "legal", "rest", "work"


class HolidayDataError(Exception):
    """A source answered, but its payload cannot be trusted."""


def _get_json(url, timeout):
    response = requests.get(url, headers={"User-Agent": USER_AGENT}, timeout=timeout)
    response.raise_for_status()
    payload = response.json()
    if not isinstance(payload, dict):
        raise HolidayDataError(f"{url} 返回的不是 JSON 对象")
    return payload


def _parse_date(value, year):
    try:
        day = dt.date.fromisoformat(str(value).strip())
    except (TypeError, ValueError):
        return None
    return day if day.year == year else None


def _rows_from_timor(payload, year):
    if payload.get("code") != 0:
        raise HolidayDataError(f"timor.tech: {payload.get('msg') or '接口返回错误'}")
    days = payload.get("holiday")
    if not isinstance(days, dict) or not days:
        raise HolidayDataError(f"timor.tech 尚未收录 {year} 年的安排")
    rows = []
    for item in days.values():
        if not isinstance(item, dict):
            continue
        day = _parse_date(item.get("date"), year)
        if day is None:
            continue
        name = str(item.get("name") or "").strip()
        if item.get("holiday") is True and item.get("wage") == 3:
            rows.append((day, LEGAL, name or "法定节假日"))
        elif item.get("holiday") is True:
            rows.append((day, REST, name or "调休休息"))
        elif item.get("holiday") is False:
            rows.append((day, WORK, name or "调休上班"))
    return rows, [], "timor.tech"


def _rows_from_holiday_cn(payload, year):
    if payload.get("year") != year:
        raise HolidayDataError(f"holiday-cn 返回了 {payload.get('year')} 年的数据")
    days = payload.get("days")
    if not isinstance(days, list) or not days:
        raise HolidayDataError(f"holiday-cn 未收录 {year} 年的数据")
    rows, pending_days = [], []
    for item in days:
        if not isinstance(item, dict):
            continue
        day = _parse_date(item.get("date"), year)
        if day is None:
            continue
        name = str(item.get("name") or "").strip()
        if item.get("isOffDay") is True:
            if f"{day:%m-%d}" in FIXED_LEGAL_DAYS.get(name, ()):
                rows.append((day, LEGAL, name))
            else:
                rows.append((day, REST, name or "调休休息"))
                if name in LUNAR_HOLIDAY_NAMES:
                    pending_days.append((day, name))
        else:
            rows.append((day, WORK, f"{name}补班" if name else "补班"))
    pending = _group_ranges(pending_days)
    papers = payload.get("papers") or []
    source = f"holiday-cn {papers[0]}" if papers else "holiday-cn"
    return rows, pending, source


def _group_ranges(days):
    """Group ``(date, name)`` items into ``{"name", "start", "end"}`` ranges."""
    ranges = []
    for day, name in sorted(days):
        last = ranges[-1] if ranges else None
        if last and last["name"] == name and \
                dt.date.fromisoformat(last["end"]) + dt.timedelta(days=1) == day:
            last["end"] = day.isoformat()
        else:
            ranges.append({"name": name, "start": day.isoformat(), "end": day.isoformat()})
    return ranges


def validate(rows, year):
    """Reject partial or inconsistent payloads before they reach the file."""
    if not rows:
        raise HolidayDataError("没有解析到任何法定节假日")
    dates = [row[0] for row in rows]
    if any(day.year != year for day in dates):
        raise HolidayDataError(f"数据里混入了 {year} 年以外的日期")
    if len(set(dates)) != len(dates):
        raise HolidayDataError("同一天出现了多条记录")
    if len(rows) < MIN_ROWS:
        raise HolidayDataError(f"只解析到 {len(rows)} 天，少于下限 {MIN_ROWS}，疑似数据不完整")
    if not any(kind == LEGAL for _day, kind, _name in rows):
        raise HolidayDataError("没有识别出法定节假日")
    return rows


def fetch_year(year, timeout=8):
    """Return ``rows``, ``pending`` and the source for one year.

    Raises :class:`HolidayDataError` (or a requests error) when no source can be
    trusted; the caller keeps the previous file in that case.
    """
    problems = []
    try:
        rows, pending, source = _rows_from_timor(_get_json(TIMOR_URL.format(year=year), timeout), year)
        return validate(rows, year), pending, source
    except (HolidayDataError, requests.RequestException, ValueError, OSError) as error:
        problems.append(f"timor.tech: {error}")
    for template in HOLIDAY_CN_URLS:
        try:
            rows, pending, source = _rows_from_holiday_cn(
                _get_json(template.format(year=year), timeout), year)
            return validate(rows, year), pending, source
        except (HolidayDataError, requests.RequestException, ValueError, OSError) as error:
            problems.append(f"holiday-cn: {error}")
    raise HolidayDataError("；".join(problems) or f"没有可用的 {year} 年数据源")


def fetch_all(years, timeout=8, fetcher=fetch_year):
    """Fetch several years; failures are returned per year instead of raised."""
    results = []
    for year in years:
        try:
            rows, pending, source = fetcher(year, timeout=timeout)
            results.append({"year": year, "rows": rows, "pending": pending,
                            "source": source})
        except (HolidayDataError, requests.RequestException, ValueError, OSError) as error:
            results.append({"year": year, "error": f"{type(error).__name__}: {error}"})
    return results


def _rows_from_entries(entries, kind):
    rows = []
    for entry in entries or []:
        try:
            kind_name = entry.get("kind") or kind
            if kind == "adjustments" and kind_name not in ("workday", "restday"):
                continue
            dates = holidays.entry_dates(entry, kind)
        except (ValueError, TypeError, AttributeError):
            continue
        name = str(entry.get("name") or "").strip()
        for day in dates:
            if kind == "holidays":
                rows.append((day, LEGAL, name or "法定节假日"))
            else:
                rows.append((day, REST if kind_name == "restday" else WORK, name or "调休"))
    return rows


def rows_from_file(data):
    """Expand the on-disk entries into ``(date, kind, name)`` rows."""
    rows = _rows_from_entries(data.get("holidays"), "holidays")
    rows += _rows_from_entries(data.get("adjustments"), "adjustments")
    return rows


def _entries(rows, kind):
    allowed = (LEGAL,) if kind == "holidays" else (REST, WORK)
    grouped = []
    for day, row_kind, name in sorted(rows):
        if row_kind not in allowed:
            continue
        entry = {"name": name, "start": day.isoformat(), "end": day.isoformat()}
        if kind == "adjustments":
            entry["kind"] = "restday" if row_kind == REST else "workday"
        previous = grouped[-1] if grouped else None
        if (previous and previous["name"] == entry["name"]
                and previous.get("kind") == entry.get("kind")
                and dt.date.fromisoformat(previous["end"]) + dt.timedelta(days=1) == day):
            previous["end"] = entry["end"]
        else:
            grouped.append(entry)
    return grouped


def load_file(path):
    """Read the current file; a missing or broken file is treated as empty."""
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8-sig"))
    except (FileNotFoundError, OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def covered(data, year):
    """True when *year* has legal days and no pending range left."""
    rows = [row for row in rows_from_file(data) if row[0].year == year and row[1] == LEGAL]
    pending = [entry for entry in data.get("pending") or []
               if str(entry.get("start") or "").startswith(str(year))]
    return bool(rows) and not pending


def _content(data):
    """The parts of the file that matter for change detection (not the date)."""
    return (data.get("holidays"), data.get("adjustments"), data.get("pending"),
            data.get("source"), data.get("note"))


def merge(existing, fetched, today=None):
    """Replace the fetched years, keep every other year and the file's prose."""
    years = {item["year"] for item in fetched}
    rows = [row for row in rows_from_file(existing) if row[0].year not in years]
    pending = []
    for entry in existing.get("pending") or []:
        try:
            start = holidays.as_date(entry.get("start"))
        except (TypeError, ValueError):
            continue
        if start.year not in years:
            pending.append({"name": str(entry.get("name") or "放假区间"),
                            "start": entry["start"], "end": entry.get("end") or entry["start"]})
    for item in fetched:
        rows.extend(item["rows"])
        pending.extend(item["pending"])
    # Keep the provenance of every year already in the file: a nightly retry that
    # only refreshes one year must not look like a change to the whole file.
    parts = [part for part in str(existing.get("source") or "").split("；") if part.strip()]
    for item in fetched:
        if item["source"] not in parts:
            parts.append(item["source"])
    data = {
        "updated": str(today or dt.date.today()),
        "source": "；".join(parts),
        "note": AUTO_NOTE,
        "holidays": _entries(rows, "holidays"),
        "adjustments": _entries(rows, "adjustments"),
        "pending": sorted(pending, key=lambda entry: entry["start"]),
    }
    return data


def write_calendar(path, data):
    """Write atomically, so a crash never leaves a half-written calendar."""
    target = Path(path)
    temp = target.with_name(target.name + ".tmp")
    temp.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(temp, target)


def apply(path, results, today=None):
    """Merge fetched years into *path*; returns what happened, never raises."""
    fetched = [item for item in results if "rows" in item]
    errors = [f"{item['year']}: {item['error']}" for item in results if item.get("error")]
    if not fetched:
        return {"written": False, "errors": errors, "years": [], "pending": 0}
    existing = load_file(path)
    data = merge(existing, fetched, today=today)
    written = _content(data) != _content(existing)
    if written:
        write_calendar(path, data)
    return {"written": written, "errors": errors,
            "years": [item["year"] for item in fetched],
            "holidays": len(data["holidays"]), "adjustments": len(data["adjustments"]),
            "pending": len(data["pending"]), "sources": [item["source"] for item in fetched]}


def update(path, years, timeout=8, fetcher=fetch_year, today=None):
    """Fetch and apply in one call (CLI and automatic update)."""
    return apply(path, fetch_all(years, timeout=timeout, fetcher=fetcher), today=today)


def ensure_year(path, year, timeout=4, fetcher=fetch_year, today=None):
    """Try to fill *year* only when the file has no complete data for it."""
    if covered(load_file(path), year):
        return {"written": False, "errors": [], "years": [], "skipped": f"{year} 年已覆盖"}
    return update(path, [year], timeout=timeout, fetcher=fetcher, today=today)


def describe(results):
    """Human-readable Chinese lines for the GUI and CLI."""
    lines = []
    for item in results:
        if item.get("error"):
            lines.append(f"{item['year']} 年：获取失败（{item['error']}）")
            continue
        legal = sum(1 for _day, kind, _name in item["rows"] if kind == LEGAL)
        work = sum(1 for _day, kind, _name in item["rows"] if kind == WORK)
        lines.append(f"{item['year']} 年：法定节假日 {legal} 天、调休上班 {work} 天"
                     f"（来源 {item['source']}）")
        for entry in item["pending"]:
            lines.append(f"  · {entry['name']} {entry['start']}~{entry['end']} 的法定日期"
                         "暂未确认，稍后会自动重试")
    return lines
