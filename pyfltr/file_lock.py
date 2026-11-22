import contextlib
import os
import pathlib
import sys

if sys.platform == "win32":
    import msvcrt  # pylint: disable=import-error  # Windows専用の標準モジュールで、他のOSでは存在しない
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
