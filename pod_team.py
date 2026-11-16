"""Two-account pod teams and the school's current application form.

Protocol follows reservepc/teamDialog.vue and forms apply 2.197.0.
Only book_with_accounts performs writes to the school service.
"""
import base64
import hashlib
import json
import uuid
import zlib
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from pod_reservation import BASE, MULTI_IDS, PodClient, validate_plan
from reserve_segments import exclusive_file_lock

COLLEGES = ("法学院", "管理学院", "国际学院", "经济学院", "会计学院", "数字经济产业学院",
            "体育学院", "外国语学院", "现代信息产业学院", "信息技术与工程学院", "艺术设计学院", "其他")


def validate_team_config(config, account, settings):
    users = config["reserve"]
    if len(users) < 2 or account not in (0, 1):
        raise ValueError("多人舱使用账号 1 和账号 2，请先保存这两个账号。")
    pair = [users[account], users[1-account]]
    profiles = settings.get("teamProfiles")
    if profiles is not None:
        if not isinstance(profiles, list) or len(profiles) != 2:
            raise ValueError("请填写两个组队账号的资料。")
        pair = [dict(user, podProfile=profiles[index]) for user, index in zip(pair, (account, 1-account))]
    if not all(u.get("username") and u.get("password") for u in pair):
        raise ValueError("两个组队账号都需要保存登录账号和密码。")
    if pair[0]["username"] == pair[1]["username"]:
        raise ValueError("组队需要两个不同的学习通账号。")
    for i, user in enumerate(pair):
        profile = user.get("podProfile", {})
        if not all(str(profile.get(k, "")).strip() for k in ("name", "studentId", "college", "phone")):
            raise ValueError(f"请填写{'发起人' if i == 0 else '同行成员'}的姓名、学号、学院和联系方式。")
        if profile["college"] not in COLLEGES or not str(profile["studentId"]).isdigit():
            raise ValueError("请核对组队成员的学号和学院。")
        if len(str(profile["phone"])) != 11 or not str(profile["phone"]).isdigit():
            raise ValueError("请填写 11 位手机号码。")
    if not settings.get("activity", "").strip() or len(settings["activity"].strip()) > 300:
        raise ValueError("请填写 1—300 字的多人舱研讨主题。")
    return pair


def team_request(client, path, params, post=False):
    url = BASE + "/data/apps/" + path
    response = client.session.post(url, data=params) if post else client.session.get(url, params=params)
    response.raise_for_status()
    result = json.loads(response.content.decode("utf-8"))
    if not result.get("success"):
        raise RuntimeError(result.get("msg") or "静音舱组队失败。")
    return result.get("data") or {}


def office_cookie(client, name):
    # Login to the form adds a second oa_uid cookie on office.chaoxing.com.
    # RequestsCookieJar.get(name) raises CookieConflictError in that case.
    cookies = client.session.cookies
    if isinstance(cookies, dict):
        return cookies.get(name, "")
    candidates = [c for c in cookies if c.name == name and c.domain.lstrip(".") in
                  ("office.chaoxing.com", "chaoxing.com", "reserve.chaoxing.com")]
    candidates.sort(key=lambda c: c.domain.lstrip(".") == "office.chaoxing.com")
    return candidates[-1].value if candidates else ""


def form_context(client, item_id):
    """Read the current form, not the stale customFormConfig on item/detail."""
    detail = client.request("item/detail", {"id": item_id, "reserveIdEnc": client.enc, "fidEnc": client.fid})
    item = detail["reserveItemData"]
    mode = item if item.get("isSpecial") == 1 else detail["reserveData"]
    if mode.get("multiReserve") != 2 or mode.get("timeModel") != 1 or item.get("isAprv") or item.get("isForms") != 1:
        raise RuntimeError("多人舱规则已变化，请在学习通核对。")
    if not mode.get("minUserNum", 0) <= 2 <= mode.get("maxUserNum", 0):
        raise RuntimeError("学校当前组队人数要求不允许两人预约。")
    entry = item.get("pcUrl", "")
    if urlparse(entry).hostname != "office.chaoxing.com":
        raise RuntimeError("学校申请表入口已变化。")
    response = client.session.get(entry)
    response.raise_for_status()
    if urlparse(response.url).hostname != "office.chaoxing.com":
        raise RuntimeError("学校申请表登录失败。")
    params = {k: v[0] for k, v in parse_qs(urlparse(response.url).query).items()}
    params["formId"] = params.get("id", "")
    if str(item.get("formId")) != params["formId"] or not params.get("enc"):
        raise RuntimeError("未取得正确的学校申请表标识。")
    response = client.session.get("https://office.chaoxing.com/data/web/apps/forms/fore/user/verify/info", params=params)
    response.raise_for_status()
    result = json.loads(response.content.decode("utf-8"))
    if not result.get("success") or not result.get("data", {}).get("forms"):
        raise RuntimeError(result.get("msg") or "申请表信息读取失败。")
    info = result["data"]
    forms = info["forms"]
    if str(forms.get("id")) != params["formId"] or info.get("isSubmitValidate"):
        raise RuntimeError(info.get("submitValidateMsg") or "学校不允许提交此申请表。")
    # This integration deliberately handles only the verified simple form.
    if forms.get("userInputShow") or forms.get("collectShow") or forms.get("isArchive"):
        raise RuntimeError("学校申请表增加了提交限制，请在学习通处理。")
    return params, info


def form_values(forms, leader, member, activity):
    """Build the official formIdValueData structure; reject unknown fields."""
    values = {}
    top = {"申请人姓名": leader["name"], "申请人学号或工号": leader["studentId"],
           "申请人学院或部门": leader["college"], "联系方式": leader["phone"], "研讨主题": activity}
    child = {"姓名": member["name"], "学号或工号": member["studentId"], "学院或部门": member["college"]}
    seen = set()
    details = []

    def field(widget, source, index=-1):
        fields = widget.get("fields", [])
        if len(fields) != 1 or widget["compt"] not in ("editinput", "selectbox"):
            raise RuntimeError("学校申请表字段类型已变化。")
        label = fields[0].get("label")
        if label not in source:
            raise RuntimeError(f"学校申请表新增字段：{label}，请在学习通处理。")
        val = source[label]
        if widget["compt"] == "selectbox" and val not in [o.get("title") for o in fields[0].get("options", [])]:
            raise ValueError(f"{label}不在学校当前选项中。")
        group = {"values": [[{"val": val}]], "hasAuthority": True, "isShow": True}
        if index >= 0:
            group.update(inDetailGroupIndex=index, fromDetail=True)
        result = {"compt": widget["compt"], "id": widget["id"], "groupValues": [group]}
        if index < 0:
            result.update(inDetailGroupIndex=-1, fromDetail=False, hasAuthority=True)
        values[str(widget["id"])] = result
        return label

    for widget in forms["formConfig"]:
        kind = widget["compt"]
        if kind == "captiontext":
            continue
        if kind == "detailcombox":
            details.append(widget)
        else:
            seen.add(field(widget, top))
    if seen != set(top) or [x.get("label") for x in details] != ["研讨参与人1信息", "研讨参与人2信息"]:
        raise RuntimeError("学校申请表结构已变化，请在学习通处理。")
    # The form says to list everyone except the applicant. Two accounts mean
    # one participant row; never invent a third participant to fill the form.
    for index, widget in enumerate(details):
        values[str(widget["id"])] = {"compt": "detailcombox", "id": widget["id"],
            "inDetailGroupIndex": -1, "rowNos": [uuid.uuid4().hex] if index == 0 else [],
            "fromDetail": False, "hasAuthority": True, "isShow": True}
        children = widget.get("compts", [])
        if {f.get("label") for x in children for f in x.get("fields", [])} != set(child):
            raise RuntimeError("学校成员表字段已变化。")
        for entry in children:
            field(entry, child, 0)
            if index:
                values[str(entry["id"])]["groupValues"] = []
    return values


def submit_form(client, context, values):
    params, info = context
    forms = info["forms"]
    raw = json.dumps(values, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    # Official apply bundle: UTF-8 -> zlib deflate -> xor 102 -> base64.
    packed = base64.b64encode(bytes(x ^ 102 for x in zlib.compress(raw))).decode("ascii")
    payload = {"formId": forms["id"], "formAppId": params.get("formAppId", ""),
               "version": forms["version"], "ext": params.get("ext", ""), "formData": "",
               "formIdValueData": packed, "uniqueCondition": "[]", "enc": params["enc"],
               "uuid": str(uuid.uuid4()), "t": 1, "checkCode": info.get("eo", ""),
               "submitVersion": forms["updatetime"], "oaUidEnc": office_cookie(client, "oa_uid_enc")}
    uid = office_cookie(client, "oa_uid")
    response = client.session.post("https://office.chaoxing.com/data/apps/forms/fore/user/save",
                                   params={"lookuid": uid}, data=payload)
    response.raise_for_status()
    result = json.loads(response.content.decode("utf-8"))
    if not result.get("success"):
        raise RuntimeError(result.get("msg") or "学校申请表提交失败。")
    post = result.get("data", {}).get("postDataMap", {})
    if not post.get("formUserId") or not post.get("formData") or str(post.get("formId")) != str(forms["id"]):
        raise RuntimeError("申请表已提交，但未取得预约关联信息，请在学习通核对，勿重复提交。")
    return post


def book_with_accounts(client, config, account, settings, day, config_path):
    if str(settings["itemId"]) not in MULTI_IDS:
        return client.book(settings, day)
    pair = validate_team_config(config, account, settings)
    start, end, _ = validate_plan(settings)
    directory = Path(config_path).parent
    # One lock covers both leaders, manual windows and scheduled processes.
    with exclusive_file_lock(directory / "pod-team.lock"):
        partner = PodClient(pair[1]["username"], pair[1]["password"], settings["url"])
        if partner.matching(settings["itemId"], day, start, end):
            return "同行账号已有相同时段预约（已核对），未重复提交。"

        def submit(leader, plan, target, slot):
            context = form_context(leader, plan["itemId"])
            fields = form_values(context[1]["forms"], pair[0]["podProfile"], pair[1]["podProfile"], plan["activity"])
            path = directory / "pod-team-state.json"
            state = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
            key = hashlib.sha256(json.dumps([sorted(u["username"] for u in pair), plan["itemId"], target, slot["start"]]).encode()).hexdigest()
            if key in state:
                raise RuntimeError("这两个账号已尝试过该多人舱时段。请在学习通核对组队、表单和预约记录；为避免重复提交，本次停止。")
            row = {"itemId": plan["itemId"], "starttime": f"{target} {slot['start']}",
                   "endtime": f"{target} {slot['end']}", "today": target,
                   "intervalIds": json.dumps([slot["id"]]), "remark": plan["remark"]}
            checked = leader.request("verify/data", {"reserveId": leader.reserve_id, "itemId": plan["itemId"],
                "fidEnc": leader.fid, "reserveUsers": json.dumps([row], ensure_ascii=False),
                "timeModel": 1, "reserveNum": 1, "administrator": "false"}, post=True)
            if checked.get("reserveNum", 1) < 1:
                raise RuntimeError("学校校验返回无可预约名额。")

            def stage(value):
                state[key] = {"stage": value, "date": target, "itemId": plan["itemId"]}
                temporary = path.with_suffix(".tmp")
                temporary.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
                temporary.replace(path)

            stage("创建队伍中，结果待核对")
            created = team_request(leader, "reservepc/create/team", {"teamId": 0, "fidEnc": leader.fid,
                "reserveId": leader.reserve_id, "itemId": plan["itemId"], "starttime": f"{target} {slot['start']}",
                "endtime": f"{target} {slot['end']}", "remark": plan["remark"], "intervalIds": json.dumps([slot["id"]])})
            team = created["reserveTeam"]
            if not created.get("leader") or not team.get("id") or not team["minUserNum"] <= 2 <= team["maxUserNum"]:
                raise RuntimeError("学校组队信息或人数限制不符合两人预约，请在学习通核对。")
            stage("队伍已创建，加入成员中")
            team_request(partner, "reserve/team/join", {"teamId": team["id"],
                "minUserNum": team["minUserNum"], "maxUserNum": team["maxUserNum"]})
            verified = team_request(leader, "reservepc/create/team", {"teamId": team["id"], "fidEnc": leader.fid,
                "reserveId": leader.reserve_id, "itemId": plan["itemId"]})
            if verified.get("joinNum") != 2 or not verified.get("leader"):
                raise RuntimeError("未核实两个账号组队成功，请在学习通核对。")
            members = team_request(leader, "reservepc/team/user/list", {"teamId": team["id"], "fidEnc": leader.fid})
            expected = {str(office_cookie(leader, "oa_uid")), str(office_cookie(partner, "oa_uid"))}
            actual = {str(u.get("uid")) for u in members.get("teamUsers", [])}
            if "" in expected or len(expected) != 2 or actual != expected:
                raise RuntimeError("队伍成员与两个配置账号不一致，请在学习通核对。")
            team_request(leader, "reservepc/team/verify", {"teamId": team["id"], "itemId": plan["itemId"]}, post=True)
            stage("组队完成，申请表提交中")
            form = submit_form(leader, context, fields)
            stage("申请表已提交，预约提交中")
            error = None
            try:
                leader.request("multiple/submit", leader.params(itemId=plan["itemId"], teamId=team["id"],
                    cycleType=0, ua="pc", formsData=form["formData"], formUserId=form["formUserId"],
                    aprvId="", aprvStatusTypeId=""), post=True)
            except Exception as exc:
                error = exc
            import time
            for delay in (0, 1, 2):
                time.sleep(delay)
                try:
                    if leader.matching(plan["itemId"], target, slot["start"], slot["end"]):
                        stage("预约成功，已核对")
                        return f"多人舱预约成功（已核对，2 人）：{target} {slot['start']}–{slot['end']}。"
                except Exception:
                    pass
            raise RuntimeError(f"多人舱提交后未能核实结果，请在学习通核对，勿重复提交。{(' 原因：'+str(error)) if error else ''}")

        return client.book(settings, day, team_booking=submit)
