"""One worker for Obsidian reminders and the owner's Telegram task views."""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import time

from reminders import AlreadyRunning, Config, TaskEngine, WorkerLock
from bot_controls import BotControls


class BotService:
    def __init__(self, config_path, *, engine=None, controls=None):
        self.engine = engine or TaskEngine(config_path)
        self.controls = controls or BotControls(self.engine.data_dir, self.control_status,
                                                complete_task_handler=self.complete_task,
                                                task_provider=self.task_list)
        self.last_sync = None
        self.drain_pending = False
        self.status_path = self.engine.data_dir / 'service-status.json'

    def control_status(self):
        self.engine.fresh_tasks(_lock_held=True)
        snapshot = self.engine.status()
        counts = snapshot.get('counts', {})
        return {'counts': {'active': counts.get('active', 0),
                           'pending': counts.get('pending', 0),
                           'leased': counts.get('leased', 0)},
                'next_due': snapshot.get('next_due'),
                'repeat_minutes': self.engine.config.repeat_seconds // 60,
                'last_run': snapshot.get('last_run'),
                'error': snapshot.get('error')}

    def complete_task(self, task_id, expected_version):
        return self.engine.complete_task(task_id, expected_version, _lock_held=True)

    def task_list(self):
        # Read the current synchronized file for every view, without sending
        # reminders or returning a cached list after a failed file read.
        return self.engine.fresh_tasks(_lock_held=True)

    def step(self, *, monotonic=None):
        clock = time.monotonic() if monotonic is None else monotonic
        token, chat_id, auth_error = self.engine._auth()
        control_result = {'error': auth_error}
        if token and chat_id:
            try:
                control_result = self.controls.tick(token, chat_id)
            except Exception:
                control_result = {'error': 'controls_error'}
        engine_error = None
        if (self.last_sync is None or self.drain_pending
                or clock - self.last_sync >= self.engine.config.poll_seconds):
            try:
                # Give buttons a turn between every two deliveries. A backlog
                # keeps draining on the next loop instead of waiting 20 seconds.
                result = self.engine.run_once(_lock_held=True, max_deliveries=1)
                engine_error = result.get('error')
                self.drain_pending = bool(result.get('sent_this_run') or result.get('failed_this_run'))
            except Exception:
                engine_error = 'engine_error'
                self.drain_pending = False
            self.last_sync = clock
        # No input messages, task text, API bodies or credentials in worker status.
        snapshot = {'last_loop': datetime.now(timezone.utc).isoformat(),
                    'telegram_configured': bool(token and chat_id),
                    'engine_error': engine_error or self.engine.status().get('error'),
                    'controls_error': control_result.get('error'),
                    'repeat_seconds': self.engine.config.repeat_seconds,
                    'task_poll_seconds': self.engine.config.poll_seconds}
        temporary = self.status_path.with_suffix('.tmp')
        temporary.write_text(json.dumps(snapshot, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
        os.replace(temporary, self.status_path)
        return snapshot

    def publish_menu(self):
        token, chat_id, error = self.engine._auth()
        if error or not token or not chat_id:
            return {'status': 'failed', 'error': error or 'telegram_not_configured'}
        try:
            result = self.controls.publish_menu(token, chat_id)
            if result.get('error') or result.get('published') is not True:
                return {'status': 'failed', 'error': result.get('error') or 'menu_delivery_failed'}
            return {'status': 'menu_sent', 'result': result}
        except Exception:
            return {'status': 'failed', 'error': 'menu_delivery_failed'}

    def watch(self, *, _lock_held=False):
        if not _lock_held:
            with WorkerLock(self.engine.lock_path):
                return self.watch(_lock_held=True)
        failures = 0
        while True:
            started = time.monotonic()
            result = self.step()
            if result.get('controls_error'):
                failures += 1
                delay = min(30, 2 ** min(failures - 1, 5))
            else:
                failures = 0
                # getUpdates already waits for new input. Do not add two
                # seconds to every response; only guard an instant-empty poll.
                delay = max(0, 0.1 - (time.monotonic() - started))
            if delay:
                time.sleep(delay)


def main(argv=None):
    parser = argparse.ArgumentParser(description='Obsidian Telegram reminder service')
    parser.add_argument('--config', type=Path, default=Path(__file__).with_name('config.json'))
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument('--watch', action='store_true')
    mode.add_argument('--once', action='store_true')
    mode.add_argument('--menu', action='store_true')
    args = parser.parse_args(argv)
    try:
        config = Config.read(args.config)
        # Acquire before constructors: controls recovery and schema migration
        # must never run concurrently with the live worker's in-flight send.
        with WorkerLock(config.data_directory / 'worker.lock'):
            service = BotService(args.config)
            if args.watch:
                service.watch(_lock_held=True)
                return 0
            result = service.publish_menu() if args.menu else service.step()
        print(json.dumps(result, ensure_ascii=True))
        return int(result.get('status') == 'failed')
    except AlreadyRunning:
        print('{"status":"busy"}')
        return 0
    except KeyboardInterrupt:
        return 0
    except Exception:
        print('{"status":"failed","error":"service_error"}')
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
