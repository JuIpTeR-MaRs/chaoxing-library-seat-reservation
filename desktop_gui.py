"""Windows desktop UI for querying and reserving ChaoXing library seats."""

import calendar
import datetime as dt
import json
import math
import queue
import re
import subprocess
import sys
import threading
import tkinter as tk
from pathlib import Path
from tkinter import messagebox, ttk
from urllib.parse import parse_qs, urlparse

from query_available_seats import LOCAL_TZ, available_seats, list_rooms
from reservation_records import cancel_reservation, list_reservations, reservation_location
from reserve_segments import TIMES_BY_WEEKDAY_KEY, build_segments
from run_log_view import format_run_log, format_transfer_log
from transfer_seats import TRANSFER_KEY, transfer_settings
from utils import holidays, holiday_update, reserve


ROOT = Path(sys.executable if getattr(sys, "frozen", False) else __file__).resolve().parent
CONFIG_PATH = ROOT / "config.local.json"
ROOMS = {
    "三楼经典文库阅览学习区（11031）": "11031",
    "三楼北面阅览学习区（11032）": "11032",
}
SCHOOL_DEPT_ID_ENC = "5f39e4d995092852"
ADJUSTMENT_WINDOW_DAYS = 60
COLORS = {
    "ink": "#172B32", "forest": "#245E50", "forest_hover": "#1B4D41",
    "canvas": "#F5F3ED", "surface": "#FCFCFA", "line": "#DEDCD4",
    "muted": "#73796F", "soft": "#E8EFE7", "amber": "#A86525",
}
WEEKDAYS = [
    ("Sunday", "周日"), ("Monday", "周一"), ("Tuesday", "周二"),
    ("Wednesday", "周三"), ("Thursday", "周四"),
    ("Friday", "周五"), ("Saturday", "周六"),
]


def manual_default_datetime(now=None):
    now = now or dt.datetime.now(LOCAL_TZ)
    day = now.date() + dt.timedelta(days=now.time() > dt.time(22, 0))
    start = now.replace(minute=30 if now.minute >= 30 else 0, second=0, microsecond=0)
    return day.isoformat(), start.strftime("%H:%M")


def load_config():
    with CONFIG_PATH.open(encoding="utf-8") as file:
        return json.load(file)


def delete_account_config(index):
    """Remove numbered tasks before changing the account positions they use."""
    data = load_config()
    accounts = data["reserve"]
    if not 0 <= index < len(accounts):
        raise ValueError("账号已改变，请重新打开软件后重试。")
    affected = [(number, kind) for number in range(index + 1, len(accounts) + 1)
                for kind in ("reserve", "pod")]
    affected.append((1, "transfer"))
    for number, kind in affected:
        if str(task_status(account=number, kind=kind).get("state", "")).lower() == "running":
            raise ValueError("相关定时任务正在运行，请等待结束后再删除账号。")
    for number, kind in affected:
        task_status("Remove", account=number, kind=kind)
    accounts.pop(index)
    for item in accounts[index:]:
        item["scheduleEnabled"] = False
        if isinstance(item.get("pod"), dict):
            item["pod"]["enabled"] = False
    if not accounts:
        accounts.append({"username": "", "password": "", "roomid": "", "seatid": [],
                         "time": ["11:00", "15:00"], "daysofweek": [],
                         "scheduleEnabled": False, "deptIdEnc": ""})
    data[TRANSFER_KEY] = {**(data.get(TRANSFER_KEY) or {}), "enabled": False, "from": 2, "to": 1}
    data["deptIdEnc"] = accounts[0].get("deptIdEnc", "")
    temporary = CONFIG_PATH.with_suffix(".json.tmp")
    try:
        temporary.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        temporary.replace(CONFIG_PATH)
    finally:
        temporary.unlink(missing_ok=True)
    return min(index, len(accounts) - 1)


def task_status(action="Inspect", at=None, days=None, account=1, kind="reserve"):
    command = ["powershell.exe", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
               "-File", str(ROOT / "schedule_task.ps1"), "-Action", action,
               "-Account", str(account), "-Kind", kind]
    if action == "Set":
        command += ["-At", at, "-Days", ",".join(days)]
    try:
        result = subprocess.run(command, capture_output=True, text=True, timeout=30,
                                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    except subprocess.TimeoutExpired:
        raise RuntimeError("Windows 定时任务响应超时，请刷新任务状态确认是否保存成功。") from None
    if result.returncode:
        detail = result.stderr.strip() or result.stdout.strip()
        with (ROOT / "schedule-task-error.log").open("a", encoding="utf-8") as log:
            log.write(f"{dt.datetime.now().isoformat()} {action} {kind} account={account}\n{detail}\n")
        if any(word in detail.lower() for word in ("0x80041003", "0x80070005", "access", "拒绝", "denied")):
            raise RuntimeError("Windows 拒绝访问定时任务，无法确认或保存任务。请以管理员身份重新打开软件后重试；"
                               "这不代表原任务已丢失。详细原因已记录到 schedule-task-error.log。")
        raise RuntimeError("Windows 定时任务操作未完成，请刷新任务状态后重试。"
                           "详细原因已记录到 schedule-task-error.log。")
    return json.loads(result.stdout)


def parse_room(value):
    value = value.strip()
    if value in ROOMS:
        return ROOMS[value]
    match = re.search(r"(?:（|\()?(\d+)(?:）|\))?$", value)
    if match:
        return match.group(1)
    raise ValueError("请选择阅览区，或输入数字 roomid。")


def parse_dept_id(value):
    value = value.strip()
    if "?" in value:
        value = parse_qs(urlparse(value).query).get("deptIdEnc", [""])[0]
    if not re.fullmatch(r"[0-9a-fA-F]{16}", value):
        raise ValueError("请填写学校选座链接（含 deptIdEnc），或 16 位 deptIdEnc。")
    return value


def format_record_time(milliseconds):
    if milliseconds is None:
        return "未知"
    return dt.datetime.fromtimestamp(milliseconds / 1000, LOCAL_TZ).strftime("%m-%d %H:%M")


class ClockDialog(tk.Toplevel):
    """24-hour clock picker that commits only when confirmed."""

    def __init__(self, parent, value):
        super().__init__(parent)
        self.title("选择时间")
        self.configure(bg=COLORS["surface"])
        self.resizable(False, False)
        self.result = None
        self.hour, self.minute = map(int, value.split(":"))
        self.part = "hour"
        self.dragging = False
        self.transient(parent.winfo_toplevel())
        self.protocol("WM_DELETE_WINDOW", self.destroy)
        self.bind("<Escape>", lambda _event: self.destroy())

        body = tk.Frame(self, bg=COLORS["surface"], padx=22, pady=18)
        body.pack()
        readout = tk.Frame(body, bg=COLORS["surface"])
        readout.pack()
        self.hour_label = tk.Label(readout, bg=COLORS["surface"],
                                   font=("Segoe UI", 25, "bold"), cursor="hand2")
        self.hour_label.pack(side="left")
        self.hour_label.bind("<Button-1>", lambda _event: self._show_part("hour"))
        tk.Label(readout, text=":", bg=COLORS["surface"], fg=COLORS["forest"],
                 font=("Segoe UI", 25, "bold")).pack(side="left")
        self.minute_label = tk.Label(readout, bg=COLORS["surface"],
                                     font=("Segoe UI", 25, "bold"), cursor="hand2")
        self.minute_label.pack(side="left")
        self.minute_label.bind("<Button-1>", lambda _event: self._show_part("minute"))
        self.hint = tk.Label(body, bg=COLORS["surface"], fg=COLORS["muted"],
                             font=("Microsoft YaHei UI", 9))
        self.hint.pack(pady=(2, 8))
        self.dial = tk.Canvas(body, width=272, height=272, bg=COLORS["surface"],
                              highlightthickness=0, cursor="hand2")
        self.dial.pack()
        self.dial.bind("<ButtonPress-1>", self._start_drag)
        self.dial.bind("<B1-Motion>", self._drag)
        self.dial.bind("<ButtonRelease-1>", self._end_drag)
        actions = tk.Frame(body, bg=COLORS["surface"])
        actions.pack(fill="x", pady=(12, 0))
        ttk.Button(actions, text="确定", style="Accent.TButton", command=self._confirm).pack(side="right")
        self._draw()
        self.update_idletasks()
        x = min(parent.winfo_screenwidth() - self.winfo_width(),
                parent.winfo_rootx() + max(0, (parent.winfo_width() - self.winfo_width()) // 2))
        y = parent.winfo_rooty() + parent.winfo_height()
        if y + self.winfo_height() > parent.winfo_screenheight():
            y = max(0, parent.winfo_rooty() - self.winfo_height())
        self.geometry(f"+{x}+{y}")
        self.grab_set()
        self.focus_set()

    def _show_part(self, part):
        self.part = part
        self._draw()

    def _start_drag(self, event):
        self.dragging = self._set_from_pointer(event)

    def _drag(self, event):
        if self.dragging:
            self._set_from_pointer(event)

    def _end_drag(self, event):
        if not self.dragging:
            return
        self._set_from_pointer(event)
        self.dragging = False
        if self.part == "hour":
            self.part = "minute"
            self._draw()

    def _set_from_pointer(self, event):
        dx, dy = event.x - 136, event.y - 136
        radius = math.hypot(dx, dy)
        if radius < 20:
            return False
        step = (math.degrees(math.atan2(dx, -dy)) % 360) / 30
        if self.part == "hour":
            number = round(step) % 12
            self.hour = number if radius >= 91 else (number + 12) % 24
        else:
            self.minute = 0 if dy < 0 else 30
        self._draw()
        return True

    def _draw(self):
        self.hour_label.configure(text=f"{self.hour:02d}",
                                  fg=COLORS["forest"] if self.part == "hour" else COLORS["muted"])
        self.minute_label.configure(text=f"{self.minute:02d}",
                                    fg=COLORS["forest"] if self.part == "minute" else COLORS["muted"])
        self.hint.configure(text="拖动选择小时：外圈 00–11，内圈 12–23" if self.part == "hour"
                            else "拖动选择分钟：仅 00 或 30")
        dial = self.dial
        dial.delete("all")
        dial.create_oval(8, 8, 264, 264, fill=COLORS["soft"], outline="")
        if self.part == "hour":
            angle = math.radians((self.hour % 12) * 30 - 90)
            hand_length = 108 if self.hour < 12 else 70
        else:
            angle = math.radians(self.minute * 6 - 90)
            hand_length = 108
        dial.create_line(136, 136, 136 + hand_length * math.cos(angle),
                         136 + hand_length * math.sin(angle),
                         fill=COLORS["forest"], width=3)
        if self.part == "hour":
            for index in range(12):
                angle = math.radians(index * 30 - 90)
                for ring, hour in ((108, index), (70, (index + 12) % 24)):
                    x, y = 136 + ring * math.cos(angle), 136 + ring * math.sin(angle)
                    chosen = hour == self.hour
                    if chosen:
                        dial.create_oval(x - 17, y - 17, x + 17, y + 17,
                                         fill=COLORS["forest"], outline="")
                    dial.create_text(x, y, text=f"{hour:02d}",
                                     fill="white" if chosen else COLORS["ink"],
                                     font=("Segoe UI", 10, "bold" if chosen else "normal"))
        else:
            for minute, y in ((0, 28), (30, 244)):
                x = 136
                if minute == self.minute:
                    dial.create_oval(x - 18, y - 18, x + 18, y + 18,
                                     fill=COLORS["forest"], outline="")
                dial.create_text(x, y, text=f"{minute:02d}",
                                 fill="white" if minute == self.minute else COLORS["ink"],
                                 font=("Segoe UI", 10, "bold" if minute == self.minute else "normal"))
        dial.create_oval(132, 132, 140, 140, fill=COLORS["forest"], outline="")

    def _confirm(self):
        self.result = f"{self.hour:02d}:{self.minute:02d}"
        self.destroy()


class SeatApp(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("超星图书馆座位预约")
        self.minsize(860, 500)
        self.geometry(f"{min(1320, self.winfo_screenwidth() - 60)}x"
                      f"{min(820, self.winfo_screenheight() - 100)}")
        self.configure(bg=COLORS["canvas"])
        self.events = queue.Queue()
        self.busy = False
        self.last_query = None
        self.room_names = dict(ROOMS)
        self.loaded_dept = None
        self.saved_username = ""
        self.account_index = 0
        self._loading_account = False
        self._loaded_schedule = None
        self._style()
        self._build()
        self._load_fields()
        self._navigate("schedule", refresh=False)
        self.username.trace_add("write", self._account_changed)
        self.protocol("WM_DELETE_WINDOW", self._close)
        self.after(100, self._drain_events)
        self.after(200, self._load_rooms)

    def _style(self):
        style = ttk.Style(self)
        style.theme_use("clam")
        font = ("Microsoft YaHei UI", 10)
        style.configure("TFrame", background=COLORS["canvas"])
        style.configure("Surface.TFrame", background=COLORS["surface"])
        style.configure("Soft.TFrame", background=COLORS["soft"])
        style.configure("TLabel", background=COLORS["canvas"], foreground=COLORS["ink"], font=font)
        style.configure("Surface.TLabel", background=COLORS["surface"], foreground=COLORS["ink"], font=font)
        style.configure("Field.TLabel", background=COLORS["surface"], foreground=COLORS["muted"], font=("Microsoft YaHei UI", 9))
        style.configure("Hint.TLabel", background=COLORS["canvas"], foreground=COLORS["muted"], font=("Microsoft YaHei UI", 9))
        style.configure("SurfaceHint.TLabel", background=COLORS["surface"], foreground=COLORS["muted"], font=("Microsoft YaHei UI", 9))
        style.configure("Section.TLabel", background=COLORS["surface"], foreground=COLORS["ink"], font=("Microsoft YaHei UI", 13, "bold"))
        style.configure("Eyebrow.TLabel", background=COLORS["canvas"], foreground=COLORS["forest"], font=("Microsoft YaHei UI", 9, "bold"))
        style.configure("Soft.TLabel", background=COLORS["soft"], foreground=COLORS["forest"], font=("Microsoft YaHei UI", 9))
        style.configure("TButton", background=COLORS["surface"], foreground=COLORS["ink"], bordercolor=COLORS["line"], font=font, padding=(13, 8))
        style.map("TButton", background=[("active", COLORS["soft"]), ("disabled", COLORS["canvas"])])
        style.configure("Accent.TButton", background=COLORS["forest"], foreground="white", bordercolor=COLORS["forest"], font=("Microsoft YaHei UI", 10, "bold"), padding=(17, 9))
        style.map("Accent.TButton", background=[("active", COLORS["forest_hover"]), ("disabled", COLORS["line"])], foreground=[("disabled", COLORS["muted"])])
        style.configure("TEntry", fieldbackground=COLORS["surface"], bordercolor=COLORS["line"], padding=6)
        style.configure("TCombobox", fieldbackground=COLORS["surface"], bordercolor=COLORS["line"], padding=6)
        style.configure("TCheckbutton", background=COLORS["surface"], foreground=COLORS["ink"], font=font)
        style.configure("TNotebook", background=COLORS["canvas"], borderwidth=0)
        style.configure("TNotebook.Tab", background=COLORS["canvas"], foreground=COLORS["muted"], font=("Microsoft YaHei UI", 10, "bold"), padding=(22, 11), borderwidth=0)
        style.map("TNotebook.Tab", background=[("selected", COLORS["surface"]), ("active", COLORS["soft"])], foreground=[("selected", COLORS["forest"])])
        style.configure("Treeview", background=COLORS["surface"], fieldbackground=COLORS["surface"], foreground=COLORS["ink"], rowheight=36, borderwidth=0, font=font)
        style.configure("Treeview.Heading", background=COLORS["soft"], foreground=COLORS["ink"], font=("Microsoft YaHei UI", 10, "bold"), padding=7)
        style.map("Treeview", background=[("selected", COLORS["soft"])], foreground=[("selected", COLORS["forest"])])
        style.configure("Records.Treeview", background=COLORS["surface"], fieldbackground=COLORS["surface"],
                        foreground=COLORS["ink"], rowheight=54, borderwidth=0,
                        font=("Microsoft YaHei UI", 11))
        style.configure("Records.Treeview.Heading", background=COLORS["surface"],
                        foreground=COLORS["muted"], font=("Microsoft YaHei UI", 10), padding=(8, 11))
        style.map("Records.Treeview", background=[("selected", "#DDEBE3")],
                  foreground=[("selected", COLORS["ink"])])
        style.configure("Cancel.TButton", background=COLORS["surface"], foreground="#9B3F35",
                        bordercolor="#C99088", font=("Microsoft YaHei UI", 10, "bold"), padding=(13, 8))
        style.map("Cancel.TButton", background=[("active", "#F9EDEA"), ("disabled", COLORS["canvas"])],
                  foreground=[("disabled", COLORS["muted"])])
        style.configure("Nav.TButton", background=COLORS["canvas"], foreground=COLORS["ink"],
                        borderwidth=0, padding=(16, 15), anchor="w",
                        font=("Microsoft YaHei UI", 12))
        style.map("Nav.TButton", background=[("active", COLORS["soft"])])
        style.configure("Selected.Nav.TButton", background=COLORS["forest"], foreground="white")
        style.map("Selected.Nav.TButton", background=[("active", COLORS["forest_hover"])],
                  foreground=[("active", "white")])
        style.configure("PageTitle.TLabel", background=COLORS["surface"],
                        foreground=COLORS["ink"], font=("Microsoft YaHei UI", 25, "bold"))

    def _build(self):
        self.username = tk.StringVar()
        self.password = tk.StringVar()
        self.show_password = tk.BooleanVar(value=False)
        self.account_choice = tk.StringVar(value="账号 1")
        shell = ttk.Frame(self, style="Surface.TFrame")
        shell.pack(fill="both", expand=True)
        sidebar = ttk.Frame(shell, padding=(16, 26), width=200)
        sidebar.pack(side="left", fill="y")
        sidebar.pack_propagate(False)
        ttk.Label(sidebar, text="超星图书馆\n座位预约", foreground=COLORS["forest"],
                  font=("Microsoft YaHei UI", 18, "bold"), justify="left").pack(
                      anchor="w", pady=(0, 34))
        self.nav_buttons = {}
        for key, label in (("manual", "手动预约"), ("schedule", "定时预约"),
                           ("records", "我的预约"), ("accounts", "账号管理"), ("logs", "运行日志")):
            button = ttk.Button(sidebar, text=label, style="Nav.TButton",
                                command=lambda page=key: self._navigate(page))
            button.pack(fill="x", pady=4)
            self.nav_buttons[key] = button
        ttk.Label(sidebar, text="超星图书馆座位预约\n本机保存 · 自动预约", style="Hint.TLabel",
                  justify="left").pack(side="bottom", anchor="w")
        workspace = ttk.Frame(shell, style="Surface.TFrame")
        workspace.pack(side="left", fill="both", expand=True)
        topbar = ttk.Frame(workspace, style="Surface.TFrame", padding=(28, 18))
        topbar.pack(fill="x")
        self.header_date = tk.StringVar(value=dt.datetime.now(LOCAL_TZ).strftime("%Y-%m-%d"))
        ttk.Label(topbar, textvariable=self.header_date, style="SurfaceHint.TLabel").pack(side="left")
        self.account_combo = ttk.Combobox(topbar, textvariable=self.account_choice,
                                          values=["账号 1"], state="readonly", width=12)
        self.account_combo.pack(side="right")
        self.account_combo.bind("<<ComboboxSelected>>", self._select_account)
        ttk.Label(topbar, text="当前账号", style="SurfaceHint.TLabel").pack(side="right", padx=12)

        viewport = ttk.Frame(workspace, style="Surface.TFrame")
        viewport.pack(fill="both", expand=True)
        self.content_canvas = tk.Canvas(viewport, bg=COLORS["surface"], highlightthickness=0)
        self.content_canvas.pack(side="left", fill="both", expand=True)
        content_scrollbar = ttk.Scrollbar(viewport, orient="vertical", command=self.content_canvas.yview)
        content_scrollbar.pack(side="right", fill="y")
        self.content_canvas.configure(yscrollcommand=content_scrollbar.set)
        content_xscroll = ttk.Scrollbar(workspace, orient="horizontal", command=self.content_canvas.xview)
        content_xscroll.pack(fill="x", side="bottom")
        self.content_canvas.configure(xscrollcommand=content_xscroll.set)
        outer = ttk.Frame(self.content_canvas, style="Surface.TFrame", padding=(28, 16, 28, 20))
        content_window = self.content_canvas.create_window((0, 0), window=outer, anchor="nw")
        def resize_content(_event=None):
            self.content_canvas.itemconfigure(content_window, width=max(
                self.content_canvas.winfo_width(), outer.winfo_reqwidth()))
            self.content_canvas.configure(scrollregion=self.content_canvas.bbox("all"))
        outer.bind("<Configure>", resize_content)
        self.content_canvas.bind("<Configure>", resize_content)
        self.bind_all("<MouseWheel>", self._scroll_content, add="+")
        account = ttk.Frame(outer, style="Surface.TFrame")
        ttk.Label(account, text="账号管理", style="PageTitle.TLabel").grid(
            row=0, column=0, columnspan=4, sticky="w", pady=(0, 24))
        ttk.Button(account, text="添加账号", command=self._add_account).grid(row=1, column=0, sticky="w")
        ttk.Button(account, text="删除当前账号", command=self._delete_account).grid(row=1, column=1, sticky="w", padx=(14, 0))
        ttk.Label(account, text="学习通账号", style="Field.TLabel").grid(row=2, column=0, sticky="w", pady=(9, 0))
        ttk.Label(account, text="密码", style="Field.TLabel").grid(row=2, column=1, sticky="w", padx=(14, 0), pady=(9, 0))
        ttk.Entry(account, textvariable=self.username).grid(row=3, column=0, sticky="ew", pady=(5, 0))
        self.password_entry = ttk.Entry(account, textvariable=self.password, show="●", width=28)
        self.password_entry.grid(row=3, column=1, sticky="ew", padx=(14, 0), pady=(5, 0))
        ttk.Checkbutton(account, text="显示密码", variable=self.show_password, command=self._toggle_password).grid(row=3, column=2, padx=(12, 0))
        ttk.Button(account, text="保存账号", command=self._save_account).grid(row=3, column=3, padx=(12, 0))
        account.columnconfigure(0, weight=1)
        account.columnconfigure(1, weight=1)
        ttk.Label(account, text="账号信息保存在本机 config.local.json，定时任务使用同一份配置。",
                  style="SurfaceHint.TLabel").grid(row=4, column=0, columnspan=4, sticky="w", pady=(10, 0))

        manual_page = ttk.Frame(outer, style="Surface.TFrame")
        schedule_page = ttk.Frame(outer, style="Surface.TFrame")
        self.pod_panels = {}
        self.resource_tabs = {}
        self.pod_hosts = {}
        seat_tabs = {}
        for mode, page in (("manual", manual_page), ("schedule", schedule_page)):
            tabs = ttk.Notebook(page)
            tabs.pack(fill="both", expand=True)
            seat_tabs[mode] = ttk.Frame(tabs, style="Surface.TFrame", padding=(0, 16))
            host = ttk.Frame(tabs)
            tabs.add(seat_tabs[mode], text="座位")
            tabs.add(host, text="静音舱")
            self.resource_tabs[mode], self.pod_hosts[mode] = tabs, host
            tabs.bind("<<NotebookTabChanged>>", lambda event, mode=mode: self._pod_tab_changed(mode))
        manual_tab, schedule_tab = seat_tabs["manual"], seat_tabs["schedule"]
        records_tab = ttk.Frame(outer, style="Surface.TFrame")
        logs_tab = ttk.Frame(outer, style="Surface.TFrame")
        self.pages = dict(manual=manual_page, schedule=schedule_page, records=records_tab,
                          accounts=account, logs=logs_tab)
        ttk.Label(logs_tab, text="运行日志", style="PageTitle.TLabel").pack(anchor="w", pady=(0, 14))
        ttk.Label(logs_tab, text="分别查看当前账号的预约过程与跨账号转预约结果。",
                  style="SurfaceHint.TLabel").pack(anchor="w", pady=(0, 24))
        ttk.Button(logs_tab, text="查看当前账号预约日志", command=self._show_run_log).pack(anchor="w", pady=8)
        ttk.Button(logs_tab, text="查看转预约日志", command=self._show_transfer_log).pack(anchor="w", pady=8)

        search = ttk.Frame(manual_tab, style="Surface.TFrame")
        search.pack(fill="x", pady=(0, 15))
        ttk.Label(search, text="手动预约", style="PageTitle.TLabel").grid(row=0, column=0, columnspan=4, sticky="w")
        ttk.Label(search, text="学校选座链接或 deptIdEnc", style="Field.TLabel").grid(row=1, column=0, columnspan=4, sticky="w", pady=(13, 0))
        self.dept_id = tk.StringVar()
        self.room = tk.StringVar(value=list(ROOMS)[1])
        default_day, default_start = manual_default_datetime()
        self.day = tk.StringVar(value=default_day)
        self.start = tk.StringVar(value=default_start)
        self.end = tk.StringVar(value="21:00")
        ttk.Entry(search, textvariable=self.dept_id).grid(row=2, column=0, columnspan=4, sticky="ew", pady=(5, 12))
        ttk.Label(search, text="阅览区", style="Field.TLabel").grid(row=3, column=0, sticky="w")
        room_box = ttk.Frame(search, style="Surface.TFrame")
        room_box.grid(row=4, column=0, sticky="ew", padx=(0, 14), pady=(5, 12))
        self.room_combo = ttk.Combobox(room_box, textvariable=self.room, values=list(ROOMS), width=26)
        self.room_combo.pack(side="left", fill="x", expand=True)
        self._block_mouse_wheel(self.room_combo)
        self.refresh_rooms_button = ttk.Button(room_box, text="刷新区域", command=self._load_rooms)
        self.refresh_rooms_button.pack(side="left", padx=(8, 0))
        ttk.Label(search, text="预约日期", style="Field.TLabel").grid(row=3, column=1, sticky="w")
        day_picker, self.day_year, self.day_month, self.day_number = self._date_picker(search, self.day)
        day_picker.grid(row=4, column=1, sticky="w", padx=(0, 8), pady=(5, 12))
        date_actions = ttk.Frame(search, style="Surface.TFrame")
        date_actions.grid(row=5, column=1, columnspan=3, sticky="w", pady=(0, 12))
        ttk.Button(date_actions, text="今天", command=lambda: self._set_day(0)).pack(side="left", padx=(0, 5))
        ttk.Button(date_actions, text="明天", command=lambda: self._set_day(1)).pack(side="left")
        ttk.Label(search, text="开始时间", style="Field.TLabel").grid(row=6, column=0, sticky="w")
        start_picker = self._time_picker(search, self.start)
        start_picker.grid(row=7, column=0, sticky="w", pady=(5, 0))
        ttk.Label(search, text="结束时间", style="Field.TLabel").grid(row=6, column=1, sticky="w")
        end_picker = self._time_picker(search, self.end)
        end_picker.grid(row=7, column=1, sticky="w", pady=(5, 0))
        self.query_button = ttk.Button(search, text="查询可预约座位", style="Accent.TButton", command=self._query)
        self.query_button.grid(row=7, column=2, columnspan=2, sticky="e")
        search.columnconfigure(0, weight=2)
        search.columnconfigure(1, weight=1)

        ttk.Separator(manual_tab).pack(fill="x", pady=(1, 14))
        result_frame = ttk.Frame(manual_tab, style="Surface.TFrame")
        result_frame.pack(fill="both", expand=True)
        ttk.Label(result_frame, text="查询结果", style="Section.TLabel").pack(anchor="w", pady=(0, 5))
        self.summary = tk.StringVar(value="填写条件后点击“查询可预约座位”。")
        ttk.Label(result_frame, textvariable=self.summary, style="SurfaceHint.TLabel").pack(anchor="w", pady=(0, 10))
        tree_area = ttk.Frame(result_frame, style="Surface.TFrame")
        tree_area.pack(fill="both", expand=True)
        self.tree = ttk.Treeview(tree_area, columns=("seat", "state"), show="headings", selectmode="browse")
        self.tree.heading("seat", text="座位号")
        self.tree.heading("state", text="状态")
        self.tree.column("seat", width=140, anchor="center")
        self.tree.column("state", width=220, anchor="center")
        self.tree.pack(side="left", fill="both", expand=True)
        scrollbar = ttk.Scrollbar(tree_area, orient="vertical", command=self.tree.yview)
        scrollbar.pack(side="right", fill="y")
        self.tree.configure(yscrollcommand=scrollbar.set)
        self.tree.bind("<<TreeviewSelect>>", lambda _event: self._update_book_button())
        self.book_button = ttk.Button(result_frame, text="预约选中座位", style="Accent.TButton", command=self._book, state="disabled")
        self.book_button.pack(side="bottom", anchor="e", pady=(11, 0), before=tree_area)

        ttk.Label(records_tab, text="我的预约", style="PageTitle.TLabel").pack(anchor="w")
        self.records_attention = tk.StringVar(value="尚未查询预约")
        self.records_attention_label = tk.Label(
            records_tab, textvariable=self.records_attention, bg=COLORS["surface"],
            fg=COLORS["ink"], font=("Microsoft YaHei UI", 22, "bold"), anchor="w")
        self.records_attention_label.pack(fill="x", pady=(16, 0))
        self.records_summary = tk.StringVar(value="点击“刷新预约记录”查询当前账号。")
        ttk.Label(records_tab, textvariable=self.records_summary,
                  style="SurfaceHint.TLabel").pack(anchor="w", pady=(3, 18))
        record_controls = ttk.Frame(records_tab, style="Surface.TFrame")
        record_controls.pack(fill="x", pady=(0, 11))
        self.records_refresh_button = ttk.Button(record_controls, text="刷新预约记录", command=self._load_records)
        self.records_refresh_button.pack(side="left")
        self.cancel_button = ttk.Button(record_controls, text="取消选中预约", style="Cancel.TButton",
                                        command=self._cancel_record, state="disabled")
        self.cancel_button.pack(side="right")
        records_area = ttk.Frame(records_tab, style="Surface.TFrame")
        records_area.pack(fill="both", expand=True)
        self.records_location = tk.StringVar(value="选中预约可查看完整区域和座位号。")
        location_label = ttk.Label(records_tab, textvariable=self.records_location,
                                   style="SurfaceHint.TLabel", wraplength=700)
        location_label.pack(fill="x", pady=(0, 10), before=records_area)
        location_label.bind("<Configure>", lambda event: location_label.configure(
            wraplength=max(100, event.width)))
        columns = ("date", "time", "area", "seat", "state")
        self.records_tree = ttk.Treeview(records_area, columns=columns, show="headings",
                                         selectmode="browse", style="Records.Treeview")
        for key, label, width in (("date", "日期", 110), ("time", "时段", 140),
                                  ("area", "区域", 340), ("seat", "座位号", 90),
                                  ("state", "状态", 90)):
            self.records_tree.heading(key, text=label)
            self.records_tree.column(key, width=width, minwidth=width,
                                     anchor="w" if key == "area" else "center")
        records_area.rowconfigure(0, weight=1)
        records_area.columnconfigure(0, weight=1)
        self.records_tree.grid(row=0, column=0, sticky="nsew")
        records_scroll = ttk.Scrollbar(records_area, orient="vertical", command=self.records_tree.yview)
        records_scroll.grid(row=0, column=1, sticky="ns")
        records_xscroll = ttk.Scrollbar(records_area, orient="horizontal", command=self.records_tree.xview)
        records_xscroll.grid(row=1, column=0, sticky="ew")
        self.records_tree.configure(yscrollcommand=records_scroll.set,
                                    xscrollcommand=records_xscroll.set)
        self.records_tree.tag_configure("pending", background="#EDF5EF", foreground="#174B38",
                                        font=("Microsoft YaHei UI", 11, "bold"))
        self.records_tree.bind("<<TreeviewSelect>>", lambda _event: self._update_cancel_button())

        ttk.Label(schedule_tab, text="定时预约", style="PageTitle.TLabel").pack(anchor="w")
        ttk.Label(schedule_tab, text="到设定时间自动运行。请保持电脑开机，并确保账号已保存。",
                  style="SurfaceHint.TLabel").pack(anchor="w", pady=(4, 19))
        schedule_body = ttk.Frame(schedule_tab, style="Surface.TFrame")
        schedule_body.pack(fill="x")
        form = ttk.Frame(schedule_body, style="Surface.TFrame")
        form.pack(fill="x")
        self.schedule_aside = ttk.Frame(schedule_body, style="Surface.TFrame", padding=(24, 4, 0, 0))
        ttk.Label(self.schedule_aside, text="下次任务触发", style="Section.TLabel").pack(anchor="w")
        self.next_task_time = tk.StringVar(value="刷新任务状态后显示")
        ttk.Label(self.schedule_aside, textvariable=self.next_task_time,
                  foreground=COLORS["forest"], style="Surface.TLabel",
                  font=("Microsoft YaHei UI", 13, "bold"), wraplength=230).pack(anchor="w", pady=(12, 25))
        ttk.Label(self.schedule_aside, text="预约目标日期", style="Section.TLabel").pack(anchor="w")
        self.next_booking_day = tk.StringVar(value="尚未查询")
        ttk.Label(self.schedule_aside, textvariable=self.next_booking_day,
                  foreground=COLORS["forest"], style="Surface.TLabel",
                  font=("Microsoft YaHei UI", 13, "bold"), wraplength=230).pack(anchor="w", pady=(12, 24))
        ttk.Label(self.schedule_aside, text="以 Windows 已保存任务为准。\n保存后刷新，确认计划已生效。",
                  style="SurfaceHint.TLabel", wraplength=230).pack(anchor="w")

        def arrange_schedule(event):
            wide = event.width >= 1000
            if wide == getattr(self, "_schedule_wide", None):
                return
            self._schedule_wide = wide
            form.pack_forget()
            self.schedule_aside.pack_forget()
            if wide:
                self.schedule_aside.pack(side="right", fill="y")
                form.pack(side="left", fill="x", expand=True)
            else:
                form.pack(fill="x")
        schedule_body.bind("<Configure>", arrange_schedule)
        self.schedule_at = tk.StringVar(value="22:00")
        self.schedule_room = tk.StringVar(value="11031")
        self.schedule_seat = tk.StringVar(value="150")
        self.schedule_start = tk.StringVar(value="11:00")
        self.schedule_end = tk.StringVar(value="21:00")
        self.weekday_times = {}
        self.weekday_summary = tk.StringVar(value="按星期：未设置，每天沿用上面的时段")
        self.backup_plans = []
        self.backup_summary = tk.StringVar(value="备选方案（0）")
        self.schedule_days = {name: tk.BooleanVar() for name, _ in WEEKDAYS}
        ttk.Label(form, text="执行时间（本机时间）", style="Field.TLabel").grid(row=0, column=0, sticky="w")
        at_picker = self._time_picker(form, self.schedule_at)
        at_picker.grid(row=1, column=0, sticky="w", padx=(0, 14), pady=(5, 15))
        ttk.Label(form, text="阅览区", style="Field.TLabel").grid(row=0, column=1, sticky="w")
        self.schedule_room_combo = ttk.Combobox(form, textvariable=self.schedule_room, values=list(ROOMS))
        self.schedule_room_combo.grid(row=1, column=1, columnspan=2, sticky="ew", pady=(5, 15))
        self._block_mouse_wheel(self.schedule_room_combo)
        ttk.Label(form, text="座位号", style="Field.TLabel").grid(row=2, column=0, sticky="w")
        ttk.Entry(form, textvariable=self.schedule_seat).grid(row=3, column=0, sticky="ew", padx=(0, 14), pady=(5, 15))
        ttk.Label(form, text="次日预约开始", style="Field.TLabel").grid(row=2, column=1, sticky="w")
        schedule_start_picker = self._time_picker(form, self.schedule_start)
        schedule_start_picker.grid(row=3, column=1, sticky="w", padx=(0, 14), pady=(5, 15))
        ttk.Label(form, text="次日预约结束", style="Field.TLabel").grid(row=2, column=2, sticky="w")
        schedule_end_picker = self._time_picker(form, self.schedule_end)
        schedule_end_picker.grid(row=3, column=2, sticky="w", pady=(5, 15))
        ttk.Button(form, text="按星期设置时段…", command=self._edit_weekday_times).grid(
            row=4, column=0, sticky="w", pady=(0, 15))
        ttk.Label(form, textvariable=self.weekday_summary, style="SurfaceHint.TLabel",
                  justify="left", wraplength=520).grid(row=4, column=1, columnspan=2,
                                                       sticky="w", pady=(0, 15))
        ttk.Label(form, text="执行星期（预约次日）", style="Field.TLabel").grid(row=5, column=0, columnspan=3, sticky="w")
        days_frame = ttk.Frame(form, style="Surface.TFrame")
        days_frame.grid(row=6, column=0, columnspan=3, sticky="w", pady=(5, 15))
        for name, label in WEEKDAYS:
            ttk.Checkbutton(days_frame, text=label, variable=self.schedule_days[name]).pack(side="left", padx=(0, 9))
        ttk.Separator(schedule_tab).pack(fill="x", pady=(15, 10))
        backup_row = ttk.Frame(schedule_tab, style="Surface.TFrame")
        backup_row.pack(fill="x", pady=8)
        ttk.Label(backup_row, text="备选方案", style="Section.TLabel").pack(side="left")
        ttk.Button(backup_row, textvariable=self.backup_summary, command=self._edit_backups).pack(side="right")
        ttk.Label(backup_row, text="按顺序补充目标时段内的空缺。", style="SurfaceHint.TLabel").pack(
            side="left", padx=16)
        ttk.Separator(schedule_tab).pack(fill="x", pady=(10, 8))
        self.rules_toggle = ttk.Button(schedule_tab, text="节假日与转预约    查看设置",
                                        command=self._toggle_schedule_rules)
        self.rules_toggle.pack(fill="x", pady=(4, 12))
        self.rules_panel = ttk.Frame(schedule_tab, style="Surface.TFrame")
        self.rules_anchor = ttk.Frame(schedule_tab, style="Surface.TFrame")
        self.rules_anchor.pack(fill="x")
        rules_form = ttk.Frame(self.rules_panel, style="Surface.TFrame")
        rules_form.pack(fill="x")
        self.skip_holidays = tk.BooleanVar(value=True)
        ttk.Checkbutton(rules_form, text="法定节假日不预约", variable=self.skip_holidays,
                        command=self._update_holiday_hint).grid(row=7, column=0, sticky="w", pady=(0, 6))
        self.holiday_hint = tk.StringVar()
        ttk.Label(rules_form, textvariable=self.holiday_hint, justify="left", wraplength=600,
                  style="SurfaceHint.TLabel").grid(row=8, column=0, columnspan=3, sticky="w")
        self.auto_cancel_other = tk.BooleanVar(value=False)
        ttk.Checkbutton(rules_form, text="任一账号定时预约成功后，另一账号不再预约同一天",
                        variable=self.auto_cancel_other).grid(row=9, column=0, columnspan=3, sticky="w", pady=(8, 0))
        ttk.Label(rules_form, text="两个账号共用；以首个成功时段为准，避免重复预约。",
                  style="SurfaceHint.TLabel").grid(row=10, column=0, columnspan=3, sticky="w")
        transfer_row = ttk.Frame(rules_form, style="Surface.TFrame")
        transfer_row.grid(row=11, column=0, columnspan=3, sticky="w", pady=(10, 0))
        self.transfer_enabled = tk.BooleanVar(value=False)
        ttk.Checkbutton(transfer_row, text="仅将账号2定时预约成功且全部约满的座位转给账号1",
                        variable=self.transfer_enabled).grid(row=0, column=0, columnspan=2, sticky="w")
        ttk.Label(transfer_row, text="转预约时间", style="Field.TLabel").grid(
            row=1, column=0, sticky="w", pady=(8, 0))
        self.transfer_at = tk.StringVar(value="22:30")
        self._time_picker(transfer_row, self.transfer_at).grid(row=1, column=1, sticky="w", padx=(6, 0), pady=(8, 0))
        ttk.Label(rules_form, text="两个账号共用。账号1已全部约到则只取消账号2；账号1只约到部分而"
                             "账号2全部约到，则先取消账号1再转。需两个账号都已保存定时预约。",
                  style="SurfaceHint.TLabel", justify="left", wraplength=640).grid(
            row=12, column=0, columnspan=3, sticky="w")
        form.columnconfigure(0, weight=1)
        form.columnconfigure(1, weight=2)
        form.columnconfigure(2, weight=1)
        ttk.Label(rules_form, text="取消后重新预约可能失败，原座位不保证能够恢复。",
                  style="SurfaceHint.TLabel", foreground=COLORS["amber"]).grid(
                      row=13, column=0, columnspan=3, sticky="w", pady=(8, 0))
        ttk.Separator(self.rules_panel).pack(fill="x", pady=(12, 4))
        self.adjustment_decisions = {}
        adjustment_head = ttk.Frame(self.rules_panel, style="Surface.TFrame")
        adjustment_head.pack(fill="x", pady=(8, 0))
        ttk.Label(adjustment_head, text="调休日决定", style="Section.TLabel").pack(side="left")
        self.adjustment_summary = tk.StringVar()
        ttk.Label(adjustment_head, textvariable=self.adjustment_summary,
                  style="SurfaceHint.TLabel").pack(side="left", padx=(10, 0))
        adjustment_area = ttk.Frame(self.rules_panel, style="Surface.TFrame")
        adjustment_area.pack(fill="x", pady=(6, 0))
        self.adjustment_tree = ttk.Treeview(
            adjustment_area, columns=("date", "weekday", "kind", "decision"),
            show="headings", height=4, selectmode="browse")
        for key, title, width in (("date", "日期", 110), ("weekday", "星期", 80),
                                  ("kind", "类型", 110), ("decision", "决定", 120)):
            self.adjustment_tree.heading(key, text=title)
            self.adjustment_tree.column(key, width=width, anchor="w" if key == "kind" else "center")
        adjustment_scroll = ttk.Scrollbar(adjustment_area, orient="vertical",
                                          command=self.adjustment_tree.yview)
        self.adjustment_tree.configure(yscrollcommand=adjustment_scroll.set)
        self.adjustment_tree.pack(side="left", fill="x", expand=True)
        adjustment_scroll.pack(side="right", fill="y")
        adjustment_buttons = ttk.Frame(self.rules_panel, style="Surface.TFrame")
        adjustment_buttons.pack(fill="x", pady=(7, 0))
        ttk.Button(adjustment_buttons, text="预约",
                   command=lambda: self._set_adjustment_decision("reserve")).pack(side="left")
        ttk.Button(adjustment_buttons, text="跳过",
                   command=lambda: self._set_adjustment_decision("skip")).pack(side="left", padx=(7, 0))
        ttk.Button(adjustment_buttons, text="清除决定",
                   command=lambda: self._set_adjustment_decision(None)).pack(side="left", padx=(7, 0))
        ttk.Label(self.rules_panel, text="未决定的调休日照常预约；决定会立即保存。",
                  style="SurfaceHint.TLabel").pack(anchor="w", pady=8)
        ttk.Button(self.rules_panel, text="更新节假日表", command=self._update_holiday_table).pack(anchor="w", pady=(0, 12))
        controls = ttk.Frame(schedule_tab, style="Surface.TFrame")
        controls.pack(fill="x", pady=(12, 15))
        self.schedule_save_button = ttk.Button(controls, text="保存定时预约", style="Accent.TButton",
                                               command=self._save_schedule)
        self.schedule_save_button.pack(side="left")
        ttk.Button(controls, text="刷新任务状态", command=self._refresh_schedule).pack(side="left", padx=(12, 0))
        self.schedule_status = tk.StringVar(value="点击“刷新任务状态”查看下一次运行时间。")
        self.schedule_overview = tk.StringVar(value="选择执行星期与时段，保存后生效。")
        self.schedule_overview_label = ttk.Label(schedule_tab, textvariable=self.schedule_overview,
                                                 style="SurfaceHint.TLabel", wraplength=700,
                                                 justify="left")
        self.schedule_overview_label.pack(anchor="w", pady=(0, 12), before=schedule_body)
        for variable in (self.schedule_at, self.schedule_room, self.schedule_seat,
                         self.schedule_start, self.schedule_end, *self.schedule_days.values()):
            variable.trace_add("write", lambda *_args: self._update_schedule_overview())
        ttk.Label(schedule_tab, text="任务状态", style="Section.TLabel").pack(anchor="w", pady=(10, 5))
        ttk.Label(schedule_tab, textvariable=self.schedule_status, justify="left", style="Surface.TLabel").pack(anchor="w")
        ttk.Label(schedule_tab, text="已成功时段会保留；备选方案只补主方案目标内的空缺。单次最多 4 小时，结果见 last-run.log。",
                  style="SurfaceHint.TLabel", wraplength=700).pack(anchor="w", pady=(12, 0))
        ttk.Label(schedule_tab, text="法定节假日按 holidays.json 判断；命中时任务直接跳过、不提交预约，"
                                     "日志会记录 skipped 原因。调休日不会自动跳过，在上方逐日决定即可；"
                                     "节假日数据可点“更新节假日表”按国务院通知自动获取，也可手工修改该文件。",
                  style="SurfaceHint.TLabel", justify="left", wraplength=700).pack(anchor="w", pady=(4, 0))

        self.status = tk.StringVar(value="就绪")
        status_bar = ttk.Frame(workspace, style="Soft.TFrame", padding=(20, 9))
        status_bar.pack(fill="x", side="bottom")
        ttk.Label(status_bar, text="程序状态", style="Soft.TLabel").pack(side="left", padx=(0, 13))
        ttk.Label(status_bar, textvariable=self.status, style="Soft.TLabel").pack(side="left")

    def _navigate(self, page, refresh=True):
        """Switch view without rebuilding forms or losing unsaved values."""
        for frame in self.pages.values():
            frame.pack_forget()
        self.pages[page].pack(fill="both", expand=True)
        for key, button in self.nav_buttons.items():
            button.configure(style="Selected.Nav.TButton" if key == page else "Nav.TButton")
        self.current_page = page
        if page in self.resource_tabs:
            self._pod_tab_changed(page)
        self.header_date.set(dt.datetime.now(LOCAL_TZ).strftime("%Y-%m-%d"))
        self.content_canvas.yview_moveto(0)
        if page == "schedule" and refresh:
            self._refresh_schedule()

    def _toggle_schedule_rules(self):
        if self.rules_panel.winfo_manager():
            self.rules_panel.pack_forget()
            self.rules_toggle.configure(text="节假日与转预约    查看设置")
        else:
            self.rules_panel.pack(fill="x", before=self.rules_anchor)
            self.rules_toggle.configure(text="节假日与转预约    收起设置")

    def _update_schedule_overview(self):
        days = "、".join(label for name, label in WEEKDAYS if self.schedule_days[name].get())
        self.schedule_overview.set(
            f"每{days or '（未选择执行星期）'} {self.schedule_at.get()} 执行 · 预约次日 "
            f"{self.schedule_start.get()}–{self.schedule_end.get()} · {self.schedule_seat.get()} 号座位\n"
            "这是当前表单预览，实际任务状态以下方查询结果为准。")

    def _scroll_content(self, event):
        if event.widget.winfo_toplevel() is not self:
            return
        if isinstance(event.widget, (ttk.Treeview, ttk.Combobox)):
            return
        self.content_canvas.yview_scroll(-int(event.delta / 120), "units")

    @staticmethod
    def _block_mouse_wheel(widget):
        """Only a click may change a dropdown; the wheel must not pick an item."""
        for sequence in ("<MouseWheel>", "<Button-4>", "<Button-5>"):
            widget.bind(sequence, lambda _event: "break")

    @staticmethod
    def _time_picker(parent, value):
        def open_clock():
            dialog = ClockDialog(parent, value.get())
            parent.wait_window(dialog)
            if dialog.result is not None:
                value.set(dialog.result)

        return ttk.Button(parent, textvariable=value, command=open_clock, width=9)

    @staticmethod
    def _date_picker(parent, value):
        selected = dt.date.fromisoformat(value.get())
        year_var = tk.StringVar(value=str(selected.year))
        month_var = tk.StringVar(value=f"{selected.month:02d}")
        day_var = tk.StringVar(value=f"{selected.day:02d}")
        syncing = False
        frame = ttk.Frame(parent, style="Surface.TFrame")
        current_year = dt.datetime.now(LOCAL_TZ).year
        year_box = ttk.Combobox(frame, textvariable=year_var,
                                values=[str(year) for year in range(current_year - 1, current_year + 4)],
                                state="readonly", width=6, height=8)
        year_box.pack(side="left")
        ttk.Label(frame, text="年", style="Surface.TLabel").pack(side="left", padx=(3, 5))
        month_box = ttk.Combobox(frame, textvariable=month_var,
                                 values=[f"{month:02d}" for month in range(1, 13)],
                                 state="readonly", width=4, height=12)
        month_box.pack(side="left")
        ttk.Label(frame, text="月", style="Surface.TLabel").pack(side="left", padx=(3, 5))
        day_box = ttk.Combobox(frame, textvariable=day_var, state="readonly", width=4, height=12)
        day_box.pack(side="left")
        ttk.Label(frame, text="日", style="Surface.TLabel").pack(side="left", padx=(3, 0))

        def set_days(year, month):
            last_day = calendar.monthrange(year, month)[1]
            day_box.configure(values=[f"{day:02d}" for day in range(1, last_day + 1)])
            return last_day

        set_days(selected.year, selected.month)

        def from_parts(*_args):
            nonlocal syncing
            if syncing:
                return
            syncing = True
            try:
                year, month = int(year_var.get()), int(month_var.get())
                day = min(int(day_var.get()), set_days(year, month))
                day_var.set(f"{day:02d}")
                value.set(dt.date(year, month, day).isoformat())
            finally:
                syncing = False

        def from_value(*_args):
            nonlocal syncing
            if syncing:
                return
            try:
                day = dt.date.fromisoformat(value.get())
            except ValueError:
                return
            syncing = True
            try:
                year_var.set(str(day.year))
                month_var.set(f"{day.month:02d}")
                set_days(day.year, day.month)
                day_var.set(f"{day.day:02d}")
            finally:
                syncing = False

        year_var.trace_add("write", from_parts)
        month_var.trace_add("write", from_parts)
        day_var.trace_add("write", from_parts)
        value.trace_add("write", from_value)
        return frame, year_var, month_var, day_var

    def _load_fields(self):
        try:
            data = load_config()
            accounts = data["reserve"]
            self.account_index = min(self.account_index, len(accounts) - 1)
            self.account_combo.configure(values=[f"账号 {index + 1} · {item.get('username') or '未填写'}"
                                                  for index, item in enumerate(accounts)])
            self.account_choice.set(self.account_combo["values"][self.account_index])
            account = accounts[self.account_index]
            self._loading_account = True
            try:
                self.username.set(account.get("username", ""))
                self.password.set(account.get("password", ""))
                self.saved_username = self.username.get().strip()
                self.dept_id.set(account.get("deptIdEnc") or data.get("deptIdEnc", SCHOOL_DEPT_ID_ENC))
            finally:
                self._loading_account = False
            manual = account.get("manual") or {}
            self.room.set(str(manual.get("room", "")))
            default_day, default_start = manual_default_datetime()
            self.day.set(default_day)
            self.start.set(default_start)
            self.end.set("21:00")
            try:
                value = dt.datetime.strptime(manual.get("end", ""), "%H:%M").strftime("%H:%M")
                self.end.set(value)
            except ValueError:
                pass
            if self.end.get() <= default_start:
                self.end.set("23:59")
            self.schedule_at.set(account.get("scheduleAt", data.get("scheduleAt", "22:00")))
            self.auto_cancel_other.set(data.get("autoCancelOtherOnSuccess") is True)
            try:
                transfer = transfer_settings(data)
                self.transfer_enabled.set(transfer["enabled"])
                self.transfer_at.set(transfer["at"])
            except ValueError:
                self.transfer_enabled.set(False)
                self.transfer_at.set("22:30")
            self.schedule_room.set(str(account.get("roomid", "")))
            seats = account.get("seatid") or []
            self.schedule_seat.set(str(seats[0]) if seats else "")
            period = account.get("time") or []
            if len(period) == 2:
                self.schedule_start.set(period[0])
                self.schedule_end.set(period[1])
            self.backup_plans = list(account.get("backups") or [])
            self.backup_summary.set(f"备选方案（{len(self.backup_plans)}）")
            self.weekday_times = {}
            raw_weekday_times = account.get(TIMES_BY_WEEKDAY_KEY)
            if isinstance(raw_weekday_times, dict):
                known = dict(WEEKDAYS)
                for name, value in raw_weekday_times.items():
                    if name in known and isinstance(value, (list, tuple)) and len(value) == 2:
                        self.weekday_times[name] = [str(value[0]), str(value[1])]
            self._update_weekday_summary()
            for name, _ in WEEKDAYS:
                self.schedule_days[name].set(name in account.get("daysofweek", []))
            skip_holidays = account.get("skipHolidays", data.get("skipHolidays", True))
            self.skip_holidays.set(True if skip_holidays is None else bool(skip_holidays))
            decisions = account.get("adjustments", data.get("adjustments"))
            self.adjustment_decisions = decisions if isinstance(decisions, dict) else {}
        except (OSError, ValueError, KeyError, IndexError) as error:
            self.status.set(f"读取本机账号配置失败：{error}")
        self._update_holiday_hint()
        self._refresh_adjustments()
        self._loaded_schedule = self._schedule_state()
        self.next_task_time.set("刷新任务状态后显示")
        self.next_booking_day.set("尚未查询")

    def _update_holiday_hint(self):
        tomorrow = dt.datetime.now(LOCAL_TZ).date() + dt.timedelta(days=1)
        holiday_calendar = holidays.calendar()
        if holiday_calendar.error:
            self.holiday_hint.set(f"读取节假日数据失败：{holiday_calendar.error}")
            return
        name = holiday_calendar.name(tomorrow)
        if name:
            action = "将跳过" if self.skip_holidays.get() else "仍会提交"
            self.holiday_hint.set(f"明天 {tomorrow:%Y-%m-%d} 是法定节假日（{name}），任务{action}预约。")
        elif not holiday_calendar.covers(tomorrow):
            self.holiday_hint.set(
                f"节假日表未覆盖 {tomorrow.year} 年，请在 {holiday_calendar.path.name} 中补充，"
                "或点“更新节假日表”自动获取。")
        elif holiday_calendar.pending_for(tomorrow):
            self.holiday_hint.set(
                f"明天 {tomorrow:%Y-%m-%d} 在 {holiday_calendar.pending_for(tomorrow)} 放假区间内，"
                "但法定节假日日期尚未确认（等待数据源更新），任务会照常预约。")
        else:
            self.holiday_hint.set(
                f"明天 {tomorrow:%Y-%m-%d} 不是法定节假日；节假日表：{holiday_calendar.path.name}。")

    def _update_holiday_table(self):
        """Fetch the official arrangement in the background, then write it after a preview."""
        today = dt.datetime.now(LOCAL_TZ).date()
        years = [today.year, today.year + 1]

        def task():
            return holiday_update.fetch_all(years, timeout=10)

        self._work(task, self._apply_holiday_table, status="正在获取国务院节假日安排…")

    def _apply_holiday_table(self, results):
        lines = holiday_update.describe(results)
        if any(item.get("error") for item in results):
            messagebox.showerror("更新节假日表", "\n".join(lines), parent=self)
            return
        if not messagebox.askyesno("更新节假日表",
                                   "\n".join(lines) + "\n\n确认写入 holidays.json？", parent=self):
            return
        try:
            summary = holiday_update.apply(holidays.default_path(), results)
        except OSError as error:
            messagebox.showerror("更新失败", f"无法写入 holidays.json：{error}", parent=self)
            return
        self._update_holiday_hint()
        self._refresh_adjustments()
        self.status.set("节假日表已是最新。" if not summary["written"] else "节假日表已更新。")

    def _refresh_adjustments(self):
        holiday_calendar = holidays.calendar()
        today = dt.datetime.now(LOCAL_TZ).date()
        last_day = today + dt.timedelta(days=ADJUSTMENT_WINDOW_DAYS)
        rows = [] if holiday_calendar.error else holiday_calendar.upcoming(today, last_day)
        self.adjustment_tree.delete(*self.adjustment_tree.get_children())
        undecided = 0
        for day, _kind, name in rows:
            decision = self.adjustment_decisions.get(str(day))
            undecided += decision is None
            self.adjustment_tree.insert("", "end", iid=str(day), values=(
                f"{day:%Y-%m-%d}", f"周{'一二三四五六日'[day.weekday()]}", name,
                {"reserve": "预约", "skip": "跳过"}.get(decision, "未决定（照常预约）")))
        if holiday_calendar.error:
            self.adjustment_summary.set(f"读取节假日数据失败：{holiday_calendar.error}")
        elif not rows:
            self.adjustment_summary.set(
                f"未来 {ADJUSTMENT_WINDOW_DAYS} 天没有登记的调休日；"
                f"可在 {holiday_calendar.path.name} 的 adjustments 中添加")
        else:
            self.adjustment_summary.set(
                f"未来 {ADJUSTMENT_WINDOW_DAYS} 天有 {len(rows)} 个调休日，其中 {undecided} 个未决定")

    def _set_adjustment_decision(self, value):
        selection = self.adjustment_tree.selection()
        if not selection:
            messagebox.showinfo("请选择日期", "先选中要决定的调休日。", parent=self)
            return
        day = selection[0]
        if value:
            self.adjustment_decisions[day] = value
        else:
            self.adjustment_decisions.pop(day, None)
        today = dt.datetime.now(LOCAL_TZ).date().isoformat()
        self.adjustment_decisions = {key: item for key, item in self.adjustment_decisions.items()
                                     if key >= today}
        try:
            data = load_config()
            data["reserve"][self.account_index]["adjustments"] = dict(sorted(self.adjustment_decisions.items()))
            with CONFIG_PATH.open("w", encoding="utf-8") as file:
                json.dump(data, file, ensure_ascii=False, indent=2)
                file.write("\n")
        except (OSError, ValueError, KeyError, IndexError) as error:
            messagebox.showerror("保存失败", f"无法写入本机配置：{error}", parent=self)
            return
        self._refresh_adjustments()
        label = {"reserve": "预约", "skip": "跳过"}.get(value, "未决定（照常预约）")
        self.status.set(f"调休日 {day} 的决定已保存：{label}")

    def _account_changed(self, *_args):
        if self._loading_account:
            return
        if self.username.get().strip() != self.saved_username:
            self.record_rows = {}
            for row in self.records_tree.get_children():
                self.records_tree.delete(row)
            self.records_attention.set("尚未查询预约")
            self.records_attention_label.configure(fg=COLORS["ink"])
            self.records_summary.set("账号已更改，请刷新预约记录。")
            self._update_cancel_button()
            self.dept_id.set("")
            self.room_combo.configure(values=[])
            self.schedule_room_combo.configure(values=[])
            self.room_names = {}
            self.loaded_dept = None
            self.room.set("")
            self.schedule_room.set("")
            self.status.set("账号已更换；请填写该学校的选座链接以加载区域。")

    def _toggle_password(self):
        self.password_entry.configure(show="" if self.show_password.get() else "●")

    def _schedule_state(self):
        """The schedule tab's current values, or None when they cannot be read."""
        try:
            return {
                "roomid": parse_room(self.schedule_room.get()),
                "seatid": [self.schedule_seat.get().strip().zfill(3)]
                          if self.schedule_seat.get().strip() else [],
                "time": [self.schedule_start.get(), self.schedule_end.get()],
                "daysofweek": [name for name, _ in WEEKDAYS if self.schedule_days[name].get()],
                "scheduleAt": self.schedule_at.get(),
                "backups": [dict(plan) for plan in self.backup_plans],
                "weekday": {name: list(value) for name, value in self.weekday_times.items()},
                "skipHolidays": bool(self.skip_holidays.get()),
                "transfer": [bool(self.transfer_enabled.get()), self.transfer_at.get().strip()],
            }
        except (ValueError, KeyError):
            return None

    def _schedule_dirty(self):
        """True when the form holds edits that are not in the loaded snapshot.

        Comparing with the values loaded into the form (not with the live file)
        matters: the config file can legitimately be changed from outside while
        this window is open (for example by the user editing it or by the
        transfer setup), and that must not look like an unsaved edit here.
        """
        loaded = getattr(self, "_loaded_schedule", None)
        current = self._schedule_state()
        return current is None or current != loaded

    def _select_account(self, _event=None):
        if self.busy:
            self.account_choice.set(self.account_combo["values"][self.account_index])
            return
        if not self._confirm_pod_changes():
            self.account_choice.set(self.account_combo["values"][self.account_index])
            return
        if not self._save_manual_settings():
            self.account_choice.set(self.account_combo["values"][self.account_index])
            return
        if self._schedule_dirty() and not messagebox.askyesno(
                "放弃未保存的修改？",
                "定时预约页有未保存的修改，切换账号会丢弃它们。\n"
                "选“是”继续切换；选“否”留在当前账号。\n"
                "（如果你没有改过这个页面，选“是”即可。）",
                parent=self):
            self.account_choice.set(self.account_combo["values"][self.account_index])
            return
        self.account_index = self.account_combo.current()
        for panel in self.pod_panels.values():
            panel.destroy()
        self.pod_panels.clear()
        for mode in self.resource_tabs:
            self._pod_tab_changed(mode)
        self.loaded_dept = None
        self.room_names = {}
        self.room_combo.configure(values=[])
        self.schedule_room_combo.configure(values=[])
        self._load_fields()
        self.schedule_status.set("点击“刷新任务状态”查看当前账号的任务。")
        self._load_rooms()

    def _pod_tab_changed(self, mode):
        tabs = self.resource_tabs[mode]
        if tabs.select() != str(self.pod_hosts[mode]):
            return
        from pod_gui import PodPanel
        panel = self.pod_panels.get(mode)
        # Reload saved shared settings when returning to an unchanged form.
        if panel and not panel.busy and panel.settings() == panel.saved_settings:
            panel.destroy()
            panel = None
        if panel is None:
            panel = PodPanel(self.pod_hosts[mode], CONFIG_PATH, self.account_index, mode)
            panel.pack(fill="both", expand=True)
            self.pod_panels[mode] = panel

    def _confirm_pod_changes(self):
        for panel in self.pod_panels.values():
            if panel.busy:
                messagebox.showinfo("静音舱操作尚未结束", "请等待查询或提交结果。", parent=self)
                return False
        if any(panel.settings() != panel.saved_settings for panel in self.pod_panels.values()):
            return messagebox.askyesno("放弃静音舱未保存的设置？", "静音舱有未保存的设置，是否放弃并继续？", parent=self)
        return True

    def _save_manual_settings(self):
        try:
            data = load_config()
            data["reserve"][self.account_index]["manual"] = {
                "room": self.room.get().strip(),
                "day": self.day.get(), "start": self.start.get(), "end": self.end.get(),
            }
            with CONFIG_PATH.open("w", encoding="utf-8") as file:
                json.dump(data, file, ensure_ascii=False, indent=2)
                file.write("\n")
            return True
        except (OSError, ValueError, KeyError, IndexError) as error:
            messagebox.showerror("保存手动设置失败", str(error), parent=self)
            return False

    def _close(self):
        if not self._confirm_pod_changes():
            return
        if self._schedule_dirty() and not messagebox.askyesno(
                "放弃未保存的修改？",
                "定时预约页有未保存的修改，关闭窗口会丢弃它们。\n"
                "选“是”继续关闭；选“否”返回。\n"
                "（如果你没有改过这个页面，选“是”即可。）",
                parent=self):
            return
        if self._save_manual_settings():
            self.destroy()

    def _add_account(self):
        try:
            if not self._save_manual_settings():
                return
            data = load_config()
            first = data["reserve"][0]
            data["reserve"].append({"username": "", "password": "", "roomid": "",
                                    "seatid": [], "time": list(first.get("time") or ["11:00", "15:00"]),
                                    "backups": [], "daysofweek": [], "scheduleAt": "22:00",
                                    "skipHolidays": True, "adjustments": {}, "deptIdEnc": "",
                                    "scheduleEnabled": False})
            with CONFIG_PATH.open("w", encoding="utf-8") as file:
                json.dump(data, file, ensure_ascii=False, indent=2)
                file.write("\n")
            self.account_index = len(data["reserve"]) - 1
            self._load_fields()
            self.status.set(f"账号 {self.account_index + 1} 已添加；保存定时预约后才会创建该账号的任务。")
        except (OSError, ValueError, KeyError, IndexError) as error:
            messagebox.showerror("添加失败", str(error), parent=self)

    def _delete_account(self):
        if self.busy or not self._confirm_pod_changes():
            return
        if not messagebox.askyesno(
                "删除当前账号？",
                f"确定删除账号 {self.account_index + 1}？该账号的本机配置和未保存的修改将被移除。\n"
                "该账号及其后续账号的座位、静音舱定时任务将被移除，转预约将关闭。\n"
                "后续账号序号会前移，需要重新保存定时预约。最后一个账号删除后保留空白入口。\n"
                "学习通上已有的预约不会取消。", parent=self):
            return
        def completed(index):
            self.account_index = index
            for panel in self.pod_panels.values():
                panel.destroy()
            self.pod_panels.clear()
            self.loaded_dept = None
            self.room_names = {}
            self.room_combo.configure(values=[])
            self.schedule_room_combo.configure(values=[])
            self.record_rows = {}
            for row in self.records_tree.get_children():
                self.records_tree.delete(row)
            self._load_fields()
            self.records_summary.set("账号已更改，请刷新预约记录。")
            self._update_cancel_button()
            for mode in self.resource_tabs:
                self._pod_tab_changed(mode)
            self.schedule_status.set("受影响的定时任务已移除，请重新保存定时预约。")
            self.status.set("账号已删除，转预约已关闭；学习通上已有预约保持不变。")
        self._work(lambda: delete_account_config(self.account_index), completed,
                   status="正在清理定时任务并删除账号…")

    def _save_account(self):
        if not self._save_manual_settings():
            return
        if self._schedule_dirty() and not messagebox.askyesno(
                "放弃未保存的修改？",
                "定时预约页有未保存的修改，保存账号会重新载入该页并丢弃它们。\n"
                "选“是”继续；选“否”返回。\n"
                "（如果你没有改过这个页面，选“是”即可。）",
                parent=self):
            return
        username = self.username.get().strip()
        password = self.password.get()
        if not username or not password:
            messagebox.showerror("无法保存", "账号和密码都不能为空。", parent=self)
            return
        try:
            dept_id_enc = parse_dept_id(self.dept_id.get())
        except ValueError as error:
            messagebox.showerror("无法保存", str(error), parent=self)
            return
        try:
            data = load_config()
            if any(item.get("username") == username for index, item in enumerate(data["reserve"])
                   if index != self.account_index):
                raise ValueError("账号不能使用相同的用户名。")
            item = data["reserve"][self.account_index]
            if item.get("username") != username:
                item.pop("classicEveningFirst", None)
            item.update(username=username, password=password, deptIdEnc=dept_id_enc)
            if self.account_index == 0:
                data["deptIdEnc"] = dept_id_enc
            with CONFIG_PATH.open("w", encoding="utf-8") as file:
                json.dump(data, file, ensure_ascii=False, indent=2)
                file.write("\n")
            self.status.set("账号已保存到本机配置文件。")
            self.saved_username = username
            self._load_fields()
            self._load_rooms()
        except (OSError, ValueError, KeyError, IndexError) as error:
            messagebox.showerror("保存失败", str(error), parent=self)

    def _set_day(self, offset):
        self.day.set((dt.datetime.now(LOCAL_TZ).date() + dt.timedelta(days=offset)).isoformat())

    def _form(self):
        username = self.username.get().strip()
        password = self.password.get()
        if not username or not password:
            raise ValueError("请先填写学习通账号和密码。")
        if parse_dept_id(self.dept_id.get()) != self.loaded_dept:
            raise ValueError("请先刷新该学校的阅览区。")
        roomid = parse_room(self.room.get())
        if roomid not in self.room_names.values():
            raise ValueError("请选择当前学校的阅览区。")
        day = dt.date.fromisoformat(self.day.get().strip())
        start = dt.datetime.strptime(self.start.get().strip(), "%H:%M").strftime("%H:%M")
        end = dt.datetime.strptime(self.end.get().strip(), "%H:%M").strftime("%H:%M")
        if end <= start:
            raise ValueError("结束时间必须晚于开始时间。")
        return username, password, str(day), roomid, start, end

    @staticmethod
    def _client(username, password, reserve_next_day=False):
        client = reserve(sleep_time=0.2, max_attempt=2, enable_slider=True, reserve_next_day=reserve_next_day)
        original_request = client.requests.request

        def request_with_timeout(method, url, **kwargs):
            kwargs.setdefault("timeout", 15)
            return original_request(method, url, **kwargs)

        client.requests.request = request_with_timeout
        client.get_login_status()
        success, message = client.login(username, password)
        if not success:
            raise RuntimeError(message or "学习通登录失败。")
        client.requests.headers.update({"Host": "office.chaoxing.com"})
        return client

    def _work(self, task, callback, status="正在连接学习通，请稍候…"):
        self.busy = True
        self.query_button.configure(state="disabled")
        self.refresh_rooms_button.configure(state="disabled")
        self.schedule_save_button.configure(state="disabled")
        self.book_button.configure(state="disabled")
        self.records_refresh_button.configure(state="disabled")
        self.cancel_button.configure(state="disabled")
        self.status.set(status)

        def worker():
            try:
                self.events.put((callback, task(), None))
            except Exception as error:
                self.events.put((callback, None, error))

        threading.Thread(target=worker, daemon=True).start()

    def _drain_events(self):
        try:
            while True:
                callback, result, error = self.events.get_nowait()
                self.busy = False
                self.query_button.configure(state="normal")
                self.refresh_rooms_button.configure(state="normal")
                self.schedule_save_button.configure(state="normal")
                self.records_refresh_button.configure(state="normal")
                if error:
                    self.status.set("操作失败。")
                    if callback == self._show_records:
                        self.records_attention.set("查询失败")
                        self.records_attention_label.configure(fg=COLORS["ink"])
                        self.records_summary.set("查询失败，请查看错误提示后重试。")
                    messagebox.showerror("操作失败", str(error), parent=self)
                else:
                    callback(result)
                self._update_book_button()
                self._update_cancel_button()
        except queue.Empty:
            pass
        self.after(100, self._drain_events)

    def _record_credentials(self):
        username, password = self.username.get().strip(), self.password.get()
        if not username or not password:
            raise ValueError("请先填写学习通账号和密码。")
        return username, password, parse_dept_id(self.dept_id.get())

    def _load_records(self):
        if self.busy:
            return
        try:
            username, password, fid_enc = self._record_credentials()
        except ValueError as error:
            messagebox.showerror("无法查询", str(error), parent=self)
            return
        self.records_summary.set("正在查询预约记录，请稍候…")
        from pod_reservation import DEFAULT_URL
        entry = load_config()["reserve"][self.account_index].get("pod", {}).get("url", DEFAULT_URL)
        self._work(lambda: self._query_all_records(username, password, fid_enc, entry),
                   self._show_records, "正在查询已有预约…")

    def _query_all_records(self, username, password, fid_enc, entry):
        from pod_reservation import PodClient
        records, errors = [], []
        try:
            records.extend(list_reservations(self._client(username, password), fid_enc))
        except Exception as error:
            errors.append(f"座位查询失败：{error}")
        try:
            client = PodClient(username, password, entry)
            pods = {p["id"]: p for p in client.pods()}
            for row in client.records(current_only=True):
                pod = pods.get(str(row.get("itemId")), {})
                records.append(dict(row, id=f"pod:{row['id']}", resourceType="pod",
                                    startTime=row.get("starttime"), endTime=row.get("endtime"),
                                    roomName="静音舱 / " + (pod.get("location") or "地点未提供"),
                                    seatNum=pod.get("name") or row.get("name") or str(row.get("itemId"))))
        except Exception as error:
            errors.append(f"静音舱查询失败：{error}")
        return {"records": records, "errors": errors}

    def _show_records(self, records):
        errors = records.get("errors", []) if isinstance(records, dict) else []
        records = records["records"] if isinstance(records, dict) else records
        now_ms = dt.datetime.now(LOCAL_TZ).timestamp() * 1000
        visible = []
        for item in records:
            try:
                if (item.get("resourceType") == "pod" or int(item.get("status")) in (0, 1, 3, 5)) and int(item["endTime"]) > now_ms:
                    visible.append(item)
            except (TypeError, ValueError, KeyError):
                continue
        self.record_rows = {str(item["id"]): item for item in visible}
        self.records_location.set("选中预约可查看完整区域和座位号。")
        for row in self.records_tree.get_children():
            self.records_tree.delete(row)
        status_names = {0: "待履约", 1: "学习中", 2: "已履约", 3: "暂离中",
                        5: "被监督中", 7: "已取消"}
        for item in visible:
            start, end = item.get("startTime"), item.get("endTime")
            try:
                beginning = dt.datetime.fromtimestamp(int(start) / 1000, LOCAL_TZ)
                ending = dt.datetime.fromtimestamp(int(end) / 1000, LOCAL_TZ)
                day, period = beginning.strftime("%Y-%m-%d"), (
                    f"{beginning:%H:%M}–{ending:%H:%M}")
            except (TypeError, ValueError, OverflowError, OSError):
                day, period = "未知", "未知"
            status = item.get("status")
            try:
                status = int(status)
            except (TypeError, ValueError):
                pass
            self.records_tree.insert("", "end", iid=str(item["id"]), values=(
                day, period, *reservation_location(item),
                f"状态码 {status}" if item.get("resourceType") == "pod" else status_names.get(status, str(status))),
                tags=("pending",) if status == 0 and item.get("resourceType") != "pod" else ())
        pending_count = sum(str(item.get("status")) == "0" and item.get("resourceType") != "pod" for item in visible)
        self.records_attention.set(f"{pending_count} 条待履约" if pending_count else "暂无待履约预约")
        self.records_attention_label.configure(fg=COLORS["forest"] if pending_count else COLORS["ink"])
        self.records_summary.set(f"当前预约 {len(visible)} 条 · 选中可取消的预约后点击“取消选中预约”"
                                 + (" · " + "；".join(errors) if errors else ""))
        self.status.set("预约记录已更新。")

    def _update_cancel_button(self):
        selected = self.records_tree.selection()
        item = getattr(self, "record_rows", {}).get(selected[0]) if selected else None
        if item:
            area, seat = reservation_location(item)
            self.records_location.set(f"区域：{area}    座位号：{seat}")
        else:
            self.records_location.set("选中预约可查看完整区域和座位号。")
        try:
            from pod_reservation import can_cancel_record
            cancellable = (can_cancel_record(item) if item.get("resourceType") == "pod"
                           else int(item.get("status")) == 0) if item else False
        except (TypeError, ValueError):
            cancellable = False
        self.cancel_button.configure(state="normal" if cancellable and not self.busy else "disabled")

    def _cancel_record(self):
        if self.busy or not self.records_tree.selection():
            return
        record_id = self.records_tree.selection()[0]
        item = getattr(self, "record_rows", {}).get(record_id)
        if item and item.get("resourceType") == "pod":
            self._cancel_pod_record(record_id, item)
            return
        if not item or item.get("resourceType") == "pod" or str(item.get("status")) != "0":
            return
        details = self.records_tree.item(record_id, "values")
        area, seat = reservation_location(item)
        if not messagebox.askyesno("确认取消预约",
                                   f"确定取消 {details[0]} {details[1]} 的预约吗？\n"
                                   f"区域：{area}\n座位号：{seat}\n"
                                   f"记录 ID：{record_id}", parent=self):
            return
        try:
            username, password, fid_enc = self._record_credentials()
        except ValueError as error:
            messagebox.showerror("无法取消", str(error), parent=self)
            return

        def task():
            client = self._client(username, password)
            current = list_reservations(client, fid_enc)
            matched = next((row for row in current if str(row.get("id")) == record_id), None)
            if not matched or str(matched.get("status")) != "0":
                raise RuntimeError("预约状态已变化，请刷新记录后重试。")
            cancel_reservation(client, record_id)
            try:
                return record_id, list_reservations(client, fid_enc), None
            except Exception as error:
                return record_id, None, str(error)

        def done(result):
            cancelled_id, records, refresh_error = result
            if records is None:
                self.record_rows = {}
                for row in self.records_tree.get_children():
                    self.records_tree.delete(row)
                self.records_attention.set("取消结果待核对")
                self.records_attention_label.configure(fg=COLORS["ink"])
                self.records_summary.set("取消接口返回成功，刷新记录失败；请重新查询并在学习通核对。")
                messagebox.showwarning("取消结果待核对", f"取消接口返回成功，但无法刷新记录：{refresh_error}", parent=self)
                return
            pods = [row for row in getattr(self, "record_rows", {}).values() if row.get("resourceType") == "pod"]
            self._show_records(records + pods)
            current = next((row for row in records if str(row.get("id")) == cancelled_id), None)
            if current and str(current.get("status")) == "0":
                messagebox.showwarning("取消结果待核对", "接口返回成功，但刷新后仍显示待履约；请在学习通核对。", parent=self)
            else:
                messagebox.showinfo("取消预约", "取消请求已成功，请在学习通核对最终记录。", parent=self)

        self._work(task, done, "正在取消预约并核对结果…")

    def _cancel_pod_record(self, record_id, item):
        from pod_reservation import PodClient, DEFAULT_URL, can_cancel_record, cancel_includes_team
        if not can_cancel_record(item):
            return
        include_team = cancel_includes_team(item)
        area, pod = reservation_location(item)
        details = self.records_tree.item(record_id, "values")
        note = "\n注意：你是队长，此操作将同时取消同组队员的预约。" if include_team else ""
        if not messagebox.askyesno("确认取消静音舱预约",
                                  f"确定取消 {details[0]} {details[1]} 的预约吗？\n"
                                  f"地点：{area}\n舱位：{pod}{note}", parent=self):
            return
        try:
            username, password, fid_enc = self._record_credentials()
            entry = load_config()["reserve"][self.account_index].get("pod", {}).get("url", DEFAULT_URL)
        except (ValueError, OSError, KeyError, IndexError) as error:
            messagebox.showerror("无法取消", str(error), parent=self)
            return

        def task():
            client = PodClient(username, password, entry)
            client.cancel(record_id.removeprefix("pod:"), include_team=include_team)
            return self._query_all_records(username, password, fid_enc, entry)

        def done(result):
            self._show_records(result)
            if any("静音舱查询失败" in error for error in result["errors"]):
                messagebox.showwarning("取消结果待核对", "取消接口返回成功，但记录查询失败，请刷新核对。", parent=self)
                return
            current = next((r for r in result["records"] if str(r.get("id")) == record_id), None)
            if current and str(current.get("status")) != "7" and str(current.get("isCancel", 0)) != "1":
                messagebox.showwarning("取消结果待核对", "取消接口返回成功，但记录尚未确认取消，请刷新核对。", parent=self)
            else:
                messagebox.showinfo("取消静音舱预约", "已核对：该静音舱预约已取消或已移出当前预约。", parent=self)

        self._work(task, done, "正在取消静音舱预约并核对结果…")

    def _load_rooms(self):
        if self.busy:
            return
        username, password = self.username.get().strip(), self.password.get()
        if not username or not password:
            self.status.set("填写账号密码后可加载学校全部阅览区。")
            return
        try:
            dept_id_enc = parse_dept_id(self.dept_id.get())
        except ValueError as error:
            self.status.set(str(error))
            return

        def task():
            client = self._client(username, password)
            return dept_id_enc, list_rooms(client, dept_id_enc)

        self._work(task, self._show_rooms)

    def _show_rooms(self, result):
        dept_id_enc, rooms = result
        if not rooms:
            self.status.set("学校接口未返回阅览区，请检查账号权限。")
            return
        try:
            current_id = parse_room(self.room.get())
        except ValueError:
            current_id = None
        names = {}
        for room in rooms:
            parts = [str(room.get(key) or "").strip() for key in
                     ("firstLevelName", "secondLevelName", "thirdLevelName")]
            label = " / ".join(part for part in parts if part)
            room_id = str(room["id"])
            names[f"{label or '未命名区域'}（{room_id}）"] = room_id
        self.room_names = names
        self.loaded_dept = dept_id_enc
        self.room_combo.configure(values=list(names))
        self.schedule_room_combo.configure(values=list(names))
        selected = next((name for name, room_id in names.items() if room_id == current_id), None)
        if selected:
            self.room.set(selected)
        elif self.room.get() not in names:
            self.room.set(next(iter(names)))
        try:
            scheduled_id = parse_room(self.schedule_room.get())
        except ValueError:
            scheduled_id = None
        scheduled_name = next((name for name, room_id in names.items() if room_id == scheduled_id), None)
        if scheduled_name:
            self.schedule_room.set(scheduled_name)
        elif scheduled_id not in names.values():
            self.schedule_room.set("")
        self.status.set(f"已加载学校 {len(names)} 个阅览区。")

    def _refresh_schedule(self):
        if self.busy:
            return
        self._update_holiday_hint()
        self._refresh_adjustments()
        self._work(lambda: (task_status(account=self.account_index + 1),
                            task_status(kind="transfer")),
                   self._show_schedule_status, "正在读取 Windows 定时任务…")

    @staticmethod
    def _task_schedule(info):
        """Inspect output -> (HH:MM time, weekday names) of one task."""
        at = (dt.datetime.fromisoformat(info["at"]).strftime("%H:%M")
              if info.get("at") else None)
        mask = info.get("daysOfWeek") or 0
        days = [name for index, (name, _) in enumerate(WEEKDAYS) if mask & (1 << index)]
        return at, days

    @staticmethod
    def _transfer_task_line(info):
        if not info or info.get("state") == "Missing":
            return "转预约任务：未创建（设置里未启用）"
        next_run = info.get("nextRunTime") or "未安排"
        last_run = info.get("lastRunTime") or "尚未运行"
        return (f"转预约任务：{info.get('state')}；下次执行：{next_run}；"
                f"上次执行：{last_run}")

    def _show_schedule_status(self, result):
        if isinstance(result, tuple):
            reserve_result, transfer_info = result
        else:
            reserve_result, transfer_info = result, None
        next_run = reserve_result.get("nextRunTime") or "未安排"
        last_run = reserve_result.get("lastRunTime") or "尚未运行"
        code = reserve_result["lastTaskResult"]
        last_result = ("尚无结果" if code is None else "运行结束，请查看预约结果" if code == 0
                       else "预约未全部完成或运行出错，请查看运行日志" if code == 1
                       else "任务被终止" if code == 267014 else f"结果代码 {code}；请查看运行日志")
        state = {"Missing": "未创建，请保存定时预约以创建任务", "Ready": "已就绪",
                 "Running": "正在运行", "Disabled": "已禁用", "Queued": "排队中",
                 "Unknown": "状态未知"}.get(reserve_result["state"], reserve_result["state"])
        text = (f"任务：{state}\n下次执行：{next_run}\n"
                f"上次执行：{last_run}；{last_result}")
        if transfer_info is not None:
            text += "\n" + self._transfer_task_line(transfer_info)
        self.schedule_status.set(text)
        self.next_task_time.set(str(next_run))
        try:
            next_day = dt.datetime.fromisoformat(str(next_run)).date() + dt.timedelta(days=1)
            self.next_booking_day.set(next_day.isoformat())
        except ValueError:
            self.next_booking_day.set("尚未安排")
        self.status.set("定时任务状态已更新。")

    def _show_transfer_log(self):
        try:
            path = ROOT / "last-transfer.log"
            content = format_transfer_log(path.read_text(encoding="utf-8-sig"),
                                          dt.datetime.fromtimestamp(
                                              path.stat().st_mtime).strftime("%Y-%m-%d %H:%M"))
        except FileNotFoundError:
            content = "尚无转预约日志；当天 22:30 的转预约任务运行后会生成。"
        except OSError as error:
            content = f"读取日志失败：{error}"
        dialog = tk.Toplevel(self)
        dialog.title("转预约结果")
        dialog.geometry("700x520")
        dialog.transient(self)
        frame = ttk.Frame(dialog, padding=16)
        frame.pack(fill="both", expand=True)
        text = tk.Text(frame, wrap="word", font=("Microsoft YaHei UI", 11), padx=14, pady=14)
        scroll = ttk.Scrollbar(frame, orient="vertical", command=text.yview)
        text.configure(yscrollcommand=scroll.set)
        scroll.pack(side="right", fill="y")
        text.pack(side="left", fill="both", expand=True)
        text.insert("1.0", content)
        text.configure(state="disabled")

    def _show_run_log(self):
        try:
            path = ROOT / ("last-run.log" if self.account_index == 0
                           else f"last-run-{self.account_index + 1}.log")
            content = format_run_log(path.read_text(encoding="utf-8-sig"),
                                     dt.datetime.fromtimestamp(path.stat().st_mtime).strftime("%Y-%m-%d %H:%M"))
        except FileNotFoundError:
            content = "尚无运行日志；任务首次执行后会生成。"
        except OSError as error:
            content = f"读取日志失败：{error}"
        dialog = tk.Toplevel(self)
        dialog.title("定时预约结果")
        dialog.geometry("700x520")
        dialog.transient(self)
        frame = ttk.Frame(dialog, padding=16)
        frame.pack(fill="both", expand=True)
        text = tk.Text(frame, wrap="word", font=("Microsoft YaHei UI", 11), padx=14, pady=14)
        scroll = ttk.Scrollbar(frame, orient="vertical", command=text.yview)
        text.configure(yscrollcommand=scroll.set)
        scroll.pack(side="right", fill="y")
        text.pack(side="left", fill="both", expand=True)
        text.insert("1.0", content)
        text.configure(state="disabled")

    def _write_backup_plans(self, plans):
        """Store the backup plans of the current account right away.

        The dialog used to keep them in memory only until "保存定时预约" ran, so
        switching accounts, saving the account (which reloads the form) or closing
        the window silently dropped them. Writing here keeps the config in step
        with what the user just confirmed.
        """
        backups = []
        for index, plan in enumerate(plans, start=1):
            backup_room = str(plan["roomid"])
            backup_seat = str(plan["seatid"][0])
            backup_start, backup_end = plan["time"]
            if backup_room not in self.room_names.values() or not backup_seat.isdecimal() or int(backup_seat) < 1:
                raise ValueError(f"备选方案 {index} 的区域或座位无效，请重新编辑。")
            build_segments(backup_start, backup_end)
            backups.append({"roomid": backup_room, "seatid": [backup_seat.zfill(3)],
                            "time": [backup_start, backup_end]})
        data = load_config()
        data["reserve"][self.account_index]["backups"] = backups
        with CONFIG_PATH.open("w", encoding="utf-8") as file:
            json.dump(data, file, ensure_ascii=False, indent=2)
            file.write("\n")

    def _edit_backups(self):
        try:
            dept_id_enc = parse_dept_id(self.dept_id.get())
        except ValueError:
            dept_id_enc = None
        if not dept_id_enc or self.loaded_dept != dept_id_enc or not self.room_names:
            messagebox.showwarning("请先刷新区域", "请先填写学校选座链接并刷新当前学校的阅览区。", parent=self)
            return

        plans = [dict(plan) for plan in self.backup_plans]
        dialog = tk.Toplevel(self)
        dialog.title("管理定时预约备选方案")
        dialog.geometry("760x520")
        dialog.minsize(700, 470)
        dialog.configure(bg=COLORS["canvas"])
        dialog.transient(self)
        dialog.grab_set()
        body = ttk.Frame(dialog, padding=18)
        body.pack(fill="both", expand=True)
        ttk.Label(body, text="备选方案", style="Eyebrow.TLabel").pack(anchor="w")
        ttk.Label(body, text="按顺序补主方案未成功的时段；不会重复提交已成功或目标范围外的时段。",
                  style="Hint.TLabel").pack(anchor="w", pady=(4, 12))

        table = ttk.Treeview(body, columns=("order", "room", "seat", "time"), show="headings", height=6, selectmode="browse")
        for key, title, width in (("order", "顺序", 60), ("room", "阅览区", 300),
                                  ("seat", "座位", 80), ("time", "时段", 150)):
            table.heading(key, text=title)
            table.column(key, width=width, anchor="w" if key == "room" else "center")
        table.pack(fill="both", expand=True)

        editor = ttk.Frame(body, style="Surface.TFrame", padding=12)
        editor.pack(fill="x", pady=(12, 0))
        room_var = tk.StringVar(value=self.schedule_room.get())
        seat_var = tk.StringVar()
        start_var = tk.StringVar(value=self.schedule_start.get())
        end_var = tk.StringVar(value=self.schedule_end.get())
        ttk.Label(editor, text="阅览区", style="Field.TLabel").grid(row=0, column=0, sticky="w")
        ttk.Label(editor, text="座位号", style="Field.TLabel").grid(row=0, column=1, sticky="w", padx=(10, 0))
        ttk.Label(editor, text="开始", style="Field.TLabel").grid(row=0, column=2, sticky="w", padx=(10, 0))
        ttk.Label(editor, text="结束", style="Field.TLabel").grid(row=0, column=3, sticky="w", padx=(10, 0))
        room_combo = ttk.Combobox(editor, textvariable=room_var, values=list(self.room_names), state="readonly")
        room_combo.grid(row=1, column=0, sticky="ew", pady=(5, 0))
        self._block_mouse_wheel(room_combo)
        ttk.Entry(editor, textvariable=seat_var, width=9).grid(row=1, column=1, sticky="ew", padx=(10, 0), pady=(5, 0))
        start_picker = self._time_picker(editor, start_var)
        start_picker.grid(row=1, column=2, padx=(10, 0), pady=(5, 0))
        end_picker = self._time_picker(editor, end_var)
        end_picker.grid(row=1, column=3, padx=(10, 0), pady=(5, 0))
        editor.columnconfigure(0, weight=1)

        def room_label(roomid):
            return next((name for name, value in self.room_names.items() if value == roomid), roomid)

        def refresh(selected=None):
            table.delete(*table.get_children())
            for index, plan in enumerate(plans):
                table.insert("", "end", iid=str(index), values=(index + 1, room_label(plan["roomid"]),
                            plan["seatid"][0], "–".join(plan["time"])))
            if selected is not None and 0 <= selected < len(plans):
                table.selection_set(str(selected))

        def selected_index():
            selection = table.selection()
            return int(selection[0]) if selection else None

        def select_plan(_event=None):
            index = selected_index()
            if index is None:
                return
            plan = plans[index]
            room_var.set(room_label(plan["roomid"]))
            seat_var.set(str(plan["seatid"][0]))
            start_var.set(plan["time"][0])
            end_var.set(plan["time"][1])

        def form_plan():
            roomid = parse_room(room_var.get())
            if roomid not in self.room_names.values():
                raise ValueError("请选择当前学校的阅览区。")
            seat = seat_var.get().strip()
            if not seat.isdecimal() or int(seat) < 1:
                raise ValueError("备选座位号必须是正整数。")
            start = dt.datetime.strptime(start_var.get(), "%H:%M").strftime("%H:%M")
            end = dt.datetime.strptime(end_var.get(), "%H:%M").strftime("%H:%M")
            build_segments(start, end)
            return {"roomid": roomid, "seatid": [seat.zfill(3)], "time": [start, end]}

        def change(replace=False):
            try:
                plan = form_plan()
            except ValueError as error:
                messagebox.showerror("备选方案有误", str(error), parent=dialog)
                return
            index = selected_index() if replace else None
            if replace and index is None:
                messagebox.showinfo("请选择方案", "先选中要修改的备选方案。", parent=dialog)
                return
            if replace:
                plans[index] = plan
            else:
                plans.append(plan)
                index = len(plans) - 1
            refresh(index)

        def remove():
            index = selected_index()
            if index is not None:
                plans.pop(index)
                refresh(min(index, len(plans) - 1))

        def move(offset):
            index = selected_index()
            if index is None or not 0 <= index + offset < len(plans):
                return
            plans[index], plans[index + offset] = plans[index + offset], plans[index]
            refresh(index + offset)

        table.bind("<<TreeviewSelect>>", select_plan)
        refresh()
        actions = ttk.Frame(body)
        actions.pack(fill="x", pady=(12, 0))
        ttk.Button(actions, text="添加", command=change).pack(side="left")
        ttk.Button(actions, text="更新选中", command=lambda: change(True)).pack(side="left", padx=(7, 0))
        ttk.Button(actions, text="删除", command=remove).pack(side="left", padx=(7, 0))
        ttk.Button(actions, text="上移", command=lambda: move(-1)).pack(side="left", padx=(7, 0))
        ttk.Button(actions, text="下移", command=lambda: move(1)).pack(side="left", padx=(7, 0))

        def save():
            try:
                self._write_backup_plans(plans)
            except (ValueError, OSError, KeyError, IndexError) as error:
                messagebox.showerror("保存备选方案失败", str(error), parent=dialog)
                return
            self.backup_plans = plans
            self.backup_summary.set(f"备选方案（{len(plans)}）")
            self._loaded_schedule = self._schedule_state()
            self.status.set(f"备选方案已保存（{len(plans)} 个）。")
            dialog.destroy()

        def close():
            if plans != self.backup_plans and not messagebox.askyesno(
                    "放弃修改？", "备选方案有未保存的修改，关闭后不会生效。要放弃吗？", parent=dialog):
                return
            dialog.destroy()

        dialog.protocol("WM_DELETE_WINDOW", close)
        ttk.Button(actions, text="完成编辑", style="Accent.TButton", command=save).pack(side="right")
        ttk.Label(body, text="点“完成编辑”会立即写入配置；“保存定时预约”再一并保存时段、星期与执行时间。",
                  style="Hint.TLabel").pack(anchor="w", pady=(9, 0))

    def _update_weekday_summary(self):
        labels = dict(WEEKDAYS)
        ordered = [name for name, _ in WEEKDAYS if name in self.weekday_times]
        if not ordered:
            self.weekday_summary.set("按星期：未设置，每天沿用上面的时段")
            return
        parts = [f"{labels[name]} {'–'.join(self.weekday_times[name])}" for name in ordered]
        self.weekday_summary.set("按星期：" + "、".join(parts) + "；其余星期沿用上面的时段")

    def _edit_weekday_times(self):
        dialog = tk.Toplevel(self)
        dialog.title("按星期设置预约时段")
        dialog.geometry("500x440")
        dialog.minsize(460, 400)
        dialog.configure(bg=COLORS["canvas"])
        dialog.transient(self)
        dialog.grab_set()
        body = ttk.Frame(dialog, padding=18)
        body.pack(fill="both", expand=True)
        ttk.Label(body, text="按预约日星期设置时段", style="Eyebrow.TLabel").pack(anchor="w")
        ttk.Label(body, text="勾选后按该行时段预约那一天；未勾选的星期沿用“次日预约开始/结束”。"
                             "时段指被预约的那一天（通常是次日）。",
                  style="Hint.TLabel", justify="left", wraplength=440).pack(anchor="w", pady=(4, 12))
        grid = ttk.Frame(body, style="Surface.TFrame")
        grid.pack(fill="x")
        labels = dict(WEEKDAYS)
        rows = {}
        for index, (name, label) in enumerate(WEEKDAYS):
            current = self.weekday_times.get(name) or [self.schedule_start.get(), self.schedule_end.get()]
            enabled = tk.BooleanVar(value=name in self.weekday_times)
            start = tk.StringVar(value=current[0])
            end = tk.StringVar(value=current[1])
            ttk.Checkbutton(grid, text=label, variable=enabled).grid(
                row=index, column=0, sticky="w", pady=3)
            self._time_picker(grid, start).grid(row=index, column=1, padx=(10, 0), pady=3)
            ttk.Label(grid, text="至", style="SurfaceHint.TLabel").grid(
                row=index, column=2, padx=6, pady=3)
            self._time_picker(grid, end).grid(row=index, column=3, pady=3)
            rows[name] = (enabled, start, end)
        grid.columnconfigure(4, weight=1)

        def clear_all():
            for enabled, _start, _end in rows.values():
                enabled.set(False)

        def confirm():
            chosen = {}
            for name, (enabled, start, end) in rows.items():
                if not enabled.get():
                    continue
                try:
                    start_text = dt.datetime.strptime(start.get().strip(), "%H:%M").strftime("%H:%M")
                    end_text = dt.datetime.strptime(end.get().strip(), "%H:%M").strftime("%H:%M")
                except ValueError:
                    messagebox.showerror("时间有误", f"{labels[name]}的时间应为 HH:MM。", parent=dialog)
                    return
                if end_text <= start_text:
                    messagebox.showerror("时间有误", f"{labels[name]}的结束时间必须晚于开始时间。",
                                         parent=dialog)
                    return
                chosen[name] = [start_text, end_text]
            self.weekday_times = chosen
            self._update_weekday_summary()
            dialog.destroy()

        actions = ttk.Frame(body)
        actions.pack(fill="x", pady=(14, 0))
        ttk.Button(actions, text="全部清除", command=clear_all).pack(side="left")
        ttk.Button(actions, text="确定", style="Accent.TButton", command=confirm).pack(side="right")
        ttk.Button(actions, text="取消", command=dialog.destroy).pack(side="right", padx=(0, 8))

    def _save_schedule(self):
        if self.busy:
            return
        try:
            username = self.username.get().strip()
            password = self.password.get()
            if not username or not password:
                raise ValueError("请先填写学习通账号和密码。")
            dept_id_enc = parse_dept_id(self.dept_id.get())
            if dept_id_enc != self.loaded_dept:
                raise ValueError("请先刷新该学校的阅览区，再保存定时预约。")
            roomid = parse_room(self.schedule_room.get())
            if roomid not in self.room_names.values():
                raise ValueError("请先刷新该学校的阅览区，再选择定时预约区域。")
            seat = self.schedule_seat.get().strip()
            if not seat.isdecimal() or int(seat) < 1:
                raise ValueError("座位号必须是正整数。")
            start = dt.datetime.strptime(self.schedule_start.get().strip(), "%H:%M").strftime("%H:%M")
            end = dt.datetime.strptime(self.schedule_end.get().strip(), "%H:%M").strftime("%H:%M")
            build_segments(start, end)
            at = dt.datetime.strptime(self.schedule_at.get().strip(), "%H:%M").strftime("%H:%M")
            days = [name for name, _ in WEEKDAYS if self.schedule_days[name].get()]
            if not days:
                raise ValueError("请至少选择一个执行星期。")
            weekday_times = {}
            weekday_labels = dict(WEEKDAYS)
            for name, value in self.weekday_times.items():
                if name not in weekday_labels or not isinstance(value, (list, tuple)) or len(value) != 2:
                    raise ValueError(f"按星期时段有误：{name}")
                try:
                    weekday_start = dt.datetime.strptime(str(value[0]).strip(), "%H:%M").strftime("%H:%M")
                    weekday_end = dt.datetime.strptime(str(value[1]).strip(), "%H:%M").strftime("%H:%M")
                except ValueError:
                    raise ValueError(f"{weekday_labels[name]}的按星期时段格式应为 HH:MM。") from None
                build_segments(weekday_start, weekday_end)
                weekday_times[name] = [weekday_start, weekday_end]
            original = load_config()
            updated = json.loads(json.dumps(original))
            if any(item.get("username") == username for index, item in enumerate(updated["reserve"])
                   if index != self.account_index):
                raise ValueError("账号不能使用相同的用户名。")
            item = updated["reserve"][self.account_index]
            if item.get("username") != username:
                item.pop("classicEveningFirst", None)
            item.update(username=username, password=password, roomid=roomid,
                        seatid=[seat.zfill(3)], time=[start, end], daysofweek=days,
                        scheduleEnabled=True)
            backups = []
            for index, plan in enumerate(self.backup_plans, start=1):
                backup_room = str(plan["roomid"])
                backup_seat = str(plan["seatid"][0])
                backup_start, backup_end = plan["time"]
                if backup_room not in self.room_names.values() or not backup_seat.isdecimal() or int(backup_seat) < 1:
                    raise ValueError(f"备选方案 {index} 的区域或座位无效，请重新编辑。")
                build_segments(backup_start, backup_end)
                backups.append({"roomid": backup_room, "seatid": [backup_seat.zfill(3)],
                                "time": [backup_start, backup_end]})
            item["backups"] = backups
            if weekday_times:
                item[TIMES_BY_WEEKDAY_KEY] = weekday_times
            else:
                item.pop(TIMES_BY_WEEKDAY_KEY, None)
            item["deptIdEnc"] = dept_id_enc
            if self.account_index == 0:
                updated["deptIdEnc"] = dept_id_enc
            item["scheduleAt"] = at
            item["skipHolidays"] = bool(self.skip_holidays.get())
            updated["autoCancelOtherOnSuccess"] = bool(self.auto_cancel_other.get())
            try:
                transfer_at = dt.datetime.strptime(self.transfer_at.get().strip(),
                                                   "%H:%M").strftime("%H:%M")
            except ValueError:
                raise ValueError("转预约时间应为 HH:MM。") from None
            transfer_enabled = bool(self.transfer_enabled.get())
            try:
                current_transfer = transfer_settings(original)
            except ValueError:
                current_transfer = {"from": 2, "to": 1}
            transfer_from, transfer_to = current_transfer["from"], current_transfer["to"]
            if transfer_enabled:
                if len(updated["reserve"]) < 2:
                    raise ValueError("转预约需要至少两个账号。")
                if not (1 <= transfer_from <= len(updated["reserve"])
                        and 1 <= transfer_to <= len(updated["reserve"])
                        and transfer_from != transfer_to):
                    raise ValueError("转预约的 from/to 账号序号无效。")
                source_at = str(updated["reserve"][transfer_from - 1]
                                .get("scheduleAt") or "22:00")
                if transfer_at <= source_at:
                    raise ValueError(
                        f"转预约时间要晚于账号 {transfer_from} 的执行时间（{source_at}）。")
            updated[TRANSFER_KEY] = {"enabled": transfer_enabled, "from": transfer_from,
                                     "to": transfer_to, "at": transfer_at}
            task_days = days
        except (ValueError, KeyError, IndexError, OSError) as error:
            messagebox.showerror("定时设置有误", str(error), parent=self)
            return

        def write_config(data):
            with CONFIG_PATH.open("w", encoding="utf-8") as file:
                json.dump(data, file, ensure_ascii=False, indent=2)
                file.write("\n")

        def sync_transfer_task(data):
            """Create, update or remove the shared 22:30 transfer task."""
            settings = transfer_settings(data)
            if not settings["enabled"]:
                task_status("Remove", kind="transfer")
                return
            source = data["reserve"][settings["from"] - 1]
            source_days = [name for name, _ in WEEKDAYS
                           if name in (source.get("daysofweek") or [])]
            if not source_days:
                raise ValueError("账号2没有设置执行星期，无法创建转预约任务。")
            task_status("Set", settings["at"], source_days,
                        account=settings["from"], kind="transfer")

        def task():
            previous = task_status(account=self.account_index + 1)
            previous_at, previous_days = self._task_schedule(previous)
            previous_transfer = task_status(kind="transfer")
            previous_transfer_at, previous_transfer_days = self._task_schedule(
                previous_transfer)
            reserve_saved = False
            transfer_started = False
            try:
                write_config(updated)
                saved = task_status("Set", at, task_days,
                                    account=self.account_index + 1)
                reserve_saved = True
                transfer_started = True
                sync_transfer_task(updated)
                return saved, task_status(kind="transfer")
            except Exception:
                write_config(original)
                if reserve_saved:
                    try:
                        if previous_at:
                            # Inspect returns the pre-warm trigger time; Set accepts
                            # the opening time and subtracts one minute itself.
                            opening = (dt.datetime.strptime(previous_at, "%H:%M")
                                       + dt.timedelta(minutes=1)).strftime("%H:%M")
                            task_status("Set", opening, previous_days,
                                        account=self.account_index + 1)
                        elif previous.get("state") == "Missing":
                            task_status("Remove", account=self.account_index + 1)
                    except Exception as rollback_error:
                        raise RuntimeError("保存未完成，恢复原预约任务也失败，请刷新并检查定时任务状态。") from rollback_error
                if transfer_started:
                    try:
                        if previous_transfer_at:
                            task_status("Set", previous_transfer_at,
                                        previous_transfer_days, kind="transfer")
                        elif previous_transfer.get("state") == "Missing":
                            task_status("Remove", kind="transfer")
                    except Exception as rollback_error:
                        raise RuntimeError("保存未完成，恢复转预约任务也失败，请刷新并检查定时任务状态。") from rollback_error
                raise

        def done(result):
            self.saved_username = username
            self._update_weekday_summary()
            self._show_schedule_status(result)
            self._update_holiday_hint()
            self._loaded_schedule = self._schedule_state()
            self.status.set("定时预约已保存；按所选星期和时间预约次日座位。" +
                            ("转预约任务已同步。" if self.transfer_enabled.get() else ""))

        self._work(task, done, "正在保存定时预约…")

    def _query(self):
        if self.busy:
            return
        try:
            form = self._form()
        except ValueError as error:
            messagebox.showerror("查询条件有误", str(error), parent=self)
            return
        username, password, day, roomid, start, end = form
        self.last_query = None
        self.tree.delete(*self.tree.get_children())

        def task():
            client = self._client(username, password)
            free, room_name = available_seats(client, day, roomid, start, end)
            return form, free, room_name, dt.datetime.now(LOCAL_TZ)

        self._work(task, self._show_query)

    def _show_query(self, result):
        form, free, room_name, checked_at = result
        self.last_query = form
        for seat_num in free:
            self.tree.insert("", "end", iid=seat_num, values=(seat_num, "全时段可预约"))
        self.summary.set(f"{room_name} · {form[2]} {form[4]}–{form[5]} · 可预约 {len(free)} 个座位")
        self.status.set(f"查询完成：{checked_at:%H:%M:%S}。座位状态可能随时变化，预约时会再次核对。")

    def _update_book_button(self):
        enabled = not self.busy and self.last_query is not None and bool(self.tree.selection())
        self.book_button.configure(state="normal" if enabled else "disabled")

    def _book(self):
        if self.busy or not self.tree.selection() or self.last_query is None:
            return
        try:
            form = self._form()
            if form != self.last_query:
                raise ValueError("查询条件已改变，请重新查询座位。")
            target_day = dt.date.fromisoformat(form[2])
            today = dt.datetime.now(LOCAL_TZ).date()
            if target_day not in (today, today + dt.timedelta(days=1)):
                raise ValueError("目前只能提交今天或明天的预约。")
            seat_num = self.tree.selection()[0]
            segments = build_segments(form[4], form[5])
        except ValueError as error:
            messagebox.showerror("无法预约", str(error), parent=self)
            return

        username, password, day, roomid, start, end = form

        def task():
            client = self._client(username, password, reserve_next_day=(target_day != today))
            free, _ = available_seats(client, day, roomid, start, end)
            if seat_num not in free:
                return seat_num, [], "该座位已不再全时段可预约，请重新查询。"

            original_post = client.requests.post
            response_holder = {"submit": None}

            def capture(*args, **kwargs):
                response = original_post(*args, **kwargs)
                url = kwargs.get("url") or (args[0] if args else "")
                if "/seat/submit" in url:
                    response_holder["submit"] = response
                return response

            client.requests.post = capture
            results = []
            for segment in segments:
                client.max_attempt = 2
                response_holder["submit"] = None
                try:
                    success = client.submit(segment, roomid, [seat_num], False)
                    reply = response_holder["submit"].json() if response_holder["submit"] is not None else {}
                    record = (reply.get("data") or {}).get("seatReserve") or {}
                    results.append((segment, bool(success), reply.get("msg") or "", record))
                except Exception as error:
                    results.append((segment, False, type(error).__name__, {}))
            return seat_num, results, None

        self._work(task, self._show_booking)

    def _show_booking(self, result):
        seat_num, segments, error = result
        if error:
            self.status.set(error)
            messagebox.showwarning("预约未提交", error, parent=self)
            return
        lines = []
        for (start, end), success, message, record in segments:
            status = "成功" if success else f"失败：{message or '服务器未确认'}"
            line = f"{start}–{end}：{status}"
            if success and record:
                line += f"\n  记录 ID：{record.get('id', '未知')}；记录时段：{format_record_time(record.get('startTime'))}–{format_record_time(record.get('endTime'))}"
            lines.append(line)
        all_ok = all(item[1] for item in segments)
        title = "预约结果" if all_ok else "部分时段未预约成功"
        messagebox.showinfo(title, f"{seat_num} 号座位\n\n" + "\n\n".join(lines) + "\n\n请在学习通“我的预约”核对并按要求签到。", parent=self)
        self.status.set("预约请求已完成；请在学习通核对最终记录。")
        self.last_query = None
        self.tree.delete(*self.tree.get_children())
        self.summary.set("预约后座位状态已变化，请重新查询。")


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "--pod-run":
        from pod_reservation import main as pod_main
        sys.argv.pop(1)
        account = int(sys.argv[sys.argv.index("--account") + 1]) if "--account" in sys.argv else 1
        log_name = f"last-pod-{account}-check.log" if "--check-only" in sys.argv else f"last-pod-{account}.log"
        with (ROOT / log_name).open("w", encoding="utf-8") as log:
            sys.stdout = sys.stderr = log
            try:
                sys.exit(pod_main())
            except Exception as error:
                print(f"静音舱预约失败：{error}")
                sys.exit(1)
    if len(sys.argv) > 1 and sys.argv[1] == "--scheduled-run":
        from reserve_segments import main as scheduled_main

        sys.argv.pop(1)
        original_stdout, original_stderr = sys.stdout, sys.stderr
        account = int(sys.argv[sys.argv.index("--account") + 1]) if "--account" in sys.argv else 1
        with (ROOT / ("last-run.log" if account == 1 else f"last-run-{account}.log")).open("w", encoding="utf-8") as log:
            sys.stdout = log
            sys.stderr = log
            try:
                exit_code = scheduled_main()
            except Exception:
                import traceback
                traceback.print_exc()
                exit_code = 1
            finally:
                sys.stdout, sys.stderr = original_stdout, original_stderr
        sys.exit(exit_code)
    if len(sys.argv) > 1 and sys.argv[1] == "--transfer-run":
        from transfer_seats import main as transfer_main

        sys.argv.pop(1)
        # A read-only check must not overwrite the real last-transfer.log.
        check_only = "--check-only" in sys.argv
        log_name = "last-transfer-check.log" if check_only else "last-transfer.log"
        original_stdout, original_stderr = sys.stdout, sys.stderr
        with (ROOT / log_name).open("w", encoding="utf-8") as log:
            sys.stdout = log
            sys.stderr = log
            try:
                exit_code = transfer_main()
            except Exception:
                import traceback
                traceback.print_exc()
                exit_code = 1
            finally:
                sys.stdout, sys.stderr = original_stdout, original_stderr
        sys.exit(exit_code)
    SeatApp().mainloop()
