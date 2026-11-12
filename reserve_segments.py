"""Reserve consecutive seat slots of at most four hours each."""

import argparse
from contextlib import contextmanager
import datetime as dt
import json
import os
import re
import sys
import time
from pathlib import Path
from urllib.parse import urlsplit
import requests

from utils import holidays, holiday_update, reserve

if os.name == "nt":
    import msvcrt


@contextmanager
def exclusive_file_lock(lock_path):
    """Hold a cross-process lock on *lock_path* for the body of the block."""
    with Path(lock_path).open("a+b") as lock:
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
            yield
        finally:
            if os.name == "nt":
                lock.seek(0)
                msvcrt.locking(lock.fileno(), msvcrt.LK_UNLCK, 1)


@contextmanager
def scheduled_account_guard(config_path, target_day, username):
    """Serialize scheduled accounts and keep the first successful account for a day."""
    lock_path = Path(config_path).with_name("scheduled-account.lock")
    state_path = Path(config_path).with_name("scheduled-account-state.json")
    with exclusive_file_lock(lock_path):
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


SCHEDULED_RUN_STATE = "scheduled-run-state.json"
SCHEDULED_RUN_LOCK = "scheduled-run.lock"
# Waiting for the other accounts has no fixed give-up delay: it ends when those
# accounts really finish (or their process is gone). The only backstop is the
# task's own execution limit, which run-local.ps1 keeps at 30 minutes, so the
# process stops waiting early enough to still log its own check before Windows
# kills the task (a sibling cannot outlive its own 30-minute limit either).
TASK_TIME_LIMIT = 30 * 60.0
WAIT_DEADLINE = TASK_TIME_LIMIT - 2 * 60.0
PROCESS_STARTED = time.monotonic()


def _account_numbers(values):
    numbers = set()
    for value in values or []:
        try:
            numbers.add(int(value))
        except (TypeError, ValueError):
            continue
    return sorted(numbers)


def _account_pids(values):
    pids = {}
    if isinstance(values, dict):
        for key, value in values.items():
            try:
                pids[int(key)] = int(value)
            except (TypeError, ValueError):
                continue
    return pids


def process_alive(pid):
    """True while *pid* is a live process; never raises."""
    try:
        pid = int(pid)
    except (TypeError, ValueError):
        return False
    if pid <= 0:
        return False
    try:
        if os.name == "nt":
            import ctypes

            synchronize, still_running = 0x00100000, 0x00000102
            handle = ctypes.windll.kernel32.OpenProcess(synchronize, False, pid)
            if not handle:
                return False
            try:
                return ctypes.windll.kernel32.WaitForSingleObject(handle, 0) == still_running
            finally:
                ctypes.windll.kernel32.CloseHandle(handle)
        os.kill(pid, 0)
        return True
    except Exception:
        return False


def _run_state(path, target_day):
    """Completion markers for one day; a new day starts from scratch."""
    try:
        state = json.loads(Path(path).read_text(encoding="utf-8"))
    except (FileNotFoundError, ValueError):
        state = {}
    if not isinstance(state, dict) or state.get("date") != str(target_day):
        return {"date": str(target_day), "started": [], "finished": [], "pids": {}}
    return {"date": str(target_day),
            "started": _account_numbers(state.get("started")),
            "finished": _account_numbers(state.get("finished")),
            "pids": _account_pids(state.get("pids"))}


def scheduled_run_state(config_path, target_day):
    """Read how far this day's scheduled accounts have got."""
    return _run_state(Path(config_path).with_name(SCHEDULED_RUN_STATE), target_day)


def mark_scheduled_run(config_path, target_day, account, phase):
    """Record that *account* started or finished its scheduled run for a day."""
    if phase not in ("started", "finished"):
        raise ValueError("phase must be 'started' or 'finished'")
    state_path = Path(config_path).with_name(SCHEDULED_RUN_STATE)
    changed = False
    with exclusive_file_lock(Path(config_path).with_name(SCHEDULED_RUN_LOCK)):
        state = _run_state(state_path, target_day)
        if int(account) not in state[phase]:
            state[phase] = sorted({*state[phase], int(account)})
            changed = True
        if phase == "started" and state["pids"].get(int(account)) != os.getpid():
            # The PID lets the other accounts tell "still booking" from "gone".
            state["pids"][int(account)] = os.getpid()
            changed = True
        if changed:
            temp_path = state_path.with_suffix(".tmp")
            temp_path.write_text(json.dumps(state, ensure_ascii=True), encoding="utf-8")
            os.replace(temp_path, state_path)
    return state


def wait_for_scheduled_runs(config_path, target_day, expected, wait_seconds=WAIT_DEADLINE,
                            sleep_fn=time.sleep, clock=time.monotonic,
                            alive_fn=process_alive):
    """Wait until every scheduled account that actually started has finished.

    There is no fixed give-up delay: an account counts as still running while its
    process is alive, so a slow run is waited for until it really ends, while an
    account that died without writing its marker stops blocking immediately.
    Only accounts that already wrote a "started" marker are awaited, so a
    single-account setup, or an account whose task was never registered, cannot
    keep the other account idle. ``wait_seconds`` is only the backstop that keeps
    this process inside its own task execution limit; it returns the accounts
    still pending at that deadline (empty when everything finished).
    """
    deadline = clock() + max(0.0, wait_seconds)
    while True:
        state = scheduled_run_state(config_path, target_day)
        pending = sorted((set(state["started"]) & set(expected)) - set(state["finished"]))
        pending = [index for index in pending
                   if state["pids"].get(index) is None or alive_fn(state["pids"][index])]
        if not pending or clock() >= deadline:
            return pending
        sleep_fn(0.5)


CLASSIC_ROOM_ID = "11031"
AFTERNOON_START = "15:00"
CLASSIC_EVENING_FIRST_KEY = "classicEveningFirst"
TIMES_BY_WEEKDAY_KEY = "timesByWeekday"
WEEKDAY_NAMES = ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday")

SPRINT_WINDOW_MAX = 300.0
SPRINT_TOKEN_LEAD = 4.0
SPRINT_CAPTCHA_LEAD = 2.5
SPRINT_FIRE_AFTER = 0.3

SEAT_TAKEN_MARKERS = ("已被预约", "已经预约", "已预约", "已被占用", "已占用", "冲突")
SEAT_HELD_MARKERS = ("已有预约",)
TRANSIENT_SUBMIT_MARKERS = ("页面停留过久", "安全验证已超时")


def seat_taken_message(message):
    """True when the school rejected a submit because the slot is no longer free.

    The school returns the same HTTP 200 for this rejection, so the text is the
    only signal. It can also mean an earlier retry of ours already landed.
    """
    text = str(message or "")
    return any(marker in text for marker in SEAT_TAKEN_MARKERS)


def seat_held_message(message):
    """True when the school says the account already holds this slot.

    A submit whose response timed out can still be processed server-side, so
    later attempts see this message instead of a fresh booking.  The message
    only proves an overlap, not that the whole attempted chunk is held.
    """
    text = str(message or "")
    return any(marker in text for marker in SEAT_HELD_MARKERS)


def transient_submit_message(message):
    """True when the school rejected a submit before it could be processed.

    A 302 ("页面停留过久") means the one-shot page/security token expired.  The
    slot is certainly not booked, so a fresh token and captcha can still win it
    after every other chunk has been fired.
    """
    text = str(message or "")
    return any(marker in text for marker in TRANSIENT_SUBMIT_MARKERS)


def sprint_opening(config):
    """Local datetime when next-day reservations open, from the saved schedule time."""
    text = str(config.get("scheduleAt") or "").strip()
    try:
        parsed = dt.datetime.strptime(text, "%H:%M").time()
    except ValueError:
        return None
    return dt.datetime.combine(dt.date.today(), parsed)


def sprint_window(opening, now_fn=dt.datetime.now):
    """Seconds until opening; None when opening is unknown or already passed."""
    if opening is None:
        return None
    lead = (opening - now_fn()).total_seconds()
    if lead < 0:
        return None
    return lead


def prepare_sprint(client, plans, evening_first, target_start, target_end, config,
                   now_fn=dt.datetime.now):
    """Wait out the last seconds before opening and return a submit-ready package.

    Returns None when there is no sprint opportunity, so callers fall back to
    the normal flow. Callers must never submit before the opening time.
    """
    opening = sprint_opening(config)
    lead = sprint_window(opening, now_fn)
    if lead is None or lead > SPRINT_WINDOW_MAX:
        return None
    if lead > SPRINT_TOKEN_LEAD:
        time.sleep(lead - SPRINT_TOKEN_LEAD)
    first = plans[0]
    window_start = max(time_minutes(target_start), time_minutes(first["segments"][0][0]))
    window_end = min(time_minutes(target_end), time_minutes(first["segments"][-1][1]))
    segments = ordered_uncovered_segments(
        f"{window_start // 60:02d}:{window_start % 60:02d}",
        f"{window_end // 60:02d}:{window_end % 60:02d}", [],
        evening_first and first["roomid"] == CLASSIC_ROOM_ID)
    if not segments:
        return None
    start, end = segments[0]
    seat = first["seatid"][0]
    print(json.dumps({"progress": "sprint_preparing", "time": [start, end],
                      "roomid": first["roomid"], "seatid": [seat],
                      "opening": opening.strftime("%H:%M:%S")}, ensure_ascii=True), flush=True)
    token, value = "", ""
    try:
        token, value = client._get_page_token(
            client.url.format(first["roomid"], seat), require_value=True)
        if not token or not value:
            day = (dt.date.today() + dt.timedelta(days=1)).isoformat()
            token, value = client._get_page_token(
                f"https://office.chaoxing.com/front/apps/seat/select?id={first['roomid']}&day={day}&backLevel=2",
                require_value=True)
    except Exception:
        token, value = "", ""
    if not token or not value:
        return None
    remaining = (opening - now_fn()).total_seconds()
    if remaining > SPRINT_CAPTCHA_LEAD:
        time.sleep(remaining - SPRINT_CAPTCHA_LEAD)
    try:
        captcha = client.resolve_captcha() if client.enable_slider else ""
    except Exception:
        captcha = ""
    remaining = (opening - now_fn()).total_seconds()
    if remaining > 0:
        time.sleep(remaining + SPRINT_FIRE_AFTER)
    print(json.dumps({"progress": "sprint_firing", "time": [start, end],
                      "seatid": [seat]}, ensure_ascii=True), flush=True)
    return {"start": start, "end": end, "roomid": first["roomid"], "seat": seat,
            "token": token, "value": value, "captcha": captcha, "used": False}


# --- fast sprint (opt-in per account, used by account 1) ---------------------
#
# The school rejects any submit that carries a page token fetched before the
# reservation window opened ("页面停留过久"), and invalidates a token as soon as
# the seat page is fetched again. So a preheated token can never be used: the
# fast path fetches one token right after the opening and submits immediately,
# reusing a single captcha result for every chunk.

FAST_SPRINT_KEY = "fastSprint"
FAST_SUBMIT_TIMEOUT = 4.0

# Per-account opt-in: wait for the submit answer before fetching the next seat
# page.  The school voids a page token as soon as the seat page is fetched again,
# so a 4s cut-off followed by an immediate refetch can kill a submit that is
# still queued server-side.  Only accounts with ``patientSubmit: true`` wait.
PATIENT_SUBMIT_KEY = "patientSubmit"
PATIENT_SUBMIT_TIMEOUT = 8.0

# The post-race check is the only trustworthy source, so it must not be cut
# short per account: on 2026-11-12 a 2s budget lost a query that answered in
# 2.007s, which turned a complete booking into a false failure.  Submit timeouts
# stay per-account; the check timeout is shared.
VERIFY_TIMEOUT = 10.0
LOCAL_TZ = dt.timezone(dt.timedelta(hours=8))


def fast_mode(user):
    """True when the account opted in to the token-then-submit sprint."""
    return user.get(FAST_SPRINT_KEY) is True


def submit_timeouts(user):
    """(submit, verify) seconds for one account.

    ``patientSubmit`` only changes how long a submit waits for its answer (8s vs
    4s), so the two settings can still be compared side by side in the same
    night.  The post-race check always gets ``VERIFY_TIMEOUT``: it runs after the
    race, so waiting longer costs nothing, while a false "check failed" makes the
    run report the opposite of what actually happened.
    """
    if user.get(PATIENT_SUBMIT_KEY) is True:
        return PATIENT_SUBMIT_TIMEOUT, VERIFY_TIMEOUT
    return FAST_SUBMIT_TIMEOUT, VERIFY_TIMEOUT


def _solve_captcha(client):
    try:
        return client.resolve_captcha() if client.enable_slider else ""
    except Exception:
        return ""


def seat_page_token(client, roomid, seat):
    """Fetch a fresh submission token; reloading the page invalidates older ones."""
    token, value = client._get_page_token(
        client.url.format(roomid, seat), require_value=True)
    if not token or not value:
        day = (dt.date.today() + dt.timedelta(
            days=1 if client.reserve_next_day else 0)).isoformat()
        token, value = client._get_page_token(
            f"https://office.chaoxing.com/front/apps/seat/select?id={roomid}"
            f"&day={day}&backLevel=2",
            require_value=True)
    return token, value


def submit_chunk(client, chunk, roomid, seat, captcha, timeout=FAST_SUBMIT_TIMEOUT):
    """One token fetch plus exactly one submit.

    A timeout is *not* a failure: the request may still be processed server-side,
    so it is returned as ``outcome: unknown`` and resolved by verification later.
    """
    started = time.perf_counter()
    result = {"time": [str(chunk[0]), str(chunk[1])], "roomid": str(roomid),
              "seatid": [str(seat)], "success": False, "message": "",
              "reason": None, "reservation_id": None, "outcome": "rejected"}
    try:
        token, value = seat_page_token(client, roomid, seat)
    except Exception as error:
        # A single broken page must not abort the whole sprint.
        result.update(error=type(error).__name__, message="座位页面加载失败，未提交",
                      elapsed_ms=round((time.perf_counter() - started) * 1000))
        return result
    if not token or not value:
        result.update(error="NoToken", message="座位页面未提供提交令牌，未提交",
                      elapsed_ms=round((time.perf_counter() - started) * 1000))
        return result
    client.last_submit_payload = {}
    try:
        success = client.get_submit(
            client.submit_url, times=[str(chunk[0]), str(chunk[1])], token=token,
            roomid=str(roomid), seatid=str(seat), captcha=captcha, action=False,
            value=value, timeout=timeout)
    except (requests.exceptions.ConnectionError, requests.exceptions.Timeout) as error:
        result.update(outcome="unknown", error=type(error).__name__,
                      elapsed_ms=round((time.perf_counter() - started) * 1000))
        return result
    except Exception as error:
        result.update(error=type(error).__name__,
                      message="提交返回无法解析（可能被重定向），请核对预约记录",
                      elapsed_ms=round((time.perf_counter() - started) * 1000))
        return result
    payload = client.last_submit_payload or {}
    record = (payload.get("data") or {}).get("seatReserve") or {}
    message = payload.get("msg", "")
    result.update(success=bool(success), message=message,
                  reservation_id=record.get("id"),
                  outcome="success" if success else "rejected",
                  elapsed_ms=round((time.perf_counter() - started) * 1000))
    if not success:
        if seat_taken_message(message):
            result["reason"] = "seat_taken"
        elif seat_held_message(message):
            # "当前时间段您已有预约" means an earlier timed-out submit of ours landed.
            result["reason"] = "already_held"
    return result


def chunk_bounds(chunk, day):
    """Epoch milliseconds of a chunk on the target day (Asia/Shanghai)."""
    start = dt.datetime.fromisoformat(f"{day}T{chunk[0]}").replace(tzinfo=LOCAL_TZ)
    end = dt.datetime.fromisoformat(f"{day}T{chunk[1]}").replace(tzinfo=LOCAL_TZ)
    return int(start.timestamp() * 1000), int(end.timestamp() * 1000)


def chunk_landed(chunk, day, records):
    """True when the reservations *fully* cover the chunk.

    Partial overlap is not enough: a neighbouring 18:00-22:00 booking overlaps a
    15:00-19:00 chunk without covering 15:00-18:00.
    """
    start, end = chunk_bounds(chunk, day)
    spans = []
    for record in records or []:
        try:
            record_start, record_end = int(record["startTime"]), int(record["endTime"])
        except (KeyError, TypeError, ValueError):
            continue
        spans.append((max(record_start, start), min(record_end, end)))
    cursor = start
    for span_start, span_end in sorted(spans):
        if span_start <= cursor < span_end:
            cursor = span_end
        if cursor >= end:
            return True
    return False


def reservation_fid_enc(client, roomid, day):
    """Read the account's fidEnc from the seat selection page."""
    try:
        page = client.requests.get(
            "https://office.chaoxing.com/front/apps/seat/select",
            params={"id": roomid, "day": str(day), "backLevel": 2})
        match = re.search(r"fidEnc\s*=\s*'([^']+)'", page.text)
    except Exception:
        return None
    return match.group(1) if match else None


def current_reservations(client, fid_enc, timeout=VERIFY_TIMEOUT):
    """Current reservations, or None when the check itself fails (bounded by *timeout*)."""
    if not fid_enc:
        return None
    try:
        response = client.requests.get(
            "https://office.chaoxing.com/data/apps/seat/index",
            params={"fidEnc": fid_enc}, timeout=timeout)
        payload = response.json()
    except Exception:
        return None
    data = payload.get("data") if isinstance(payload, dict) else None
    records = (data or {}).get("curReserves")
    return records if isinstance(records, list) else None


def _record_span(record, target_day):
    """One reservation record as (start, end) minutes from midnight of *target_day*.

    Records for other days, and records whose times cannot be parsed, return None;
    a record ending after midnight is clipped to 23:59 so the span stays on one day.
    """
    try:
        begin_ms, finish_ms = int(record["startTime"]), int(record["endTime"])
    except (KeyError, TypeError, ValueError):
        return None
    midnight = int(dt.datetime.combine(target_day, dt.time(), tzinfo=LOCAL_TZ).timestamp() * 1000)
    begin = max(0, (begin_ms - midnight) // 60000)
    finish = min(1439, (finish_ms - midnight) // 60000)
    if finish <= begin:
        return None
    return begin, finish


def emit_line(line):
    """Write one log line to stdout and flush it, even when stdout is piped."""
    print(line, flush=True)


def verify_account_reservations(config, index, user, target_day, emit=emit_line, timeout=10):
    """Log back in and record what the school actually holds for *target_day*.

    This runs once every scheduled account of the day has finished, so a submit
    that timed out can no longer change the answer: the school's own record list
    is the only trustworthy source. A failing check is reported, never raised, so
    it can never turn a successful booking into a failed run.
    """
    event = {"verify": "account_result", "account_index": index,
             "username": user.get("username", ""), "date": str(target_day),
             "checked_at": dt.datetime.now(LOCAL_TZ).strftime("%H:%M:%S"),
             "target": None, "reservations": [], "missing": [],
             "verified": "unavailable", "message": "", "error": None}
    try:
        plan = reservation_plans(user, {}, target_day.strftime("%A"))[0]
        start, end = plan["segments"][0][0], plan["segments"][-1][1]
        event["target"] = [start, end]
        client = reserve(sleep_time=0.2, max_attempt=1, enable_slider=True,
                         reserve_next_day=False)
        client.get_login_status()
        logged_in, message = client.login(user["username"], user["password"])
        if not logged_in:
            event.update(verified="unavailable", error="LoginFailed",
                         message=message or "登录失败")
        else:
            client.requests.headers.update({"Host": "office.chaoxing.com"})
            fid_enc = (reservation_fid_enc(client, plan["roomid"], target_day)
                       or user.get("deptIdEnc") or config.get("deptIdEnc"))
            records = current_reservations(client, fid_enc, timeout=timeout)
            if records is None:
                event.update(error="VerifyUnavailable", message="预约记录查询失败")
            else:
                covered = []
                for record in records:
                    span = _record_span(record, target_day)
                    if span is None:
                        continue
                    begin, finish = span
                    covered.append((begin, finish))
                    event["reservations"].append({
                        "time": [f"{begin // 60:02d}:{begin % 60:02d}",
                                 f"{finish // 60:02d}:{finish % 60:02d}"],
                        "seat": str(record.get("seatNum") or "").strip(),
                        "room": str(record.get("roomId") or "").strip(),
                        "id": record.get("id"),
                        "status": record.get("status"),
                    })
                event["missing"] = uncovered_segments(start, end, covered)
                if not event["reservations"]:
                    event["verified"] = "none"
                elif not event["missing"]:
                    event["verified"] = "complete"
                else:
                    event["verified"] = "partial"
                event["message"] = ("已核对学习通预约记录" if event["verified"] != "none"
                                    else "核对未查到该日的预约记录")
    except Exception as error:
        event.update(verified="unavailable", error=type(error).__name__,
                     message=str(error)[:200])
    emit(json.dumps(event, ensure_ascii=True))
    return event


def _chunk_event(target_day, plan, chunk, result):
    event = {
        "date": str(target_day),
        "plan": plan["plan"],
        "time": [chunk[0], chunk[1]],
        "roomid": str(plan["roomid"]),
        "seatid": result.get("seatid") or [str(plan["seatid"][0])],
        "success": bool(result.get("success")),
        "message": result.get("message", ""),
        "reason": result.get("reason"),
        "reservation_id": result.get("reservation_id"),
        "elapsed_ms": result.get("elapsed_ms"),
    }
    if result.get("outcome") == "unknown":
        event["outcome"] = "unknown"
    if result.get("error"):
        event["error"] = result["error"]
    if result.get("attempts"):
        event["attempts"] = result["attempts"]
    return event


def _race_pass(client, plans, target_start, target_end, covered, captcha,
               target_day, emit, tried, deferred, on_success=None,
               submit_timeout=FAST_SUBMIT_TIMEOUT):
    """Fire every chunk once, in time order, without waiting for retries.

    Returns ``covered`` (scheduling coverage), ``deferred`` (timed-out chunks),
    ``confirmed`` (successful submits only), ``held`` (chunks the school
    reported as already held) and ``rejected`` (transient security rejections
    such as 302).
    """
    confirmed, held, rejected = [], [], []
    for plan in plans:
        window_start = max(time_minutes(target_start), time_minutes(plan["segments"][0][0]))
        window_end = min(time_minutes(target_end), time_minutes(plan["segments"][-1][1]))
        if window_start >= window_end:
            continue
        start_text = f"{window_start // 60:02d}:{window_start % 60:02d}"
        end_text = f"{window_end // 60:02d}:{window_end % 60:02d}"
        # Afternoon/evening slots vanish first, so book them before the morning ones.
        # Reversing the 4h chunks keeps the segmentation intact (no 1h tail).
        for chunk in reversed(uncovered_segments(start_text, end_text, covered)):
            key = (chunk[0], chunk[1], str(plan["roomid"]), str(plan["seatid"][0]))
            if key in tried:
                continue
            tried.add(key)
            emit(json.dumps({"progress": "submitting", "plan_number": plan["plan"],
                             "time": chunk, "roomid": plan["roomid"]},
                            ensure_ascii=True))
            result = submit_chunk(client, chunk, plan["roomid"], plan["seatid"][0],
                                  captcha, timeout=submit_timeout)
            if result["outcome"] == "unknown":
                deferred.append((chunk, plan, result))
                emit(json.dumps({"progress": "submit_timeout", "plan_number": plan["plan"],
                                 "time": chunk, "error": result.get("error")},
                                ensure_ascii=True))
                continue
            emit(json.dumps(_chunk_event(target_day, plan, chunk, result),
                            ensure_ascii=True))
            if result["success"] or result.get("reason") == "already_held":
                if on_success is not None:
                    on_success()
                # already_held keeps the sprint from re-submitting the same chunk,
                # but it is not proof that the whole chunk is booked; the caller
                # decides later whether it can be trusted for coverage.
                covered.append((time_minutes(chunk[0]), time_minutes(chunk[1])))
                if result["success"]:
                    confirmed.append((time_minutes(chunk[0]), time_minutes(chunk[1])))
                else:
                    held.append((chunk, plan))
            elif transient_submit_message(result.get("message")):
                rejected.append((chunk, plan))
    return covered, deferred, confirmed, held, rejected


def _reconcile_coverage(covered, plans, target_start, target_end, target_day,
                        records, emit):
    """Add coverage the school actually recorded, even when a submit looked failed.

    A submit that timed out can be committed server-side and then answer "302" or
    "您已有预约" to our retry, so one final read of the real reservations is the
    only trustworthy source.
    """
    added = []
    for chunk in uncovered_segments(target_start, target_end, covered):
        if chunk_landed(chunk, target_day, records):
            covered.append((time_minutes(chunk[0]), time_minutes(chunk[1])))
            added.append(chunk)
    if added:
        emit(json.dumps({"verify": "reconciled", "added": added}, ensure_ascii=True))
    return covered


def _plan_for_chunk(plans, chunk):
    """First plan whose configured window contains *chunk* (primary plan first)."""
    for plan in plans:
        segments = plan.get("segments") or []
        if segments and segments[0][0] <= chunk[0] and chunk[1] <= segments[-1][1]:
            return plan
    return plans[0]


def fast_race(client, config, plans, target_start, target_end, target_day,
              emit=print, now_fn=dt.datetime.now, sleep_fn=time.sleep,
              on_success=None, submit_timeout=FAST_SUBMIT_TIMEOUT,
              verify_timeout=VERIFY_TIMEOUT):
    """Sprint at the opening, then read the real reservations once and retry only those.

    ``submit_timeout``/``verify_timeout`` make the waiting configurable per account
    (see ``submit_timeouts``): the submit wait defaults to 4s (8s for a patient
    account), while the post-race check always waits ``VERIFY_TIMEOUT`` because
    it runs after the race and a false "check failed" is worse than the seconds
    it costs.

    Returns confirmed (start, end) minutes, or None when this run is too early for
    a sprint so the caller should use the legacy flow.  A chunk the school answered
    with "already held" is scheduling coverage only: it proves an overlap, not that
    the whole chunk is booked, so it is trusted for coverage only when no timed-out
    submit overlaps it.  A 302 security rejection is retried once with a fresh
    captcha after every other chunk has been fired.  Slots the final check proves
    empty are retried once more after the server has recovered.
    """
    opening = sprint_opening(config)
    lead = sprint_window(opening, now_fn)
    if lead is not None and lead > SPRINT_WINDOW_MAX:
        return None
    captcha = ""
    if lead is not None:
        if lead > SPRINT_CAPTCHA_LEAD:
            sleep_fn(lead - SPRINT_CAPTCHA_LEAD)
        captcha = _solve_captcha(client)
        remaining = (opening - now_fn()).total_seconds()
        if remaining > 0:
            sleep_fn(remaining + SPRINT_FIRE_AFTER)
    if client.enable_slider and not captcha:
        captcha = _solve_captcha(client)
    emit(json.dumps({"progress": "fast_sprint", "seatid": [str(plans[0]["seatid"][0])],
                     "roomid": str(plans[0]["roomid"]),
                     "captcha": bool(captcha),
                     "submit_timeout": submit_timeout,
                     "verify_timeout": verify_timeout}, ensure_ascii=True))

    covered, deferred, confirmed, held, rejected = _race_pass(
        client, plans, target_start, target_end, [], captcha, target_day, emit,
        set(), [], on_success=on_success, submit_timeout=submit_timeout)
    unknown_spans = [chunk for chunk, _plan, _result in deferred]
    # One read of the real reservations decides what actually landed; it runs after
    # every chunk has been fired, so it never delays the race itself.
    records = current_reservations(
        client, reservation_fid_enc(client, plans[0]["roomid"], target_day),
        timeout=verify_timeout)
    retry = []
    if deferred:
        for chunk, plan, result in deferred:
            landed = records is not None and chunk_landed(chunk, target_day, records)
            if landed:
                result.update(success=True, outcome="confirmed", reason=None,
                              message="提交超时，核对后确认已生效")
                covered.append((time_minutes(chunk[0]), time_minutes(chunk[1])))
                confirmed.append((time_minutes(chunk[0]), time_minutes(chunk[1])))
                if on_success is not None:
                    on_success()
            elif records is None:
                result.update(outcome="unknown",
                              message="提交超时，核对未成功（请在学习通查看预约记录）")
                retry.append((chunk, plan))
            else:
                result.update(outcome="rejected",
                              message="提交超时且核对未查到预约")
            emit(json.dumps(_chunk_event(target_day, plan, chunk, result),
                            ensure_ascii=True))
        emit(json.dumps({"verify": "checked" if records is not None else "unavailable",
                         "deferred": len(deferred)}, ensure_ascii=True))
    if records is not None:
        confirmed = _reconcile_coverage(confirmed, plans, target_start, target_end,
                                        target_day, records, emit)
        for span in confirmed:
            if span not in covered:
                covered.append(span)
    # "当前时间段您已有预约" only proves an overlap.  When a timed-out submit could
    # explain that overlap, the rest of the chunk may still be free: do not credit
    # the whole chunk as covered.
    for chunk, _plan in held:
        if any(spans_overlap(chunk, unknown) for unknown in unknown_spans):
            continue
        if span_covered(chunk, confirmed):
            continue
        confirmed.append((time_minutes(chunk[0]), time_minutes(chunk[1])))
    for chunk, plan in retry:
        start_minutes, end_minutes = time_minutes(chunk[0]), time_minutes(chunk[1])
        # Skip a backup seat once this timeslot is already confirmed.  Retrying a
        # slot that timed out again is still allowed: a different seat may work,
        # but re-asking after "already held" only adds load during a slow opening.
        if span_covered(chunk, confirmed):
            continue
        emit(json.dumps({"progress": "retry_unverified", "plan_number": plan["plan"],
                         "time": chunk, "roomid": plan["roomid"]}, ensure_ascii=True))
        result = submit_chunk(client, chunk, plan["roomid"], plan["seatid"][0],
                              captcha, timeout=submit_timeout)
        emit(json.dumps(_chunk_event(target_day, plan, chunk, result), ensure_ascii=True))
        if result["success"] or result.get("reason") == "already_held":
            # "Already held" after a timed-out submit means that submit landed:
            # the account holds the timeslot even though the seat id may differ.
            if on_success is not None:
                on_success()
            covered.append((start_minutes, end_minutes))
            confirmed.append((start_minutes, end_minutes))
    if rejected:
        try:
            # A 302 is a one-shot security-token rejection, so the slot certainly
            # did not land.  Retry it only here, after every primary/backup chunk,
            # with a fresh captcha; this stays completely off the sprint path.
            fresh_captcha = _solve_captcha(client) if client.enable_slider else ""
            if client.enable_slider and not fresh_captcha:
                fresh_captcha = captcha
            for chunk, plan in rejected[:2]:
                if records is not None and chunk_landed(chunk, target_day, records):
                    continue
                if span_covered(chunk, confirmed):
                    continue
                emit(json.dumps({"progress": "retry_rejected",
                                 "plan_number": plan["plan"], "time": chunk,
                                 "roomid": plan["roomid"]}, ensure_ascii=True))
                result = submit_chunk(client, chunk, plan["roomid"],
                                      plan["seatid"][0], fresh_captcha,
                                      timeout=submit_timeout)
                emit(json.dumps(_chunk_event(target_day, plan, chunk, result),
                                ensure_ascii=True))
                if result["success"]:
                    if on_success is not None:
                        on_success()
                    covered.append((time_minutes(chunk[0]), time_minutes(chunk[1])))
                    confirmed.append((time_minutes(chunk[0]), time_minutes(chunk[1])))
        except Exception as error:
            emit(json.dumps({"verify": "post_pass_error",
                             "error": type(error).__name__}, ensure_ascii=True))
    # Slots the verification proved empty were probably lost to a seat-page or
    # submit timeout during the opening burst.  The server has usually recovered
    # by the time we get here, so give each such slot one more bounded attempt;
    # without it the gap would survive until the 22:30 transfer.
    if records is not None:
        missing = uncovered_segments(target_start, target_end, covered)
        if missing and client.enable_slider:
            # The sprint captcha result is minutes old by now; a fresh one avoids
            # spending these bounded retries on a rejected security check.
            fresh_captcha = _solve_captcha(client)
            if fresh_captcha:
                captcha = fresh_captcha
        for chunk in missing:
            plan = _plan_for_chunk(plans, chunk)
            emit(json.dumps({"progress": "retry_missing", "plan_number": plan["plan"],
                             "time": chunk, "roomid": plan["roomid"]},
                            ensure_ascii=True))
            result = submit_chunk(client, chunk, plan["roomid"], plan["seatid"][0],
                                  captcha, timeout=submit_timeout)
            if (not result["success"] and result.get("reason") != "already_held"
                    and transient_submit_message(result.get("message"))
                    and client.enable_slider):
                fresh_captcha = _solve_captcha(client)
                if fresh_captcha and fresh_captcha != captcha:
                    captcha = fresh_captcha
                    result = submit_chunk(client, chunk, plan["roomid"],
                                          plan["seatid"][0], captcha,
                                          timeout=submit_timeout)
            emit(json.dumps(_chunk_event(target_day, plan, chunk, result),
                            ensure_ascii=True))
            if result["success"] or result.get("reason") == "already_held":
                if on_success is not None:
                    on_success()
                covered.append((time_minutes(chunk[0]), time_minutes(chunk[1])))
                confirmed.append((time_minutes(chunk[0]), time_minutes(chunk[1])))
    return confirmed


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


def weekday_window(user, weekday=None):
    """Return the window for the day being booked (not the day the task runs).

    ``timesByWeekday`` maps a weekday name to ``[start, end]`` and overrides
    ``time`` for that weekday, for example ``{"Saturday": ["10:00", "21:00"]}``:
    the same room often closes earlier at weekends than on weekdays.
    """
    overrides = user.get(TIMES_BY_WEEKDAY_KEY)
    if overrides is None:
        overrides = {}
    if not isinstance(overrides, dict):
        raise ValueError(f"{TIMES_BY_WEEKDAY_KEY} must map a weekday to [start, end]")
    unknown = sorted(name for name in overrides if name not in WEEKDAY_NAMES)
    if unknown:
        raise ValueError(f"{TIMES_BY_WEEKDAY_KEY} has unknown weekday: {', '.join(unknown)}")
    selected = overrides.get(weekday) if weekday else None
    if selected is None:
        return user.get("time") or []
    if not isinstance(selected, (list, tuple)) or len(selected) != 2:
        raise ValueError(f"{TIMES_BY_WEEKDAY_KEY}.{weekday} must be [start, end]")
    start, end = (str(value).strip() for value in selected)
    for value in (start, end):
        if not re.fullmatch(r"(?:[01]\d|2[0-3]):[0-5]\d", value):
            raise ValueError(f"{TIMES_BY_WEEKDAY_KEY}.{weekday} needs HH:MM times")
    if time_minutes(end) <= time_minutes(start):
        raise ValueError(f"{TIMES_BY_WEEKDAY_KEY}.{weekday} end must be later than start")
    return [start, end]


def reservation_plans(user, overrides=None, weekday=None):
    """Return the primary plan followed by ordered alternatives.

    ``weekday`` is the day being booked: the primary window comes from
    ``timesByWeekday`` when that day has one. Backups keep their own range and
    the caller clamps them to the primary window, so they never book outside it.
    """
    overrides = overrides or {}
    raw_plans = [user] if overrides else [user, *user.get("backups", [])]
    window = weekday_window(user, weekday)
    plans = []
    for index, item in enumerate(raw_plans):
        roomid = str(overrides.get("roomid") or item["roomid"]).strip()
        seatids = ([str(overrides["seatid"])] if overrides.get("seatid")
                   else [str(value).strip() for value in item["seatid"]])
        times = window if index == 0 else (item.get("time") or [])
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


def spans_overlap(first, second):
    """True when two [start, end] HH:MM windows share at least one minute."""
    first_start, first_end = time_minutes(first[0]), time_minutes(first[1])
    second_start, second_end = time_minutes(second[0]), time_minutes(second[1])
    return first_start < second_end and second_start < first_end


def span_covered(span, covered):
    """True when a single HH:MM window is fully inside the minute coverage list."""
    start, end = time_minutes(span[0]), time_minutes(span[1])
    return any(begin <= start and end <= finish for begin, finish in covered)


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
    """Retry temporary access failures and log safe request durations.

    ``session.temporary_retry_attempts`` caps the internal retries and is read on
    every attempt, so the fast sprint can lower it to 1 right before wrapping: a
    submit carries a single-use page token, so retrying the same request only
    burns seconds and comes back as "页面停留过久(302)".
    """
    if not hasattr(session, "request"):
        return
    original_request = session.request

    def timed_request(method, url, *args, **kwargs):
        deadline = time.monotonic() + 60
        kwargs.setdefault("timeout", 10)
        attempt = 0
        while True:
            attempt += 1
            # Read the cap on every attempt: the fast sprint lowers it to 1 just
            # before wrapping, and an install-time snapshot would keep 5 in force.
            max_attempts = max(1, int(getattr(session, "temporary_retry_attempts", 5) or 5))
            started = time.perf_counter()
            response = None
            error_type = None
            try:
                response = original_request(method, url, *args, **kwargs)
                if response.status_code not in (429, 500, 502, 503, 504):
                    return response
                if attempt >= max_attempts or time.monotonic() >= deadline:
                    response.raise_for_status()
            except (requests.exceptions.ConnectionError, requests.exceptions.Timeout) as error:
                error_type = type(error).__name__
                if attempt >= max_attempts or time.monotonic() >= deadline:
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


def update_holidays(args, emit=emit_line):
    """``--update-holidays``: refresh ``holidays.json`` from the official arrangement."""
    year = dt.date.today().year
    years = [args.holiday_year] if args.holiday_year else [year, year + 1]
    results = holiday_update.fetch_all(years, timeout=10)
    for line in holiday_update.describe(results):
        emit(json.dumps({"holidays": "source", "detail": line}, ensure_ascii=True))
    path = holidays.default_path()
    summary = holiday_update.apply(path, results)
    emit(json.dumps({"holidays": "written" if summary["written"] else "unchanged",
                     "path": str(path), "years": summary["years"],
                     "pending": summary["pending"], "errors": summary["errors"]},
                    ensure_ascii=True))
    return 0 if not summary["errors"] else 1


def ensure_holiday_calendar(target_day, emit=emit_line):
    """Fill the target year once per run; a bad network never blocks the run."""
    path = holidays.default_path()
    try:
        summary = holiday_update.ensure_year(path, target_day.year, timeout=4)
    except Exception as error:
        emit(json.dumps({"warning": f"节假日表自动更新失败：{type(error).__name__}: {error}"},
                        ensure_ascii=True))
        return None
    if summary.get("written"):
        emit(json.dumps({"holidays": "updated", "year": target_day.year,
                         "pending": summary.get("pending"),
                         "sources": summary.get("sources")}, ensure_ascii=True))
    elif summary.get("errors"):
        emit(json.dumps({"warning": "节假日表更新失败：" + "；".join(summary["errors"])},
                        ensure_ascii=True))
    return summary


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("-u", "--user")
    parser.add_argument("--check-only", action="store_true")
    parser.add_argument("--account", type=int)
    parser.add_argument("--roomid")
    parser.add_argument("--seatid")
    parser.add_argument("--start")
    parser.add_argument("--end")
    parser.add_argument("--ignore-weekday", action="store_true")
    parser.add_argument("--ignore-holiday", action="store_true")
    parser.add_argument("--today", action="store_true", help="Book today instead of tomorrow for a one-off run")
    parser.add_argument("--verify-only", action="store_true",
                        help="Only query and log each account's recorded reservations")
    parser.add_argument("--update-holidays", action="store_true",
                        help="Fetch the official holiday arrangement into holidays.json")
    parser.add_argument("--holiday-year", type=int,
                        help="Year to fetch with --update-holidays (default: this year and the next)")
    args = parser.parse_args()

    if args.update_holidays:
        return update_holidays(args)
    if not args.user:
        parser.error("-u/--user is required unless --update-holidays is used")

    with open(args.user, encoding="utf-8") as file:
        config = json.load(file)
    users = config["reserve"]
    if not users:
        raise ValueError("Configure at least one account")
    if args.account is not None and not 1 <= args.account <= len(users):
        raise ValueError("Account number is out of range")
    planned = [(index, user) for index, user in enumerate(users, start=1)
               if user.get("scheduleEnabled", True) and
               str(user.get("roomid", "")).isdecimal() and user.get("seatid")]
    ready = [(index, user) for index, user in planned
             if args.account is None or index == args.account]
    if not ready:
        raise ValueError("No account has a saved reservation plan")
    if len({user.get("username") for _, user in ready}) != len(ready):
        raise ValueError("Account usernames must be unique")
    target_day = dt.date.today() + dt.timedelta(days=0 if args.today else 1)
    if args.verify_only:
        # Re-runs the same check the scheduled runs do, without booking anything.
        for index, user in ready:
            verify_account_reservations(config, index, user, target_day)
        return 0
    if not args.check_only:
        # Never let a stale holidays.json silently stop skipping holidays.
        ensure_holiday_calendar(target_day)
    results = []
    outcomes = []
    lead = sprint_window(sprint_opening(config)) if not args.check_only and not args.today else None
    race = lead is not None and lead <= SPRINT_WINDOW_MAX
    # The nightly batch is spread over one scheduled task per account, so every
    # process marks itself: the post-run check waits until all accounts that
    # actually started are done, then reads the school's records for each one.
    scheduled = not args.check_only and not args.today
    if scheduled:
        for index, _user in ready:
            mark_scheduled_run(args.user, target_day, index, "started")
    attempted = False
    try:
        for index, user in ready:
            if len(users) > 1:
                print(json.dumps({"account": index, "username": user.get("username", "")}, ensure_ascii=True), flush=True)
            outcome = {}
            try:
                if (args.account is not None and not args.check_only and
                        config.get("autoCancelOtherOnSuccess") is True and len(users) == 2
                        and not race):
                    with scheduled_account_guard(args.user, target_day, user["username"]) as (other_won, mark_success):
                        if other_won:
                            print(json.dumps({"date": str(target_day), "skipped": "other account succeeded"}), flush=True)
                            outcome["skipped"] = "other account succeeded"
                            results.append(0)
                        else:
                            results.append(run_account(args, config, user, on_success=mark_success,
                                                       outcome=outcome))
                else:
                    results.append(run_account(args, config, user, outcome=outcome))
            except Exception as error:
                print(json.dumps({"account": index, "error": type(error).__name__,
                                  "detail": str(error), "status": "failed"}, ensure_ascii=True), flush=True)
                results.append(1)
            attempted = attempted or bool(outcome.get("attempted"))
            outcomes.append(outcome)
    finally:
        if scheduled:
            for index, _user in ready:
                mark_scheduled_run(args.user, target_day, index, "finished")
    if scheduled and attempted:
        # No fixed "give up" delay: keep waiting until every started account is
        # really done, which is what "check after all accounts finished" means.
        # The deadline only shields this task from its own execution limit.
        remaining = max(60.0, WAIT_DEADLINE - (time.monotonic() - PROCESS_STARTED))
        pending = wait_for_scheduled_runs(args.user, target_day,
                                          [index for index, _user in planned],
                                          wait_seconds=remaining)
        if pending:
            print(json.dumps({"verify": "wait_timeout", "pending": pending,
                              "waited_seconds": round(remaining, 1),
                              "checked_accounts": [index for index, _user in ready]},
                             ensure_ascii=True), flush=True)
        checks = []
        for index, user in ready:
            checks.append((index, verify_account_reservations(config, index, user, target_day)))
        # The school's own record list is the only authoritative result: a submit
        # that timed out can have landed, so the earlier per-submit status line may
        # say "failed" while the booking is actually complete.  Re-state the final
        # outcome (and the process exit code) from this check so the log, the log
        # viewer and the Windows task result all agree.
        for position, (index, event) in enumerate(checks):
            outcome = outcomes[position] if position < len(outcomes) else {}
            if outcome.get("skipped"):
                continue
            state = (event or {}).get("verified")
            if state not in ("complete", "partial", "none"):
                continue
            if state == "complete":
                status, code = "complete", 0
            elif state == "partial":
                status, code = "partial_success", 1
            else:
                status, code = "failed", 1
            if position < len(results):
                results[position] = code
            print(json.dumps({"status": status,
                              "date": str(target_day),
                              "target": (event or {}).get("target"),
                              "missing": (event or {}).get("missing") or [],
                              "account": index,
                              "verified": True,
                              "source": "学习通预约记录核对"},
                             ensure_ascii=True), flush=True)
    return int(any(results))


def run_account(args, config, user, on_success=None, outcome=None):
    """Reserve one account's window.

    ``outcome`` is filled in for the caller: ``attempted`` becomes True once a
    login succeeded (so the post-run check knows a booking was actually tried)
    and ``skipped`` names the reason when nothing was submitted.
    """
    notes = outcome if outcome is not None else {}
    overrides = {key: value for key in ("roomid", "seatid", "start", "end")
                 if (value := getattr(args, key)) is not None}
    today = dt.date.today()
    target_day = today + dt.timedelta(days=0 if args.today else 1)
    plans = reservation_plans(user, overrides, target_day.strftime("%A"))
    evening_first = user.get(CLASSIC_EVENING_FIRST_KEY) is True
    skip_holidays = user.get("skipHolidays", config.get("skipHolidays", True))
    if skip_holidays is None:
        skip_holidays = True
    holiday_calendar = holidays.calendar()
    decision = adjustment_decision({"adjustments": user.get("adjustments", config.get("adjustments"))}, target_day)
    adjustment = holiday_calendar.adjustment(target_day)
    if args.check_only:
        fast = fast_mode(user)
        target_start = plans[0]["segments"][0][0]
        target_end = plans[0]["segments"][-1][1]
        attempt_order = []
        for plan in plans:
            # Mirror the real run: every plan is clamped to the primary window, so
            # backups cannot preview (or submit) slots outside it.
            window_start = max(time_minutes(target_start), time_minutes(plan["segments"][0][0]))
            window_end = min(time_minutes(target_end), time_minutes(plan["segments"][-1][1]))
            if window_start >= window_end:
                continue
            start_text = f"{window_start // 60:02d}:{window_start % 60:02d}"
            end_text = f"{window_end // 60:02d}:{window_end % 60:02d}"
            segments = (list(reversed(uncovered_segments(start_text, end_text, [])))
                        if fast else
                        ordered_uncovered_segments(start_text, end_text, [],
                                                   evening_first and plan["roomid"] == CLASSIC_ROOM_ID))
            attempt_order.append({"plan": plan["plan"], "segments": segments})
        print(json.dumps({"plans": plans, "targetDate": str(target_day),
                          "weekday": target_day.strftime("%A"),
                          "attemptOrder": attempt_order,
                          "sprint": "fast" if fast else "preheated",
                          "submitTimeout": submit_timeouts(user)[0],
                          "verifyTimeout": submit_timeouts(user)[1],
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
        notes["skipped"] = "weekday not configured"
        return 0

    if holiday_calendar.error:
        print(json.dumps({"warning": holiday_calendar.error}, ensure_ascii=True),
              file=sys.stderr, flush=True)
    elif not holiday_calendar.covers(target_day):
        print(json.dumps({"warning": f"holidays.json has no {target_day.year} entries; "
                                     "update it to skip statutory holidays"}, ensure_ascii=True),
              file=sys.stderr, flush=True)
    pending_break = holiday_calendar.pending_for(target_day)
    if pending_break:
        # The arrangement is published, but its legal days are not known yet: never
        # guess, just make the gap visible so the user can decide for that date.
        print(json.dumps({"warning": f"{target_day} 在{pending_break}放假区间内，法定节假日日期尚未确认"
                                     "（等待数据源更新），本次不会自动跳过"}, ensure_ascii=True),
              file=sys.stderr, flush=True)
    holiday = holiday_calendar.name(target_day)
    if decision == "skip":
        print(json.dumps({"date": str(target_day), "skipped": "user decision",
                          "holiday": holiday,
                          "adjustment": (adjustment or {}).get("name")}, ensure_ascii=True))
        notes["skipped"] = "user decision"
        return 0
    if holiday and decision != "reserve" and skip_holidays and not args.ignore_holiday:
        print(json.dumps({"date": str(target_day), "skipped": "statutory holiday",
                          "holiday": holiday}, ensure_ascii=True))
        notes["skipped"] = "statutory holiday"
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
    # Account 1 opts into the fast sprint: one fresh token per chunk, fired right
    # after the opening; other accounts keep the legacy preheated-token flow.
    fast_sprint = fast_mode(user) and client.reserve_next_day
    if fast_sprint:
        # One submit carries one single-use token: an internal retry only burns
        # seconds and comes back as "页面停留过久(302)". Set the cap *before* the
        # tracer wraps the session, so even the first submit is capped.
        client.requests.temporary_retry_attempts = 1
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

    notes["attempted"] = True
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
    covered = None
    if fast_sprint:
        submit_timeout, verify_timeout = submit_timeouts(user)
        covered = fast_race(client, config, plans, target_start, target_end, target_day,
                            on_success=on_success, submit_timeout=submit_timeout,
                            verify_timeout=verify_timeout)
    if covered is None:
        covered = []
        prepared = None
        if client.reserve_next_day:
            prepared = prepare_sprint(client, plans, evening_first, target_start,
                                      target_end, config)
            if prepared is None:
                opening = sprint_opening(config)
                lead = sprint_window(opening)
                if lead is not None and lead <= SPRINT_WINDOW_MAX:
                    time.sleep(lead + SPRINT_FIRE_AFTER)
                elif lead is not None:
                    print(json.dumps({"warning": f"本次运行比开放时间早 {lead / 60:.0f} 分钟，"
                                                 "超出抢点预热范围；请把定时预约时间改到开放时间附近"}, 
                                      ensure_ascii=True), file=sys.stderr, flush=True)
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
                use_prepared = (prepared is not None and not prepared["used"]
                                and plan["plan"] == 1
                                and [start, end] == [prepared["start"], prepared["end"]]
                                and str(plan["roomid"]) == str(prepared["roomid"])
                                and str(plan["seatid"][0]) == str(prepared["seat"]))
                client.max_attempt = 2
                submit_response = None
                client.submit_attempts = []
                started = time.perf_counter()
                try:
                    success = client.submit([start, end], plan["roomid"], plan["seatid"], False,
                                            prepared if use_prepared else None)
                    response = submit_response.json() if submit_response is not None else {}
                    record = (response.get("data") or {}).get("seatReserve") or {}
                    message = response.get("msg", "")
                    result = {
                        "date": str(target_day),
                        "plan": plan["plan"],
                        "time": [start, end],
                        "roomid": plan["roomid"],
                        "seatid": plan["seatid"],
                        "success": bool(success),
                        "message": message,
                        "reason": "seat_taken" if not success and seat_taken_message(message) else None,
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
                    # A timeout may still be processed server-side, so never report
                    # it as a plain failure; a dropped connection never arrived.
                    if isinstance(error, requests.exceptions.Timeout):
                        result["outcome"] = "unknown"
                        result["message"] = "提交超时，结果未确认（可能已在服务端生效，请核对预约记录）"
                    elif isinstance(error, requests.exceptions.ConnectionError):
                        result["message"] = "网络中断，提交未送达（请检查网络或代理设置）"
                if not result["success"] and result.get("reason") is None and \
                        seat_held_message(result.get("message", "")):
                    result["reason"] = "already_held"
                if getattr(client, "submit_attempts", None):
                    result["attempts"] = list(client.submit_attempts)
                result["elapsed_ms"] = round((time.perf_counter() - started) * 1000)
                print(json.dumps(result, ensure_ascii=True), flush=True)
                if result["success"] or result.get("reason") == "already_held":
                    if on_success is not None:
                        on_success()
                    covered.append((time_minutes(start), time_minutes(end)))
    missing = uncovered_segments(target_start, target_end, covered)
    if not missing:
        print(json.dumps({"status": "complete", "target": [target_start, target_end]}), flush=True)
        return 0
    print(json.dumps({"status": "partial_success" if covered else "failed",
                      "target": [target_start, target_end], "missing": missing}), flush=True)
    return 1


if __name__ == "__main__":
    sys.exit(main())
