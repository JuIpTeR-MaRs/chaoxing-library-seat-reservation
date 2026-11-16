"""Pod reservations for the school's resource reservation app."""
import argparse
import datetime as dt
import json
import re
import time
from pathlib import Path

from utils import reserve

BASE = "https://reserve.chaoxing.com"
TZ = dt.timezone(dt.timedelta(hours=8))
DEFAULT_URL = BASE + "/front/web/apps/reservepc/index?reserveId=15133&fidEnc=5f39e4d995092852"
# Verified against this school's resource list; do not classify by maxUserNum
# (the server reports maxUserNum=20 even for these single-person pods).
SINGLE_IDS = {"38541", "38542", "38543", "38544"}
MULTI_IDS = {"30835", "31055"}
POD_IDS = SINGLE_IDS | MULTI_IDS
MULTI_STARTS = ("08:20", "10:00", "12:20", "14:30", "16:10", "18:30", "20:10")

WEEKDAYS = ("Sunday", "Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday")


def validate_schedule(settings):
    at = settings.get("scheduleAt", "22:00")
    if not re.fullmatch(r"(?:22|23):[0-5]\d", at):
        raise ValueError("静音舱次日预约 22:00 开放，请选择 22:00—23:59 的执行时间。")
    days = settings.get("daysofweek", list(WEEKDAYS))
    if not days or any(day not in WEEKDAYS for day in days):
        raise ValueError("请至少选择一个有效的执行星期（预约次日）。")
    return at, days


def can_cancel_record(record):
    """Match reservepc's mineCancelShow rather than the seat status rules."""
    return (str(record.get("status")) == "0" and str(record.get("violation")) == "-1"
            and str(record.get("aprvStatusTypeId")) != "-1")


def cancel_includes_team(record):
    return str(record.get("teamLeaderCancel")) == "1" and str(record.get("leader")) == "1"


def parse_entry(url):
    from urllib.parse import urlparse, parse_qs
    parsed = urlparse(url)
    if parsed.scheme != "https" or parsed.hostname != "reserve.chaoxing.com":
        raise ValueError("请填写 reserve.chaoxing.com 的 HTTPS 静音舱链接。")
    params = parse_qs(parsed.query)
    rid, fid = params.get("reserveId", [""])[0], params.get("fidEnc", [""])[0]
    if rid != "15133" or fid != "5f39e4d995092852":
        raise ValueError("首版仅支持当前学校静音舱入口（reserveId=15133）。")
    return rid, fid


def validate_plan(settings):
    parse_entry(settings.get("url", DEFAULT_URL))
    if str(settings.get("itemId", "")) not in POD_IDS:
        raise ValueError("请选择 1—6 号静音舱（1、6 号为多人舱）。")
    start = settings.get("start", "")
    multi = str(settings["itemId"]) in MULTI_IDS
    if multi and start not in MULTI_STARTS:
        raise ValueError("请选择多人舱的学校固定 90 分钟时段。")
    if not multi and not re.fullmatch(r"(?:[01]\d|2[0-2]):00", start):
        raise ValueError("请选择整点开始的一小时时段（不可跨日）。")
    reason = settings.get("remark", "").strip()
    if not reason or len(reason.encode("utf-16-le")) // 2 > 300:
        raise ValueError("请填写 1—300 字的真实预约事由。")
    return start, (dt.datetime.strptime(start, "%H:%M") + dt.timedelta(minutes=90 if multi else 60)).strftime("%H:%M"), reason


def date_bounds(day, start, end):
    return [dt.datetime.fromisoformat(f"{day}T{x}").replace(tzinfo=TZ) for x in (start, end)]


class PodClient:
    def __init__(self, username, password, entry=DEFAULT_URL):
        self.reserve_id, self.fid = parse_entry(entry)
        self.client = reserve(max_attempt=1)
        self.session = self.client.requests
        original = self.session.request
        def bounded(method, url, **kwargs):
            kwargs.setdefault("timeout", 15)
            return original(method, url, **kwargs)
        self.session.request = bounded
        self.client.get_login_status()
        ok, message = self.client.login(username, password)
        if not ok:
            raise RuntimeError(message or "学习通登录失败。")
        self.session.headers.pop("Host", None)
        self.session.headers.update({"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/130.0 Safari/537.36", "Referer": entry})
        page = self.session.get(BASE + "/front/web/apps/reservepc/index", params={"reserveId": self.reserve_id, "fidEnc": self.fid})
        page.raise_for_status()
        match = re.search(r"reserveIdEnc\s*:\s*['\"]([^'\"]+)", page.content.decode("utf-8"))
        if not match:
            raise RuntimeError("未取得静音舱登录页面标识，请检查账号权限或入口。")
        self.enc = match.group(1)

    def request(self, path, data=None, post=False):
        response = (self.session.post(BASE + "/data/apps/reserve/" + path, data=data) if post else
                    self.session.get(BASE + "/data/apps/reserve/" + path, params=data))
        response.raise_for_status()
        try:
            result = json.loads(response.content.decode("utf-8"))
        except (ValueError, UnicodeError) as error:
            raise RuntimeError("静音舱接口未返回 JSON，登录可能失效。") from error
        if not result.get("success"):
            raise RuntimeError(result.get("msg") or "静音舱请求失败。")
        return result.get("data") or {}

    def params(self, **extra):
        return dict(reserveId=self.reserve_id, reserveIdEnc=self.enc, fidEnc=self.fid, **extra)

    def pods(self):
        data = self.request("item/list/time", self.params(resvAppId=self.reserve_id, sw="", sortIds="", startTime="", endTime=""), post=True)
        return [{"id": str(x["id"]), "name": x["name"],
                 "location": next((p.get("value", "") for p in x.get("propertyValues", [])
                                   if p.get("key") == "地点"), "")}
                for x in data["reserveItemList"] if str(x["id"]) in POD_IDS]

    def records(self, current_only=False):
        data = self.request("user/list/myself", self.params())
        keys = ("current",) if current_only else ("current", "history")
        return [r for key in keys for r in data.get(key, []) if str(r.get("itemId")) in POD_IDS]

    def cancel(self, record_id, include_team=False):
        if not str(record_id).isdecimal():
            raise ValueError("静音舱预约记录 ID 无效。")
        current = next((r for r in self.records(current_only=True)
                        if str(r.get("id")) == str(record_id)), None)
        if not current or not can_cancel_record(current):
            raise RuntimeError("静音舱预约状态已变化或不允许取消，请刷新记录。")
        if cancel_includes_team(current) != include_team:
            raise RuntimeError("组队取消范围已变化，请刷新并重新确认。")
        return self.request("item/user/cancel", {"id": str(record_id),
                                                 "allCancel": int(include_team)}, post=True)

    def slots(self, item_id, day):
        if str(item_id) not in POD_IDS:
            raise ValueError("仅支持已核实的 1—6 号静音舱。")
        date = dt.date.fromisoformat(day)
        detail = self.request("item/detail", {"id": item_id, "reserveIdEnc": self.enc, "fidEnc": self.fid})
        item = detail["reserveItemData"]
        # Match the official reservepc/item page: independent item settings
        # override application defaults only when isSpecial == 1.
        mode = item if item.get("isSpecial") == 1 else detail["reserveData"]
        multi = str(item_id) in MULTI_IDS
        if mode.get("multiReserve") == 2 and not multi:
            raise RuntimeError("该舱位实际启用了多人组队预约，本版仅支持单人预约，请在学习通处理。")
        if multi and (mode.get("multiReserve") != 2 or not item.get("isForms")):
            raise RuntimeError("多人舱组队或表单规则已变化，请在学习通核对。")
        if (item.get("isForms") and not multi) or item.get("isAprv") or mode.get("timeModel") != 1:
            raise RuntimeError("该舱位预约流程已变化（表单、审批或时间模式），请在学习通处理。")
        setting = self.request("item/setting", self.params(itemId=item_id))
        # Holiday rules have a separate schema; fail closed rather than book a
        # normal-week interval on a day whose override we have not interpreted.
        if setting.get("hoildays"):
            raise RuntimeError("学校配置了特殊日期时段，请先在学习通核对；本版暂不自动处理特殊日期。")
        intervals = setting.get("commonIntervalMap", {}).get(str(date.isoweekday()), [])
        used = self.request("item/user/list", self.params(itemId=item_id, startTime=day, endTime=day)).get("reserveUserList", [])
        result = []
        for slot in intervals:
            start, end = slot["startTime"], slot["endTime"]
            begin, finish = date_bounds(day, start, end)
            if finish - begin != dt.timedelta(minutes=90 if multi else 60) or slot.get("available") != 1:
                continue
            occupied = sum((1 if multi else r.get("reserveNum", 1) or 1) for r in used if r["starttime"] < finish.timestamp()*1000 and r["endtime"] > begin.timestamp()*1000)
            result.append({"id": slot["id"], "start": start, "end": end,
                           "free": occupied < slot.get("userNum", 1) and begin > dt.datetime.now(TZ)})
        return result

    def matching(self, item_id, day, start, end):
        begin, finish = date_bounds(day, start, end)
        return [r for r in self.records() if str(r.get("itemId")) == str(item_id)
                and r.get("starttime") == int(begin.timestamp()*1000)
                and r.get("endtime") == int(finish.timestamp()*1000)
                and r.get("isCancel", 0) == 0 and r.get("status") not in (7, 8)]

    def book(self, settings, day, team_booking=None):
        start, end, reason = validate_plan(settings)
        target = dt.date.fromisoformat(day)
        now = dt.datetime.now(TZ)
        if target not in (now.date(), now.date() + dt.timedelta(days=1)):
            raise ValueError("仅支持今天或明天的静音舱预约。")
        if target > now.date() and now.time().replace(tzinfo=None) < dt.time(22):
            raise ValueError("次日预约尚未开放，请在今天 22:00 后提交。")
        item_id = str(settings["itemId"])
        if self.matching(item_id, day, start, end):
            return "已有相同时段预约（已核对），未重复提交。"
        slot = next((x for x in self.slots(item_id, day) if x["start"] == start and x["end"] == end), None)
        if not slot or not slot["free"]:
            raise RuntimeError("该时段不开放、已满或已过期。")
        if item_id in MULTI_IDS:
            if team_booking is None:
                raise ValueError("多人舱需要两个账号及完整申请表，请从程序界面或定时任务预约。")
            return team_booking(self, settings, day, slot)
        row = {"itemId": item_id, "starttime": f"{day} {start}", "endtime": f"{day} {end}",
               "today": day, "remark": reason, "intervalIds": json.dumps([slot["id"]]),
               "formUserId": None, "aprvId": None, "aprvStatusTypeId": None}
        data = self.params(itemId=item_id, teamId=0, cycleType=0, startDate="", endDate="", ua="pc", wfwEngineEnc="",
                           formsData="", formUserId="", aprvId="", aprvStatusTypeId="",
                           reserveNum=1, reserveUsers=json.dumps([row], ensure_ascii=False))
        check = self.request("verify/data", {"reserveId": self.reserve_id, "itemId": item_id,
                             "fidEnc": self.fid, "reserveUsers": data["reserveUsers"],
                             "timeModel": 1, "reserveNum": 1, "administrator": "false"}, post=True)
        if check.get("reserveNum", 1) < 1:
            raise RuntimeError("学校校验返回无可预约名额。")
        error = None
        try:
            self.request("multiple/submit", data, post=True)
        except Exception as exc:
            error = exc
        # Never blindly retry an ambiguous submission: the server may have
        # committed it before the response was lost.
        for delay in (0, 1, 2):
            time.sleep(delay)
            try:
                if self.matching(item_id, day, start, end):
                    return f"预约成功（已核对）：{day} {start}–{end}。"
            except Exception:
                pass
        raise RuntimeError(f"提交后未能核实预约结果，请在学习通核对，勿重复提交。{(' 原因：' + str(error)) if error else ''}")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", default=str(Path(__file__).with_name("config.local.json")))
    parser.add_argument("--account", type=int, default=1)
    parser.add_argument("--check-only", action="store_true")
    args = parser.parse_args()
    config = json.loads(Path(args.config).read_text(encoding="utf-8"))
    if not 1 <= args.account <= len(config["reserve"]):
        raise ValueError("账号序号不存在。")
    user = config["reserve"][args.account-1]
    settings = user.get("pod", {})
    if not settings.get("enabled") and not args.check_only:
        print("静音舱定时预约未启用，已跳过。")
        return 0
    start, end, _ = validate_plan(settings)
    if str(settings["itemId"]) in MULTI_IDS:
        from pod_team import validate_team_config
        validate_team_config(config, args.account-1, settings)
    now = dt.datetime.now(TZ)
    target = now.date() + dt.timedelta(days=1)
    at, days = validate_schedule(settings)
    if args.check_only:
        print(json.dumps({"enabled": settings.get("enabled", False), "scheduleAt": at, "daysofweek": days, "date": str(target), "itemId": settings["itemId"], "start": start, "end": end}, ensure_ascii=False))
        return 0
    if WEEKDAYS[(now.weekday()+1) % 7] not in days:
        print("今天不在静音舱执行星期内，已跳过。")
        return 0
    hour, minute = map(int, at.split(":"))
    opening = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if now < opening - dt.timedelta(minutes=2) or now > opening + dt.timedelta(minutes=5):
        raise RuntimeError(f"不在 {at} 定时运行窗口内，已停止，避免预约错误日期。")
    client = PodClient(user["username"], user["password"], settings.get("url", DEFAULT_URL))
    while dt.datetime.now(TZ) < opening:
        time.sleep(min(.2, max(0, (opening-dt.datetime.now(TZ)).total_seconds())))
    from pod_team import book_with_accounts
    print(book_with_accounts(client, config, args.account-1, settings, str(target), Path(args.config)))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(f"静音舱预约失败：{exc}")
        raise SystemExit(1)
