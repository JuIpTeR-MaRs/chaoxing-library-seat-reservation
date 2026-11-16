import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
import desktop_gui as gui


class DeleteAccountTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / 'config.local.json'
        self.data = {'reserve': [dict(username=name, password='secret', deptIdEnc=name,
                     scheduleEnabled=True, pod={'enabled': True}) for name in ('a', 'b', 'c')]}
        self.path.write_text(json.dumps(self.data), encoding='utf-8')
        patcher = patch.object(gui, 'CONFIG_PATH', self.path)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_delete_middle_disables_shifted_tasks(self):
        with patch.object(gui, 'task_status', return_value={'state': 'Ready'}) as task:
            self.assertEqual(gui.delete_account_config(1), 1)
        data = json.loads(self.path.read_text(encoding='utf-8'))
        self.assertEqual([a['username'] for a in data['reserve']], ['a', 'c'])
        self.assertTrue(data['reserve'][0]['scheduleEnabled'])
        self.assertFalse(data['reserve'][1]['scheduleEnabled'])
        self.assertFalse(data['reserve'][1]['pod']['enabled'])
        self.assertFalse(data[gui.TRANSFER_KEY]['enabled'])
        self.assertEqual(len([c for c in task.call_args_list if c.args == ('Remove',)]), 5)

    def test_last_account_leaves_blank_entry(self):
        self.path.write_text(json.dumps({'reserve': self.data['reserve'][:1]}), encoding='utf-8')
        with patch.object(gui, 'task_status', return_value={'state': 'Missing'}):
            self.assertEqual(gui.delete_account_config(0), 0)
        self.assertEqual(json.loads(self.path.read_text(encoding='utf-8'))['reserve'][0]['username'], '')

    def test_running_or_failed_task_keeps_config(self):
        original = self.path.read_bytes()
        with patch.object(gui, 'task_status', return_value={'state': 'Running'}) as task:
            with self.assertRaises(ValueError):
                gui.delete_account_config(0)
            self.assertEqual(task.call_count, 1)
        self.assertEqual(self.path.read_bytes(), original)
        with patch.object(gui, 'task_status', side_effect=RuntimeError('denied')):
            with self.assertRaises(RuntimeError):
                gui.delete_account_config(0)
        self.assertEqual(self.path.read_bytes(), original)
