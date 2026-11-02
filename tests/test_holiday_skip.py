"""Offline checks for skipping scheduled reservations on statutory holidays."""

import contextlib
import datetime as dt
import io
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import reserve_segments
from utils import holidays

TOMORROW = dt.date.today() + dt.timedelta(days=1)


class FakeCalendar:
    """Holiday calendar stub whose coverage and errors are fixed by the test."""

    def __init__(self, days=None, error=None, years=None, adjustments=None):
        self.days = {holidays.as_date(day): name for day, name in (days or {}).items()}
        self.adjustments = {holidays.as_date(day): info
                            for day, info in (adjustments or {}).items()}
        self.error = error
        self.path = Path("holidays.json")
        self.years = {day.year for day in self.days} if years is None else set(years)

    def name(self, value):
        return self.days.get(holidays.as_date(value))

    def adjustment(self, value):
        return self.adjustments.get(holidays.as_date(value))

    def covers(self, value):
        return holidays.as_date(value).year in self.years

    def upcoming(self, start, end):
        first, last = holidays.as_date(start), holidays.as_date(end)
        return [(day, info["kind"], info["name"]) for day, info in sorted(self.adjustments.items())
                if first <= day <= last]


class FakeRequests:
    headers = {}

    def post(self, *_args, **_kwargs):
        return type("Response", (), {"json": lambda self: {"success": True, "msg": "ok"}})()


class FakeReserve:
    created = 0
    attempts = []

    def __init__(self, **_kwargs):
        FakeReserve.created += 1
        self.requests = FakeRequests()

    def get_login_status(self):
        pass

    def login(self, *_args):
        return True, ""

    def submit(self, segment, roomid, seatid, _action):
        FakeReserve.attempts.append((roomid, seatid[0], segment[0]))
        self.requests.post("https://office.chaoxing.com/data/apps/seat/submit")
        return True


class HolidaySkipTests(unittest.TestCase):
    def setUp(self):
        FakeReserve.created = 0
        FakeReserve.attempts = []
        self.temp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(self.temp.cleanup)
        self.config = Path(self.temp.name) / "config.json"

    def write_config(self, **extra):
        data = {
            "reserve": [{
                "username": "test", "password": "test", "roomid": "101",
                "seatid": ["001"], "time": ["11:00", "15:00"], "backups": [],
                "daysofweek": ["Sunday", "Monday", "Tuesday", "Wednesday",
                               "Thursday", "Friday", "Saturday"],
            }],
        }
        data.update(extra)
        self.config.write_text(json.dumps(data), encoding="utf-8")

    def run_cli(self, *extra, calendar=None):
        output, errors = io.StringIO(), io.StringIO()
        args = ["reserve_segments.py", "-u", str(self.config), *extra]
        with contextlib.ExitStack() as stack:
            stack.enter_context(patch.object(sys, "argv", args))
            stack.enter_context(patch.object(reserve_segments, "reserve", FakeReserve))
            if calendar is not None:
                stack.enter_context(patch.object(reserve_segments.holidays, "calendar",
                                                 return_value=calendar))
            stack.enter_context(contextlib.redirect_stdout(output))
            stack.enter_context(contextlib.redirect_stderr(errors))
            code = reserve_segments.main()
        return code, [json.loads(line) for line in output.getvalue().splitlines()], errors.getvalue()

    def test_holiday_skips_before_login(self):
        self.write_config()
        code, lines, _ = self.run_cli("--ignore-weekday", calendar=FakeCalendar({TOMORROW: "春节"}))
        self.assertEqual(code, 0)
        self.assertEqual(lines, [{"date": str(TOMORROW), "skipped": "statutory holiday",
                                  "holiday": "春节"}])
        self.assertEqual(FakeReserve.created, 0)

    def test_ignore_holiday_flag_still_reserves(self):
        self.write_config()
        code, lines, _ = self.run_cli("--ignore-weekday", "--ignore-holiday",
                                      calendar=FakeCalendar({TOMORROW: "春节"}))
        self.assertEqual(code, 0)
        self.assertEqual(FakeReserve.created, 1)
        self.assertEqual(lines[-1]["status"], "complete")

    def test_config_can_turn_holiday_skip_off(self):
        self.write_config(skipHolidays=False)
        code, lines, _ = self.run_cli("--ignore-weekday", calendar=FakeCalendar({TOMORROW: "春节"}))
        self.assertEqual(code, 0)
        self.assertEqual(FakeReserve.created, 1)
        self.assertEqual(lines[-1]["status"], "complete")

    def test_working_day_reserves_as_before(self):
        self.write_config()
        code, lines, _ = self.run_cli("--ignore-weekday", calendar=FakeCalendar())
        self.assertEqual(code, 0)
        self.assertEqual(FakeReserve.created, 1)
        self.assertEqual(lines[-1]["status"], "complete")

    def test_check_only_reports_holiday_without_booking(self):
        self.write_config()
        code, lines, _ = self.run_cli("--check-only", calendar=FakeCalendar({TOMORROW: "国庆节"}))
        self.assertEqual(code, 0)
        self.assertEqual(lines[0]["holiday"], "国庆节")
        self.assertEqual(lines[0]["targetDate"], str(TOMORROW))
        self.assertTrue(lines[0]["skipHolidays"])
        self.assertEqual(FakeReserve.created, 0)

    def test_unreadable_holiday_data_warns_but_reserves(self):
        self.write_config()
        code, _lines, errors = self.run_cli(
            "--ignore-weekday", calendar=FakeCalendar(error="holidays.json: boom"))
        self.assertEqual(code, 0)
        self.assertEqual(FakeReserve.created, 1)
        self.assertIn("holidays.json", errors)

    def test_year_without_entries_warns_but_reserves(self):
        self.write_config()
        code, _lines, errors = self.run_cli("--ignore-weekday", calendar=FakeCalendar(years=set()))
        self.assertEqual(code, 0)
        self.assertEqual(FakeReserve.created, 1)
        self.assertIn(f"no {TOMORROW.year} entries", errors)

    def test_undecided_adjusted_day_reserves_and_warns(self):
        self.write_config()
        calendar = FakeCalendar(adjustments={TOMORROW: {"kind": "workday", "name": "春节调休上班"}})
        code, lines, errors = self.run_cli("--ignore-weekday", calendar=calendar)
        self.assertEqual(code, 0)
        self.assertEqual(FakeReserve.created, 1)
        self.assertEqual(lines[-1]["status"], "complete")
        self.assertIn("adjusted workday", errors)

    def test_saved_skip_decision_stops_adjusted_day(self):
        self.write_config(adjustments={str(TOMORROW): "skip"})
        calendar = FakeCalendar(adjustments={TOMORROW: {"kind": "restday", "name": "春节调休放假"}})
        code, lines, _ = self.run_cli("--ignore-weekday", calendar=calendar)
        self.assertEqual(code, 0)
        self.assertEqual(lines, [{"date": str(TOMORROW), "skipped": "user decision",
                                  "holiday": None, "adjustment": "春节调休放假"}])
        self.assertEqual(FakeReserve.created, 0)

    def test_saved_reserve_decision_overrides_holiday_skip(self):
        self.write_config(adjustments={str(TOMORROW): "reserve"})
        code, lines, _ = self.run_cli("--ignore-weekday", calendar=FakeCalendar({TOMORROW: "国庆节"}))
        self.assertEqual(code, 0)
        self.assertEqual(FakeReserve.created, 1)
        self.assertEqual(lines[-1]["status"], "complete")

    def test_check_only_reports_adjustment_and_decision(self):
        self.write_config(adjustments={str(TOMORROW): "skip"})
        calendar = FakeCalendar(adjustments={TOMORROW: {"kind": "workday", "name": "国庆调休上班"}})
        code, lines, _ = self.run_cli("--check-only", calendar=calendar)
        self.assertEqual(code, 0)
        self.assertEqual(lines[0]["adjustment"]["kind"], "workday")
        self.assertEqual(lines[0]["decision"], "skip")
        self.assertEqual(FakeReserve.created, 0)


class HolidayDataTests(unittest.TestCase):
    def setUp(self):
        self.project_file = Path(holidays.__file__).resolve().parent.parent / holidays.FILENAME

    def test_shipped_statutory_days_are_read(self):
        calendar = holidays.load_calendar(self.project_file)
        self.assertIsNone(calendar.error)
        self.assertEqual(calendar.name(dt.date(2026, 1, 1)), "元旦")
        self.assertEqual(calendar.name(dt.date(2026, 2, 17)), "春节")
        self.assertEqual(calendar.name(dt.date(2026, 10, 2)), "国庆节")
        self.assertIsNone(calendar.name(dt.date(2026, 10, 23)))
        self.assertTrue(calendar.covers(dt.date(2026, 6, 1)))
        self.assertFalse(calendar.covers(dt.date(2035, 6, 1)))

    def test_ranges_and_explicit_dates(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as directory:
            path = Path(directory) / holidays.FILENAME
            path.write_text(json.dumps({"holidays": [
                {"name": "测试节", "start": "2030-01-01", "end": "2030-01-03"},
                {"name": "单日", "dates": ["2030-05-01"]},
            ]}), encoding="utf-8")
            calendar = holidays.load_calendar(path)
            self.assertIsNone(calendar.error)
            self.assertEqual(len(calendar), 4)
            self.assertEqual(calendar.name("2030-01-03"), "测试节")
            self.assertIsNone(calendar.name("2030-01-04"))
            self.assertEqual(calendar.name("2030-05-01"), "单日")
            self.assertTrue(calendar.is_holiday("2030-01-01"))

    def test_missing_file_degrades_instead_of_raising(self):
        calendar = holidays.load_calendar(Path("missing-directory") / holidays.FILENAME)
        self.assertIsNotNone(calendar.error)
        self.assertEqual(len(calendar), 0)
        self.assertFalse(calendar.is_holiday("2026-10-01"))

    def test_malformed_file_degrades_instead_of_raising(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as directory:
            path = Path(directory) / holidays.FILENAME
            path.write_text("{not json", encoding="utf-8")
            calendar = holidays.load_calendar(path)
            self.assertIsNotNone(calendar.error)
            self.assertEqual(len(calendar), 0)

    def test_adjustments_are_parsed(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as directory:
            path = Path(directory) / holidays.FILENAME
            path.write_text(json.dumps({"adjustments": [
                {"name": "春节调休上班", "start": "2030-02-16", "end": "2030-02-17",
                 "kind": "workday"},
                {"kind": "restday", "dates": ["2030-02-20"]},
            ]}), encoding="utf-8")
            calendar = holidays.load_calendar(path)
            self.assertIsNone(calendar.error)
            self.assertEqual(calendar.adjustment("2030-02-17"),
                             {"kind": "workday", "name": "春节调休上班"})
            self.assertEqual(calendar.adjustment("2030-02-20")["kind"], "restday")
            self.assertEqual(calendar.adjustment("2030-02-20")["name"], "调休放假")
            self.assertIsNone(calendar.adjustment("2030-02-18"))
            self.assertEqual([day for day, _kind, _name in
                              calendar.upcoming("2030-02-16", "2030-02-20")],
                             [dt.date(2030, 2, 16), dt.date(2030, 2, 17), dt.date(2030, 2, 20)])

    def test_unknown_adjustment_kind_is_reported(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as directory:
            path = Path(directory) / holidays.FILENAME
            path.write_text(json.dumps({"adjustments": [
                {"start": "2030-02-16", "kind": "holiday"},
            ]}), encoding="utf-8")
            calendar = holidays.load_calendar(path)
            self.assertIsNotNone(calendar.error)
            self.assertEqual(calendar.adjustments, {})


class GuiHolidaySaveTests(unittest.TestCase):
    @unittest.skipUnless(sys.platform == "win32", "Windows desktop GUI")
    def test_schedule_save_persists_holiday_toggle(self):
        import desktop_gui

        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as directory:
            config = Path(directory) / "config.local.json"
            config.write_text(json.dumps({
                "deptIdEnc": "1234567890abcdef", "scheduleAt": "22:00",
                "reserve": [{"username": "test", "password": "test", "roomid": "11031",
                             "seatid": ["145"], "time": ["11:00", "15:00"],
                             "daysofweek": ["Monday"]}],
            }), encoding="utf-8")

            def fake_task_status(action="Inspect", at=None, days=None, account=1):
                return {"state": "Ready", "at": None, "daysOfWeek": 0,
                        "nextRunTime": None, "lastRunTime": None, "lastTaskResult": None}

            with patch.object(desktop_gui, "CONFIG_PATH", config), patch.object(
                    desktop_gui, "task_status", fake_task_status):
                app = desktop_gui.SeatApp()
                try:
                    app.withdraw()
                    self.assertTrue(app.skip_holidays.get())
                    app.loaded_dept = "1234567890abcdef"
                    app.room_names = {"主区域（11031）": "11031"}
                    app.schedule_room.set("主区域（11031）")
                    app.schedule_days["Monday"].set(True)
                    app.skip_holidays.set(False)
                    app._work = lambda task, done, _status=None: done(task())
                    app._save_schedule()
                    saved = json.loads(config.read_text(encoding="utf-8"))
                    self.assertIs(saved["reserve"][0]["skipHolidays"], False)
                finally:
                    app.destroy()

    @unittest.skipUnless(sys.platform == "win32", "Windows desktop GUI")
    def test_adjustment_decision_is_saved_right_away(self):
        import desktop_gui
        from unittest.mock import patch as mock_patch

        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as directory:
            config = Path(directory) / "config.local.json"
            config.write_text(json.dumps({
                "deptIdEnc": "1234567890abcdef", "scheduleAt": "22:00",
                "reserve": [{"username": "test", "password": "test", "roomid": "11031",
                             "seatid": ["145"], "time": ["11:00", "15:00"],
                             "daysofweek": ["Monday"]}],
            }), encoding="utf-8")

            def fake_task_status(action="Inspect", at=None, days=None, account=1):
                return {"state": "Ready", "at": None, "daysOfWeek": 0,
                        "nextRunTime": None, "lastRunTime": None, "lastTaskResult": None}

            calendar = FakeCalendar(adjustments={TOMORROW: {"kind": "workday",
                                                            "name": "国庆调休上班"}})
            with mock_patch.object(desktop_gui, "CONFIG_PATH", config), mock_patch.object(
                    desktop_gui, "task_status", fake_task_status), mock_patch.object(
                    desktop_gui.holidays, "calendar", return_value=calendar):
                app = desktop_gui.SeatApp()
                try:
                    app.withdraw()
                    self.assertEqual(app.adjustment_tree.get_children(), (str(TOMORROW),))
                    app.adjustment_tree.selection_set(str(TOMORROW))
                    app._set_adjustment_decision("skip")
                    saved = json.loads(config.read_text(encoding="utf-8"))
                    self.assertEqual(saved["reserve"][0]["adjustments"], {str(TOMORROW): "skip"})
                    app.adjustment_tree.selection_set(str(TOMORROW))
                    app._set_adjustment_decision(None)
                    saved = json.loads(config.read_text(encoding="utf-8"))
                    self.assertEqual(saved["reserve"][0]["adjustments"], {})
                finally:
                    app.destroy()


if __name__ == "__main__":
    unittest.main()
