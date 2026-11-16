import json
import subprocess
import unittest
import uuid
from pathlib import Path


class PodTaskTests(unittest.TestCase):
    def test_disposable_pod_task_waits_for_22_and_uses_separate_runner(self):
        name = "ChaoXingPodRegression-" + uuid.uuid4().hex
        script = Path(__file__).resolve().parents[1] / "schedule_task.ps1"
        def invoke(action, *extra):
            result = subprocess.run(["powershell.exe", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-File", str(script), "-TaskName", name, "-Kind", "pod", "-Account", "2", "-Action", action, *extra], capture_output=True, text=True, timeout=30)
            self.assertEqual(result.returncode, 0, result.stderr)
            return json.loads(result.stdout)
        try:
            info = invoke("Set", "-At", "22:00", "-Days", "Sunday,Monday,Tuesday,Wednesday,Thursday,Friday,Saturday")
            self.assertEqual(info["daysOfWeek"], 127)
            self.assertIn("T21:59:00", info["at"])
            result = subprocess.run(["powershell.exe", "-NoProfile", "-NonInteractive", "-Command",
                "$s=New-Object -ComObject Schedule.Service; $s.Connect(); $d=$s.GetFolder('\').GetTask('"+name+"').Definition; $d.Actions.Item(1).Arguments; $d.Settings.ExecutionTimeLimit"], capture_output=True, text=True, timeout=30)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("run-pod.ps1", result.stdout)
            self.assertIn("-Account 2", result.stdout)
            self.assertIn("PT10M", result.stdout)
        finally:
            invoke("Remove")
