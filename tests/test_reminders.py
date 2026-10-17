"""Meaningful offline checks. All files/databases are temporary; no Telegram."""
from datetime import datetime, timedelta, timezone
import contextlib
import importlib.util
import io
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

sys.dont_write_bytecode = True
MODULE = Path(__file__).resolve().parents[1] / "reminders.py"
sys.path.insert(0, str(MODULE.parent))
spec = importlib.util.spec_from_file_location("tested_reminders", MODULE)
r = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = r
spec.loader.exec_module(r)
UTC = timezone.utc
NOW = datetime(2026, 10, 17, 4, 0, tzinfo=UTC)  # 09:00 in Qyzylorda.


class EngineTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.vault = self.root / "vault"
        self.vault.mkdir()
        self.taskfile = self.vault / "Задачи.md"
        self.taskfile.write_text("# Задачи\n", encoding="utf-8")
        self.data = self.root / "private-data"
        self.config = self.root / "config.json"
        self.config.write_text(json.dumps({"vault":str(self.vault), "tasks_file":"Задачи.md",
            "data_directory":str(self.data), "timezone":"Asia/Qyzylorda", "poll_seconds":20,
            "repeat_seconds":0}), encoding="utf-8")
        self.sent = []
        self.engine = self.new_engine()

    def new_engine(self, sender=None):
        return r.TaskEngine(self.config, sender=sender or self.record_send,
                            token_loader=lambda unused: "offline-test-value")

    def record_send(self, token, chat_id, text):
        self.sent.append((chat_id, text))

    def authorize(self):
        # Dummy encrypted-file placeholder and injected loader; no real credentials.
        (self.data / ("token.dpapi" if os.name == "nt" else "token.enc")).write_bytes(b"offline-placeholder")
        (self.data / "settings.json").write_text(json.dumps({"chat_id":1234,"bot_username":"offline_bot"}),encoding="utf-8")

    def write(self, text):
        self.taskfile.write_text(text, encoding="utf-8", newline="")

    def rows(self):
        return self.engine.list_tasks()

    def enable_repeats(self, seconds=7200):
        values=json.loads(self.config.read_text(encoding="utf-8"))
        values["repeat_seconds"]=seconds
        self.config.write_text(json.dumps(values),encoding="utf-8")
        self.engine=self.new_engine()
        return self.engine

    def recreate_legacy_table(self, rows):
        """Rebuild only this fixture's DB using the exact old queue shape."""
        columns=("task_id","text","expression","due_utc","state","schedule_version","delivered_version",
                 "armed","first_seen_utc","updated_utc","delivered_utc","retry_at","lease_owner","lease_until",
                 "attempts","last_error")
        with self.engine._connection() as connection:
            connection.executescript("""DROP TABLE tasks;
                CREATE TABLE tasks(task_id TEXT PRIMARY KEY,text TEXT NOT NULL,expression TEXT,due_utc REAL,
                    state TEXT NOT NULL,schedule_version INTEGER NOT NULL DEFAULT 1,
                    delivered_version INTEGER NOT NULL DEFAULT 0,armed INTEGER NOT NULL DEFAULT 0,
                    first_seen_utc REAL NOT NULL,updated_utc REAL NOT NULL,delivered_utc REAL,
                    retry_at REAL,lease_owner TEXT,lease_until REAL,attempts INTEGER NOT NULL DEFAULT 0,last_error TEXT);
            """)
            for row in rows:
                connection.execute("INSERT INTO tasks("+",".join(columns)+") VALUES("+",".join("?" for unused in columns)+")",
                                   tuple(row[key] for key in columns))

    def due_task(self):
        self.write("- [ ] Проверить backup 🔔 2026-10-17 09:01\n")
        self.engine.sync_tasks(now=NOW)
        return NOW + timedelta(minutes=1)

    def test_absolute_date_and_qyzylorda_timezone(self):
        parsed = r.parse_reminder("2026-10-17 09:30", NOW, r.user_timezone("Asia/Qyzylorda"))
        self.assertEqual(parsed, datetime(2026,10,17,4,30,tzinfo=UTC))

    def test_relative_date_uses_local_not_utc_day(self):
        late_utc = datetime(2026,10,17,21,0,tzinfo=UTC)
        parsed = r.parse_reminder("завтра 09:00",late_utc,r.user_timezone("Asia/Qyzylorda"))
        self.assertEqual(parsed,datetime(2026,10,19,4,0,tzinfo=UTC))

    def test_invalid_dates_and_format_do_not_schedule(self):
        for value in ("2026-02-29 10:00","2026-10-17 24:00","2026-10-17 10:60","17.10.2026 10:00","завтра"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                r.parse_reminder(value,NOW,timezone(timedelta(hours=5)))
        self.write("- [ ] Неверная дата 🔔 2026-02-29 10:00\n- [ ] Нет времени 🔔 завтра\n")
        report = self.engine.run_once(now=NOW)
        self.assertEqual(report["counts"]["invalid"],2)
        self.assertEqual(self.sent,[])

    def test_fallback_without_windows_tzdata(self):
        with mock.patch.object(r,"ZoneInfo",side_effect=r.ZoneInfoNotFoundError):
            self.assertEqual(r.user_timezone("Asia/Qyzylorda").utcoffset(NOW),timedelta(hours=5))
            with self.assertRaises(r.EngineError):
                r.user_timezone("Unknown/Zone")

    def test_fences_bom_crlf_and_non_task_content_are_preserved(self):
        original=("# Моя заметка\r\n\r\n```markdown\r\n- [ ] Пример 🔔 2026-10-17 09:01\r\n```\r\n"
                  "~~~md\r\n- [x] Тоже пример\r\n~~~\r\nТекст  \r\n"
                  "- [ ] Настоящее дело 🔔 2026-10-17 09:01  \r\n"
                  "  Продолжение без отдельного дела\r\n- [ ] Без времени\r\n")
        self.taskfile.write_bytes(b"\xef\xbb\xbf"+original.encode("utf-8"))
        self.engine.sync_tasks(now=NOW)
        first=self.taskfile.read_bytes()
        self.assertTrue(first.startswith(b"\xef\xbb\xbf"))
        decoded=first.decode("utf-8-sig")
        self.assertIn("```markdown\r\n- [ ] Пример 🔔 2026-10-17 09:01\r\n```",decoded)
        self.assertIn("~~~md\r\n- [x] Тоже пример\r\n~~~",decoded)
        self.assertIn("Текст  \r\n",decoded)
        self.assertNotRegex(decoded,r"(?<!\r)\n")
        self.assertEqual(decoded.count("<!-- obs-task:"),2)
        self.assertEqual(len(self.rows()),2)
        self.engine.sync_tasks(now=NOW+timedelta(seconds=20))
        self.assertEqual(self.taskfile.read_bytes(),first)

    def test_duplicate_copied_marker_receives_new_stable_id(self):
        self.due_task()
        first_id=self.rows()[0]["task_id"]
        saved=self.taskfile.read_text(encoding="utf-8")
        self.write(saved+saved.replace("Проверить backup","Второе дело"))
        self.engine.sync_tasks(now=NOW)
        ids={item["task_id"] for item in self.rows()}
        self.assertEqual(len(ids),2)
        self.assertIn(first_id,ids)
        after=self.taskfile.read_bytes()
        self.engine.sync_tasks(now=NOW)
        self.assertEqual(self.taskfile.read_bytes(),after)

    def test_wrapped_continuation_does_not_become_a_reminder(self):
        self.write("- [ ] Ручной перенос\n  🔔 2026-10-17 09:01\n")
        self.engine.sync_tasks(now=NOW)
        self.assertEqual(self.rows()[0]["state"],"unscheduled")

    def test_list_and_blockquote_fences_are_not_tasks(self):
        self.write("- ```md\n  - [ ] Пример внутри списка\n  ```\n> ~~~\n- [ ] Пример внутри цитаты\n> ~~~\n- [ ] Реальная задача\n")
        self.engine.sync_tasks(now=NOW)
        self.assertEqual(len(self.rows()),1)
        self.assertEqual(self.taskfile.read_text(encoding="utf-8").count("<!-- obs-task:"),1)

    def test_no_credentials_still_persists_tasks_and_status(self):
        self.due_task()
        report=self.engine.run_once(now=NOW+timedelta(minutes=2))
        self.assertEqual(report["error"],"telegram_not_configured")
        self.assertEqual(report["counts"]["pending"],1)
        self.assertEqual(self.sent,[])
        self.assertTrue(self.engine.status_path.is_file())
        self.authorize()
        report=self.engine.run_once(now=NOW+timedelta(minutes=3))
        self.assertEqual(report["sent_this_run"],1)
        self.assertIn("позже",self.sent[0][1])

    def test_new_past_tasks_do_not_send_on_first_capture_or_restart(self):
        self.authorize()
        self.write("- [ ] Старое дело 🔔 2026-10-16 09:00\n")
        for engine in (self.engine,self.new_engine()):
            report=engine.run_once(now=NOW)
            self.assertEqual(report["counts"]["past_needs_time"],1)
        self.assertEqual(self.sent,[])
        self.write(self.taskfile.read_text(encoding="utf-8").replace("2026-10-16 09:00","2026-10-17 09:01"))
        self.engine.run_once(now=NOW)
        self.engine.run_once(now=NOW+timedelta(minutes=1))
        self.assertEqual(len(self.sent),1)

    def test_newly_discovered_reminder_within_polling_grace_is_delivered(self):
        self.authorize()
        self.write("- [ ] Новое дело 🔔 2026-10-17 09:01\n")
        report=self.engine.run_once(now=NOW+timedelta(minutes=1,seconds=5))
        self.assertEqual(report["sent_this_run"],1)
        self.assertEqual(report["counts"]["past_needs_time"],0)
        self.new_engine().run_once(now=NOW+timedelta(minutes=1,seconds=25))
        self.assertEqual(len(self.sent),1)

    def test_grace_does_not_create_a_storm_from_older_past_tasks(self):
        self.authorize()
        self.write("- [ ] Пропущенное дело 🔔 2026-10-17 09:00\n")
        report=self.engine.run_once(now=NOW+timedelta(seconds=61))
        self.assertEqual(report["counts"]["past_needs_time"],1)
        self.assertEqual(self.sent,[])

    def test_checked_cancels_before_due(self):
        due=self.due_task()
        self.authorize()
        self.write(self.taskfile.read_text(encoding="utf-8").replace("[ ]","[x]"))
        report=self.engine.run_once(now=due+timedelta(seconds=5))
        self.assertEqual(report["counts"]["completed"],1)
        self.assertEqual(self.sent,[])

    def test_deleted_cancels_before_due(self):
        due=self.due_task()
        self.authorize()
        self.write("# Все дела удалены\n")
        report=self.engine.run_once(now=due+timedelta(seconds=5))
        self.assertEqual(report["counts"]["deleted"],1)
        self.assertEqual(self.sent,[])

    def test_removed_bell_unschedules(self):
        due=self.due_task()
        self.authorize()
        self.write(self.taskfile.read_text(encoding="utf-8").replace(" 🔔 2026-10-17 09:01",""))
        report=self.engine.run_once(now=due)
        self.assertEqual(report["counts"]["unscheduled"],1)
        self.assertEqual(self.sent,[])

    def test_no_duplicates_on_repeat_restart_or_text_edit(self):
        due=self.due_task()
        self.authorize()
        task_id=self.rows()[0]["task_id"]
        self.engine.run_once(now=due)
        self.new_engine().run_once(now=due+timedelta(seconds=30))
        self.write(self.taskfile.read_text(encoding="utf-8").replace("Проверить backup","Уточнённый текст"))
        report=self.new_engine().run_once(now=due+timedelta(minutes=1))
        self.assertEqual(len(self.sent),1)
        self.assertEqual(report["counts"]["sent"],1)
        self.assertEqual(self.rows()[0]["task_id"],task_id)
        self.assertIn("Уточнённый",self.rows()[0]["text"])

    def test_new_time_reschedules_even_after_delivery(self):
        due=self.due_task()
        self.authorize()
        self.engine.run_once(now=due)
        self.write(self.taskfile.read_text(encoding="utf-8").replace("09:01","09:03"))
        self.engine.run_once(now=due+timedelta(seconds=10))
        self.assertEqual(len(self.sent),1)
        self.assertEqual(self.rows()[0]["schedule_version"],2)
        self.engine.run_once(now=NOW+timedelta(minutes=3))
        self.engine.run_once(now=NOW+timedelta(minutes=4))
        self.assertEqual(len(self.sent),2)

    def test_relative_anchor_is_not_recomputed_after_midnight(self):
        self.authorize()
        self.write("- [ ] Относительная дата 🔔 сегодня 10:00\n")
        self.engine.run_once(now=NOW)
        original=self.rows()[0]["due_utc"]
        tomorrow=NOW+timedelta(days=1)
        report=self.new_engine().run_once(now=tomorrow)
        self.assertEqual(self.rows()[0]["due_utc"],original)
        self.assertEqual(report["sent_this_run"],1)
        self.assertIn("позже",self.sent[0][1])
        self.new_engine().run_once(now=tomorrow+timedelta(minutes=1))
        self.assertEqual(len(self.sent),1)

    def test_relative_anchor_survives_changing_expression_and_changing_back(self):
        self.write("- [ ] Дело 🔔 сегодня 10:00\n")
        self.engine.sync_tasks(now=NOW)
        original=self.rows()[0]["due_utc"]
        self.write(self.taskfile.read_text(encoding="utf-8").replace("10:00","11:00"))
        self.engine.sync_tasks(now=NOW)
        self.write(self.taskfile.read_text(encoding="utf-8").replace("11:00","10:00"))
        self.new_engine().sync_tasks(now=NOW+timedelta(days=1))
        self.assertEqual(self.rows()[0]["due_utc"],original)
        self.assertEqual(self.rows()[0]["state"],"past_needs_time")

    def test_retry_is_persistent_and_not_before_one_minute(self):
        due=self.due_task()
        self.authorize()
        attempts=[]
        def failure(token,chat_id,text):
            attempts.append(1)
            raise r.DeliveryError("network_error")
        report=self.new_engine(sender=failure).run_once(now=due)
        self.assertEqual(report["failed_this_run"],1)
        self.assertEqual(self.rows()[0]["state"],"pending")
        self.new_engine().run_once(now=due+timedelta(seconds=59))
        self.assertEqual(self.sent,[])
        self.new_engine().run_once(now=due+timedelta(seconds=60))
        self.assertEqual(len(self.sent),1)
        self.assertEqual(len(attempts),1)

    def test_unknown_transport_exception_does_not_leak_private_content(self):
        due=self.due_task()
        self.authorize()
        def failure(*unused):
            raise RuntimeError("PRIVATE_CONTENT_MUST_NOT_BE_SAVED")
        output=io.StringIO()
        with contextlib.redirect_stdout(output),contextlib.redirect_stderr(output):
            report=self.new_engine(sender=failure).run_once(now=due)
        self.assertEqual(report["error"],"delivery_error")
        self.assertNotIn("PRIVATE_CONTENT",self.engine.status_path.read_text(encoding="utf-8"))
        self.assertEqual(output.getvalue(),"")
        self.assertEqual(self.rows()[0]["last_error"],"delivery_error")

    def test_transport_sanitizes_network_exception_without_a_live_request(self):
        private="PRIVATE_URL_MUST_NOT_LEAK"
        with mock.patch.object(r.urllib.request,"urlopen",side_effect=r.urllib.error.URLError(private)) as opened:
            with self.assertRaises(r.DeliveryError) as caught:
                r.telegram_send("offline-test-value",1234,"Offline message")
        self.assertEqual(str(caught.exception),"network_error")
        request=opened.call_args.args[0]
        self.assertTrue(request.full_url.endswith("/sendMessage"))
        self.assertEqual(request.method,"POST")
        self.assertNotIn(private,str(caught.exception))

    def test_crashed_lease_recovers_only_after_expiry(self):
        due=self.due_task()
        self.authorize()
        row=self.engine._claim_next(due)
        self.assertIsNotNone(row)
        self.new_engine().run_once(now=due+timedelta(seconds=r.LEASE_SECONDS-1))
        self.assertEqual(self.sent,[])
        self.new_engine().run_once(now=due+timedelta(seconds=r.LEASE_SECONDS+1))
        self.assertEqual(len(self.sent),1)
        self.assertEqual(self.rows()[0]["state"],"sent")

    def test_cancellation_after_claim_is_rechecked_before_send(self):
        due=self.due_task()
        self.authorize()
        original=self.engine._claim_next
        changed=False
        def claim(now):
            nonlocal changed
            row=original(now)
            if row and not changed:
                self.write(self.taskfile.read_text(encoding="utf-8").replace("[ ]","[x]"))
                changed=True
            return row
        with mock.patch.object(self.engine,"_claim_next",side_effect=claim):
            report=self.engine.run_once(now=due)
        self.assertEqual(report["counts"]["completed"],1)
        self.assertEqual(self.sent,[])

    def test_missing_tasks_file_never_delivers_stale_queue(self):
        due=self.due_task()
        self.authorize()
        self.taskfile.unlink()
        report=self.engine.run_once(now=due)
        self.assertEqual(report["error"],"tasks_file_missing")
        self.assertEqual(self.sent,[])

    def test_status_has_counts_not_task_text(self):
        self.due_task()
        report=self.engine.status()
        self.assertEqual(report["counts"]["pending"],1)
        self.assertEqual(report["next_due"],"2026-10-17T04:01:00+00:00")
        self.assertNotIn("backup",json.dumps(report))

    def test_os_lock_blocks_parallel_process_and_stale_file_does_not(self):
        code=("from pathlib import Path; import sys; from reminders import WorkerLock,AlreadyRunning\n"
              "try:\n with WorkerLock(Path(sys.argv[1])): print('free')\n"
              "except AlreadyRunning: print('busy')\n")
        command=[sys.executable,"-B","-X","utf8","-c",code,str(self.engine.lock_path)]
        with r.WorkerLock(self.engine.lock_path):
            run=subprocess.run(command,cwd=MODULE.parent,text=True,capture_output=True,timeout=10)
            self.assertEqual(run.returncode,0,run.stderr)
            self.assertEqual(run.stdout.strip(),"busy")
            with self.assertRaises(r.AlreadyRunning):
                self.engine.run_once(now=NOW)
        self.assertTrue(self.engine.lock_path.is_file())
        run=subprocess.run(command,cwd=MODULE.parent,text=True,capture_output=True,timeout=10)
        self.assertEqual(run.returncode,0,run.stderr)
        self.assertEqual(run.stdout.strip(),"free")

    def test_repeat_default_is_two_hours_and_zero_is_explicit(self):
        self.assertEqual(self.engine.config.repeat_seconds,0)
        values=json.loads(self.config.read_text(encoding="utf-8"))
        values.pop("repeat_seconds")
        self.config.write_text(json.dumps(values),encoding="utf-8")
        self.assertEqual(r.Config.read(self.config).repeat_seconds,7200)

    def test_repeat_boundaries_119_120_240_minutes_and_same_tick(self):
        self.enable_repeats()
        due=self.due_task()
        self.authorize()
        self.engine.run_once(now=due)
        self.assertEqual(self.rows()[0]["due_utc"],due.timestamp())
        self.assertEqual(self.rows()[0]["reminder_count"],1)
        self.engine.run_once(now=due+timedelta(minutes=119))
        self.assertEqual(len(self.sent),1)
        self.engine.run_once(now=due+timedelta(minutes=120))
        self.engine.run_once(now=due+timedelta(minutes=120))
        self.assertEqual(len(self.sent),2)
        self.engine.run_once(now=due+timedelta(minutes=240))
        self.engine.run_once(now=due+timedelta(minutes=240))
        self.assertEqual(len(self.sent),3)
        self.assertEqual(self.rows()[0]["reminder_count"],3)
        self.assertEqual(self.rows()[0]["next_reminder_utc"],(due+timedelta(minutes=360)).timestamp())
        self.assertIn("Повторное",self.sent[1][1])
        self.assertIn("Срок задачи: 17.10.2026 09:01",self.sent[1][1])
        self.assertIn("через 2 часа",self.sent[1][1])
        self.assertNotIn("позже",self.sent[1][1])

    def test_repeat_after_sleep_sends_once_and_anchors_from_delivery(self):
        self.enable_repeats()
        due=self.due_task()
        self.authorize()
        self.engine.run_once(now=due)
        wake=due+timedelta(hours=8)
        report=self.new_engine().run_once(now=wake)
        self.assertEqual(report["sent_this_run"],1)
        self.assertEqual(len(self.sent),2)
        self.assertEqual(self.rows()[0]["next_reminder_utc"],(wake+timedelta(hours=2)).timestamp())
        self.assertIn("позже",self.sent[-1][1])
        self.new_engine().run_once(now=wake+timedelta(seconds=1))
        self.assertEqual(len(self.sent),2)

    def test_repeat_restart_and_text_edit_preserve_next_occurrence(self):
        self.enable_repeats()
        due=self.due_task()
        self.authorize()
        self.engine.run_once(now=due)
        task_id=self.rows()[0]["task_id"]
        self.write(self.taskfile.read_text(encoding="utf-8").replace("Проверить backup","Проверить копию и отчёт"))
        self.new_engine().run_once(now=due+timedelta(minutes=119))
        self.assertEqual(len(self.sent),1)
        self.assertEqual(self.rows()[0]["task_id"],task_id)
        self.assertEqual(self.rows()[0]["next_reminder_utc"],(due+timedelta(hours=2)).timestamp())
        self.new_engine().run_once(now=due+timedelta(hours=2))
        self.assertEqual(len(self.sent),2)
        self.assertIn("копию и отчёт",self.sent[-1][1])

    def test_repeat_checked_after_first_delivery_cancels_all_future(self):
        self.enable_repeats()
        due=self.due_task()
        self.authorize()
        self.engine.run_once(now=due)
        self.write(self.taskfile.read_text(encoding="utf-8").replace("[ ]","[x]"))
        self.new_engine().run_once(now=due+timedelta(minutes=1))
        self.new_engine().run_once(now=due+timedelta(hours=6))
        row=self.rows()[0]
        self.assertEqual(row["state"],"completed")
        self.assertIsNone(row["next_reminder_utc"])
        self.assertEqual(row["reminder_count"],1)
        self.assertEqual(len(self.sent),1)

    def test_repeat_deletion_after_first_delivery_cancels_all_future(self):
        self.enable_repeats()
        due=self.due_task()
        self.authorize()
        self.engine.run_once(now=due)
        self.write("# Задачи\n")
        report=self.new_engine().run_once(now=due+timedelta(hours=6))
        self.assertEqual(report["counts"]["deleted"],1)
        self.assertIsNone(report["next_due"])
        with self.engine._connection() as connection:
            self.assertIsNone(connection.execute("SELECT next_reminder_utc FROM tasks").fetchone()[0])
        self.assertEqual(len(self.sent),1)

    def test_repeat_schedule_edit_resets_count_and_first_delivery(self):
        self.enable_repeats()
        due=self.due_task()
        self.authorize()
        self.engine.run_once(now=due)
        self.write(self.taskfile.read_text(encoding="utf-8").replace("09:01","12:00"))
        self.engine.run_once(now=due+timedelta(minutes=1))
        row=self.rows()[0]
        new_due=NOW+timedelta(hours=3)
        self.assertEqual(row["reminder_count"],0)
        self.assertIsNone(row["delivered_utc"])
        self.assertEqual(row["next_reminder_utc"],new_due.timestamp())
        self.assertEqual(row["due_utc"],new_due.timestamp())
        self.assertEqual(row["schedule_version"],2)
        self.engine.run_once(now=due+timedelta(hours=2))
        self.assertEqual(len(self.sent),1)
        self.engine.run_once(now=new_due)
        self.assertEqual(len(self.sent),2)
        self.assertEqual(self.rows()[0]["reminder_count"],1)
        self.assertNotIn("Повторное",self.sent[-1][1])

    def test_repeat_network_retry_advances_only_after_success(self):
        self.enable_repeats()
        due=self.due_task()
        self.authorize()
        self.engine.run_once(now=due)
        repeat=due+timedelta(hours=2)
        def fail(*unused):
            raise r.DeliveryError("network_error")
        report=self.new_engine(sender=fail).run_once(now=repeat)
        row=self.rows()[0]
        self.assertEqual(report["failed_this_run"],1)
        self.assertEqual(row["reminder_count"],1)
        self.assertEqual(row["delivered_utc"],due.timestamp())
        self.assertEqual(row["next_reminder_utc"],repeat.timestamp())
        self.assertEqual(report["next_due"],r.iso(repeat+timedelta(minutes=1)))
        self.new_engine().run_once(now=repeat+timedelta(seconds=59))
        self.assertEqual(len(self.sent),1)
        self.new_engine().run_once(now=repeat+timedelta(seconds=60))
        self.assertEqual(len(self.sent),2)
        self.assertEqual(self.rows()[0]["next_reminder_utc"],(repeat+timedelta(seconds=60,hours=2)).timestamp())

    def test_repeat_lease_recovery_retains_counter_and_new_period(self):
        self.enable_repeats()
        due=self.due_task()
        self.authorize()
        self.engine.run_once(now=due)
        repeat=due+timedelta(hours=2)
        self.engine._claim_next(repeat)
        self.new_engine().run_once(now=repeat+timedelta(seconds=r.LEASE_SECONDS-1))
        self.assertEqual(len(self.sent),1)
        recovered=repeat+timedelta(seconds=r.LEASE_SECONDS+1)
        self.new_engine().run_once(now=recovered)
        self.assertEqual(len(self.sent),2)
        self.assertEqual(self.rows()[0]["reminder_count"],2)
        self.assertEqual(self.rows()[0]["next_reminder_utc"],(recovered+timedelta(hours=2)).timestamp())

    def test_migration_of_legacy_sent_rows_schedules_delivered_plus_two_hours(self):
        due=self.due_task()
        self.authorize()
        self.engine.run_once(now=due)
        old=self.rows()[0]
        self.recreate_legacy_table([old])
        self.enable_repeats()
        row=self.rows()[0]
        self.assertEqual(row["task_id"],old["task_id"])
        self.assertEqual(row["due_utc"],old["due_utc"])
        self.assertEqual(row["armed"],old["armed"])
        self.assertEqual(row["delivered_version"],old["delivered_version"])
        self.assertEqual(row["reminder_count"],1)
        self.assertEqual(row["next_reminder_utc"],(due+timedelta(hours=2)).timestamp())
        self.new_engine().run_once(now=due+timedelta(hours=2,seconds=-1))
        self.assertEqual(len(self.sent),1)
        self.new_engine().run_once(now=due+timedelta(hours=2))
        self.new_engine().run_once(now=due+timedelta(hours=2))
        self.assertEqual(len(self.sent),2)

    def test_migration_preserves_legacy_pending_and_unexpired_lease(self):
        self.write("- [ ] Первое 🔔 2026-10-17 09:01\n- [ ] Второе 🔔 2026-10-17 09:01\n")
        self.engine.sync_tasks(now=NOW)
        legacy=self.rows()
        due=NOW+timedelta(minutes=1)
        legacy[1].update(state="leased",lease_owner="old-worker",lease_until=(due+timedelta(seconds=120)).timestamp())
        self.recreate_legacy_table(legacy)
        self.enable_repeats()
        self.authorize()
        migrated=self.rows()
        self.assertEqual({row["task_id"] for row in migrated},{row["task_id"] for row in legacy})
        self.assertTrue(all(row["reminder_count"]==0 and row["next_reminder_utc"]==due.timestamp() for row in migrated))
        self.engine.run_once(now=due)
        self.assertEqual(len(self.sent),1)
        self.new_engine().run_once(now=due+timedelta(seconds=121))
        self.assertEqual(len(self.sent),2)
        self.assertTrue(all(row["reminder_count"]==1 for row in self.rows()))

    def test_repeat_can_be_disabled_and_reenabled_without_catchup_burst(self):
        self.enable_repeats()
        due=self.due_task()
        self.authorize()
        self.engine.run_once(now=due)
        self.enable_repeats(0).run_once(now=due+timedelta(hours=4))
        self.assertEqual(len(self.sent),1)
        self.assertIsNone(self.rows()[0]["next_reminder_utc"])
        moment=due+timedelta(hours=5)
        self.enable_repeats().run_once(now=moment)
        self.engine.run_once(now=moment)
        self.assertEqual(len(self.sent),2)
        self.assertEqual(self.rows()[0]["next_reminder_utc"],(moment+timedelta(hours=2)).timestamp())

    def test_repeat_schedule_edit_during_delivery_does_not_overwrite_new_version(self):
        self.enable_repeats()
        due=self.due_task()
        self.authorize()
        def edit_during_send(token,chat_id,text):
            self.sent.append((chat_id,text))
            self.write(self.taskfile.read_text(encoding="utf-8").replace("09:01","12:00"))
            self.engine._sync_tasks(due)
        self.engine.sender=edit_during_send
        self.engine.run_once(now=due)
        row=self.rows()[0]
        self.assertEqual(row["schedule_version"],2)
        self.assertEqual(row["reminder_count"],0)
        self.assertIsNone(row["delivered_utc"])
        self.assertEqual(row["next_reminder_utc"],(NOW+timedelta(hours=3)).timestamp())

    def test_completion_changes_only_target_checkbox_and_preserves_bom_crlf(self):
        self.enable_repeats()
        initial="# Задачи\r\n- [ ] Целевая задача 🔔 2026-10-17 09:01\r\n- [ ] Другая задача 🔔 2026-10-17 10:00\r\n"
        self.taskfile.write_bytes(b"\xef\xbb\xbf"+initial.encode("utf-8"))
        self.engine.sync_tasks(now=NOW)
        target=next(row for row in self.rows() if row["text"]=="Целевая задача")
        # A later new task and a code example must not be modified by a callback.
        suffix=(f"```md\r\n- [ ] Пример <!-- obs-task:{target['task_id']} -->\r\n```\r\n"
                "- [ ] Ещё не синхронизированная задача\r\nРучной текст  \r\n")
        with self.taskfile.open("ab") as handle:
            handle.write(suffix.encode("utf-8"))
        before=self.taskfile.read_bytes()
        result=self.engine.complete_task(target["task_id"],target["schedule_version"])
        self.assertEqual(result,{"status":"completed"})
        expected=before.replace("- [ ] Целевая задача".encode("utf-8"),"- [x] Целевая задача".encode("utf-8"),1)
        self.assertEqual(self.taskfile.read_bytes(),expected)
        current=next(row for row in self.rows() if row["task_id"]==target["task_id"])
        self.assertEqual(current["state"],"completed")
        self.assertIsNone(current["next_reminder_utc"])

    def test_completion_after_first_reminder_is_idempotent_and_stops_repeat(self):
        self.enable_repeats()
        due=self.due_task()
        self.authorize()
        self.engine.run_once(now=due)
        row=self.rows()[0]
        self.assertEqual(self.engine.complete_task(row["task_id"],row["schedule_version"]),{"status":"completed"})
        after=self.taskfile.read_bytes()
        self.assertEqual(self.engine.complete_task(row["task_id"],row["schedule_version"]),{"status":"already_completed"})
        self.assertEqual(self.taskfile.read_bytes(),after)
        self.new_engine().run_once(now=due+timedelta(hours=8))
        self.assertEqual(len(self.sent),1)
        self.assertEqual(self.rows()[0]["state"],"completed")

    def test_completion_rejects_unsynced_and_synced_deadline_changes(self):
        self.due_task()
        row=self.rows()[0]
        self.write(self.taskfile.read_text(encoding="utf-8").replace("09:01","12:00"))
        before=self.taskfile.read_bytes()
        self.assertEqual(self.engine.complete_task(row["task_id"],row["schedule_version"]),{"status":"changed"})
        self.assertEqual(self.taskfile.read_bytes(),before)
        self.engine.sync_tasks(now=NOW)
        self.assertEqual(self.engine.complete_task(row["task_id"],row["schedule_version"]),{"status":"changed"})
        self.assertEqual(self.taskfile.read_bytes(),before)
        self.assertEqual(self.engine.complete_task(row["task_id"],2),{"status":"completed"})

    def test_completion_deleted_unknown_or_malformed_id_does_not_touch_other_tasks(self):
        self.due_task()
        row=self.rows()[0]
        self.write("- [ ] Другая задача без ID\n")
        before=self.taskfile.read_bytes()
        for identifier in (row["task_id"],str(r.uuid.uuid4()),"not-a-uuid"):
            with self.subTest(identifier=identifier):
                self.assertEqual(self.engine.complete_task(identifier,1),{"status":"not_found"})
                self.assertEqual(self.taskfile.read_bytes(),before)

    def test_completion_rejects_duplicate_uuid_conflict(self):
        self.due_task()
        row=self.rows()[0]
        content=self.taskfile.read_text(encoding="utf-8")
        self.write(content+content.replace("Проверить backup","Скопированная задача"))
        before=self.taskfile.read_bytes()
        self.assertEqual(self.engine.complete_task(row["task_id"],1),{"status":"changed"})
        self.assertEqual(self.taskfile.read_bytes(),before)

    def test_completion_does_not_mark_fenced_example_as_real_task(self):
        self.due_task()
        row=self.rows()[0]
        self.write(f"```md\n- [ ] Только пример <!-- obs-task:{row['task_id']} -->\n```\n")
        before=self.taskfile.read_bytes()
        self.assertEqual(self.engine.complete_task(row["task_id"],1),{"status":"not_found"})
        self.assertEqual(self.taskfile.read_bytes(),before)

    def test_completion_os_lock_busy_and_held_lock_service_contract(self):
        self.due_task()
        row=self.rows()[0]
        with r.WorkerLock(self.engine.lock_path):
            self.assertEqual(self.engine.complete_task(row["task_id"],1),{"status":"busy"})
            self.assertEqual(self.engine.complete_task(row["task_id"],1,_lock_held=True),{"status":"completed"})

    def test_completion_storage_busy_is_safe_and_preserves_file(self):
        self.due_task()
        row=self.rows()[0]
        import markdown_store
        before=self.taskfile.read_bytes()
        with mock.patch.object(markdown_store,"update_markdown",side_effect=markdown_store.MarkdownBusy()):
            self.assertEqual(self.engine.complete_task(row["task_id"],1),{"status":"busy"})
        self.assertEqual(self.taskfile.read_bytes(),before)

    def test_default_sender_receives_completion_button_without_changing_injected_contract(self):
        due=self.due_task()
        self.authorize()
        with mock.patch.object(r,"telegram_send") as send:
            default=r.TaskEngine(self.config,token_loader=lambda unused:"offline-test-value")
            report=default.run_once(now=due)
        self.assertEqual(report["sent_this_run"],1)
        self.assertEqual(send.call_count,1)
        markup=send.call_args.kwargs["reply_markup"]
        button=markup["inline_keyboard"][0][0]
        self.assertEqual(button["text"],"✅ Выполнено")
        self.assertRegex(button["callback_data"],r"^done:[0-9a-f-]{36}:1$")
        self.assertLessEqual(len(button["callback_data"].encode("utf-8")),64)

    def test_send_message_serializes_optional_button_without_live_network(self):
        markup={"inline_keyboard":[[{"text":"✅ Выполнено","callback_data":"done:offline:1"}]]}
        response=mock.MagicMock()
        response.__enter__.return_value.read.return_value=b'{"ok":true,"result":{"message_id":1}}'
        with mock.patch.object(r.urllib.request,"urlopen",return_value=response) as opened:
            r.telegram_send("offline-test-value",1234,"Offline",reply_markup=markup)
        payload=json.loads(opened.call_args.args[0].data)
        self.assertEqual(payload["reply_markup"],markup)

    def test_incompatible_default_sender_does_not_fallback_and_send_twice(self):
        due=self.due_task()
        self.authorize()
        with mock.patch.object(r,"telegram_send",side_effect=TypeError("incompatible")) as send:
            default=r.TaskEngine(self.config,token_loader=lambda unused:"offline-test-value")
            report=default.run_once(now=due)
        self.assertEqual(send.call_count,1)
        self.assertEqual(report["error"],"delivery_error")
        self.assertEqual(self.rows()[0]["reminder_count"],0)

    def test_delivery_budget_one_leaves_other_due_tasks_for_next_control_cycle(self):
        self.write("".join(f"- [ ] Дело {index} 🔔 2026-10-17 09:01\n" for index in range(3)))
        self.engine.sync_tasks(now=NOW)
        self.authorize()
        due=NOW+timedelta(minutes=1)
        for count in (1,2,3):
            with r.WorkerLock(self.engine.lock_path):
                report=self.engine.run_once(now=due,max_deliveries=1,_lock_held=True)
            self.assertEqual(report["sent_this_run"],1)
            self.assertEqual(len(self.sent),count)
        self.assertEqual(self.engine.run_once(now=due,max_deliveries=1)["sent_this_run"],0)

    def test_zero_delivery_budget_syncs_cancellation_without_transport(self):
        due=self.due_task()
        self.authorize()
        self.write(self.taskfile.read_text(encoding="utf-8").replace("[ ]","[x]"))
        report=self.engine.run_once(now=due,max_deliveries=0)
        self.assertEqual(self.sent,[])
        self.assertEqual(report["counts"]["completed"],1)
        self.assertIsNone(report["next_due"])

    def test_delivery_limit_rejects_invalid_values_before_mutating_files(self):
        self.write("- [ ] Нет ID 🔔 завтра 10:00\n")
        original=self.taskfile.read_bytes()
        for limit in (-1,True,1.5,"1",None):
            with self.subTest(limit=limit),self.assertRaisesRegex(r.EngineError,"^delivery_limit_invalid$"):
                self.engine.run_once(now=NOW,max_deliveries=limit)
        self.assertEqual(self.taskfile.read_bytes(),original)
        self.assertEqual(self.rows(),[])

    def test_default_delivery_budget_retains_legacy_fifty_limit(self):
        self.write("".join(f"- [ ] Дело {index} 🔔 2026-10-17 09:01\n" for index in range(51)))
        self.engine.sync_tasks(now=NOW)
        self.authorize()
        report=self.engine.run_once(now=NOW+timedelta(minutes=1))
        self.assertEqual(report["sent_this_run"],50)
        self.assertEqual(len(self.sent),50)
        self.assertEqual(report["counts"]["pending"],1)

    def test_fresh_tasks_under_service_lock_reads_current_markdown_without_auth(self):
        self.due_task()
        self.write("- [ ] Новое дело 🔔 2026-10-17 12:00\n")
        with mock.patch.object(self.engine,"token_loader") as loader:
            with r.WorkerLock(self.engine.lock_path):
                rows=self.engine.fresh_tasks(now=NOW,_lock_held=True)
        loader.assert_not_called()
        self.assertEqual([row["text"] for row in rows],["Новое дело"])
        self.assertEqual(rows[0]["due_utc"],(NOW+timedelta(hours=3)).timestamp())
        self.assertIn("<!-- obs-task:",self.taskfile.read_text(encoding="utf-8"))
        self.assertEqual(self.sent,[])

    def test_fresh_tasks_propagates_busy_instead_of_returning_stale_db(self):
        self.due_task()
        import markdown_store
        self.write("- [ ] Изменение пока недоступно\n")
        with mock.patch.object(markdown_store,"update_markdown",side_effect=markdown_store.MarkdownBusy()):
            with r.WorkerLock(self.engine.lock_path):
                with self.assertRaisesRegex(r.EngineError,"^tasks_file_busy$"):
                    self.engine.fresh_tasks(now=NOW,_lock_held=True)
        self.assertEqual(self.rows()[0]["text"],"Проверить backup")
        self.assertEqual(self.sent,[])

    def test_fresh_tasks_keeps_original_deadline_separate_from_next_repeat(self):
        self.enable_repeats()
        due=self.due_task()
        self.authorize()
        self.engine.run_once(now=due,max_deliveries=1)
        rows=self.engine.fresh_tasks(now=due+timedelta(minutes=1))
        self.assertEqual(rows[0]["due_utc"],due.timestamp())
        self.assertEqual(rows[0]["next_reminder_utc"],(due+timedelta(hours=2)).timestamp())
        self.assertEqual(len(self.sent),1)

    def test_busy_presend_recheck_releases_unsent_claim_for_next_pass(self):
        due=self.due_task()
        self.authorize()
        real_sync=self.engine._sync_tasks
        calls=0
        def sync(moment,**kwargs):
            nonlocal calls
            calls+=1
            if calls==2:
                raise r.EngineError("tasks_file_busy")
            return real_sync(moment,**kwargs)
        with mock.patch.object(self.engine,"_sync_tasks",side_effect=sync):
            report=self.engine.run_once(now=due,max_deliveries=1)
        row=self.rows()[0]
        self.assertEqual(report["error"],"tasks_file_busy")
        self.assertEqual(row["state"],"pending")
        self.assertIsNone(row["lease_owner"])
        self.assertIsNone(row["lease_until"])
        self.assertEqual(self.sent,[])
        self.engine.run_once(now=due+timedelta(seconds=1),max_deliveries=1)
        self.assertEqual(len(self.sent),1)

    def test_releasing_failed_presend_claim_never_resurrects_cancellation(self):
        due=self.due_task()
        self.authorize()
        real_sync=self.engine._sync_tasks
        calls=0
        def sync(moment,**kwargs):
            nonlocal calls
            calls+=1
            if calls==2:
                self.write(self.taskfile.read_text(encoding="utf-8").replace("[ ]","[x]"))
                real_sync(moment,**kwargs)
                raise r.EngineError("tasks_file_busy")
            return real_sync(moment,**kwargs)
        with mock.patch.object(self.engine,"_sync_tasks",side_effect=sync):
            self.engine.run_once(now=due,max_deliveries=1)
        self.assertEqual(self.rows()[0]["state"],"completed")
        self.assertIsNone(self.rows()[0]["next_reminder_utc"])
        self.assertEqual(self.sent,[])

    def test_retry_waits_one_minute_from_network_failure_not_request_start(self):
        due=self.due_task()
        self.authorize()
        clock=[due]
        calls=[]
        def sender(token,chat_id,text):
            calls.append(clock[0])
            clock[0]+=timedelta(seconds=5)
            raise r.DeliveryError("network_error")
        self.engine.sender=sender
        with mock.patch.object(r,"utc_now",side_effect=lambda:clock[0]):
            report=self.engine.run_once(max_deliveries=1)
            self.assertEqual(self.rows()[0]["retry_at"],(due+timedelta(seconds=65)).timestamp())
            self.assertEqual(report["last_run"],r.iso(due+timedelta(seconds=5)))
            clock[0]=due+timedelta(seconds=64)
            self.engine.run_once(max_deliveries=1)
        self.assertEqual(len(calls),1)

    def test_status_next_delivery_accounts_for_active_lease(self):
        due=self.due_task()
        self.engine._claim_next(due)
        report=self.engine.status()
        self.assertEqual(report["next_due"],r.iso(due+timedelta(seconds=r.LEASE_SECONDS)))
        self.assertEqual(self.rows()[0]["due_utc"],due.timestamp())

    def test_astral_emoji_message_is_bounded_and_deadline_footer_survives(self):
        self.enable_repeats()
        self.write("- [ ] "+"😀"*5000+" 🔔 2026-10-17 09:01\n")
        self.engine.sync_tasks(now=NOW)
        text=self.engine._message(self.rows()[0],NOW+timedelta(minutes=1))
        self.assertLessEqual(len(text.encode("utf-16-le"))//2,4096)
        self.assertIn("Срок задачи: 17.10.2026 09:01",text)
        self.assertIn("через 2 часа",text)
        self.assertEqual(r.truncate_telegram_text("A😀B",2),"A")
        self.assertEqual(r.truncate_telegram_text("A😀B",3),"A😀")

    def test_transport_caps_unicode_payload_and_uses_short_control_latency_timeout(self):
        response=mock.MagicMock()
        response.__enter__.return_value.read.return_value=b'{"ok":true}'
        with mock.patch.object(r.urllib.request,"urlopen",return_value=response) as opened:
            r.telegram_send("offline-test-value",1234,"😀"*3000)
        payload=json.loads(opened.call_args.args[0].data)
        self.assertEqual(payload["text"],"😀"*2048)
        self.assertEqual(opened.call_args.kwargs["timeout"],5)


if __name__ == "__main__":
    unittest.main(verbosity=2)
