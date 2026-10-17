"""Local masked-token setup. Never writes tokens to logs, Markdown or config."""
import json
import os
import re
import sqlite3
import threading
import time
import tkinter as tk
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from tkinter import ttk

import secrets_store

ROOT = Path(__file__).resolve().parent
CONFIG = json.loads((ROOT / 'config.json').read_text(encoding='utf-8-sig'))
DATA = Path(CONFIG['data_directory'])
DATA.mkdir(parents=True, exist_ok=True)


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def api(token, method, parameters=None):
    payload = urllib.parse.urlencode(parameters or {}).encode()
    request = urllib.request.Request('https://api.telegram.org/bot' + token + '/' + method,
                                     data=payload, method='POST')
    try:
        with urllib.request.build_opener(NoRedirect).open(request, timeout=20) as response:
            result = json.load(response)
    except urllib.error.HTTPError as error:
        # Never show str(error): it may contain a credential-bearing URL.
        code = error.code
        error.close()
        if code == 401:
            raise RuntimeError('Токен не принят. Проверь новый токен в BotFather.') from None
        if code in (400, 403):
            raise RuntimeError('Открой своего нового бота и нажми START, затем повтори проверку.') from None
        raise RuntimeError('Telegram временно недоступен. Повтори проверку позже.') from None
    except Exception:
        raise RuntimeError('Нет подключения к Telegram. Проверь интернет и повтори.') from None
    if not result.get('ok'):
        raise RuntimeError('Telegram не принял запрос. Проверь токен и нажатие START.')
    return result['result']


def read_settings():
    try:
        return json.loads((DATA / 'settings.json').read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return {}


def owner_chat():
    settings = read_settings()
    if settings.get('chat_id'):
        return int(settings['chat_id'])
    # Only reuse this user's numeric destination, never the other bot's token.
    database = Path(os.environ['LOCALAPPDATA']) / 'AITUGradeTracker' / 'grades.sqlite3'
    if not database.is_file():
        return None
    try:
        with sqlite3.connect(database.as_uri() + '?mode=ro', uri=True) as connection:
            row = connection.execute("SELECT value FROM settings WHERE key='owner_id'").fetchone()
        value = json.loads(row[0]) if row else None
        return int(value) if value and int(value) > 0 else None
    except (OSError, sqlite3.Error, ValueError, TypeError):
        return None


def atomic_json(path, value):
    temporary = path.with_suffix('.tmp')
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    os.replace(temporary, path)


window = tk.Tk()
window.title('Obsidian → отдельный бот задач')
window.geometry('650x500')
window.minsize(600, 470)
frame = ttk.Frame(window, padding=22)
frame.pack(fill='both', expand=True)
ttk.Label(frame, text='Подключить напоминания о задачах', font=('Segoe UI', 15, 'bold')).pack(anchor='w')
ttk.Label(frame, text='1. В BotFather отправь /revoke для нового бота задач.\n'
          '2. Вставь полученный новый токен сюда, затем нажми «Подключить».\n'
          'Токен хранится зашифрованным Windows и не попадает в заметки.',
          wraplength=590, justify='left').pack(anchor='w', pady=(16, 12))
ttk.Label(frame, text='Новый токен (ввод скрыт)').pack(anchor='w')
token_entry = ttk.Entry(frame, show='●', width=70)
token_entry.pack(fill='x', pady=(5, 12))
chat = owner_chat()
if chat is None:
    ttk.Label(frame, text='Твой числовой Telegram ID (для личного чата с ботом)').pack(anchor='w')
    chat_entry = ttk.Entry(frame, width=30)
    chat_entry.pack(anchor='w', pady=5)
else:
    chat_entry = None
    ttk.Label(frame, text='Получатель: твой личный Telegram, уже определён на этом компьютере.').pack(anchor='w')
status = tk.StringVar(value='Ожидаю новый токен. Не отправляй его в чат Codex.')
status_label = ttk.Label(frame, textvariable=status, wraplength=590, justify='left')
status_label.pack(anchor='w', pady=18)
bot_label = tk.StringVar(value='')
ttk.Label(frame, textvariable=bot_label, font=('Segoe UI', 12, 'bold')).pack(anchor='w')
buttons = ttk.Frame(frame)
buttons.pack(anchor='w', pady=15)
connect_button = ttk.Button(buttons, text='Подключить')
connect_button.pack(side='left', padx=(0, 12))
test_button = ttk.Button(buttons, text='START нажат → Проверить', state='disabled')
test_button.pack(side='left')
ttk.Label(frame, text='Напоминания работают, пока компьютер включён и ты вошёл в Windows.\n'
          'Задачи добавляй в Задачи.md. Время — Астана, UTC+05.',
          wraplength=590, justify='left').pack(anchor='w', pady=(10, 0))
pending_token = None


def finish(message):
    status.set(message)
    connect_button.configure(state='normal')


def connect():
    global pending_token
    value = token_entry.get().strip()
    if not re.fullmatch(r'\d{6,15}:[A-Za-z0-9_-]{25,100}', value):
        status.set('Вставь целый новый токен из BotFather в поле выше.')
        return
    destination = chat
    if destination is None:
        try:
            destination = int(chat_entry.get().strip())
            if destination <= 0:
                raise ValueError
        except ValueError:
            status.set('Укажи свой числовой Telegram ID для личного чата.')
            return
    connect_button.configure(state='disabled')
    test_button.configure(state='disabled')
    status.set('Проверяю отдельного бота…')

    def worker():
        global pending_token
        try:
            identity = api(value, 'getMe')
            username = identity.get('username', '')
            if not identity.get('is_bot') or not username:
                raise RuntimeError('Не удалось подтвердить бота.')
            if username.lower() == 'calcqxlove_bot':
                raise RuntimeError('Это бот оценок. Для задач нужен токен отдельного нового бота.')
            secrets_store.save(DATA / 'token.dpapi', value)
            settings = {'chat_id': destination, 'bot_username': username}
            atomic_json(DATA / 'settings.json', settings)
            atomic_json(DATA / 'setup-status.json', {'state': 'awaiting_start', 'bot_username': username,
                                                    'updated_at': time.time()})
            pending_token = None
            def saved():
                token_entry.delete(0, 'end')
                bot_label.set('@' + username)
                test_button.configure(state='normal')
                finish('3. Найди этого бота в Telegram и нажми START.\n'
                       '4. Нажми «START нажат → Проверить»: я отправлю одно пробное сообщение.')
            window.after(0, saved)
        except RuntimeError as error:
            message = str(error)
            window.after(0, lambda: finish(message))
        except Exception:
            window.after(0, lambda: finish('Настройка не сохранена. Повтори подключение.'))
    threading.Thread(target=worker, daemon=True).start()


def test():
    test_button.configure(state='disabled')
    status.set('Отправляю пробное напоминание…')
    def worker():
        try:
            settings = read_settings()
            token = secrets_store.load(DATA / 'token.dpapi')
            api(token, 'sendMessage', {'chat_id': int(settings['chat_id']),
                 'text': '✅ Obsidian подключён. Напоминания о задачах будут приходить сюда.\n'
                         'Добавляй задачи в Задачи.md и указывай 🔔 дату и время (Астана).',
                 'disable_web_page_preview': True})
            atomic_json(DATA / 'setup-status.json', {'state': 'delivery_verified',
                        'bot_username': settings['bot_username'], 'updated_at': time.time()})
            window.after(0, lambda: finish('Готово: пробное сообщение доставлено в Telegram.\n'
                                          'Можно закрыть это окно. Напоминания работают в фоне.'))
        except RuntimeError as error:
            message = str(error)
            def failed():
                test_button.configure(state='normal')
                finish(message)
            window.after(0, failed)
        except Exception:
            window.after(0, lambda: finish('Не удалось проверить доставку. Повтори подключение.'))
    threading.Thread(target=worker, daemon=True).start()


connect_button.configure(command=connect)
test_button.configure(command=test)
settings = read_settings()
if (DATA / 'token.dpapi').is_file() and settings.get('bot_username'):
    bot_label.set('@' + settings['bot_username'])
    test_button.configure(state='normal')
    status.set('Подключение сохранено. Нажми START у бота и проверь доставку, либо введи новый токен.')
window.mainloop()
