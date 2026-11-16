"""Offline checks for retrying a transient login-page redirect loop."""

import contextlib
import datetime as dt
import io
import json
import unittest
from unittest import mock

import requests

import reserve_segments as runner
import transfer_seats

TODAY = dt.date.today()


def at(hour, minute, second=0):
    return dt.datetime.combine(TODAY, dt.time(hour, minute, second))


class Clock:
    """Deterministic clock: sleeping advances the wall time."""

    def __init__(self, start):
        self.now = start

    def __call__(self):
        return self.now

    def sleep(self, seconds):
        self.now += dt.timedelta(seconds=seconds)


class FakeSession:
    def __init__(self):
        self.headers = {}
        self.closed = False

    def close(self):
        self.closed = True


class FakeClient:
    def __init__(self, page_failures=(), login_failures=(), login_result=(True, "")):
        self.requests = FakeSession()
        self.page_failures = list(page_failures)
        self.login_failures = list(login_failures)
        self.login_result = login_result
        self.page_calls = 0
        self.login_calls = 0

    def get_login_status(self):
        self.page_calls += 1
        if self.page_failures:
            raise self.page_failures.pop(0)

    def login(self, _username, _password):
        self.login_calls += 1
        if self.login_failures:
            raise self.login_failures.pop(0)
        return self.login_result


class FakeResponse:
    def __init__(self, status, url, location=None):
        self.status_code = status
        self.url = url
        self.headers = {"Location": location} if location else {}


def redirect_loop(hops=30):
    history = [FakeResponse(302, f"https://passport2.chaoxing.com/mlogin?i={index}")
               for index in range(hops)]
    response = FakeResponse(302, "https://passport2.chaoxing.com/mlogin?i=30")
    response.history = history
    return requests.exceptions.TooManyRedirects("Exceeded 30 redirects.", response=response)


def run_login(client, clock=None, sessions=None, fast_sprint=False):
    sessions = sessions if sessions is not None else []

    def factory():
        session = FakeSession()
        sessions.append(session)
        return session

    output = io.StringIO()
    with contextlib.redirect_stdout(output):
        result = runner.login_with_retry(
            client, {"username": "u", "password": "p"}, {"scheduleAt": "22:00"},
            fast_sprint=fast_sprint, now_fn=clock, sleep_fn=clock.sleep,
            session_factory=factory)
    events = [json.loads(line) for line in output.getvalue().splitlines() if line]
    return result, events, sessions


class DeadlineTests(unittest.TestCase):
    def test_near_the_opening_the_window_ends_shortly_after_it(self):
        now = at(21, 59, 6)
        deadline = runner.login_retry_deadline({"scheduleAt": "22:00"}, now_fn=lambda: now)
        self.assertEqual(deadline, at(22, 0, 30))

    def test_a_run_far_from_the_opening_is_capped(self):
        now = at(20, 0, 0)
        deadline = runner.login_retry_deadline({"scheduleAt": "22:00"}, now_fn=lambda: now)
        self.assertEqual(deadline, now + dt.timedelta(seconds=90))

    def test_a_run_after_the_opening_still_gets_a_full_window(self):
        now = at(22, 5, 0)
        deadline = runner.login_retry_deadline({"scheduleAt": "22:00"}, now_fn=lambda: now)
        self.assertEqual(deadline, now + dt.timedelta(seconds=90))


class LoginRetryTests(unittest.TestCase):
    def test_login_page_exhausting_budget_does_not_post_credentials(self):
        clock = Clock(at(22, 0, 29))
        client = FakeClient()
        def page():
            client.page_calls += 1
            clock.sleep(2)
        client.get_login_status = page
        with contextlib.redirect_stdout(io.StringIO()), self.assertRaises(requests.exceptions.Timeout):
            run_login(client, clock)
        self.assertEqual(client.login_calls, 0)

    def test_transfer_login_uses_shared_retry_with_short_budget(self):
        client = FakeClient()
        before = dt.datetime.now()
        with mock.patch.object(transfer_seats, "reserve", return_value=client), \
                mock.patch.object(transfer_seats, "login_with_retry", return_value=(True, "")) as login:
            self.assertIs(transfer_seats._login_client("u", "p", TODAY), client)
        self.assertEqual(login.call_args.args[1], {"username": "u", "password": "p"})
        budget = (login.call_args.kwargs["deadline"] - before).total_seconds()
        self.assertGreater(budget, 29)
        self.assertLess(budget, 31)

    def test_expired_deadline_never_starts_a_request(self):
        clock = Clock(at(22, 0, 30))
        client = FakeClient()
        with contextlib.redirect_stdout(io.StringIO()), self.assertRaises(requests.exceptions.Timeout):
            runner.login_with_retry(client, {}, {}, deadline=clock.now,
                                    now_fn=clock, sleep_fn=clock.sleep)
        self.assertEqual(client.page_calls, 0)

    def test_sleep_to_deadline_does_not_start_another_attempt(self):
        clock = Clock(at(22, 0, 29))
        client = FakeClient(page_failures=[redirect_loop()])
        with contextlib.redirect_stdout(io.StringIO()), self.assertRaises(requests.exceptions.TooManyRedirects):
            run_login(client, clock)
        self.assertEqual(client.page_calls, 1)

    def test_replaced_session_is_closed(self):
        client = FakeClient(page_failures=[redirect_loop()])
        original = client.requests
        run_login(client, Clock(at(21, 59, 20)))
        self.assertTrue(original.closed)

    def test_redirect_hops_share_budget_and_restore_session(self):
        session = requests.Session()
        original_send = mock.Mock(return_value="ok")
        session.send = original_send
        session.temporary_retry_attempts = 5
        with mock.patch.object(runner.time, "monotonic", side_effect=[100, 101, 105]):
            with runner._login_request_budget(session, 5):
                self.assertEqual(session.temporary_retry_attempts, 1)
                self.assertEqual(session.send(object()), "ok")
                self.assertEqual(original_send.call_args.kwargs["timeout"].total, 4)
                with self.assertRaises(requests.exceptions.Timeout):
                    session.send(object())
        self.assertIs(session.send, original_send)
        self.assertEqual(session.temporary_retry_attempts, 5)

    def test_redirect_loop_is_retried_with_a_clean_session(self):
        clock = Clock(at(21, 59, 20))
        client = FakeClient(page_failures=[redirect_loop()])
        result, events, sessions = run_login(client, clock, fast_sprint=True)

        self.assertEqual(result, (True, ""))
        self.assertEqual(client.page_calls, 2)
        self.assertEqual(client.login_calls, 1)
        self.assertEqual(len(sessions), 1)
        self.assertIs(client.requests, sessions[0])
        self.assertEqual(sessions[0].temporary_retry_attempts, 1)

        retry = next(event for event in events if event.get("progress") == "login_retry")
        self.assertEqual(retry["attempt"], 1)
        self.assertEqual(retry["error"], "TooManyRedirects")
        self.assertEqual(len(retry["redirects"]), 5)
        self.assertIn("302 https://passport2.chaoxing.com/mlogin?i=", retry["redirects"][0])

    def test_a_redirect_loop_on_the_login_post_is_also_retried(self):
        clock = Clock(at(21, 59, 20))
        client = FakeClient(login_failures=[redirect_loop()])
        result, events, sessions = run_login(client, clock)

        self.assertEqual(result, (True, ""))
        self.assertEqual(client.page_calls, 2)
        self.assertEqual(client.login_calls, 2)
        self.assertEqual(len(sessions), 1)
        self.assertFalse(hasattr(sessions[0], "temporary_retry_attempts"))

    def test_other_temporary_http_failures_are_retried(self):
        clock = Clock(at(21, 59, 20))
        client = FakeClient(page_failures=[requests.exceptions.ConnectionError("proxy down")])
        result, events, _sessions = run_login(client, clock)

        self.assertEqual(result, (True, ""))
        self.assertEqual(client.page_calls, 2)
        self.assertEqual(events[0]["progress"], "login_retry")
        self.assertEqual(events[0]["error"], "ConnectionError")

    def test_a_wrong_password_is_never_retried(self):
        clock = Clock(at(21, 59, 20))
        client = FakeClient(login_result=(False, "密码错误"))
        result, events, sessions = run_login(client, clock)

        self.assertEqual(result, (False, "密码错误"))
        self.assertEqual(client.page_calls, 1)
        self.assertEqual(client.login_calls, 1)
        self.assertEqual(sessions, [])
        self.assertEqual([event for event in events if event.get("progress") == "login_retry"], [])

    def test_persistent_redirect_loop_stops_at_the_deadline(self):
        clock = Clock(at(21, 59, 6))
        client = FakeClient(page_failures=[redirect_loop() for _ in range(200)])
        output = io.StringIO()

        with contextlib.redirect_stdout(output):
            with self.assertRaises(requests.exceptions.TooManyRedirects):
                runner.login_with_retry(
                    client, {"username": "u", "password": "p"}, {"scheduleAt": "22:00"},
                    now_fn=clock, sleep_fn=clock.sleep, session_factory=FakeSession)

        events = [json.loads(line) for line in output.getvalue().splitlines() if line]
        self.assertGreater(client.page_calls, 5)
        self.assertGreaterEqual(clock.now, at(22, 0, 30))
        failure = next(event for event in reversed(events) if event.get("login_ok") is False)
        self.assertIn("TooManyRedirects", failure["message"])
        self.assertEqual(len([event for event in events
                              if event.get("progress") == "login_retry"]),
                         client.page_calls)


if __name__ == "__main__":
    unittest.main()
