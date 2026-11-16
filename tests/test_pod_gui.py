import json
import tempfile
import tkinter as tk
import unittest
from pathlib import Path
from unittest import mock

from pod_gui import PodPanel, PodWindow


class PodGuiTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name) / "config.local.json"
        self.original = {"reserve": [{"username": "user", "password": "password", "seatid": ["007"], "time": ["08:00", "22:00"]}], "other": {"unchanged": True}}
        self.path.write_text(json.dumps(self.original), encoding="utf-8")
        self.root = tk.Tk()
        self.root.withdraw()
        self.window = PodWindow(self.root, self.path, 0)
        self.window.withdraw()
        self.addCleanup(self.root.destroy)
        self.addCleanup(self.window.destroy)

    def fill(self):
        self.window.reason.insert("1.0", "练习英语口语")
        self.window.enabled.set(True)

    def test_refresh_displays_count_and_clears_stale_slots(self):
        self.window.query_result = ("old",)
        self.window.tree.insert("", "end", values=("08:00–09:00", "空闲"))
        pods = [{"id": "38541", "name": "静音2号舱", "location": "图书馆"}]
        with mock.patch.object(self.window, "work", side_effect=lambda action, done: done(pods)):
            self.window.load_pods()
        self.assertIn("已刷新 1 个静音舱", self.window.status.get())
        self.assertIn("查询时段", self.window.status.get())
        self.assertEqual(self.window.settings()["itemId"], "38541")
        self.assertIsNone(self.window.query_result)
        self.assertFalse(self.window.tree.get_children())

    def test_refresh_empty_result_has_explicit_feedback(self):
        with mock.patch.object(self.window, "work", side_effect=lambda action, done: done([])):
            self.window.load_pods()
        self.assertIn("未查询到", self.window.status.get())
        self.assertEqual(self.window.pod.get(), "")

    def test_multi_selection_shows_profiles_and_ninety_minute_starts(self):
        self.window.pod.set("30835")
        self.window.update_team_controls()
        self.assertEqual(self.window.start.get(), "16:10")
        self.assertTrue(self.window.team_frame.winfo_manager())
        self.assertIn("08:20", self.window.start_combo.cget("values"))
        self.window.pod.set("38541")
        self.window.update_team_controls()
        self.assertEqual(self.window.start.get(), "16:00")
        self.assertFalse(self.window.team_frame.winfo_manager())

    def test_manual_switch_to_multi_saves_valid_scheduled_start(self):
        data = self.original
        profiles = [dict(name="甲", studentId="123", college="现代信息产业学院", phone="13800000000"),
                    dict(name="乙", studentId="456", college="现代信息产业学院", phone="13900000000")]
        data["reserve"].append(dict(username="second", password="password"))
        data["reserve"][0]["pod"] = {"start": "16:00", "enabled": False, "teamProfiles": profiles}
        self.path.write_text(json.dumps(data), encoding="utf-8")
        panel = PodPanel(self.root, self.path, 0, "manual")
        try:
            panel.pod.set("30835")
            panel.update_team_controls()
            panel.reason.insert("1.0", "学习")
            with mock.patch("desktop_gui.task_status") as scheduler:
                panel.save()
            scheduler.assert_not_called()
            saved = json.loads(self.path.read_text(encoding="utf-8"))["reserve"][0]["pod"]
            self.assertEqual(saved["start"], "16:10")
            self.assertFalse(saved["enabled"])
            self.assertEqual(saved["teamProfiles"], profiles)
        finally:
            panel.destroy()

    def test_refresh_while_busy_explains_wait(self):
        self.window.busy = True
        with mock.patch.object(self.window, "work") as work:
            self.window.load_pods()
        work.assert_not_called()
        self.assertIn("请等待结果", self.window.status.get())

    def test_embedded_modes_separate_manual_and_scheduled_actions(self):
        for mode in ("manual", "schedule"):
            panel = PodPanel(self.root, self.path, 0, mode)
            try:
                buttons = {}
                def visit(widget):
                    if widget.winfo_class() == "TButton" and widget.winfo_manager():
                        buttons[widget.cget("text")] = widget
                    for child in widget.winfo_children():
                        visit(child)
                visit(panel)
                self.assertEqual("预约选中时段" in buttons, mode == "manual")
                self.assertEqual("保存静音舱定时预约" in buttons, mode == "schedule")
                self.assertEqual(bool(panel.tree.winfo_manager()), mode == "manual")
                self.assertEqual(panel.winfo_toplevel(), self.root)
            finally:
                panel.destroy()

    def test_manual_save_preserves_schedule_without_touching_task(self):
        data = self.original.copy()
        data["reserve"][0]["pod"] = {"enabled": True, "start": "18:00", "remark": "旧事由", "scheduleAt": "22:30", "daysofweek": ["Monday"]}
        self.path.write_text(json.dumps(data), encoding="utf-8")
        panel = PodPanel(self.root, self.path, 0, "manual")
        try:
            panel.reason.delete("1.0", "end")
            panel.reason.insert("1.0", "新事由")
            panel.start.set("10:00")
            panel.schedule_at.set("23:00")
            panel.schedule_days["Monday"].set(False)
            with mock.patch("desktop_gui.task_status") as scheduler:
                panel.save()
            scheduler.assert_not_called()
            saved = json.loads(self.path.read_text(encoding="utf-8"))["reserve"][0]["pod"]
            self.assertTrue(saved["enabled"])
            self.assertEqual(saved["start"], "18:00")
            self.assertEqual(saved["remark"], "新事由")
            self.assertEqual(saved["scheduleAt"], "22:30")
            self.assertEqual(saved["daysofweek"], ["Monday"])
        finally:
            panel.destroy()

    def test_save_keeps_seat_settings_and_uses_independent_daily_task(self):
        self.fill()
        with mock.patch("desktop_gui.task_status") as scheduler:
            self.window.save()
        data = json.loads(self.path.read_text(encoding="utf-8"))
        self.assertEqual(data["reserve"][0]["seatid"], ["007"])
        self.assertEqual(data["other"], self.original["other"])
        self.assertTrue(data["reserve"][0]["pod"]["enabled"])
        self.assertEqual(data["reserve"][0]["pod"]["remark"], "练习英语口语")
        self.assertEqual(scheduler.call_args.kwargs["kind"], "pod")
        self.assertEqual(scheduler.call_args.kwargs["at"], "22:00")
        self.assertEqual(len(scheduler.call_args.kwargs["days"]), 7)

    def test_task_failure_leaves_runner_disabled_and_reason_saved(self):
        self.fill()
        with mock.patch("desktop_gui.task_status", side_effect=RuntimeError("failed")), mock.patch("pod_gui.messagebox.showerror") as error:
            self.window.save()
        saved = json.loads(self.path.read_text(encoding="utf-8"))["reserve"][0]["pod"]
        self.assertFalse(saved["enabled"])
        self.assertEqual(saved["remark"], "练习英语口语")
        error.assert_called_once()

    def test_custom_schedule_is_saved_and_sent_to_scheduler(self):
        self.fill()
        self.window.schedule_at.set("22:30")
        for name, variable in self.window.schedule_days.items():
            variable.set(name == "Monday")
        with mock.patch("desktop_gui.task_status", return_value={"state": "Ready", "nextRunTime": "2026-11-16 22:29:00"}) as scheduler:
            self.window.save()
        self.assertEqual(scheduler.call_args.kwargs["at"], "22:30")
        self.assertEqual(scheduler.call_args.kwargs["days"], ["Monday"])
        saved = json.loads(self.path.read_text(encoding="utf-8"))["reserve"][0]["pod"]
        self.assertEqual(saved["scheduleAt"], "22:30")
        self.assertEqual(saved["daysofweek"], ["Monday"])
        self.assertIn("22:29:00", self.window.task_state.get())

    def test_invalid_schedule_does_not_write_or_change_task(self):
        self.fill()
        for at, days in [("21:00", ["Monday"]), ("22:00", [])]:
            self.window.schedule_at.set(at)
            for name, variable in self.window.schedule_days.items():
                variable.set(name in days)
            with mock.patch("desktop_gui.task_status") as scheduler, mock.patch("pod_gui.messagebox.showerror") as error:
                self.window.save()
            scheduler.assert_not_called()
            error.assert_called_once()
            self.assertEqual(json.loads(self.path.read_text(encoding="utf-8")), self.original)

    def test_open_and_query_do_not_submit_or_save(self):
        self.window.pods = {"静音2号舱": "38541"}
        self.window.pod.set("静音2号舱")
        self.window._work_capture = None
        actions = []
        self.window.work = lambda action, done: actions.append(action)
        self.window.query()
        fake = mock.Mock()
        fake.slots.return_value = []
        actions[0](fake)
        fake.book.assert_not_called()
        self.assertEqual(json.loads(self.path.read_text(encoding="utf-8")), self.original)


if __name__ == "__main__":
    unittest.main()
