"""Turn the scheduled runner's JSON lines into a reader-friendly report."""

import json
from collections import Counter


def seconds(milliseconds):
    return f"{milliseconds / 1000:.1f} 秒"


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
    skipped = next((event for event in events if "skipped" in event), None)
    login_failure = next((event for event in events if event.get("login_ok") is False), None)
    warnings = [event["warning"] for event in events if "warning" in event]
    date = attempts[0].get("date") if attempts else (skipped or {}).get("date")
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
        target = (outcome or {}).get("target")
        target_text = f"{target[0]}–{target[1]}" if target else "未记录"
        lines.append(f"目标时段：{target_text}  ·  成功 {len(successes)} 段 / 尝试 {len(attempts)} 次")
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
            reasons = Counter(item.get("message") or item.get("error") or "未返回原因" for item in failures)
            lines.extend(["", "失败原因（按尝试次数统计）："])
            lines.extend(f"  {reason} × {count}" for reason, count in reasons.items())
    else:
        lines.append("结果：日志中没有预约结果；请查看下方运行异常。")

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
    lines.extend(["", "请在学习通“我的预约”核对最终记录，并按要求签到。", "原始技术日志：last-run.log"])
    return "\n".join(lines)
