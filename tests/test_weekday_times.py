"""Offline checks for per-weekday booking windows (timesByWeekday)."""

import argparse
import contextlib
import datetime as dt
import io
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import reserve_segments as runner

SATURDAY = "Saturday"
MONDAY = "Monday"


class FakeResponse:
    def __init__(self, payload=None, text=""):
        self._payload = payload
        self.text = text

    def json(self):
        return self._payload


class FakeRequests:
    def __init__(self):
        self.headers = {}
        self.post = None
        self.calls = []

    def get(self, url, params=None, timeout=None):
        self.calls.append({"url": url, "params": params})
        if "/front/apps/seat/select" in url:
            return FakeResponse(text="fidEnc = 'fid-123'")
        if "/data/apps/seat/index" in url:
            return FakeResponse({"success": True, "data": {"curReserves": []}})
        raise AssertionError(url)


class FakeClient:
    """Minimal stand-in for utils.reserve that records submitted windows."""

    def __init__(self, fail_first=0):
        self.reserve_next_day = True
        self.enable_slider = True
        self.url = "https://office.chaoxing.com/front/third/apps/seat/code?id={}&seatNum={}"
        self.submit_url = "https://office.chaoxing.com/data/apps/seat/submit"
        self.last_submit_payload = {}
        self.submit_attempts = []
        self.max_attempt = 2
        self.requests = FakeRequests()
        self.submitted = []
        self.fail_first = fail_first

    def get_login_status(self):
        return None

    def login(self, _username, _password):
        return True, ""

    def _get_page_token(self, _url, require_value=False):
        return "token", "value"

    def resolve_captcha(self):
        return "captcha"

    def get_submit(self, url, times, token, roomid, seatid, captcha, action, value, timeout=None):
        self.submitted.append(list(times))
        if len(self.submitted) <= self.fail_first:
            self.last_submit_payload = {"success": False, "msg": "该时间段已被占用！"}
            return False
        self.last_submit_payload = {"success": True, "msg": "",
                                    "data": {"seatReserve": {"id": len(self.submitted)}}}
        return True


def account(**overrides):
    user = {
        "username": "u1", "password": "p", "roomid": "11031", "seatid": ["144"],
        "time": ["10:00", "22:00"], "daysofweek": ["Saturday"], "fastSprint": True,
        "backups": [{"roomid": "11031", "seatid": ["143"], "time": ["11:00", "22:00"]}],
        "timesByWeekday": {"Saturday": ["10:00", "21:00"]},
    }
    user.update(overrides)
    return user


class WeekdayWindowTests(unittest.TestCase):
    def test_weekday_override_replaces_the_default_window(self):
        self.assertEqual(runner.weekday_window(account(), SATURDAY), ["10:00", "21:00"])

    def test_other_weekdays_keep_the_default_window(self):
        self.assertEqual(runner.weekday_window(account(), MONDAY), ["10:00", "22:00"])

    def test_no_weekday_keeps_the_default_window_for_older_callers(self):
        self.assertEqual(runner.weekday_window(account()), ["10:00", "22:00"])
        self.assertEqual(runner.weekday_window(account(timesByWeekday=None), SATURDAY),
                         ["10:00", "22:00"])

    def test_primary_plan_uses_the_target_weekday_window(self):
        plans = runner.reservation_plans(account(), weekday=SATURDAY)
        self.assertEqual(plans[0]["segments"],
                         [["10:00", "14:00"], ["14:00", "18:00"], ["18:00", "21:00"]])
        plans = runner.reservation_plans(account(), weekday=MONDAY)
        self.assertEqual(plans[0]["segments"],
                         [["10:00", "14:00"], ["14:00", "18:00"], ["18:00", "22:00"]])

    def test_backup_keeps_its_own_range(self):
        plans = runner.reservation_plans(account(backups=[]), weekday=SATURDAY)
        self.assertEqual(len(plans), 1)
        plans = runner.reservation_plans(account(), weekday=SATURDAY)
        self.assertEqual(plans[1]["segments"],
                         [["11:00", "15:00"], ["15:00", "19:00"], ["19:00", "22:00"]])

    def test_cli_start_and_end_override_the_weekday_window(self):
        plans = runner.reservation_plans(account(), {"start": "09:00", "end": "12:00"}, SATURDAY)
        self.assertEqual(plans[0]["segments"], [["09:00", "12:00"]])

    def test_invalid_weekday_windows_are_rejected(self):
        for bad in (["10:00"], {"start": "10:00", "end": "21:00"}, ["21:00", "10:00"],
                    ["10:00", "9:00"], ["10:00", "21:00", "22:00"], "10:00-21:00"):
            with self.subTest(bad=bad):
                with self.assertRaises(ValueError):
                    runner.weekday_window(account(timesByWeekday={"Saturday": bad}), SATURDAY)

    def test_non_object_overrides_are_rejected(self):
        with self.assertRaises(ValueError):
            runner.weekday_window(account(timesByWeekday=["10:00", "21:00"]), SATURDAY)

    def test_unknown_weekday_names_are_rejected_instead_of_ignored(self):
        with self.assertRaises(ValueError):
            runner.weekday_window(account(timesByWeekday={"Saterday": ["10:00", "21:00"]}), SATURDAY)


class SprintWindowTests(unittest.TestCase):
    """The sprint must not submit outside the resolved weekday window."""

    def _run(self, user, weekday, fail_first=0):
        client = FakeClient(fail_first)
        plans = runner.reservation_plans(user, weekday=weekday)
        target_start = plans[0]["segments"][0][0]
        target_end = plans[0]["segments"][-1][1]
        with contextlib.redirect_stdout(io.StringIO()):
            runner.fast_race(client, {"scheduleAt": "00:00"}, plans, target_start, target_end,
                             dt.date.today().isoformat(), emit=lambda _line: None,
                             now_fn=lambda: dt.datetime.now(), sleep_fn=lambda _seconds: None)
        return client.submitted, target_end

    def test_saturday_backup_is_clamped_to_the_21_00_close(self):
        # The primary plan fails so the backup has to fill the same target window.
        submitted, target_end = self._run(account(), SATURDAY, fail_first=3)
        self.assertEqual(target_end, "21:00")
        self.assertTrue(all(chunk[1] <= "21:00" for chunk in submitted))
        self.assertNotIn(["19:00", "22:00"], submitted)
        self.assertIn(["19:00", "21:00"], submitted)
        self.assertIn(["11:00", "15:00"], submitted)

    def test_monday_keeps_the_22_00_close(self):
        submitted, target_end = self._run(account(), MONDAY)
        self.assertEqual(target_end, "22:00")
        self.assertTrue(all(chunk[1] <= "22:00" for chunk in submitted))
        self.assertIn(["18:00", "22:00"], submitted)


class RetryCapOrderTests(unittest.TestCase):
    def _args(self):
        return argparse.Namespace(user="config.json", check_only=False, account=None,
                                  roomid=None, seatid=None, start=None, end=None,
                                  ignore_weekday=True, ignore_holiday=True, today=False)

    def test_fast_sprint_sets_the_retry_cap_before_the_tracer_wraps_the_session(self):
        client = FakeClient()
        seen = {}
        with mock.patch.object(runner, "reserve", return_value=client), \
                mock.patch.object(runner, "trace_http_timings",
                                  side_effect=lambda session: seen.update(
                                      cap=getattr(session, "temporary_retry_attempts", None))), \
                contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            runner.run_account(self._args(), {"scheduleAt": "00:00", "skipHolidays": False},
                               account())
        self.assertEqual(seen["cap"], 1)

    def test_legacy_account_keeps_the_default_retry_cap(self):
        client = FakeClient()
        seen = {}
        with mock.patch.object(runner, "reserve", return_value=client), \
                mock.patch.object(runner, "trace_http_timings",
                                  side_effect=lambda session: seen.update(
                                      cap=getattr(session, "temporary_retry_attempts", None))), \
                contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            runner.run_account(self._args(), {"scheduleAt": "00:00", "skipHolidays": False},
                               account(fastSprint=False))
        self.assertIsNone(seen["cap"])


class CheckOnlyOutputTests(unittest.TestCase):
    def test_check_only_reports_the_target_weekday_and_clamps_backup_preview(self):
        target = dt.date.today() + dt.timedelta(days=1)
        weekday = target.strftime("%A")
        output = io.StringIO()
        args = argparse.Namespace(user="config.json", check_only=True, account=None,
                                  roomid=None, seatid=None, start=None, end=None,
                                  ignore_weekday=False, ignore_holiday=False, today=False)
        with contextlib.redirect_stdout(output), contextlib.redirect_stderr(io.StringIO()):
            code = runner.run_account(
                args, {"skipHolidays": False},
                account(timesByWeekday={weekday: ["10:00", "21:00"]}))
        self.assertEqual(code, 0)
        payload = json.loads(output.getvalue().strip().splitlines()[-1])
        self.assertEqual(payload["weekday"], weekday)
        self.assertEqual(payload["targetDate"], target.isoformat())
        self.assertEqual(payload["plans"][0]["segments"][-1][1], "21:00")
        backup = next(plan for plan in payload["attemptOrder"] if plan["plan"] == 2)
        self.assertTrue(all(segment[1] <= "21:00" for segment in backup["segments"]))
        self.assertIn(["19:00", "21:00"], backup["segments"])


@unittest.skipUnless(sys.platform == "win32", "Windows desktop GUI")
class GuiWeekdaySaveTests(unittest.TestCase):
    def _fake_task_status(self, action="Inspect", at=None, days=None, account=1,
                          kind="reserve"):
        return {"state": "Ready", "at": None, "daysOfWeek": 0, "nextRunTime": None,
                "lastRunTime": None, "lastTaskResult": None}

    def test_schedule_save_persists_and_clears_per_weekday_windows(self):
        import desktop_gui

        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as directory:
            config = Path(directory) / "config.local.json"
            config.write_text(json.dumps({
                "deptIdEnc": "1234567890abcdef", "scheduleAt": "22:00",
                "reserve": [{"username": "test", "password": "test", "roomid": "11031",
                             "seatid": ["144"], "time": ["10:00", "22:00"],
                             "timesByWeekday": {"Sunday": ["10:00", "21:00"]},
                             "daysofweek": ["Monday"]}],
            }), encoding="utf-8")
            with mock.patch.object(desktop_gui, "CONFIG_PATH", config), \
                    mock.patch.object(desktop_gui, "task_status", self._fake_task_status):
                app = desktop_gui.SeatApp()
                try:
                    app.withdraw()
                    self.assertEqual(app.weekday_times, {"Sunday": ["10:00", "21:00"]})
                    self.assertIn("周日 10:00–21:00", app.weekday_summary.get())
                    app.loaded_dept = "1234567890abcdef"
                    app.room_names = {"主区域（11031）": "11031"}
                    app.schedule_room.set("主区域（11031）")
                    app.schedule_days["Monday"].set(True)
                    app.weekday_times = {"Saturday": ["10:00", "21:00"]}
                    app._work = lambda task, done, _status=None: done(task())
                    app._save_schedule()
                    saved = json.loads(config.read_text(encoding="utf-8"))
                    self.assertEqual(saved["reserve"][0]["timesByWeekday"],
                                     {"Saturday": ["10:00", "21:00"]})
                    app.weekday_times = {}
                    app._save_schedule()
                    saved = json.loads(config.read_text(encoding="utf-8"))
                    self.assertNotIn("timesByWeekday", saved["reserve"][0])
                    self.assertIn("未设置", app.weekday_summary.get())
                finally:
                    app.destroy()


if __name__ == "__main__":
    unittest.main()
