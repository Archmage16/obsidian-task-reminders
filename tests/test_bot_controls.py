"""Offline controls tests: fake IDs and fake transport; no network or vault writes."""
import datetime as dt
import importlib.util
import json
from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest
from unittest.mock import patch

MODULE = Path(__file__).resolve().parents[1] / "bot_controls.py"
spec = importlib.util.spec_from_file_location("bot_controls", MODULE)
bc = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = bc
spec.loader.exec_module(bc)
OWNER = 12345
TOKEN = "900001:fake_test_token"
STAMP = 1792213200.0
TASK = "11111111-2222-4333-8444-555555555555"


def message(update_id, text="/test", date=STAMP, user=OWNER, chat=OWNER, kind="private"):
    return {"update_id": update_id, "message": {"date": date, "from": {"id": user},
            "chat": {"id": chat, "type": kind}, "text": text}}


def callback(update_id, data="test:ping", callback_id=None, user=OWNER, chat=OWNER, kind="private"):
    return {"update_id": update_id, "callback_query": {
        "id": callback_id or f"cb-{update_id}", "from": {"id": user}, "data": data,
        # A menu may be old; its date must not invalidate the new button click.
        "message": {"date": 1, "from": {"id": 900001}, "chat": {"id": chat, "type": kind}}}}


class FakeTransport:
    def __init__(self):
        self.calls = []
        self.updates = []
        self.fail = set()

    def __call__(self, token, method, payload, timeout=5):
        self.calls.append((method, payload, timeout))
        if method in self.fail:
            raise RuntimeError("https://api.telegram.org/botPRIVATE_SECRET/getUpdates?secret=PRIVATE_SECRET")
        return {"ok": True, "result": self.updates if method == "getUpdates" else {"message_id": len(self.calls)}}

    def sent(self):
        return [item[1] for item in self.calls if item[0] == "sendMessage"]

    def acks(self):
        return [item[1] for item in self.calls if item[0] == "answerCallbackQuery"]


class ControlsTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="controls-offline-")
        self.addCleanup(self.temp.cleanup)
        self.transport = FakeTransport()
        self.status = {"counts": {"active": 3, "pending": 2, "leased": 1},
                       "next_due": "2026-10-17T13:00:00Z", "last_run": "2026-10-17T12:40:00+00:00",
                       "repeat_minutes": 120, "task_text": "PRIVATE_TASK", "error": "PRIVATE_SECRET"}
        self.controls = self.make()

    def make(self, **kwargs):
        return bc.BotControls(self.temp.name, lambda: self.status, self.transport, now=STAMP, **kwargs)

    def process(self, update, now=STAMP):
        return self.controls.process_update(TOKEN, OWNER, update, now)

    def sql(self, query):
        db = sqlite3.connect(Path(self.temp.name) / "controls.sqlite3")
        try:
            return db.execute(query).fetchall()
        finally:
            db.close()

    def test_tick_never_publishes_menu_automatically_and_safe_poll(self):
        result = self.controls.tick(TOKEN, OWNER)
        self.assertIsNone(result["error"])
        self.assertEqual(self.transport.sent(), [])
        method, payload, timeout = self.transport.calls[0]
        self.assertEqual(method, "getUpdates")
        self.assertEqual(payload["timeout"], 1)
        self.assertEqual(timeout, 5)
        self.assertEqual(payload["allowed_updates"], ["message", "callback_query"])

    def test_explicit_menu_and_button_contract(self):
        self.assertTrue(self.controls.publish_menu(TOKEN, OWNER)["published"])
        sent = self.transport.sent()[0]
        buttons = sum(sent["reply_markup"]["inline_keyboard"], [])
        self.assertEqual([b["callback_data"] for b in buttons], ["list:tasks:active:0", "list:deadlines:active:0", "test:ping", "test:minute", "test:status"])
        self.assertIn("каждые 2 часа", sent["text"])
        self.assertIn("в указанный срок", sent["text"])
        self.assertNotIn("PRIVATE", json.dumps(sent))

    def test_commands_and_stale_backlog(self):
        for index, command in enumerate(("/start", "/help", "/test", "/menu", "/test@FakeBot"), 1):
            self.process(message(index, command))
        self.assertEqual(len(self.transport.sent()), 5)
        self.process(message(6, "/start", STAMP - 301))
        self.process(message(7, "/start", STAMP + 61))
        self.assertEqual(len(self.transport.sent()), 5)
        self.assertEqual(self.sql("SELECT value FROM meta WHERE key='offset'")[0][0], "8")

    def test_owner_private_required_for_commands_and_callbacks(self):
        updates = [message(1, user=777), message(2, chat=-99, kind="group"),
                   callback(3, user=777), callback(4, chat=777), callback(5, kind="group")]
        absent_message = callback(6)
        del absent_message["callback_query"]["message"]
        updates.append(absent_message)
        absent_sender = callback(7)
        del absent_sender["callback_query"]["from"]
        absent_sender["callback_query"]["message"]["from"] = {"id": OWNER}
        updates.append(absent_sender)
        for update in updates:
            self.process(update)
        self.assertEqual(self.transport.sent(), [])
        self.assertEqual(self.transport.acks(), [])
        self.assertEqual(self.sql("SELECT COUNT(*) FROM jobs")[0][0], 0)

    def test_ping_deduplicates_update_and_callback_after_restart(self):
        update = callback(1)
        self.process(update)
        self.process(update)
        self.controls = self.make()
        self.process(callback(2, callback_id="cb-1"), STAMP + 10)
        self.assertEqual(len(self.transport.sent()), 1)
        self.assertEqual(len(self.transport.acks()), 3)
        self.assertIn("✅", self.transport.sent()[0]["text"])

    def test_callback_cooldown(self):
        self.process(callback(1))
        result = self.process(callback(2), STAMP + 4)
        self.assertEqual(result["action"], "cooldown")
        self.assertEqual(len(self.transport.sent()), 1)
        self.process(callback(3), STAMP + 5)
        self.assertEqual(len(self.transport.sent()), 2)

    def test_minute_job_persists_reuses_and_delivers_normally_once(self):
        self.process(callback(1, "test:minute"))
        self.process(callback(2, "test:minute"), STAMP + 10)
        self.assertEqual(self.sql("SELECT COUNT(*) FROM jobs")[0][0], 1)
        self.assertEqual(self.sql("SELECT due FROM jobs")[0][0], STAMP + 60)
        self.controls = self.make()
        self.controls.tick(TOKEN, OWNER, STAMP + 59)
        self.assertEqual(len(self.transport.sent()), 2)
        result = self.controls.tick(TOKEN, OWNER, STAMP + 60)
        self.assertEqual(result["pending_tests"], 0)
        self.assertIn("Минутный тест пройден", self.transport.sent()[-1]["text"])
        self.controls.tick(TOKEN, OWNER, STAMP + 1000)
        self.assertEqual(len(self.transport.sent()), 3)
        self.assertEqual(self.sql("SELECT state FROM jobs")[0][0], "sent")

    def test_callback_ack_failure_does_not_repeat_local_schedule(self):
        self.transport.fail.add("answerCallbackQuery")
        result = self.process(callback(1, "test:minute"))
        self.assertEqual(result["error"], "telegram_unavailable")
        self.transport.fail.clear()
        self.controls = self.make()
        self.process(callback(2, "test:minute", callback_id="cb-1"), STAMP + 10)
        self.assertEqual(self.sql("SELECT COUNT(*) FROM jobs")[0][0], 1)
        self.assertEqual(len(self.transport.sent()), 1)

    def test_due_test_retries_after_send_failure_and_polls_can_fail(self):
        self.process(callback(1, "test:minute"))
        self.transport.fail.update({"getUpdates", "sendMessage"})
        result = self.controls.tick(TOKEN, OWNER, STAMP + 60)
        self.assertEqual(result["pending_tests"], 1)
        self.assertNotIn("PRIVATE", json.dumps(result))
        self.assertEqual(self.sql("SELECT due FROM jobs")[0][0], STAMP + 65)
        self.transport.fail.remove("sendMessage")
        result = self.controls.tick(TOKEN, OWNER, STAMP + 65)
        self.assertEqual(result["pending_tests"], 0)
        self.assertEqual(result["counts"]["tests_sent"], 1)

    def test_cancel_generation_survives_restart(self):
        self.process(callback(1, "test:minute"))
        self.assertTrue(self.controls.cancel_tests()["cancelled"])
        self.controls = self.make()
        self.controls.tick(TOKEN, OWNER, STAMP + 100)
        self.assertEqual(self.sql("SELECT state FROM jobs")[0][0], "cancelled")
        self.assertEqual(len(self.transport.sent()), 1)

    def test_status_uses_astana_and_only_selected_fields(self):
        self.process(callback(1, "test:status"))
        text = self.transport.sent()[0]["text"]
        self.assertIn("Время Астаны", text)
        self.assertIn("17.10 18:00", text)
        self.assertIn("17.10 17:40", text)
        self.assertIn("Активных задач: 3", text)
        self.assertIn("Ожидают напоминания: 3", text)
        self.assertIn("120 минут", text)
        self.assertNotIn("PRIVATE", text)

    def test_unknown_status_is_not_zero(self):
        self.status = {}
        self.process(callback(1, "test:status"))
        text = self.transport.sent()[0]["text"]
        self.assertIn("ещё не получено", text)
        self.assertIn("Нет запланированных", text)
        self.assertNotIn("Активных задач: 0", text)

    def test_unknown_callback_acknowledged_without_effect(self):
        for index, data in enumerate(("unknown", "done:x:1", "done:" + TASK + ":x", "done:" + TASK + ":" + "1" * 30, None, {}, "\ud800"), 1):
            self.process(callback(index, data))
        self.assertEqual(len(self.transport.acks()), 7)
        self.assertEqual(self.transport.sent(), [])

    def test_bot_identity_scope_rotation_and_change(self):
        self.process(message(100, date=STAMP - 500))
        self.controls.tick("900001:rotated_fake_token", OWNER)
        self.assertEqual(self.transport.calls[-1][1]["offset"], 101)
        self.process(callback(101, "test:minute"))
        self.controls.tick("900002:other_fake_bot", OWNER)
        self.assertEqual(self.transport.calls[-1][1]["offset"], 0)
        self.assertEqual(self.sql("SELECT state FROM jobs")[0][0], "cancelled")
        serialized = json.dumps(self.sql("SELECT * FROM meta"))
        self.assertNotIn("fake_token", serialized)
        self.assertNotIn(TOKEN, serialized)

    def test_storage_failure_stops_batch_before_advancing_later_update(self):
        self.transport.updates = [message(10), message(11)]
        original = self.controls._prepare
        def fail_first(update, owner, stamp):
            if update["update_id"] == 10:
                raise sqlite3.OperationalError("locked")
            return original(update, owner, stamp)
        with patch.object(self.controls, "_prepare", fail_first):
            result = self.controls.tick(TOKEN, OWNER)
        self.assertEqual(result["error"], "storage_unavailable")
        self.assertEqual(self.transport.sent(), [])
        self.assertEqual(self.sql("SELECT COUNT(*) FROM updates")[0][0], 0)
        self.transport.updates = []
        self.controls.tick(TOKEN, OWNER)
        self.assertEqual(self.transport.calls[-1][1]["offset"], 0)

    def test_completion_owner_validation_version_and_dedup(self):
        calls = []
        self.controls = self.make(complete_task_handler=lambda task_id, version: calls.append((task_id, version)) or {"status": "completed"})
        data = f"done:{TASK}:7"
        self.process(callback(1, data, user=777))
        self.process(callback(2, data, kind="group"))
        self.assertEqual(calls, [])
        self.process(callback(3, data))
        self.controls = self.make(complete_task_handler=lambda *args: calls.append(args))
        self.process(callback(4, data, callback_id="cb-3"))
        self.assertEqual(calls, [(TASK, 7)])
        self.assertIn("Напоминания о ней остановлены", self.transport.sent()[0]["text"])
        self.assertEqual(len(self.transport.sent()), 1)
        self.assertEqual(self.sql("SELECT COUNT(*) FROM jobs")[0][0], 0)

    def test_completion_changed_missing_busy_and_error_are_safe(self):
        responses = iter(({"status": "changed"}, {"status": "not_found"}, {"status": "already_completed"}, {"status": "busy"}, {"status": "unexpected PRIVATE_SECRET"}))
        self.controls = self.make(complete_task_handler=lambda *_: next(responses))
        for index in range(1, 6):
            self.process(callback(index, f"done:{TASK}:7"))
        text = json.dumps(self.transport.sent(), ensure_ascii=False)
        for phrase in ("Задача изменилась", "не найдена", "уже выполнена", "Файл сейчас занят", "Не удалось обновить"):
            self.assertIn(phrase, text)
        self.assertNotIn("PRIVATE_SECRET", text)
        self.assertEqual(self.sql("SELECT value FROM meta WHERE key='offset'")[0][0], "6")

    def test_completion_exception_can_be_reclicked_with_new_callback(self):
        attempts = []
        def handler(*args):
            attempts.append(args)
            if len(attempts) == 1:
                raise RuntimeError("PRIVATE_SECRET")
            return {"status": "completed"}
        self.controls = self.make(complete_task_handler=handler)
        self.process(callback(1, f"done:{TASK}:7"))
        self.process(callback(2, f"done:{TASK}:7"))
        self.assertEqual(len(attempts), 2)
        self.assertIn("Файл сейчас занят", self.transport.sent()[0]["text"])
        self.assertIn("отмечена выполненной", self.transport.sent()[1]["text"])

    def test_completion_keyboard_validates_payload(self):
        data = self.controls.completion_keyboard(TASK, 7)["inline_keyboard"][0][0]["callback_data"]
        self.assertLessEqual(len(data.encode()), 64)
        with self.assertRaises(bc.ControlsError):
            self.controls.completion_keyboard("bad", 7)

    def test_native_transport_disallows_arbitrary_method_without_network(self):
        with patch.object(bc.urllib.request, "build_opener") as opener:
            with self.assertRaises(bc.ControlsError):
                bc.telegram_transport(TOKEN, "deleteMessage", {})
            with self.assertRaises(bc.ControlsError):
                bc.telegram_transport("900001:secret/evil", "getUpdates", {})
            opener.assert_not_called()

    def test_native_transport_closes_http_error_and_hides_url(self):
        import io
        error = bc.urllib.error.HTTPError("https://example.invalid/PRIVATE_SECRET", 302, "PRIVATE_SECRET", {}, io.BytesIO(b"PRIVATE_SECRET"))
        with patch.object(bc.urllib.request, "build_opener") as opener:
            opener.return_value.open.side_effect = error
            with self.assertRaises(bc.ControlsError) as caught:
                bc.telegram_transport(TOKEN, "getUpdates", {})
            self.assertEqual(str(caught.exception), "telegram_unavailable")
            self.assertTrue(error.fp.closed)

    def test_tasks_commands_filters_and_fresh_provider(self):
        rows = [{"task_id": "a", "text": "ACTIVE-TITLE", "state": "pending", "due_utc": STAMP + 60},
                {"task_id": "b", "text": "FINISHED-TITLE", "state": "completed", "due_utc": STAMP - 60},
                {"task_id": "c", "text": "DELETED-TITLE", "state": "deleted", "due_utc": STAMP},
                {"task_id": "d", "text": "NO-TIME-TITLE", "state": "unscheduled", "due_utc": None}]
        reads = []
        self.controls = self.make(task_provider=lambda: reads.append(1) or rows)
        self.process(message(1, "/tasks"))
        first = self.transport.sent()[-1]["text"]
        self.assertIn("ACTIVE-TITLE", first)
        self.assertIn("NO-TIME-TITLE", first)
        self.assertNotIn("FINISHED-TITLE", first)
        self.process(callback(2, "list:tasks:completed:0"))
        second = self.transport.sent()[-1]["text"]
        self.assertIn("FINISHED-TITLE", second)
        self.assertNotIn("ACTIVE-TITLE", second)
        rows.append({"task_id": "e", "text": "NEW-TITLE", "state": "invalid", "due_utc": None})
        self.process(callback(3, "list:tasks:all:0"))
        third = self.transport.sent()[-1]["text"]
        self.assertIn("ACTIVE-TITLE", third)
        self.assertIn("FINISHED-TITLE", third)
        self.assertIn("NEW-TITLE", third)
        self.assertNotIn("DELETED-TITLE", third)
        self.assertEqual(len(reads), 3)

    def test_deadlines_original_due_not_repeat_and_overdue(self):
        due = dt.datetime(2026, 10, 17, 11, 0, tzinfo=dt.timezone.utc).timestamp()
        now = dt.datetime(2026, 10, 17, 13, 0, tzinfo=dt.timezone.utc).timestamp()
        rows = [{"task_id": "a", "text": "ORIGINAL-DUE", "state": "pending", "due_utc": due,
                 "next_reminder_utc": due + 7200},
                {"task_id": "b", "text": "COMPLETE-SKIP", "state": "completed", "due_utc": due},
                {"task_id": "c", "text": "DELETED-SKIP", "state": "deleted", "due_utc": due},
                {"task_id": "d", "text": "UNSCHEDULED", "state": "unscheduled", "due_utc": None}]
        self.controls = self.make(task_provider=lambda: rows)
        self.process(message(1, "/deadlines", date=now), now=now)
        text = self.transport.sent()[-1]["text"]
        self.assertIn("17.10.2026 16:00", text)
        self.assertNotIn("17.10.2026 18:00", text)
        self.assertIn("просрочено", text)
        self.assertIn("Со сроком: 1. Без срока: 1.", text)
        self.assertIn("Без срока\n", text)
        self.assertIn("UNSCHEDULED", text)
        self.assertNotIn("COMPLETE-SKIP", text)
        self.assertNotIn("DELETED-SKIP", text)

    def test_large_list_pagination_covers_all_rows_once(self):
        rows = [{"task_id": str(i), "text": f"TASK-{i:04d}-END " + "подробности " * 12,
                 "state": "pending", "due_utc": STAMP + i * 60} for i in range(250)]
        self.controls = self.make(task_provider=lambda: rows)
        seen = []
        page = 0
        while True:
            self.process(callback(page + 1, f"list:tasks:active:{page}"))
            sent = self.transport.sent()[-1]
            self.assertLessEqual(bc._utf16_length(sent["text"]), 4096)
            seen.extend(__import__("re").findall(r"TASK-([0-9]{4})-END", sent["text"]))
            buttons = sum(sent["reply_markup"]["inline_keyboard"], [])
            next_buttons = [button for button in buttons if button["text"] == "Далее →"]
            for button in buttons:
                self.assertLessEqual(len(button["callback_data"].encode("utf-8")), 64)
            if not next_buttons:
                break
            self.assertEqual(next_buttons[0]["callback_data"], f"list:tasks:active:{page + 1}")
            page += 1
            self.assertLess(page, 250)
        self.assertGreater(page, 1)
        self.assertEqual(seen, [f"{i:04d}" for i in range(250)])

    def test_long_unicode_title_preserved_across_utf16_safe_pages(self):
        title = ("😀Ж👩‍💻<>&_* " * 1600) + "END-TITLE"
        self.controls = self.make(task_provider=lambda: [{"task_id": "a", "text": title, "state": "pending", "due_utc": None}])
        pages = []
        page = 0
        while True:
            text, keyboard = self.controls._view_message("tasks", "active", page, STAMP)
            self.assertLessEqual(bc._utf16_length(text), 4096)
            self.assertNotIn("\ufffd", text)
            body = text.split("\n\n", 1)[1].rsplit("\n\nСтраница ", 1)[0]
            for block in body.split("\n\n"):
                fragment = block.removeprefix("1. ☐ ").removeprefix(" (продолжение)\n").removesuffix("\nБез срока.")
                pages.append(fragment)
            if not any(button["text"] == "Далее →" for button in sum(keyboard["inline_keyboard"], [])):
                break
            page += 1
        self.assertGreater(page, 3)
        self.assertEqual("".join(pages), title)

    def test_page_out_of_range_clamps_after_live_list_changes(self):
        self.controls = self.make(task_provider=lambda: [{"text": "ONLY", "state": "pending", "due_utc": None}])
        self.process(callback(1, "list:tasks:active:999999999"))
        self.assertIn("Страница 1/1", self.transport.sent()[-1]["text"])
        self.assertIn("ONLY", self.transport.sent()[-1]["text"])

    def test_empty_and_unscheduled_only_are_clear(self):
        rows = []
        self.controls = self.make(task_provider=lambda: rows)
        self.process(message(1, "/tasks"))
        self.assertIn("Активных задач нет", self.transport.sent()[-1]["text"])
        self.process(message(2, "/deadlines"))
        self.assertIn("Незавершённых задач нет", self.transport.sent()[-1]["text"])
        rows.append({"text": "NO-DATE", "state": "invalid", "due_utc": None})
        self.process(callback(3, "list:deadlines:active:0"))
        text = self.transport.sent()[-1]["text"]
        self.assertIn("Со сроком: 0. Без срока: 1", text)
        self.assertIn("NO-DATE", text)
        self.assertIn("Без срока", text)

    def test_task_list_owner_gate_and_stale_commands_prevent_read(self):
        reads = []
        self.controls = self.make(task_provider=lambda: reads.append(1) or [])
        updates = [message(1, "/tasks", user=777), message(2, "/deadlines", kind="group"),
                   message(3, "/tasks", date=STAMP - 301),
                   callback(4, "list:tasks:all:0", user=777),
                   callback(5, "list:deadlines:active:0", chat=777),
                   callback(6, "list:tasks:active:0", kind="group")]
        for update in updates:
            self.process(update)
        self.assertEqual(reads, [])
        self.assertEqual(self.transport.sent(), [])
        self.assertEqual(self.transport.acks(), [])

    def test_view_callback_replay_does_not_read_or_send_again(self):
        reads = []
        self.controls = self.make(task_provider=lambda: reads.append(1) or [])
        self.process(callback(1, "list:tasks:active:0"))
        self.controls = self.make(task_provider=lambda: reads.append(1) or [])
        self.process(callback(2, "list:tasks:active:0", callback_id="cb-1"))
        self.assertEqual(len(reads), 1)
        self.assertEqual(len(self.transport.sent()), 1)
        self.assertEqual(len(self.transport.acks()), 2)

    def test_bad_view_payload_has_no_provider_side_effect(self):
        reads = []
        self.controls = self.make(task_provider=lambda: reads.append(1) or [])
        for index, data in enumerate(("list:tasks:active:-1", "list:tasks:unknown:0", "list:deadlines:completed:0",
                                      "list:tasks:active:1000000000", "list:tasks:active:0:extra"), 1):
            self.process(callback(index, data))
        self.assertEqual(reads, [])
        self.assertEqual(self.transport.sent(), [])
        self.assertEqual(len(self.transport.acks()), 5)

    def test_unavailable_provider_no_error_details_or_internal_fields(self):
        def broken():
            raise RuntimeError("PRIVATE_SECRET DB_PATH")
        self.controls = self.make(task_provider=broken)
        self.process(message(1, "/tasks"))
        text = self.transport.sent()[-1]["text"]
        self.assertIn("Не удалось получить свежий список", text)
        self.assertNotIn("PRIVATE", text)
        self.controls = self.make(task_provider=lambda: [{"text": "ALLOWED-TITLE", "state": "pending", "due_utc": None,
                                                         "last_error": "PRIVATE_SECRET", "lease_owner": "PRIVATE_OWNER"}])
        self.process(message(2, "/tasks"))
        self.assertIn("ALLOWED-TITLE", self.transport.sent()[-1]["text"])
        self.assertNotIn("PRIVATE", self.transport.sent()[-1]["text"])
        self.assertNotIn("ALLOWED-TITLE", json.dumps(self.sql("SELECT * FROM meta")))

    def test_malformed_dates_and_plain_text_safe(self):
        rows = [{"text": "<b>PLAIN</b> *_ `code` 😀\x00", "state": "pending", "due_utc": "bad date"},
                {"text": "ISO-TIME", "state": "pending", "due_utc": "2026-10-17T13:00:00Z"},
                {"text": "NAN-TIME", "state": "pending", "due_utc": float("nan")}]
        self.controls = self.make(task_provider=lambda: rows)
        self.process(message(1, "/tasks"))
        sent = self.transport.sent()[-1]
        self.assertNotIn("parse_mode", sent)
        self.assertIn("<b>PLAIN</b>", sent["text"])
        self.assertNotIn("\x00", sent["text"])
        self.assertIn("17.10.2026 18:00", sent["text"])

    def test_main_menu_callback_and_missing_provider(self):
        self.process(callback(1, "list:menu"))
        self.assertIn("Задачи и напоминания", self.transport.sent()[-1]["text"])
        self.process(message(2, "/tasks"))
        self.assertIn("Список задач сейчас недоступен", self.transport.sent()[-1]["text"])

    def test_six_items_per_page_and_done_buttons_match_visible_numbers(self):
        import uuid
        rows = [{"task_id": str(uuid.UUID(int=index + 1)), "schedule_version": 2,
                 "text": f"VISIBLE-{index + 1}", "state": "pending", "due_utc": STAMP + index}
                for index in range(14)]
        self.controls = self.make(task_provider=lambda: rows)
        self.process(message(1, "/tasks"))
        first = self.transport.sent()[-1]
        self.assertIn("Страница 1/3", first["text"])
        self.assertIn("6. ☐ VISIBLE-6", first["text"])
        self.assertNotIn("VISIBLE-7", first["text"])
        done = [button for button in sum(first["reply_markup"]["inline_keyboard"], []) if button["callback_data"].startswith("done:")]
        self.assertEqual([button["text"] for button in done], [f"✅ {i}" for i in range(1, 7)])
        self.assertEqual([button["callback_data"] for button in done], [f"done:{row['task_id']}:2" for row in rows[:6]])
        self.process(callback(2, "list:tasks:active:1"))
        second = self.transport.sent()[-1]
        self.assertIn("7. ☐ VISIBLE-7", second["text"])
        done = [button for button in sum(second["reply_markup"]["inline_keyboard"], []) if button["callback_data"].startswith("done:")]
        self.assertEqual([button["text"] for button in done], [f"✅ {i}" for i in range(7, 13)])
        for button in done:
            self.assertLessEqual(len(button["callback_data"].encode()), 64)

    def test_unscheduled_section_heading_stays_with_task_after_page_break(self):
        rows = [{"task_id": str(i), "text": "DATED", "state": "pending", "due_utc": STAMP + i} for i in range(6)]
        rows.append({"task_id": "undated", "text": "UNDATED", "state": "unscheduled", "due_utc": None})
        self.controls = self.make(task_provider=lambda: rows)
        first, _ = self.controls._view_message("deadlines", "active", 0, STAMP)
        second, _ = self.controls._view_message("deadlines", "active", 1, STAMP)
        self.assertNotIn("Без срока\nУ этих", first)
        self.assertIn("Без срока\nУ этих", second)
        self.assertIn("7. ☐ UNDATED", second)

    def test_unscheduled_task_done_through_existing_handler_and_not_found(self):
        rows = [{"task_id": TASK, "schedule_version": 9, "text": "NO-DATE-DONE", "state": "unscheduled", "due_utc": None}]
        completions = []
        self.controls = self.make(task_provider=lambda: rows,
                                  complete_task_handler=lambda *args: completions.append(args) or {"status": "not_found"})
        self.process(message(1, "/deadlines"))
        sent = self.transport.sent()[-1]
        done = [button for button in sum(sent["reply_markup"]["inline_keyboard"], []) if button["callback_data"].startswith("done:")]
        self.assertEqual(done, [{"text": "✅ 1", "callback_data": f"done:{TASK}:9"}])
        self.process(callback(2, done[0]["callback_data"]))
        self.assertEqual(completions, [(TASK, 9)])
        self.assertIn("больше не найдена", self.transport.sent()[-1]["text"])

    def test_completed_and_malformed_rows_have_no_done_buttons(self):
        import uuid
        rows = [{"task_id": TASK, "schedule_version": 1, "text": "COMPLETED", "state": "completed", "due_utc": None},
                {"task_id": "bad-id", "schedule_version": 1, "text": "BAD-UUID", "state": "pending", "due_utc": None},
                {"task_id": str(uuid.UUID(int=2)), "schedule_version": 0, "text": "ZERO-VERSION", "state": "pending", "due_utc": None},
                {"task_id": str(uuid.UUID(int=3)), "schedule_version": -1, "text": "NEGATIVE-VERSION", "state": "pending", "due_utc": None},
                {"task_id": str(uuid.UUID(int=4)), "schedule_version": "7", "text": "STRING-VERSION", "state": "pending", "due_utc": None},
                {"task_id": str(uuid.UUID(int=5)), "schedule_version": True, "text": "BOOL-VERSION", "state": "pending", "due_utc": None}]
        self.controls = self.make(task_provider=lambda: rows)
        self.process(callback(1, "list:tasks:all:0"))
        done = [button for button in sum(self.transport.sent()[-1]["reply_markup"]["inline_keyboard"], []) if button["callback_data"].startswith("done:")]
        self.assertEqual(done, [])
        self.process(callback(2, "list:tasks:completed:0"))
        done = [button for button in sum(self.transport.sent()[-1]["reply_markup"]["inline_keyboard"], []) if button["callback_data"].startswith("done:")]
        self.assertEqual(done, [])

    def test_long_task_done_keeps_same_number_and_uuid_on_continuation_pages(self):
        row = {"task_id": TASK, "schedule_version": 3, "text": "😀" * 9000, "state": "unscheduled", "due_utc": None}
        self.controls = self.make(task_provider=lambda: [row])
        for page in range(3):
            self.process(callback(page + 1, f"list:tasks:active:{page}"))
            sent = self.transport.sent()[-1]
            self.assertLessEqual(bc._utf16_length(sent["text"]), 4096)
            self.assertIn("1. ☐ ", sent["text"])
            done = [button for button in sum(sent["reply_markup"]["inline_keyboard"], []) if button["callback_data"].startswith("done:")]
            self.assertEqual(done, [{"text": "✅ 1", "callback_data": f"done:{TASK}:3"}])


if __name__ == "__main__":
    unittest.main(verbosity=2)
