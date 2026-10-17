"""Offline integration checks; never reads credentials or calls Telegram."""
from pathlib import Path
import ctypes
import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

PACKAGE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PACKAGE))
import markdown_store as store


class MarkdownStoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='markdown-store-')
        self.root = Path(self.temp.name)
        self.path = self.root / 'Задачи.md'
        self.backup = self.root / 'private' / 'markdown-backups'
        self.original = b'\xef\xbb\xbf' + '# Notes\r\n\r\n- [ ] Проверить файл\r\n'.encode('utf-8')
        self.path.write_bytes(self.original)

    def tearDown(self):
        self.temp.cleanup()

    def snapshots(self):
        return list(self.backup.glob('*.original.md'))

    def test_bom_crlf_and_payload_preserved(self):
        def transform(text):
            return text.replace('Проверить файл', 'Проверить файл <!-- obs-task:demo -->'), ['task']
        result = store.update_markdown(self.path, self.backup, transform)
        self.assertEqual(result, ['task'])
        self.assertEqual(self.path.read_bytes(), self.original.replace('Проверить файл'.encode(), 'Проверить файл <!-- obs-task:demo -->'.encode()))
        self.assertEqual(len(self.snapshots()), 1)
        self.assertEqual(self.snapshots()[0].read_bytes(), self.original)
        metadata = json.loads(next(self.backup.glob('*.json')).read_text(encoding='utf-8'))
        self.assertEqual(metadata['source_path'], str(self.path.resolve()))
        self.assertEqual(metadata['recovery'], 'manual_only_compare_current_file_before_restoring')

    def test_noop_does_not_write_or_backup(self):
        before = self.path.stat().st_mtime_ns
        self.assertEqual(store.update_markdown(self.path, self.backup, lambda text: (text, 42)), 42)
        self.assertEqual(self.path.read_bytes(), self.original)
        self.assertEqual(self.path.stat().st_mtime_ns, before)
        self.assertFalse(self.backup.exists())

    def test_shorter_write_truncates_old_tail(self):
        store.update_markdown(self.path, self.backup, lambda text: ('X\n', None))
        self.assertEqual(self.path.read_bytes(), b'\xef\xbb\xbfX\n')
        self.assertEqual(self.snapshots()[0].read_bytes(), self.original)

    def test_missing_and_invalid_encoding_safe(self):
        with self.assertRaises(store.MarkdownMissing):
            store.update_markdown(self.root/'missing.md', self.backup, lambda text: (text, None))
        self.path.write_bytes(b'\xff\xfeinvalid')
        with self.assertRaises(store.MarkdownEncodingError) as caught:
            store.update_markdown(self.path, self.backup, lambda text: (text, None))
        self.assertEqual(str(caught.exception), 'markdown_encoding_invalid')
        self.assertEqual(self.path.read_bytes(), b'\xff\xfeinvalid')
        self.assertFalse(self.backup.exists())

    def test_transform_error_never_leaks_details_or_changes_original(self):
        def fail(text):
            raise RuntimeError('dummy-private-details')
        with self.assertRaises(store.MarkdownWriteError) as caught:
            store.update_markdown(self.path, self.backup, fail)
        self.assertEqual(str(caught.exception), 'markdown_write_error')
        self.assertEqual(self.path.read_bytes(), self.original)
        self.assertFalse(self.backup.exists())

    def test_backup_failure_prevents_document_write(self):
        with patch.object(store, '_save_backup', side_effect=OSError('dummy-private-details')):
            with self.assertRaises(store.MarkdownWriteError):
                store.update_markdown(self.path, self.backup, lambda text: ('Changed', None))
        self.assertEqual(self.path.read_bytes(), self.original)

    def test_partial_write_error_restores_original_under_same_handle(self):
        backend = store._WindowsFile if os.name == 'nt' else store._PosixFile
        original_write = backend.replace_bytes
        fault_used = False
        def fail_once(document, data):
            nonlocal fault_used
            if document.path == self.path and not fault_used:
                fault_used = True
                original_write(document, b'partial')
                raise OSError('dummy-private-details')
            original_write(document, data)
        with patch.object(backend, 'replace_bytes', fail_once):
            with self.assertRaises(store.MarkdownWriteError) as caught:
                store.update_markdown(self.path, self.backup, lambda text: ('Changed', None))
        self.assertTrue(fault_used)
        self.assertEqual(str(caught.exception), 'markdown_write_error')
        self.assertEqual(self.path.read_bytes(), self.original)
        self.assertEqual(self.snapshots()[0].read_bytes(), self.original)

    def test_process_crash_keeps_backup_but_later_user_edit_is_not_reverted(self):
        code = '''
import os,sys
from pathlib import Path
sys.path.insert(0,sys.argv[1])
import markdown_store as m
target=Path(sys.argv[2]); backup=Path(sys.argv[3])
backend=m._WindowsFile if os.name=='nt' else m._PosixFile
original=backend.replace_bytes
def crash(document,data):
    if document.path==target:
        original(document,b'partial-before-crash')
        os._exit(77)
    original(document,data)
backend.replace_bytes=crash
m.update_markdown(target,backup,lambda text: ('Changed',None))
'''
        completed = subprocess.run([sys.executable, '-c', code, str(PACKAGE), str(self.path), str(self.backup)], capture_output=True, timeout=20)
        self.assertEqual(completed.returncode, 77)
        self.assertEqual(self.path.read_bytes(), b'partial-before-crash')
        self.assertEqual(self.snapshots()[0].read_bytes(), self.original)
        self.path.write_text('Saved by user after crash\n', encoding='utf-8')
        user_bytes = self.path.read_bytes()
        store.update_markdown(self.path, self.backup, lambda text: (text, None))
        self.assertEqual(self.path.read_bytes(), user_bytes)

    @unittest.skipUnless(os.name == 'nt', 'Native Windows sharing contract')
    def test_native_handle_blocks_competing_write_replace_and_delete(self):
        kernel = store._windows_api()
        replacement = self.root/'replacement.tmp'
        replacement.write_bytes(b'concurrent replacement')
        observed = []
        def transform(text):
            # A genuine competing OS handle, no mocks and no file-content edits.
            handle = kernel.CreateFileW(str(self.path), 0x40000000, 7, None, 3, 0x80, None)
            code = ctypes.get_last_error()
            if handle != ctypes.c_void_p(-1).value:
                kernel.CloseHandle(handle)
                self.fail('Competing write unexpectedly opened')
            self.assertIn(code, (32, 33))
            observed.append('write blocked')
            with self.assertRaises(PermissionError):
                os.replace(replacement, self.path)
            observed.append('replace blocked')
            with self.assertRaises(PermissionError):
                self.path.unlink()
            observed.append('delete blocked')
            return text + 'Added safely\r\n', None
        store.update_markdown(self.path, self.backup, transform)
        self.assertEqual(observed, ['write blocked','replace blocked','delete blocked'])
        self.assertEqual(self.path.read_bytes(), self.original + b'Added safely\r\n')
        self.assertEqual(self.snapshots()[0].read_bytes(), self.original)
        # The handle is released after the update, so a later genuine save works.
        os.replace(replacement, self.path)
        self.assertEqual(self.path.read_bytes(), b'concurrent replacement')

    @unittest.skipUnless(os.name == 'nt', 'Native Windows sharing contract')
    def test_existing_native_writer_causes_busy_skip_without_transform(self):
        kernel = store._windows_api()
        handle = kernel.CreateFileW(str(self.path), 0x40000000, 7, None, 3, 0x80, None)
        self.assertNotEqual(handle, ctypes.c_void_p(-1).value)
        called = []
        try:
            with self.assertRaises(store.MarkdownBusy):
                store.update_markdown(self.path, self.backup, lambda text: (called.append(True) or 'Changed', None))
        finally:
            kernel.CloseHandle(handle)
        self.assertEqual(called, [])
        self.assertEqual(self.path.read_bytes(), self.original)
        self.assertFalse(self.backup.exists())


if __name__ == '__main__':
    unittest.main(verbosity=2)
