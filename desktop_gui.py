"""Windows desktop UI for querying and reserving ChaoXing library seats."""

import calendar
import datetime as dt
import json
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
from reserve_segments import build_segments
from utils import reserve


ROOT = Path(sys.executable if getattr(sys, "frozen", False) else __file__).resolve().parent
CONFIG_PATH = ROOT / "config.local.json"
ROOMS = {
    "三楼经典文库阅览学习区（11031）": "11031",
    "三楼北面阅览学习区（11032）": "11032",
}
SCHOOL_DEPT_ID_ENC = "5f39e4d995092852"
COLORS = {
    "ink": "#172B32", "forest": "#245E50", "forest_hover": "#1B4D41",
    "canvas": "#F2F5F3", "surface": "#FFFFFF", "line": "#D9E3DE",
    "muted": "#61746D", "soft": "#E9F1EC", "amber": "#A86525",
}
WEEKDAYS = [
    ("Sunday", "周日"), ("Monday", "周一"), ("Tuesday", "周二"),
    ("Wednesday", "周三"), ("Thursday", "周四"),
    ("Friday", "周五"), ("Saturday", "周六"),
]


def load_config():
    with CONFIG_PATH.open(encoding="utf-8") as file:
        return json.load(file)


def task_status(action="Inspect", at=None, days=None):
    command = ["powershell.exe", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
               "-File", str(ROOT / "schedule_task.ps1"), "-Action", action]
    if action == "Set":
        command += ["-At", at, "-Days", ",".join(days)]
    result = subprocess.run(command, capture_output=True, text=True, timeout=30,
                            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    if result.returncode:
        raise RuntimeError(result.stderr.strip() or result.stdout.strip() or "Windows 定时任务操作失败。")
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


class SeatApp(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("超星图书馆座位预约")
        self.geometry("1040x820")
        self.minsize(860, 820)
        self.configure(bg=COLORS["canvas"])
        self.events = queue.Queue()
        self.busy = False
        self.last_query = None
        self.room_names = dict(ROOMS)
        self.loaded_dept = None
        self.saved_username = ""
        self._style()
        self._build()
        self._load_fields()
        self.username.trace_add("write", self._account_changed)
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

    def _build(self):
        header = tk.Frame(self, bg=COLORS["ink"], padx=28, pady=19)
        header.pack(fill="x")
        tk.Label(header, text="LIBRARY  /  SEAT DESK", bg=COLORS["ink"], fg="#9FC7B5",
                 font=("Segoe UI", 9, "bold")).pack(anchor="w")
        tk.Label(header, text="图书馆座位预约", bg=COLORS["ink"], fg="white",
                 font=("Microsoft YaHei UI", 21, "bold")).pack(anchor="w", pady=(5, 0))
        tk.Label(header, text="选择阅览区与时段，查询空座；也可以设定自动预约。", bg=COLORS["ink"],
                 fg="#C4D5CE", font=("Microsoft YaHei UI", 10)).pack(anchor="w", pady=(3, 0))

        outer = ttk.Frame(self, padding=(24, 17, 24, 13))
        outer.pack(fill="both", expand=True)
        ttk.Label(outer, text="01  账号与学校", style="Eyebrow.TLabel").pack(anchor="w", pady=(0, 8))
        account = ttk.Frame(outer, style="Surface.TFrame", padding=15)
        account.pack(fill="x", pady=(0, 15))
        self.username = tk.StringVar()
        self.password = tk.StringVar()
        self.show_password = tk.BooleanVar(value=False)
        ttk.Label(account, text="学习通账号", style="Field.TLabel").grid(row=0, column=0, sticky="w")
        ttk.Label(account, text="密码", style="Field.TLabel").grid(row=0, column=1, sticky="w", padx=(14, 0))
        ttk.Entry(account, textvariable=self.username).grid(row=1, column=0, sticky="ew", pady=(5, 0))
        self.password_entry = ttk.Entry(account, textvariable=self.password, show="●", width=28)
        self.password_entry.grid(row=1, column=1, sticky="ew", padx=(14, 0), pady=(5, 0))
        ttk.Checkbutton(account, text="显示密码", variable=self.show_password, command=self._toggle_password).grid(row=1, column=2, padx=(12, 0))
        ttk.Button(account, text="保存账号", command=self._save_account).grid(row=1, column=3, padx=(12, 0))
        account.columnconfigure(0, weight=1)
        account.columnconfigure(1, weight=1)
        ttk.Label(account, text="账号信息保存在本机 config.local.json，定时任务使用同一份配置。",
                  style="SurfaceHint.TLabel").grid(row=2, column=0, columnspan=4, sticky="w", pady=(10, 0))

        tabs = ttk.Notebook(outer)
        tabs.pack(fill="both", expand=True)
        manual_tab = ttk.Frame(tabs, style="Surface.TFrame", padding=19)
        schedule_tab = ttk.Frame(tabs, style="Surface.TFrame", padding=19)
        tabs.add(manual_tab, text="02  手动查询与预约")
        tabs.add(schedule_tab, text="03  定时预约")
        tabs.bind("<<NotebookTabChanged>>", lambda _event: self._refresh_schedule()
                  if tabs.index(tabs.select()) == 1 else None)

        search = ttk.Frame(manual_tab, style="Surface.TFrame")
        search.pack(fill="x", pady=(0, 15))
        ttk.Label(search, text="查找可预约座位", style="Section.TLabel").grid(row=0, column=0, columnspan=4, sticky="w")
        ttk.Label(search, text="学校选座链接或 deptIdEnc", style="Field.TLabel").grid(row=1, column=0, columnspan=4, sticky="w", pady=(13, 0))
        self.dept_id = tk.StringVar()
        self.room = tk.StringVar(value=list(ROOMS)[1])
        self.day = tk.StringVar(value=dt.datetime.now(LOCAL_TZ).date().isoformat())
        self.start = tk.StringVar(value="18:30")
        self.end = tk.StringVar(value="21:00")
        ttk.Entry(search, textvariable=self.dept_id).grid(row=2, column=0, columnspan=4, sticky="ew", pady=(5, 12))
        ttk.Label(search, text="阅览区", style="Field.TLabel").grid(row=3, column=0, sticky="w")
        room_box = ttk.Frame(search, style="Surface.TFrame")
        room_box.grid(row=4, column=0, sticky="ew", padx=(0, 14), pady=(5, 12))
        self.room_combo = ttk.Combobox(room_box, textvariable=self.room, values=list(ROOMS), width=33)
        self.room_combo.pack(side="left", fill="x", expand=True)
        self.refresh_rooms_button = ttk.Button(room_box, text="刷新区域", command=self._load_rooms)
        self.refresh_rooms_button.pack(side="left", padx=(8, 0))
        ttk.Label(search, text="预约日期", style="Field.TLabel").grid(row=3, column=1, sticky="w")
        day_picker, self.day_year, self.day_month, self.day_number = self._date_picker(search, self.day)
        day_picker.grid(row=4, column=1, sticky="w", padx=(0, 8), pady=(5, 12))
        ttk.Button(search, text="今天", command=lambda: self._set_day(0)).grid(row=4, column=2, padx=(0, 5), pady=(5, 12))
        ttk.Button(search, text="明天", command=lambda: self._set_day(1)).grid(row=4, column=3, pady=(5, 12))
        ttk.Label(search, text="开始时间", style="Field.TLabel").grid(row=5, column=0, sticky="w")
        start_picker, self.start_hour, self.start_minute = self._time_picker(search, self.start)
        start_picker.grid(row=6, column=0, sticky="w", pady=(5, 0))
        ttk.Label(search, text="结束时间", style="Field.TLabel").grid(row=5, column=1, sticky="w")
        end_picker, self.end_hour, self.end_minute = self._time_picker(search, self.end)
        end_picker.grid(row=6, column=1, sticky="w", pady=(5, 0))
        self.query_button = ttk.Button(search, text="查询可预约座位", style="Accent.TButton", command=self._query)
        self.query_button.grid(row=6, column=2, columnspan=2, sticky="e")
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

        ttk.Label(schedule_tab, text="预约次日座位", style="Section.TLabel").pack(anchor="w")
        ttk.Label(schedule_tab, text="到设定时间自动运行。请保持电脑开机，并确保账号已保存。",
                  style="SurfaceHint.TLabel").pack(anchor="w", pady=(4, 19))
        form = ttk.Frame(schedule_tab, style="Surface.TFrame")
        form.pack(fill="x")
        self.schedule_at = tk.StringVar(value="22:00")
        self.schedule_room = tk.StringVar(value="11031")
        self.schedule_seat = tk.StringVar(value="150")
        self.schedule_start = tk.StringVar(value="11:00")
        self.schedule_end = tk.StringVar(value="21:00")
        self.backup_plans = []
        self.backup_summary = tk.StringVar(value="备选方案（0）")
        self.schedule_days = {name: tk.BooleanVar() for name, _ in WEEKDAYS}
        ttk.Label(form, text="执行时间（本机时间）", style="Field.TLabel").grid(row=0, column=0, sticky="w")
        at_picker, self.schedule_at_hour, self.schedule_at_minute = self._time_picker(form, self.schedule_at)
        at_picker.grid(row=1, column=0, sticky="w", padx=(0, 14), pady=(5, 15))
        ttk.Label(form, text="阅览区", style="Field.TLabel").grid(row=0, column=1, sticky="w")
        self.schedule_room_combo = ttk.Combobox(form, textvariable=self.schedule_room, values=list(ROOMS))
        self.schedule_room_combo.grid(row=1, column=1, columnspan=2, sticky="ew", pady=(5, 15))
        ttk.Label(form, text="座位号", style="Field.TLabel").grid(row=2, column=0, sticky="w")
        ttk.Entry(form, textvariable=self.schedule_seat).grid(row=3, column=0, sticky="ew", padx=(0, 14), pady=(5, 15))
        ttk.Label(form, text="次日预约开始", style="Field.TLabel").grid(row=2, column=1, sticky="w")
        schedule_start_picker, self.schedule_start_hour, self.schedule_start_minute = self._time_picker(form, self.schedule_start)
        schedule_start_picker.grid(row=3, column=1, sticky="w", padx=(0, 14), pady=(5, 15))
        ttk.Label(form, text="次日预约结束", style="Field.TLabel").grid(row=2, column=2, sticky="w")
        schedule_end_picker, self.schedule_end_hour, self.schedule_end_minute = self._time_picker(form, self.schedule_end)
        schedule_end_picker.grid(row=3, column=2, sticky="w", pady=(5, 15))
        ttk.Label(form, text="执行星期（预约次日）", style="Field.TLabel").grid(row=4, column=0, columnspan=3, sticky="w")
        days_frame = ttk.Frame(form, style="Surface.TFrame")
        days_frame.grid(row=5, column=0, columnspan=3, sticky="w", pady=(5, 15))
        for name, label in WEEKDAYS:
            ttk.Checkbutton(days_frame, text=label, variable=self.schedule_days[name]).pack(side="left", padx=(0, 18))
        form.columnconfigure(0, weight=1)
        form.columnconfigure(1, weight=2)
        form.columnconfigure(2, weight=1)
        ttk.Separator(schedule_tab).pack(fill="x", pady=(12, 4))
        controls = ttk.Frame(schedule_tab, style="Surface.TFrame")
        controls.pack(fill="x", pady=(10, 15))
        self.schedule_save_button = ttk.Button(controls, text="保存定时预约", style="Accent.TButton",
                                               command=self._save_schedule)
        self.schedule_save_button.pack(side="left")
        ttk.Button(controls, textvariable=self.backup_summary, command=self._edit_backups).pack(side="left", padx=(10, 0))
        ttk.Button(controls, text="刷新任务状态", command=self._refresh_schedule).pack(side="left", padx=(12, 0))
        ttk.Button(controls, text="查看运行日志", command=self._show_run_log).pack(side="left", padx=(12, 0))
        self.schedule_status = tk.StringVar(value="点击“刷新任务状态”查看下一次运行时间。")
        ttk.Label(schedule_tab, text="任务状态", style="Section.TLabel").pack(anchor="w", pady=(10, 5))
        ttk.Label(schedule_tab, textvariable=self.schedule_status, justify="left", style="Surface.TLabel").pack(anchor="w")
        ttk.Label(schedule_tab, text="已成功时段会保留；备选方案只补主方案目标内的空缺。单次最多 4 小时，结果见 last-run.log。",
                  style="SurfaceHint.TLabel").pack(anchor="w", pady=(12, 0))

        self.status = tk.StringVar(value="就绪")
        status_bar = ttk.Frame(outer, style="Soft.TFrame", padding=(12, 7))
        status_bar.pack(fill="x", pady=(12, 0))
        ttk.Label(status_bar, text="●  程序状态", style="Soft.TLabel").pack(side="left", padx=(0, 13))
        ttk.Label(status_bar, textvariable=self.status, style="Soft.TLabel").pack(side="left")

    @staticmethod
    def _time_picker(parent, value):
        hour, minute = value.get().split(":")
        hour_var = tk.StringVar(value=hour)
        minute_var = tk.StringVar(value=minute)
        syncing = False
        frame = ttk.Frame(parent, style="Surface.TFrame")
        ttk.Combobox(frame, textvariable=hour_var, values=[f"{n:02d}" for n in range(24)],
                     state="readonly", width=4, height=12).pack(side="left")
        ttk.Label(frame, text="时", style="Surface.TLabel").pack(side="left", padx=(4, 8))
        ttk.Combobox(frame, textvariable=minute_var, values=[f"{n:02d}" for n in range(60)],
                     state="readonly", width=4, height=12).pack(side="left")
        ttk.Label(frame, text="分", style="Surface.TLabel").pack(side="left", padx=(4, 0))
        def from_parts(*_args):
            nonlocal syncing
            if syncing:
                return
            syncing = True
            try:
                value.set(f"{hour_var.get()}:{minute_var.get()}")
            finally:
                syncing = False

        def from_value(*_args):
            nonlocal syncing
            if syncing:
                return
            try:
                new_hour, new_minute = value.get().split(":")
            except ValueError:
                return
            syncing = True
            try:
                hour_var.set(new_hour)
                minute_var.set(new_minute)
            finally:
                syncing = False

        hour_var.trace_add("write", from_parts)
        minute_var.trace_add("write", from_parts)
        value.trace_add("write", from_value)
        return frame, hour_var, minute_var

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
            account = data["reserve"][0]
            self.username.set(account.get("username", ""))
            self.password.set(account.get("password", ""))
            self.saved_username = self.username.get().strip()
            self.dept_id.set(data.get("deptIdEnc", SCHOOL_DEPT_ID_ENC))
            self.schedule_at.set(data.get("scheduleAt", "22:00"))
            self.schedule_room.set(str(account.get("roomid", "")))
            seats = account.get("seatid") or []
            self.schedule_seat.set(str(seats[0]) if seats else "")
            period = account.get("time") or []
            if len(period) == 2:
                self.schedule_start.set(period[0])
                self.schedule_end.set(period[1])
            self.backup_plans = list(account.get("backups") or [])
            self.backup_summary.set(f"备选方案（{len(self.backup_plans)}）")
            for name, _ in WEEKDAYS:
                self.schedule_days[name].set(name in account.get("daysofweek", []))
        except (OSError, ValueError, KeyError, IndexError) as error:
            self.status.set(f"读取本机账号配置失败：{error}")

    def _account_changed(self, *_args):
        if self.username.get().strip() != self.saved_username:
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

    def _save_account(self):
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
            data["reserve"][0]["username"] = username
            data["reserve"][0]["password"] = password
            data["deptIdEnc"] = dept_id_enc
            with CONFIG_PATH.open("w", encoding="utf-8") as file:
                json.dump(data, file, ensure_ascii=False, indent=2)
                file.write("\n")
            self.status.set("账号已保存到本机配置文件。")
            self.saved_username = username
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
                if error:
                    self.status.set("操作失败。")
                    messagebox.showerror("操作失败", str(error), parent=self)
                else:
                    callback(result)
                self._update_book_button()
        except queue.Empty:
            pass
        self.after(100, self._drain_events)

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
        self._work(lambda: task_status(), self._show_schedule_status, "正在读取 Windows 定时任务…")

    def _show_schedule_status(self, result):
        next_run = result.get("nextRunTime") or "未安排"
        last_run = result.get("lastRunTime") or "尚未运行"
        code = result["lastTaskResult"]
        last_result = "尚无结果" if code is None else "成功" if code == 0 else "任务被终止" if code == 267014 else f"结果代码 {code}"
        self.schedule_status.set(
            f"任务：{result['state']}\n下次执行：{next_run}\n"
            f"上次执行：{last_run}；{last_result}"
        )
        self.status.set("定时任务状态已更新。")

    def _show_run_log(self):
        try:
            lines = (ROOT / "last-run.log").read_text(encoding="utf-8-sig").splitlines()
            content = "\n".join(lines[-30:]) or "日志为空。"
        except FileNotFoundError:
            content = "尚无运行日志；任务首次执行后会生成。"
        except OSError as error:
            content = f"读取日志失败：{error}"
        messagebox.showinfo("定时任务运行日志", content, parent=self)

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
        ttk.Combobox(editor, textvariable=room_var, values=list(self.room_names), state="readonly").grid(
            row=1, column=0, sticky="ew", pady=(5, 0))
        ttk.Entry(editor, textvariable=seat_var, width=9).grid(row=1, column=1, sticky="ew", padx=(10, 0), pady=(5, 0))
        start_picker, _, _ = self._time_picker(editor, start_var)
        start_picker.grid(row=1, column=2, padx=(10, 0), pady=(5, 0))
        end_picker, _, _ = self._time_picker(editor, end_var)
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
            self.backup_plans = plans
            self.backup_summary.set(f"备选方案（{len(plans)}）")
            dialog.destroy()

        ttk.Button(actions, text="完成编辑", style="Accent.TButton", command=save).pack(side="right")
        ttk.Label(body, text="完成编辑后，请回到主窗口点击“保存定时预约”才会写入配置。",
                  style="Hint.TLabel").pack(anchor="w", pady=(9, 0))

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
            original = load_config()
            updated = json.loads(json.dumps(original))
            item = updated["reserve"][0]
            item.update(username=username, password=password, roomid=roomid,
                        seatid=[seat.zfill(3)], time=[start, end], daysofweek=days)
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
            updated["deptIdEnc"] = dept_id_enc
            updated["scheduleAt"] = at
        except (ValueError, KeyError, IndexError, OSError) as error:
            messagebox.showerror("定时设置有误", str(error), parent=self)
            return

        def write_config(data):
            with CONFIG_PATH.open("w", encoding="utf-8") as file:
                json.dump(data, file, ensure_ascii=False, indent=2)
                file.write("\n")

        def task():
            previous = task_status()
            previous_at = (dt.datetime.fromisoformat(previous["at"]).strftime("%H:%M")
                           if previous["at"] else None)
            previous_days = [name for index, (name, _) in enumerate(WEEKDAYS)
                             if previous["daysOfWeek"] & (1 << index)]
            try:
                write_config(updated)
                return task_status("Set", at, days)
            except Exception:
                write_config(original)
                if previous_at:
                    try:
                        task_status("Set", previous_at, previous_days)
                    except Exception:
                        pass
                raise

        def done(result):
            self.saved_username = username
            self._show_schedule_status(result)
            self.status.set("定时预约已保存；按所选星期和时间预约次日座位。")

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
    if len(sys.argv) > 1 and sys.argv[1] == "--scheduled-run":
        from reserve_segments import main as scheduled_main

        sys.argv.pop(1)
        original_stdout, original_stderr = sys.stdout, sys.stderr
        with (ROOT / "last-run.log").open("w", encoding="utf-8") as log:
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
    SeatApp().mainloop()
