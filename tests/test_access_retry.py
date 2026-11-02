"""Offline checks for temporary access failures in scheduled reservations."""

import contextlib
import io
import json
import unittest
from unittest.mock import patch

import requests

import reserve_segments


class FakeSession:
    def __init__(self, outcomes):
        self.outcomes = iter(outcomes)
        self.calls = 0

    def request(self, _method, _url, **_kwargs):
        self.calls += 1
        outcome = next(self.outcomes)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


class FakeResponse:
    def __init__(self, status):
        self.status_code = status

    def raise_for_status(self):
        raise requests.exceptions.HTTPError(str(self.status_code))


class AccessRetryTests(unittest.TestCase):
    def test_persistent_timeout_stops_after_five_attempts(self):
        session = FakeSession([requests.exceptions.Timeout() for _ in range(6)])
        with patch.object(reserve_segments.time, "sleep"), contextlib.redirect_stdout(io.StringIO()):
            reserve_segments.trace_http_timings(session)
            with self.assertRaises(requests.exceptions.Timeout):
                session.request("POST", "https://example.com/submit")
        self.assertEqual(session.calls, 5)

    def test_connection_timeout_and_busy_response_retry_until_response(self):
        session = FakeSession([
            requests.exceptions.ConnectionError(),
            requests.exceptions.Timeout(),
            FakeResponse(503),
            FakeResponse(200),
        ])
        output = io.StringIO()
        with patch.object(reserve_segments.time, "sleep"), contextlib.redirect_stdout(output):
            reserve_segments.trace_http_timings(session)
            result = session.request("GET", "https://example.com/test")
        self.assertEqual(result.status_code, 200)
        self.assertEqual(session.calls, 4)
        self.assertEqual(sum("retry" in json.loads(line) for line in output.getvalue().splitlines()), 3)

    def test_definitive_client_response_is_returned_without_retry(self):
        session = FakeSession([FakeResponse(400)])
        with contextlib.redirect_stdout(io.StringIO()):
            reserve_segments.trace_http_timings(session)
            result = session.request("POST", "https://example.com/submit")
        self.assertEqual(result.status_code, 400)
        self.assertEqual(session.calls, 1)


if __name__ == "__main__":
    unittest.main()
