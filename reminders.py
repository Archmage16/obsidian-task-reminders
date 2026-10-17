"""Local Markdown reminders. Telegram is an outbound-only, optional transport.

Only the configured tasks file is read/updated. The queue and status live in the
configured private data directory. No Telegram update polling is performed.
"""
from __future__ import annotations

import argparse
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone, tzinfo
import importlib
import json
import os
from pathlib import Path
import re
import sqlite3
import sys
import time
from typing import Callable, Iterator
import urllib.error
import urllib.request
import uuid
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

UTC = timezone.utc
LEASE_SECONDS = 120
RETRY_SECONDS = 60
STATES = ("pending", "leased", "sent", "completed", "deleted", "unscheduled", "invalid", "past_needs_time")
TASK = re.compile(r"^(?P<prefix>[ \t]*[-*+]\s+\[(?P<checked>[ xX])\][ \t]+)(?P<body>.*)$")
MARKER = re.compile(r"[ \t]*<!--\s*obs-task:\s*([0-9a-fA-F-]{36})\s*-->")
FENCE = re.compile(r"^[ \t]*(?:>[ \t]*)*(?:(?:[-*+]|\d+[.)])[ \t]+)?(?P<ticks>`{3,}|~{3,})(?P<tail>.*)$")
DATE = re.compile(r"(?P<day>\d{4}-\d{2}-\d{2}|сегодня|завтра)\s+(?P<hour>\d{1,2}):(?P<minute>\d{2})", re.I)


def utc_now() -> datetime:
    return datetime.now(UTC)


def iso(value: float | datetime | None) -> str | None:
    if value is None:
        return None
    if not isinstance(value, datetime):
        value = datetime.fromtimestamp(value, UTC)
    return value.astimezone(UTC).isoformat()


def user_timezone(name: str) -> tzinfo:
    try:
        return ZoneInfo(name)
    except ZoneInfoNotFoundError:
        # Windows does not always ship an IANA database. Qyzylorda is UTC+05.
        if name == "Asia/Qyzylorda":
            return timezone(timedelta(hours=5), name="UTC+05:00")
        raise EngineError("timezone_unavailable") from None


def parse_reminder(expression: str, now: datetime, zone: tzinfo) -> datetime:
    """Parse an absolute date or a relative expression; past is a queue policy."""
    match = DATE.fullmatch(expression.strip())
    if not match:
        raise ValueError("invalid_datetime")
    day = match["day"].lower()
    local = now.astimezone(zone)
    if day in ("сегодня", "завтра"):
        target = local.date() + timedelta(days=day == "завтра")
    else:
        try:
            target = datetime.strptime(day, "%Y-%m-%d").date()
        except ValueError:
            raise ValueError("invalid_datetime") from None
    try:
        return datetime(target.year, target.month, target.day,
                        int(match["hour"]), int(match["minute"]), tzinfo=zone).astimezone(UTC)
    except ValueError:
        raise ValueError("invalid_datetime") from None


class EngineError(Exception):
    """An intentionally short, safe error code, suitable for local status."""


class AlreadyRunning(EngineError):
    pass


class DeliveryError(EngineError):
    pass


class WorkerLock:
    """An OS lock. An old lock file after a crash does not block a new worker."""
    def __init__(self, path: Path):
        self.path = Path(path)
        self.handle = None

    def __enter__(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.handle = self.path.open("a+b")
        self.handle.seek(0, os.SEEK_END)
        if self.handle.tell() == 0:
            self.handle.write(b"\0")
            self.handle.flush()
        self.handle.seek(0)
        try:
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(self.handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(self.handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            self.handle.close()
            self.handle = None
            raise AlreadyRunning("worker_running") from None
        return self

    def __exit__(self, *unused):
        if self.handle is not None:
            try:
                self.handle.seek(0)
                if os.name == "nt":
                    import msvcrt
                    msvcrt.locking(self.handle.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(self.handle.fileno(), fcntl.LOCK_UN)
            finally:
                self.handle.close()
                self.handle = None


@dataclass(frozen=True)
class Config:
    vault: Path
    tasks_path: Path
    data_directory: Path
    timezone: str = "Asia/Qyzylorda"
    poll_seconds: int = 20
    repeat_seconds: int = 7200

    @classmethod
    def read(cls, path: Path) -> "Config":
        try:
            values = json.loads(Path(path).read_text(encoding="utf-8-sig"))
            vault = Path(values["vault"]).expanduser().resolve()
            task_name = Path(values.get("tasks_file", "Задачи.md"))
            tasks = (vault / task_name).resolve()
            if task_name.is_absolute() or not tasks.is_relative_to(vault):
                raise ValueError
            directory = Path(values["data_directory"]).expanduser().resolve()
            interval = max(1, min(3600, int(values.get("poll_seconds", 20))))
            repeat = int(values.get("repeat_seconds", 7200))
            if repeat < 0:
                raise ValueError
            zone = str(values.get("timezone", "Asia/Qyzylorda"))
        except (OSError, ValueError, TypeError, KeyError):
            raise EngineError("configuration_invalid") from None
        return cls(vault, tasks, directory, zone, interval, repeat)


@dataclass(frozen=True)
class MarkdownTask:
    task_id: str
    text: str
    expression: str | None
    checked: bool


def attach_ids(source: str, *, assign_missing: bool = True) -> tuple[str, list[MarkdownTask]]:
    """Keep all non-task text and line endings; ignore fenced code examples."""
    result: list[str] = []
    tasks: list[MarkdownTask] = []
    seen: set[str] = set()
    fence_char = ""
    fence_size = 0
    for original in source.splitlines(keepends=True):
        content = original.rstrip("\r\n")
        ending = original[len(content):]
        fence = FENCE.match(content)
        if fence_char:
            if fence and fence["ticks"][0] == fence_char and len(fence["ticks"]) >= fence_size and not fence["tail"].strip():
                fence_char = ""
            result.append(original)
            continue
        if fence:
            fence_char, fence_size = fence["ticks"][0], len(fence["ticks"])
            result.append(original)
            continue
        match = TASK.match(content)
        if not match:
            result.append(original)
            continue
        body = match["body"]
        markers = list(MARKER.finditer(body))
        task_id = None
        for marker in markers:
            try:
                task_id = str(uuid.UUID(marker[1]))
                break
            except ValueError:
                continue
        replace_marker = task_id is None or task_id in seen or len(markers) > 1
        if task_id is None or task_id in seen:
            if not assign_missing:
                result.append(original)
                continue
            task_id = str(uuid.uuid4())
        seen.add(task_id)
        if replace_marker and assign_missing:
            clean = MARKER.sub("", body)
            trailing = clean[len(clean.rstrip(" \t")):]
            body = clean.rstrip(" \t") + f" <!-- obs-task:{task_id} -->" + trailing
            content = match["prefix"] + body
        clean_body = MARKER.sub("", body).strip()
        title, bell, expression = clean_body.partition("🔔")
        expression = " ".join(expression.split()).lower() if bell else None
        tasks.append(MarkdownTask(task_id, title.strip() or "Задача", expression, match["checked"].lower() == "x"))
        result.append(content + ending)
    return "".join(result), tasks


def read_and_attach_ids(path: Path, backup_dir: Path) -> list[MarkdownTask]:
    """Use the locked writer; never fall back to an unsafe read/replace race."""
    try:
        storage = importlib.import_module("markdown_store")
    except ImportError:
        raise EngineError("markdown_store_unavailable") from None
    try:
        return storage.update_markdown(path, backup_dir, attach_ids)
    except storage.MarkdownMissing:
        raise EngineError("tasks_file_missing") from None
    except storage.MarkdownEncodingError:
        raise EngineError("tasks_encoding_invalid") from None
    except storage.MarkdownBusy:
        raise EngineError("tasks_file_busy") from None
    except storage.MarkdownWriteError:
        raise EngineError("storage_error") from None


def load_private_token(path: Path) -> str:
    """The GUI owns encryption and provisioning of this new bot's token."""
    module = importlib.import_module("secrets_store")
    return module.load(path)


def truncate_telegram_text(text: str, maximum: int = 4096) -> str:
    """Bound UTF-16 units conservatively without splitting an astral character."""
    length = 0
    for index, char in enumerate(text):
        width = 2 if ord(char) > 0xFFFF else 1
        if length + width > maximum:
            return text[:index]
        length += width
    return text


def telegram_send(token: str, chat_id: int, text: str, *, reply_markup: dict | None = None) -> None:
    """Only sendMessage. Never print a URL, token, response body or raw error."""
    values = {"chat_id": chat_id, "text": truncate_telegram_text(text), "disable_web_page_preview": True}
    if reply_markup is not None:
        values["reply_markup"] = reply_markup
    payload = json.dumps(values).encode("utf-8")
    request = urllib.request.Request(f"https://api.telegram.org/bot{token}/sendMessage", data=payload,
                                     headers={"Content-Type": "application/json"}, method="POST")
    try:
        with urllib.request.urlopen(request, timeout=5) as response:
            result = json.loads(response.read(65536))
        if not isinstance(result, dict) or not result.get("ok"):
            code = result.get("error_code") if isinstance(result, dict) else None
            raise DeliveryError(f"telegram_api_{code}" if isinstance(code, int) else "telegram_api_rejected")
    except urllib.error.HTTPError as error:
        raise DeliveryError(f"telegram_http_{error.code}") from None
    except (urllib.error.URLError, TimeoutError, OSError):
        raise DeliveryError("network_error") from None
    except (ValueError, TypeError):
        raise DeliveryError("telegram_response_invalid") from None


class TaskEngine:
    def __init__(self, config_path: Path | str, *, sender: Callable[[str, int, str], None] | None = None,
                 token_loader: Callable[[Path], str] | None = None):
        self.config_path = Path(config_path)
        self.config = Config.read(self.config_path)
        self.zone = user_timezone(self.config.timezone)
        self.data_dir = self.config.data_directory
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.db_path = self.data_dir / "tasks.sqlite3"
        self.lock_path = self.data_dir / "worker.lock"
        self.status_path = self.data_dir / "status.json"
        self.sender = sender or telegram_send
        self.uses_default_sender = sender is None
        self.token_loader = token_loader or load_private_token
        self.owner = str(uuid.uuid4())
        with self._connection() as connection:
            connection.executescript("""
                CREATE TABLE IF NOT EXISTS tasks (
                    task_id TEXT PRIMARY KEY, text TEXT NOT NULL,
                    expression TEXT, due_utc REAL, state TEXT NOT NULL,
                    schedule_version INTEGER NOT NULL DEFAULT 1,
                    delivered_version INTEGER NOT NULL DEFAULT 0,
                    armed INTEGER NOT NULL DEFAULT 0,
                    first_seen_utc REAL NOT NULL, updated_utc REAL NOT NULL,
                    delivered_utc REAL, retry_at REAL, lease_owner TEXT, lease_until REAL,
                    attempts INTEGER NOT NULL DEFAULT 0, last_error TEXT,
                    next_reminder_utc REAL, reminder_count INTEGER NOT NULL DEFAULT 0
                );
                CREATE INDEX IF NOT EXISTS tasks_queue ON tasks(state, due_utc, retry_at);
                CREATE TABLE IF NOT EXISTS relative_anchors (
                    task_id TEXT NOT NULL, expression TEXT NOT NULL, due_utc REAL NOT NULL,
                    PRIMARY KEY(task_id, expression)
                );
            """)
            self._migrate_queue(connection)

    def _migrate_queue(self, connection: sqlite3.Connection):
        """Add durable recurrence to existing databases without replaying claims."""
        connection.execute("BEGIN IMMEDIATE")
        columns = {row["name"] for row in connection.execute("PRAGMA table_info(tasks)")}
        if "next_reminder_utc" not in columns:
            connection.execute("ALTER TABLE tasks ADD COLUMN next_reminder_utc REAL")
        if "reminder_count" not in columns:
            connection.execute("ALTER TABLE tasks ADD COLUMN reminder_count INTEGER NOT NULL DEFAULT 0")
        connection.execute("CREATE INDEX IF NOT EXISTS tasks_reminder_queue ON tasks(state,next_reminder_utc,retry_at)")
        # Old one-shot rows recorded the last successful delivery in these fields.
        connection.execute("""UPDATE tasks SET reminder_count=1 WHERE reminder_count=0
            AND delivered_version=schedule_version AND delivered_utc IS NOT NULL""")
        connection.execute("""UPDATE tasks SET next_reminder_utc=due_utc
            WHERE state IN ('pending','leased') AND next_reminder_utc IS NULL
            AND delivered_version!=schedule_version""")
        if self.config.repeat_seconds:
            # Also handles explicitly enabling repeats for a previous one-shot queue.
            connection.execute("""UPDATE tasks SET state='pending',next_reminder_utc=
                COALESCE(delivered_utc,due_utc)+? WHERE state='sent'
                AND delivered_version=schedule_version AND due_utc IS NOT NULL""",
                (self.config.repeat_seconds,))
            connection.execute("""UPDATE tasks SET next_reminder_utc=COALESCE(delivered_utc,due_utc)+?
                WHERE state IN ('pending','leased') AND next_reminder_utc IS NULL
                AND delivered_version=schedule_version AND due_utc IS NOT NULL""",
                (self.config.repeat_seconds,))
        else:
            connection.execute("""UPDATE tasks SET state='sent',next_reminder_utc=NULL,
                lease_owner=NULL,lease_until=NULL,retry_at=NULL WHERE state='pending'
                AND delivered_version=schedule_version""")
        connection.execute("""UPDATE tasks SET next_reminder_utc=NULL
            WHERE state IN ('completed','deleted','unscheduled','invalid','past_needs_time','sent')""")

    @contextmanager
    def _connection(self) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(self.db_path, timeout=5)
        connection.row_factory = sqlite3.Row
        try:
            with connection:
                yield connection
        finally:
            connection.close()

    def _sync_tasks(self, now: datetime, *, captured: list[MarkdownTask] | None = None) -> dict[str, int]:
        if captured is None:
            captured = read_and_attach_ids(self.config.tasks_path, self.data_dir / "markdown-backups")
        stamp = now.timestamp()
        grace = max(60, self.config.poll_seconds * 2)
        seen = {item.task_id for item in captured}
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            for item in captured:
                old = connection.execute("SELECT * FROM tasks WHERE task_id=?", (item.task_id,)).fetchone()
                changed = old is None or old["expression"] != item.expression
                version = (old["schedule_version"] + 1 if old else 1) if changed else old["schedule_version"]
                delivered = old["delivered_version"] if old and not changed else 0
                delivered_at = old["delivered_utc"] if old and not changed else None
                reminder_count = old["reminder_count"] if old and not changed else 0
                due = old["due_utc"] if old and not changed else None
                next_reminder = old["next_reminder_utc"] if old and not changed else None
                armed = old["armed"] if old and not changed else 0
                invalid = False
                if changed and item.expression is not None:
                    try:
                        relative = re.match(r"^(сегодня|завтра)\s", item.expression, re.I) is not None
                        anchor = connection.execute("SELECT due_utc FROM relative_anchors WHERE task_id=? AND expression=?",
                                                    (item.task_id,item.expression)).fetchone() if relative else None
                        due = anchor["due_utc"] if anchor else parse_reminder(item.expression, now, self.zone).timestamp()
                        if relative and anchor is None:
                            connection.execute("INSERT INTO relative_anchors(task_id,expression,due_utc) VALUES(?,?,?)",
                                               (item.task_id,item.expression,due))
                    except ValueError:
                        invalid = True
                elif old and not changed:
                    invalid = old["due_utc"] is None and item.expression is not None
                if item.checked:
                    state = "completed"
                elif item.expression is None:
                    state = "unscheduled"
                elif invalid:
                    state = "invalid"
                elif delivered == version:
                    if self.config.repeat_seconds:
                        if next_reminder is None:
                            next_reminder = (delivered_at if delivered_at is not None else due) + self.config.repeat_seconds
                        state = "leased" if old and old["state"] == "leased" else "pending"
                        armed = 1
                    else:
                        state = "sent"
                elif due <= stamp and not armed and stamp - due > grace:
                    # A newly discovered past task requires an intentional new time.
                    state = "past_needs_time"
                elif old and not changed and old["state"] == "leased":
                    state = "leased"
                else:
                    state, armed = "pending", 1
                if state in ("pending", "leased"):
                    if next_reminder is None:
                        next_reminder = due
                else:
                    next_reminder = None
                keep_lease = state == "leased" and old is not None and not changed
                retry = old["retry_at"] if old and not changed and state in ("pending", "leased") else None
                connection.execute("""
                    INSERT INTO tasks(task_id,text,expression,due_utc,state,schedule_version,delivered_version,
                        armed,first_seen_utc,updated_utc,delivered_utc,retry_at,lease_owner,lease_until,attempts,last_error,
                        next_reminder_utc,reminder_count)
                    VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                    ON CONFLICT(task_id) DO UPDATE SET text=excluded.text,expression=excluded.expression,
                        due_utc=excluded.due_utc,state=excluded.state,schedule_version=excluded.schedule_version,
                        armed=excluded.armed,updated_utc=excluded.updated_utc,retry_at=excluded.retry_at,
                        lease_owner=excluded.lease_owner,lease_until=excluded.lease_until,
                        attempts=excluded.attempts,last_error=excluded.last_error,
                        delivered_version=excluded.delivered_version,delivered_utc=excluded.delivered_utc,
                        next_reminder_utc=excluded.next_reminder_utc,reminder_count=excluded.reminder_count
                """, (item.task_id,item.text,item.expression,due,state,version,delivered,armed,
                      old["first_seen_utc"] if old else stamp,stamp,delivered_at,
                      retry,old["lease_owner"] if keep_lease else None,old["lease_until"] if keep_lease else None,
                      old["attempts"] if old and not changed else 0,
                      old["last_error"] if old and not changed and state in ("pending", "leased") else None,
                      next_reminder,reminder_count))
            for row in connection.execute("SELECT task_id FROM tasks WHERE state!='deleted'").fetchall():
                if row["task_id"] not in seen:
                    connection.execute("UPDATE tasks SET state='deleted',next_reminder_utc=NULL,lease_owner=NULL,lease_until=NULL,retry_at=NULL,updated_utc=? WHERE task_id=?",
                                       (stamp,row["task_id"]))
        return self._counts()

    def sync_tasks(self, *, now: datetime | None = None, _lock_held: bool = False) -> dict:
        """Attach IDs and reconcile queue; this method never calls Telegram."""
        if not _lock_held:
            with WorkerLock(self.lock_path):
                return self.sync_tasks(now=now, _lock_held=True)
        moment = now or utc_now()
        self._sync_tasks(moment)
        return self._write_status(moment, None, configured=None)

    def fresh_tasks(self, *, now: datetime | None = None, _lock_held: bool = False) -> list[dict]:
        """Reconcile Markdown before listing, without sending or hiding sync errors.

        Services holding WorkerLock must pass _lock_held=True. A busy or invalid
        Markdown file raises EngineError rather than returning an old DB list.
        due_utc is the original task deadline; next_reminder_utc is delivery time.
        """
        if not _lock_held:
            with WorkerLock(self.lock_path):
                return self.fresh_tasks(now=now, _lock_held=True)
        self.sync_tasks(now=now, _lock_held=True)
        return self.list_tasks()

    def complete_task(self, task_id: str, expected_version: int | None = None, *, _lock_held: bool = False) -> dict:
        """Complete exactly one existing Markdown task; never send a message."""
        if not _lock_held:
            try:
                with WorkerLock(self.lock_path):
                    return self.complete_task(task_id, expected_version, _lock_held=True)
            except AlreadyRunning:
                return {"status":"busy"}
        try:
            task_id = str(uuid.UUID(task_id))
        except (ValueError, TypeError, AttributeError):
            return {"status":"not_found"}
        try:
            with self._connection() as connection:
                row = connection.execute("SELECT * FROM tasks WHERE task_id=?", (task_id,)).fetchone()
            if row is None or row["state"] == "deleted":
                return {"status":"not_found"}
            if expected_version is not None and (isinstance(expected_version, bool) or
                                                  not isinstance(expected_version, int) or
                                                  row["schedule_version"] != expected_version):
                return {"status":"changed"}
            storage = importlib.import_module("markdown_store")

            def transform(source: str):
                lines = source.splitlines(keepends=True)
                matches = []
                fence_char, fence_size = "", 0
                for index, original in enumerate(lines):
                    content = original.rstrip("\r\n")
                    ending = original[len(content):]
                    fence = FENCE.match(content)
                    if fence_char:
                        if fence and fence["ticks"][0] == fence_char and len(fence["ticks"]) >= fence_size and not fence["tail"].strip():
                            fence_char = ""
                        continue
                    if fence:
                        fence_char, fence_size = fence["ticks"][0], len(fence["ticks"])
                        continue
                    match = TASK.match(content)
                    if not match:
                        continue
                    identifiers = []
                    for marker in MARKER.finditer(match["body"]):
                        try:
                            identifiers.append(str(uuid.UUID(marker[1])))
                        except ValueError:
                            continue
                    if task_id in identifiers:
                        if len(identifiers) != 1:
                            return source, ("changed", None)
                        clean = MARKER.sub("", match["body"]).strip()
                        unused, bell, expression = clean.partition("🔔")
                        expression = " ".join(expression.split()).lower() if bell else None
                        matches.append((index,content,ending,match,expression))
                if not matches:
                    return source, ("not_found", None)
                if len(matches) != 1:
                    return source, ("changed", None)
                index,content,ending,match,expression = matches[0]
                if expected_version is not None and expression != row["expression"]:
                    # Deadline edits may not have reached SQLite at callback time.
                    return source, ("changed", None)
                state = "already_completed" if match["checked"].lower() == "x" else "completed"
                if state == "completed":
                    position = match.start("checked")
                    lines[index] = content[:position]+"x"+content[position+1:]+ending
                updated = "".join(lines)
                unused, captured = attach_ids(updated, assign_missing=False)
                return updated, (state,captured)

            try:
                state,captured = storage.update_markdown(self.config.tasks_path, self.data_dir/"markdown-backups", transform)
            except storage.MarkdownMissing:
                return {"status":"not_found"}
            except (storage.MarkdownBusy, storage.MarkdownEncodingError, storage.MarkdownWriteError):
                return {"status":"busy"}
            if captured is not None:
                self._sync_tasks(utc_now(), captured=captured)
                self._write_status(utc_now(), None, configured=None)
            return {"status":state}
        except (ImportError, sqlite3.Error, OSError):
            return {"status":"busy"}

    def _auth(self) -> tuple[str | None, int | None, str | None]:
        token_path = self.data_dir / ("token.dpapi" if os.name == "nt" else "token.enc")
        settings_path = self.data_dir / "settings.json"
        if not token_path.is_file() or not settings_path.is_file():
            return None, None, "telegram_not_configured"
        try:
            settings = json.loads(settings_path.read_text(encoding="utf-8-sig"))
            chat_id = settings.get("chat_id")
            if not isinstance(chat_id, int) or isinstance(chat_id, bool) or chat_id == 0:
                return None, None, "chat_not_configured"
            token = self.token_loader(token_path)
            if not isinstance(token, str) or not token.strip() or any(c.isspace() for c in token):
                return None, None, "token_unavailable"
            return token, chat_id, None
        except Exception:
            # DPAPI, configuration or loader errors may contain private content.
            return None, None, "token_unavailable"

    def _claim_next(self, now: datetime) -> dict | None:
        stamp = now.timestamp()
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute("UPDATE tasks SET state='pending',lease_owner=NULL,lease_until=NULL WHERE state='leased' AND lease_until<=?", (stamp,))
            row = connection.execute("""SELECT * FROM tasks WHERE state='pending' AND next_reminder_utc<=?
                AND (retry_at IS NULL OR retry_at<=?) ORDER BY next_reminder_utc,task_id LIMIT 1""", (stamp,stamp)).fetchone()
            if row is None:
                return None
            connection.execute("UPDATE tasks SET state='leased',lease_owner=?,lease_until=?,attempts=attempts+1 WHERE task_id=? AND schedule_version=?",
                               (self.owner,stamp+LEASE_SECONDS,row["task_id"],row["schedule_version"]))
            return dict(row)

    def _still_claimed(self, row: dict) -> dict | None:
        with self._connection() as connection:
            current = connection.execute("SELECT * FROM tasks WHERE task_id=? AND schedule_version=? AND state='leased' AND lease_owner=?",
                                         (row["task_id"],row["schedule_version"],self.owner)).fetchone()
            return dict(current) if current else None

    def _release_claim(self, row: dict):
        """A failed pre-send recheck must not leave an unsent two-minute lease."""
        with self._connection() as connection:
            connection.execute("""UPDATE tasks SET state='pending',lease_owner=NULL,lease_until=NULL
                WHERE task_id=? AND schedule_version=? AND state='leased' AND lease_owner=?""",
                (row["task_id"],row["schedule_version"],self.owner))

    def _finish(self, row: dict, now: datetime, error: str | None):
        with self._connection() as connection:
            if error:
                connection.execute("""UPDATE tasks SET state='pending',retry_at=?,last_error=?,lease_owner=NULL,lease_until=NULL
                    WHERE task_id=? AND schedule_version=? AND state='leased' AND lease_owner=?""",
                    (now.timestamp()+RETRY_SECONDS,error,row["task_id"],row["schedule_version"],self.owner))
            else:
                state = "pending" if self.config.repeat_seconds else "sent"
                next_reminder = now.timestamp()+self.config.repeat_seconds if self.config.repeat_seconds else None
                connection.execute("""UPDATE tasks SET state=?,delivered_version=schedule_version,delivered_utc=?,
                    next_reminder_utc=?,reminder_count=reminder_count+1,
                    retry_at=NULL,last_error=NULL,lease_owner=NULL,lease_until=NULL
                    WHERE task_id=? AND schedule_version=? AND state='leased' AND lease_owner=?""",
                    (state,now.timestamp(),next_reminder,row["task_id"],row["schedule_version"],self.owner))

    def _message(self, row: dict, now: datetime) -> str:
        due = datetime.fromtimestamp(row["due_utc"], self.zone)
        heading = "🔁 Повторное напоминание" if row["reminder_count"] else "🔔 Напоминание"
        text = f"{heading}\n{truncate_telegram_text(row['text'],3500)}\n\nСрок задачи: {due:%d.%m.%Y %H:%M}"
        planned = row["next_reminder_utc"] if row["next_reminder_utc"] is not None else row["due_utc"]
        if now.timestamp() - planned >= max(60, self.config.poll_seconds * 3):
            text += "\nОтправлено позже назначенного времени."
        if self.config.repeat_seconds:
            interval = "2 часа" if self.config.repeat_seconds == 7200 else f"{self.config.repeat_seconds // 60} мин."
            text += f"\nСледующее — через {interval}, если дело не выполнено."
        return truncate_telegram_text(text)

    @staticmethod
    def completion_button(row: dict) -> dict:
        callback = f"done:{row['task_id']}:{row['schedule_version']}"
        if len(callback.encode("utf-8")) > 64:
            return {"inline_keyboard":[]}
        return {"inline_keyboard":[[{"text":"✅ Выполнено", "callback_data":callback}]]}

    def run_once(self, *, now: datetime | None = None, _lock_held: bool = False,
                 max_deliveries: int = 50) -> dict:
        """Sync then attempt at most max_deliveries claims (default CLI limit 50).

        Zero only reconciles/status-checks. Services can use one per pass to
        handle Telegram controls between deliveries. Sender injection is offline.
        """
        if isinstance(max_deliveries, bool) or not isinstance(max_deliveries, int) or max_deliveries < 0:
            raise EngineError("delivery_limit_invalid")
        if not _lock_held:
            with WorkerLock(self.lock_path):
                return self.run_once(now=now, _lock_held=True, max_deliveries=max_deliveries)
        moment = now or utc_now()
        error = None
        configured = False
        sent = failed = 0
        try:
            self._sync_tasks(moment)
            token, chat_id, error = self._auth()
            configured = token is not None and chat_id is not None
            if configured:
                for unused in range(max_deliveries):
                    moment = now or utc_now()
                    row = self._claim_next(moment)
                    if row is None:
                        break
                    # Re-read immediately before sending so edits/cancellation win.
                    try:
                        self._sync_tasks(moment)
                    except (EngineError, sqlite3.Error, OSError):
                        self._release_claim(row)
                        raise
                    current = self._still_claimed(row)
                    if current is None:
                        continue
                    try:
                        if self.uses_default_sender:
                            self.sender(token, chat_id, self._message(current, moment),
                                        reply_markup=self.completion_button(current))
                        else:
                            self.sender(token, chat_id, self._message(current, moment))
                    except DeliveryError as failure:
                        safe = str(failure)
                        if not re.fullmatch(r"(?:telegram_(?:http|api)_\d{3}|telegram_api_rejected|telegram_response_invalid|network_error)", safe):
                            safe = "delivery_error"
                        self._finish(row, now or utc_now(), safe)
                        error, failed = safe, failed + 1
                    except Exception:
                        self._finish(row, now or utc_now(), "delivery_error")
                        error, failed = "delivery_error", failed + 1
                    else:
                        self._finish(row, now or utc_now(), None)
                        sent += 1
        except EngineError as failure:
            error = str(failure)
        except (sqlite3.Error, OSError):
            error = "storage_error"
        return self._write_status(now or utc_now(), error, configured=configured, sent=sent, failed=failed)

    def _counts(self) -> dict[str, int]:
        counts = dict.fromkeys(STATES, 0)
        with self._connection() as connection:
            for row in connection.execute("SELECT state,COUNT(*) AS n FROM tasks GROUP BY state"):
                counts[row["state"]] = row["n"]
        # Delivery does not complete a task: one-shot 'sent' rows stay active
        # until the Markdown checkbox is checked or the task is deleted.
        counts["active"] = sum(counts[x] for x in ("pending", "leased", "sent", "unscheduled", "invalid", "past_needs_time"))
        return counts

    def status(self) -> dict:
        """Compact safe status. Does not read or change the Markdown file."""
        previous = {}
        try:
            previous = json.loads(self.status_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            pass
        with self._connection() as connection:
            row = connection.execute("""SELECT MIN(MAX(next_reminder_utc,
                COALESCE(retry_at,next_reminder_utc),
                COALESCE(CASE WHEN state='leased' THEN lease_until END,next_reminder_utc))) AS next_due
                FROM tasks WHERE state IN ('pending','leased')""").fetchone()
        return {"last_run":previous.get("last_run"), "next_due":iso(row["next_due"]),
                "counts":self._counts(), "error":previous.get("error"),
                "repeat_seconds":self.config.repeat_seconds,
                "telegram_configured":previous.get("telegram_configured", False)}

    def _write_status(self, now: datetime, error: str | None, *, configured: bool | None,
                      sent: int = 0, failed: int = 0) -> dict:
        snapshot = self.status()
        snapshot.update(last_run=iso(now), error=error, sent_this_run=sent, failed_this_run=failed)
        if configured is not None:
            snapshot["telegram_configured"] = configured
        temporary = self.status_path.with_suffix(".tmp")
        temporary.write_text(json.dumps(snapshot, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(temporary, self.status_path)
        return snapshot

    def list_tasks(self) -> list[dict]:
        """Local GUI data; never emitted by the background worker."""
        with self._connection() as connection:
            return [dict(row) for row in connection.execute("SELECT * FROM tasks WHERE state!='deleted' ORDER BY due_utc IS NULL,due_utc,first_seen_utc")]

    def watch(self):
        with WorkerLock(self.lock_path):
            while True:
                self.run_once(_lock_held=True)
                time.sleep(self.config.poll_seconds)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Local Obsidian task reminders")
    parser.add_argument("--config", type=Path, default=Path(__file__).with_name("config.json"))
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--once", action="store_true")
    mode.add_argument("--watch", action="store_true")
    mode.add_argument("--status", action="store_true")
    args = parser.parse_args(argv)
    try:
        engine = TaskEngine(args.config)
        if args.watch:
            engine.watch()
            return 0
        result = engine.status() if args.status else engine.run_once()
        print(json.dumps(result, ensure_ascii=True, separators=(",", ":")))
        return 0
    except AlreadyRunning:
        print('{"error":"worker_running"}')
        return 0
    except KeyboardInterrupt:
        return 0
    except EngineError as error:
        print(json.dumps({"error":str(error)}, ensure_ascii=True))
        return 1
    except Exception:
        print('{"error":"engine_error"}')
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
