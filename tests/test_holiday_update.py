"""Offline checks for the automatic holiday update (sources, merge, safety)."""

import json
import os
from pathlib import Path
import unittest
from unittest import mock

import requests

import reserve_segments  # noqa: F401  (import order: utils.net must load first)
from utils import holiday_update, holidays


TIMOR_2026 = {
    "code": 0,
    "holiday": {
        "01-01": {"holiday": True, "name": "元旦", "wage": 3, "date": "2026-01-01"},
        "01-02": {"holiday": True, "name": "元旦", "wage": 2, "date": "2026-01-02"},
        "01-03": {"holiday": True, "name": "元旦", "wage": 2, "date": "2026-01-03"},
        "01-04": {"holiday": False, "name": "元旦后补班", "wage": 1, "date": "2026-01-04"},
        "02-14": {"holiday": False, "name": "春节前补班", "wage": 1, "date": "2026-02-14"},
        "02-15": {"holiday": True, "name": "春节", "wage": 2, "date": "2026-02-15"},
        "02-16": {"holiday": True, "name": "除夕", "wage": 3, "date": "2026-02-16"},
        "02-17": {"holiday": True, "name": "初一", "wage": 3, "date": "2026-02-17"},
        "02-18": {"holiday": True, "name": "初二", "wage": 3, "date": "2026-02-18"},
        "02-19": {"holiday": True, "name": "初三", "wage": 3, "date": "2026-02-19"},
        "02-28": {"holiday": False, "name": "春节后补班", "wage": 1, "date": "2026-02-28"},
        "04-04": {"holiday": True, "name": "清明节", "wage": 2, "date": "2026-04-04"},
        "04-05": {"holiday": True, "name": "清明节", "wage": 3, "date": "2026-04-05"},
        "04-06": {"holiday": True, "name": "清明节", "wage": 2, "date": "2026-04-06"},
        "05-01": {"holiday": True, "name": "劳动节", "wage": 3, "date": "2026-05-01"},
        "05-02": {"holiday": True, "name": "劳动节", "wage": 3, "date": "2026-05-02"},
        "05-03": {"holiday": True, "name": "劳动节", "wage": 2, "date": "2026-05-03"},
        "05-09": {"holiday": False, "name": "劳动节后补班", "wage": 1, "date": "2026-05-09"},
        "06-19": {"holiday": True, "name": "端午节", "wage": 3, "date": "2026-06-19"},
        "06-20": {"holiday": True, "name": "端午节", "wage": 2, "date": "2026-06-20"},
    },
}

HOLIDAY_CN_2027 = {
    "year": 2027,
    "papers": ["https://www.gov.cn/zhengce/zhengceku/202611/content_7085102.htm"],
    "days": (
        [{"name": "元旦", "date": f"2027-01-0{day}", "isOffDay": True} for day in (1, 2, 3)]
        + [{"name": "春节", "date": "2027-01-31", "isOffDay": False}]
        + [{"name": "春节", "date": f"2027-02-{day:02d}", "isOffDay": True} for day in range(5, 14)]
        + [{"name": "春节", "date": "2027-02-14", "isOffDay": False}]
        + [{"name": "清明节", "date": f"2027-04-0{day}", "isOffDay": True} for day in (3, 4, 5)]
        + [{"name": "劳动节", "date": f"2027-05-0{day}", "isOffDay": True} for day in (1, 2, 3, 4, 5)]
        + [{"name": "劳动节", "date": "2027-05-08", "isOffDay": False}]
        + [{"name": "端午节", "date": "2027-06-09", "isOffDay": True}]
        + [{"name": "中秋节", "date": "2027-09-15", "isOffDay": True}]
        + [{"name": "国庆节", "date": f"2027-10-0{day}", "isOffDay": True} for day in range(1, 8)]
        + [{"name": "国庆节", "date": "2027-10-09", "isOffDay": False}]
    ),
}


def fake_get(mapping):
    """A stand-in for ``holiday_update._get_json`` keyed by a URL fragment."""

    def get(url, timeout=8):
        for fragment, payload in mapping.items():
            if fragment in url:
                if isinstance(payload, Exception):
                    raise payload
                if payload is None:
                    raise requests.exceptions.ConnectionError(f"no route to {url}")
                return payload
        raise requests.exceptions.ConnectionError(f"no route to {url}")

    return get


TIMOR = {"timor.tech": TIMOR_2026}
HOLIDAY_CN = {"holiday-cn": HOLIDAY_CN_2027}


def fetch(mapping, year):
    with mock.patch.object(holiday_update, "_get_json", fake_get(mapping)):
        return holiday_update.fetch_year(year, timeout=1)


class FetchTests(unittest.TestCase):
    def test_timor_splits_legal_rest_and_makeup_days(self):
        rows, pending, source = fetch(TIMOR, 2026)
        self.assertEqual(source, "timor.tech")
        self.assertEqual(pending, [])
        legal = [day for day, kind, _name in rows if kind == holiday_update.LEGAL]
        rest = [day for day, kind, _name in rows if kind == holiday_update.REST]
        work = [day for day, kind, _name in rows if kind == holiday_update.WORK]
        self.assertEqual(len(legal), 9)      # 元旦 + 除夕/初一/初二/初三 + 清明 + 劳动×2 + 端午
        self.assertEqual(len(rest), 7)
        self.assertEqual(len(work), 4)

    def test_lunar_holidays_stay_pending_when_only_holiday_cn_answers(self):
        rows, pending, source = fetch(HOLIDAY_CN, 2027)
        self.assertIn("gov.cn", source)
        legal = sorted(day.isoformat() for day, kind, _name in rows
                       if kind == holiday_update.LEGAL)
        self.assertEqual(legal, ["2027-01-01", "2027-05-01", "2027-05-02",
                                 "2027-10-01", "2027-10-02", "2027-10-03"])
        self.assertEqual([entry["name"] for entry in pending], ["春节", "清明节", "端午节", "中秋节"])
        self.assertEqual(pending[0]["start"], "2027-02-05")
        self.assertEqual(pending[0]["end"], "2027-02-13")

    def test_unreachable_timor_still_falls_back(self):
        rows, pending, source = fetch(
            {"timor.tech": requests.exceptions.Timeout("slow"), "holiday-cn": HOLIDAY_CN_2027}, 2027)
        self.assertIn("gov.cn", source)
        self.assertEqual(len(pending), 4)

    def test_empty_timor_year_is_not_trusted(self):
        with self.assertRaises(holiday_update.HolidayDataError):
            fetch({"timor.tech": {"code": 0, "holiday": {}}, "holiday-cn": None}, 2027)

    def test_all_sources_down_reports_both(self):
        with self.assertRaises(holiday_update.HolidayDataError) as caught:
            fetch({"timor.tech": requests.exceptions.Timeout("a")}, 2027)
        self.assertIn("timor.tech", str(caught.exception))
        self.assertIn("holiday-cn", str(caught.exception))

    def test_incomplete_payload_is_rejected(self):
        small = {"code": 0, "holiday": {
            "01-01": {"holiday": True, "name": "元旦", "wage": 3, "date": "2026-01-01"}}}
        with self.assertRaises(holiday_update.HolidayDataError):
            fetch({"timor.tech": small}, 2026)

    def test_payload_for_another_year_is_rejected(self):
        with self.assertRaises(holiday_update.HolidayDataError):
            fetch({"timor.tech": TIMOR_2026, "holiday-cn": None}, 2027)


class TempDir:
    """A real directory inside the project (tempfile is blocked in some sandboxes)."""

    def __init__(self, name):
        root = Path(__file__).resolve().parent.parent / ".tmp-holiday-tests"
        self.path = root / f"{name}-{os.getpid()}"
        self.path.mkdir(parents=True, exist_ok=True)

    def file(self, name="holidays.json"):
        return self.path / name

    def cleanup(self):
        for child in self.path.glob("*"):
            child.unlink()
        self.path.rmdir()
        try:
            self.path.parent.rmdir()
        except OSError:
            pass


class MergeAndWriteTests(unittest.TestCase):
    def setUp(self):
        self.temp = TempDir("merge")
        self.addCleanup(self.temp.cleanup)

    def _existing(self):
        return {"updated": "2026-10-22", "source": "manual", "note": "old",
                "holidays": [{"name": "元旦", "start": "2026-01-01", "end": "2026-01-01"}],
                "adjustments": [{"kind": "workday", "name": "旧补班",
                                 "start": "2026-01-04", "end": "2026-01-04"}],
                "pending": []}

    def test_merge_keeps_other_years_and_replaces_the_fetched_one(self):
        existing = self._existing()
        rows, pending, source = fetch(HOLIDAY_CN, 2027)
        data = holiday_update.merge(existing, [{"year": 2027, "rows": rows, "pending": pending,
                                                "source": source}], today="2026-11-11")
        years = {entry["start"][:4] for entry in data["holidays"]}
        self.assertEqual(years, {"2026", "2027"})
        self.assertEqual(data["updated"], "2026-11-11")
        self.assertEqual(len(data["pending"]), 4)

    def test_merge_replaces_a_fetched_year_completely(self):
        existing = self._existing()
        rows, pending, source = fetch(TIMOR, 2026)
        data = holiday_update.merge(existing, [{"year": 2026, "rows": rows,
                                                "pending": pending, "source": source}],
                                    today="2026-11-11")
        self.assertEqual({entry["start"][:4] for entry in data["holidays"]}, {"2026"})
        self.assertEqual(len(data["holidays"]), 8)   # 元旦 1 + 除夕~初三 4 + 清明 1 + 劳动 1 + 端午 1
        self.assertTrue(any(entry.get("kind") == "workday" for entry in data["adjustments"]))

    def test_written_file_round_trips_through_the_calendar(self):
        path = self.temp.file()
        rows, pending, source = fetch(HOLIDAY_CN, 2027)
        summary = holiday_update.apply(path, [{"year": 2027, "rows": rows,
                                               "pending": pending, "source": source}],
                                       today="2026-11-11")
        self.assertTrue(summary["written"])
        calendar = holidays.load_calendar(path)
        self.assertIsNone(calendar.error)
        self.assertTrue(calendar.is_holiday("2027-01-01"))
        self.assertFalse(calendar.is_holiday("2027-02-06"))
        self.assertEqual(calendar.pending_for("2027-02-06"), "春节")
        self.assertEqual(calendar.adjustment("2027-02-14")["kind"], "workday")
        self.assertEqual(calendar.adjustment("2027-05-03")["kind"], "restday")

    def test_second_apply_does_not_rewrite_the_file(self):
        path = self.temp.file()
        rows, pending, source = fetch(HOLIDAY_CN, 2027)
        item = [{"year": 2027, "rows": rows, "pending": pending, "source": source}]
        holiday_update.apply(path, item, today="2026-11-11")
        before = path.stat().st_mtime_ns
        summary = holiday_update.apply(path, item, today="2026-11-12")
        self.assertFalse(summary["written"])
        self.assertEqual(path.stat().st_mtime_ns, before)
        self.assertFalse(path.with_name(path.name + ".tmp").exists())

    def test_failed_fetch_keeps_the_old_file(self):
        path = self.temp.file()
        path.write_text(json.dumps(self._existing(), ensure_ascii=False), encoding="utf-8")
        before = path.read_text(encoding="utf-8")
        summary = holiday_update.apply(path, [{"year": 2027, "error": "HolidayDataError: 无数据"}])
        self.assertFalse(summary["written"])
        self.assertEqual(path.read_text(encoding="utf-8"), before)

    def test_ensure_year_skips_when_the_year_is_complete(self):
        path = self.temp.file()
        rows, pending, source = fetch(TIMOR, 2026)
        holiday_update.apply(path, [{"year": 2026, "rows": rows,
                                     "pending": pending, "source": source}],
                             today="2026-11-11")
        calls = []

        def fetcher(year, timeout=8):
            calls.append(year)
            return [], [], "should not run"

        summary = holiday_update.ensure_year(path, 2026, fetcher=fetcher)
        self.assertTrue(summary["written"] is False)
        self.assertIn("skipped", summary)
        self.assertEqual(calls, [])

    def test_ensure_year_retries_while_a_break_is_pending(self):
        path = self.temp.file()
        rows, pending, source = fetch(HOLIDAY_CN, 2027)
        holiday_update.apply(path, [{"year": 2027, "rows": rows,
                                     "pending": pending, "source": source}],
                             today="2026-11-11")
        calls = []

        def fetcher(year, timeout=8):
            calls.append(year)
            legal = [(holidays.as_date("2027-02-05"), holiday_update.LEGAL, "除夕")]
            rows = legal + [(holidays.as_date(f"2027-02-{day:02d}"), holiday_update.LEGAL, "春节")
                            for day in (6, 7, 8)]
            rows += [(holidays.as_date(f"2027-01-{day:02d}"), holiday_update.REST, "元旦")
                     for day in range(5, 22)]
            return rows, [], "timor.tech"

        summary = holiday_update.ensure_year(path, 2027, fetcher=fetcher)
        self.assertEqual(calls, [2027])
        self.assertTrue(summary["written"])
        self.assertEqual(summary["pending"], 0)
        calendar = holidays.load_calendar(path)
        self.assertTrue(calendar.is_holiday("2027-02-06"))
        self.assertIsNone(calendar.pending_for("2027-02-06"))


class ShippedFileTests(unittest.TestCase):
    def test_project_calendar_still_loads(self):
        calendar = holidays.load_calendar(holidays.default_path())
        self.assertIsNone(calendar.error)
        self.assertTrue(len(calendar) > 0)

    def test_broken_pending_entry_does_not_disable_holidays(self):
        temp = TempDir("broken")
        self.addCleanup(temp.cleanup)
        path = temp.file()
        path.write_text(json.dumps({
            "holidays": [{"name": "元旦", "start": "2027-01-01", "end": "2027-01-01"}],
            "adjustments": [],
            "pending": [{"name": "春节"}, {"name": "春节", "start": "2027-02-05",
                                            "end": "2027-02-13"}],
        }, ensure_ascii=False), encoding="utf-8")
        calendar = holidays.load_calendar(path)
        self.assertIsNone(calendar.error)
        self.assertTrue(calendar.is_holiday("2027-01-01"))
        self.assertEqual(calendar.pending_for("2027-02-06"), "春节")


if __name__ == "__main__":
    unittest.main()
