"""Preserve Markdown while adding IDs under an OS-held editing handle.

Windows uses CreateFileW with FILE_SHARE_READ only: competing writers and
delete/rename operations cannot enter between reading and writing. Existing
readers that do not share write access make the update busy, so the caller skips
it. POSIX flock is advisory and only serializes cooperating editors; identity
and content checks reject detected replacements by sync tools before success.

Before a changed file is written, its exact original bytes are flushed to a
unique backup in the caller's private backup directory. Backups are retained.
Restoration is attempted only in the same still-held handle after a write error.
No old backup is ever automatically restored on a subsequent run: after a crash,
inspect the backup and the current document before recovering manually.
"""
from __future__ import annotations

from contextlib import contextmanager
import ctypes
from ctypes import wintypes
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
from typing import Callable, Iterator, TypeVar
import uuid

T = TypeVar("T")


class MarkdownStoreError(Exception):
    code = "markdown_store_error"

    def __init__(self):
        super().__init__(self.code)


class MarkdownBusy(MarkdownStoreError):
    code = "markdown_busy"


class MarkdownEncodingError(MarkdownStoreError):
    code = "markdown_encoding_invalid"


class MarkdownMissing(MarkdownStoreError):
    code = "markdown_missing"


class MarkdownWriteError(MarkdownStoreError):
    code = "markdown_write_error"


_GENERIC_READ = 0x80000000
_GENERIC_WRITE = 0x40000000
_FILE_SHARE_READ = 0x00000001
_OPEN_EXISTING = 3
_CREATE_NEW = 1
_FILE_ATTRIBUTE_NORMAL = 0x00000080
_FILE_FLAG_WRITE_THROUGH = 0x80000000
_INVALID_HANDLE_VALUE = ctypes.c_void_p(-1).value
_CHUNK = 1024 * 1024


def _windows_api():
    # Explicit pointer-width signatures avoid truncating HANDLE on 64-bit Python.
    api = ctypes.WinDLL("kernel32", use_last_error=True)
    api.CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD,
                               ctypes.c_void_p, wintypes.DWORD, wintypes.DWORD,
                               wintypes.HANDLE]
    api.CreateFileW.restype = wintypes.HANDLE
    api.ReadFile.argtypes = [wintypes.HANDLE, ctypes.c_void_p, wintypes.DWORD,
                            ctypes.POINTER(wintypes.DWORD), ctypes.c_void_p]
    api.ReadFile.restype = wintypes.BOOL
    api.WriteFile.argtypes = list(api.ReadFile.argtypes)
    api.WriteFile.restype = wintypes.BOOL
    api.SetFilePointerEx.argtypes = [wintypes.HANDLE, ctypes.c_longlong,
                                    ctypes.POINTER(ctypes.c_longlong), wintypes.DWORD]
    api.SetFilePointerEx.restype = wintypes.BOOL
    api.SetEndOfFile.argtypes = [wintypes.HANDLE]
    api.SetEndOfFile.restype = wintypes.BOOL
    api.FlushFileBuffers.argtypes = [wintypes.HANDLE]
    api.FlushFileBuffers.restype = wintypes.BOOL
    api.CloseHandle.argtypes = [wintypes.HANDLE]
    api.CloseHandle.restype = wintypes.BOOL
    return api


def _extended_path(path: Path) -> str:
    value = str(path.resolve())
    if value.startswith("\\\\?\\"):
        return value
    if value.startswith("\\\\"):
        return "\\\\?\\UNC\\" + value[2:]
    return "\\\\?\\" + value


def _raise_open_error(code: int):
    if code in (32, 33):  # ERROR_SHARING_VIOLATION / ERROR_LOCK_VIOLATION
        raise MarkdownBusy() from None
    if code in (2, 3):
        raise MarkdownMissing() from None
    raise MarkdownWriteError() from None


class _WindowsFile:
    def __init__(self, path: Path, *, create: bool = False):
        self.path = path
        self.api = _windows_api()
        self.handle = self.api.CreateFileW(
            _extended_path(path), _GENERIC_READ | _GENERIC_WRITE, _FILE_SHARE_READ,
            None, _CREATE_NEW if create else _OPEN_EXISTING,
            _FILE_ATTRIBUTE_NORMAL | (_FILE_FLAG_WRITE_THROUGH if create else 0),
            None,
        )
        if self.handle == _INVALID_HANDLE_VALUE:
            _raise_open_error(ctypes.get_last_error())

    def close(self):
        if self.handle is not None:
            self.api.CloseHandle(self.handle)
            self.handle = None

    def read_bytes(self) -> bytes:
        parts = []
        buffer = ctypes.create_string_buffer(_CHUNK)
        amount = wintypes.DWORD()
        if not self.api.SetFilePointerEx(self.handle, 0, None, 0):
            raise MarkdownWriteError()
        while True:
            if not self.api.ReadFile(self.handle, buffer, _CHUNK, ctypes.byref(amount), None):
                raise MarkdownWriteError()
            if not amount.value:
                return b"".join(parts)
            parts.append(buffer.raw[:amount.value])

    def replace_bytes(self, data: bytes):
        # In place: do not replace the directory entry or release the share lock.
        if not self.api.SetFilePointerEx(self.handle, 0, None, 0):
            raise MarkdownWriteError()
        offset = 0
        while offset < len(data):
            chunk = data[offset:offset + _CHUNK]
            buffer = ctypes.create_string_buffer(chunk)
            amount = wintypes.DWORD()
            if not self.api.WriteFile(self.handle, buffer, len(chunk), ctypes.byref(amount), None):
                raise MarkdownWriteError()
            if not amount.value:
                raise MarkdownWriteError()
            offset += amount.value
        if not self.api.SetEndOfFile(self.handle):
            raise MarkdownWriteError()
        if not self.api.FlushFileBuffers(self.handle):
            raise MarkdownWriteError()


class _PosixFile:
    def __init__(self, path: Path):
        import fcntl
        self.path = path
        self.stream = path.open("r+b", buffering=0)
        try:
            fcntl.flock(self.stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            self.stream.close()
            raise MarkdownBusy() from None
        info = os.fstat(self.stream.fileno())
        self.identity = (info.st_dev, info.st_ino)

    def close(self):
        self.stream.close()

    def read_bytes(self) -> bytes:
        self.stream.seek(0)
        return self.stream.read()

    def assert_current(self, expected: bytes | None = None):
        """flock cannot stop another process atomically replacing the path."""
        try:
            info = os.stat(self.path, follow_symlinks=False)
        except OSError:
            raise MarkdownBusy() from None
        if (info.st_dev, info.st_ino) != self.identity:
            raise MarkdownBusy()
        if expected is not None and self.read_bytes() != expected:
            raise MarkdownBusy()
        # Recheck after reading: rename could happen during the content check.
        if expected is not None:
            self.assert_current()

    def replace_bytes(self, data: bytes):
        self.assert_current()
        self.stream.seek(0)
        offset = 0
        while offset < len(data):
            amount = self.stream.write(data[offset:offset + _CHUNK])
            if not amount:
                raise MarkdownWriteError()
            offset += amount
        self.stream.truncate()
        self.stream.flush()
        os.fsync(self.stream.fileno())
        self.assert_current(data)


@contextmanager
def _editing_file(path: Path) -> Iterator[_WindowsFile | _PosixFile]:
    document = None
    try:
        document = _WindowsFile(path) if os.name == "nt" else _PosixFile(path)
        yield document
    except MarkdownStoreError:
        raise
    except FileNotFoundError:
        raise MarkdownMissing() from None
    except Exception:
        raise MarkdownWriteError() from None
    finally:
        if document is not None:
            document.close()


def _durable_new_file(path: Path, data: bytes):
    if os.name == "nt":
        document = _WindowsFile(path, create=True)
        try:
            document.replace_bytes(data)
        finally:
            document.close()
    else:
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())


def _save_backup(path: Path, backup_dir: Path, original: bytes, updated: bytes):
    backup_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    stem = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ") + "-" + uuid.uuid4().hex
    # A uniquely named complete snapshot, never an overwritten rolling backup.
    _durable_new_file(backup_dir / (stem + ".original.md"), original)
    metadata = {
        "format": 1, "source_path": str(path.resolve()),
        "original_sha256": hashlib.sha256(original).hexdigest(),
        "updated_sha256": hashlib.sha256(updated).hexdigest(),
        "recovery": "manual_only_compare_current_file_before_restoring",
    }
    _durable_new_file(backup_dir / (stem + ".json"),
                      (json.dumps(metadata, ensure_ascii=False, indent=2) + "\n").encode("utf-8"))
    if os.name != "nt":
        descriptor = os.open(backup_dir, os.O_RDONLY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)


def update_markdown(path: Path | str, backup_dir: Path | str,
                    transform: Callable[[str], tuple[str, T]]) -> T:
    """Read, transform and persist under one editing handle; return payload.

    Preserve UTF-8 BOM and all newline bytes that the transform retains. No-op
    transformations create no backup. Errors expose only static safe codes.
    """
    try:
        path, backup_dir = Path(path), Path(backup_dir)
        with _editing_file(path) as document:
            original = document.read_bytes()
            bom = original.startswith(b"\xef\xbb\xbf")
            try:
                text = original.decode("utf-8-sig")
            except UnicodeError:
                raise MarkdownEncodingError() from None
            updated_text, payload = transform(text)
            if not isinstance(updated_text, str):
                raise MarkdownWriteError()
            updated = (b"\xef\xbb\xbf" if bom else b"") + updated_text.encode("utf-8")
            if updated == original:
                if isinstance(document, _PosixFile):
                    document.assert_current(original)
                return payload
            _save_backup(path, backup_dir, original, updated)
            if isinstance(document, _PosixFile):
                # Advisory locks do not exclude an in-place editor. Confirm the
                # snapshot again after the backup and before changing any bytes.
                document.assert_current(original)
            try:
                document.replace_bytes(updated)
            except BaseException as error:
                if isinstance(document, _PosixFile) and isinstance(error, MarkdownBusy):
                    # The path or its bytes now belong to a concurrent editor.
                    # Never roll an old snapshot over that newer version.
                    raise
                # Same exclusive handle: no later editor's bytes can be restored
                # over. A process/power crash instead leaves the durable backup.
                try:
                    document.replace_bytes(original)
                except BaseException:
                    pass
                raise MarkdownWriteError() from None
            return payload
    except MarkdownStoreError:
        raise
    except Exception:
        raise MarkdownWriteError() from None
