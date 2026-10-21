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
        self.config = Path(self.temp.name) / "config.json"
        self.config.write_text(json.dumps({"reserve": [{
            "username": "test", "password": "test", "roomid": "101", "seatid": ["001"],
            "time": ["11:00", "19:00"], "daysofweek": [],
            "backups": [{"roomid": "202", "seatid": ["002"], "time": ["12:00", "16:00"]}],
        }]}), encoding="utf-8")

    def run_cli(self, *extra):
        output = io.StringIO()
        args = ["reserve_segments.py", "-u", str(self.config), *extra]
        with patch.object(sys, "argv", args), patch.object(reserve_segments, "reserve", FakeReserve), contextlib.redirect_stdout(output):
            code = reserve_segments.main()
        return code, [json.loads(line) for line in output.getvalue().splitlines()]

    def test_backup_runs_after_primary_fully_fails(self):
        FakeReserve.outcomes = {("202", "12:00"): True}
        code, lines = self.run_cli("--ignore-weekday")
        self.assertEqual(code, 0)
        self.assertEqual([entry[0] for entry in FakeReserve.attempts], ["101", "101", "202"])
        self.assertEqual([line["plan"] for line in lines], [1, 1, 2])

    def test_partial_primary_stops_before_backup(self):
        FakeReserve.outcomes = {("101", "11:00"): True, ("202", "12:00"): True}
        code, lines = self.run_cli("--ignore-weekday")
        self.assertEqual(code, 1)
        self.assertEqual([entry[0] for entry in FakeReserve.attempts], ["101", "101"])
        self.assertEqual(lines[-1]["status"], "partial_success")

    def test_check_only_lists_plans_without_booking(self):
        code, lines = self.run_cli("--check-only")
        self.assertEqual(code, 0)
        self.assertEqual([plan["roomid"] for plan in lines[0]["plans"]], ["101", "202"])
        self.assertEqual(FakeReserve.attempts, [])

    def test_explicit_override_does_not_use_backups(self):
        code, lines = self.run_cli("--check-only", "--roomid", "303")
        self.assertEqual(code, 0)
        self.assertEqual([plan["roomid"] for plan in lines[0]["plans"]], ["303"])


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

            def fake_task_status(action="Inspect", at=None, days=None):
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
