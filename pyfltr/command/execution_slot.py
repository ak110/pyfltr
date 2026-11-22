# psutilは実行枠の取得で初めて使う時に読み込む（PEP 810。Python 3.15未満では通常のimport）。
__lazy_modules__ = ["psutil"]

import collections.abc
import contextlib
import dataclasses
import datetime as dt
import json
import logging
import os
import pathlib
import sys
import tempfile
import threading
import time
import typing

import psutil

import pyfltr.file_lock
import pyfltr.timestamps
import pyfltr.user_cache

logger = logging.getLogger(__name__)

_LOCK_FILENAME = "execution.lock"
_OWNER_FILENAME = "execution.owner.json"
DEFAULT_SLOTS = 2
"""同時に外部ツールを実行できる独立した検証の既定の上限。"""
SLOTS_ENV = "PYFLTR_EXECUTION_SLOTS"
"""上限を`DEFAULT_SLOTS`以下へ下げる環境変数。資源が不足するホストで1件ずつに戻す退避手段である。"""
_QUEUE_FILENAME = "execution.queue.lock"
_MULTI_SLOT_MARK = "m"
"""複数の枠を扱う版の要求ファイル名に入れる目印。旧版の要求（`execution.wait-<連番>-<乱数>`）と区別する。"""
_POLL_INTERVAL = 0.2
"""待機中の再試行間隔（秒）。解放から次の取得までの遅れと、待機中の負荷の釣り合いで選ぶ。"""
_CREATE_TIME_TOLERANCE = 1.0
"""保持者記録とプロセスの生成時刻を同一とみなす差（秒）。pidの再利用を取り違えないための判定に使う。"""
_SEQUENCE_STEP = 2**20
"""末尾へ登録する要求の連番の間隔。

区切りで待機者へ枠を譲って取り直していた旧版は新規要求を取り直し要求の前の空き番号へ置くため、同じ保存先を共有する旧版と登録順を保てるよう間隔を空ける。
"""

_local = threading.local()


@dataclasses.dataclass(frozen=True)
class SlotOwner:
    pid: int
    create_time: float
    cwd: str
    acquired_at: str
    label: str | None = None
    linux_start_time: int | None = None
    boot_id: str | None = None

    def is_alive(self) -> bool:
        try:
            if sys.platform == "linux":
                if self.linux_start_time is None:
                    return psutil.pid_exists(self.pid)
                return _linux_identity(self.pid) == (self.boot_id, self.linux_start_time)
            return abs(psutil.Process(self.pid).create_time() - self.create_time) < _CREATE_TIME_TOLERANCE
        except (psutil.Error, OSError, ValueError, IndexError):
            return False


@dataclasses.dataclass(frozen=True)
class SlotWait:
    kind: typing.Literal["initial"]
    sequence: int
    started_at: str
    finished_at: str | None = None
    elapsed: float | None = None
    outcome: str | None = None

    def to_dict(self) -> dict[str, typing.Any]:
        return dataclasses.asdict(self)


WaitIntervalCallback = collections.abc.Callable[[SlotWait], None]
"""`hold`の`on_wait_interval`へ渡す通知関数の型。"""


class WaitAborted(Exception):
    pass


def default_lock_dir() -> pathlib.Path:
    return pyfltr.user_cache.default_cache_root() / "locks"


def slot_count() -> int:
    raw = os.environ.get(SLOTS_ENV)
    if raw is None or raw.strip() == "":
        return DEFAULT_SLOTS
    try:
        value = int(raw)
    except ValueError:
        value = 0
    if not 1 <= value <= DEFAULT_SLOTS:
        raise ValueError(
            f"環境変数{SLOTS_ENV}の値が不正です: {raw!r}。1から{DEFAULT_SLOTS}までの整数を指定するか、未設定にしてください。"
        )
    return value


def _slot_paths(directory: pathlib.Path, index: int) -> tuple[pathlib.Path, pathlib.Path]:
    if index == 0:
        return directory / _LOCK_FILENAME, directory / _OWNER_FILENAME
    return directory / f"execution-{index}.lock", directory / f"execution-{index}.owner.json"


@contextlib.contextmanager
def hold(
    *,
    label: str | None = None,
    should_abort: collections.abc.Callable[[], bool] | None = None,
    on_wait: collections.abc.Callable[[SlotOwner | None], None] | None = None,
    on_wait_interval: WaitIntervalCallback | None = None,
    lock_dir: pathlib.Path | None = None,
) -> collections.abc.Generator[None, None, None]:
    if getattr(_local, "depth", 0):
        _local.depth += 1
        try:
            yield
        finally:
            _local.depth -= 1
        return

    directory = lock_dir if lock_dir is not None else default_lock_dir()
    slots = [_slot_paths(directory, index) for index in range(slot_count())]
    held: list[tuple[pyfltr.file_lock.FileLock, pathlib.Path]] = []

    def acquire() -> bool:
        slot = _acquire(directory, slots, label=label, should_abort=should_abort, on_wait=on_wait)
        if slot is not None:
            held.append(slot)
        return slot is not None

    def release() -> None:
        while held:
            _release(*held.pop())

    _observe(on_wait_interval, acquire, release=release)
    _local.depth = 1
    try:
        yield
    finally:
        _local.depth = 0
        release()


def _observe(
    callback: WaitIntervalCallback | None,
    acquire: collections.abc.Callable[[], bool],
    *,
    release: collections.abc.Callable[[], None],
) -> bool:
    if callback is None:
        return acquire()
    started = SlotWait(kind="initial", sequence=0, started_at=pyfltr.timestamps.now_iso())
    callback(started)
    start = time.monotonic()
    outcome = "failed"
    acquired = False
    try:
        acquired = acquire()
        outcome = "acquired" if acquired else "inherited"
        return acquired
    except WaitAborted:
        outcome = "aborted"
        raise
    except KeyboardInterrupt:
        outcome = "interrupted"
        raise
    finally:
        finished = dataclasses.replace(
            started, finished_at=pyfltr.timestamps.now_iso(), elapsed=round(time.monotonic() - start, 3), outcome=outcome
        )
        try:
            callback(finished)
        except BaseException:
            if acquired:
                release()
            raise


def _release(lock: pyfltr.file_lock.FileLock, owner_path: pathlib.Path) -> None:
    with contextlib.suppress(OSError):
        owner_path.write_text("", encoding="utf-8")
    lock.release()


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
    active = []
    for path in sorted(directory.glob("execution.wait-*")):
        if pyfltr.file_lock.is_locked(path):
            active.append(path)
        else:
            path.unlink(missing_ok=True)
    return active


def _sequence(path: pathlib.Path) -> int:
    return int(path.name.split("-")[1])


@contextlib.contextmanager
def _request(directory: pathlib.Path) -> collections.abc.Generator[pathlib.Path, None, None]:
    with _queue_guard(directory):
        waiters = _waiters(directory)
        sequence = _sequence(waiters[-1]) + _SEQUENCE_STEP if waiters else _SEQUENCE_STEP
        with tempfile.NamedTemporaryFile(
            prefix=f"execution.wait-{sequence:020d}-{_MULTI_SLOT_MARK}-", dir=directory, delete=False
        ) as file:
            path = pathlib.Path(file.name)
        request_lock = pyfltr.file_lock.FileLock(path)
        if not request_lock.try_acquire():
            raise RuntimeError(
                f"実行枠の要求ファイルをロックできません: {path}。"
                "保存先のアクセス権とファイルロックへの対応を確認して再実行してください。"
            )
    try:
        yield path
    finally:
        with _queue_guard(directory):
            request_lock.release(remove=True)


def _is_legacy_request(path: pathlib.Path) -> bool:
    parts = path.name.split("-")
    return not (len(parts) >= 4 and parts[2] == _MULTI_SLOT_MARK)


def _eligible_slots(waiters: list[pathlib.Path], request: pathlib.Path, count: int) -> range:
    if request not in waiters:
        return range(0)
    ahead = waiters[: waiters.index(request)]
    if not ahead:
        return range(count)
    if all(_is_legacy_request(path) for path in ahead):
        return range(1, count)
    return range(0)


def waiter_count(lock_dir: pathlib.Path | None = None) -> int:
    directory = lock_dir if lock_dir is not None else default_lock_dir()
    if not directory.is_dir():
        return 0
    with _queue_guard(directory):
        return len(_waiters(directory))


def read_owner(lock_dir: pathlib.Path | None = None) -> SlotOwner | None:
    owners = read_owners(lock_dir)
    return owners[0] if owners else None


def read_owners(lock_dir: pathlib.Path | None = None) -> list[SlotOwner]:
    directory = lock_dir if lock_dir is not None else default_lock_dir()
    owners = []
    for index in range(DEFAULT_SLOTS):
        owner = _read_owner(_slot_paths(directory, index)[1])
        if owner is not None:
            owners.append(owner)
    return owners


def _acquire(
    directory: pathlib.Path,
    slots: list[tuple[pathlib.Path, pathlib.Path]],
    *,
    label: str | None,
    should_abort: collections.abc.Callable[[], bool] | None,
    on_wait: collections.abc.Callable[[SlotOwner | None], None] | None,
) -> tuple[pyfltr.file_lock.FileLock, pathlib.Path] | None:
    with _request(directory) as request:
        notified = False
        while True:
            owners = read_owners(directory)
            if any(_is_ancestor(owner) for owner in owners):
                return None
            with _queue_guard(directory):
                waiters = _waiters(directory)
                for index in _eligible_slots(waiters, request, len(slots)):
                    lock_path, owner_path = slots[index]
                    lock = pyfltr.file_lock.FileLock(lock_path)
                    if not lock.try_acquire():
                        continue
                    try:
                        _write_owner(owner_path, label)
                    except BaseException:
                        lock.release()
                        raise
                    return lock, owner_path
            if should_abort is not None and should_abort():
                raise WaitAborted("実行枠の取得待機が取り消されました。")
            if not notified:
                notified = True
                if on_wait is not None:
                    on_wait(owners[0] if owners else None)
            time.sleep(_POLL_INTERVAL)


def _write_owner(owner_path: pathlib.Path, label: str | None) -> None:
    current = psutil.Process()
    boot_id, start_time = _linux_identity(current.pid) if sys.platform == "linux" else (None, None)
    owner = SlotOwner(
        pid=current.pid,
        create_time=current.create_time(),
        cwd=os.getcwd(),
        acquired_at=dt.datetime.now(dt.UTC).isoformat(),
        label=label,
        linux_start_time=start_time,
        boot_id=boot_id,
    )
    owner_path.write_text(json.dumps(dataclasses.asdict(owner), ensure_ascii=False), encoding="utf-8")


def _read_owner(owner_path: pathlib.Path) -> SlotOwner | None:
    try:
        raw: typing.Any = json.loads(owner_path.read_text(encoding="utf-8"))
        owner = SlotOwner(
            pid=int(raw["pid"]),
            create_time=float(raw["create_time"]),
            cwd=str(raw["cwd"]),
            acquired_at=str(raw["acquired_at"]),
            label=None if raw.get("label") is None else str(raw["label"]),
            linux_start_time=None if raw.get("linux_start_time") is None else int(raw["linux_start_time"]),
            boot_id=None if raw.get("boot_id") is None else str(raw["boot_id"]),
        )
        if sys.platform == "linux" and owner.linux_start_time is None:
            with contextlib.suppress(OSError, ValueError, IndexError):
                identity = _linux_identity(owner.pid)
                lock_path = owner_path.with_name(owner_path.name.replace(".owner.json", ".lock"))
                if _linux_holds_lock(owner.pid, lock_path) and _linux_identity(owner.pid) == identity:
                    owner = dataclasses.replace(owner, boot_id=identity[0], linux_start_time=identity[1])
        return owner
    except (OSError, ValueError, TypeError, KeyError, AttributeError, IndexError):
        return None


def _linux_identity(pid: int) -> tuple[str, int]:
    boot_id = pathlib.Path("/proc/sys/kernel/random/boot_id").read_text(encoding="ascii").strip()
    return boot_id, _linux_process_stat(pid)[1]


def _linux_process_stat(pid: int) -> tuple[int, int]:
    stat = pathlib.Path(f"/proc/{pid}/stat").read_text(encoding="utf-8", errors="surrogateescape")
    fields = stat.rsplit(")", 1)[1].split()
    return int(fields[1]), int(fields[19])


def _linux_is_ancestor(owner: SlotOwner) -> bool:
    if owner.linux_start_time is None:
        return False
    try:
        pid = os.getpid()
        boot_id, expected_start = _linux_identity(pid)
        if boot_id != owner.boot_id:
            return False
        seen = {pid}
        while True:
            child = _linux_process_stat(pid)
            ppid = child[0]
            if child[1] != expected_start or ppid <= 0 or ppid in seen:
                return False
            parent = _linux_process_stat(ppid)
            if parent[1] > child[1] or _linux_process_stat(pid) != child:
                return False
            if ppid == owner.pid:
                return parent[1] == owner.linux_start_time
            seen.add(ppid)
            pid = ppid
            expected_start = parent[1]
    except (OSError, ValueError, IndexError):
        return False


def _linux_holds_lock(pid: int, lock_path: pathlib.Path) -> bool:
    stat = lock_path.stat()
    for info in pathlib.Path(f"/proc/{pid}/fdinfo").iterdir():
        try:
            lines = info.read_text(encoding="ascii").splitlines()
        except FileNotFoundError:
            continue
        for line in lines:
            fields = line.split()
            if len(fields) != 9 or fields[:1] != ["lock:"] or fields[2:5] != ["FLOCK", "ADVISORY", "WRITE"]:
                continue
            major, minor, inode = fields[6].split(":")
            # os.major / os.minorはPOSIX専用でWindows型スタブに未定義。この関数はLinuxからだけ呼ぶ。
            dev_major = os.major(stat.st_dev)  # type: ignore[attr-defined,unused-ignore]  # pyright: ignore[reportAttributeAccessIssue]  # ty: ignore  # pylint: disable=no-member
            dev_minor = os.minor(stat.st_dev)  # type: ignore[attr-defined,unused-ignore]  # pyright: ignore[reportAttributeAccessIssue]  # ty: ignore  # pylint: disable=no-member
            if (int(fields[5]), int(major, 16), int(minor, 16), int(inode)) == (pid, dev_major, dev_minor, stat.st_ino):
                return True
    return False


def _is_ancestor(owner: SlotOwner) -> bool:
    if sys.platform == "linux":
        return _linux_is_ancestor(owner)
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
