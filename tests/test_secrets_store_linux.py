"""Linux-only encrypted storage contracts; synthetic values and temporary files."""
from concurrent.futures import ThreadPoolExecutor
import os
from pathlib import Path
import stat
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import secrets_store as store


@unittest.skipUnless(sys.platform.startswith('linux'), 'Linux ownership/nofollow contract')
class LinuxSecretTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='secret-store-offline-')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.root.chmod(0o700)
        self.path = self.root / 'token.enc'

    def test_roundtrip_private_modes_and_ciphertext(self):
        value = 'offline-dummy-value'
        store.save(self.path, value)
        self.assertEqual(store.load(self.path), value)
        self.assertNotIn(value.encode(), self.path.read_bytes())
        self.assertEqual(stat.S_IMODE(self.path.stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE((self.root/'.secret.key').stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(self.root.stat().st_mode), 0o700)

    def test_broad_directory_permissions_rejected_without_write(self):
        self.root.chmod(0o755)
        with self.assertRaisesRegex(store.SecretStoreError, '^secret_permissions_invalid$'):
            store.save(self.path, 'offline-dummy-value')
        self.assertFalse(self.path.exists())

    def test_broad_key_permissions_rejected(self):
        store.save(self.path, 'offline-dummy-value')
        (self.root/'.secret.key').chmod(0o644)
        with self.assertRaisesRegex(store.SecretStoreError, '^secret_permissions_invalid$'):
            store.load(self.path)

    def test_token_symlink_rejected_and_target_preserved(self):
        other = self.root/'untouched.txt'
        other.write_text('unchanged', encoding='utf-8')
        self.path.symlink_to(other)
        with self.assertRaises(store.SecretStoreError):
            store.save(self.path, 'offline-dummy-value')
        self.assertEqual(other.read_text(), 'unchanged')

    def test_missing_and_wrong_key_fail_safely(self):
        store.save(self.path, 'offline-dummy-value')
        key = self.root/'.secret.key'
        key.unlink()
        with self.assertRaisesRegex(store.SecretStoreError, '^secret_key_missing$'):
            store.load(self.path)
        key.write_bytes(store._fernet_class().generate_key())
        key.chmod(0o600)
        with self.assertRaisesRegex(store.SecretStoreError, '^secret_decryption_failed$'):
            store.load(self.path)

    def test_concurrent_first_writers_share_one_complete_key(self):
        values = ['offline-first', 'offline-second']
        with ThreadPoolExecutor(max_workers=2) as executor:
            list(executor.map(lambda value: store.save(self.path, value), values))
        self.assertIn(store.load(self.path), values)
        self.assertEqual(len((self.root/'.secret.key').read_bytes()), 44)
        self.assertFalse(list(self.root.glob('*.tmp')))


if __name__ == '__main__':
    unittest.main()
