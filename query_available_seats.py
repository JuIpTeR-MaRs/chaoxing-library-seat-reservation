"""List seats free for an entire interval; optionally reserve one today."""

import argparse
import datetime as dt
import json
import re
import sys

from utils import reserve


BASE = "https://office.chaoxing.com"
LOCAL_TZ = dt.timezone(dt.timedelta(hours=8))


def get_json(client, path, params):
    response = client.requests.get(BASE + path, params=params)
    response.raise_for_status()
    response.encoding = "utf-8"
    payload = response.json()
    if not payload.get("success"):
        raise RuntimeError(payload.get("msg") or path)
    return payload["data"]


def list_rooms(client, dept_id_enc):
    """Return every library area visible to the signed-in account."""
    rooms = []
    seen = set()
    for page in range(1, 101):
        data = get_json(client, "/data/apps/seat/room/list", {
            "cpage": page,
            "pageSize": 100,
            "firstLevelName": "",
            "secondLevelName": "",
            "thirdLevelName": "",
            "deptIdEnc": dept_id_enc,
        })
        batch = data.get("seatRoomList", [])
        for room in batch:
            room_id = str(room["id"])
            if room_id not in seen:
                rooms.append(room)
                seen.add(room_id)
        if len(batch) < 100:
            return rooms
    raise RuntimeError("阅览区列表分页超过 100 页，请检查接口返回。")


def available_seats(client, day, roomid, start, end):
    page = client.requests.get(
        BASE + "/front/apps/seat/select",
        params={"id": roomid, "day": day, "backLevel": 2},
    )
    page.raise_for_status()
    match = re.search(r"fidEnc\s*=\s*'([^']+)'", page.text)
    if not match:
        raise RuntimeError("Could not find fidEnc on the seat selection page")
    fid_enc = match.group(1)

    room = get_json(
        client,
        "/data/apps/seat/room/info",
        {"id": roomid, "toDay": day, "fidEnc": fid_enc, "queryReserve": "true"},
    )
    used = get_json(
        client,
        "/data/apps/seat/getusedseatnums",
        {"roomId": roomid, "startTime": start, "endTime": end, "day": day, "fidEnc": fid_enc},
    )
    seat_room = room["seatRoom"]
    numbers = {
        f"{number:03d}"
        for number in range(
            seat_room["startSeatNum"],
            seat_room["startSeatNum"] + seat_room["capacity"],
        )
    }
    disabled = {
        seat["seatNum"]
        for seat in room["seatAttributes"]
        if seat["isReserve"] == 0
    }
    occupied = {seat["seatNum"] for seat in used["seatReserves"]}
    candidates = numbers - disabled - occupied

    target_start = dt.datetime.fromisoformat(f"{day}T{start}").replace(tzinfo=LOCAL_TZ).timestamp() * 1000
    target_end = dt.datetime.fromisoformat(f"{day}T{end}").replace(tzinfo=LOCAL_TZ).timestamp() * 1000
    free = []
    for seat_num in sorted(candidates):
        intervals = get_json(
            client,
            "/data/apps/seat/getusedtimes",
            {"roomId": roomid, "seatNum": seat_num, "day": day, "fidEnc": fid_enc},
        )
        if not any(begin < target_end and target_start < finish for begin, finish in intervals):
            free.append(seat_num)
    return free, seat_room["thirdLevelName"]


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("-u", "--user", default="config.local.json")
    parser.add_argument("--date", required=True)
    parser.add_argument("--roomid", required=True)
    parser.add_argument("--start", required=True)
    parser.add_argument("--end", required=True)
    parser.add_argument("--reserve-seat")
    args = parser.parse_args()
    if dt.datetime.fromisoformat(f"{args.date}T{args.end}") <= dt.datetime.fromisoformat(f"{args.date}T{args.start}"):
        parser.error("End time must be after start time")
    if args.reserve_seat and args.date != dt.date.today().isoformat():
        parser.error("--reserve-seat currently supports today only")

    with open(args.user, encoding="utf-8") as file:
        account = json.load(file)["reserve"][0]
    client = reserve(sleep_time=0.2, max_attempt=2, enable_slider=True, reserve_next_day=False)
    client.get_login_status()
    logged_in, message = client.login(account["username"], account["password"])
    if not logged_in:
        raise RuntimeError(message)
    client.requests.headers.update({"Host": "office.chaoxing.com"})

    free, room_name = available_seats(client, args.date, args.roomid, args.start, args.end)
    print(json.dumps({
        "checked_at": dt.datetime.now(LOCAL_TZ).isoformat(timespec="seconds"),
        "date": args.date,
        "roomid": args.roomid,
        "room_name": room_name,
        "time": [args.start, args.end],
        "available": free,
    }, ensure_ascii=True), flush=True)

    if args.reserve_seat:
        seat_num = args.reserve_seat.zfill(3)
        if seat_num not in free:
            print(json.dumps({"seat": seat_num, "reservation_attempted": False, "reason": "Not available for the full interval"}))
            return 1

        original_post = client.requests.post
        submit_response = None

        def capture_submit(*post_args, **post_kwargs):
            nonlocal submit_response
            response = original_post(*post_args, **post_kwargs)
            url = post_kwargs.get("url") or (post_args[0] if post_args else "")
            if "/seat/submit" in url:
                submit_response = response
            return response

        client.requests.post = capture_submit
        success = client.submit([args.start, args.end], args.roomid, [seat_num], False)
        payload = submit_response.json() if submit_response is not None else {}
        record = (payload.get("data") or {}).get("seatReserve") or {}
        print(json.dumps({
            "seat": seat_num,
            "reservation_attempted": True,
            "success": bool(success),
            "message": payload.get("msg", ""),
            "reservation_id": record.get("id"),
            "record_start": record.get("startTime"),
            "record_end": record.get("endTime"),
        }, ensure_ascii=True), flush=True)
        return 0 if success else 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
