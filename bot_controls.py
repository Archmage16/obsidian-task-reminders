"""Owner-only Telegram controls; this module never reads or writes a vault.

The embedding service owns the worker lock. Network delivery is at least once:
a crash after Telegram accepts a test message but before SQLite records success
can repeat that message after restart. Callback effects are durably claimed before
execution, so replay does not repeat them; a failed action asks the owner to click
again. Only public bot IDs, update/callback IDs and test-job metadata are stored.
"""
from __future__ import annotations

import datetime as dt
from contextlib import contextmanager
import json
import math
from pathlib import Path
import re
import sqlite3
import time
import urllib.error
import urllib.request
from typing import Callable
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


_TOKEN = re.compile(r"^[1-9][0-9]*:[A-Za-z0-9_-]+$")
_DONE = re.compile(r"^done:([0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}):([0-9]{1,18})$")
_VIEW = re.compile(r"^list:(tasks|deadlines):(active|completed|all):([0-9]{1,9})$")
_METHODS = frozenset({"getUpdates", "sendMessage", "answerCallbackQuery"})
try:
    _ASTANA = ZoneInfo("Asia/Qyzylorda")
except ZoneInfoNotFoundError:
    _ASTANA = dt.timezone(dt.timedelta(hours=5))
_COUNT_NAMES = ("menus_sent", "pings_sent", "status_sent", "minute_jobs_scheduled",
                "tests_sent", "completed_actions", "views_sent", "ignored", "duplicates")


class ControlsError(Exception):
    """A deliberately terse error with no request URL, response or credentials."""


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def telegram_transport(token: str, method: str, payload: dict, timeout: int = 5):
    """Fixed-host, fixed-method JSON transport. Never expose Telegram descriptions."""
    if not isinstance(token, str) or not _TOKEN.fullmatch(token) or method not in _METHODS:
        raise ControlsError("invalid_request")
    request = urllib.request.Request(
        "https://api.telegram.org/bot" + token + "/" + method,
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={"Content-Type": "application/json"}, method="POST")
    try:
        with urllib.request.build_opener(_NoRedirect()).open(request, timeout=timeout) as response:
            raw = response.read(2_000_001)
        if len(raw) > 2_000_000:
            raise ControlsError("invalid_response")
        result = json.loads(raw)
        if not isinstance(result, dict) or result.get("ok") is not True:
            raise ControlsError("telegram_unavailable")
        return result
    except urllib.error.HTTPError as exc:
        exc.close()
        raise ControlsError("telegram_unavailable") from None
    except ControlsError:
        raise
    except Exception:
        raise ControlsError("telegram_unavailable") from None


def _positive_id(value):
    if isinstance(value, bool):
        return None
    if isinstance(value, int) and value > 0:
        return value
    if isinstance(value, str) and re.fullmatch(r"[1-9][0-9]{0,19}", value):
        return int(value)
    return None


def _integer(value):
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else None


def _timestamp(value):
    try:
        number = float(value)
        return number if math.isfinite(number) and 0 <= number <= 253402300799 else None
    except (TypeError, ValueError, OverflowError):
        return None


def _time_label(value):
    try:
        if isinstance(value, str):
            date = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
            if date.tzinfo is None:
                return None
        else:
            stamp = _timestamp(value)
            if stamp is None:
                return None
            date = dt.datetime.fromtimestamp(stamp, dt.timezone.utc)
        return date.astimezone(_ASTANA).strftime("%d.%m %H:%M")
    except (ValueError, TypeError, OverflowError, OSError):
        return None


def _utf16_length(text):
    return len(text.encode("utf-16-le", errors="replace")) // 2


def _clean_text(value):
    if not isinstance(value, str):
        return "(Без названия)"
    value = value.encode("utf-8", errors="replace").decode("utf-8")
    value = value.replace("\r\n", "\n").replace("\r", "\n").replace("\t", " ")
    return "".join(ch for ch in value if ord(ch) >= 32 or ch == "\n").strip() or "(Без названия)"


def _text_chunks(text, budget):
    """Split on Python code points; an astral character consumes two UTF-16 units."""
    chunks, current, used = [], [], 0
    for char in text:
        size = 2 if ord(char) > 0xFFFF else 1
        if used + size > budget:
            chunks.append("".join(current))
            current, used = [], 0
        current.append(char)
        used += size
    if current:
        chunks.append("".join(current))
    return chunks or [""]


def _due_timestamp(value):
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, str):
        try:
            date = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
            return _timestamp(date.timestamp()) if date.tzinfo is not None else None
        except (ValueError, OverflowError, OSError):
            return None
    return _timestamp(value)


class BotControls:
    def __init__(self, data_dir, status_provider: Callable[[], dict], transport=None,
                 now=None, *, complete_task_handler=None, task_provider=None):
        self.data_dir = Path(data_dir)
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.db_path = self.data_dir / "controls.sqlite3"
        self.status_provider = status_provider
        self.transport = transport or telegram_transport
        self.clock = now if callable(now) else (lambda: now) if now is not None else time.time
        self.complete_task_handler = complete_task_handler
        self.task_provider = task_provider
        with self._db() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY, value TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS updates(id INTEGER PRIMARY KEY, handled REAL NOT NULL);
                CREATE TABLE IF NOT EXISTS callbacks(id TEXT PRIMARY KEY, handled REAL NOT NULL);
                CREATE TABLE IF NOT EXISTS cooldowns(action TEXT PRIMARY KEY, used REAL NOT NULL);
                CREATE TABLE IF NOT EXISTS counts(name TEXT PRIMARY KEY, value INTEGER NOT NULL);
                CREATE TABLE IF NOT EXISTS jobs(
                    id INTEGER PRIMARY KEY, chat_id INTEGER NOT NULL, due REAL NOT NULL,
                    state TEXT NOT NULL, generation INTEGER NOT NULL, attempts INTEGER NOT NULL DEFAULT 0);
            """)
            # Only one service holds the external worker lock, so an interrupted
            # send can be safely retried by the newly constructed controller.
            db.execute("UPDATE jobs SET state='pending' WHERE state='sending'")

    @contextmanager
    def _db(self):
        db = sqlite3.connect(self.db_path, timeout=2)
        try:
            db.row_factory = sqlite3.Row
            db.execute("PRAGMA journal_mode=WAL")
            db.execute("PRAGMA synchronous=FULL")
            with db:
                yield db
        finally:
            db.close()

    def _now(self, value=None):
        stamp = _timestamp(self.clock() if value is None else value)
        if stamp is None:
            raise ControlsError("invalid_time")
        return stamp

    @staticmethod
    def keyboard():
        return {"inline_keyboard": [
            [{"text": "📝 Задачи", "callback_data": "list:tasks:active:0"},
             {"text": "📅 Дедлайны", "callback_data": "list:deadlines:active:0"}],
            [{"text": "✅ Проверить связь", "callback_data": "test:ping"}],
            [{"text": "⏱ Тест через 1 минуту", "callback_data": "test:minute"},
             {"text": "📋 Статус", "callback_data": "test:status"}]]}

    @staticmethod
    def completion_keyboard(task_id, expected_version):
        data = f"done:{task_id}:{expected_version}"
        if len(data.encode("utf-8")) > 64 or not _DONE.fullmatch(data):
            raise ControlsError("invalid_callback")
        return {"inline_keyboard": [[{"text": "✅ Выполнено", "callback_data": data}]]}

    @staticmethod
    def _meta(db, key, default="0"):
        row = db.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        return row[0] if row else default

    @staticmethod
    def _set(db, key, value):
        db.execute("INSERT INTO meta(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                   (key, str(value)))

    @staticmethod
    def _count(db, name, number=1):
        db.execute("INSERT INTO counts(name,value) VALUES(?,?) ON CONFLICT(name) DO UPDATE SET value=value+excluded.value",
                   (name, number))

    def _scope(self, token, owner):
        # Token rotation for the same bot keeps its offset. No token is persisted.
        match = re.match(r"^([1-9][0-9]*):", token) if isinstance(token, str) else None
        if not match:
            raise ControlsError("invalid_bot_identity")
        identity = match[1]
        with self._db() as db:
            old = self._meta(db, "bot_id", "")
            previous_owner = self._meta(db, "owner", "")
            if old and old != identity:
                for table in ("updates", "callbacks", "cooldowns", "counts"):
                    db.execute("DELETE FROM " + table)
                self._set(db, "offset", 0)
                self._set(db, "last_poll", "")
            if (old and old != identity) or (previous_owner and previous_owner != str(owner)):
                db.execute("UPDATE jobs SET state='cancelled' WHERE state IN ('pending','sending')")
                self._set(db, "generation", int(self._meta(db, "generation")) + 1)
            self._set(db, "bot_id", identity)
            self._set(db, "owner", owner)

    def _call(self, token, method, payload):
        try:
            result = self.transport(token, method, payload, timeout=5)
            if not isinstance(result, dict) or result.get("ok") is not True:
                raise ControlsError("telegram_unavailable")
            return result.get("result")
        except Exception:
            raise ControlsError("telegram_unavailable") from None

    def _send(self, token, owner, text, reply_markup=None):
        if _utf16_length(text) > 4096:
            raise ControlsError("message_too_long")
        self._call(token, "sendMessage", {"chat_id": owner, "text": text,
                                         "reply_markup": reply_markup if reply_markup is not None else self.keyboard()})

    def _ack(self, token, callback_id, text):
        self._call(token, "answerCallbackQuery", {"callback_query_id": callback_id, "text": text})

    def _summary(self, error=None, **extra):
        try:
            with self._db() as db:
                result = {
                    "last_poll": self._meta(db, "last_poll", "") or None,
                    "pending_tests": db.execute("SELECT COUNT(*) FROM jobs WHERE state IN ('pending','sending')").fetchone()[0],
                    "error": error,
                    "counts": {name: 0 for name in _COUNT_NAMES}}
                result["counts"].update({row[0]: row[1] for row in db.execute("SELECT name,value FROM counts")
                                         if row[0] in _COUNT_NAMES})
                result.update(extra)
                return result
        except sqlite3.Error:
            return {"last_poll": None, "pending_tests": None, "error": "storage_unavailable", "counts": {}}

    def _record_sent(self, name):
        with self._db() as db:
            self._count(db, name)

    def _menu_text(self):
        return ("Задачи и напоминания\n\n"
                "«Задачи» — список с фильтрами. «Дедлайны» — исходные сроки, включая просроченные.\n"
                "Тест через минуту не создаёт задачу в Obsidian.\n"
                "Напоминание о задаче придёт в указанный срок, затем каждые 2 часа, "
                "пока вы не отметите её выполненной.\n\nВремя Астаны (UTC+05).")

    def publish_menu(self, token, chat_id):
        owner = _positive_id(chat_id)
        if owner is None:
            return self._summary("invalid_owner")
        try:
            self._scope(token, owner)
            self._send(token, owner, self._menu_text())
            self._record_sent("menus_sent")
            return self._summary(published=True)
        except sqlite3.Error:
            return self._summary("storage_unavailable")
        except ControlsError:
            return self._summary("telegram_unavailable")

    def _status_text(self):
        try:
            info = self.status_provider()
            if not isinstance(info, dict):
                raise ValueError
        except Exception:
            return "📋 Статус сейчас недоступен. Попробуйте немного позже.\nВремя Астаны (UTC+05)."
        counts = info.get("counts") if isinstance(info.get("counts"), dict) else info
        active = _integer(counts.get("active"))
        pending = _integer(counts.get("pending"))
        leased = _integer(counts.get("leased"))
        if pending is None:
            pending = _integer(info.get("pending_tasks"))
        waiting = pending + (leased or 0) if pending is not None else None
        next_due = _time_label(info.get("next_due"))
        last_run = _time_label(info.get("last_run"))
        repeat = info.get("repeat_minutes", 120)
        repeat = float(repeat) if isinstance(repeat, (int, float)) and not isinstance(repeat, bool) else 120.0
        if not math.isfinite(repeat) or repeat < 0 or repeat > 525600:
            repeat = 120.0
        lines = ["📋 Напоминания", "Время Астаны (UTC+05)."]
        lines.append(f"Активных задач: {active}." if active is not None else "Число активных задач ещё не получено.")
        if waiting is not None:
            lines.append(f"Ожидают напоминания: {waiting}.")
        lines.append(f"Ближайшее напоминание: {next_due}." if next_due else "Нет запланированных напоминаний.")
        lines.append(f"Повтор: каждые {repeat:g} минут до выполнения задачи." if repeat else "Каждое напоминание отправляется один раз.")
        lines.append(f"Последняя проверка: {last_run}." if last_run else "Первая проверка ещё не завершена.")
        return "\n".join(lines)

    def _view_keyboard(self, view, state_filter, page, pages, visible_tasks=()):
        rows = []
        done_buttons = [{"text": f"✅ {number}", "callback_data": payload}
                        for number, payload in visible_tasks]
        rows.extend(done_buttons[index:index + 3] for index in range(0, len(done_buttons), 3))
        navigation = []
        if page > 0:
            navigation.append({"text": "← Назад", "callback_data": f"list:{view}:{state_filter}:{page - 1}"})
        if page + 1 < pages:
            navigation.append({"text": "Далее →", "callback_data": f"list:{view}:{state_filter}:{page + 1}"})
        if navigation:
            rows.append(navigation)
        if view == "tasks":
            rows.append([{"text": ("• " if state_filter == code else "") + label,
                          "callback_data": f"list:tasks:{code}:0"}
                         for code, label in (("active", "Активные"), ("completed", "Выполненные"), ("all", "Все"))])
        rows.append([{"text": "📝 Задачи", "callback_data": "list:tasks:active:0"},
                     {"text": "📅 Дедлайны", "callback_data": "list:deadlines:active:0"}])
        rows.append([{"text": "Главное меню", "callback_data": "list:menu"}])
        return {"inline_keyboard": rows}

    def _view_message(self, view, state_filter, requested_page, stamp):
        """Fresh read-only snapshot; task text is never persisted in controls DB."""
        if self.task_provider is None:
            return "Список задач сейчас недоступен. Попробуйте немного позже.", self.keyboard()
        try:
            source = self.task_provider()
            if not isinstance(source, list):
                raise ValueError
            rows = []
            for item in source:
                if not isinstance(item, dict) or item.get("state") == "deleted":
                    continue
                completed = item.get("state") == "completed"
                if view == "deadlines" and completed:
                    continue
                if view == "tasks" and ((state_filter == "active" and completed)
                                         or (state_filter == "completed" and not completed)):
                    continue
                rows.append({"text": _clean_text(item.get("text")), "completed": completed,
                             "due": _due_timestamp(item.get("due_utc")),
                             "order": _timestamp(item.get("first_seen_utc")) or 0,
                             "id": str(item.get("task_id", "")),
                             "version": _integer(item.get("schedule_version"))})
        except Exception:
            return "Не удалось получить свежий список задач. Попробуйте немного позже.", self.keyboard()
        rows.sort(key=lambda item: (item["due"] is None, item["due"] or 0, item["order"], item["id"]))
        labels = {"active": "Активные", "completed": "Выполненные", "all": "Все"}
        title = "📅 Дедлайны" if view == "deadlines" else "📝 Задачи · " + labels[state_filter]
        scheduled = sum(item["due"] is not None for item in rows)
        if view == "deadlines":
            summary = f"Со сроком: {scheduled}. Без срока: {len(rows) - scheduled}."
        else:
            summary = f"Задач: {len(rows)}."
        blocks = []
        if not rows:
            blocks = [("Незавершённых задач нет." if view == "deadlines" else
                       {"active": "Активных задач нет.", "completed": "Выполненных задач пока нет.", "all": "Список задач пуст."}[state_filter], None, None)]
        no_due_heading = False
        for number, row in enumerate(rows, 1):
            due = row["due"]
            section_header = ""
            if view == "deadlines" and due is None and not no_due_heading:
                section_header = "Без срока\nУ этих задач нет корректного срока; дата напоминания не назначена.\n\n"
                no_due_heading = True
            prefix = f"{number}. " + ("✅ " if row["completed"] else "☐ ")
            if due is not None:
                try:
                    date = dt.datetime.fromtimestamp(due, dt.timezone.utc).astimezone(_ASTANA).strftime("%d.%m.%Y %H:%M")
                except (ValueError, OverflowError, OSError):
                    date = None
                suffix = "\nСрок: " + date if date else "\nБез корректного срока."
                if date and due < stamp and not row["completed"]:
                    suffix += " · просрочено"
            else:
                suffix = "\nБез срока."
            # Preserve long titles in full across consecutive page fragments.
            fragments = _text_chunks(row["text"], 2800)
            payload = f"done:{row['id']}:{row['version']}"
            if row["completed"] or not row["version"] or len(payload) > 64 or not _DONE.fullmatch(payload):
                payload = None
            for index, fragment in enumerate(fragments):
                continuation = " (продолжение)" if index else ""
                blocks.append(((section_header if not index else "") + prefix + continuation + ("\n" if index else "") + fragment + suffix, number, payload))
        pages, current, used, numbers, buttons = [], [], 0, set(), {}
        for block, number, payload in blocks:
            size = _utf16_length(block) + (2 if current else 0)
            if current and (used + size > 3500 or (number is not None and number not in numbers and len(numbers) >= 6)):
                pages.append(("\n\n".join(current), list(buttons.items())))
                current, used, numbers, buttons = [], 0, set(), {}
                size = _utf16_length(block)
            current.append(block)
            used += size
            if number is not None:
                numbers.add(number)
            if payload:
                buttons[number] = payload
        if current:
            pages.append(("\n\n".join(current), list(buttons.items())))
        page = min(requested_page, len(pages) - 1)
        text = f"{title}\nВремя Астаны (UTC+05).\n{summary}\n\n{pages[page][0]}\n\nСтраница {page + 1}/{len(pages)}."
        return text, self._view_keyboard(view, state_filter, page, len(pages), pages[page][1])

    @staticmethod
    def _owner_message(message, owner, sender=None):
        if not isinstance(message, dict):
            return False
        chat = message.get("chat")
        author = sender if isinstance(sender, dict) else message.get("from")
        return (isinstance(chat, dict) and chat.get("type") == "private"
                and _positive_id(chat.get("id")) == owner and isinstance(author, dict)
                and _positive_id(author.get("id")) == owner)

    def _prepare(self, update, owner, stamp):
        """Durably claim an update and local effects before any outbound response."""
        update_id = _integer(update.get("update_id")) if isinstance(update, dict) else None
        if update_id is None:
            return {"action": "ignore", "invalid": True}
        callback = update.get("callback_query")
        callback_id = callback.get("id") if isinstance(callback, dict) else None
        callback_valid_id = isinstance(callback_id, str) and 0 < len(callback_id) <= 256
        callback_owner = (isinstance(callback, dict) and callback_valid_id
                          and isinstance(callback.get("from"), dict)
                          and self._owner_message(callback.get("message"), owner, callback.get("from")))
        with self._db() as db:
            db.execute("BEGIN IMMEDIATE")
            if db.execute("SELECT 1 FROM updates WHERE id=?", (update_id,)).fetchone():
                self._count(db, "duplicates")
                return {"action": "duplicate", "callback_id": callback_id if callback_owner else None}
            db.execute("INSERT INTO updates VALUES(?,?)", (update_id, stamp))
            self._set(db, "offset", max(update_id + 1, int(self._meta(db, "offset"))))
            action = {"action": "ignore"}
            if callback_owner:
                action["callback_id"] = callback_id
                if db.execute("SELECT 1 FROM callbacks WHERE id=?", (callback_id,)).fetchone():
                    self._count(db, "duplicates")
                    action["action"] = "duplicate"
                    return action
                db.execute("INSERT INTO callbacks VALUES(?,?)", (callback_id, stamp))
                data = callback.get("data")
                done = _DONE.fullmatch(data) if isinstance(data, str) and len(data) <= 64 else None
                view = _VIEW.fullmatch(data) if isinstance(data, str) and len(data) <= 64 else None
                if done:
                    action.update(action="done", task_id=done[1].lower(), version=int(done[2]))
                elif view and (view[1] != "deadlines" or view[2] == "active"):
                    action.update(action="view", view=view[1], state_filter=view[2], page=int(view[3]))
                elif data == "list:menu":
                    action["action"] = "menu"
                elif data in ("test:ping", "test:minute", "test:status"):
                    previous = db.execute("SELECT used FROM cooldowns WHERE action=?", (data,)).fetchone()
                    if previous and stamp - previous[0] < 5:
                        action.update(action="cooldown")
                    else:
                        db.execute("INSERT INTO cooldowns VALUES(?,?) ON CONFLICT(action) DO UPDATE SET used=excluded.used", (data, stamp))
                        action["action"] = data.removeprefix("test:")
                        if data == "test:minute":
                            generation = int(self._meta(db, "generation"))
                            existing = db.execute("SELECT id,due FROM jobs WHERE chat_id=? AND generation=? AND state IN ('pending','sending')", (owner, generation)).fetchone()
                            action["reused"] = bool(existing)
                            if not existing:
                                db.execute("INSERT INTO jobs(chat_id,due,state,generation) VALUES(?,?,'pending',?)", (owner, stamp + 60, generation))
                                self._count(db, "minute_jobs_scheduled")
                else:
                    action["action"] = "unknown"
            elif isinstance(update.get("message"), dict):
                message = update["message"]
                date = _timestamp(message.get("date"))
                text = message.get("text")
                command = text.split(maxsplit=1)[0].split("@", 1)[0] if isinstance(text, str) and text.strip() else ""
                if self._owner_message(message, owner) and date is not None and stamp - 300 <= date <= stamp + 60:
                    if command in ("/start", "/help", "/test", "/menu"):
                        action["action"] = "menu"
                    elif command in ("/tasks", "/deadlines"):
                        action.update(action="view", view=command[1:], state_filter="active", page=0)
            if action["action"] == "ignore":
                self._count(db, "ignored")
            return action

    def process_update(self, token, chat_id, update, now=None):
        owner = _positive_id(chat_id)
        if owner is None:
            return self._summary("invalid_owner", handled=False)
        try:
            stamp = self._now(now)
            self._scope(token, owner)
            action = self._prepare(update, owner, stamp)
        except sqlite3.Error:
            return self._summary("storage_unavailable", handled=False)
        except ControlsError:
            return self._summary("invalid_request", handled=False)
        kind = action["action"]
        error = None
        ack = {"duplicate": "Уже обработано.", "cooldown": "Подождите несколько секунд.",
               "unknown": "Кнопка устарела. Откройте /test.", "ping": "Связь работает.",
               "status": "Статус обновлён.", "minute": "Тест уже запланирован." if action.get("reused") else "Тест придёт через 1 минуту."}.get(kind)
        text = None
        counter = None
        reply_markup = None
        if kind == "menu":
            text, counter = self._menu_text(), "menus_sent"
            ack = "Главное меню."
        elif kind == "view":
            text, reply_markup = self._view_message(action["view"], action["state_filter"], action["page"], stamp)
            counter, ack = "views_sent", "Список обновлён."
        elif kind == "ping":
            text, counter = "✅ Бот работает. Сообщения доходят.", "pings_sent"
        elif kind == "status":
            text, counter = self._status_text(), "status_sent"
        elif kind == "minute":
            text = "⏱ Тест уже запланирован. Он придёт в назначенное время." if action.get("reused") else "⏱ Тест запланирован: сообщение придёт через 1 минуту."
        elif kind == "done":
            result = "busy"
            if self.complete_task_handler is not None:
                try:
                    outcome = self.complete_task_handler(action["task_id"], action["version"])
                    result = outcome.get("status") if isinstance(outcome, dict) else "busy"
                except Exception:
                    result = "busy"
            text = {"completed": "✅ Задача отмечена выполненной. Напоминания о ней остановлены.",
                    "already_completed": "✅ Эта задача уже выполнена.",
                    "not_found": "Задача больше не найдена. Проверьте список в Obsidian.",
                    "changed": "Задача изменилась. Используйте кнопку в новом напоминании.",
                    "busy": "Файл сейчас занят. Нажмите «Выполнено» ещё раз немного позже."}.get(result,
                    "Не удалось обновить задачу. Нажмите «Выполнено» ещё раз немного позже.")
            ack = {"completed": "Задача выполнена.", "already_completed": "Уже выполнено.",
                   "not_found": "Задача не найдена.", "changed": "Задача изменилась.",
                   "busy": "Файл занят. Попробуйте ещё раз."}.get(result, "Попробуйте ещё раз.")
            counter = "completed_actions" if result == "completed" else None
        # An acknowledgement error must not prevent a committed local test/action
        # from running or cause a replay to run that action again.
        if action.get("callback_id") and ack:
            try:
                self._ack(token, action["callback_id"], ack)
            except ControlsError:
                error = "telegram_unavailable"
        if text:
            try:
                self._send(token, owner, text, reply_markup)
                if counter:
                    self._record_sent(counter)
            except ControlsError:
                error = "telegram_unavailable"
            except sqlite3.Error:
                error = "storage_unavailable"
        return self._summary(error, handled=not action.get("invalid", False), action=kind)

    def cancel_tests(self, chat_id=None):
        """Local durable cancellation; no Telegram or task/vault access."""
        owner = _positive_id(chat_id) if chat_id is not None else None
        if chat_id is not None and owner is None:
            return self._summary("invalid_owner")
        try:
            with self._db() as db:
                self._set(db, "generation", int(self._meta(db, "generation")) + 1)
                db.execute("UPDATE jobs SET state='cancelled' WHERE state IN ('pending','sending')")
            return self._summary(cancelled=True)
        except sqlite3.Error:
            return self._summary("storage_unavailable")

    def _deliver_tests(self, token, owner, stamp):
        error = None
        with self._db() as db:
            generation = int(self._meta(db, "generation"))
            rows = db.execute("SELECT id FROM jobs WHERE chat_id=? AND generation=? AND state='pending' AND due<=? ORDER BY due,id", (owner, generation, stamp)).fetchall()
        for row in rows:
            with self._db() as db:
                changed = db.execute("UPDATE jobs SET state='sending', attempts=attempts+1 WHERE id=? AND state='pending' AND generation=?", (row[0], generation)).rowcount
            if not changed:
                continue
            try:
                self._send(token, owner, "✅ Минутный тест пройден. Отложенные сообщения работают.")
            except ControlsError:
                with self._db() as db:
                    db.execute("UPDATE jobs SET state='pending',due=? WHERE id=? AND state='sending'", (stamp + 5, row[0]))
                error = "telegram_unavailable"
                continue
            with self._db() as db:
                db.execute("UPDATE jobs SET state='sent' WHERE id=? AND state='sending'", (row[0],))
                self._count(db, "tests_sent")
        return error

    def tick(self, token, chat_id, now=None):
        owner = _positive_id(chat_id)
        if owner is None:
            return self._summary("invalid_owner")
        error = None
        try:
            stamp = self._now(now)
            self._scope(token, owner)
            with self._db() as db:
                offset = int(self._meta(db, "offset"))
            try:
                updates = self._call(token, "getUpdates", {"offset": offset, "timeout": 1, "limit": 5,
                                                           "allowed_updates": ["message", "callback_query"]})
                if not isinstance(updates, list):
                    raise ControlsError("telegram_unavailable")
                with self._db() as db:
                    self._set(db, "last_poll", dt.datetime.fromtimestamp(stamp, dt.timezone.utc).isoformat())
                updates = [item for item in updates if isinstance(item, dict) and _integer(item.get("update_id")) is not None]
                for update in sorted(updates, key=lambda item: item["update_id"]):
                    result = self.process_update(token, owner, update, stamp)
                    if result.get("error"):
                        error = result["error"]
                    # Never confirm a later update if durable local processing of
                    # this one failed. The next poll will fetch this update again.
                    if not result.get("handled"):
                        break
            except ControlsError:
                error = "telegram_unavailable"
            error = self._deliver_tests(token, owner, stamp) or error
        except sqlite3.Error:
            error = "storage_unavailable"
        except ControlsError:
            error = "invalid_request"
        return self._summary(error)
