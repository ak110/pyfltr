"""検証の実行枠。

同一ホスト・同一OSアカウントで、独立した検証が外部ツールを実行する区間を同時に`DEFAULT_SLOTS`（2）件までにする。
共有の一時領域・inotify上限・CPUなどの資源は起動ごとの並列設定（`jobs`など）では調停できず、
別worktree・別リポジトリ・MCPから起動した検証の並列数がホスト上で積み上がるためである。
上限を1件でなく2件とするのは、並行する作業の短い検証が長い検証の終了を待ち続けないようにするためで、
2件の並行で資源の枯渇が起きないことを測定で確かめた（判定の記録は`docs/development/architecture.md`「検証の実行枠」）。
環境変数`PYFLTR_EXECUTION_SLOTS`に`1`を指定すると1件ずつになる。上限を上げる指定は受け付けない。

pyfltrは`pyfltr.command.stage_runner.run_stages`を`hold`の内側で実行し、1回の実行の間は枠を保持し続ける。
CLI・TUI・MCPの全実行経路とcommit hookからの起動が同じ枠へ参加する。
pyfltr以外の検証を起動する呼出元（他のツールのスクリプトなど）も、同じ`hold`で枠を共有できる::

    import pyfltr.command.execution_slot

    with pyfltr.command.execution_slot.hold(label="my-check"):
        subprocess.run([...], check=True)

待機の内訳を記録する呼出元は`on_wait_interval`を渡す。
取得の待ちの開始時に未完了の`SlotWait`、終了時に完了した`SlotWait`を受け取る::

    with pyfltr.command.execution_slot.hold(label="my-check", on_wait_interval=lambda wait: log(wait.to_dict())):
        ...

取得規則:

- 同じスレッドが保持中なら入れ子として再入する
- 保持者記録のプロセスが呼出元プロセスの祖先なら、取得せずに所有を継承する。
  検証コマンドから起動した子のpyfltr・pytest経由のpyfltr・API呼出が自己待機で止まらないようにするためである。
  祖先の判定はpidとプロセス生成時刻で行い、環境変数を引き継がない子孫にも働く
- それ以外は待機列へ登録し、先に並んだ要求と空いた枠を`_POLL_INTERVAL`秒ごとに確かめ、登録順に空いた枠を取得する。
  同じプロセスの別スレッドも別の要求として扱い、枠が全て埋まっていれば待つ
- 先に並んだ要求が全て枠を1つだけ扱う旧版の要求なら、2つ目以降の空いた枠は先に取得する。
  旧版の要求は1つ目の枠しか取得できず、登録順を守ると旧版が先頭で待つ間は空いた枠が使われないためである。
  1つ目の枠は旧版の要求に先を譲るため、先頭のときだけ取得する

実行の途中で待機者へ譲る機能と、子プロセスが参加券で協調して譲る機能は、2026年11月に撤去した。
区切りごとに待機列へ並び直す方式は、混雑時に短い検証の待ちを区切りの数だけ増やし、効果を実測で示せなかったためである
（判定の記録は`docs/development/architecture.md`「検証の実行枠」）。

ロックはユーザーキャッシュのルート（`pyfltr.user_cache.default_cache_root`）配下の`locks/`に置き、枠ごとにロックと保持者記録を分ける。
1つ目の枠は撤去前から使うファイル名を保つ。枠を1つだけ扱う旧版と同じ保存先を共有しても、全体の同時実行を上限内に保つためである。
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
import logging
import os
import pathlib
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

撤去前の版は新規要求を取り直し要求の前の空き番号へ置くため、同じ保存先を共有する旧版と登録順を保てるよう間隔を空ける。
"""

_local = threading.local()


@dataclasses.dataclass(frozen=True)
class SlotOwner:
    """実行枠の保持者記録。"""

    pid: int
    create_time: float
    cwd: str
    acquired_at: str
    label: str | None = None

    def is_alive(self) -> bool:
        """記録したプロセスが生存しているかを返す。待機中の表示で、終了済みの記録を保持者と誤認させないために使う。"""
        try:
            return abs(psutil.Process(self.pid).create_time() - self.create_time) < _CREATE_TIME_TOLERANCE
        except psutil.Error:
            return False


@dataclasses.dataclass(frozen=True)
class SlotWait:
    """実行枠を取得するまでの待機区間。

    `kind`は最初の取得の待ちを表す`"initial"`だけを使い、`sequence`は常に0である。
    実行の途中で譲る機能を撤去する前の実行アーカイブと同じ形式で記録するため、両フィールドを保つ。
    開始時に未完了の値（`finished_at`・`elapsed`・`outcome`が`None`）、終了時に完了した値で1回ずつ通知する。

    `outcome`は終了の理由で、`"acquired"`（取得）、`"inherited"`（祖先の所有を継承し、取得しなかった）、
    `"aborted"`（`should_abort`による中断）、`"interrupted"`（`KeyboardInterrupt`）、`"failed"`（その他の例外）のいずれかである。
    待機が無かった場合も、取得までの短い区間を通知する。待機なしの実行と未計測を区別するためである。
    """

    kind: typing.Literal["initial"]
    sequence: int
    started_at: str
    finished_at: str | None = None
    elapsed: float | None = None
    outcome: str | None = None

    def to_dict(self) -> dict[str, typing.Any]:
        """JSONへ直列化できる辞書を返す。"""
        return dataclasses.asdict(self)


WaitIntervalCallback = collections.abc.Callable[[SlotWait], None]
"""`hold`の`on_wait_interval`へ渡す通知関数の型。"""


class WaitAborted(Exception):
    """取得待機の取消により処理を中断した。"""


def default_lock_dir() -> pathlib.Path:
    """実行枠のロックを置くディレクトリを返す。"""
    return pyfltr.user_cache.default_cache_root() / "locks"


def slot_count() -> int:
    """同時に外部ツールを実行できる独立した検証の上限を返す。

    `PYFLTR_EXECUTION_SLOTS`は1から`DEFAULT_SLOTS`までを受け付け、未設定と空白だけの値は既定値とする。
    上限を上げる指定を受け付けないのは、実行枠を無効にする手段を設けない方針を保つためである。
    """
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
    """枠の番号に対応するロックと保持者記録のパスを返す。1つ目の枠は撤去前からのファイル名を保つ。"""
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
    """実行枠を所有する区間を開く。

    Args:
        label: 保持者記録へ残す任意の名前。待機する側の通知に使う。
        should_abort: 待機中に再試行のたびに呼ぶ判定。真を返すと`WaitAborted`を送出する。
            `KeyboardInterrupt`も待機を中断する。
        on_wait: 待機を始めるときに1回だけ呼ぶ通知。保持者記録を読めない場合は`None`を渡す。
        on_wait_interval: 待機区間（`SlotWait`）の開始と終了を受け取る通知。
            入れ子の`hold`は取得しないため呼ばない。例外はそのまま呼出元へ伝わる。
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
    """取得処理を待機区間として計測し、開始と終了を`callback`へ通知する。

    終了の通知が例外を送出した場合は、取得済みの枠を`release`で解放してから例外を伝える。
    呼出元は戻り値を受け取れず枠を持たない状態になるため、解放しないと後続の検証が待ち続ける。
    """
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
    """取得済みの枠の保持者記録を消してロックを解放する。"""
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
    """調停ロックの内側で、生存する要求を登録順に返し、異常終了した要求を回収する。"""
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
        # 連番の割当と要求ロック取得を同じ区間で行い、他の走査による未登録要求の回収を防ぐ。
        sequence = _sequence(waiters[-1]) + _SEQUENCE_STEP if waiters else _SEQUENCE_STEP
        # 旧版は名前の2つ目の区切りを連番として読むため、目印は連番の後ろに置く。
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
    """枠を1つだけ扱う旧版の要求かを返す。旧版の要求ファイル名は目印を持たない。"""
    parts = path.name.split("-")
    return not (len(parts) >= 4 and parts[2] == _MULTI_SLOT_MARK)


def _eligible_slots(waiters: list[pathlib.Path], request: pathlib.Path, count: int) -> range:
    """要求が今取得してよい枠の番号を返す。

    先頭の要求は全ての枠を取得できる。
    先に並んだ要求が全て旧版の要求なら、旧版が使えない2つ目以降の枠だけを取得できる。
    それ以外は先に並んだ新版の要求を待つため、取得できる枠は無い。
    """
    if request not in waiters:
        return range(0)
    ahead = waiters[: waiters.index(request)]
    if not ahead:
        return range(count)
    if all(_is_legacy_request(path) for path in ahead):
        return range(1, count)
    return range(0)


def waiter_count(lock_dir: pathlib.Path | None = None) -> int:
    """待機中の取得要求の件数を返す。待機状況の表示に使う観測値であり、取得順の判定には使わない。"""
    directory = lock_dir if lock_dir is not None else default_lock_dir()
    if not directory.is_dir():
        return 0
    with _queue_guard(directory):
        return len(_waiters(directory))


def read_owner(lock_dir: pathlib.Path | None = None) -> SlotOwner | None:
    """現在の保持者記録の1件を返す。1つ目の枠を優先し、保持者がいないか記録を読めない場合は`None`を返す。"""
    owners = read_owners(lock_dir)
    return owners[0] if owners else None


def read_owners(lock_dir: pathlib.Path | None = None) -> list[SlotOwner]:
    """全ての枠の保持者記録を枠の順に返す。記録を読めない枠は除く。

    枠の数は`slot_count`ではなく`DEFAULT_SLOTS`までを走査する。
    上限を下げた実行からも、上限を下げていない別の実行が保持する枠を観測できるようにするためである。
    """
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
    """空いた枠を取得してそのロックと保持者記録のパスを返す。祖先の所有を継承した場合は取得せず`None`を返す。

    祖先の判定を空いた枠の取得より先に行う。
    検証コマンドから起動した子のpyfltrが、祖先の枠を継承せずに空いた枠を使い、別の検証の枠を減らさないようにするためである。
    """
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
