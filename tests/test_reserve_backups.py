"""Offline checks for ordered scheduled backup plans."""

import contextlib
import io
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import reserve_segments
from run_log_view import format_run_log


class NoHolidays:
    """Deterministic stand-in so the weekday tests never depend on the calendar."""

    error = None
    path = "holidays.json"

    def name(self, _value):
        return None

    def adjustment(self, _value):
        return None

    def covers(self, _value):
        return True


class FakeRequests:
    def __init__(self):
        self.headers = {}
        self.response = {}

    def post(self, *_args, **_kwargs):
        response = self.response
        return type("Response", (), {"json": lambda self: response})()


class FakeReserve:
    outcomes = {}
    attempts = []

    def __init__(self, **_kwargs):
        self.requests = FakeRequests()

    def get_login_status(self):
        pass

    def login(self, *_args):
        return True, ""

    def submit(self, segment, roomid, seatid, _action):
        self.attempts.append((roomid, seatid[0], segment[0]))
        success = self.outcomes.get((roomid, segment[0]), False)
        self.requests.response = {"success": success, "msg": "ok" if success else "occupied"}
        self.requests.post("https://office.chaoxing.com/data/apps/seat/submit")
        return success


class BackupTests(unittest.TestCase):
    def setUp(self):
        FakeReserve.attempts = []
        FakeReserve.outcomes = {}
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        holiday_patch = patch.object(reserve_segments.holidays, "calendar",
                                     return_value=NoHolidays())
        holiday_patch.start()
        self.addCleanup(holiday_patch.stop)
        self.config = Path(self.temp.name) / "config.json"
        self.config.write_text(json.dumps({"reserve": [{
            "username": "test", "password": "test", "roomid": "101", "seatid": ["001"],
            "time": ["11:00", "19:00"], "daysofweek": [],
            "backups": [{"roomid": "202", "seatid": ["002"], "time": ["11:00", "19:00"]}],
        }]}), encoding="utf-8")

    def run_cli(self, *extra):
        output = io.StringIO()
        args = ["reserve_segments.py", "-u", str(self.config), *extra]
        with patch.object(sys, "argv", args), patch.object(reserve_segments, "reserve", FakeReserve), contextlib.redirect_stdout(output):
            code = reserve_segments.main()
        return code, [json.loads(line) for line in output.getvalue().splitlines()]

    def test_first_success_skips_other_scheduled_account(self):
        data = json.loads(self.config.read_text(encoding="utf-8"))
        data["autoCancelOtherOnSuccess"] = True
        second = dict(data["reserve"][0])
        second["username"] = "second"
        data["reserve"].append(second)
        self.config.write_text(json.dumps(data), encoding="utf-8")
        FakeReserve.outcomes = {("101", "11:00"): True}
        first_code, first = self.run_cli("--account", "1", "--ignore-weekday")
        attempts_after_first = list(FakeReserve.attempts)
        second_code, second_lines = self.run_cli("--account", "2", "--ignore-weekday")
        self.assertEqual(first_code, 1)  # Partial success still claims the day.
        self.assertEqual(second_code, 0)
        self.assertEqual(FakeReserve.attempts, attempts_after_first)
        self.assertEqual(second_lines[-1]["skipped"], "other account succeeded")

    def test_backup_runs_after_primary_fully_fails(self):
        FakeReserve.outcomes = {("202", "11:00"): True, ("202", "15:00"): True}
        code, lines = self.run_cli("--ignore-weekday")
        self.assertEqual(code, 0)
        self.assertEqual([entry[0] for entry in FakeReserve.attempts], ["101", "101", "202", "202"])
        self.assertEqual([line["plan"] for line in lines if "plan" in line], [1, 1, 2, 2])
        self.assertEqual(lines[-1]["status"], "complete")

    def test_partial_primary_is_filled_without_rebooking_successful_slot(self):
        FakeReserve.outcomes = {("101", "11:00"): True, ("202", "15:00"): True}
        code, lines = self.run_cli("--ignore-weekday")
        self.assertEqual(code, 0)
        self.assertEqual(FakeReserve.attempts, [("101", "001", "11:00"),
                                                 ("101", "001", "15:00"),
                                                 ("202", "002", "15:00")])
        self.assertEqual(lines[-1]["status"], "complete")

    def test_backup_time_is_limited_to_primary_target(self):
        user = json.loads(self.config.read_text(encoding="utf-8"))
        user["reserve"][0]["backups"][0]["time"] = ["10:00", "16:00"]
        self.config.write_text(json.dumps(user), encoding="utf-8")
        FakeReserve.outcomes = {("202", "11:00"): True, ("202", "15:00"): True}
        code, lines = self.run_cli("--ignore-weekday")
        self.assertEqual(code, 1)
        self.assertEqual([entry[2] for entry in FakeReserve.attempts if entry[0] == "202"],
                         ["11:00", "15:00"])
        self.assertEqual(lines[-1]["missing"], [["16:00", "19:00"]])

    def test_later_backup_only_fills_earlier_backup_gap(self):
        user = json.loads(self.config.read_text(encoding="utf-8"))
        user["reserve"][0]["backups"].append(
            {"roomid": "303", "seatid": ["003"], "time": ["11:00", "19:00"]})
        self.config.write_text(json.dumps(user), encoding="utf-8")
        FakeReserve.outcomes = {("202", "11:00"): True, ("303", "15:00"): True}
        code, lines = self.run_cli("--ignore-weekday")
        self.assertEqual(code, 0)
        self.assertEqual(FakeReserve.attempts[-1], ("303", "003", "15:00"))
        self.assertEqual([entry[2] for entry in FakeReserve.attempts if entry[0] == "303"], ["15:00"])
        self.assertEqual(lines[-1]["status"], "complete")

    def test_check_only_lists_plans_without_booking(self):
        code, lines = self.run_cli("--check-only")
        self.assertEqual(code, 0)
        self.assertEqual([plan["roomid"] for plan in lines[0]["plans"]], ["101", "202"])
        self.assertEqual(FakeReserve.attempts, [])

    def test_today_check_only_targets_current_date(self):
        import datetime as dt
        code, lines = self.run_cli("--today", "--check-only")
        self.assertEqual(code, 0)
        self.assertEqual(lines[0]["targetDate"], dt.date.today().isoformat())
        self.assertEqual(FakeReserve.attempts, [])

    def test_two_accounts_are_checked_and_reserved_independently(self):
        data = json.loads(self.config.read_text(encoding="utf-8"))
        second = dict(data["reserve"][0], username="second", roomid="303",
                      seatid=["003"], backups=[])
        data["reserve"].append(second)
        self.config.write_text(json.dumps(data), encoding="utf-8")
        code, lines = self.run_cli("--check-only")
        self.assertEqual(code, 0)
        self.assertEqual([line["account"] for line in lines if "username" in line], [1, 2])
        self.assertEqual([line["plans"][0]["roomid"] for line in lines if "plans" in line],
                         ["101", "303"])
        FakeReserve.outcomes = {("303", "11:00"): True, ("303", "15:00"): True}
        code, lines = self.run_cli("--ignore-weekday")
        self.assertEqual(code, 1)
        self.assertIn(("303", "003", "11:00"), FakeReserve.attempts)
        self.assertEqual([line["status"] for line in lines if "status" in line],
                         ["failed", "complete"])

    def test_incomplete_second_account_does_not_block_first(self):
        data = json.loads(self.config.read_text(encoding="utf-8"))
        data["reserve"].append({"username": "second", "password": "test",
                                "roomid": "", "seatid": [], "time": ["11:00", "19:00"],
                                "daysofweek": ["Wednesday"]})
        self.config.write_text(json.dumps(data), encoding="utf-8")
        code, lines = self.run_cli("--check-only")
        self.assertEqual(code, 0)
        self.assertEqual([line["account"] for line in lines if "account" in line], [1])
        self.assertEqual([line["plans"][0]["roomid"] for line in lines if "plans" in line],
                         ["101"])

    def test_account_selector_runs_only_selected_account(self):
        data = json.loads(self.config.read_text(encoding="utf-8"))
        data["reserve"].append(dict(data["reserve"][0], username="second",
                                    roomid="303", seatid=["003"], backups=[]))
        self.config.write_text(json.dumps(data), encoding="utf-8")
        code, lines = self.run_cli("--check-only", "--account", "2")
        self.assertEqual(code, 0)
        self.assertEqual([line["account"] for line in lines if "account" in line], [2])
        self.assertEqual([line["plans"][0]["roomid"] for line in lines if "plans" in line],
                         ["303"])

    def test_third_account_is_isolated_and_requires_saved_schedule(self):
        data = json.loads(self.config.read_text(encoding="utf-8"))
        for number in (2, 3):
            data["reserve"].append(dict(data["reserve"][0],
                                        username=f"account{number}",
                                        roomid=str(300 + number),
                                        seatid=[f"00{number}"], backups=[],
                                        scheduleEnabled=(number == 2)))
        self.config.write_text(json.dumps(data), encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "No account has a saved reservation plan"):
            self.run_cli("--check-only", "--account", "3")
        code, lines = self.run_cli("--check-only", "--account", "2")
        self.assertEqual(code, 0)
        self.assertEqual([line["account"] for line in lines if "account" in line], [2])
        data["reserve"][2]["scheduleEnabled"] = True
        self.config.write_text(json.dumps(data), encoding="utf-8")
        code, lines = self.run_cli("--check-only", "--account", "3")
        self.assertEqual(code, 0)
        self.assertEqual([line["plans"][0]["roomid"] for line in lines if "plans" in line],
                         ["303"])

    def test_two_account_log_has_separate_results(self):
        log = "\n".join(json.dumps(event) for event in (
            {"account": 1, "username": "first"},
            {"login_ok": False, "message": "wrong password"},
            {"account": 2, "username": "second"},
            {"date": "2026-10-29", "skipped": "weekday not configured"},
        ))
        report = format_run_log(log)
        self.assertIn("账号 1 · first", report)
        self.assertIn("账号 2 · second", report)
        self.assertIn("登录失败", report)
        self.assertIn("今天未设置定时预约", report)

    def test_explicit_override_does_not_use_backups(self):
        code, lines = self.run_cli("--check-only", "--roomid", "303")
        self.assertEqual(code, 0)
        self.assertEqual([plan["roomid"] for plan in lines[0]["plans"]], ["303"])

    def test_classic_room_evening_first_only_when_account_opted_in(self):
        data = json.loads(self.config.read_text(encoding="utf-8"))
        user = data["reserve"][0]
        user.update(roomid="11031", time=["11:00", "22:00"], classicEveningFirst=True)
        user["backups"] = [{"roomid": "11031", "seatid": ["002"],
                            "time": ["11:00", "22:00"]}]
        self.config.write_text(json.dumps(data), encoding="utf-8")
        code, lines = self.run_cli("--check-only")
        self.assertEqual(code, 0)
        self.assertEqual(lines[0]["attemptOrder"][0]["segments"],
                         [["15:00", "19:00"], ["19:00", "22:00"], ["11:00", "15:00"]])

        self.run_cli("--ignore-weekday")
        self.assertEqual([entry[2] for entry in FakeReserve.attempts[:3]],
                         ["15:00", "19:00", "11:00"])
        self.assertEqual([entry[2] for entry in FakeReserve.attempts[3:]],
                         ["15:00", "19:00", "11:00"])

        user.pop("classicEveningFirst")
        self.config.write_text(json.dumps(data), encoding="utf-8")
        FakeReserve.attempts = []
        self.run_cli("--ignore-weekday")
        self.assertEqual([entry[2] for entry in FakeReserve.attempts[:3]],
                         ["11:00", "15:00", "19:00"])

    def test_evening_priority_does_not_apply_to_other_room(self):
        data = json.loads(self.config.read_text(encoding="utf-8"))
        user = data["reserve"][0]
        user.update(roomid="11032", time=["11:00", "22:00"], classicEveningFirst=True)
        user["backups"] = []
        self.config.write_text(json.dumps(data), encoding="utf-8")
        self.run_cli("--ignore-weekday")
        self.assertEqual([entry[2] for entry in FakeReserve.attempts],
                         ["11:00", "15:00", "19:00"])

    def test_evening_priority_backup_only_fills_missing_slots(self):
        data = json.loads(self.config.read_text(encoding="utf-8"))
        user = data["reserve"][0]
        user.update(roomid="11031", time=["11:00", "22:00"], classicEveningFirst=True)
        user["backups"] = [{"roomid": "11031", "seatid": ["002"],
                            "time": ["11:00", "22:00"]}]
        self.config.write_text(json.dumps(data), encoding="utf-8")
        FakeReserve.outcomes = {("11031", "15:00"): True}
        self.run_cli("--ignore-weekday")
        self.assertEqual([entry[2] for entry in FakeReserve.attempts],
                         ["15:00", "19:00", "11:00", "19:00", "11:00"])

    def test_evening_priority_splits_at_15_for_nonstandard_start(self):
        self.assertEqual(reserve_segments.ordered_uncovered_segments(
            "14:30", "20:00", [], True),
            [["15:00", "19:00"], ["19:00", "20:00"], ["14:30", "15:00"]])


class GuiBackupSaveTests(unittest.TestCase):
    @unittest.skipUnless(sys.platform == "win32", "Windows desktop GUI")
    def test_schedule_save_persists_backup_without_touching_real_task(self):
        import desktop_gui

        with tempfile.TemporaryDirectory() as directory:
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
                    app.loaded_dept = "1234567890abcdef"
                    app.room_names = {"主区域（11031）": "11031", "备选区域（11032）": "11032"}
                    app.schedule_room.set("主区域（11031）")
                    app.schedule_days["Monday"].set(True)
                    app.backup_plans = [{"roomid": "11032", "seatid": ["095"],
                                         "time": ["18:30", "21:00"]}]
                    app._work = lambda task, done, _status=None: done(task())
                    app._save_schedule()
                    saved = json.loads(config.read_text(encoding="utf-8"))
                    self.assertEqual(saved["reserve"][0]["backups"], app.backup_plans)
                finally:
                    app.destroy()


if __name__ == "__main__":
    unittest.main()
