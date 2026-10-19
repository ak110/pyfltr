"""プロセス間の排他に使うファイルロック。

実行アーカイブの稼働中判定（`pyfltr.state.archive`）と検証の実行枠（`pyfltr.command.execution_slot`）が共用する。
取得は待機しない1回の試行とし、待機・中断・通知は呼出元が再試行の間隔で組み立てる。

ロックは開いたファイルごとに独立する。POSIXの`flock`もWindowsの`msvcrt.locking`も、
同じプロセスの別スレッドが別に開いた同じパスを保持中として扱う。
`os.open`が返す記述子は継承不可のため、子プロセスはロックを引き継がない。
保持者のプロセスが強制終了した場合はOSがロックを解放する。
"""

import contextlib
import os
import pathlib
import sys

if sys.platform == "win32":
    import msvcrt  # pylint: disable=import-error  # Windows専用の標準モジュールで、他のOSでは存在しない
else:
    import fcntl


def _lock(fd: int) -> None:
    """記述子へ排他ロックを非待機で掛ける。保持中なら`OSError`を送出する。"""
    if sys.platform == "win32":
        os.lseek(fd, 0, os.SEEK_SET)
        msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
    else:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)


def _unlock(fd: int) -> None:
    """記述子のロックを外す。"""
    if sys.platform == "win32":
        os.lseek(fd, 0, os.SEEK_SET)
        msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
    else:
        fcntl.flock(fd, fcntl.LOCK_UN)


class FileLock:
    """ファイル1件に対する排他ロック。"""

    def __init__(self, path: pathlib.Path) -> None:
        self._path = path
        self._fd: int | None = None

    @property
    def path(self) -> pathlib.Path:
        """ロックファイルのパス。"""
        return self._path

    def try_acquire(self) -> bool:
        """ロックを非待機で取得し、取得できたかを返す。親ディレクトリが無ければ作成する。"""
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
        """ロックを解放する。`remove=True`ではロックファイルも削除する。未取得なら何もしない。

        POSIXでは保持したまま削除してから閉じ、削除までの間に別の主体が同じファイルを取得できないようにする。
        Windowsは保持中のファイルを削除できないため閉じてから削除し、別の主体が開いていれば削除を諦める。
        """
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
    """他の主体がロックを保持中かを返す。ファイルが無ければ保持者なしとして`False`を返す。

    Windowsでは削除待ちのファイルを開くと`PermissionError`になる。削除は保持者の解放後に行うため、保持者なしとして扱う。
    """
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
