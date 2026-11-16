"""Hand account 2's fully-booked next-day seat over to account 1.

The nightly scheduled runs book the next day separately for every account.  When
account 2 ends up holding the whole target window, this extra step moves that
reservation to account 1 at the configured time (default 22:30) by cancelling
account 2's records and re-booking the same room, seat and time slot with
account 1.  The school merges neighbouring submissions into one record, so a
record is split back into at most four-hour chunks before it is re-submitted.

The decision always uses the school's own reservation list (``curReserves``),
never the per-submit return values:

* account 1 already covers the window -> cancel account 2's records only;
* account 2 covers the window but account 1 does not -> cancel account 1's
  partial records, then move every account-2 record to account 1;
* account 2 does not cover the window -> do nothing.

A chunk that account 1 fails to re-book is written back to account 2 right away
(``restored``), so the two accounts together never lose coverage.  The final
result is verified again and reported as ``complete`` or ``partial``.
"""

import argparse
import datetime as dt
import json
import os
import re
import sys
import time
from pathlib import Path

import scheduled_provenance as provenance
from reservation_records import cancel_reservation
from reserve_segments import (
    LOCAL_TZ,
    _record_span as record_span,
    _solve_captcha as solve_captcha,
    current_reservations,
    emit_line,
    exclusive_file_lock,
    login_with_retry,
    trace_http_timings,
    reservation_fid_enc,
    reservation_plans,
    submit_chunk,
    time_minutes,
    uncovered_segments,
    wait_for_scheduled_runs,
)
from utils import reserve

TRANSFER_KEY = "transferToAccount"
TRANSFER_STATE = "seat-transfer-state.json"
TRANSFER_LOCK = "seat-transfer.lock"
DEFAULT_AT = "22:30"
TRANSFER_TASK_LIMIT = 20 * 60.0
TRANSFER_WAIT_DEADLINE = TRANSFER_TASK_LIMIT - 2 * 60.0
# Never start cancelling when a wait ate most of the task's own time limit: a
# kill in the middle of "cancel -> rebook" would leave the seat without owner.
MIN_TRANSFER_BUDGET = 3 * 60.0
PROCESS_STARTED = time.monotonic()
SUBMIT_TIMEOUT = 8.0
MAX_SUBMIT_ATTEMPTS = 3
READ_TIMEOUT = 10.0


class TransferUnavailable(RuntimeError):
    """The reservation state cannot be trusted (login or query failed)."""


def _hhmm(minutes):
    return f"{minutes // 60:02d}:{minutes % 60:02d}"


def _span_text(span):
    return [_hhmm(span[0]), _hhmm(span[1])]


def _parse_at(value):
    text = str(value or "").strip()
    if not re.fullmatch(r"(?:[01]\d|2[0-3]):[0-5]\d", text):
        raise ValueError(f"{TRANSFER_KEY}.at 需要 HH:MM 时间")
    return text


def transfer_settings(config):
    """Read and normalize the shared ``transferToAccount`` setting.

    ``from``/``to`` are 1-based account numbers so they match the GUI and the
    log files.  A missing setting means "disabled", which keeps old configs
    working unchanged.
    """
    raw = config.get(TRANSFER_KEY)
    if raw is None:
        return {"enabled": False, "from": 2, "to": 1, "at": DEFAULT_AT}
    if not isinstance(raw, dict):
        raise ValueError(f"{TRANSFER_KEY} 必须是对象")
    enabled = raw.get("enabled") is True
    try:
        source = int(raw.get("from", 2))
        target = int(raw.get("to", 1))
    except (TypeError, ValueError):
        raise ValueError(f"{TRANSFER_KEY} 的 from/to 必须是账号序号") from None
    at = _parse_at(raw.get("at", DEFAULT_AT))
    users = config.get("reserve") or []
    if enabled:
        if len(users) < 2:
            raise ValueError("转预约需要至少两个账号")
        for number in (source, target):
            if not 1 <= number <= len(users):
                raise ValueError(f"{TRANSFER_KEY} 的账号序号超出范围")
        if source == target:
            raise ValueError(f"{TRANSFER_KEY} 的 from 和 to 不能是同一个账号")
    return {"enabled": enabled, "from": source, "to": target, "at": at}


def usable_records(records, target_day):
    """The account's still-pending records for *target_day*, sorted by start.

    ``status`` 0 is "待履约"; cancelled or finished records of other days are
    ignored.  A record ending after midnight is clipped to the day by
    ``record_span``.
    """
    parsed = []
    for record in records or []:
        if not isinstance(record, dict):
            continue
        try:
            status = int(record.get("status"))
        except (TypeError, ValueError):
            continue
        if status != 0:
            continue
        span = record_span(record, target_day)
        if span is None:
            continue
        parsed.append({
            "raw": record,
            "id": record.get("id"),
            "span": span,
            "roomid": str(record.get("roomId") or "").strip(),
            "seat": str(record.get("seatNum") or "").strip(),
            "status": status,
        })
    return sorted(parsed, key=lambda item: (item["span"], str(item["id"])))


def window_missing(window, records):
    """Chunks of *window* that *records* do not fully cover."""
    return uncovered_segments(window[0], window[1], [item["span"] for item in records])


def overlaps_window(record, window):
    begin, finish = record["span"]
    return begin < time_minutes(window[1]) and time_minutes(window[0]) < finish


def plan_window(user, target_day):
    """The account's primary next-day window as (start, end) HH:MM strings."""
    plan = reservation_plans(user, {}, target_day.strftime("%A"))[0]
    return [plan["segments"][0][0], plan["segments"][-1][1]]


def _login_client(username, password, target_day):
    tomorrow = dt.date.today() + dt.timedelta(days=1)
    client = reserve(sleep_time=0.2, max_attempt=2, enable_slider=True,
                     reserve_next_day=(target_day == tomorrow))
    trace_http_timings(client.requests)
    logged_in, message = login_with_retry(
        client, {"username": username, "password": password}, {},
        deadline=dt.datetime.now() + dt.timedelta(seconds=30))
    if not logged_in:
        raise TransferUnavailable(f"登录失败：{message or '未知原因'}")
    client.requests.headers.update({"Host": "office.chaoxing.com"})
    return client


def _read_records(client, user, config, target_day, timeout=READ_TIMEOUT):
    """The school's reservation list for *target_day*, or None when unreadable."""
    try:
        roomid = reservation_plans(user, {}, target_day.strftime("%A"))[0]["roomid"]
        fid_enc = (reservation_fid_enc(client, roomid, target_day)
                   or user.get("deptIdEnc") or config.get("deptIdEnc"))
        return current_reservations(client, fid_enc, timeout=timeout)
    except Exception:
        return None


def _verify_day(client, user, config, target_day, window):
    """Read and classify one account's coverage of *window*."""
    records = _read_records(client, user, config, target_day)
    if records is None:
        return None
    usable = usable_records(records, target_day)
    missing = window_missing(window, usable)
    return {"usable": usable, "missing": missing,
            "verified": "complete" if not missing else ("partial" if usable else "none"),
            "coverage": [item["span"] for item in usable]}


def _cancel(client, record, account, emit):
    """Cancel one record; the school may refuse a started or stale one."""
    event = {"account": account, "id": record["id"], "time": _span_text(record["span"]),
             "seat": record["seat"], "room": record["roomid"]}
    try:
        cancel_reservation(client, record["id"])
    except Exception as error:
        emit(json.dumps({"transfer": "cancel_failed", **event,
                         "error": type(error).__name__, "message": str(error)[:200]},
                        ensure_ascii=True))
        return False
    emit(json.dumps({"transfer": "cancelled", **event}, ensure_ascii=True))
    return True


def _book_chunk(client, chunk, roomid, seat, captcha_box):
    """Submit one chunk with a fresh page token, retrying a few times.

    The seat may need a moment to become free after account 2's cancellation,
    and a timed-out submit may still land server-side.  The authoritative answer
    is the final reservation read, not this return value.
    """
    result = {"success": False, "reason": None, "message": "", "outcome": "rejected"}
    for attempt in range(MAX_SUBMIT_ATTEMPTS):
        captcha = captcha_box.get("value")
        if client.enable_slider and not captcha:
            captcha = solve_captcha(client)
            captcha_box["value"] = captcha
        try:
            result = submit_chunk(client, chunk, roomid, seat, captcha or "",
                                  timeout=SUBMIT_TIMEOUT)
        except Exception as error:
            result = {"success": False, "reason": None, "outcome": "unknown",
                      "message": type(error).__name__}
        if result.get("success") or result.get("reason") == "already_held":
            return result
        if "验证" in str(result.get("message") or ""):
            captcha_box["value"] = ""
        if attempt + 1 < MAX_SUBMIT_ATTEMPTS:
            time.sleep(0.8)
    return result


def _book_chunks(client, record, chunks, captcha_box, account, emit):
    booked = []
    for chunk in chunks:
        result = _book_chunk(client, chunk, record["roomid"], record["seat"],
                             captcha_box)
        event = {"transfer": "booked", "account": account, "time": list(chunk),
                 "seat": record["seat"], "room": record["roomid"],
                 "success": bool(result.get("success")),
                 "reason": result.get("reason"),
                 "message": result.get("message", "")}
        if result.get("outcome") == "unknown":
            event["outcome"] = "unknown"
        if result.get("error"):
            event["error"] = result["error"]
        emit(json.dumps(event, ensure_ascii=True))
        booked.append(event)
    return booked


def _record_chunks(record, covered=()):
    """Split one reserved span into re-submittable gaps of at most four hours."""
    start, finish = _span_text(record["span"])
    return uncovered_segments(start, finish, list(covered))


def _outside_chunks(record, window):
    """The parts of *record* outside *window* (the school cancels a record whole)."""
    start, finish = _span_text(record["span"])
    bounds = [(time_minutes(window[0]), time_minutes(window[1]))]
    return uncovered_segments(start, finish, bounds)


def _outside_spans(record, window):
    return [(time_minutes(chunk[0]), time_minutes(chunk[1]))
            for chunk in _outside_chunks(record, window)]


def _inside_chunks(record, window):
    """The chunks of *record* that fall inside the transfer window."""
    start = max(record["span"][0], time_minutes(window[0]))
    finish = min(record["span"][1], time_minutes(window[1]))
    if finish <= start:
        return []
    return uncovered_segments(_hhmm(start), _hhmm(finish), [])


def _confirmed_spans(chunks, events):
    """Spans the school already confirmed (or reported as already held)."""
    spans = []
    for chunk, event in zip(chunks, events):
        if event["success"] or event.get("reason") == "already_held":
            spans.append((time_minutes(chunk[0]), time_minutes(chunk[1])))
    return spans


def _definite_failure(event):
    """True when a submit certainly did not land (timeouts stay unknown)."""
    return (not event["success"]
            and event.get("reason") != "already_held"
            and event.get("outcome") != "unknown")


def _verify_retry(client, user, config, target_day, window, attempts=3,
                  sleep_fn=time.sleep):
    """Read the reservation list, retrying a few times when the query fails."""
    state = None
    for attempt in range(attempts):
        state = _verify_day(client, user, config, target_day, window)
        if state is not None:
            return state
        if attempt + 1 < attempts:
            sleep_fn(1.0)
    return None


def _read_state(config_path):
    try:
        payload = json.loads(Path(config_path).with_name(TRANSFER_STATE)
                             .read_text(encoding="utf-8"))
    except (FileNotFoundError, ValueError):
        return {}
    return payload if isinstance(payload, dict) else {}


def _write_state(config_path, payload):
    state_path = Path(config_path).with_name(TRANSFER_STATE)
    temp_path = state_path.with_suffix(".tmp")
    temp_path.write_text(json.dumps(payload, ensure_ascii=True), encoding="utf-8")
    os.replace(temp_path, state_path)


def _base_result(settings, target_day, window):
    return {
        "transfer": "result", "date": str(target_day),
        "at": dt.datetime.now(LOCAL_TZ).strftime("%H:%M:%S"),
        "mode": "none", "result": "skipped", "message": "",
        "window": list(window or []),
        "from": settings["from"], "to": settings["to"],
        "source": {"index": settings["from"], "verified": "unavailable",
                   "remaining": [], "coverage": [], "records": []},
        "target": {"index": settings["to"], "verified": "unavailable",
                   "missing": [], "coverage": [], "records": []},
        "cancelled": [], "booked": [], "restored": [], "wait_timeout": None,
    }


def _fill_account(result, key, state):
    if state is None:
        result[key].update(verified="unavailable", coverage=[], records=[],
                           remaining=[], missing=[])
        return
    result[key]["verified"] = state["verified"]
    result[key]["coverage"] = [_span_text(span) for span in state["coverage"]]
    result[key]["records"] = [
        {"time": _span_text(item["span"]), "seat": item["seat"],
         "room": item["roomid"], "id": item["id"],
         "status": item.get("status")}
        for item in state["usable"]]
    if key == "source":
        result[key]["remaining"] = [list(chunk) for chunk in state["missing"]]
    else:
        result[key]["missing"] = [list(chunk) for chunk in state["missing"]]


def _decide(source_state, target_state):
    """Pick the action from the two verified coverage reports."""
    if target_state["verified"] == "complete":
        return "cancel_source"
    if source_state["verified"] == "complete":
        return "transfer"
    return "none"


def _record_event(account, record):
    return {"account": account, "id": record["id"],
            "time": _span_text(record["span"]), "seat": record["seat"],
            "room": record["roomid"]}


def _planned_actions(mode, source_state, target_state, window, settings):
    """Read-only description of what a real run would do."""
    plan = {"mode": mode, "cancels": [], "books": [], "rebooks_outside": []}
    if mode == "cancel_source":
        records = [record for record in source_state["usable"]
                   if overlaps_window(record, window)]
        plan["cancels"] = [_record_event(settings["from"], record)
                           for record in records]
        plan["rebooks_outside"] = [
            {"account": settings["from"], "time": list(chunk),
             "seat": record["seat"], "room": record["roomid"]}
            for record in records for chunk in _outside_chunks(record, window)]
    elif mode == "transfer":
        target_records = [record for record in target_state["usable"]
                          if overlaps_window(record, window)]
        source_records = [record for record in source_state["usable"]
                          if overlaps_window(record, window)]
        plan["cancels"] = ([_record_event(settings["to"], record)
                            for record in target_records]
                           + [_record_event(settings["from"], record)
                              for record in source_records])
        plan["rebooks_outside"] = [
            {"account": account, "time": list(chunk), "seat": record["seat"],
             "room": record["roomid"]}
            for account, records in ((settings["to"], target_records),
                                     (settings["from"], source_records))
            for record in records for chunk in _outside_chunks(record, window)]
        plan["books"] = [{"account": settings["to"], "time": list(chunk),
                          "seat": record["seat"], "room": record["roomid"]}
                         for record in source_records
                         for chunk in _inside_chunks(record, window)]
    return plan


def transfer_day(config, config_path, target_day, *, settings=None, emit=emit_line,
                 client_factory=None, check_only=False, force=False,
                 wait_seconds=0.0):
    """Run one transfer decision for *target_day* and return the result event.

    Everything the school says is read back through *client_factory* (injectable
    for offline tests); ``check_only`` performs the same reads and reports the
    planned action without cancelling or submitting anything.
    """
    settings = settings or transfer_settings(config)
    users = config.get("reserve") or []
    source_user = users[settings["from"] - 1]
    target_user = users[settings["to"] - 1]
    window = plan_window(source_user, target_day)
    result = _base_result(settings, target_day, window)
    emit(json.dumps({"transfer": "start", "date": str(target_day),
                     "from": settings["from"], "to": settings["to"],
                     "window": window, "at": settings["at"]},
                    ensure_ascii=True))

    state = _read_state(config_path)
    if not force and not check_only and state.get("date") == str(target_day) \
            and state.get("result") == "complete":
        result["message"] = "今天已经完成过转预约，已跳过"
        emit(json.dumps(result, ensure_ascii=True))
        return result

    if wait_seconds > 0:
        emit(json.dumps({"transfer": "waiting",
                         "accounts": list(range(1, len(users) + 1))},
                        ensure_ascii=True))
        pending = wait_for_scheduled_runs(
            config_path, target_day, list(range(1, len(users) + 1)),
            wait_seconds=wait_seconds)
        if pending:
            result["wait_timeout"] = pending
            emit(json.dumps({"transfer": "wait_timeout", "pending": pending},
                            ensure_ascii=True))
        budget = TRANSFER_TASK_LIMIT - (time.monotonic() - PROCESS_STARTED)
        if budget < MIN_TRANSFER_BUDGET:
            result["result"] = "skipped"
            result["message"] = (f"等待账号定时任务结束后只剩 {int(budget)} 秒，"
                                 "为避免任务被系统终止时座位没有归属，本次未执行转预约")
            emit(json.dumps(result, ensure_ascii=True))
            return result

    factory = client_factory or _login_client
    try:
        source_client = factory(source_user["username"], source_user["password"],
                                target_day)
        target_client = factory(target_user["username"], target_user["password"],
                                target_day)
    except TransferUnavailable as error:
        result.update(result="unavailable", message=str(error))
        emit(json.dumps(result, ensure_ascii=True))
        return result
    except Exception as error:
        result.update(result="unavailable",
                      message=f"登录失败：{type(error).__name__}: {error}")
        emit(json.dumps(result, ensure_ascii=True))
        return result

    source_state = _verify_day(source_client, source_user, config, target_day, window)
    target_state = _verify_day(target_client, target_user, config, target_day, window)
    if source_state is None or target_state is None:
        result.update(result="unavailable",
                      message="无法读取学习通预约记录，未做任何取消或预约")
        emit(json.dumps(result, ensure_ascii=True))
        return result
    _fill_account(result, "source", source_state)
    _fill_account(result, "target", target_state)
    mode = _decide(source_state, target_state)
    evidence = provenance.read(config_path)
    source_records = [r for r in source_state["usable"] if overlaps_window(r, window)]
    target_records = [r for r in target_state["usable"] if overlaps_window(r, window)]
    required = [(source_user, source_records)]
    if mode == "transfer":
        required.append((target_user, target_records))
    untrusted = any(not provenance.eligible(evidence, user["username"], target_day, r["raw"])
                    for user, records in required for r in records)
    if mode != "none" and (not source_records or untrusted
                            or source_state["verified"] != "complete"):
        result["message"] = "仅处理定时预约成功记录：缺少来源凭据、记录已变化或来源未约满，已跳过"
        if check_only:
            emit(json.dumps({"transfer": "plan", "mode": "none", "cancels": [],
                             "books": [], "rebooks_outside": []}, ensure_ascii=True))
        emit(json.dumps(result, ensure_ascii=True))
        return result
    result["mode"] = mode
    emit(json.dumps({"transfer": "state", "date": str(target_day), "mode": mode,
                     "window": window, "source": result["source"],
                     "target": result["target"]}, ensure_ascii=True))

    if check_only:
        plan = _planned_actions(mode, source_state, target_state, window, settings)
        emit(json.dumps({"transfer": "plan", **plan}, ensure_ascii=True))
        result["message"] = {"cancel_source": "只检查：账号1已约满，将只取消账号2的预约",
                             "transfer": "只检查：账号2已约满，将先取消账号1部分记录，"
                                         "再把账号2的预约转给账号1",
                             "none": "只检查：账号2没有约满，不会执行转预约"}[mode]
        emit(json.dumps(result, ensure_ascii=True))
        return result
    if mode == "none":
        result["message"] = "账号2没有约满且账号1也未约满，本次未做任何更改"
        emit(json.dumps(result, ensure_ascii=True))
        return result

    if mode == "cancel_source":
        failed = 0
        outside_failed = 0
        source_captcha = {}
        for record in source_state["usable"]:
            if not overlaps_window(record, window):
                continue
            if not _cancel(source_client, record, settings["from"], emit):
                failed += 1
                continue
            result["cancelled"].append(_record_event(settings["from"], record))
            # A record may reach outside the window; cancelling it as a whole
            # would drop that part, so book the outside part back right away.
            outside_failed += _rebook_outside(
                source_client, record, window, source_captcha, settings["from"],
                result, emit)
        source_after = _verify_retry(source_client, source_user, config, target_day,
                                     window)
        _fill_account(result, "source", source_after)
        remaining = ([record for record in source_after["usable"]
                      if overlaps_window(record, window)]
                     if source_after is not None else None)
        if remaining is not None and not remaining and not outside_failed:
            result["result"] = "complete"
            result["message"] = "账号1已约满；已取消账号2的预约"
        elif failed:
            result["result"] = "partial"
            result["message"] = "账号1已约满，但有账号2的预约取消失败，请到学习通核对"
        elif outside_failed:
            result["result"] = "partial"
            result["message"] = "账号1已约满，但账号2窗口外时段补回失败，请到学习通核对"
        else:
            result["result"] = "partial"
            result["message"] = "账号1已约满，但账号2仍有该窗口内的预约，请到学习通核对"
    else:
        _transfer_records(source_client, target_client, source_user, target_user,
                          config, target_day, source_state, target_state, window,
                          settings, result, emit)

    if result["result"] in ("complete", "partial"):
        try:
            _write_state(config_path, {"date": str(target_day),
                                       "result": result["result"],
                                       "at": result["at"],
                                       "mode": result["mode"]})
        except OSError as error:
            # Never report a finished transfer as failed just because the guard
            # file could not be written; it only prevents a second run.
            emit(json.dumps({"transfer": "warning",
                             "message": f"写入防重复状态失败：{error}"},
                            ensure_ascii=True))
    emit(json.dumps(result, ensure_ascii=True))
    return result


def _rebook_outside(client, record, window, captcha_box, account, result, emit):
    """After cancelling a whole record, book back the part outside the window.

    The school cancels a reservation record as a whole, so a record that starts
    before or ends after the transfer window would silently lose that part.
    Booking it back to the same account keeps "window外不动" true in effect.
    Returns how many chunks could not be booked back.
    """
    chunks = _outside_chunks(record, window)
    if not chunks:
        return 0
    events = _book_chunks(client, record, chunks, captcha_box, account, emit)
    result["restored"].extend(events)
    return sum(1 for event in events
               if not (event["success"] or event.get("reason") == "already_held"))


def _transfer_records(source_client, target_client, source_user, target_user, config,
                      target_day, source_state, target_state, window, settings,
                      result, emit):
    """Move account 2's records to account 1, restoring chunks that fail.

    Account 1's own partial records are cancelled first: a re-book of an
    overlapping slot is rejected with "当前时间段您已有预约".  A cancelled record
    that reaches outside the window gets its outside part booked back to its
    owner, and every window chunk account 1 does not end up holding is booked
    back to account 2, so the two accounts never lose coverage.
    """
    target_captcha = {}
    source_captcha = {}
    outside_failed = 0

    for record in target_state["usable"]:
        if not overlaps_window(record, window):
            continue
        if not _cancel(target_client, record, settings["to"], emit):
            continue
        result["cancelled"].append(_record_event(settings["to"], record))
        outside_failed += _rebook_outside(target_client, record, window,
                                          target_captcha, settings["to"], result,
                                          emit)

    moved = []
    restored_spans = []
    confirmed_spans = []
    for record in source_state["usable"]:
        if not overlaps_window(record, window):
            continue
        if not _cancel(source_client, record, settings["from"], emit):
            continue
        result["cancelled"].append(_record_event(settings["from"], record))
        moved.append(record)
        outside_failed += _rebook_outside(source_client, record, window,
                                          source_captcha, settings["from"], result,
                                          emit)
        chunks = _inside_chunks(record, window)
        booked = _book_chunks(target_client, record, chunks, target_captcha,
                              settings["to"], emit)
        result["booked"].extend(booked)
        confirmed_spans.extend(_confirmed_spans(chunks, booked))
        if all(event["success"] for event in booked):
            continue
        # Some submits looked failed: only the school's records decide what
        # really landed.  Give back what certainly failed even when the read
        # itself is unavailable, so a cancelled chunk cannot just disappear.
        check = _verify_retry(target_client, target_user, config, target_day, window)
        if check is None:
            failed_chunks = [chunk for chunk, event in zip(chunks, booked)
                             if _definite_failure(event)]
            if failed_chunks:
                restored_spans.extend(
                    _restore_chunks(source_client, record, failed_chunks,
                                    source_captcha, settings["from"], result, emit))
            continue
        covered = (_outside_spans(record, window) + list(check["coverage"])
                   + restored_spans + confirmed_spans)
        gaps = _record_chunks(record, covered)
        if gaps:
            restored_spans.extend(
                _restore_chunks(source_client, record, gaps, source_captcha,
                                settings["from"], result, emit))

    target_after = _verify_retry(target_client, target_user, config, target_day, window)
    source_after = _verify_retry(source_client, source_user, config, target_day, window)
    _fill_account(result, "target", target_after)
    _fill_account(result, "source", source_after)
    if target_after is None:
        result.update(result="partial",
                      message="转预约已提交，但无法重新读取预约记录；确定失败的时段已约回"
                              "账号2，其余请到学习通核对")
        return
    if not target_after["missing"] and not outside_failed:
        result["result"] = "complete"
        result["message"] = "账号2的预约已全部转给账号1"
        return
    # Final safety net: restore everything account 1 does not hold.
    for record in moved:
        covered = (_outside_spans(record, window) + list(target_after["coverage"])
                   + restored_spans + confirmed_spans)
        gaps = _record_chunks(record, covered)
        if gaps:
            restored_spans.extend(
                _restore_chunks(source_client, record, gaps, source_captcha,
                                settings["from"], result, emit))
    final_target = _verify_retry(target_client, target_user, config, target_day, window)
    final_source = _verify_retry(source_client, source_user, config, target_day, window)
    _fill_account(result, "target", final_target)
    _fill_account(result, "source", final_source)
    result["result"] = "partial"
    result["message"] = ("账号1未能约满，未成功的时段已尽量用账号2约回；请到学习通核对"
                         if not outside_failed else
                         "账号1未能约满，且窗口外时段补回失败，请到学习通核对")


def _restore_chunks(source_client, record, chunks, captcha_box, account, result, emit):
    events = _book_chunks(source_client, record, chunks, captcha_box, account, emit)
    result["restored"].extend(events)
    spans = []
    for chunk, event in zip(chunks, events):
        if event["success"] or event.get("reason") == "already_held":
            spans.append((time_minutes(chunk[0]), time_minutes(chunk[1])))
    return spans


def wait_for_transfer_time(settings, now_fn=dt.datetime.now, sleep_fn=time.sleep,
                           max_wait=600.0):
    """Wait the last minutes before the configured time; skip when it is too early.

    The Windows task fires exactly at the configured time, but a manual run or a
    clock that is still early must not transfer before the account tasks had a
    chance to finish.  Returns True when the transfer may run now.
    """
    try:
        at = dt.datetime.strptime(settings["at"], "%H:%M")
    except ValueError:
        return True
    now = now_fn()
    remaining = (now.replace(hour=at.hour, minute=at.minute, second=0,
                             microsecond=0) - now).total_seconds()
    if remaining <= 0:
        return True
    if remaining <= max_wait:
        sleep_fn(remaining)
        return True
    return False


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Move account 2's fully-booked next-day seat to account 1")
    parser.add_argument("-u", "--user", help="config.local.json path")
    parser.add_argument("--check-only", action="store_true",
                        help="only read the reservations and print the planned action")
    parser.add_argument("--force", action="store_true",
                        help="ignore the already-transferred state and the configured time")
    parser.add_argument("--no-wait", action="store_true",
                        help="do not wait for the scheduled account runs")
    parser.add_argument("--date", help="target day YYYY-MM-DD (default: tomorrow)")
    args = parser.parse_args(argv)

    if not args.user:
        parser.error("-u/--user is required")
    try:
        config = json.loads(Path(args.user).read_text(encoding="utf-8"))
    except (OSError, ValueError) as error:
        print(json.dumps({"transfer": "error", "message": f"读取配置失败：{error}"},
                         ensure_ascii=True), flush=True)
        return 1

    today = dt.date.today()
    try:
        target_day = (dt.date.fromisoformat(args.date) if args.date
                      else today + dt.timedelta(days=1))
    except ValueError:
        print(json.dumps({"transfer": "error", "message": "日期格式应为 YYYY-MM-DD"},
                         ensure_ascii=True), flush=True)
        return 1
    if target_day not in (today, today + dt.timedelta(days=1)):
        print(json.dumps({"transfer": "error", "message": "只处理今天或明天的预约"},
                         ensure_ascii=True), flush=True)
        return 1

    try:
        settings = transfer_settings(config)
    except ValueError as error:
        print(json.dumps({"transfer": "error", "message": str(error)},
                         ensure_ascii=True), flush=True)
        return 1
    if not settings["enabled"]:
        print(json.dumps({"transfer": "result", "date": str(target_day),
                          "at": dt.datetime.now(LOCAL_TZ).strftime("%H:%M:%S"),
                          "mode": "none", "result": "skipped",
                          "message": "转预约未启用"}, ensure_ascii=True), flush=True)
        return 0

    if not args.check_only and not args.force:
        if not wait_for_transfer_time(settings):
            print(json.dumps({"transfer": "result", "date": str(target_day),
                              "at": dt.datetime.now(LOCAL_TZ).strftime("%H:%M:%S"),
                              "mode": "none", "result": "skipped",
                              "message": f"还没到设定的转预约时间 {settings['at']}"},
                             ensure_ascii=True), flush=True)
            return 0

    wait_seconds = 0.0
    if not args.check_only and not args.no_wait:
        wait_seconds = max(0.0, TRANSFER_WAIT_DEADLINE
                           - (time.monotonic() - PROCESS_STARTED))

    lock_path = Path(args.user).with_name(TRANSFER_LOCK)
    try:
        with exclusive_file_lock(lock_path):
            result = transfer_day(config, args.user, target_day, settings=settings,
                                  check_only=args.check_only, force=args.force,
                                  wait_seconds=wait_seconds)
    except Exception as error:
        print(json.dumps({"transfer": "result", "date": str(target_day),
                          "mode": "none", "result": "error",
                          "message": f"{type(error).__name__}: {error}"},
                         ensure_ascii=True), flush=True)
        return 1
    return 0 if result.get("result") in ("complete", "skipped") else 1


if __name__ == "__main__":
    sys.exit(main())
