"""Real engine/service/controls against temporary files and an offline Telegram."""
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

sys.dont_write_bytecode = True
APP = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(APP))
import bot_controls
import bot_service
import markdown_store
import reminders

UTC = timezone.utc
NOW = datetime(2026, 10, 17, 4, 0, tzinfo=UTC)
OWNER = 12345
TOKEN = "900001:offline_test_value"


class OfflineTelegram:
    """Model getUpdates limits/offset; every side effect remains in memory."""
    def __init__(self, events):
        self.events = events
        self.updates = []
        self.sent = []

    def __call__(self, token, method, payload, timeout=5):
        self.events.append((method, payload.copy()))
        if method == "getUpdates":
            updates = [item for item in self.updates if item["update_id"] >= payload["offset"]]
            return {"ok": True, "result": updates[:payload["limit"]]}
        if method == "sendMessage":
            self.sent.append(payload.copy())
        return {"ok": True, "result": {"message_id": len(self.sent)}}


class IntegrationViewTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="obsidian-integration-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.vault = self.root / "vault"
        self.vault.mkdir()
        self.tasks = self.vault / "Tasks.md"
        self.tasks.write_text("# Tasks\n", encoding="utf-8")
        self.data = self.root / "private"
        self.config = self.root / "config.json"
        self.config.write_text(json.dumps({"vault": str(self.vault), "tasks_file": "Tasks.md",
            "data_directory": str(self.data), "timezone": "Asia/Qyzylorda", "poll_seconds": 20,
            "repeat_seconds": 7200}), encoding="utf-8")
        self.clock = NOW
        self.events = []
        self.reminder_messages = []
        self.engine = reminders.TaskEngine(self.config, sender=self.reminder_sender,
                                            token_loader=lambda path: TOKEN)
        (self.data / ("token.dpapi" if os.name == "nt" else "token.enc")).write_bytes(b"offline-placeholder")
        (self.data / "settings.json").write_text(json.dumps({"chat_id": OWNER}), encoding="utf-8")
        self.telegram = OfflineTelegram(self.events)
        self.controls = bot_controls.BotControls(self.data, lambda: self.service.control_status(),
            transport=self.telegram, now=lambda: self.clock.timestamp(),
            complete_task_handler=lambda task_id, version: self.service.complete_task(task_id, version),
            task_provider=lambda: self.service.task_list())
        self.service = bot_service.BotService(self.config, engine=self.engine, controls=self.controls)
        self.patcher = mock.patch.object(reminders, "utc_now", side_effect=lambda: self.clock)
        self.patcher.start()
        self.addCleanup(self.patcher.stop)
        self.serial = 0

    def reminder_sender(self, token, owner, text):
        self.events.append(("reminder", text))
        self.reminder_messages.append(text)

    def write(self, text):
        self.tasks.write_text(text, encoding="utf-8", newline="")

    def queue_message(self, text):
        self.serial += 1
        self.telegram.updates.append({"update_id": self.serial, "message": {
            "date": self.clock.timestamp(), "from": {"id": OWNER},
            "chat": {"id": OWNER, "type": "private"}, "text": text}})

    def queue_callback(self, data):
        self.serial += 1
        self.telegram.updates.append({"update_id": self.serial, "callback_query": {
            "id": f"offline-callback-{self.serial}", "from": {"id": OWNER}, "data": data,
            "message": {"date": 1, "chat": {"id": OWNER, "type": "private"}}}})

    def step(self, monotonic):
        # Match production's single externally held worker lock.
        with reminders.WorkerLock(self.engine.lock_path):
            return self.service.step(monotonic=monotonic)

    @staticmethod
    def payloads(message, prefix):
        return [button["callback_data"] for row in message["reply_markup"]["inline_keyboard"]
                for button in row if button["callback_data"].startswith(prefix)]

    def test_task_view_reads_edits_before_background_poll_and_never_sends_reminder(self):
        self.write("- [ ] Удалённое дело 🔔 2026-10-17 12:00\n")
        self.step(0)
        self.write("- [ ] Новое дело без срока\n")
        self.queue_message("/tasks")
        self.step(1)
        text = self.telegram.sent[-1]["text"]
        self.assertIn("Новое дело без срока", text)
        self.assertNotIn("Удалённое дело", text)
        self.assertEqual([row["text"] for row in self.engine.list_tasks()], ["Новое дело без срока"])
        self.assertEqual(self.reminder_messages, [])

    def test_status_reads_current_file_before_background_poll(self):
        self.write("- [ ] Первое дело\n")
        self.step(0)
        self.write(self.tasks.read_text(encoding="utf-8") + "- [ ] Второе дело\n")
        self.queue_callback("test:status")
        self.step(1)
        self.assertIn("Активных задач: 2.", self.telegram.sent[-1]["text"])
        self.assertEqual(self.reminder_messages, [])

    def test_busy_views_and_status_never_present_cached_tasks_as_current(self):
        self.write("- [ ] Старый заголовок\n")
        self.step(0)
        self.write("- [ ] Новая пока занятая версия\n")
        self.queue_message("/tasks")
        with mock.patch.object(markdown_store, "update_markdown", side_effect=markdown_store.MarkdownBusy()):
            self.step(1)
        self.assertIn("Не удалось получить свежий список", self.telegram.sent[-1]["text"])
        self.assertNotIn("Старый заголовок", self.telegram.sent[-1]["text"])
        self.queue_callback("test:status")
        with mock.patch.object(markdown_store, "update_markdown", side_effect=markdown_store.MarkdownBusy()):
            self.step(2)
        self.assertIn("Статус сейчас недоступен", self.telegram.sent[-1]["text"])
        self.assertNotIn("Активных задач:", self.telegram.sent[-1]["text"])

    def test_pagination_button_completes_only_visible_task_and_completed_filter_updates(self):
        self.write("".join(f"- [ ] Дело {index:02}\n" for index in range(7)))
        self.queue_message("/tasks")
        self.step(0)
        first = self.telegram.sent[-1]
        self.assertEqual(len(self.payloads(first, "done:")), 6)
        next_page = self.payloads(first, "list:tasks:active:1")[0]
        self.queue_callback(next_page)
        self.step(1)
        second = self.telegram.sent[-1]
        done = self.payloads(second, "done:")
        self.assertEqual(len(done), 1)
        task_id = done[0].split(":")[1]
        row = next(item for item in self.engine.list_tasks() if item["task_id"] == task_id)
        self.assertIn(row["text"], second["text"])
        before = self.tasks.read_bytes()
        target_line = next(line for line in before.splitlines(keepends=True) if task_id.encode() in line)
        self.queue_callback(done[0])
        self.step(2)
        self.assertEqual(self.tasks.read_bytes(), before.replace(target_line, target_line.replace(b"[ ]", b"[x]", 1), 1))
        self.assertIn("отмечена выполненной", self.telegram.sent[-1]["text"])
        self.queue_callback("list:tasks:completed:0")
        self.step(3)
        completed = self.telegram.sent[-1]
        self.assertIn("Задач: 1.", completed["text"])
        self.assertIn(row["text"], completed["text"])
        self.assertEqual(self.payloads(completed, "done:"), [])
        self.assertEqual(self.reminder_messages, [])

    def test_old_done_button_rejects_unsynced_deadline_edit_then_new_view_succeeds(self):
        self.write("- [ ] Перенесённое дело 🔔 2026-10-17 09:30\n")
        self.queue_message("/tasks")
        self.step(0)
        old_done = self.payloads(self.telegram.sent[-1], "done:")[0]
        self.write(self.tasks.read_text(encoding="utf-8").replace("09:30", "10:00"))
        before = self.tasks.read_bytes()
        self.queue_callback(old_done)
        self.step(1)
        self.assertEqual(self.tasks.read_bytes(), before)
        self.assertIn("Задача изменилась", self.telegram.sent[-1]["text"])
        self.queue_message("/tasks")
        self.step(2)
        new_done = self.payloads(self.telegram.sent[-1], "done:")[0]
        self.assertNotEqual(new_done, old_done)
        self.queue_callback(new_done)
        self.step(3)
        self.assertEqual(self.engine.list_tasks()[0]["state"], "completed")
        self.clock = NOW + timedelta(hours=8)
        self.step(30)
        self.assertEqual(self.reminder_messages, [])

    def test_deadline_view_shows_original_deadline_after_repeat_is_scheduled(self):
        self.write("- [ ] С исходным сроком 🔔 2026-10-17 09:01\n")
        self.step(0)
        self.clock = NOW + timedelta(minutes=1)
        self.step(60)
        row = self.engine.list_tasks()[0]
        self.assertEqual(row["next_reminder_utc"], self.clock.timestamp() + 7200)
        self.queue_message("/deadlines")
        self.step(61)
        text = self.telegram.sent[-1]["text"]
        self.assertIn("Срок: 17.10.2026 09:01", text)
        self.assertNotIn("11:01", text)
        self.assertEqual(len(self.reminder_messages), 1)

    def test_one_shot_delivery_stays_active_until_actual_checkbox_completion(self):
        values = json.loads(self.config.read_text(encoding="utf-8"))
        values["repeat_seconds"] = 0
        self.config.write_text(json.dumps(values), encoding="utf-8")
        self.engine = reminders.TaskEngine(self.config, sender=self.reminder_sender,
                                           token_loader=lambda path: TOKEN)
        self.service.engine = self.engine
        self.write("- [ ] Отправка не означает выполнение 🔔 2026-10-17 09:01\n")
        self.step(0)
        self.clock = NOW + timedelta(minutes=1)
        self.step(60)
        self.assertEqual(self.engine.list_tasks()[0]["state"], "sent")
        self.queue_callback("test:status")
        self.step(61)
        status = self.telegram.sent[-1]["text"]
        self.assertIn("Активных задач: 1.", status)
        self.assertIn("Ожидают напоминания: 0.", status)
        self.queue_message("/tasks")
        self.step(62)
        message = self.telegram.sent[-1]
        self.assertIn("Отправка не означает выполнение", message["text"])
        self.queue_callback(self.payloads(message, "done:")[0])
        self.step(63)
        self.assertEqual(self.engine.status()["counts"]["active"], 0)
        self.assertEqual(self.engine.list_tasks()[0]["state"], "completed")
        self.assertEqual(len(self.reminder_messages), 1)

    def test_controls_backlog_gives_due_reminder_a_turn_before_all_views_finish(self):
        self.write("- [ ] Пора напомнить 🔔 2026-10-17 09:01\n")
        self.step(0)
        self.clock = NOW + timedelta(minutes=1)
        for unused in range(9):
            self.queue_callback("list:tasks:active:0")
        self.events.clear()
        self.step(60)
        reminder_index = next(index for index, item in enumerate(self.events) if item[0] == "reminder")
        views_before = sum(item[0] == "sendMessage" for item in self.events[:reminder_index])
        self.assertGreater(views_before, 0)
        self.assertLess(views_before, 9, "A whole control backlog must not monopolize the reminder worker")
        self.step(61)
        view_messages = [item for item in self.telegram.sent if "📝 Задачи" in item["text"]]
        self.assertEqual(len(view_messages), 9)
        self.assertEqual(len(self.reminder_messages), 1)

    def test_second_cli_worker_is_busy_without_constructing_private_queues(self):
        # Even a regressed CLI cannot reach Telegram: remove fixture authorization.
        for name in ("token.dpapi", "token.enc", "settings.json"):
            (self.data / name).unlink(missing_ok=True)
        self.write("- [ ] Не должен появиться ID второго worker\n")
        original = self.tasks.read_bytes()
        with reminders.WorkerLock(self.engine.lock_path):
            result = subprocess.run([sys.executable, "-B", str(APP / "bot_service.py"),
                "--config", str(self.config), "--once"], capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 0)
        self.assertEqual(json.loads(result.stdout), {"status": "busy"})
        self.assertEqual(self.tasks.read_bytes(), original)


if __name__ == "__main__":
    unittest.main(verbosity=2)
