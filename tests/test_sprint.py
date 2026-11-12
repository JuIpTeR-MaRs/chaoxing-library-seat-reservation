"""Offline checks for the pre-opening sprint path."""

import contextlib
import datetime as dt
import io
import unittest
from unittest.mock import patch

import reserve_segments
from utils import reserve


class SprintWindowTests(unittest.TestCase):
    def test_opening_parse(self):
        opening = reserve_segments.sprint_opening({"scheduleAt": "22:00"})
        self.assertEqual(opening.date(), dt.date.today())
        self.assertEqual(opening.strftime("%H:%M"), "22:00")
        self.assertIsNone(reserve_segments.sprint_opening({}))
        self.assertIsNone(reserve_segments.sprint_opening({"scheduleAt": "bad"}))

    def test_window_before_and_after_opening(self):
        opening = dt.datetime.combine(dt.date.today(), dt.time(22, 0))
        self.assertEqual(
            reserve_segments.sprint_window(opening, lambda: opening - dt.timedelta(seconds=30)),
            30.0)
        self.assertIsNone(
            reserve_segments.sprint_window(opening, lambda: opening + dt.timedelta(seconds=1)))
        self.assertIsNone(reserve_segments.sprint_window(None))


class SprintPrepareTests(unittest.TestCase):
    def _client(self, token="t", value="v", captcha="c"):
        client = reserve(reserve_next_day=True, enable_slider=True)
        client._get_page_token = lambda url, require_value=False: (token, value)
        client.resolve_captcha = lambda: captcha
        return client

    @staticmethod
    def _plans():
        return [{"plan": 1, "roomid": "11031", "seatid": ["144"],
                 "segments": [["11:00", "15:00"], ["15:00", "19:00"], ["19:00", "22:00"]]}]

    def test_no_sprint_after_opening(self):
        opening = dt.datetime.combine(dt.date.today(), dt.time(22, 0))
        with contextlib.redirect_stdout(io.StringIO()):
            prepared = reserve_segments.prepare_sprint(
                self._client(), self._plans(), True, "11:00", "22:00",
                {"scheduleAt": "22:00"}, now_fn=lambda: opening + dt.timedelta(seconds=5))
        self.assertIsNone(prepared)

    def test_no_sprint_when_started_far_before_opening(self):
        opening = dt.datetime.combine(dt.date.today(), dt.time(22, 0))
        with contextlib.redirect_stdout(io.StringIO()):
            prepared = reserve_segments.prepare_sprint(
                self._client(), self._plans(), True, "11:00", "22:00",
                {"scheduleAt": "22:00"}, now_fn=lambda: opening - dt.timedelta(minutes=30))
        self.assertIsNone(prepared)

    def test_six_minutes_early_is_beyond_window(self):
        opening = dt.datetime.combine(dt.date.today(), dt.time(22, 0))
        with contextlib.redirect_stdout(io.StringIO()):
            prepared = reserve_segments.prepare_sprint(
                self._client(), self._plans(), True, "11:00", "22:00",
                {"scheduleAt": "22:00"}, now_fn=lambda: opening - dt.timedelta(minutes=6))
        self.assertIsNone(prepared)

    def test_three_minutes_early_still_sprints(self):
        opening = dt.datetime.combine(dt.date.today(), dt.time(22, 0))
        clock = {"now": opening - dt.timedelta(minutes=3)}
        sleeps = []

        def now():
            return clock["now"]

        def fake_sleep(seconds):
            clock["now"] += dt.timedelta(seconds=seconds)
            sleeps.append(seconds)

        with patch.object(reserve_segments.time, "sleep", side_effect=fake_sleep), \
                contextlib.redirect_stdout(io.StringIO()):
            prepared = reserve_segments.prepare_sprint(
                self._client(), self._plans(), True, "11:00", "22:00",
                {"scheduleAt": "22:00"}, now_fn=now)
        self.assertIsNotNone(prepared)
        self.assertEqual(sleeps[0], 176.0)
        self.assertGreaterEqual(clock["now"], opening + dt.timedelta(seconds=0.3))

    def test_prepares_package_and_times_sleeps(self):
        opening = dt.datetime.combine(dt.date.today(), dt.time(22, 0))
        clock = {"now": opening - dt.timedelta(seconds=30)}
        sleeps = []

        def now():
            return clock["now"]

        def fake_sleep(seconds):
            clock["now"] += dt.timedelta(seconds=seconds)
            sleeps.append(seconds)

        with patch.object(reserve_segments.time, "sleep", side_effect=fake_sleep), \
                contextlib.redirect_stdout(io.StringIO()):
            prepared = reserve_segments.prepare_sprint(
                self._client(), self._plans(), True, "11:00", "22:00",
                {"scheduleAt": "22:00"}, now_fn=now)
        self.assertIsNotNone(prepared)
        self.assertEqual([prepared["start"], prepared["end"]], ["15:00", "19:00"])
        self.assertEqual(prepared["seat"], "144")
        self.assertEqual(prepared["roomid"], "11031")
        self.assertEqual(prepared["token"], "t")
        self.assertEqual(prepared["value"], "v")
        self.assertEqual(prepared["captcha"], "c")
        self.assertFalse(prepared["used"])
        self.assertEqual(sleeps, [26.0, 1.5, 2.8])
        self.assertGreaterEqual(clock["now"], opening + dt.timedelta(seconds=0.3))

    def test_no_sprint_without_page_token(self):
        opening = dt.datetime.combine(dt.date.today(), dt.time(22, 0))
        with patch.object(reserve_segments.time, "sleep"), \
                contextlib.redirect_stdout(io.StringIO()):
            prepared = reserve_segments.prepare_sprint(
                self._client(token="", value=""), self._plans(), True, "11:00", "22:00",
                {"scheduleAt": "22:00"}, now_fn=lambda: opening - dt.timedelta(seconds=30))
        self.assertIsNone(prepared)


class SubmitPreparedTests(unittest.TestCase):
    def _client(self):
        client = reserve(reserve_next_day=True, enable_slider=True)
        client.max_attempt = 2
        client.sleep_time = 0
        client.calls = {"token": 0, "captcha": 0, "submit": []}

        def fake_token(url, require_value=False):
            client.calls["token"] += 1
            return "t", "v"

        def fake_captcha():
            client.calls["captcha"] += 1
            return "c"

        def fake_submit(url, times, token, roomid, seatid, captcha, action, value):
            client.calls["submit"].append({"token": token, "captcha": captcha})
            return len(client.calls["submit"]) >= 2

        client._get_page_token = fake_token
        client.resolve_captcha = fake_captcha
        client.get_submit = fake_submit
        client.url = "https://office.chaoxing.com/front/third/apps/seat/code?id={}&seatNum={}"
        return client

    def test_first_attempt_uses_prepared_second_refetches(self):
        client = self._client()
        prepared = {"start": "15:00", "end": "19:00", "seat": "144",
                    "token": "tok", "value": "val", "captcha": "cap", "used": False}
        with contextlib.redirect_stdout(io.StringIO()):
            ok = client.submit(["15:00", "19:00"], "11031", ["144"], False,
                               prepared=prepared)
        self.assertTrue(ok)
        self.assertTrue(prepared["used"])
        self.assertEqual(client.calls["submit"][0], {"token": "tok", "captcha": "cap"})
        self.assertEqual(client.calls["submit"][1], {"token": "t", "captcha": "c"})
        self.assertEqual(client.calls["token"], 1)
        self.assertEqual(client.calls["captcha"], 1)

    def test_without_prepared_fetches_page_token_first(self):
        client = self._client()
        with contextlib.redirect_stdout(io.StringIO()):
            ok = client.submit(["15:00", "19:00"], "11031", ["144"], False)
        self.assertTrue(ok)
        self.assertEqual(client.calls["token"], 2)
        self.assertEqual(client.calls["submit"][0], {"token": "t", "captcha": "c"})

    def test_mismatched_prepared_is_ignored(self):
        client = self._client()
        prepared = {"start": "11:00", "end": "15:00", "seat": "999",
                    "token": "tok", "value": "val", "captcha": "cap", "used": False}
        with contextlib.redirect_stdout(io.StringIO()):
            ok = client.submit(["15:00", "19:00"], "11031", ["144"], False,
                               prepared=prepared)
        self.assertTrue(ok)
        self.assertFalse(prepared["used"])
        self.assertEqual(client.calls["submit"][0], {"token": "t", "captcha": "c"})


if __name__ == "__main__":
    unittest.main()
