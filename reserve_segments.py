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
    roomid = args.roomid or user["roomid"]
    seatid = [args.seatid] if args.seatid else user["seatid"]
    segments = build_segments(args.start or user["time"][0], args.end or user["time"][1])
    if args.check_only:
        print(json.dumps({"roomid": roomid, "seatid": seatid, "segments": segments}, ensure_ascii=True))
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
    all_successful = True
    for start, end in segments:
        client.max_attempt = 2
        submit_response = None
        try:
            success = client.submit([start, end], roomid, seatid, False)
            response = submit_response.json() if submit_response is not None else {}
            record = (response.get("data") or {}).get("seatReserve") or {}
            result = {
                "date": str(today + dt.timedelta(days=1)),
                "time": [start, end],
                "roomid": roomid,
                "seatid": seatid,
                "success": bool(success),
                "message": response.get("msg", ""),
                "reservation_id": record.get("id"),
            }
        except Exception as error:
            result = {
                "date": str(today + dt.timedelta(days=1)),
                "time": [start, end],
                "success": False,
                "error": type(error).__name__,
            }
        print(json.dumps(result, ensure_ascii=True), flush=True)
        all_successful &= result["success"]
    return 0 if all_successful else 1


if __name__ == "__main__":
    sys.exit(main())
