"""検証の実行枠。

同一ホスト・同一OSアカウントで、独立した検証が外部ツールを実行する区間を同時に1つだけにする。
共有の一時領域・inotify上限・CPUなどの資源は起動ごとの並列設定（`jobs`など）では調停できず、
別worktree・別リポジトリ・MCPから起動した検証の並列数がホスト上で積み上がるためである。

pyfltrは`pyfltr.command.stage_runner.run_stages`を`hold`の内側で実行し、
起動済みのツールを回収した区切りで待機者へ譲る。
CLI・TUI・MCPの全実行経路とcommit hookからの起動が同じ枠へ参加する。
pyfltr以外の検証を起動する呼出元（他のツールのスクリプトなど）も、同じ`hold`で枠を共有できる::

    import pyfltr.command.execution_slot

    with pyfltr.command.execution_slot.hold(label="my-check"):
        subprocess.run([...], check=True)

取得規則:

- 同じスレッドが保持中なら入れ子として再入する
- 保持者記録のプロセスが呼出元プロセスの祖先なら、取得せずに所有を継承する。
  検証コマンドから起動した子のpyfltr・pytest経由のpyfltr・API呼出が自己待機で止まらないようにするためである。
  祖先の判定はpidとプロセス生成時刻で行い、環境変数を引き継がない子孫にも働く
- それ以外は保持者の解放まで`_POLL_INTERVAL`秒ごとに再試行して待つ。
  同じプロセスの別スレッドも待つ（長寿命の呼出元が並行して受けた要求を順に実行するため）

ロックはユーザーキャッシュのルート（`pyfltr.user_cache.default_cache_root`）配下の`locks/`に置く。
`PYFLTR_CACHE_DIR`で保存先を変えた実行どうしは別の枠になる。
保持者が強制終了した場合はOSがロックを解放する。
"""

# psutilは実行枠の取得で初めて使う時に読み込む（PEP 810。Python 3.15未満では通常のimport）。
__lazy_modules__ = ["psutil"]

import collections.abc
import contextlib
import dataclasses
import datetime as dt
import json
import os
import pathlib
import tempfile
import threading
import time
import typing

import psutil

import pyfltr.file_lock
import pyfltr.user_cache

_LOCK_FILENAME = "execution.lock"
_OWNER_FILENAME = "execution.owner.json"
_QUEUE_FILENAME = "execution.queue.lock"
_POLL_INTERVAL = 0.2
"""待機中の再試行間隔（秒）。解放から次の取得までの遅れと、待機中の負荷の釣り合いで選ぶ。"""
_CREATE_TIME_TOLERANCE = 1.0
"""保持者記録とプロセスの生成時刻を同一とみなす差（秒）。pidの再利用を取り違えないための判定に使う。"""

_local = threading.local()


@dataclasses.dataclass(frozen=True)
class SlotOwner:
    """実行枠の保持者記録。"""

    pid: int
    create_time: float
    cwd: str
    acquired_at: str
    label: str | None = None


class WaitAborted(Exception):
    """`should_abort`が真を返したため、実行枠を取得せずに待機を終えた。"""


@dataclasses.dataclass
class _Lease:
    lock: pyfltr.file_lock.FileLock
    owner_path: pathlib.Path
    label: str | None
    should_abort: collections.abc.Callable[[], bool] | None
    acquired: bool = False

    def release(self) -> None:
        if self.acquired:
            self.acquired = False
            with contextlib.suppress(OSError):
                self.owner_path.write_text("", encoding="utf-8")
            self.lock.release()


def default_lock_dir() -> pathlib.Path:
    """実行枠のロックを置くディレクトリを返す。"""
    return pyfltr.user_cache.default_cache_root() / "locks"


@contextlib.contextmanager
def hold(
    *,
    label: str | None = None,
    should_abort: collections.abc.Callable[[], bool] | None = None,
    on_wait: collections.abc.Callable[[SlotOwner | None], None] | None = None,
    lock_dir: pathlib.Path | None = None,
) -> collections.abc.Generator[None, None, None]:
    """実行枠を所有する区間を開く。

    Args:
        label: 保持者記録へ残す任意の名前。待機する側の通知に使う。
        should_abort: 待機中に再試行のたびに呼ぶ判定。真を返すと`WaitAborted`を送出する。
            `KeyboardInterrupt`も待機を中断する。
        on_wait: 待機を始めるときに1回だけ呼ぶ通知。保持者記録を読めない場合は`None`を渡す。
        lock_dir: ロックを置くディレクトリ。省略時は`default_lock_dir()`。

    区間の内側で起動した子プロセスは、区間を抜ける前に呼出元が終了・回収する。
    回収前に抜けると、子プロセスの実行中に次の検証が始まる。
    """
    if getattr(_local, "depth", 0):
        _local.depth += 1
        try:
            yield
        finally:
            _local.depth -= 1
        return

    directory = lock_dir if lock_dir is not None else default_lock_dir()
    lock = pyfltr.file_lock.FileLock(directory / _LOCK_FILENAME)
    owner_path = directory / _OWNER_FILENAME
    lease = _Lease(lock, owner_path, label, should_abort)
    lease.acquired = _acquire(lock, owner_path, label=label, should_abort=should_abort, on_wait=on_wait)
    _local.depth = 1
    _local.lease = lease
    try:
        yield
    finally:
        _local.depth = 0
        _local.lease = None
        lease.release()


def yield_requested() -> bool:
    """最外側の保持者に待機者がいるか返す。外部hold内の入れ子と祖先継承中は譲らない。"""
    lease: _Lease | None = getattr(_local, "lease", None)
    if getattr(_local, "depth", 0) != 1 or lease is None or not lease.acquired:
        return False
    with _queue_guard(lease.owner_path.parent):
        return bool(_waiters(lease.owner_path.parent))


def yield_to_waiters() -> None:
    """子プロセスを回収済みの区切りで、待機者の後ろへ再登録して実行枠を取り直す。"""
    if not yield_requested():
        return
    lease: _Lease = _local.lease
    # 解放より先に列末へ登録し、先行待機者より前へ再取得する競合を防ぐ。
    with _request(lease.owner_path.parent) as request:
        lease.release()
        lease.acquired = _acquire(
            lease.lock,
            lease.owner_path,
            label=lease.label,
            should_abort=lease.should_abort,
            on_wait=None,
            request=request,
        )


@contextlib.contextmanager
def _queue_guard(directory: pathlib.Path) -> collections.abc.Generator[None, None, None]:
    guard = pyfltr.file_lock.FileLock(directory / _QUEUE_FILENAME)
    while not guard.try_acquire():
        time.sleep(_POLL_INTERVAL)
    try:
        yield
    finally:
        guard.release()


def _waiters(directory: pathlib.Path) -> list[pathlib.Path]:
    """調停ロックの内側で、生存する要求を登録順に返し、異常終了した要求を回収する。"""
    active = []
    for path in sorted(directory.glob("execution.wait-*")):
        if pyfltr.file_lock.is_locked(path):
            active.append(path)
        else:
            path.unlink(missing_ok=True)
    return active


@contextlib.contextmanager
def _request(directory: pathlib.Path) -> collections.abc.Generator[pathlib.Path, None, None]:
    with _queue_guard(directory):
        waiters = _waiters(directory)
        # 連番の割当と要求ロック取得を同じ区間で行い、他の走査による未登録要求の回収を防ぐ。
        sequence = int(waiters[-1].name.split("-")[1]) + 1 if waiters else 0
        with tempfile.NamedTemporaryFile(prefix=f"execution.wait-{sequence:020d}-", dir=directory, delete=False) as file:
            path = pathlib.Path(file.name)
        request_lock = pyfltr.file_lock.FileLock(path)
        if not request_lock.try_acquire():
            raise RuntimeError(f"実行枠の要求を登録できません: {path}")
    try:
        yield path
    finally:
        with _queue_guard(directory):
            request_lock.release(remove=True)


def read_owner(lock_dir: pathlib.Path | None = None) -> SlotOwner | None:
    """現在の保持者記録を返す。保持者がいないか記録を読めない場合は`None`を返す。"""
    directory = lock_dir if lock_dir is not None else default_lock_dir()
    return _read_owner(directory / _OWNER_FILENAME)


def _acquire(
    lock: pyfltr.file_lock.FileLock,
    owner_path: pathlib.Path,
    *,
    label: str | None,
    should_abort: collections.abc.Callable[[], bool] | None,
    on_wait: collections.abc.Callable[[SlotOwner | None], None] | None,
    request: pathlib.Path | None = None,
) -> bool:
    """実行枠を取得して`True`を返す。祖先の所有を継承した場合は取得せず`False`を返す。"""
    if request is None:
        with _request(owner_path.parent) as registered:
            return _acquire(lock, owner_path, label=label, should_abort=should_abort, on_wait=on_wait, request=registered)
    notified = False
    while True:
        with _queue_guard(owner_path.parent):
            waiters = _waiters(owner_path.parent)
            if waiters and waiters[0] == request and lock.try_acquire():
                try:
                    _write_owner(owner_path, label)
                except BaseException:
                    lock.release()
                    raise
                return True
        owner = _read_owner(owner_path)
        if owner is not None and _is_ancestor(owner):
            return False
        if should_abort is not None and should_abort():
            raise WaitAborted
        if not notified:
            notified = True
            if on_wait is not None:
                on_wait(owner)
        time.sleep(_POLL_INTERVAL)


def _write_owner(owner_path: pathlib.Path, label: str | None) -> None:
    current = psutil.Process()
    owner = SlotOwner(
        pid=current.pid,
        create_time=current.create_time(),
        cwd=os.getcwd(),
        acquired_at=dt.datetime.now(dt.UTC).isoformat(),
        label=label,
    )
    owner_path.write_text(json.dumps(dataclasses.asdict(owner), ensure_ascii=False), encoding="utf-8")


def _read_owner(owner_path: pathlib.Path) -> SlotOwner | None:
    # 書込途中の記録は空か不完全なJSONとして読めるため、読めない記録は保持者不明として扱う。
    try:
        raw: typing.Any = json.loads(owner_path.read_text(encoding="utf-8"))
        return SlotOwner(
            pid=int(raw["pid"]),
            create_time=float(raw["create_time"]),
            cwd=str(raw["cwd"]),
            acquired_at=str(raw["acquired_at"]),
            label=None if raw.get("label") is None else str(raw["label"]),
        )
    except (OSError, ValueError, TypeError, KeyError, AttributeError):
        return None


def _is_ancestor(owner: SlotOwner) -> bool:
    """保持者記録のプロセスが呼出元プロセスの祖先かを返す。"""
    try:
        parents = psutil.Process().parents()
    except psutil.Error:
        return False
    for parent in parents:
        if parent.pid != owner.pid:
            continue
        try:
            return abs(parent.create_time() - owner.create_time) < _CREATE_TIME_TOLERANCE
        except psutil.Error:
            return False
    return False
