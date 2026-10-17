"""Current-user encrypted secrets: Windows DPAPI or private Linux Fernet files.

Linux uses token.enc and a sibling .secret.key. Both files must be owned by the
running user with mode 0600, inside an owned 0700 directory. Existing directories
with broader permissions are rejected rather than silently changed.
"""
from contextlib import contextmanager
import ctypes
import os
import secrets
import stat
import tempfile
from ctypes import wintypes
from pathlib import Path


class Blob(ctypes.Structure):
    _fields_ = [('size', wintypes.DWORD), ('data', ctypes.POINTER(ctypes.c_char))]


class SecretStoreError(RuntimeError):
    """A static error code; never include secret values, paths or raw exceptions."""


def transform(value: bytes, encrypt: bool = True) -> bytes:
    if os.name != 'nt':
        raise RuntimeError('Эта настройка предназначена для Windows.')
    buffer = ctypes.create_string_buffer(value)
    source = Blob(len(value), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_char)))
    result = Blob()
    crypt = ctypes.WinDLL('crypt32', use_last_error=True)
    if encrypt:
        okay = crypt.CryptProtectData(ctypes.byref(source), 'ObsidianTasks', None,
                                      None, None, 1, ctypes.byref(result))
    else:
        okay = crypt.CryptUnprotectData(ctypes.byref(source), None, None, None,
                                        None, 1, ctypes.byref(result))
    if not okay:
        raise RuntimeError('Windows не смог открыть секрет. Подключи бота заново.')
    try:
        return ctypes.string_at(result.data, result.size)
    finally:
        kernel = ctypes.WinDLL('kernel32', use_last_error=True)
        kernel.LocalFree.argtypes = [ctypes.c_void_p]
        kernel.LocalFree.restype = ctypes.c_void_p
        kernel.LocalFree(result.data)


def _windows_save(path, value: str) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix='.secret-', dir=path.parent)
    try:
        with os.fdopen(descriptor, 'wb') as stream:
            stream.write(transform(value.encode('utf-8')))
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def _windows_load(path) -> str:
    return transform(Path(path).read_bytes(), False).decode('utf-8')


def _fernet_class():
    try:
        from cryptography.fernet import Fernet
    except ImportError:
        raise SecretStoreError('fernet_unavailable') from None
    return Fernet


@contextmanager
def _linux_directory(path, *, create: bool):
    """Traverse with directory handles; refuse symlinks at every component."""
    absolute = os.path.abspath(os.fspath(path))
    parent, name = os.path.split(absolute)
    if not name or name in ('.', '..', '.secret.key'):
        raise SecretStoreError('secret_path_invalid')
    if name.lower().endswith('.dpapi'):
        raise SecretStoreError('secret_platform_mismatch')
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
    descriptor = None
    try:
        descriptor = os.open('/', flags)
        for part in Path(parent).parts[1:]:
            try:
                following = os.open(part, flags, dir_fd=descriptor)
            except FileNotFoundError:
                if not create:
                    raise SecretStoreError('secret_directory_missing') from None
                try:
                    os.mkdir(part, 0o700, dir_fd=descriptor)
                except FileExistsError:
                    pass  # Another provisioner may have created it; re-open safely.
                following = os.open(part, flags, dir_fd=descriptor)
            os.close(descriptor)
            descriptor = following
        info = os.fstat(descriptor)
        if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.geteuid():
            raise SecretStoreError('secret_owner_invalid')
        if stat.S_IMODE(info.st_mode) != 0o700:
            raise SecretStoreError('secret_permissions_invalid')
        yield descriptor, name
    except SecretStoreError:
        raise
    except OSError:
        raise SecretStoreError('secret_directory_invalid') from None
    finally:
        if descriptor is not None:
            os.close(descriptor)


def _check_private_file(info):
    if not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid():
        raise SecretStoreError('secret_owner_invalid')
    if stat.S_IMODE(info.st_mode) != 0o600:
        raise SecretStoreError('secret_permissions_invalid')


def _read_private_file(directory, name: str, *, maximum: int = 131072) -> bytes:
    # NONBLOCK prevents a substituted FIFO from hanging before the file-type check.
    flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK
    descriptor = os.open(name, flags, dir_fd=directory)
    try:
        _check_private_file(os.fstat(descriptor))
        chunks = []
        length = 0
        while True:
            chunk = os.read(descriptor, min(8192, maximum + 1 - length))
            if not chunk:
                break
            chunks.append(chunk)
            length += len(chunk)
            if length > maximum:
                raise SecretStoreError('secret_format_invalid')
        return b''.join(chunks)
    finally:
        os.close(descriptor)


def _write_private_temporary(directory, value: bytes, prefix: str) -> str:
    name = prefix + secrets.token_hex(16) + '.tmp'
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC
    descriptor = os.open(name, flags, 0o600, dir_fd=directory)
    try:
        os.fchmod(descriptor, 0o600)
        _check_private_file(os.fstat(descriptor))
        view = memoryview(value)
        while view:
            count = os.write(descriptor, view)
            if count <= 0:
                raise SecretStoreError('secret_write_failed')
            view = view[count:]
        os.fsync(descriptor)
    except BaseException:
        os.close(descriptor)
        os.unlink(name, dir_fd=directory)
        raise
    else:
        os.close(descriptor)
    return name


def _linux_key(directory, *, create: bool) -> bytes:
    try:
        return _read_private_file(directory, '.secret.key', maximum=4096)
    except FileNotFoundError:
        if not create:
            raise SecretStoreError('secret_key_missing') from None
    candidate = _fernet_class().generate_key()
    temporary = _write_private_temporary(directory, candidate, '.key-')
    try:
        # link() publishes an already complete/fsynced key atomically and refuses
        # to replace a key won by a concurrent provisioner. rename() would not.
        try:
            os.link(temporary, '.secret.key', src_dir_fd=directory,
                    dst_dir_fd=directory, follow_symlinks=False)
        except FileExistsError:
            pass
    finally:
        os.unlink(temporary, dir_fd=directory)
        os.fsync(directory)
    return _read_private_file(directory, '.secret.key', maximum=4096)


def _linux_save(path, value: str) -> None:
    temporary = None
    try:
        with _linux_directory(path, create=True) as (directory, name):
            try:
                existing = os.stat(name, dir_fd=directory, follow_symlinks=False)
            except FileNotFoundError:
                pass
            else:
                _check_private_file(existing)
            key = _linux_key(directory, create=True)
            encrypted = _fernet_class()(key).encrypt(value.encode('utf-8'))
            temporary = _write_private_temporary(directory, encrypted, '.secret-')
            try:
                os.replace(temporary, name, src_dir_fd=directory, dst_dir_fd=directory)
                temporary = None
                os.fsync(directory)
            finally:
                if temporary is not None:
                    os.unlink(temporary, dir_fd=directory)
    except SecretStoreError:
        raise
    except Exception:
        raise SecretStoreError('secret_write_failed') from None


def _linux_load(path) -> str:
    try:
        with _linux_directory(path, create=False) as (directory, name):
            key = _linux_key(directory, create=False)
            encrypted = _read_private_file(directory, name)
            return _fernet_class()(key).decrypt(encrypted).decode('utf-8')
    except SecretStoreError:
        raise
    except FileNotFoundError:
        raise SecretStoreError('secret_file_missing') from None
    except Exception:
        raise SecretStoreError('secret_decryption_failed') from None


def save(path, value: str) -> None:
    if os.name == 'nt':
        _windows_save(path, value)
    else:
        _linux_save(path, value)


def load(path) -> str:
    if os.name == 'nt':
        return _windows_load(path)
    return _linux_load(path)
