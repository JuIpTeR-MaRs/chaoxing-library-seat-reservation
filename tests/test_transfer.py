"""Offline checks for moving account 2's next-day reservation to account 1."""

import contextlib
import datetime as dt
import io
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import transfer_seats as transfer
from run_log_view import format_transfer_log

LOCAL_TZ = dt.timezone(dt.timedelta(hours=8))
DAY = dt.date(2026, 11, 12)  # the day being booked by the nightly run


def epoch(day, text):
    return int(dt.datetime.fromisoformat(f"{day}T{text}")
               .replace(tzinfo=LOCAL_TZ).timestamp() * 1000)


def record_span(record):
    return (record["startTime"], record["endTime"])


class FakeSchool:
    """A minimal stand-in for the school's reservation API."""

    def __init__(self):
        self.records = []
        self.next_id = 100
        self.cancelled = []
        self.submitted = []
        self.fail_cancel = set()
        self.fail_submit = set()  # (username, seat, chunk start) that must fail
        self.fail_then_unavailable = False
        self.unavailable = set()

    def add(self, username, roomid, seat, start, end, status=0, day=DAY):
        record = {"id": self.next_id, "username": username, "roomId": roomid,
                  "seatNum": seat, "startTime": epoch(day, start),
                  "endTime": epoch(day, end), "status": status}
        self.next_id += 1
        self.records.append(record)
        return record

    def records_of(self, username):
        return [record for record in self.records if record["username"] == username]

    def cancel(self, client, record_id):
        self.cancelled.append((client.username, str(record_id)))
        if int(record_id) in self.fail_cancel:
            raise RuntimeError("不可取消")
        self.records = [record for record in self.records
                        if not (record["username"] == client.username
                                and str(record["id"]) == str(record_id))]

    def _overlapping(self, record, chunk):
        start, end = epoch(DAY, chunk[0]), epoch(DAY, chunk[1])
        return record["startTime"] < end and start < record["endTime"]

    def submit(self, client, chunk, roomid, seat, captcha="", timeout=None):
        self.submitted.append((client.username, str(seat), chunk[0], chunk[1]))
        if (client.username, str(seat), chunk[0]) in self.fail_submit:
            if self.fail_then_unavailable:
                # Simulate a network drop right after the failed submit: the
                # follow-up reservation query is then unavailable.
                self.unavailable.add(client.username)
            return {"success": False, "reason": "seat_taken",
                    "message": "该时间段已被预约"}
        for record in self.records:
            if record["status"] != 0 or not self._overlapping(record, chunk):
                continue
            if record["username"] == client.username:
                return {"success": False, "reason": "already_held",
                        "message": "当前时间段您已有预约"}
            if (str(record["seatNum"]) == str(seat)
                    and str(record["roomId"]) == str(roomid)):
                return {"success": False, "reason": "seat_taken",
                        "message": "该时间段已被预约"}
        self.add(client.username, roomid, seat, chunk[0], chunk[1])
        return {"success": True, "reason": None, "message": ""}


class FakeClient:
    def __init__(self, username, school):
        self.username = username
        self.school = school
        self.enable_slider = False
        self.requests = type("FakeRequests", (), {"headers": {}})()


class TransferTestBase(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.config_path = Path(self.temp.name) / "config.local.json"
        self.school = FakeSchool()
        self.config = {
            "reserve": [
                {"username": "u1", "password": "p1", "roomid": "11031",
                 "seatid": ["144"], "time": ["11:00", "22:00"],
                 "daysofweek": ["Thursday"], "backups": []},
                {"username": "u2", "password": "p2", "roomid": "11031",
                 "seatid": ["142"], "time": ["11:00", "22:00"],
                 "daysofweek": ["Thursday"], "backups": []},
            ],
            "deptIdEnc": "dept",
            "transferToAccount": {"enabled": True, "from": 2, "to": 1,
                                  "at": "22:30"},
        }
        self.write_config()
        self.events = []
        sleep_patch = mock.patch.object(transfer.time, "sleep", return_value=None)
        sleep_patch.start()
        self.addCleanup(sleep_patch.stop)
        for target, value in (
                ("reservation_fid_enc", mock.Mock(return_value="fid")),
                ("current_reservations", mock.Mock(side_effect=self._read)),
                ("cancel_reservation", mock.Mock(side_effect=self.school.cancel)),
                ("submit_chunk", mock.Mock(side_effect=self.school.submit)),
                ("solve_captcha", mock.Mock(return_value="captcha"))):
            patcher = mock.patch.object(transfer, target, value)
            patcher.start()
            self.addCleanup(patcher.stop)

    def write_config(self):
        self.config_path.write_text(json.dumps(self.config), encoding="utf-8")

    def _read(self, client, fid_enc, timeout=None):
        if client.username in self.school.unavailable:
            return None
        return [dict(record) for record in self.school.records_of(client.username)]

    def run_transfer(self, **kwargs):
        factory = kwargs.pop("client_factory",
                             lambda username, password, day: FakeClient(username,
                                                                        self.school))
        return transfer.transfer_day(self.config, str(self.config_path), DAY,
                                     emit=self.events.append, client_factory=factory,
                                     **kwargs)

    def parsed_events(self):
        return [json.loads(event) for event in self.events]


class SettingsTests(unittest.TestCase):
    def test_missing_setting_is_disabled(self):
        self.assertEqual(transfer.transfer_settings({"reserve": [{}, {}]}),
                         {"enabled": False, "from": 2, "to": 1, "at": "22:30"})

    def test_enabled_setting_is_read(self):
        settings = transfer.transfer_settings({"reserve": [{}, {}], "transferToAccount": {
            "enabled": True, "from": 2, "to": 1, "at": "22:30"}})
        self.assertEqual(settings, {"enabled": True, "from": 2, "to": 1,
                                    "at": "22:30"})

    def test_bad_time_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "HH:MM"):
            transfer.transfer_settings({"reserve": [{}, {}], "transferToAccount": {
                "enabled": True, "at": "22:3"}})

    def test_same_account_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "不能是同一个账号"):
            transfer.transfer_settings({"reserve": [{}, {}], "transferToAccount": {
                "enabled": True, "from": 1, "to": 1}})

    def test_out_of_range_account_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "超出范围"):
            transfer.transfer_settings({"reserve": [{}, {}], "transferToAccount": {
                "enabled": True, "from": 3, "to": 1}})

    def test_disabled_setting_does_not_validate_accounts(self):
        settings = transfer.transfer_settings({"reserve": [{}], "transferToAccount": {
            "enabled": False, "from": 5, "to": 1}})
        self.assertFalse(settings["enabled"])


class DecisionTests(TransferTestBase):
    def test_account1_complete_cancels_account2_only(self):
        account1 = self.school.add("u1", "11031", "144", "11:00", "22:00")
        account2 = self.school.add("u2", "11031", "142", "11:00", "22:00")
        result = self.run_transfer()
        self.assertEqual(result["mode"], "cancel_source")
        self.assertEqual(result["result"], "complete")
        self.assertEqual(self.school.cancelled, [("u2", str(account2["id"]))])
        self.assertEqual(self.school.submitted, [])
        self.assertEqual([record["id"] for record in self.school.records],
                         [account1["id"]])

    def test_account2_complete_moves_every_record_to_account1(self):
        account2 = self.school.add("u2", "11031", "142", "11:00", "22:00")
        result = self.run_transfer()
        self.assertEqual(result["mode"], "transfer")
        self.assertEqual(result["result"], "complete")
        self.assertEqual(self.school.cancelled, [("u2", str(account2["id"]))])
        self.assertEqual([item[2:] for item in self.school.submitted],
                         [("11:00", "15:00"), ("15:00", "19:00"),
                          ("19:00", "22:00")])
        coverage = [record_span(record) for record in self.school.records_of("u1")]
        self.assertEqual(coverage[0][0], epoch(DAY, "11:00"))
        self.assertEqual(coverage[-1][1], epoch(DAY, "22:00"))
        # The result event carries the queried reservation details for the popup.
        records = result["target"]["records"]
        self.assertEqual(len(records), 3)
        self.assertEqual({item["seat"] for item in records}, {"142"})
        self.assertTrue(all(item["id"] for item in records))

    def test_account1_partial_records_are_cancelled_before_the_move(self):
        account1 = self.school.add("u1", "11031", "144", "11:00", "15:00")
        account2 = self.school.add("u2", "11031", "142", "11:00", "22:00")
        result = self.run_transfer()
        self.assertEqual(result["result"], "complete")
        self.assertIn(("u1", str(account1["id"])), self.school.cancelled)
        self.assertIn(("u2", str(account2["id"])), self.school.cancelled)
        # Account 1 must be able to re-book 11:00-15:00 after its own record was
        # cancelled first; the submits therefore only come from u1.
        self.assertEqual({item[0] for item in self.school.submitted}, {"u1"})

    def test_account1_complete_wins_even_when_account2_is_partial(self):
        """User rule: account 1 complete means only account 2's records go."""
        self.school.add("u1", "11031", "144", "11:00", "22:00")
        account2 = self.school.add("u2", "11031", "142", "11:00", "15:00")
        result = self.run_transfer()
        self.assertEqual(result["mode"], "cancel_source")
        self.assertEqual(result["result"], "complete")
        self.assertEqual(self.school.cancelled, [("u2", str(account2["id"]))])
        self.assertEqual(self.school.submitted, [])

    def test_cross_window_target_record_keeps_its_outside_part(self):
        """A cancelled whole record must not lose the part outside the window."""
        self.school.add("u1", "11031", "144", "10:00", "14:00")
        self.school.add("u2", "11031", "142", "11:00", "22:00")
        result = self.run_transfer()
        self.assertEqual(result["mode"], "transfer")
        self.assertEqual(result["result"], "complete")
        held = sorted(record_span(record) for record in self.school.records_of("u1"))
        self.assertEqual(held[0], (epoch(DAY, "10:00"), epoch(DAY, "11:00")))
        self.assertEqual(held[-1][1], epoch(DAY, "22:00"))

    def test_check_only_does_not_write_the_state_file(self):
        self.school.add("u2", "11031", "142", "11:00", "22:00")
        self.run_transfer(check_only=True)
        self.assertFalse((Path(self.temp.name) / transfer.TRANSFER_STATE).exists())

    def test_incomplete_account2_makes_no_changes(self):
        account2 = self.school.add("u2", "11031", "142", "11:00", "15:00")
        result = self.run_transfer()
        self.assertEqual(result["mode"], "none")
        self.assertEqual(result["result"], "skipped")
        self.assertEqual(self.school.cancelled, [])
        self.assertEqual(self.school.submitted, [])
        self.assertEqual([record["id"] for record in self.school.records],
                         [account2["id"]])

    def test_nothing_booked_makes_no_changes(self):
        result = self.run_transfer()
        self.assertEqual(result["result"], "skipped")
        self.assertEqual(self.school.cancelled, [])
        self.assertEqual(self.school.submitted, [])

    def test_other_days_and_finished_records_are_ignored(self):
        self.school.add("u2", "11031", "142", "11:00", "22:00",
                        day=DAY - dt.timedelta(days=1))
        self.school.add("u2", "11031", "142", "11:00", "22:00", status=2)
        result = self.run_transfer()
        self.assertEqual(result["result"], "skipped")
        self.assertEqual(self.school.cancelled, [])
        self.assertEqual(self.school.submitted, [])

    def test_check_only_prints_the_plan_without_touching_anything(self):
        self.school.add("u2", "11031", "142", "11:00", "22:00")
        result = self.run_transfer(check_only=True)
        self.assertEqual(result["result"], "skipped")
        self.assertEqual(self.school.cancelled, [])
        self.assertEqual(self.school.submitted, [])
        plan = next(event for event in self.parsed_events()
                    if event.get("transfer") == "plan")
        self.assertEqual(plan["mode"], "transfer")
        self.assertEqual(len(plan["books"]), 3)

    def test_login_failure_reports_unavailable(self):
        self.school.add("u2", "11031", "142", "11:00", "22:00")

        def failing_factory(username, password, day):
            raise transfer.TransferUnavailable("登录失败：密码错误")

        result = self.run_transfer(client_factory=failing_factory)
        self.assertEqual(result["result"], "unavailable")
        self.assertIn("登录失败", result["message"])
        self.assertEqual(self.school.cancelled, [])

    def test_unreadable_account2_records_report_unavailable(self):
        self.school.add("u2", "11031", "142", "11:00", "22:00")
        self.school.unavailable = {"u2"}
        result = self.run_transfer()
        self.assertEqual(result["result"], "unavailable")
        self.assertEqual(self.school.cancelled, [])
        self.assertEqual(self.school.submitted, [])

    def test_completed_day_is_skipped_by_the_state_file(self):
        self.school.add("u2", "11031", "142", "11:00", "22:00")
        (Path(self.temp.name) / transfer.TRANSFER_STATE).write_text(
            json.dumps({"date": str(DAY), "result": "complete"}), encoding="utf-8")
        result = self.run_transfer()
        self.assertEqual(result["result"], "skipped")
        self.assertIn("已经完成过转预约", result["message"])
        self.assertEqual(self.school.cancelled, [])

    def test_force_ignores_the_completed_state(self):
        self.school.add("u2", "11031", "142", "11:00", "22:00")
        (Path(self.temp.name) / transfer.TRANSFER_STATE).write_text(
            json.dumps({"date": str(DAY), "result": "complete"}), encoding="utf-8")
        result = self.run_transfer(force=True)
        self.assertEqual(result["result"], "complete")


class FailureTests(TransferTestBase):
    def test_cross_window_source_record_keeps_its_outside_part(self):
        self.school.add("u1", "11031", "144", "11:00", "22:00")
        self.school.add("u2", "11031", "142", "10:00", "22:00")
        result = self.run_transfer()
        self.assertEqual(result["mode"], "cancel_source")
        self.assertEqual(result["result"], "complete")
        held = [record_span(record) for record in self.school.records_of("u2")]
        self.assertEqual(held, [(epoch(DAY, "10:00"), epoch(DAY, "11:00"))])

    def test_unavailable_query_still_restores_definite_failures(self):
        """Requirement: a failed re-book is given back even if the read fails."""
        self.school.add("u2", "11031", "142", "11:00", "22:00")
        self.school.fail_submit = {("u1", "142", "15:00")}
        self.school.fail_then_unavailable = True
        result = self.run_transfer()
        self.assertEqual(result["result"], "partial")
        self.assertTrue(result["restored"])
        restored = [record_span(record) for record in self.school.records_of("u2")]
        self.assertIn((epoch(DAY, "15:00"), epoch(DAY, "19:00")), restored)

    def test_state_write_failure_does_not_turn_success_into_error(self):
        self.school.add("u2", "11031", "142", "11:00", "22:00")
        with mock.patch.object(transfer, "_write_state",
                               side_effect=OSError("disk full")):
            result = self.run_transfer()
        self.assertEqual(result["result"], "complete")
        self.assertTrue(any(event.get("transfer") == "warning"
                            for event in self.parsed_events()))

    def test_failed_rebook_is_restored_to_account2_immediately(self):
        self.school.add("u2", "11031", "142", "11:00", "22:00")
        self.school.fail_submit = {("u1", "142", "15:00")}
        result = self.run_transfer()
        self.assertEqual(result["result"], "partial")
        self.assertTrue(result["restored"])
        self.assertTrue(all(item["success"] for item in result["restored"]))
        restored = [record_span(record) for record in self.school.records_of("u2")]
        self.assertEqual(restored, [(epoch(DAY, "15:00"), epoch(DAY, "19:00"))])
        held = [record_span(record) for record in self.school.records_of("u1")]
        self.assertEqual(held, [(epoch(DAY, "11:00"), epoch(DAY, "15:00")),
                                (epoch(DAY, "19:00"), epoch(DAY, "22:00"))])

    def test_account2_record_that_cannot_be_cancelled_stays(self):
        blocked = self.school.add("u2", "11031", "142", "11:00", "15:00")
        moving = self.school.add("u2", "11031", "142", "15:00", "22:00")
        self.school.fail_cancel = {blocked["id"]}
        result = self.run_transfer()
        self.assertEqual(result["result"], "partial")
        self.assertIn(("u2", str(blocked["id"])), self.school.cancelled)
        self.assertIn(("u2", str(moving["id"])), self.school.cancelled)
        self.assertEqual([record["id"] for record in self.school.records_of("u2")],
                         [blocked["id"]])
        self.assertEqual(len(self.school.records_of("u1")), 2)

    def test_account2_cancel_failure_when_account1_is_complete(self):
        self.school.add("u1", "11031", "144", "11:00", "22:00")
        blocked = self.school.add("u2", "11031", "142", "11:00", "22:00")
        self.school.fail_cancel = {blocked["id"]}
        result = self.run_transfer()
        self.assertEqual(result["mode"], "cancel_source")
        self.assertEqual(result["result"], "partial")
        self.assertEqual([record["id"] for record in self.school.records_of("u2")],
                         [blocked["id"]])

    def test_rebook_timeout_resolved_by_the_school_records(self):
        """An 'unknown' submit is not failure: the record list decides."""
        self.school.add("u2", "11031", "142", "11:00", "22:00")
        original = self.school.submit

        def timed_out_then_landed(client, chunk, roomid, seat, captcha="", timeout=None):
            answer = original(client, chunk, roomid, seat, captcha, timeout)
            if chunk[0] == "11:00":
                return {"success": False, "reason": None, "outcome": "unknown",
                        "message": "ReadTimeout"}
            return answer

        with mock.patch.object(transfer, "submit_chunk",
                               side_effect=timed_out_then_landed):
            result = self.run_transfer()
        self.assertEqual(result["result"], "complete")
        self.assertEqual(result["restored"], [])


class StateTests(TransferTestBase):
    def test_a_completed_run_is_recorded_in_the_state_file(self):
        self.school.add("u2", "11031", "142", "11:00", "22:00")
        self.run_transfer()
        state = json.loads((Path(self.temp.name) / transfer.TRANSFER_STATE)
                           .read_text(encoding="utf-8"))
        self.assertEqual(state["date"], str(DAY))
        self.assertEqual(state["result"], "complete")


class CliTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.config_path = Path(self.temp.name) / "config.local.json"

    def run_main(self, *argv):
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            code = transfer.main(["-u", str(self.config_path), *argv])
        lines = [json.loads(line) for line in output.getvalue().splitlines() if line.strip()]
        return code, lines

    def test_disabled_config_skips_without_network(self):
        self.config_path.write_text(json.dumps({"reserve": [{}, {}]}),
                                    encoding="utf-8")
        code, lines = self.run_main("--check-only")
        self.assertEqual(code, 0)
        self.assertEqual(lines[-1]["result"], "skipped")
        self.assertIn("未启用", lines[-1]["message"])

    def test_invalid_date_is_rejected(self):
        self.config_path.write_text(json.dumps({"reserve": [{}, {}]}),
                                    encoding="utf-8")
        code, lines = self.run_main("--date", "2020-01-01")
        self.assertEqual(code, 1)
        self.assertEqual(lines[-1]["transfer"], "error")

    def test_bad_settings_are_reported(self):
        self.config_path.write_text(json.dumps({
            "reserve": [{}, {}],
            "transferToAccount": {"enabled": True, "from": 1, "to": 1},
        }), encoding="utf-8")
        code, lines = self.run_main("--check-only")
        self.assertEqual(code, 1)
        self.assertEqual(lines[-1]["transfer"], "error")


class TransferTimeTests(unittest.TestCase):
    def test_too_early_run_is_skipped(self):
        now = dt.datetime(2026, 11, 11, 15, 0)
        self.assertFalse(transfer.wait_for_transfer_time({"at": "22:30"},
                                                         now_fn=lambda: now))

    def test_last_minutes_are_waited_out(self):
        slept = []
        now = dt.datetime(2026, 11, 11, 22, 25)
        self.assertTrue(transfer.wait_for_transfer_time(
            {"at": "22:30"}, now_fn=lambda: now, sleep_fn=slept.append))
        self.assertEqual(slept, [300.0])

    def test_time_already_passed_runs_at_once(self):
        now = dt.datetime(2026, 11, 11, 22, 31)
        self.assertTrue(transfer.wait_for_transfer_time({"at": "22:30"},
                                                        now_fn=lambda: now))


@unittest.skipUnless(sys.platform == "win32", "Windows desktop GUI")
class GuiScheduleDirtyTests(unittest.TestCase):
    """The schedule tab must not look dirty just because config changed externally."""

    def _write_config(self, path, weekend_start):
        data = {
            "reserve": [
                {"username": "u1", "password": "p1", "roomid": "11031",
                 "seatid": ["144"], "time": ["10:00", "22:00"],
                 "timesByWeekday": {"Saturday": [weekend_start, "21:00"],
                                    "Sunday": [weekend_start, "21:00"]},
                 "daysofweek": ["Monday"], "backups": []},
                {"username": "u2", "password": "p2", "roomid": "11031",
                 "seatid": ["142"], "time": ["10:00", "22:00"],
                 "timesByWeekday": {"Saturday": [weekend_start, "21:00"],
                                    "Sunday": [weekend_start, "21:00"]},
                 "daysofweek": ["Monday"], "backups": []},
            ],
            "deptIdEnc": "1234567890abcdef", "scheduleAt": "22:00",
        }
        path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")

    def test_switch_works_and_external_change_is_not_dirty(self):
        import desktop_gui

        with tempfile.TemporaryDirectory() as directory:
            config = Path(directory) / "config.local.json"
            self._write_config(config, "11:00")
            prompts = []

            def fake_askyesno(title, message, **_kwargs):
                prompts.append(title)
                return False  # a phantom prompt would then keep us on account 2

            def fake_task_status(**_kwargs):
                return {"state": "Ready", "at": None, "daysOfWeek": 0,
                        "nextRunTime": None, "lastRunTime": None,
                        "lastTaskResult": None}

            with mock.patch.object(desktop_gui, "CONFIG_PATH", config), \
                    mock.patch.object(desktop_gui, "task_status", fake_task_status), \
                    mock.patch.object(desktop_gui.SeatApp, "_load_rooms",
                                      lambda _self: None), \
                    mock.patch.object(desktop_gui.messagebox, "askyesno",
                                      fake_askyesno):
                app = desktop_gui.SeatApp()
                try:
                    app.withdraw()
                    values = app.account_combo["values"]
                    app.account_combo.set(values[1])
                    app._select_account()
                    self.assertEqual(app.account_index, 1)
                    # Change the file outside the window (e.g. weekend window).
                    self._write_config(config, "10:00")
                    self.assertFalse(app._schedule_dirty())
                    app.account_combo.set(values[0])
                    app._select_account()
                    self.assertEqual(app.account_index, 0)
                    self.assertEqual(prompts, [])
                    # A real edit is still detected.
                    app.schedule_seat.set("999")
                    self.assertTrue(app._schedule_dirty())
                finally:
                    app.destroy()


class TransferLogViewTests(unittest.TestCase):
    def test_result_events_are_rendered(self):
        result = {
            "transfer": "result", "date": str(DAY), "at": "22:30:05",
            "mode": "transfer", "result": "complete",
            "message": "账号2的预约已全部转给账号1",
            "from": 2, "to": 1, "window": ["11:00", "22:00"],
            "source": {"index": 2, "verified": "none", "remaining": [],
                       "coverage": [], "records": []},
            "target": {"index": 1, "verified": "complete", "missing": [],
                       "coverage": [["11:00", "15:00"], ["15:00", "22:00"]],
                       "records": [{"time": ["11:00", "15:00"], "seat": "142",
                                    "room": "11031", "id": 21, "status": 0}]},
            "cancelled": [{"account": 2, "id": 7, "time": ["11:00", "22:00"],
                           "seat": "142", "room": "11031"}],
            "booked": [{"account": 1, "time": ["11:00", "15:00"], "seat": "142",
                        "room": "11031", "success": True, "reason": None,
                        "message": ""}],
            "restored": [], "wait_timeout": None,
        }
        raw = "\n".join([json.dumps({"transfer": "start", "date": str(DAY)}),
                         json.dumps(result)])
        text = format_transfer_log(raw, "2026-11-11 22:31")
        self.assertIn("转预约", text)
        self.assertIn("完成", text)
        self.assertIn("账号 2 → 账号 1", text)
        self.assertIn("已取消", text)
        self.assertIn("11:00–15:00", text)
        self.assertIn("记录号 21", text)
        self.assertIn("待履约", text)
        self.assertIn("请在学习通", text)


if __name__ == "__main__":
    unittest.main()
