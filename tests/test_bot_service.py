import importlib.util
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from bot_service import BotService, main
from reminders import WorkerLock


class FakeEngine:
    def __init__(self, directory):
        self.data_dir = Path(directory)
        self.config = SimpleNamespace(repeat_seconds=7200, poll_seconds=20)
        self.calls = []
        self.auth = ('non-secret-test-value', 42, None)
        self.results = []

    def run_once(self, **kwargs):
        self.calls.append(kwargs)
        return self.results.pop(0) if self.results else {'error': None}

    def fresh_tasks(self, **kwargs):
        return [{'text': 'Current task', 'state': 'unscheduled'}]

    def status(self):
        return {'counts': {'active': 3, 'pending': 2, 'leased': 0},
                'next_due': '2026-10-17T08:00:00+00:00', 'last_run': '2026-10-17T06:00:00+00:00',
                'error': None}

    def _auth(self):
        return self.auth


class FakeControls:
    def __init__(self):
        self.calls = 0
        self.failure = False

    def tick(self, token, chat_id):
        self.calls += 1
        if self.failure:
            raise ValueError('private-test-value')
        return {'error': None}


class BotServiceTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.engine = FakeEngine(self.directory.name)
        self.controls = FakeControls()
        self.service = BotService(None, engine=self.engine, controls=self.controls)

    def test_buttons_are_polled_between_task_refreshes(self):
        for now in (0, 2, 4, 19, 20):
            self.service.step(monotonic=now)
        self.assertEqual(len(self.engine.calls), 2)
        self.assertEqual(self.controls.calls, 5)
        self.assertTrue(all(x.get('max_deliveries') == 1 for x in self.engine.calls))

    def test_backlog_does_not_delay_buttons_or_wait_for_next_file_poll(self):
        order = []
        original_run = self.engine.run_once
        self.engine.results = [{'sent_this_run': 1}, {'sent_this_run': 1}, {'sent_this_run': 0}]
        def deliver(**kwargs):
            order.append('delivery')
            return original_run(**kwargs)
        def buttons(*args):
            order.append('buttons')
            return {'error': None}
        self.engine.run_once = deliver
        self.controls.tick = buttons
        for now in (0, 1, 2, 3):
            self.service.step(monotonic=now)
        self.assertEqual(order, ['buttons', 'delivery', 'buttons', 'delivery',
                                 'buttons', 'delivery', 'buttons'])

    def test_task_view_propagates_failed_refresh_instead_of_showing_stale_data(self):
        self.assertEqual(self.service.task_list()[0]['text'], 'Current task')
        with patch.object(self.engine, 'fresh_tasks', side_effect=OSError('private-data')):
            with self.assertRaises(OSError):
                self.service.task_list()

    def test_long_poll_does_not_get_an_extra_two_second_sleep(self):
        self.service.step = lambda: {'controls_error': None}
        with patch('bot_service.time.monotonic', side_effect=[0, 1, KeyboardInterrupt]), \
                patch('bot_service.time.sleep') as sleep:
            with self.assertRaises(KeyboardInterrupt):
                self.service.watch(_lock_held=True)
        sleep.assert_not_called()

    def test_network_failure_backoff_resets_after_recovery(self):
        self.service.step = __import__('unittest.mock', fromlist=['Mock']).Mock(
            side_effect=[{'controls_error': 'telegram_unavailable'},
                         {'controls_error': 'telegram_unavailable'},
                         {'controls_error': None},
                         {'controls_error': 'telegram_unavailable'}, KeyboardInterrupt])
        with patch('bot_service.time.monotonic', side_effect=[0, 1, 2, 3, 4, 5]), \
                patch('bot_service.time.sleep') as sleep:
            with self.assertRaises(KeyboardInterrupt):
                self.service.watch(_lock_held=True)
        self.assertEqual([call.args[0] for call in sleep.call_args_list], [1, 2, 1])

    def test_button_failure_does_not_stop_future_reminders_or_leak(self):
        self.controls.failure = True
        first = self.service.step(monotonic=0)
        second = self.service.step(monotonic=20)
        self.assertEqual(first['controls_error'], 'controls_error')
        self.assertEqual(len(self.engine.calls), 2)
        self.assertNotIn('private-test-value', self.service.status_path.read_text())

    def test_no_credentials_keeps_tasks_synced_without_polling(self):
        self.engine.auth = (None, None, 'telegram_not_configured')
        result = self.service.step(monotonic=0)
        self.assertEqual(len(self.engine.calls), 1)
        self.assertEqual(self.controls.calls, 0)
        self.assertFalse(result['telegram_configured'])

    def test_status_adapter_uses_two_hour_repeat_and_counts(self):
        result = self.service.control_status()
        self.assertEqual(result['repeat_minutes'], 120)
        self.assertEqual(result['counts']['active'], 3)
        self.assertEqual(result['counts']['pending'], 2)

    def test_menu_api_failure_is_never_reported_as_sent(self):
        self.controls.publish_menu = lambda token, chat: {'error':'telegram_unavailable'}
        result = self.service.publish_menu()
        self.assertEqual(result['status'],'failed')

    def test_second_worker_never_initializes_recovery_before_lock(self):
        directory = Path(self.directory.name)
        config = directory / 'config.json'
        config.write_text(__import__('json').dumps({'vault':str(directory),
            'tasks_file':'Задачи.md', 'data_directory':str(directory)}),encoding='utf-8')
        with WorkerLock(directory/'worker.lock'), patch('bot_service.BotService') as constructor:
            self.assertEqual(main(['--config',str(config),'--once']),0)
            constructor.assert_not_called()


if __name__ == '__main__':
    unittest.main(verbosity=2)
