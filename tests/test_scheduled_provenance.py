"""Exercise capture through the scheduled runner, without live requests."""
import contextlib
import datetime as dt
import io
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import requests
import reserve_segments as runner
import scheduled_provenance as provenance


class ScheduledCaptureTests(unittest.TestCase):
    def test_success_timeout_and_rejected_submission(self):
        day = dt.date.today() + dt.timedelta(days=1)
        tz = dt.timezone(dt.timedelta(hours=8))
        record = {"id": 77, "roomId": "100", "seatNum": "0142", "status": 0,
                  **{key: int(dt.datetime.combine(day, dt.time(hour)).replace(tzinfo=tz).timestamp() * 1000)
                     for key, hour in (("startTime", 11), ("endTime", 15))}}
        params = {"day": str(day), "roomId": "100", "seatNum": "0142",
                  "startTime": "11:00", "endTime": "15:00"}
        user = {"username": "u", "password": "p", "fastSprint": True,
                "skipHolidays": False, "daysofweek": [], "roomid": "100",
                "seatid": ["0142"], "time": ["11:00", "15:00"]}
        for outcome, expected in ((True, True), (requests.exceptions.Timeout(), True),
                                  (False, False)):
            with self.subTest(outcome=outcome), tempfile.TemporaryDirectory() as directory:
                path = Path(directory) / "config.local.json"
                args = SimpleNamespace(user=str(path), today=False, check_only=False,
                    ignore_weekday=True, ignore_holiday=True,
                    roomid=None, seatid=None, start=None, end=None)
                response = mock.Mock()
                response.json.return_value = {"success": outcome}
                session = SimpleNamespace(headers={}, post=mock.Mock(
                    side_effect=outcome if isinstance(outcome, Exception) else None,
                    return_value=response))
                client = SimpleNamespace(requests=session, reserve_next_day=True)

                def race(*unused, **kwargs):
                    try:
                        client.requests.post(url="https://office.chaoxing.com/data/apps/seat/submit",
                                             params=params)
                    except requests.exceptions.Timeout:
                        pass
                    return [(660, 900)]

                with mock.patch.object(runner, "reserve", return_value=client), \
                     mock.patch.object(runner, "trace_http_timings"), \
                     mock.patch.object(runner, "login_with_retry", return_value=(True, "")), \
                     mock.patch.object(runner, "reservation_fid_enc", return_value="fid"), \
                     mock.patch.object(runner, "current_reservations", side_effect=[[], [record]]), \
                     mock.patch.object(runner, "fast_race", side_effect=race), \
                     contextlib.redirect_stdout(io.StringIO()):
                    self.assertEqual(runner.run_account(args, {}, user), 0)
                data = provenance.read(path)
                self.assertEqual(provenance.eligible(data, "u", day, record), expected)


if __name__ == "__main__":
    unittest.main()
