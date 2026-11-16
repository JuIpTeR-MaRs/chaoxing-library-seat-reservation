"""Exercise the desktop redesign without network calls or config writes."""
import unittest
import json
import tempfile
from pathlib import Path
from unittest import mock

from desktop_gui import SeatApp


class DesktopNavigationTests(unittest.TestCase):
    def test_pod_records_share_list_without_seat_cancellation(self):
        import datetime as dt
        stamp = int((dt.datetime.now().timestamp() + 86400) * 1000)
        self.app._show_records([
            dict(id="42", status=0, startTime=stamp, endTime=stamp + 3600000),
            dict(id="pod:42", resourceType="pod", status=0, startTime=stamp,
                 endTime=stamp + 3600000, roomName="静音舱 / 三楼", seatNum="2号舱")])
        self.assertEqual(len(self.app.records_tree.get_children()), 2)
        self.app.records_tree.selection_set("pod:42")
        self.app._update_cancel_button()
        self.assertEqual(str(self.app.cancel_button["state"]), "disabled")
        self.app.record_rows["pod:42"]["violation"] = -1
        self.app._update_cancel_button()
        self.assertEqual(str(self.app.cancel_button["state"]), "normal")
        with mock.patch.object(self.app, "_cancel_pod_record") as cancel:
            self.app._cancel_record()
            cancel.assert_called_once_with("pod:42", self.app.record_rows["pod:42"])
        self.assertIn("静音舱", self.app.records_location.get())
        self.assertEqual(self.app.records_tree.item("pod:42", "values")[-1], "状态码 0")
        self.app.records_tree.selection_set("42")
        self.app._update_cancel_button()
        self.assertEqual(str(self.app.cancel_button["state"]), "normal")

    def setUp(self):
        self.patches = [mock.patch.object(SeatApp, name) for name in (
            "_load_fields", "_load_rooms", "_refresh_schedule", "_show_run_log",
            "_show_transfer_log", "_edit_backups", "_update_holiday_table")]
        self.callbacks = {patch.attribute: patch.start() for patch in self.patches}
        self.addCleanup(lambda: [patch.stop() for patch in reversed(self.patches)])
        self.app = SeatApp()
        self.app.withdraw()
        self.addCleanup(self.app.destroy)

    def test_navigation_keeps_unsaved_form_state(self):
        self.app.schedule_seat.set("007")
        self.app.transfer_enabled.set(True)
        self.app.weekday_times = {"Sunday": ["10:00", "21:00"]}
        for page in self.app.pages:
            self.app.nav_buttons[page].invoke()
            self.assertEqual(self.app.current_page, page)
            self.assertEqual(self.app.pages[page].winfo_manager(), "pack")
            self.assertEqual(sum(bool(frame.winfo_manager()) for frame in self.app.pages.values()), 1)
        self.assertEqual(self.app.schedule_seat.get(), "007")
        self.assertTrue(self.app.transfer_enabled.get())
        self.assertEqual(self.app.weekday_times["Sunday"], ["10:00", "21:00"])
        self.callbacks["_refresh_schedule"].assert_called_once()

    def test_pods_are_embedded_in_each_reservation_page(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.local.json"
            path.write_text(json.dumps({"reserve": [{"username": "user", "password": "password"}]}), encoding="utf-8")
            with mock.patch("desktop_gui.CONFIG_PATH", path):
                for mode in ("manual", "schedule"):
                    self.app._navigate(mode, refresh=False)
                    tabs = self.app.resource_tabs[mode]
                    self.assertEqual([tabs.tab(tab, "text") for tab in tabs.tabs()], ["座位", "静音舱"])
                    tabs.select(self.app.pod_hosts[mode])
                    self.app._pod_tab_changed(mode)
                    panel = self.app.pod_panels[mode]
                    self.assertEqual(panel.mode, mode)
                    self.assertEqual(panel.winfo_toplevel(), self.app)
                    self.assertEqual(panel.account, self.app.account_index)
                self.assertTrue(self.app._confirm_pod_changes())

    def test_collapsed_rules_preserve_holiday_and_transfer_values(self):
        self.app.skip_holidays.set(False)
        self.app.transfer_at.set("22:45")
        self.assertFalse(self.app.rules_panel.winfo_manager())
        self.app.rules_toggle.invoke()
        self.assertEqual(self.app.rules_panel.winfo_manager(), "pack")
        self.app.rules_toggle.invoke()
        self.assertFalse(self.app.rules_panel.winfo_manager())
        self.assertFalse(self.app.skip_holidays.get())
        self.assertEqual(self.app.transfer_at.get(), "22:45")

    def test_saved_task_summary_is_separate_from_form_preview(self):
        self.app.schedule_at.set("23:00")
        self.app._show_schedule_status({"nextRunTime": "2026-11-14T21:59:00",
                                        "state": "Ready", "lastTaskResult": 0})
        self.assertIn("23:00", self.app.schedule_overview.get())
        self.assertEqual(self.app.next_task_time.get(), "2026-11-14T21:59:00")
        self.assertEqual(self.app.next_booking_day.get(), "2026-11-15")

    def test_existing_dialog_entries_remain_connected(self):
        buttons = {}
        def visit(widget):
            if widget.winfo_class() == "TButton":
                buttons[widget.cget("text")] = widget
            for child in widget.winfo_children():
                visit(child)
        visit(self.app)
        for text, callback in (
                ("查看当前账号预约日志", "_show_run_log"),
                ("查看转预约日志", "_show_transfer_log"),
                ("更新节假日表", "_update_holiday_table")):
            buttons[text].invoke()
            self.callbacks[callback].assert_called_once()
        self.assertEqual(tuple(self.app.records_tree["columns"]),
                         ("date", "time", "area", "seat", "state"))
        self.assertEqual(self.app.title(), "超星图书馆座位预约")

    def test_mapped_pages_have_scrollable_content_at_both_window_sizes(self):
        self.app.deiconify()
        for geometry in ("1320x820", "860x500"):
            self.app.geometry(geometry)
            self.app.update()
            for page in self.app.pages:
                self.app._navigate(page, refresh=False)
                self.app.update()
                canvas = self.app.content_canvas
                self.assertGreater(canvas.winfo_width(), 500)
                self.assertGreater(canvas.winfo_height(), 300)
                self.assertIsNotNone(canvas.bbox("all"))
            self.app._navigate("schedule", refresh=False)
            self.app.rules_toggle.invoke()
            self.app.update()
            self.assertTrue(self.app.rules_panel.winfo_ismapped())
            self.app.rules_toggle.invoke()


if __name__ == "__main__":
    unittest.main()
