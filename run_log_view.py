"""Turn the scheduled runner's JSON lines into a reader-friendly report."""

import json
from collections import Counter


def seconds(milliseconds):
    return f"{milliseconds / 1000:.1f} 秒"


def failure_reason(item):
    if item.get("reason") == "seat_taken":
        return "该时段已被预约（可能已被他人约走，也可能你自己的请求已生效，请在学习通核对）"
    if item.get("reason") == "already_held":
        return "该时段与已有预约重叠（可能只是部分重叠），不能认定整段已约，请在学习通核对"
    return item.get("message") or item.get("error") or "未返回原因"


def format_verification(event):
    """Render the post-run check of the school's own reservation list."""
    if not event:
        return []
    checked = f"（核对时间 {event['checked_at']}）" if event.get("checked_at") else ""
    lines = ["学习通预约记录核对" + checked + "："]
    status = event.get("verified")
    if status == "unavailable":
        detail = "；".join(str(part) for part in (event.get("message"), event.get("error")) if part)
        lines.append(f"  核对失败：{detail or '未知原因'}；请在学习通“我的预约”查看")
        return lines
    reservations = event.get("reservations") or []
    if reservations:
        states = {0: "待履约", 1: "学习中", 2: "已履约", 3: "暂离中",
                  5: "被监督中", 7: "已取消"}
        lines.append("  预约信息：")
        lines.extend(
            f"    {item['time'][0]}–{item['time'][1]}  {item.get('seat') or '?'} 号座位"
            f"（阅览室 {item.get('room') or '?'}，记录号 "
            f"{item.get('id') if item.get('id') is not None else '未返回'}，"
            f"{states.get(item.get('status'), '状态未知')}）"
            for item in reservations)
    else:
        lines.append("  预约信息：没有查到预约记录")
    missing = event.get("missing") or []
    if missing:
        lines.extend(["", "  计划内仍空缺："])
        lines.extend(f"    {start}–{end}" for start, end in missing)
    conclusion = {"complete": "目标时段已全部预约（已核对）",
                  "partial": "仍有空缺时段（已核对）",
                  "none": "该日没有预约记录（已核对）"}
    lines.extend(["", "  结论：" + conclusion.get(status, "核对结果未知")])
    return lines


def format_transfer_log(raw, modified_at=None):
    """Render ``last-transfer.log`` (account 2 -> account 1 move) for the GUI."""
    events, other_lines = [], []
    for line in raw.splitlines():
        if not line.strip():
            continue
        try:
            value = json.loads(line)
        except (ValueError, TypeError):
            other_lines.append(line)
            continue
        if isinstance(value, dict):
            events.append(value)
        else:
            other_lines.append(line)

    result = next((event for event in reversed(events)
                   if event.get("transfer") == "result"), None)
    heading = "转预约" + (f"  ·  日志更新：{modified_at}" if modified_at else "")
    if result is None:
        lines = [heading, "", "日志中没有转预约结果；请查看下方运行异常。"]
        if other_lines:
            lines += ["", "运行异常：", *other_lines[-12:]]
        return "\n".join(lines)

    labels = {"complete": "完成", "partial": "部分完成", "skipped": "未执行/已跳过",
              "unavailable": "无法核对（未改动预约）", "error": "失败"}
    modes = {"cancel_source": "账号2已成功，账号1也约满：只取消账号2",
             "transfer": "账号2约满：取消账号1部分记录后，把账号2的预约转给账号1",
             "none": "无需操作"}
    lines = [heading, "",
             f"预约日期：{result.get('date') or '未知'}  ·  "
             f"结果：{labels.get(result.get('result'), result.get('result'))}",
             f"账号 {result.get('from', '?')} → 账号 {result.get('to', '?')}  ·  "
             f"模式：{modes.get(result.get('mode'), result.get('mode'))}"
             + (f"  ·  执行时间：{result['at']}" if result.get("at") else "")]
    if result.get("message"):
        lines.append(f"说明：{result['message']}")

    cancelled = result.get("cancelled") or []
    if cancelled:
        lines += ["", "已取消："]
        lines += [f"  账号 {item.get('account')} · {item['time'][0]}–{item['time'][1]} · "
                  f"{item.get('seat') or '?'} 号座位（记录号 {item.get('id')}）"
                  for item in cancelled]
    booked = result.get("booked") or []
    if booked:
        lines += ["", "账号1重新预约："]
        lines += [f"  {item['time'][0]}–{item['time'][1]} · {item.get('seat') or '?'} 号座位 · "
                  f"{'成功' if item.get('success') else '未确认'}"
                  + (f"（{item.get('message')}）" if item.get("message") else "")
                  for item in booked]
    restored = result.get("restored") or []
    if restored:
        lines += ["", "补回/回退（窗口外部分或账号1未约到的时段）："]
        lines += [f"  账号{item.get('account')} · {item['time'][0]}–{item['time'][1]} · "
                  f"{item.get('seat') or '?'} 号座位 · "
                  f"{'已约回' if item.get('success') else '未能约回'}"
                  for item in restored]

    def coverage_lines(key, title, empty_text):
        account = result.get(key) or {}
        coverage = account.get("coverage") or []
        records = account.get("records") or []
        lines.append("")
        lines.append(f"{title}（账号 {account.get('index', '?')}）："
                     f"{'已覆盖' if account.get('verified') == 'complete' else '部分覆盖' if coverage else empty_text}")
        if records:
            lines.append("  预约信息：")
            states = {0: "待履约", 1: "学习中", 2: "已履约", 3: "暂离中",
                      5: "被监督中", 7: "已取消"}
            lines.extend(
                f"    {item['time'][0]}–{item['time'][1]}  {item.get('seat') or '?'} 号座位"
                f"（阅览室 {item.get('room') or '?'}，记录号 "
                f"{item.get('id') if item.get('id') is not None else '未返回'}，"
                f"{states.get(item.get('status'), '状态未知')}）"
                for item in records)
        else:
            for start, end in coverage:
                lines.append(f"  {start}–{end}")
        gaps = account.get("remaining") if key == "source" else account.get("missing")
        if gaps:
            lines.append("  仍空缺：" + "、".join(f"{start}–{end}" for start, end in gaps))

    coverage_lines("target", "账号1现持有", "没有预约")
    coverage_lines("source", "账号2现持有", "没有预约")
    wait_timeout = result.get("wait_timeout")
    if wait_timeout:
        lines += ["", "提示：等待其他账号定时任务结束超时（未完成："
                     + "、".join(f"账号 {index}" for index in wait_timeout) + "）"]

    lines += ["", "请在学习通“我的预约”核对最终记录，并按学校要求签到。"]
    if other_lines:
        lines += ["", "运行异常：", *other_lines[-12:]]
    return "\n".join(lines)


def format_run_log(raw, modified_at=None):
    sections = []
    current = []
    preamble = []
    for line in raw.splitlines():
        try:
            event = json.loads(line)
        except (ValueError, TypeError):
            event = None
        if isinstance(event, dict) and "account" in event and "username" in event:
            if current and isinstance(current[0], dict):
                sections.append(current)
            elif current:
                preamble.extend(current)
            current = [event]
        else:
            current.append(line)
    if sections:
        sections.append(current)
        return "\n\n".join(
            f"账号 {section[0]['account']} · {section[0]['username']}\n" +
            format_run_log("\n".join(section[1:]), modified_at)
            for section in sections) + ("\n\n" + "\n".join(preamble) if preamble else "")
    events = []
    other_lines = []
    for line in raw.splitlines():
        if not line.strip():
            continue
        try:
            value = json.loads(line)
        except json.JSONDecodeError:
            other_lines.append(line)
            continue
        if isinstance(value, dict):
            events.append(value)
        else:
            other_lines.append(line)

    attempts = [event for event in events if "success" in event and "time" in event]
    outcome = next((event for event in reversed(events) if "status" in event), None)
    verified = next((event for event in reversed(events)
                     if event.get("verify") == "account_result"), None)
    verified_state = (verified or {}).get("verified")
    # The school's record list is the authoritative result.  A submit answer that
    # timed out can still have landed, so never show "failed" when the check says
    # the slot is booked.
    authoritative = verified_state in ("complete", "partial", "none")
    skipped = next((event for event in events if "skipped" in event), None)
    login_failure = next((event for event in events if event.get("login_ok") is False), None)
    warnings = [event["warning"] for event in events if "warning" in event]
    date = (attempts[0].get("date") if attempts
            else (verified or {}).get("date") or (skipped or {}).get("date"))
    heading = f"预约日期：{date}" if date else "本次运行"
    if modified_at:
        heading += f"  ·  日志更新：{modified_at}"
    lines = [heading, ""]

    if skipped:
        reasons = {"weekday not configured": "今天未设置定时预约", "user decision": "已按你的调休日设置跳过", "statutory holiday": "预约日为法定节假日，已跳过"}
        lines.append("结果：未提交预约；" + reasons.get(skipped["skipped"], skipped["skipped"]))
        if skipped.get("holiday"):
            lines.append(f"节日：{skipped['holiday']}")
    elif login_failure:
        lines.append("结果：登录失败，未提交预约")
        if login_failure.get("message"):
            lines.append(f"原因：{login_failure['message']}")
    elif attempts:
        successes = [attempt for attempt in attempts if attempt["success"]]
        target = (verified or {}).get("target") or (outcome or {}).get("target")
        target_text = f"{target[0]}–{target[1]}" if target else "未记录"
        lines.append(f"目标时段：{target_text}  ·  接口提交 {len(attempts)} 次，"
                     f"返回成功 {len(successes)} 段")
        if authoritative:
            conclusions = {"complete": "目标时段已全部预约（已核对）",
                           "partial": "部分成功，仍有时段未预约（已核对）",
                           "none": "预约失败，目标时段均未预约（已核对）"}
            lines.append(f"结果：{conclusions[verified_state]}")
        else:
            if outcome and outcome.get("status") == "complete":
                lines.append("结果：目标时段已全部预约")
            elif successes:
                lines.append("结果：部分成功，仍有时段未预约")
            else:
                lines.append("结果：预约失败，目标时段均未预约")
            lines.extend(["", "已预约："])
            lines.extend(f"  {item['time'][0]}–{item['time'][1]}  {item['seatid'][0]} 号座位（记录号 {item.get('reservation_id') or '未返回'}）" for item in successes)
            if not successes:
                lines.append("  无")
            missing = (outcome or {}).get("missing") or []
            if missing:
                lines.extend(["", "未预约时段："])
                lines.extend(f"  {start}–{end}" for start, end in missing)
        failures = [item for item in attempts if not item["success"]]
        if failures:
            if authoritative and verified_state == "complete":
                lines.extend(["", "提示：提交过程中有超时或异常返回，"
                                   "但学习通查询已确认全部约到。"])
            reasons = Counter(failure_reason(item) for item in failures)
            lines.extend(["", "失败原因（按尝试次数统计）："])
            lines.extend(f"  {reason} × {count}" for reason, count in reasons.items())
    elif authoritative:
        conclusions = {"complete": "目标时段已全部预约（已核对）",
                       "partial": "部分成功，仍有时段未预约（已核对）",
                       "none": "预约失败，目标时段均未预约（已核对）"}
        lines.append(f"结果：{conclusions[verified_state]}")
    else:
        lines.append("结果：日志中没有预约结果；请查看下方运行异常。")

    verification = format_verification(verified)
    if verification:
        lines.extend(["", *verification])
    wait_timeout = next((event for event in reversed(events)
                         if event.get("verify") == "wait_timeout"), None)
    if wait_timeout:
        pending = "、".join(f"账号 {index}" for index in wait_timeout.get("pending") or [])
        checked = "、".join(f"账号 {index}" for index in wait_timeout.get("checked_accounts") or [])
        lines.extend(["", f"提示：等待其他账号结束超时（未完成：{pending}）"
                           + (f"；本次只核对了{checked}" if checked else "")])

    if warnings:
        lines.extend(["", "提示：", *(f"  {warning}" for warning in warnings)])
    if other_lines:
        lines.extend(["", "运行异常：", *other_lines[-12:]])
    timings = {event.get("timing"): event.get("elapsed_ms") for event in events
               if event.get("timing") in ("login_page", "login")}
    if timings or attempts:
        lines.extend(["", "执行耗时："])
        if isinstance(timings.get("login_page"), (int, float)):
            lines.append(f"  打开登录页：{seconds(timings['login_page'])}")
        if isinstance(timings.get("login"), (int, float)):
            lines.append(f"  登录：{seconds(timings['login'])}")
        durations = [item["elapsed_ms"] for item in attempts
                     if isinstance(item.get("elapsed_ms"), (int, float))]
        if durations:
            lines.append(f"  预约尝试：共 {len(durations)} 次，平均每次 {seconds(sum(durations) / len(durations))}，最慢 {seconds(max(durations))}")
        slow_requests = sorted((event for event in events if event.get("timing") == "http"
                                and isinstance(event.get("elapsed_ms"), (int, float))),
                               key=lambda event: event["elapsed_ms"], reverse=True)
        if slow_requests:
            slowest = slow_requests[0]
            action = "提交预约" if "/seat/submit" in slowest.get("path", "") else "网络请求"
            lines.append(f"  最慢网络请求：{action} {seconds(slowest['elapsed_ms'])}")
        lines.append("  以上步骤可能互相包含，不能相加当作整次运行时间。")
    if verification:
        lines.extend(["", "以上核对结果由运行结束后自动查询学习通得到；请再到学习通“我的预约”确认，并按要求签到。", "原始技术日志：last-run.log"])
    else:
        lines.extend(["", "请在学习通“我的预约”核对最终记录，并按要求签到。", "原始技术日志：last-run.log"])
    return "\n".join(lines)
