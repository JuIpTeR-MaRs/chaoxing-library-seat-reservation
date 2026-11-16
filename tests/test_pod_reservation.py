import datetime as dt
import json
import unittest
from unittest import mock

import pod_reservation as pod


class PodTests(unittest.TestCase):
    def test_runner_respects_custom_time_and_weekday(self):
        settings = dict(self.settings(), enabled=True, scheduleAt="22:30", daysofweek=["Monday"])
        config = {"reserve": [dict(username="user", password="pass", pod=settings)]}
        for day, expected in [(16, True), (17, False)]:
            with self.subTest(day=day), mock.patch("sys.argv", ["pod_reservation.py"]), mock.patch.object(pod.Path, "read_text", return_value=json.dumps(config)), mock.patch.object(pod.dt, "datetime", wraps=dt.datetime) as clock, mock.patch.object(pod, "PodClient") as client, mock.patch("pod_team.book_with_accounts", return_value="ok") as book:
                clock.now.return_value = dt.datetime(2026, 11, day, 22, 30, tzinfo=pod.TZ)
                self.assertEqual(pod.main(), 0)
                self.assertEqual(book.called, expected)
                self.assertEqual(client.called, expected)
                if expected:
                    self.assertEqual(book.call_args.args[4], "2026-11-17")

    def test_schedule_legacy_defaults_and_invalid_values(self):
        self.assertEqual(pod.validate_schedule({}), ("22:00", list(pod.WEEKDAYS)))
        for settings in [dict(scheduleAt="21:59"), dict(scheduleAt="24:00"), dict(daysofweek=[]), dict(daysofweek=["invalid"])]:
            with self.assertRaises(ValueError):
                pod.validate_schedule(settings)

    def test_cancel_rechecks_eligibility_and_team_scope(self):
        client = self.client()
        row = dict(id=42, status=0, violation=-1, teamLeaderCancel=1, leader=1)
        client.records = mock.Mock(return_value=[row])
        client.request = mock.Mock(return_value={})
        with self.assertRaisesRegex(RuntimeError, "范围已变化"):
            client.cancel("42")
        client.request.assert_not_called()
        client.cancel("42", include_team=True)
        client.request.assert_called_once_with("item/user/cancel", {"id": "42", "allCancel": 1}, post=True)
        client.request.reset_mock()
        for change in (dict(status=7), dict(violation=1), dict(aprvStatusTypeId=-1)):
            client.records.return_value = [dict(row, **change)]
            with self.assertRaises(RuntimeError):
                client.cancel("42", include_team=True)
        client.request.assert_not_called()

    def test_single_cancel_and_missing_record(self):
        client = self.client()
        client.records = mock.Mock(return_value=[dict(id=42, status=0, violation=-1)])
        client.request = mock.Mock(return_value={})
        client.cancel("42")
        client.request.assert_called_once_with("item/user/cancel", {"id": "42", "allCancel": 0}, post=True)
        client.request.reset_mock()
        client.records.return_value = []
        with self.assertRaises(RuntimeError):
            client.cancel("42")
        client.request.assert_not_called()

    def settings(self, **extra):
        return dict({"itemId": "38541", "start": "16:00", "remark": "练习英语口语"}, **extra)

    def client(self):
        client = pod.PodClient.__new__(pod.PodClient)
        client.reserve_id, client.fid, client.enc = "15133", "school", "page-enc"
        return client

    def test_plan_requires_known_pod_whole_hour_and_real_reason(self):
        self.assertEqual(pod.validate_plan(self.settings())[:2], ("16:00", "17:00"))
        for key, value in [("itemId", "unknown"), ("start", "16:30"), ("start", "23:00"), ("remark", "  "), ("remark", "字"*301), ("remark", "😀"*151)]:
            settings = self.settings()
            settings[key] = value
            with self.subTest(key=key, value=str(value)[:12]), self.assertRaises(ValueError):
                pod.validate_plan(settings)

    def test_entry_rejects_other_hosts_and_schools(self):
        for url in ["http://reserve.chaoxing.com/?reserveId=15133", "https://example.com/", "https://reserve.chaoxing.com/?reserveId=999&fidEnc=test"]:
            with self.assertRaises(ValueError):
                pod.parse_entry(url)

    def test_query_uses_school_intervals_and_detects_partial_overlap(self):
        client = self.client()
        day = str(dt.datetime.now(pod.TZ).date() + dt.timedelta(days=1))
        begin, end = pod.date_bounds(day, "16:00", "17:00")
        client.request = mock.Mock(side_effect=[
            {"reserveItemData": {"isSpecial": 1, "multiReserve": 0, "timeModel": 1},
             "reserveData": {"multiReserve": 2, "timeModel": 1}},
            {"commonIntervalMap": {str(begin.isoweekday()): [
                {"id": 42, "startTime": "16:00", "endTime": "17:00", "available": 1, "userNum": 1},
                {"id": 43, "startTime": "17:00", "endTime": "18:00", "available": 1, "userNum": 1}]}},
            {"reserveUserList": [{"starttime": int((begin+dt.timedelta(minutes=30)).timestamp()*1000), "endtime": int(end.timestamp()*1000)}]}])
        slots = client.slots("38541", day)
        self.assertFalse(slots[0]["free"])
        self.assertTrue(slots[1]["free"])

    def test_query_uses_effective_booking_mode(self):
        for special, item_mode, global_mode, allowed in [
            (1, 0, 2, True), (1, 2, 0, False),
            (0, 0, 2, False), (0, 2, 0, True),
        ]:
            with self.subTest(special=special, item_mode=item_mode, global_mode=global_mode):
                client = self.client()
                client.request = mock.Mock(side_effect=[
                    {"reserveItemData": {"isSpecial": special, "multiReserve": item_mode, "timeModel": 1},
                     "reserveData": {"multiReserve": global_mode, "timeModel": 1}},
                    {"commonIntervalMap": {}}, {"reserveUserList": []}])
                if allowed:
                    self.assertEqual(client.slots("38541", "2026-11-15"), [])
                else:
                    with self.assertRaisesRegex(RuntimeError, "组队"):
                        client.slots("38541", "2026-11-15")
                    self.assertEqual(client.request.call_count, 1)

    def test_changed_form_flow_refuses_booking(self):
        client = self.client()
        client.request = mock.Mock(return_value={"reserveItemData": {"timeModel": 1, "isForms": 1}, "reserveData": {}})
        with self.assertRaisesRegex(RuntimeError, "流程已变化"):
            client.slots("38541", "2026-11-15")

    def test_timeout_verifies_success_without_repeated_submit(self):
        client = self.client()
        # Use a future date with a frozen clock so release/date validation is exercised.
        now = dt.datetime(2026, 11, 14, 22, 0, tzinfo=pod.TZ)
        client.matching = mock.Mock(side_effect=[[], [{"id": 123}]])
        client.slots = mock.Mock(return_value=[{"id": 42, "start": "16:00", "end": "17:00", "free": True}])
        client.request = mock.Mock(side_effect=[{"reserveNum": 1}, TimeoutError("timeout")])
        with mock.patch.object(pod.dt, "datetime", wraps=dt.datetime) as clock, mock.patch.object(pod.time, "sleep"):
            clock.now.return_value = now
            result = client.book(self.settings(), "2026-11-15")
        self.assertIn("预约成功", result)
        self.assertEqual(client.request.call_count, 2)
        path, payload = client.request.call_args.args
        self.assertEqual(path, "multiple/submit")
        self.assertEqual(json.loads(json.loads(payload["reserveUsers"])[0]["intervalIds"]), [42])
        self.assertEqual(json.loads(payload["reserveUsers"])[0]["remark"], "练习英语口语")

    def test_duplicate_record_never_posts(self):
        client = self.client()
        client.matching = mock.Mock(return_value=[{"id": 123}])
        client.request = mock.Mock()
        with mock.patch.object(pod.dt, "datetime", wraps=dt.datetime) as clock:
            clock.now.return_value = dt.datetime(2026, 11, 14, 22, tzinfo=pod.TZ)
            self.assertIn("未重复提交", client.book(self.settings(), "2026-11-15"))
        client.request.assert_not_called()

    def test_before_release_never_posts(self):
        client = self.client()
        client.request = mock.Mock()
        with mock.patch.object(pod.dt, "datetime", wraps=dt.datetime) as clock:
            clock.now.return_value = dt.datetime(2026, 11, 14, 21, 59, tzinfo=pod.TZ)
            with self.assertRaisesRegex(ValueError, "尚未开放"):
                client.book(self.settings(), "2026-11-15")
        client.request.assert_not_called()


if __name__ == "__main__":
    unittest.main()
