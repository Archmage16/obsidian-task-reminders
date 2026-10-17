"""Offline POSIX races using temporary Markdown files; no vault or credentials."""
import importlib.util
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock

sys.dont_write_bytecode = True
SOURCE = Path(os.environ.get("OBSIDIAN_TASKS_SOURCE", str(
    Path(__file__).resolve().parents[1])))
spec = importlib.util.spec_from_file_location("race_markdown_store", SOURCE / "markdown_store.py")
store = importlib.util.module_from_spec(spec)
spec.loader.exec_module(store)


@unittest.skipIf(os.name == "nt", "POSIX inode/flock behavior requires Linux")
class PosixSyncRaceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.path = self.root / "Tasks.md"
        self.original = b"- [ ] target\r\n"
        self.incoming = b"- [ ] incoming edit\r\n"
        self.path.write_bytes(self.original)
        self.backups = self.root / "backups"

    def replace_incoming(self):
        incoming = self.root / "syncthing-incoming.tmp"
        incoming.write_bytes(self.incoming)
        os.replace(incoming, self.path)

    def transform(self, text):
        return text.replace("[ ]", "[x]"), {"status": "completed"}

    def test_normal_write_returns_payload_and_keeps_backup(self):
        result = store.update_markdown(self.path, self.backups, self.transform)
        self.assertEqual(result, {"status": "completed"})
        self.assertEqual(self.path.read_bytes(), b"- [x] target\r\n")
        self.assertEqual(next(self.backups.glob("*.original.md")).read_bytes(), self.original)

    def test_atomic_replace_before_write_is_busy_and_preserves_incoming(self):
        def transform(text):
            self.replace_incoming()
            return self.transform(text)
        with self.assertRaises(store.MarkdownBusy):
            store.update_markdown(self.path, self.backups, transform)
        self.assertEqual(self.path.read_bytes(), self.incoming)

    def test_inplace_change_before_write_is_busy_and_preserves_incoming(self):
        def transform(text):
            self.path.write_bytes(self.incoming)
            return self.transform(text)
        with self.assertRaises(store.MarkdownBusy):
            store.update_markdown(self.path, self.backups, transform)
        self.assertEqual(self.path.read_bytes(), self.incoming)

    def test_inplace_change_after_backup_is_busy_and_preserves_incoming(self):
        real_backup = store._save_backup
        def backup_then_edit(*args):
            real_backup(*args)
            self.path.write_bytes(self.incoming)
        with mock.patch.object(store, '_save_backup', side_effect=backup_then_edit):
            with self.assertRaises(store.MarkdownBusy):
                store.update_markdown(self.path, self.backups, self.transform)
        self.assertEqual(self.path.read_bytes(), self.incoming)
        self.assertEqual(next(self.backups.glob('*.original.md')).read_bytes(), self.original)

    def test_atomic_replace_after_write_is_busy_without_rollback(self):
        identity = self.path.stat()
        real_fsync = os.fsync
        replaced = False
        def fsync(descriptor):
            nonlocal replaced
            real_fsync(descriptor)
            info = os.fstat(descriptor)
            if not replaced and (info.st_dev, info.st_ino) == (identity.st_dev, identity.st_ino):
                replaced = True
                self.replace_incoming()
        with mock.patch.object(store.os, "fsync", side_effect=fsync):
            with self.assertRaises(store.MarkdownBusy):
                store.update_markdown(self.path, self.backups, self.transform)
        self.assertTrue(replaced)
        self.assertEqual(self.path.read_bytes(), self.incoming)

    def test_inplace_change_after_write_is_busy_without_rollback(self):
        identity = self.path.stat()
        real_fsync = os.fsync
        changed = False
        def fsync(descriptor):
            nonlocal changed
            real_fsync(descriptor)
            info = os.fstat(descriptor)
            if not changed and (info.st_dev, info.st_ino) == (identity.st_dev, identity.st_ino):
                changed = True
                self.path.write_bytes(self.incoming)
        with mock.patch.object(store.os, "fsync", side_effect=fsync):
            with self.assertRaises(store.MarkdownBusy):
                store.update_markdown(self.path, self.backups, self.transform)
        self.assertTrue(changed)
        self.assertEqual(self.path.read_bytes(), self.incoming)

    def test_noop_after_atomic_replace_never_returns_false_payload(self):
        def transform(text):
            self.replace_incoming()
            return text, {"status": "already_completed"}
        with self.assertRaises(store.MarkdownBusy):
            store.update_markdown(self.path, self.backups, transform)
        self.assertEqual(self.path.read_bytes(), self.incoming)
        self.assertFalse(self.backups.exists())


if __name__ == "__main__":
    unittest.main(verbosity=2)
