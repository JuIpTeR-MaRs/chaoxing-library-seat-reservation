"""Reserve consecutive seat slots of at most four hours each."""

import argparse
from contextlib import contextmanager
import datetime as dt
import json
import os
import sys
import time
from pathlib import Path
from urllib.parse import urlsplit
import requests

from utils import holidays, reserve

if os.name == "nt":
    import msvcrt


@contextmanager
def scheduled_account_guard(config_path, target_day, username):
    """Serialize scheduled accounts and keep the first successful account for a day."""
    lock_path = Path(config_path).with_name("scheduled-account.lock")
    state_path = Path(config_path).with_name("scheduled-account-state.json")
    with lock_path.open("a+b") as lock:
        if os.name == "nt":
            # Windows permits locking beyond EOF; never write a byte another process locks.
            while True:
                try:
                    lock.seek(0)
                    msvcrt.locking(lock.fileno(), msvcrt.LK_LOCK, 1)
                    break
                except OSError:
                    time.sleep(0.1)
        try:
            try:
                state = json.loads(state_path.read_text(encoding="utf-8"))
            except FileNotFoundError:
                state = {}
            if not isinstance(state, dict):
                raise ValueError("Scheduled account state is invalid")

            def mark_success():
                temp_path = state_path.with_suffix(".tmp")
                temp_path.write_text(json.dumps({"date": str(target_day), "winner": username}), encoding="utf-8")
                os.replace(temp_path, state_path)

            yield state.get("date") == str(target_day) and bool(state.get("winner")), mark_success
        finally:
            if os.name == "nt":
                lock.seek(0)
                msvcrt.locking(lock.fileno(), msvcrt.LK_UNLCK, 1)

CLASSIC_ROOM_ID = "11031"
AFTERNOON_START = "15:00"
CLASSIC_EVENING_FIRST_KEY = "classicEveningFirst"


def build_segments(start_text, end_text):
    start = dt.datetime.strptime(start_text, "%H:%M")
    end = dt.datetime.strptime(end_text, "%H:%M")
    if end <= start:
        raise ValueError("End time must be after start time on the same day")
    segments = []
    while start < end:
        next_end = min(start + dt.timedelta(hours=4), end)
        segments.append([start.strftime("%H:%M"), next_end.strftime("%H:%M")])
        start = next_end
    return segments


def reservation_plans(user, overrides=None):
    """Return the primary plan followed by ordered alternatives."""
    overrides = overrides or {}
    raw_plans = [user] if overrides else [user, *user.get("backups", [])]
    plans = []
    for index, item in enumerate(raw_plans):
        roomid = str(overrides.get("roomid") or item["roomid"]).strip()
        seatids = ([str(overrides["seatid"])] if overrides.get("seatid")
                   else [str(value).strip() for value in item["seatid"]])
        times = item["time"]
        start = overrides.get("start") or times[0]
        end = overrides.get("end") or times[1]
        if not roomid.isdecimal() or not seatids or any(not seat.isdecimal() or int(seat) < 1 for seat in seatids):
            raise ValueError(f"Invalid room or seat in plan {index + 1}")
        plans.append({
            "plan": index + 1,
            "roomid": roomid,
            "seatid": seatids,
            "segments": build_segments(start, end),
        })
    return plans


def time_minutes(value):
    parsed = dt.datetime.strptime(value, "%H:%M")
    return parsed.hour * 60 + parsed.minute


def uncovered_segments(start, end, covered):
    """Split the unreserved part of one time window into slots up to four hours."""
    beginning, ending = time_minutes(start), time_minutes(end)
    cursor = beginning
    gaps = []
    for booked_start, booked_end in sorted(covered):
        if booked_end <= cursor or booked_start >= ending:
            continue
        if booked_start > cursor:
            gaps.append((cursor, min(booked_start, ending)))
        cursor = max(cursor, min(booked_end, ending))
        if cursor >= ending:
            break
    if cursor < ending:
        gaps.append((cursor, ending))
    return [segment for gap_start, gap_end in gaps
            for segment in build_segments(f"{gap_start // 60:02d}:{gap_start % 60:02d}",
                                          f"{gap_end // 60:02d}:{gap_end % 60:02d}")]


def ordered_uncovered_segments(start, end, covered, evening_first=False):
    """Prioritize slots from 15:00 onward without changing the target window."""
    if not evening_first or time_minutes(end) <= time_minutes(AFTERNOON_START):
        return uncovered_segments(start, end, covered)
    later_start = max(start, AFTERNOON_START)
    later = uncovered_segments(later_start, end, covered)
    earlier = (uncovered_segments(start, AFTERNOON_START, covered)
               if time_minutes(start) < time_minutes(AFTERNOON_START) else [])
    return later + earlier


def adjustment_decision(config, target_day):
    """Return the saved per-date decision for one date: "reserve", "skip" or None."""
    decisions = config.get("adjustments") or {}
    if not isinstance(decisions, dict):
        return None
    value = decisions.get(str(target_day))
    return value if value in ("reserve", "skip") else None


def trace_http_timings(session):
    """Retry temporary access failures and log safe request durations."""
    if not hasattr(session, "request"):
        return
    original_request = session.request

    def timed_request(method, url, *args, **kwargs):
        deadline = time.monotonic() + 60
        kwargs.setdefault("timeout", 10)
        attempt = 0
        while True:
            attempt += 1
            started = time.perf_counter()
            response = None
            error_type = None
            try:
                response = original_request(method, url, *args, **kwargs)
                if response.status_code not in (429, 500, 502, 503, 504):
                    return response
                if attempt >= 5 or time.monotonic() >= deadline:
                    response.raise_for_status()
            except (requests.exceptions.ConnectionError, requests.exceptions.Timeout) as error:
                error_type = type(error).__name__
                if attempt >= 5 or time.monotonic() >= deadline:
                    raise
            finally:
                parsed = urlsplit(url)
                entry = {"timing": "http", "method": method.upper(),
                         "host": parsed.hostname, "path": parsed.path,
                         "elapsed_ms": round((time.perf_counter() - started) * 1000)}
                if response is not None:
                    entry["status"] = response.status_code
                if error_type:
                    entry["error"] = error_type
                print(json.dumps(entry, ensure_ascii=True), flush=True)
            print(json.dumps({"retry": "temporary access failure", "attempt": attempt},
                             ensure_ascii=True), flush=True)
            time.sleep(min(0.5 * attempt, 3, max(0, deadline - time.monotonic())))

    session.request = timed_request


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("-u", "--user", required=True)
    parser.add_argument("--check-only", action="store_true")
    parser.add_argument("--account", type=int)
    parser.add_argument("--roomid")
    parser.add_argument("--seatid")
    parser.add_argument("--start")
    parser.add_argument("--end")
    parser.add_argument("--ignore-weekday", action="store_true")
    parser.add_argument("--ignore-holiday", action="store_true")
    parser.add_argument("--today", action="store_true", help="Book today instead of tomorrow for a one-off run")
    args = parser.parse_args()

    with open(args.user, encoding="utf-8") as file:
        config = json.load(file)
    users = config["reserve"]
    if not users:
        raise ValueError("Configure at least one account")
    if args.account is not None and not 1 <= args.account <= len(users):
        raise ValueError("Account number is out of range")
    ready = [(index, user) for index, user in enumerate(users, start=1)
             if args.account is None or index == args.account]
    ready = [(index, user) for index, user in ready
             if user.get("scheduleEnabled", True) and
             str(user.get("roomid", "")).isdecimal() and user.get("seatid")]
    if not ready:
        raise ValueError("No account has a saved reservation plan")
    if len({user.get("username") for _, user in ready}) != len(ready):
        raise ValueError("Account usernames must be unique")
    results = []
    for index, user in ready:
        if len(users) > 1:
            print(json.dumps({"account": index, "username": user.get("username", "")}, ensure_ascii=True), flush=True)
        try:
            if (args.account is not None and not args.check_only and
                    config.get("autoCancelOtherOnSuccess") is True and len(users) == 2):
                target_day = dt.date.today() + dt.timedelta(days=0 if args.today else 1)
                with scheduled_account_guard(args.user, target_day, user["username"]) as (other_won, mark_success):
                    if other_won:
                        print(json.dumps({"date": str(target_day), "skipped": "other account succeeded"}), flush=True)
                        results.append(0)
                    else:
                        results.append(run_account(args, config, user, on_success=mark_success))
            else:
                results.append(run_account(args, config, user))
        except Exception as error:
            print(json.dumps({"account": index, "error": type(error).__name__,
                              "detail": str(error), "status": "failed"}, ensure_ascii=True), flush=True)
            results.append(1)
    return int(any(results))


def run_account(args, config, user, on_success=None):
    overrides = {key: value for key in ("roomid", "seatid", "start", "end")
                 if (value := getattr(args, key)) is not None}
    plans = reservation_plans(user, overrides)
    evening_first = user.get(CLASSIC_EVENING_FIRST_KEY) is True
    today = dt.date.today()
    target_day = today + dt.timedelta(days=0 if args.today else 1)
    skip_holidays = user.get("skipHolidays", config.get("skipHolidays", True))
    if skip_holidays is None:
        skip_holidays = True
    holiday_calendar = holidays.calendar()
    decision = adjustment_decision({"adjustments": user.get("adjustments", config.get("adjustments"))}, target_day)
    adjustment = holiday_calendar.adjustment(target_day)
    if args.check_only:
        print(json.dumps({"plans": plans, "targetDate": str(target_day),
                          "attemptOrder": [
                              {"plan": plan["plan"], "segments": ordered_uncovered_segments(
                                  plan["segments"][0][0], plan["segments"][-1][1], [],
                                  evening_first and plan["roomid"] == CLASSIC_ROOM_ID)}
                              for plan in plans],
                          "holiday": holiday_calendar.name(target_day),
                          "adjustment": adjustment,
                          "decision": decision,
                          "skipHolidays": bool(skip_holidays)}, ensure_ascii=True))
        if holiday_calendar.error:
            print(json.dumps({"warning": holiday_calendar.error}, ensure_ascii=True),
                  file=sys.stderr, flush=True)
        return 0

    if not args.ignore_weekday and today.strftime("%A") not in user["daysofweek"]:
        print(json.dumps({"date": str(today), "skipped": "weekday not configured"}))
        return 0

    if holiday_calendar.error:
        print(json.dumps({"warning": holiday_calendar.error}, ensure_ascii=True),
              file=sys.stderr, flush=True)
    elif not holiday_calendar.covers(target_day):
        print(json.dumps({"warning": f"holidays.json has no {target_day.year} entries; "
                                     "update it to skip statutory holidays"}, ensure_ascii=True),
              file=sys.stderr, flush=True)
    holiday = holiday_calendar.name(target_day)
    if decision == "skip":
        print(json.dumps({"date": str(target_day), "skipped": "user decision",
                          "holiday": holiday,
                          "adjustment": (adjustment or {}).get("name")}, ensure_ascii=True))
        return 0
    if holiday and decision != "reserve" and skip_holidays and not args.ignore_holiday:
        print(json.dumps({"date": str(target_day), "skipped": "statutory holiday",
                          "holiday": holiday}, ensure_ascii=True))
        return 0
    if adjustment and decision is None:
        print(json.dumps({"warning": f"{target_day} is an adjusted {adjustment['kind']} "
                                     f"({adjustment['name']}); no decision saved, "
                                     "reserving by default"}, ensure_ascii=True),
              file=sys.stderr, flush=True)

    client = reserve(
        sleep_time=0.2,
        max_attempt=2,
        enable_slider=True,
        reserve_next_day=not args.today,
    )
    trace_http_timings(client.requests)
    print(json.dumps({"progress": "logging_in", "date": str(target_day)}, ensure_ascii=True), flush=True)
    started = time.perf_counter()
    client.get_login_status()
    print(json.dumps({"timing": "login_page", "elapsed_ms":
                      round((time.perf_counter() - started) * 1000)}), flush=True)
    started = time.perf_counter()
    login_ok, message = client.login(user["username"], user["password"])
    print(json.dumps({"timing": "login", "elapsed_ms":
                      round((time.perf_counter() - started) * 1000)}), flush=True)
    if not login_ok:
        print(json.dumps({"login_ok": False, "message": message}, ensure_ascii=True))
        return 1

    client.requests.headers.update({"Host": "office.chaoxing.com"})
    original_post = client.requests.post
    submit_response = None

    def capture_submit(*args, **kwargs):
        nonlocal submit_response
        response = original_post(*args, **kwargs)
        url = kwargs.get("url") or (args[0] if args else "")
        if "/seat/submit" in url:
            submit_response = response
        return response

    client.requests.post = capture_submit
    target_start = plans[0]["segments"][0][0]
    target_end = plans[0]["segments"][-1][1]
    covered = []
    for plan in plans:
        window_start = max(time_minutes(target_start), time_minutes(plan["segments"][0][0]))
        window_end = min(time_minutes(target_end), time_minutes(plan["segments"][-1][1]))
        if window_start >= window_end:
            continue
        start_text = f"{window_start // 60:02d}:{window_start % 60:02d}"
        end_text = f"{window_end // 60:02d}:{window_end % 60:02d}"
        for start, end in ordered_uncovered_segments(
                start_text, end_text, covered,
                evening_first and plan["roomid"] == CLASSIC_ROOM_ID):
            print(json.dumps({"progress": "submitting", "plan_number": plan["plan"],
                              "time": [start, end], "roomid": plan["roomid"]}, ensure_ascii=True), flush=True)
            client.max_attempt = 2
            submit_response = None
            started = time.perf_counter()
            try:
                success = client.submit([start, end], plan["roomid"], plan["seatid"], False)
                response = submit_response.json() if submit_response is not None else {}
                record = (response.get("data") or {}).get("seatReserve") or {}
                result = {
                    "date": str(target_day),
                    "plan": plan["plan"],
                    "time": [start, end],
                    "roomid": plan["roomid"],
                    "seatid": plan["seatid"],
                    "success": bool(success),
                    "message": response.get("msg", ""),
                    "reservation_id": record.get("id"),
                }
            except Exception as error:
                result = {
                    "date": str(target_day),
                    "plan": plan["plan"],
                    "time": [start, end],
                    "roomid": plan["roomid"],
                    "seatid": plan["seatid"],
                    "success": False,
                    "error": type(error).__name__,
                }
            result["elapsed_ms"] = round((time.perf_counter() - started) * 1000)
            print(json.dumps(result, ensure_ascii=True), flush=True)
            if result["success"]:
                if on_success is not None:
                    on_success()
                covered.append((time_minutes(start), time_minutes(end)))
        if not uncovered_segments(target_start, target_end, covered):
            print(json.dumps({"status": "complete", "target": [target_start, target_end]}), flush=True)
            return 0
    missing = uncovered_segments(target_start, target_end, covered)
    print(json.dumps({"status": "partial_success" if covered else "failed",
                      "target": [target_start, target_end], "missing": missing}), flush=True)
    return 1


if __name__ == "__main__":
    sys.exit(main())
