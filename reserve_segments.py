"""Reserve consecutive seat slots of at most four hours each."""

import argparse
import datetime as dt
import json
import sys

from utils import reserve


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


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("-u", "--user", required=True)
    parser.add_argument("--check-only", action="store_true")
    parser.add_argument("--roomid")
    parser.add_argument("--seatid")
    parser.add_argument("--start")
    parser.add_argument("--end")
    parser.add_argument("--ignore-weekday", action="store_true")
    args = parser.parse_args()

    with open(args.user, encoding="utf-8") as file:
        users = json.load(file)["reserve"]
    if len(users) != 1:
        raise ValueError("Exactly one account is supported")
    user = users[0]
    overrides = {key: value for key in ("roomid", "seatid", "start", "end")
                 if (value := getattr(args, key)) is not None}
    plans = reservation_plans(user, overrides)
    if args.check_only:
        print(json.dumps({"plans": plans}, ensure_ascii=True))
        return 0

    today = dt.date.today()
    if not args.ignore_weekday and today.strftime("%A") not in user["daysofweek"]:
        print(json.dumps({"date": str(today), "skipped": "weekday not configured"}))
        return 0

    client = reserve(
        sleep_time=0.2,
        max_attempt=2,
        enable_slider=True,
        reserve_next_day=True,
    )
    client.get_login_status()
    login_ok, message = client.login(user["username"], user["password"])
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
    for plan in plans:
        succeeded = 0
        for start, end in plan["segments"]:
            client.max_attempt = 2
            submit_response = None
            try:
                success = client.submit([start, end], plan["roomid"], plan["seatid"], False)
                response = submit_response.json() if submit_response is not None else {}
                record = (response.get("data") or {}).get("seatReserve") or {}
                result = {
                    "date": str(today + dt.timedelta(days=1)),
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
                    "date": str(today + dt.timedelta(days=1)),
                    "plan": plan["plan"],
                    "time": [start, end],
                    "roomid": plan["roomid"],
                    "seatid": plan["seatid"],
                    "success": False,
                    "error": type(error).__name__,
                }
            print(json.dumps(result, ensure_ascii=True), flush=True)
            succeeded += result["success"]
        if succeeded:
            if succeeded < len(plan["segments"]):
                print(json.dumps({"plan": plan["plan"], "status": "partial_success", "backups_skipped": True}), flush=True)
                return 1
            return 0
    return 1


if __name__ == "__main__":
    sys.exit(main())
