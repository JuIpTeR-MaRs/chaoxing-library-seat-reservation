"""Offline checks for the account-1 fast sprint (token -> submit, verify, retry)."""

import argparse
import contextlib
import datetime as dt
import io
import json
import unittest
from unittest import mock

import requests

import reserve_segments as runner


class FakeResponse:
    def __init__(self, payload):
        self._payload = payload
        self.text = ""

    def json(self):
        return self._payload


class FakeRequests:
    def __init__(self, records=None, verify_fails=False):
        self.records = records
        self.verify_fails = verify_fails
        self.calls = []
        self.headers = {}
        self.post = None

    def get(self, url, params=None, timeout=None):
        self.calls.append({"url": url, "params": params, "timeout": timeout})
        if "/front/apps/seat/select" in url:
            response = FakeResponse({})
            response.text = "fidEnc = 'fid-123'"
            return response
        if "/data/apps/seat/index" in url:
            if self.verify_fails:
                raise requests.exceptions.Timeout("verify slow")
            return FakeResponse({"success": True, "data": {"curReserves": self.records or []}})
        raise AssertionError(url)


class FakeClient:
    """Scripted stand-in for utils.reserve: each submit pops one outcome."""

    def __init__(self, outcomes, records=None, verify_fails=False):
        self.reserve_next_day = True
        self.enable_slider = True
        self.url = "https://office.chaoxing.com/front/third/apps/seat/code?id={}&seatNum={}"
        self.submit_url = "https://office.chaoxing.com/data/apps/seat/submit"
        self.last_submit_payload = {}
        self.submit_attempts = []
        self.outcomes = list(outcomes)
        self.requests = FakeRequests(records, verify_fails)
        self.token_calls = 0
        self.captcha_calls = 0
        self.no_token = False
        self.submits = []
        self.timeouts = []
        self.retry = []

    def _get_page_token(self, url, require_value=False):
        self.token_calls += 1
        if self.no_token:
            return "", ""
        return f"token{self.token_calls}", f"value{self.token_calls}"

    def resolve_captcha(self):
        self.captcha_calls += 1
        return "captcha-1"

    def get_submit(self, url, times, token, roomid, seatid, captcha, action, value, timeout=None):
        self.submits.append({"times": list(times), "token": token, "captcha": captcha,
                             "seat": seatid, "timeout": timeout})
        outcome = self.outcomes.pop(0)
        if outcome == "timeout":
            raise requests.exceptions.Timeout("submit slow")
        if outcome == "malformed":
            raise ValueError("response is not json")
        if outcome.startswith("msg:"):
            payload = {"success": False, "msg": outcome[4:]}
            self.last_submit_payload = payload
            return False
        payload = {"success": outcome == "ok", "msg": "" if outcome == "ok" else outcome}
        if outcome == "ok":
            payload["data"] = {"seatReserve": {"id": len(self.submits)}}
        self.last_submit_payload = payload
        return outcome == "ok"


def plans():
    return [{"plan": 1, "roomid": "11031", "seatid": ["144"],
             "segments": [["10:00", "14:00"], ["14:00", "18:00"], ["18:00", "22:00"]]}]


def run(client, config=None, now=None, sleep=None, **kwargs):
    events = []
    covered = runner.fast_race(
        client, config or {"scheduleAt": "22:00"}, plans(), "10:00", "22:00",
        "2026-11-05", emit=events.append,
        now_fn=now or (lambda: dt.datetime.combine(dt.date.today(), dt.time(21, 59, 58))),
        sleep_fn=sleep or (lambda seconds: None), **kwargs)
    return covered, [json.loads(e) for e in events]


class PatientSubmitTests(unittest.TestCase):
    """Per-account A/B: only the submit wait changes; the final check does not."""

    def test_only_opted_in_account_changes_the_submit_timeout(self):
        self.assertEqual(runner.submit_timeouts({}),
                         (runner.FAST_SUBMIT_TIMEOUT, runner.VERIFY_TIMEOUT))
        self.assertEqual(runner.submit_timeouts({"patientSubmit": True}),
                         (runner.PATIENT_SUBMIT_TIMEOUT, runner.VERIFY_TIMEOUT))
        # Anything that is not exactly True keeps the sprint parameters.
        self.assertEqual(runner.submit_timeouts({"patientSubmit": "yes"}),
                         (runner.FAST_SUBMIT_TIMEOUT, runner.VERIFY_TIMEOUT))

    def test_the_post_race_check_is_never_cut_short(self):
        """A 2s check budget lost a 2.007s answer and reported a booked run as failed."""
        for user in ({}, {"patientSubmit": True}, {"patientSubmit": "yes"}):
            self.assertEqual(runner.submit_timeouts(user)[1], runner.VERIFY_TIMEOUT)
        self.assertGreaterEqual(runner.VERIFY_TIMEOUT, 10.0)

    def test_patient_race_waits_longer_for_submit_and_verify(self):
        client = FakeClient(["ok", "timeout", "ok", "ok"], records=[])
        covered, events = run(client, submit_timeout=runner.PATIENT_SUBMIT_TIMEOUT,
                              verify_timeout=runner.VERIFY_TIMEOUT)
        self.assertEqual({submit["timeout"] for submit in client.submits},
                         {runner.PATIENT_SUBMIT_TIMEOUT})
        index_calls = [call for call in client.requests.calls
                       if "/data/apps/seat/index" in call["url"]]
        self.assertEqual(index_calls[0]["timeout"], runner.VERIFY_TIMEOUT)
        sprint = [e for e in events if e.get("progress") == "fast_sprint"][0]
        self.assertEqual(sprint["submit_timeout"], runner.PATIENT_SUBMIT_TIMEOUT)
        self.assertEqual(sprint["verify_timeout"], runner.VERIFY_TIMEOUT)

    def test_default_race_keeps_sprint_timeouts(self):
        client = FakeClient(["ok", "ok", "ok"])
        events = run(client)[1]
        self.assertEqual({submit["timeout"] for submit in client.submits},
                         {runner.FAST_SUBMIT_TIMEOUT})
        sprint = [e for e in events if e.get("progress") == "fast_sprint"][0]
        self.assertEqual(sprint["submit_timeout"], runner.FAST_SUBMIT_TIMEOUT)
        self.assertEqual(sprint["verify_timeout"], runner.VERIFY_TIMEOUT)


class FastModeTests(unittest.TestCase):
    def test_only_opted_in_account_uses_fast_path(self):
        self.assertTrue(runner.fast_mode({"fastSprint": True}))
        self.assertFalse(runner.fast_mode({}))
        self.assertFalse(runner.fast_mode({"fastSprint": "yes"}))


class FastRaceTests(unittest.TestCase):
    def test_fires_evening_chunks_before_morning_ones(self):
        client = FakeClient(["ok", "ok", "ok"])
        covered, events = run(client)
        self.assertEqual(client.token_calls, 3)
        self.assertEqual(client.captcha_calls, 1)      # one captcha result reused
        self.assertEqual([s["times"] for s in client.submits],
                         [["18:00", "22:00"], ["14:00", "18:00"], ["10:00", "14:00"]])
        self.assertEqual(sorted(covered), [(600, 840), (840, 1080), (1080, 1320)])
        submitting = [e for e in events if e.get("progress") == "submitting"]
        self.assertEqual([e["time"] for e in submitting],
                         [["18:00", "22:00"], ["14:00", "18:00"], ["10:00", "14:00"]])

    def test_timeout_is_verified_and_confirmed_without_retry(self):
        records = [{"id": 7, "startTime": runner.chunk_bounds(["14:00", "18:00"], "2026-11-05")[0],
                    "endTime": runner.chunk_bounds(["14:00", "18:00"], "2026-11-05")[1]}]
        client = FakeClient(["ok", "timeout", "ok"], records=records)
        covered, events = run(client)
        self.assertEqual(client.token_calls, 3)        # no extra token for a retry
        self.assertEqual(client.submits[1]["timeout"], runner.FAST_SUBMIT_TIMEOUT)
        self.assertEqual(sorted(covered), [(600, 840), (840, 1080), (1080, 1320)])
        confirmed = [e for e in events if e.get("message") == "提交超时，核对后确认已生效"]
        self.assertEqual(len(confirmed), 1)
        self.assertTrue(confirmed[0]["success"])
        self.assertEqual(confirmed[0]["elapsed_ms"] >= 0, True)
        self.assertEqual([e for e in events if e.get("verify")], [{"verify": "checked", "deferred": 1}])

    def test_unverified_timeout_retries_once(self):
        client = FakeClient(["timeout", "ok", "ok", "ok"], verify_fails=True)
        covered, events = run(client)
        self.assertEqual(client.token_calls, 4)        # 3 chunks + 1 retry
        self.assertTrue(any(e.get("progress") == "retry_unverified" for e in events))
        self.assertEqual(len(covered), 3)
        self.assertTrue(any(e.get("verify") == "unavailable" for e in events))

    def test_timeout_missing_from_records_is_retried_once(self):
        """A slot the check proved empty gets one more chance after the burst."""
        client = FakeClient(["timeout", "ok", "ok", "ok"], records=[])
        covered, events = run(client)
        self.assertEqual(client.token_calls, 4)
        self.assertEqual(len(covered), 3)
        self.assertTrue(any(e.get("progress") == "retry_missing" for e in events))
        retried = [e for e in events if e.get("time") == ["18:00", "22:00"] and e.get("success")]
        self.assertEqual(len(retried), 1)

    def test_missing_slot_that_is_taken_again_stays_failed(self):
        client = FakeClient(["timeout", "ok", "ok", "msg:该时间段已被占用！"], records=[])
        covered, events = run(client)
        self.assertEqual(client.token_calls, 4)
        self.assertEqual(len(covered), 2)
        self.assertEqual(len([e for e in events
                              if e.get("message") == "提交超时且核对未查到预约"]), 1)
        final = [e for e in events
                 if e.get("time") == ["18:00", "22:00"] and "success" in e][-1]
        self.assertFalse(final["success"])
        self.assertEqual(final.get("reason"), "seat_taken")

    def test_unconfirmed_timeout_is_reported_as_unknown(self):
        client = FakeClient(["timeout", "ok", "ok", "ok"], verify_fails=True)
        _, events = run(client)
        self.assertTrue(any(e.get("outcome") == "unknown" for e in events))

    def test_too_early_for_sprint_falls_back(self):
        clock = dt.datetime.combine(dt.date.today(), dt.time(21, 50))
        client = FakeClient([])
        covered = runner.fast_race(client, {"scheduleAt": "22:00"}, plans(), "10:00", "22:00",
                                  "2026-11-05", emit=lambda _: None, now_fn=lambda: clock,
                                  sleep_fn=lambda _: None)
        self.assertIsNone(covered)
        self.assertEqual(client.submits, [])

    def test_waits_until_opening_before_submitting(self):
        opening = dt.datetime.combine(dt.date.today(), dt.time(22, 0))
        clock = {"now": opening - dt.timedelta(seconds=10)}
        sleeps = []

        def sleep(seconds):
            sleeps.append(round(seconds, 2))
            clock["now"] += dt.timedelta(seconds=seconds)

        client = FakeClient(["ok", "ok", "ok"])
        runner.fast_race(client, {"scheduleAt": "22:00"}, plans(), "10:00", "22:00",
                         "2026-11-05", emit=lambda _: None, now_fn=lambda: clock["now"],
                         sleep_fn=sleep)
        self.assertEqual(sleeps, [7.5, 2.8])           # solve captcha 2.5s early, then fire
        self.assertGreaterEqual(clock["now"], opening + dt.timedelta(seconds=0.3))

    def test_partial_held_does_not_hide_the_morning_gap_and_302_is_retried(self):
        """A timed-out submit can explain "already held"; the rest is still free."""
        client = FakeClient(
            ["ok", "timeout",
             "msg:您在页面停留过久，本次操作安全验证已超时。请刷新后再提交预约(代码:302)",
             "timeout", "msg:当前时间段您已有预约，请选择其他时间段",
             "timeout", "timeout", "ok"],
            verify_fails=True)
        two_plans = [
            {"plan": 1, "roomid": "11031", "seatid": ["144"],
             "segments": [["10:00", "14:00"], ["14:00", "18:00"], ["18:00", "22:00"]]},
            {"plan": 2, "roomid": "11031", "seatid": ["143"],
             "segments": [["11:00", "15:00"], ["15:00", "19:00"], ["19:00", "22:00"]]},
        ]
        events = []
        covered = runner.fast_race(
            client, {"scheduleAt": "22:00"}, two_plans, "10:00", "22:00", "2026-11-05",
            emit=events.append,
            now_fn=lambda: dt.datetime.combine(dt.date.today(), dt.time(21, 59, 58)),
            sleep_fn=lambda _seconds: None)
        logged = [json.loads(event) for event in events]
        self.assertEqual([submit["times"] for submit in client.submits],
                         [["18:00", "22:00"], ["14:00", "18:00"], ["10:00", "14:00"],
                          ["15:00", "18:00"], ["11:00", "15:00"],
                          ["14:00", "18:00"], ["15:00", "18:00"],
                          ["10:00", "14:00"]])
        self.assertTrue(any(event.get("progress") == "retry_rejected" for event in logged))
        self.assertTrue(any(event.get("success") is True and
                            event.get("time") == ["10:00", "14:00"] for event in logged))
        self.assertEqual(sorted(covered), [(600, 840), (1080, 1320)])
        self.assertNotIn((660, 900), covered)     # 11:00-15:00 is not fully held
        self.assertEqual(runner.uncovered_segments("10:00", "22:00", covered),
                         [["14:00", "18:00"]])
        self.assertEqual(client.captcha_calls, 2)  # sprint + post-pass refresh


class VerifyRetryTests(unittest.TestCase):
    """The check can prove a timed-out submit landed; already-held must count."""

    def test_already_held_during_retry_counts_as_coverage(self):
        client = FakeClient(["timeout", "ok", "ok",
                             "msg:当前时间段您已有预约，请选择其他时间段"],
                            verify_fails=True)
        covered, events = run(client)
        self.assertEqual(client.token_calls, 4)
        self.assertEqual(sorted(covered), [(600, 840), (840, 1080), (1080, 1320)])
        held = [e for e in events if e.get("reason") == "already_held"]
        self.assertEqual(len(held), 1)

    def test_one_retry_per_timeslot_across_backup_plans(self):
        two_plans = [
            {"plan": 1, "roomid": "11031", "seatid": ["144"],
             "segments": [["10:00", "14:00"], ["14:00", "18:00"], ["18:00", "22:00"]]},
            {"plan": 2, "roomid": "11031", "seatid": ["143"],
             "segments": [["10:00", "14:00"], ["14:00", "18:00"], ["18:00", "22:00"]]},
        ]
        client = FakeClient(["timeout"] * 6 +
                            ["msg:当前时间段您已有预约，请选择其他时间段"] * 3,
                            verify_fails=True)
        events = []
        covered = runner.fast_race(
            client, {"scheduleAt": "22:00"}, two_plans, "10:00", "22:00", "2026-11-05",
            emit=events.append,
            now_fn=lambda: dt.datetime.combine(dt.date.today(), dt.time(21, 59, 58)),
            sleep_fn=lambda _seconds: None)
        logged = [json.loads(event) for event in events]
        self.assertEqual(len(client.submits), 9)       # 6 sprint + 3 retries
        self.assertEqual(len([e for e in logged
                              if e.get("progress") == "retry_unverified"]), 3)
        self.assertEqual(sorted(covered), [(600, 840), (840, 1080), (1080, 1320)])


class ResilienceTests(unittest.TestCase):
    def test_missing_token_does_not_abort_the_sprint(self):
        client = FakeClient(["ok", "ok", "ok"])
        client.no_token = True
        covered, events = run(client)
        self.assertEqual(covered, [])
        messages = [e.get("message", "") for e in events if "success" in e]
        # Three sprint attempts plus one bounded retry per slot after the check.
        self.assertEqual(len(messages), 6)
        self.assertTrue(all("未提供提交令牌" in message for message in messages))
        self.assertEqual(len([e for e in events
                              if e.get("progress") == "retry_missing"]), 3)

    def test_malformed_submit_response_does_not_abort_the_sprint(self):
        client = FakeClient(["malformed", "ok", "ok", "msg:该时间段已被占用！"], records=[])
        covered, events = run(client)
        self.assertEqual(len(covered), 2)
        self.assertTrue(any("无法解析" in (e.get("message") or "") for e in events))


class ChunkMatchTests(unittest.TestCase):
    def test_chunk_bounds_use_shanghai_time(self):
        start, end = runner.chunk_bounds(["10:00", "14:00"], "2026-11-05")
        self.assertEqual(end - start, 4 * 3600 * 1000)

    def test_partial_overlap_is_not_coverage(self):
        """A neighbouring 13:00-15:00 booking must not count as 14:00-18:00."""
        record = {"startTime": runner.chunk_bounds(["13:00", "15:00"], "2026-11-05")[0],
                  "endTime": runner.chunk_bounds(["13:00", "15:00"], "2026-11-05")[1]}
        self.assertFalse(runner.chunk_landed(["14:00", "18:00"], "2026-11-05", [record]))
        self.assertFalse(runner.chunk_landed(["15:00", "19:00"], "2026-11-05", [record]))

    def test_full_coverage_is_required(self):
        exact = {"startTime": runner.chunk_bounds(["14:00", "18:00"], "2026-11-05")[0],
                 "endTime": runner.chunk_bounds(["14:00", "18:00"], "2026-11-05")[1]}
        self.assertTrue(runner.chunk_landed(["14:00", "18:00"], "2026-11-05", [exact]))
        shorter = {"startTime": runner.chunk_bounds(["14:00", "17:00"], "2026-11-05")[0],
                   "endTime": runner.chunk_bounds(["14:00", "17:00"], "2026-11-05")[1]}
        self.assertFalse(runner.chunk_landed(["14:00", "18:00"], "2026-11-05", [shorter]))

    def test_contiguous_records_cover_a_chunk(self):
        first = {"startTime": runner.chunk_bounds(["14:00", "16:00"], "2026-11-05")[0],
                 "endTime": runner.chunk_bounds(["14:00", "16:00"], "2026-11-05")[1]}
        second = {"startTime": runner.chunk_bounds(["16:00", "18:00"], "2026-11-05")[0],
                  "endTime": runner.chunk_bounds(["16:00", "18:00"], "2026-11-05")[1]}
        self.assertTrue(runner.chunk_landed(["14:00", "18:00"], "2026-11-05", [second, first]))

    def test_unreadable_records_are_ignored(self):
        self.assertFalse(runner.chunk_landed(["14:00", "18:00"], "2026-11-05", None))
        self.assertFalse(runner.chunk_landed(["14:00", "18:00"], "2026-11-05", [{"bad": 1}]))


class ReconcileTests(unittest.TestCase):
    @staticmethod
    def _record(start, end):
        return {"id": 1,
                "startTime": runner.chunk_bounds([start, end], "2026-11-05")[0],
                "endTime": runner.chunk_bounds([start, end], "2026-11-05")[1]}

    def test_slot_reported_as_failed_is_credited_when_records_show_it(self):
        """The 302 above was our own landed submit; the records prove it."""
        records = [self._record("18:00", "22:00"), self._record("14:00", "18:00")]
        client = FakeClient(
            ["msg:您在页面停留过久，本次操作安全验证已超时。请刷新后再提交预约(代码:302)",
             "timeout", "ok"], records=records)
        covered, events = run(client)
        self.assertEqual(len(covered), 3)
        self.assertTrue(any(event.get("verify") == "reconciled" for event in events))
        self.assertEqual(len([event for event in events if event.get("verify") == "reconciled"][0]["added"]), 1)

    def test_held_slot_counts_as_covered_without_retry(self):
        client = FakeClient(["msg:当前时间段您已有预约，请选择其他时间段", "ok", "ok"])
        covered, events = run(client)
        self.assertEqual(len(covered), 3)
        self.assertEqual(client.token_calls, 3)
        held = [event for event in events if event.get("reason") == "already_held"]
        self.assertEqual(len(held), 1)


class RunAccountRoutingTests(unittest.TestCase):
    """Account 1 must use the fast path; other accounts keep the legacy flow."""

    def _run(self, user):
        client = FakeClient(["ok", "ok", "ok", "ok"])
        client.requests.headers = {}
        client.requests.request = None
        client.get_login_status = lambda: None
        client.login = lambda username, password: (True, "")
        client.submit = lambda times, roomid, seatid, action, prepared=None: (
            client.submits.append({"legacy": list(times)}), False)[1]
        args = argparse.Namespace(user="config.json", check_only=False, account=1,
                                  roomid=None, seatid=None, start=None, end=None,
                                  ignore_weekday=True, ignore_holiday=True, today=False)
        config = {"reserve": [user], "scheduleAt": "00:00", "skipHolidays": False,
                  "adjustments": {}}
        with mock.patch.object(runner, "reserve", return_value=client), \
                contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            code = runner.run_account(args, config, user)
        return client, code

    def test_fast_account_submits_without_legacy_submit(self):
        user = {"username": "u1", "password": "p", "roomid": "11031", "seatid": ["144"],
                "time": ["10:00", "22:00"], "daysofweek": ["Monday", "Tuesday", "Wednesday",
                                                           "Thursday", "Friday",
                                                           "Saturday", "Sunday"],
                "fastSprint": True, "backups": []}
        client, code = self._run(user)
        self.assertEqual(code, 0)
        self.assertEqual([s["times"] for s in client.submits],
                         [["18:00", "22:00"], ["14:00", "18:00"], ["10:00", "14:00"]])
        self.assertNotIn("legacy", client.submits[0])
        self.assertEqual({s["timeout"] for s in client.submits},
                         {runner.FAST_SUBMIT_TIMEOUT})

    def test_patient_account_waits_for_each_submit(self):
        user = {"username": "u1", "password": "p", "roomid": "11031", "seatid": ["144"],
                "time": ["10:00", "22:00"], "daysofweek": ["Monday", "Tuesday", "Wednesday",
                                                           "Thursday", "Friday",
                                                           "Saturday", "Sunday"],
                "fastSprint": True, "patientSubmit": True, "backups": []}
        client, code = self._run(user)
        self.assertEqual(code, 0)
        self.assertEqual({s["timeout"] for s in client.submits},
                         {runner.PATIENT_SUBMIT_TIMEOUT})

    def test_other_account_keeps_legacy_flow(self):
        user = {"username": "u2", "password": "p", "roomid": "11031", "seatid": ["142"],
                "time": ["10:00", "22:00"],
                "daysofweek": ["Monday", "Tuesday", "Wednesday", "Thursday",
                               "Friday", "Saturday", "Sunday"], "backups": []}
        client, code = self._run(user)
        self.assertEqual(code, 1)
        self.assertEqual([s.get("legacy") for s in client.submits],
                         [["10:00", "14:00"], ["14:00", "18:00"], ["18:00", "22:00"]])
        self.assertEqual(len(client.outcomes), 4)   # fast-path get_submit never ran


if __name__ == "__main__":
    unittest.main()
