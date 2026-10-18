"""Offline Telegram/engine integration: temporary Markdown, fake transport only."""
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest
from unittest import mock

sys.dont_write_bytecode = True
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import bot_controls as controls_module
import reminders as engine_module
import markdown_store

UTC = timezone.utc
NOW = datetime(2026, 10, 18, 7, 0, tzinfo=UTC)  # 12:00 UTC+05.
OWNER = 12345
TOKEN = '900001:offline_test_value'
TASK_ID = '11111111-2222-4333-8444-555555555555'
EXISTING_ID = 'aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee'
DEADLINE = '2026-10-19 09:00'
NEXT_DEADLINE = '2026-10-20 10:30'


class FakeTransport:
    def __init__(self):
        self.calls = []
        self.fail_send = False

    def __call__(self, token, method, payload, timeout=5):
        self.calls.append((method, payload))
        if self.fail_send and method == 'sendMessage':
            raise RuntimeError('offline-private-transport-error')
        return {'ok': True, 'result': [] if method == 'getUpdates' else {'message_id': len(self.calls)}}

    def messages(self):
        return [payload for method, payload in self.calls if method == 'sendMessage']


class IntegrationFixture(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='create-integration-offline-')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.vault = self.root/'vault'
        self.vault.mkdir()
        self.path = self.vault/'Задачи.md'
        self.original = ('\ufeff# Notes\r\n\r\nKeep **format**.\r\n\r\n```markdown\r\n'
                         '- [ ] Example only 🔔 2099-01-01 09:00\r\n```\r\n\r\n'
                         f'- [ ] Existing note <!-- obs-task:{EXISTING_ID} -->\r\n\r\n'
                         'Unrelated footer.\r\n').encode('utf-8')
        self.path.write_bytes(self.original)
        self.data = self.root/'private-data'
        self.config = self.root/'config.json'
        self.config.write_text(json.dumps({'vault': str(self.vault), 'tasks_file': 'Задачи.md',
            'data_directory': str(self.data), 'timezone': 'Asia/Qyzylorda',
            'poll_seconds': 20, 'repeat_seconds': 7200}), encoding='utf-8')
        self.sent = []
        self.engine = self.new_engine()

    def new_engine(self):
        return engine_module.TaskEngine(self.config,
            sender=lambda *args: self.sent.append(args),
            token_loader=lambda unused: 'offline-token-placeholder')

    def row(self, task_id=TASK_ID):
        rows = [row for row in self.engine.list_tasks() if row['task_id'] == task_id]
        self.assertEqual(len(rows), 1)
        return rows[0]

    def create(self, **kwargs):
        return self.engine.create_task('Created from Telegram', DEADLINE,
                                       task_id=TASK_ID, now=NOW, **kwargs)


class EngineCreateIntegration(IntegrationFixture):
    def test_create_preserves_bom_crlf_examples_and_existing_content(self):
        result = self.create()
        self.assertEqual(result['status'], 'created')
        after = self.path.read_bytes()
        self.assertTrue(after.startswith(self.original))
        self.assertEqual(after.count(b'Created from Telegram'), 1)
        self.assertNotIn(b'\n', after.replace(b'\r\n', b''))
        self.assertEqual(datetime.fromtimestamp(self.row()['due_utc'], UTC),
                         datetime(2026, 10, 19, 4, 0, tzinfo=UTC))
        self.assertEqual(self.sent, [])

    def test_creation_is_idempotent_after_restart_and_deadline_passes(self):
        first = self.create()
        self.engine = self.new_engine()
        second = self.engine.create_task('Created from Telegram', DEADLINE,
                    task_id=TASK_ID, now=NOW+timedelta(days=2))
        self.assertEqual(second['status'], 'already_exists')
        self.assertEqual(second['task_id'], first['task_id'])
        self.assertEqual(self.path.read_bytes().count(b'Created from Telegram'), 1)

    def test_real_worker_busy_then_retry_has_one_task_and_no_lost_content(self):
        with engine_module.WorkerLock(self.engine.lock_path):
            result = self.create()
        self.assertEqual(result['status'], 'busy')
        self.assertEqual(self.path.read_bytes(), self.original)
        self.assertEqual(self.create()['status'], 'created')
        self.assertEqual(self.path.read_bytes().count(b'Created from Telegram'), 1)

    def test_deadline_edit_preserves_other_unsynced_user_edits_and_stable_id(self):
        self.create()
        before = self.row()
        external = '\r\nExternal **paragraph**.\r\n- [ ] Another unsynced task\r\n'.encode('utf-8')
        with self.path.open('ab') as stream:
            stream.write(external)
        result = self.engine.set_deadline(TASK_ID, before['schedule_version'], NEXT_DEADLINE, now=NOW)
        self.assertEqual(result['status'], 'updated')
        after = self.path.read_bytes()
        self.assertIn(external, after)
        self.assertIn(self.original, after)
        self.assertEqual(after.count(TASK_ID.encode()), 1)
        self.assertEqual(self.row()['schedule_version'], before['schedule_version']+1)
        self.assertEqual(datetime.fromtimestamp(self.row()['due_utc'], UTC),
                         datetime(2026, 10, 20, 5, 30, tzinfo=UTC))

    def test_foreign_unsynced_deadline_change_rejects_old_edit_without_write(self):
        self.create()
        version = self.row()['schedule_version']
        foreign = self.path.read_bytes().replace(DEADLINE.encode(), NEXT_DEADLINE.encode())
        self.path.write_bytes(foreign)
        result = self.engine.set_deadline(TASK_ID, version, '2026-10-21 11:00', now=NOW)
        self.assertEqual(result['status'], 'changed')
        self.assertEqual(self.path.read_bytes(), foreign)

    def test_deadline_clear_then_future_relative_date_remain_one_task(self):
        self.create()
        result = self.engine.set_deadline(TASK_ID, self.row()['schedule_version'], None, now=NOW)
        self.assertEqual(result['status'], 'updated')
        self.assertIsNone(self.row()['due_utc'])
        result = self.engine.set_deadline(TASK_ID, self.row()['schedule_version'], 'завтра 09:00', now=NOW)
        self.assertEqual(result['status'], 'updated')
        self.assertEqual(datetime.fromtimestamp(self.row()['due_utc'], UTC),
                         datetime(2026, 10, 19, 4, 0, tzinfo=UTC))
        self.assertEqual(self.path.read_bytes().count(TASK_ID.encode()), 1)

    def test_creation_refuses_unclosed_fence_without_hiding_task_inside_example(self):
        unclosed = self.original + b'\r\n```markdown\r\nunfinished example\r\n'
        self.path.write_bytes(unclosed)
        result = self.create()
        self.assertEqual(result['status'], 'busy')
        self.assertEqual(self.path.read_bytes(), unclosed)
        self.assertFalse(any(row['task_id'] == TASK_ID for row in self.engine.list_tasks()))


class TelegramCreateIntegration(IntegrationFixture):
    def setUp(self):
        super().setUp()
        self.moment = NOW
        self.update_id = 0
        self.transport = FakeTransport()
        self.creation_calls = []
        self.deadline_calls = []
        self.controls = self.new_controls()

    def new_controls(self):
        def create(draft_id, title, expression):
            self.creation_calls.append((draft_id, title, expression))
            return self.engine.create_task(title, expression, task_id=draft_id, now=self.moment)
        def set_deadline(task_id, version, expression):
            self.deadline_calls.append((task_id, version, expression))
            return self.engine.set_deadline(task_id, version, expression, now=self.moment)
        return controls_module.BotControls(self.data, self.engine.status, self.transport,
            now=self.moment.timestamp(), create_task_handler=create,
            set_deadline_handler=set_deadline,
            task_provider=lambda: self.engine.fresh_tasks(now=self.moment))

    def message(self, text, *, user=OWNER, chat=OWNER, kind='private'):
        self.update_id += 1
        self.moment += timedelta(seconds=1)
        update = {'update_id': self.update_id, 'message': {
            'date': int(self.moment.timestamp()), 'from': {'id': user},
            'chat': {'id': chat, 'type': kind}, 'text': text}}
        self.last_update = update
        return self.controls.process_update(TOKEN, OWNER, update, self.moment.timestamp())

    def callback(self, data, *, user=OWNER, chat=OWNER, kind='private'):
        self.update_id += 1
        self.moment += timedelta(seconds=1)
        update = {'update_id': self.update_id, 'callback_query': {
            'id': f'integration-{self.update_id}', 'from': {'id': user}, 'data': data,
            'message': {'date': 1, 'from': {'id': 900001},
                        'chat': {'id': chat, 'type': kind}}}}
        self.last_update = update
        return self.controls.process_update(TOKEN, OWNER, update, self.moment.timestamp())

    def button(self, *, prefix='', suffix=''):
        for payload in reversed(self.transport.messages()):
            for row in payload.get('reply_markup', {}).get('inline_keyboard', []):
                for button in row:
                    data = button.get('callback_data', '')
                    if data.startswith(prefix) and data.endswith(suffix):
                        return data
        self.fail('Expected action button was absent from actual response markup')

    def quick(self, action='none'):
        return self.button(prefix='draft:', suffix=':'+action)

    def tick(self, seconds=6):
        self.moment += timedelta(seconds=seconds)
        return self.controls.tick(TOKEN, OWNER, self.moment.timestamp())

    def created(self, title):
        return [row for row in self.engine.list_tasks() if row['text'] == title]

    def test_real_controls_create_reverse_write_and_restart_dedup(self):
        title = 'Create through Telegram'
        self.message('/add '+title)
        data = self.quick('none')
        self.callback(data)
        final_update = self.last_update
        rows = self.created(title)
        self.assertEqual(len(rows), 1)
        self.assertTrue(self.path.read_bytes().startswith(self.original))
        task_id = rows[0]['task_id']
        self.engine = self.new_engine()
        self.controls = self.new_controls()
        self.controls.process_update(TOKEN, OWNER, final_update, self.moment.timestamp())
        self.callback(data)
        self.tick()
        self.assertEqual(len(self.created(title)), 1)
        self.assertEqual(self.created(title)[0]['task_id'], task_id)
        self.assertEqual(self.path.read_bytes().count(title.encode()), 1)

    def test_title_draft_survives_restart_and_normalizes_local_date(self):
        self.message('/add')
        self.message('Persistent draft title')
        self.engine = self.new_engine()
        self.controls = self.new_controls()
        self.message('завтра 09:00')
        rows = self.created('Persistent draft title')
        self.assertEqual(len(rows), 1)
        self.assertEqual(datetime.fromtimestamp(rows[0]['due_utc'], UTC),
                         datetime(2026, 10, 19, 4, 0, tzinfo=UTC))

    def test_busy_creation_retries_same_uuid_after_restart_without_new_callback(self):
        self.message('/add Busy but persistent')
        data = self.quick('none')
        with engine_module.WorkerLock(self.engine.lock_path):
            self.callback(data)
        self.assertEqual(self.path.read_bytes(), self.original)
        self.assertEqual(len(self.creation_calls), 1)
        first_id = self.creation_calls[0][0]
        self.engine = self.new_engine()
        self.controls = self.new_controls()
        self.tick()
        rows = self.created('Busy but persistent')
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]['task_id'], first_id)
        self.assertTrue(all(call[0] == first_id for call in self.creation_calls))
        self.callback(data)
        self.tick()
        self.assertEqual(len(self.created('Busy but persistent')), 1)

    def test_failed_confirmation_send_never_duplicates_committed_creation(self):
        self.message('/add Save before confirmation')
        data = self.quick('none')
        self.transport.fail_send = True
        self.callback(data)
        final_update = self.last_update
        self.transport.fail_send = False
        self.engine = self.new_engine()
        self.controls = self.new_controls()
        self.controls.process_update(TOKEN, OWNER, final_update, self.moment.timestamp())
        self.callback(data)
        self.tick()
        self.assertEqual(len(self.created('Save before confirmation')), 1)

    def test_unauthorized_messages_and_callbacks_cannot_create_or_mutate_draft(self):
        self.message('/add Owner draft')
        data = self.quick('none')
        for options in ({'user': 999}, {'chat': 999}, {'kind': 'group'}):
            self.message('/add Foreign title', **options)
            self.callback(data, **options)
        self.assertEqual(self.path.read_bytes(), self.original)
        self.assertEqual(self.creation_calls, [])
        self.callback(data)
        self.assertEqual(len(self.created('Owner draft')), 1)
        self.assertEqual(self.created('Foreign title'), [])

    def test_old_nonce_cannot_cancel_newer_draft(self):
        self.message('/add First draft')
        old_cancel = self.quick('cancel')
        self.message('/cancel')
        self.message('/add Second draft')
        new_save = self.quick('none')
        self.callback(old_cancel)
        self.callback(new_save)
        self.assertEqual(len(self.created('Second draft')), 1)
        self.assertEqual(self.created('First draft'), [])

    def test_deadline_edit_preserves_concurrent_title_and_other_content(self):
        self.create()
        self.message('/tasks')
        self.callback(self.button(prefix='date:'+TASK_ID+':'))
        foreign = self.path.read_bytes().replace(b'Existing note', b'Other task edited elsewhere')
        foreign = foreign.replace(b'Created from Telegram', b'Renamed outside Telegram')
        extra = b'\r\nAnother user paragraph.\r\n'
        self.path.write_bytes(foreign+extra)
        self.message(NEXT_DEADLINE)
        after = self.path.read_bytes()
        self.assertIn(b'Other task edited elsewhere', after)
        self.assertIn(b'Renamed outside Telegram', after)
        self.assertTrue(after.endswith(extra))
        self.assertEqual(after.count(TASK_ID.encode()), 1)
        self.assertEqual(self.row()['text'], 'Renamed outside Telegram')
        self.assertEqual(datetime.fromtimestamp(self.row()['due_utc'], UTC),
                         datetime(2026, 10, 20, 5, 30, tzinfo=UTC))

    def test_foreign_deadline_edit_while_draft_open_is_preserved(self):
        self.create()
        self.message('/tasks')
        self.callback(self.button(prefix='date:'+TASK_ID+':'))
        foreign = self.path.read_bytes().replace(DEADLINE.encode(), NEXT_DEADLINE.encode())
        self.path.write_bytes(foreign)
        self.message('2026-10-21 11:00')
        self.assertEqual(self.path.read_bytes(), foreign)
        self.assertEqual(self.deadline_calls[-1][0], TASK_ID)

    def test_create_write_before_queue_failure_recovers_once_through_real_intent(self):
        title = 'Written before queue failure'
        self.message('/add '+title)
        with mock.patch.object(self.engine, '_sync_tasks', side_effect=sqlite3.OperationalError('offline-private-error')):
            self.callback(self.quick('none'))
        self.assertEqual(self.path.read_bytes().count(title.encode()), 1)
        self.engine = self.new_engine()
        self.controls = self.new_controls()
        self.tick()
        self.assertEqual(len(self.created(title)), 1)
        self.assertEqual(self.path.read_bytes().count(title.encode()), 1)

    def test_deadline_write_before_queue_failure_replays_once_and_keeps_later_edit(self):
        self.create()
        original_version = self.row()['schedule_version']
        self.message('/tasks')
        self.callback(self.button(prefix='date:'+TASK_ID+':'))
        with mock.patch.object(self.engine, '_sync_tasks', side_effect=sqlite3.OperationalError('offline-private-error')):
            self.message(NEXT_DEADLINE)
        self.assertIn(NEXT_DEADLINE.encode(), self.path.read_bytes())
        extra = b'\r\nAfter failed queue update: keep this paragraph.\r\n'
        with self.path.open('ab') as stream:
            stream.write(extra)
        self.engine = self.new_engine()
        self.controls = self.new_controls()
        self.tick()
        self.assertEqual(self.row()['schedule_version'], original_version+1)
        self.assertTrue(self.path.read_bytes().endswith(extra))
        self.assertEqual(self.path.read_bytes().count(TASK_ID.encode()), 1)

    def test_created_then_edited_deadline_drives_fresh_notifications(self):
        title = 'Fresh notification source'
        self.message('/add '+title)
        self.message(DEADLINE)
        row = self.created(title)[0]
        first_due = datetime.fromtimestamp(row['due_utc'], UTC)
        with mock.patch.object(self.engine, '_auth', return_value=(TOKEN, OWNER, None)):
            self.engine.run_once(now=first_due)
        self.assertEqual(len(self.sent), 1)
        self.moment = first_due+timedelta(minutes=1)
        self.message('/tasks')
        self.callback(self.button(prefix='date:'+row['task_id']+':'))
        self.message(NEXT_DEADLINE)
        changed = self.created(title)[0]
        with mock.patch.object(self.engine, '_auth', return_value=(TOKEN, OWNER, None)):
            self.engine.run_once(now=first_due+timedelta(hours=2))
            self.assertEqual(len(self.sent), 1)
            self.engine.run_once(now=datetime.fromtimestamp(changed['due_utc'], UTC))
        self.assertEqual(len(self.sent), 2)
        self.assertIn('20.10.2026 10:30', self.sent[-1][2])

    def test_draft_title_is_private_sql_data_and_absent_from_safe_status(self):
        title = 'PRIVATE DRAFT integration sample'
        self.message('/add '+title)
        self.assertEqual(self.path.read_bytes(), self.original)
        db = sqlite3.connect(self.data/'controls.sqlite3')
        try:
            stored = db.execute('SELECT owner,title FROM drafts').fetchall()
        finally:
            db.close()
        self.assertEqual(stored, [(OWNER, title)])
        self.assertNotIn(title, json.dumps(self.controls._summary()))
        self.assertNotIn(title, json.dumps(self.engine.status()))
        self.assertNotIn(title, self.config.read_text(encoding='utf-8'))

    def test_oversized_or_service_markup_title_cannot_write_markdown(self):
        invalid = ['X'*2001, 'bad <!-- obs-task:'+TASK_ID+' -->', 'bad\n- [ ] injected', 'bad 🔔 tomorrow']
        for title in invalid:
            with self.subTest(kind=invalid.index(title)):
                self.message('/add '+title)
                self.assertEqual(self.path.read_bytes(), self.original)
                self.assertEqual(self.creation_calls, [])
                self.message('/cancel')
        self.message('/add '+'😀'*2000)
        self.callback(self.quick('none'))
        self.assertEqual(len(self.created('😀'*2000)), 1)
        for payload in self.transport.messages():
            self.assertLessEqual(len(payload['text'].encode('utf-16-le'))//2, 4096)


if __name__ == '__main__':
    unittest.main(verbosity=2)
