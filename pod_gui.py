"""Pod controls, kept separate from the existing seat booking UI."""
import datetime as dt
import json
import queue
import threading
import tkinter as tk
from tkinter import ttk, messagebox

from pod_reservation import DEFAULT_URL, MULTI_IDS, MULTI_STARTS, TZ, PodClient, validate_plan, validate_schedule
from pod_team import COLLEGES, book_with_accounts, validate_team_config


class PodControls:
    def build_controls(self, config_path, account, mode="both"):
        self.mode = mode
        self.config_path, self.account = config_path, account
        self.events, self.busy = queue.Queue(), False
        self.pods, self.query_result = {}, None
        data = self.read_config()["reserve"][account]
        self.identity = data.get("username")
        settings = data.get("pod", {})
        from desktop_gui import WEEKDAYS, SeatApp
        self.schedule_at = tk.StringVar(value=settings.get("scheduleAt", "22:00"))
        self.schedule_days = {name: tk.BooleanVar(value=name in settings.get("daysofweek", [d for d, _ in WEEKDAYS]))
                              for name, _ in WEEKDAYS}
        self.task_state = tk.StringVar(value="点击“刷新任务状态”查看下一次运行时间。")
        self.url = tk.StringVar(value=settings.get("url", DEFAULT_URL))
        self.pod = tk.StringVar(value=settings.get("itemId", "38541"))
        self.start = tk.StringVar(value=settings.get("start", "16:00"))
        self.day = tk.StringVar(value=str(dt.datetime.now(TZ).date() + dt.timedelta(days=dt.datetime.now(TZ).hour >= 22)))
        self.enabled = tk.BooleanVar(value=settings.get("enabled", False))
        self.activity = tk.StringVar(value=settings.get("activity", "多人学习"))
        profiles = settings.get("teamProfiles", [u.get("podProfile", {}) for u in self.read_config()["reserve"][:2]])
        self.team_profiles = [{key: tk.StringVar(value=(profiles[i] if i < len(profiles) else {}).get(key, ""))
                               for key in ("name", "studentId", "college", "phone")} for i in range(2)]
        self.status = tk.StringVar(value="先刷新舱位，再查询所选日期的时段。" if mode != "schedule" else "刷新并选择舱位，填写事由后保存定时预约。")
        canvas = tk.Canvas(self, highlightthickness=0)
        scrollbar = ttk.Scrollbar(self, orient="vertical", command=canvas.yview)
        scrollbar.pack(side="right", fill="y")
        canvas.pack(side="left", fill="both", expand=True)
        canvas.configure(yscrollcommand=scrollbar.set)
        body = ttk.Frame(canvas, padding=20)
        body_id = canvas.create_window((0, 0), window=body, anchor="nw")
        body.bind("<Configure>", lambda _event: canvas.configure(scrollregion=canvas.bbox("all")))
        canvas.bind("<Configure>", lambda event: canvas.itemconfigure(body_id, width=event.width))
        ttk.Label(body, text="静音舱预约 · 单人 / 多人", font=("Microsoft YaHei UI", 17, "bold")).pack(anchor="w")
        ttk.Label(body, text="单人舱每段 1 小时，多人舱每段 90 分钟。" + ("" if mode == "manual" else "按所选星期和执行时间预约次日；22:00 开放。请保持电脑开机。"), wraplength=700).pack(anchor="w", pady=8)
        ttk.Label(body, text="静音舱入口链接").pack(anchor="w")
        ttk.Entry(body, textvariable=self.url).pack(fill="x", pady=5)
        row = ttk.Frame(body)
        row.pack(fill="x", pady=8)
        self.combo = ttk.Combobox(row, textvariable=self.pod, state="readonly", width=32)
        self.combo.pack(side="left")
        self.combo.bind("<<ComboboxSelected>>", self.update_team_controls)
        ttk.Button(row, text="刷新舱位", command=self.load_pods).pack(side="left", padx=10)
        self.team_frame = ttk.LabelFrame(body, text="多人舱：账号 1、2 组队（当前账号发起，另一账号加入）", padding=6)
        manual_row = ttk.Frame(body)
        ttk.Label(self.team_frame, text="研讨主题").grid(row=0, column=0, sticky="w")
        ttk.Entry(self.team_frame, textvariable=self.activity, width=24).grid(row=0, column=1, columnspan=2, sticky="ew")
        ttk.Label(self.team_frame, text="身份：学生 · 参与人数：2").grid(row=0, column=3, columnspan=2, sticky="w")
        for i, profile in enumerate(self.team_profiles):
            ttk.Label(self.team_frame, text=f"账号 {i+1}").grid(row=i*2+1, column=0, sticky="w")
            for col, (key, label) in enumerate((("name", "姓名"), ("studentId", "学号"), ("phone", "手机"))):
                ttk.Label(self.team_frame, text=label).grid(row=i*2+1, column=col*2+1)
                ttk.Entry(self.team_frame, textvariable=profile[key], width=14).grid(row=i*2+1, column=col*2+2, padx=3)
            ttk.Label(self.team_frame, text="学院").grid(row=i*2+2, column=1)
            ttk.Combobox(self.team_frame, textvariable=profile["college"], values=COLLEGES, state="readonly", width=25).grid(row=i*2+2, column=2, columnspan=3, sticky="w")
        if mode != "schedule":
            manual_row.pack(fill="x", pady=4)
        row = manual_row
        ttk.Label(row, text="日期").pack(side="left")
        ttk.Entry(row, textvariable=self.day, width=12).pack(side="left", padx=5)
        ttk.Button(row, text="查询时段", command=self.query).pack(side="left")
        ttk.Label(body, textvariable=self.status, wraplength=700).pack(anchor="w", pady=4)
        self.tree = ttk.Treeview(body, columns=("time", "state"), show="headings", height=7)
        self.tree.heading("time", text="学校开放时段")
        self.tree.heading("state", text="当前占用情况")
        if mode != "schedule":
            self.tree.pack(fill="both", expand=True, pady=8)
        self.tree.bind("<<TreeviewSelect>>", self.select_slot)
        ttk.Label(body, text="预约事由（1—300 字，保存后手动和定时预约共用）").pack(anchor="w")
        self.reason = tk.Text(body, height=3, wrap="word")
        self.reason.insert("1.0", settings.get("remark", ""))
        self.reason.pack(fill="x", pady=5)
        row = ttk.Frame(body)
        if mode != "manual":
            row.pack(fill="x", pady=6)
        ttk.Label(row, text="定时目标开始时间").pack(side="left")
        self.start_combo = ttk.Combobox(row, textvariable=self.start, values=[f"{h:02}:00" for h in range(8, 22)], state="readonly", width=7)
        self.start_combo.pack(side="left", padx=8)
        ttk.Checkbutton(row, text="启用定时预约", variable=self.enabled).pack(side="left")
        if mode != "manual":
            schedule_row = ttk.Frame(body)
            schedule_row.pack(fill="x", pady=6)
            ttk.Label(schedule_row, text="执行时间（本机时间）").pack(side="left")
            SeatApp._time_picker(schedule_row, self.schedule_at).pack(side="left", padx=8)
            ttk.Label(body, text="执行星期（预约次日）").pack(anchor="w")
            days_row = ttk.Frame(body)
            days_row.pack(fill="x", pady=6)
            for name, label in WEEKDAYS:
                ttk.Checkbutton(days_row, text=label, variable=self.schedule_days[name]).pack(side="left", padx=(0, 9))
        row = ttk.Frame(body)
        row.pack(fill="x", pady=8)
        ttk.Button(row, text="保存静音舱设置" if mode == "manual" else "保存静音舱定时预约", command=self.save).pack(side="left")
        if mode != "schedule":
            ttk.Button(row, text="预约选中时段", command=self.book).pack(side="left", padx=10)
        ttk.Button(row, text="查看静音舱记录", command=self.records).pack(side="left")
        if mode != "manual":
            ttk.Button(row, text="刷新任务状态", command=self.refresh_schedule).pack(side="left", padx=10)
            ttk.Button(row, text="查看定时日志", command=self.show_log).pack(side="left", padx=10)
            ttk.Label(body, textvariable=self.task_state, wraplength=700).pack(anchor="w", pady=5)
        ttk.Label(body, text="空闲仅表示当前无占用，开放日期、次数和账号资格仍以学校提交校验为准。", wraplength=700).pack(anchor="w", pady=5)
        self.saved_settings = self.settings()
        self.update_team_controls()
        self.poll_id = self.after(100, self.drain)

    def read_config(self):
        return json.loads(self.config_path.read_text(encoding="utf-8"))

    def settings(self):
        return {"url": self.url.get().strip(), "itemId": self.pods.get(self.pod.get(), self.pod.get()),
                "start": self.start.get(), "remark": self.reason.get("1.0", "end-1c").strip(),
                "scheduleAt": self.schedule_at.get(),
                "daysofweek": [name for name, value in self.schedule_days.items() if value.get()],
                "activity": self.activity.get().strip(),
                "teamProfiles": [{k: v.get().strip() for k, v in profile.items()} for profile in self.team_profiles],
                "enabled": self.enabled.get()}

    def update_team_controls(self, _event=None):
        if self.settings()["itemId"] in MULTI_IDS:
            self.team_frame.pack(fill="x", after=self.combo.master, pady=4)
            self.start_combo.configure(values=MULTI_STARTS)
            if self.start.get() not in MULTI_STARTS:
                self.start.set("16:10")
        else:
            self.team_frame.pack_forget()
            starts = [f"{h:02}:00" for h in range(8, 22)]
            self.start_combo.configure(values=starts)
            if self.start.get() not in starts:
                self.start.set("16:00")

    def work(self, action, done):
        if self.busy:
            return
        self.busy = True
        self.status.set("正在连接学习通…")
        # Read Tk/config values on the UI thread, never from the worker.
        try:
            user = self.read_config()["reserve"][self.account]
            if user.get("username") != self.identity:
                raise ValueError("该账号身份已改变，请关闭此窗口后重新打开。")
            username, password, entry = user["username"], user["password"], self.url.get().strip()
        except Exception as exc:
            self.busy = False
            messagebox.showerror("无法继续", str(exc), parent=self)
            return
        def run():
            try:
                client = PodClient(username, password, entry)
                self.events.put((done, action(client), None))
            except Exception as exc:
                self.events.put((done, None, str(exc)))
        threading.Thread(target=run, daemon=True).start()

    def drain(self):
        try:
            while True:
                done, result, error = self.events.get_nowait()
                self.busy = False
                if error:
                    self.status.set(error)
                    messagebox.showerror("静音舱操作失败", error, parent=self)
                else:
                    self.status.set("操作完成。")
                    done(result)
        except queue.Empty:
            pass
        self.poll_id = self.after(100, self.drain)

    def load_pods(self):
        if self.busy:
            self.status.set("上一项操作仍在进行，请等待结果。")
            return
        selected = self.settings()["itemId"]
        def done(pods):
            self.pods = {f"{x['name']} · {x.get('location') or '地点未提供'}（{x['id']}）": x["id"] for x in pods}
            self.combo.configure(values=list(self.pods))
            self.pod.set(next((label for label, value in self.pods.items() if value == selected), next(iter(self.pods), "")))
            self.update_team_controls()
            self.query_result = None
            self.tree.delete(*self.tree.get_children())
            next_step = "请选择舱位、开始时间并保存定时预约。" if self.mode == "schedule" else "请选择舱位和日期，再点击“查询时段”。"
            self.status.set(f"已刷新 {len(pods)} 个静音舱（1、6 号多人，2—5 号单人）。{next_step}"
                            if pods else "未查询到静音舱，请检查入口或账号权限后重试。")
        self.work(lambda client: client.pods(), done)

    def query(self):
        settings, day = self.settings(), self.day.get().strip()
        try:
            dt.date.fromisoformat(day)
            if settings["itemId"] not in self.pods.values():
                raise ValueError("请先刷新并选择静音舱。")
        except ValueError as exc:
            messagebox.showerror("无法查询", str(exc), parent=self)
            return
        def done(slots):
            self.tree.delete(*self.tree.get_children())
            for i, slot in enumerate(slots):
                self.tree.insert("", "end", iid=str(i), values=(slot["start"]+"–"+slot["end"], "空闲" if slot["free"] else "已满或已过期"))
            self.query_result = settings["itemId"], day, slots, settings["url"]
        self.work(lambda client: client.slots(settings["itemId"], day), done)

    def select_slot(self, _event=None):
        if self.tree.selection() and self.query_result:
            self.start.set(self.query_result[2][int(self.tree.selection()[0])]["start"])

    def book(self):
        if self.busy:
            return
        settings, day = self.settings(), self.day.get().strip()
        try:
            start, end, _ = validate_plan(settings)
            config = self.read_config()
            if settings["itemId"] in MULTI_IDS:
                validate_team_config(config, self.account, settings)
            if not self.query_result or (settings["itemId"], day) != self.query_result[:2] or settings["url"] != self.query_result[3]:
                raise ValueError("舱位、日期或入口发生变化，请重新查询。")
            selected = self.tree.selection()
            if not selected:
                raise ValueError("请在查询结果中选择一个时段。")
            slot = self.query_result[2][int(selected[0])]
            if slot["start"] != start or not slot["free"]:
                raise ValueError("请选择与开始时间一致的空闲时段。")
        except ValueError as exc:
            messagebox.showerror("无法预约", str(exc), parent=self)
            return
        team_note = (f"\n组队：{settings['teamProfiles'][self.account]['name']} + {settings['teamProfiles'][1-self.account]['name']}（2 人）\n研讨主题：{settings['activity']}" if settings["itemId"] in MULTI_IDS else "")
        if messagebox.askyesno("提交静音舱预约", f"账号 {self.account+1} · {self.pod.get()}\n{day} {start}–{end}\n事由：{settings['remark']}{team_note}\n\n确认提交？", parent=self):
            self.work(lambda client: book_with_accounts(client, config, self.account, settings, day, self.config_path), lambda result: self.status.set(result))

    def save(self):
        if self.busy:
            return
        try:
            settings = self.settings()
            validate_plan(settings)
            data = self.read_config()
            if settings["itemId"] in MULTI_IDS:
                validate_team_config(data, self.account, settings)
            user = data["reserve"][self.account]
            if user.get("username") != self.identity:
                raise ValueError("账号身份已改变，请重新打开窗口。")
            if self.mode == "manual":
                previous = user.get("pod", {})
                for key in ("scheduleAt", "daysofweek"):
                    settings[key] = previous.get(key, settings[key])
                settings["enabled"] = previous.get("enabled", False)
                settings["start"] = previous.get("start", "16:00")
                if settings["itemId"] in MULTI_IDS and settings["start"] not in MULTI_STARTS:
                    settings["start"] = "16:10"
                elif settings["itemId"] not in MULTI_IDS and not settings["start"].endswith(":00"):
                    settings["start"] = "16:00"
                user["pod"] = settings
                self.config_path.write_text(json.dumps(data, ensure_ascii=False, indent=2)+"\n", encoding="utf-8")
                self.saved_settings = self.settings()
                self.status.set("静音舱设置已保存，预约事由与定时预约共用。")
                return
            at, days = validate_schedule(settings) if self.enabled.get() else (settings["scheduleAt"], settings["daysofweek"])
            # Disabled configurations are also persisted so the runner fails
            # closed if task removal fails.
            settings["enabled"] = False
            user["pod"] = settings
            self.config_path.write_text(json.dumps(data, ensure_ascii=False, indent=2)+"\n", encoding="utf-8")
            from desktop_gui import task_status
            result = task_status("Set" if self.enabled.get() else "Remove", at=at,
                                 days=days, account=self.account+1, kind="pod")
            settings["enabled"] = self.enabled.get()
            data = self.read_config()
            if data["reserve"][self.account].get("username") != self.identity:
                raise ValueError("保存期间账号身份已改变，定时预约保持停用。")
            data["reserve"][self.account]["pod"] = settings
            self.config_path.write_text(json.dumps(data, ensure_ascii=False, indent=2)+"\n", encoding="utf-8")
            self.saved_settings = self.settings()
            self.show_schedule_status(result)
            self.status.set("已保存。" + (f"按所选星期 {at} 预约第二天；任务提前一分钟启动等待。" if self.enabled.get() else "定时预约已关闭。"))
        except Exception as exc:
            messagebox.showerror("保存失败", str(exc), parent=self)

    def show_schedule_status(self, info):
        states = {"Missing": "未创建", "Ready": "已就绪", "Running": "正在运行", "Disabled": "已禁用", "Queued": "排队中"}
        self.task_state.set(f"任务：{states.get(info.get('state'), '状态未知')}\n"
                            f"下次执行：{info.get('nextRunTime') or '未安排'}\n"
                            f"上次执行：{info.get('lastRunTime') or '尚未运行'}；结果代码：{info.get('lastTaskResult')}\n"
                            "任务提前一分钟启动；预约结果请查看定时日志。")

    def refresh_schedule(self):
        if self.busy:
            return
        from desktop_gui import task_status
        self.busy = True
        self.status.set("正在读取 Windows 定时任务…")
        def run():
            try:
                self.events.put((self.show_schedule_status, task_status(account=self.account+1, kind="pod"), None))
            except Exception as exc:
                self.events.put((self.show_schedule_status, None, str(exc)))
        threading.Thread(target=run, daemon=True).start()

    def close(self):
        if self.busy:
            messagebox.showinfo("操作尚未结束", "请等待查询或提交结果，避免丢失预约结果。", parent=self)
            return
        if self.settings() != self.saved_settings and not messagebox.askyesno(
                "放弃未保存的静音舱设置？", "有未保存的事由或定时设置，是否放弃并关闭？", parent=self):
            return
        self.destroy()

    def destroy(self):
        if getattr(self, "poll_id", None):
            self.after_cancel(self.poll_id)
            self.poll_id = None
        super().destroy()

    def records(self):
        def done(result):
            records, pods = result
            locations = {p["id"]: p.get("location") or "地点未提供" for p in pods}
            window = tk.Toplevel(self)
            window.title("静音舱预约记录（单人 / 多人）")
            text = tk.Text(window, width=85, height=20)
            text.pack(fill="both", expand=True)
            for record in records:
                begin = dt.datetime.fromtimestamp(record["starttime"]/1000, TZ)
                end = dt.datetime.fromtimestamp(record["endtime"]/1000, TZ)
                location = locations.get(str(record["itemId"]), "地点未提供")
                text.insert("end", f"{record.get('name', record['itemId'])} · {location}  {begin:%Y-%m-%d %H:%M}–{end:%H:%M}  状态码 {record.get('status')}  记录 {record['id']}\n")
            if not records:
                text.insert("end", "暂无静音舱预约记录。")
            text.configure(state="disabled")
        self.work(lambda client: (client.records(), client.pods()), done)

    def show_log(self):
        path = self.config_path.parent / f"last-pod-{self.account+1}.log"
        window = tk.Toplevel(self)
        window.title(f"静音舱定时日志 · 账号 {self.account+1}")
        text = tk.Text(window, width=85, height=20)
        text.pack(fill="both", expand=True)
        text.insert("end", path.read_text(encoding="utf-8", errors="replace") if path.exists() else "尚无静音舱定时运行日志。")
        text.configure(state="disabled")


class PodPanel(PodControls, ttk.Frame):
    def __init__(self, parent, config_path, account, mode):
        super().__init__(parent)
        self.build_controls(config_path, account, mode)


class PodWindow(PodControls, tk.Toplevel):
    def __init__(self, parent, config_path, account):
        super().__init__(parent)
        self.title(f"静音舱 · 账号 {account + 1}")
        self.geometry("860x830")
        self.build_controls(config_path, account)
        self.protocol("WM_DELETE_WINDOW", self.close)
