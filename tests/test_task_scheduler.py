"""Scheduler regression tests using a disposable Windows task."""
import datetime as dt
import json
import subprocess
import sys
import tempfile
import unittest
import uuid
from pathlib import Path
from unittest import mock

import desktop_gui


class TaskStatusTests(unittest.TestCase):
    def test_access_denied_is_explained_and_logged(self):
        raw = "New-ScheduledTaskTrigger : access denied 0x80041003"
        with tempfile.TemporaryDirectory() as directory, mock.patch.object(
                desktop_gui, "ROOT", Path(directory)), mock.patch.object(
                desktop_gui.subprocess, "run", return_value=
                subprocess.CompletedProcess([], 1, "", raw)):
            with self.assertRaises(RuntimeError) as caught:
                desktop_gui.task_status("Set", "22:00", ["Monday"])
            self.assertIn("Windows 拒绝访问", str(caught.exception))
            self.assertNotIn("New-ScheduledTaskTrigger", str(caught.exception))
            self.assertIn(raw, (Path(directory) / "schedule-task-error.log").read_text(encoding="utf-8"))

    def test_timeout_does_not_report_missing(self):
        with mock.patch.object(desktop_gui.subprocess, "run",
                               side_effect=subprocess.TimeoutExpired([], 30)):
            with self.assertRaisesRegex(RuntimeError, "响应超时"):
                desktop_gui.task_status()

    def test_save_failure_preserves_task_and_rollback_keeps_trigger_time(self):
        for fail_reserve in (True, False):
            with self.subTest(fail_reserve=fail_reserve), tempfile.TemporaryDirectory() as directory:
                path = Path(directory) / "config.local.json"
                original = {"deptIdEnc": "1234567890abcdef", "reserve": [{
                    "username": "test", "password": "test", "roomid": "11031",
                    "seatid": ["144"], "time": ["10:00", "22:00"],
                    "daysofweek": ["Monday"], "scheduleAt": "22:00"}]}
                path.write_text(json.dumps(original), encoding="utf-8")
                calls = []
                failed = False

                def scheduler(action="Inspect", at=None, days=None, account=1, kind="reserve"):
                    nonlocal failed
                    calls.append((action, at, kind))
                    if not failed and ((fail_reserve and action == "Set") or
                                       (not fail_reserve and action == "Remove")):
                        failed = True
                        raise RuntimeError("simulated failure")
                    return {"state": "Ready", "at": "2026-11-14T21:59:00",
                            "daysOfWeek": 2, "lastTaskResult": None, "nextRunTime": None}

                with mock.patch.object(desktop_gui, "CONFIG_PATH", path), mock.patch.object(
                        desktop_gui.SeatApp, "_load_rooms"), mock.patch.object(
                        desktop_gui.SeatApp, "_refresh_schedule"), mock.patch.object(
                        desktop_gui, "task_status", scheduler):
                    app = desktop_gui.SeatApp()
                    try:
                        app.withdraw()
                        app.loaded_dept = "1234567890abcdef"
                        app.room_names = {"主区域（11031）": "11031"}
                        app.schedule_room.set("主区域（11031）")
                        app._work = lambda task, done, status=None: done(task())
                        with self.assertRaisesRegex(RuntimeError, "simulated failure"):
                            app._save_schedule()
                        self.assertEqual(json.loads(path.read_text(encoding="utf-8")), original)
                        reserve_writes = [call for call in calls if call[0] != "Inspect" and call[2] == "reserve"]
                        self.assertEqual(reserve_writes, [("Set", "22:00", "reserve")] * (1 if fail_reserve else 2))
                    finally:
                        app.destroy()


@unittest.skipUnless(sys.platform == "win32", "Windows Task Scheduler")
class TaskSchedulerIntegrationTests(unittest.TestCase):
    def test_disposable_task_roundtrip(self):
        name = "ChaoXingRegression-" + uuid.uuid4().hex
        script = Path(desktop_gui.__file__).with_name("schedule_task.ps1")

        def invoke(action, *args):
            result = subprocess.run(["powershell.exe", "-NoProfile", "-NonInteractive",
                                     "-ExecutionPolicy", "Bypass", "-File", str(script),
                                     "-TaskName", name, "-Action", action, *args],
                                    capture_output=True, text=True, timeout=30)
            self.assertEqual(result.returncode, 0, result.stderr)
            return json.loads(result.stdout)

        tomorrow = dt.datetime.now() + dt.timedelta(days=1)
        day = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"][tomorrow.weekday()]
        self.assertEqual(invoke("Inspect")["state"], "Missing")
        try:
            created = invoke("Set", "-At", "12:34", "-Days", day)
            self.assertEqual(created["state"], "Ready")
            self.assertIn("T12:33:00", created["at"])
            self.assertEqual(created["daysOfWeek"], 1 << ((tomorrow.weekday() + 1) % 7))
            self.assertIn("T13:33:00", invoke("Set", "-At", "13:34", "-Days", day)["at"])
            self.assertIn("T14:34:00", invoke("Set", "-Kind", "transfer", "-At", "14:34", "-Days", day)["at"])
        finally:
            self.assertEqual(invoke("Remove")["state"], "Missing")
        self.assertEqual(invoke("Remove")["state"], "Missing")
