import contextlib
import os
import pathlib
import sys

if sys.platform == "win32":
    import ctypes.wintypes
    import msvcrt  # pylint: disable=import-error  # Windows専用の標準モジュールで、他のOSでは存在しない

    class _Overlapped(ctypes.Structure):
        _fields_ = [
            ("internal", ctypes.c_size_t),
            ("internal_high", ctypes.c_size_t),
            ("offset", ctypes.wintypes.DWORD),
            ("offset_high", ctypes.wintypes.DWORD),
            ("event", ctypes.wintypes.HANDLE),
        ]

    _kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    _lock_file_ex = _kernel32.LockFileEx
    _lock_file_ex.argtypes = [
        ctypes.wintypes.HANDLE,
        ctypes.wintypes.DWORD,
        ctypes.wintypes.DWORD,
        ctypes.wintypes.DWORD,
        ctypes.wintypes.DWORD,
        ctypes.POINTER(_Overlapped),
    ]
    _lock_file_ex.restype = ctypes.wintypes.BOOL
    _unlock_file_ex = _kernel32.UnlockFileEx
    _unlock_file_ex.argtypes = [
        ctypes.wintypes.HANDLE,
        ctypes.wintypes.DWORD,
        ctypes.wintypes.DWORD,
        ctypes.wintypes.DWORD,
        ctypes.POINTER(_Overlapped),
    ]
    _unlock_file_ex.restype = ctypes.wintypes.BOOL
else:
    import fcntl  # pylint: disable=import-error  # POSIX専用の標準モジュールで、Windowsでは存在しない


def _lock(fd: int) -> None:
    if sys.platform == "win32":
        os.lseek(fd, 0, os.SEEK_SET)
        msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
    else:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)


def _unlock(fd: int) -> None:
    if sys.platform == "win32":
        os.lseek(fd, 0, os.SEEK_SET)
        msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
    else:
        fcntl.flock(fd, fcntl.LOCK_UN)


def _lock_shared(fd: int) -> None:
    if sys.platform == "win32":
        lockfile_fail_immediately = 1
        if not _lock_file_ex(msvcrt.get_osfhandle(fd), lockfile_fail_immediately, 0, 1, 0, ctypes.byref(_Overlapped())):
            raise ctypes.WinError(ctypes.get_last_error())
    else:
        fcntl.flock(fd, fcntl.LOCK_SH | fcntl.LOCK_NB)


def _unlock_shared(fd: int) -> None:
    if sys.platform == "win32":
        if not _unlock_file_ex(msvcrt.get_osfhandle(fd), 0, 1, 0, ctypes.byref(_Overlapped())):
            raise ctypes.WinError(ctypes.get_last_error())
    else:
        _unlock(fd)


class FileLock:
    def __init__(self, path: pathlib.Path) -> None:
        self._path = path
        self._fd: int | None = None

    @property
    def path(self) -> pathlib.Path:
        return self._path

    def try_acquire(self) -> bool:
        if self._fd is not None:
            raise RuntimeError(f"ロックを取得済みです: {self._path}")
        self._path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(self._path, os.O_RDWR | os.O_CREAT, 0o644)
        try:
            _lock(fd)
        except OSError:
            os.close(fd)
            return False
        self._fd = fd
        return True

    def release(self, *, remove: bool = False) -> None:
        fd = self._fd
        if fd is None:
            return
        self._fd = None
        if remove and sys.platform != "win32":
            with contextlib.suppress(OSError):
                self._path.unlink()
        try:
            _unlock(fd)
        finally:
            os.close(fd)
        if remove and sys.platform == "win32":
            with contextlib.suppress(OSError):
                self._path.unlink()


def is_locked(path: pathlib.Path) -> bool:
    try:
        fd = os.open(path, os.O_RDWR)
    except (FileNotFoundError, PermissionError):
        return False
    try:
        _lock(fd)
    except OSError:
        return True
    else:
        _unlock(fd)
        return False
    finally:
        os.close(fd)


def probe_lock(path: pathlib.Path) -> bool | None:
    try:
        fd = os.open(path, os.O_RDONLY)
    except FileNotFoundError:
        return False
    except OSError:
        return None
    try:
        _lock_shared(fd)
    except BlockingIOError:
        return True
    except OSError as error:
        error_lock_violation = 33
        if sys.platform == "win32" and getattr(error, "winerror", None) == error_lock_violation:
            return True
        return None
    else:
        try:
            _unlock_shared(fd)
        except OSError:
            return None
        return False
    finally:
        os.close(fd)
