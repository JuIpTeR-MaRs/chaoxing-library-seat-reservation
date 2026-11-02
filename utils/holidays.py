"""Chinese statutory holidays and adjusted days for the scheduled runner.

Dates come from ``holidays.json`` beside the program (or beside the packaged
``SeatDesk.exe``), so the list can be updated without touching code. Two tables
are read from that file:

``holidays``
    Legal holidays; the scheduled run skips these by default.
``adjustments``
    Days moved by the holiday arrangement: ``workday`` (a weekend that becomes
    a working day) or ``restday`` (a working day given as time off). They never
    skip automatically; they are listed for the user to decide per date.

A missing, unreadable or malformed file never blocks a reservation: the
calendar degrades to "nothing known" and the caller logs a warning.
"""

import datetime as dt
import json
import sys
from pathlib import Path

FILENAME = "holidays.json"
KINDS = ("workday", "restday")
KIND_LABELS = {"workday": "调休上班", "restday": "调休放假"}
_CACHE = {}


def candidate_paths():
    """Return the places ``holidays.json`` may live, most editable first."""
    paths = []
    if getattr(sys, "frozen", False):
        paths.append(Path(sys.executable).resolve().parent / FILENAME)
        bundle = getattr(sys, "_MEIPASS", None)
        if bundle:
            paths.append(Path(bundle) / FILENAME)
    else:
        paths.append(Path(__file__).resolve().parent.parent / FILENAME)
        paths.append(Path.cwd() / FILENAME)
    return paths


def default_path():
    for path in candidate_paths():
        if path.is_file():
            return path
    return candidate_paths()[0]


def as_date(value):
    """Accept a date, a datetime or an ISO ``YYYY-MM-DD`` string."""
    if isinstance(value, dt.datetime):
        return value.date()
    if isinstance(value, dt.date):
        return value
    return dt.date.fromisoformat(str(value).strip())


class HolidayCalendar:
    """Holidays and adjusted days loaded from ``holidays.json``."""

    def __init__(self, days=None, adjustments=None, path=None, error=None):
        self.days = dict(days or {})
        self.adjustments = dict(adjustments or {})
        self.path = Path(path) if path else default_path()
        self.error = error
        self.years = {day.year for day in self.days}

    def name(self, value):
        """Return the holiday name for one date, or ``None``."""
        return self.days.get(as_date(value))

    def is_holiday(self, value):
        return self.name(value) is not None

    def adjustment(self, value):
        """Return ``{"kind": ..., "name": ...}`` for an adjusted day, or ``None``."""
        return self.adjustments.get(as_date(value))

    def covers(self, value):
        """Whether the data file has any entry in that date's year."""
        return as_date(value).year in self.years

    def upcoming(self, start, end):
        """Adjusted days in ``[start, end]`` as ``(date, kind, name)`` tuples."""
        first, last = as_date(start), as_date(end)
        return [(day, info["kind"], info.get("name") or KIND_LABELS.get(info["kind"], info["kind"]))
                for day, info in sorted(self.adjustments.items()) if first <= day <= last]

    def __len__(self):
        return len(self.days)


def _parse_dates(entry, label):
    if entry.get("dates"):
        return [as_date(value) for value in entry["dates"]]
    if not entry.get("start"):
        raise ValueError(f"{label} entry {entry!r} needs 'start' or 'dates'")
    start = as_date(entry["start"])
    end = as_date(entry.get("end") or entry["start"])
    if end < start:
        raise ValueError(f"{label} entry {start} ends before it starts")
    return [start + dt.timedelta(days=offset) for offset in range((end - start).days + 1)]


def _parse_holiday(entry):
    return str(entry.get("name") or "法定节假日").strip(), _parse_dates(entry, "holiday")


def _parse_adjustment(entry):
    kind = str(entry.get("kind") or "").strip()
    if kind not in KINDS:
        raise ValueError(f"adjustment entry needs kind {' or '.join(KINDS)}: {entry!r}")
    name = str(entry.get("name") or KIND_LABELS[kind]).strip()
    return kind, name, _parse_dates(entry, "adjustment")


def load_calendar(path=None):
    """Load one calendar from disk, reporting problems instead of raising."""
    target = Path(path) if path else default_path()
    try:
        data = json.loads(target.read_text(encoding="utf-8-sig"))
        days = {}
        for entry in data.get("holidays") or []:
            name, dates = _parse_holiday(entry)
            for day in dates:
                days[day] = name
        adjustments = {}
        for entry in data.get("adjustments") or []:
            kind, name, dates = _parse_adjustment(entry)
            for day in dates:
                adjustments[day] = {"kind": kind, "name": name}
    except FileNotFoundError:
        return HolidayCalendar(path=target,
                               error=f"{target.name} not found; holiday skipping is off")
    except (OSError, ValueError, TypeError, KeyError) as error:
        return HolidayCalendar(path=target, error=f"{target.name}: {error}")
    return HolidayCalendar(days, adjustments, target)


def calendar(path=None):
    """Return a cached calendar, reloading when the file changes on disk."""
    target = Path(path) if path else default_path()
    try:
        stat = target.stat()
        key = (stat.st_mtime_ns, stat.st_size)
    except OSError:
        key = None
    cached = _CACHE.get(str(target))
    if cached and key is not None and cached[0] == key:
        return cached[1]
    loaded = load_calendar(target)
    _CACHE[str(target)] = (key, loaded)
    return loaded


def holiday_name(value, path=None):
    return calendar(path).name(value)


def is_holiday(value, path=None):
    return calendar(path).is_holiday(value)
