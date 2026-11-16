"""Offline checks for the post-run reservation check and its account barrier."""

import contextlib
import datetime as dt
import io
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import reserve_segments as runner

LOCAL_TZ = dt.timezone(dt.timedelta(hours=8))
DAY = dt.date(2026, 11, 11)


def epoch(day, text):
    """Epoch milliseconds of a local time on *day*, the shape the school returns."""
    return int(dt.datetime.fromisoformat(f"{day}T{text}").replace(tzinfo=LOCAL_TZ).timestamp() * 1000)


class FakeRequests:
    def __init__(self):
        self.headers = {}


class FakeClient:
    def __init__(self, login_ok=True, message=""):
        self.requests = FakeRequests()
        self.login_ok = login_ok
        self.message = message
        self.login_calls = []

    def get_login_status(self):
        pass

    def login(self, username, password):
        self.login_calls.append((username, password))
        return self.login_ok, self.message


def base_config():
    return {"reserve": [], "deptIdEnc": "dept-from-config"}


def base_user(**extra):
    return dict({
        "username": "u1", "password": "p", "roomid": "11031", "seatid": ["144"],
        "time": ["11:00", "19:00"], "backups": [], "daysofweek": [],
    }, **extra)


class VerificationTests(unittest.TestCase):
    def test_verification_uses_shared_login_retry(self):
        with mock.patch.object(runner, "login_with_retry", return_value=(False, "密码错误")) as login:
            event, _ = self.verify([])
        self.assertEqual(event["error"], "LoginFailed")
        self.assertEqual(login.call_args.args[1]["username"], "u1")
        budget = (login.call_args.kwargs["deadline"] - dt.datetime.now()).total_seconds()
        self.assertGreater(budget, 28)
        self.assertLessEqual(budget, 30)

    def verify(self, records, user=None, client=None, day=DAY):
        events = []
        with mock.patch.object(runner, "reserve", return_value=client or FakeClient()), \
                mock.patch.object(runner, "reservation_fid_enc", return_value="fid-1"), \
                mock.patch.object(runner, "current_reservations", return_value=records):
            event = runner.verify_account_reservations(base_config(), 2, user or base_user(),
                                                       day, emit=events.append)
        return event, [json.loads(item) for item in events]

    def test_records_on_the_target_day_fill_the_plan(self):
        records = [
            {"id": 7, "seatNum": "144", "roomId": 11031, "status": 0,
             "startTime": epoch(DAY, "11:00"), "endTime": epoch(DAY, "15:00")},
            {"id": 8, "seatNum": "144", "roomId": 11031, "status": 0,
             "startTime": epoch(DAY, "15:00"), "endTime": epoch(DAY, "19:00")},
        ]
        event, events = self.verify(records)
        self.assertEqual(event["verified"], "complete")
        self.assertEqual([item["time"] for item in event["reservations"]],
                         [["11:00", "15:00"], ["15:00", "19:00"]])
        self.assertEqual([item["id"] for item in event["reservations"]], [7, 8])
        self.assertEqual([item["room"] for item in event["reservations"]],
                         ["11031", "11031"])
        self.assertEqual([item["status"] for item in event["reservations"]], [0, 0])
        self.assertEqual(event["missing"], [])
        self.assertEqual(event["target"], ["11:00", "19:00"])
        self.assertEqual(event["account_index"], 2)
        self.assertEqual(events, [event])

    def test_partial_records_leave_the_rest_of_the_plan_missing(self):
        records = [{"id": 7, "seatNum": "144", "roomId": 11031,
                    "startTime": epoch(DAY, "11:00"), "endTime": epoch(DAY, "15:00")}]
        event, _events = self.verify(records)
        self.assertEqual(event["verified"], "partial")
        self.assertEqual(event["missing"], [["15:00", "19:00"]])

    def test_records_of_other_days_are_ignored(self):
        records = [{"id": 7, "seatNum": "144", "roomId": 11031,
                    "startTime": epoch(DAY - dt.timedelta(days=1), "11:00"),
                    "endTime": epoch(DAY - dt.timedelta(days=1), "15:00")}]
        event, _events = self.verify(records)
        self.assertEqual(event["verified"], "none")
        self.assertEqual(event["reservations"], [])
        self.assertEqual(event["missing"], [["11:00", "15:00"], ["15:00", "19:00"]])

    def test_unparsable_records_are_skipped(self):
        event, _events = self.verify([{"id": 7, "seatNum": "144"}, {"id": 8,
                                                                     "startTime": "x",
                                                                     "endTime": "y"}])
        self.assertEqual(event["verified"], "none")
        self.assertEqual(event["reservations"], [])

    def test_login_failure_is_reported_as_unavailable(self):
        event, _events = self.verify([], client=FakeClient(login_ok=False, message="密码错误"))
        self.assertEqual(event["verified"], "unavailable")
        self.assertEqual(event["error"], "LoginFailed")
        self.assertEqual(event["message"], "密码错误")

    def test_unreadable_records_are_reported_as_unavailable(self):
        event, _events = self.verify(None)
        self.assertEqual(event["verified"], "unavailable")
        self.assertEqual(event["error"], "VerifyUnavailable")

    def test_fid_enc_falls_back_to_the_configured_school(self):
        seen = {}

        def fake_fid(_client, roomid, day):
            return None

        def fake_records(_client, fid_enc, timeout=None):
            seen["fid"] = fid_enc
            return []

        with mock.patch.object(runner, "reserve", return_value=FakeClient()), \
                mock.patch.object(runner, "reservation_fid_enc", side_effect=fake_fid), \
                mock.patch.object(runner, "current_reservations", side_effect=fake_records), \
                contextlib.redirect_stdout(io.StringIO()):
            runner.verify_account_reservations(base_config(), 1, base_user(), DAY)
        self.assertEqual(seen["fid"], "dept-from-config")

    def test_unexpected_error_never_raises(self):
        events = []
        with mock.patch.object(runner, "reserve", side_effect=RuntimeError("boom")):
            event = runner.verify_account_reservations(base_config(), 1, base_user(), DAY,
                                                       emit=events.append)
        self.assertEqual(event["verified"], "unavailable")
        self.assertEqual(event["error"], "RuntimeError")
        self.assertEqual(len(events), 1)


class VerificationViewTests(unittest.TestCase):
    def test_verification_view_lists_the_query_results(self):
        from run_log_view import format_verification

        event = {"verified": "complete", "checked_at": "22:02:55",
                 "date": str(DAY),
                 "reservations": [{"time": ["11:00", "15:00"], "seat": "144",
                                   "room": "11031", "id": 7, "status": 0}],
                 "missing": []}
        text = "\n".join(format_verification(event))
        self.assertIn("预约信息", text)
        self.assertIn("144", text)
        self.assertIn("11031", text)
        self.assertIn("记录号 7", text)
        self.assertIn("待履约", text)
        self.assertIn("目标时段已全部预约", text)

    def test_run_report_uses_the_check_not_a_failed_status(self):
        """2026-11-12: status said failed while the school held the whole window."""
        from run_log_view import format_run_log

        events = [
            {"date": "2026-11-12", "plan": 1, "time": ["18:00", "22:00"],
             "roomid": "11031", "seatid": ["142"], "success": False,
             "message": "提交超时", "error": "ReadTimeout"},
            {"status": "failed", "target": ["10:00", "22:00"],
             "missing": [["10:00", "14:00"], ["14:00", "18:00"], ["18:00", "22:00"]]},
            {"verify": "account_result", "account_index": 2, "date": "2026-11-12",
             "checked_at": "22:02:54", "target": ["10:00", "22:00"],
             "reservations": [{"time": ["10:00", "22:00"], "seat": "142",
                               "room": "11031", "id": 200217154, "status": 0}],
             "missing": [], "verified": "complete", "message": ""},
        ]
        raw = "\n".join(json.dumps(event, ensure_ascii=False) for event in events)
        text = format_run_log(raw)
        self.assertIn("结果：目标时段已全部预约（已核对）", text)
        self.assertNotIn("结果：预约失败", text)
        self.assertIn("200217154", text)


class RunStateTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.config = Path(self.temp.name) / "config.json"

    def test_markers_accumulate_and_are_idempotent(self):
        runner.mark_scheduled_run(self.config, DAY, 1, "started")
        runner.mark_scheduled_run(self.config, DAY, 1, "started")
        runner.mark_scheduled_run(self.config, DAY, 2, "started")
        state = runner.scheduled_run_state(self.config, DAY)
        self.assertEqual(state["started"], [1, 2])
        self.assertEqual(state["finished"], [])
        self.assertEqual(state["pids"], {1: os.getpid(), 2: os.getpid()})
        runner.mark_scheduled_run(self.config, DAY, 1, "finished")
        self.assertEqual(runner.scheduled_run_state(self.config, DAY)["finished"], [1])

    def test_a_new_day_starts_from_scratch(self):
        runner.mark_scheduled_run(self.config, DAY, 1, "finished")
        self.assertEqual(runner.scheduled_run_state(self.config, DAY + dt.timedelta(days=1)),
                         {"date": str(DAY + dt.timedelta(days=1)), "started": [],
                          "finished": [], "pids": {}})

    def test_invalid_phase_is_rejected(self):
        with self.assertRaises(ValueError):
            runner.mark_scheduled_run(self.config, DAY, 1, "done")

    def test_accounts_that_never_started_are_not_awaited(self):
        runner.mark_scheduled_run(self.config, DAY, 1, "started")
        runner.mark_scheduled_run(self.config, DAY, 1, "finished")
        self.assertEqual(runner.wait_for_scheduled_runs(self.config, DAY, [1, 2],
                                                        wait_seconds=0), [])

    def test_wait_returns_once_every_started_account_finished(self):
        now = [0.0]

        def sleep(seconds):
            now[0] += seconds
            if now[0] >= 1.5:
                runner.mark_scheduled_run(self.config, DAY, 2, "finished")

        runner.mark_scheduled_run(self.config, DAY, 1, "started")
        runner.mark_scheduled_run(self.config, DAY, 1, "finished")
        runner.mark_scheduled_run(self.config, DAY, 2, "started")
        pending = runner.wait_for_scheduled_runs(self.config, DAY, [1, 2], wait_seconds=30,
                                                 sleep_fn=sleep, clock=lambda: now[0])
        self.assertEqual(pending, [])
        self.assertGreaterEqual(now[0], 1.5)

    def test_wait_times_out_and_names_the_missing_account(self):
        now = [0.0]

        def sleep(seconds):
            now[0] += seconds

        runner.mark_scheduled_run(self.config, DAY, 1, "started")
        runner.mark_scheduled_run(self.config, DAY, 1, "finished")
        runner.mark_scheduled_run(self.config, DAY, 2, "started")
        pending = runner.wait_for_scheduled_runs(self.config, DAY, [1, 2], wait_seconds=2,
                                                 sleep_fn=sleep, clock=lambda: now[0],
                                                 alive_fn=lambda pid: True)
        self.assertEqual(pending, [2])
        self.assertLess(now[0], 3)

    def test_a_slow_run_is_waited_for_past_the_old_fixed_delay(self):
        """A sibling that is still booking is awaited, not abandoned after 75s."""
        now = [0.0]
        finished_at = [300.0]

        def sleep(seconds):
            now[0] += seconds
            if now[0] >= finished_at[0]:
                runner.mark_scheduled_run(self.config, DAY, 2, "finished")

        runner.mark_scheduled_run(self.config, DAY, 1, "started")
        runner.mark_scheduled_run(self.config, DAY, 1, "finished")
        runner.mark_scheduled_run(self.config, DAY, 2, "started")
        pending = runner.wait_for_scheduled_runs(self.config, DAY, [1, 2], wait_seconds=420,
                                                 sleep_fn=sleep, clock=lambda: now[0],
                                                 alive_fn=lambda pid: now[0] < finished_at[0])
        self.assertEqual(pending, [])
        self.assertGreaterEqual(now[0], 300.0)

    def test_a_sibling_whose_process_died_stops_blocking(self):
        now = [0.0]

        def sleep(seconds):
            now[0] += seconds

        runner.mark_scheduled_run(self.config, DAY, 1, "started")
        runner.mark_scheduled_run(self.config, DAY, 1, "finished")
        runner.mark_scheduled_run(self.config, DAY, 2, "started")
        pending = runner.wait_for_scheduled_runs(self.config, DAY, [1, 2], wait_seconds=60,
                                                 sleep_fn=sleep, clock=lambda: now[0],
                                                 alive_fn=lambda pid: False)
        self.assertEqual(pending, [])
        self.assertEqual(now[0], 0.0)

    def test_process_liveness_helpers(self):
        self.assertTrue(runner.process_alive(os.getpid()))
        self.assertFalse(runner.process_alive(None))
        self.assertFalse(runner.process_alive(0))
        self.assertFalse(runner.process_alive(2 ** 31 - 2))

    def test_default_wait_has_no_fixed_give_up_window(self):
        """The default must outlast any realistic run, not expire after minutes."""
        import inspect

        default = inspect.signature(runner.wait_for_scheduled_runs).parameters["wait_seconds"].default
        self.assertEqual(default, runner.WAIT_DEADLINE)
        self.assertGreaterEqual(default, 20 * 60)
        self.assertEqual(default, runner.TASK_TIME_LIMIT - 2 * 60)


class MainVerificationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.config_path = Path(self.temp.name) / "config.json"
        self.config_path.write_text(json.dumps({
            "reserve": [base_user(), dict(base_user(username="u2"), roomid="11031",
                                          seatid=["142"])],
            "scheduleAt": "22:00",
        }), encoding="utf-8")

    def run_main(self, *extra, run_account=None, verify_result=None, run_code=0):
        args = ["reserve_segments.py", "-u", str(self.config_path),
                "--ignore-weekday", "--ignore-holiday", *extra]
        calls, bookings = [], []

        def fake_run_account(_args, _config, user, on_success=None, outcome=None):
            bookings.append(user["username"])
            if outcome is not None:
                outcome.update(run_account(outcome))
            return run_code

        def fake_verify(_config, index, _user, target_day):
            calls.append((index, str(target_day)))
            return dict(verify_result or {})

        captured = io.StringIO()
        with mock.patch.object(sys, "argv", args), \
                mock.patch.object(runner, "run_account", side_effect=fake_run_account), \
                mock.patch.object(runner, "verify_account_reservations", side_effect=fake_verify), \
                contextlib.redirect_stdout(captured), contextlib.redirect_stderr(io.StringIO()):
            code = runner.main()
        return code, calls, bookings, captured.getvalue()

    def test_each_account_is_checked_after_the_scheduled_run(self):
        code, calls, bookings, _out = self.run_main(
            run_account=lambda outcome: {"attempted": True})
        self.assertEqual(code, 0)
        self.assertEqual(bookings, ["u1", "u2"])
        self.assertEqual([index for index, _day in calls], [1, 2])
        self.assertEqual(calls[0][1], str(dt.date.today() + dt.timedelta(days=1)))

    def test_the_school_records_override_a_failed_submit_result(self):
        code, _calls, _bookings, out = self.run_main(
            run_account=lambda outcome: {"attempted": True}, run_code=1,
            verify_result={"verified": "complete", "target": ["10:00", "22:00"],
                           "missing": []})
        self.assertEqual(code, 0)
        events = [json.loads(line) for line in out.splitlines() if line.startswith("{")]
        final = [event for event in events if event.get("verified") is True]
        self.assertEqual(len(final), 2)
        self.assertEqual(final[-1]["status"], "complete")
        self.assertEqual(final[-1]["missing"], [])
        self.assertEqual(final[-1]["source"], "学习通预约记录核对")

    def test_a_partial_check_keeps_a_non_zero_exit_code(self):
        missing = [["10:00", "14:00"]]
        code, _calls, _bookings, out = self.run_main(
            run_account=lambda outcome: {"attempted": True}, run_code=1,
            verify_result={"verified": "partial", "target": ["10:00", "22:00"],
                           "missing": missing})
        self.assertEqual(code, 1)
        events = [json.loads(line) for line in out.splitlines() if line.startswith("{")]
        final = [event for event in events if event.get("verified") is True]
        self.assertEqual(final[-1]["status"], "partial_success")
        self.assertEqual(final[-1]["missing"], missing)

    def test_an_unavailable_check_keeps_the_submit_result(self):
        code, _calls, _bookings, out = self.run_main(
            run_account=lambda outcome: {"attempted": True}, run_code=1,
            verify_result={"verified": "unavailable", "error": "VerifyUnavailable"})
        self.assertEqual(code, 1)
        events = [json.loads(line) for line in out.splitlines() if line.startswith("{")]
        self.assertEqual([event for event in events if event.get("verified") is True], [])

    def test_skipped_run_is_not_checked(self):
        code, calls, _bookings, _out = self.run_main(
            run_account=lambda outcome: {"skipped": "statutory holiday"})
        self.assertEqual(code, 0)
        self.assertEqual(calls, [])

    def test_check_only_never_checks_results(self):
        code, calls, bookings, _out = self.run_main(
            "--check-only", run_account=lambda outcome: {"attempted": True})
        self.assertEqual(code, 0)
        self.assertEqual(bookings, ["u1", "u2"])
        self.assertEqual(calls, [])

    def test_today_run_does_not_wait_or_check(self):
        code, calls, bookings, _out = self.run_main(
            "--today", run_account=lambda outcome: {"attempted": True})
        self.assertEqual(code, 0)
        self.assertEqual(bookings, ["u1", "u2"])
        self.assertEqual(calls, [])

    def test_verify_only_checks_without_booking(self):
        code, calls, bookings, _out = self.run_main(
            "--verify-only", run_account=lambda outcome: {"attempted": True})
        self.assertEqual(code, 0)
        self.assertEqual(bookings, [])
        self.assertEqual([index for index, _day in calls], [1, 2])


if __name__ == "__main__":
    unittest.main()
