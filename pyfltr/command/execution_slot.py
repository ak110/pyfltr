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

長い区間を保持する呼出元は、区間で起動した子プロセスを全て回収した区切りで`yield_to_waiters`を呼ぶと、
待機者へ枠を譲り、先に並んだ待機者の終了後に取り直して残りの処理を続けられる::

    with pyfltr.command.execution_slot.hold(label="my-check"):
        for step in steps:
            subprocess.run(step, check=True)
            pyfltr.command.execution_slot.yield_to_waiters()

待機の内訳を記録する呼出元は`on_wait_interval`を渡す。
最初の取得の待ちと、譲渡後の取り直しの待ちごとに、開始時に未完了の`SlotWait`、終了時に完了した`SlotWait`を受け取る::

    with pyfltr.command.execution_slot.hold(label="my-check", on_wait_interval=lambda wait: log(wait.to_dict())):
        ...

通常の譲渡は最外側の`hold`で枠を取得したスレッドが行う。
外側の所有者が`cooperative_hold`で明示参加した場合は、入れ子と参加券付きの子からも停止を通知できる。
所有者が全参加者の停止・回収を確認して譲り、再取得してから子を再開させる。
子プロセスが祖先の所有を単独で解除すると、所有者が起動中の別の子と待機者の実行が重なるためである。

参加券で参加した子は、`cooperative_hold(participant=...)`で自分の子の協調所有者にもなれる（多段の協調）。
中間の協調所有者は、自分の参加者が全員停止し、自分が起動した対象処理を全て回収した区切りでだけ上位へ停止を通知する。
ロックの解放と取り直しは最上位の所有者だけが行い、再取得後は上位から下位の順に再開させる。

参加券を子プロセスへ渡す手段として、環境変数`PYFLTR_EXECUTION_SLOT_TICKET`（`TICKET_ENV`）を使える。
`hold`はこの変数を自動で読まない。起動された側が`receive_ticket`で明示的に受け取り、
受け取りと同時に`os.environ`から除くため、受け取ったプロセスの子孫へは継承されない。
pyfltrは起動する各ツールへツール1件専用の参加券をこの変数で渡し、CLI自身も起動時に上位の参加券を受け取る。

取得規則:

- 同じスレッドが保持中なら入れ子として再入する
- 保持者記録のプロセスが呼出元プロセスの祖先なら、取得せずに所有を継承する。
  検証コマンドから起動した子のpyfltr・pytest経由のpyfltr・API呼出が自己待機で止まらないようにするためである。
  祖先の判定はpidとプロセス生成時刻で行い、環境変数を引き継がない子孫にも働く
- それ以外は保持者の解放まで`_POLL_INTERVAL`秒ごとに再試行して待つ。
  同じプロセスの別スレッドも待つ（長寿命の呼出元が並行して受けた要求を順に実行するため）

配分規則:

- 最初の取得の要求は、登録から`_REACQUIRE_BYPASS_SECONDS`秒未満の取り直し要求より先に並ぶ
- まだ譲っていない最初の区間の保持者は、取得から`_FIRST_SEGMENT_HOLD_SECONDS`秒の間、
  待機者が若い取り直し要求（新規要求が前へ入れる要求）だけなら譲らない
- 新規要求どうし・取り直し要求どうしは登録順を保ち、上限を過ぎた取り直し要求の前へは置かない

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
import logging
import os
import pathlib
import tempfile
import threading
import time
import typing
import uuid

import psutil

import pyfltr.file_lock
import pyfltr.user_cache

logger = logging.getLogger(__name__)

_LOCK_FILENAME = "execution.lock"
_OWNER_FILENAME = "execution.owner.json"
_QUEUE_FILENAME = "execution.queue.lock"
_COOPERATIVE_ABORT_ACTION = "所有者と子を回収してから再実行してください。"
_POLL_INTERVAL = 0.2
"""待機中の再試行間隔（秒）。解放から次の取得までの遅れと、待機中の負荷の釣り合いで選ぶ。"""
_CREATE_TIME_TOLERANCE = 1.0
"""保持者記録とプロセスの生成時刻を同一とみなす差（秒）。pidの再利用を取り違えないための判定に使う。"""
_FIRST_SEGMENT_HOLD_SECONDS = 120.0
"""最初の区間の保持者が、若い取り直し要求だけを待機者に持つ間に譲らない時間（秒）。

短い検証が区切りごとに長い検証の取り直しへ譲ると、長い検証の区間を間に置いて待ち直し、本体数秒の実行が数百秒待つ。
実行アーカイブの集計（2026年11月の98run）で本体時間の75パーセンタイル（fast約88秒、run約74秒）を上回る値とし、
短い検証の大半を1区間で終える。新規の要求には従来どおり譲り、取り直し要求が追加で待つ時間はこの値で抑える。
"""
_REACQUIRE_BYPASS_SECONDS = 600.0
"""新規の取得要求が前へ入れる取り直し要求の、登録からの経過上限（秒）。

新規要求は既に区間を進めた長い検証の取り直しより先に並び、短い検証が長い検証の列の後ろで待ち続けないようにする。
上限を超えた取り直し要求の前へは新規要求を置かず、最初の区間の保持者もその要求へ譲るため、新規要求が続いても長い検証が進む。
"""
_SEQUENCE_STEP = 2**20
"""末尾へ登録する要求の連番の間隔。新規要求を取り直し要求の前へ置く空き番号を残す。"""
_REACQUIRE_MARK = "R"
"""取り直し要求のファイル名で、連番の次に置く印。旧版の乱数部は小文字だけのため区別できる。"""
TICKET_ENV = "PYFLTR_EXECUTION_SLOT_TICKET"
"""参加券を子プロセスへ渡す環境変数の名前。受け取りは`receive_ticket`で行う。"""

_local = threading.local()
_tool_ticket = threading.local()


@dataclasses.dataclass(frozen=True)
class SlotOwner:
    """実行枠の保持者記録。"""

    pid: int
    create_time: float
    cwd: str
    acquired_at: str
    label: str | None = None


@dataclasses.dataclass(frozen=True)
class SlotWait:
    """実行枠を取得するまでの待機区間。

    `kind`は最初の取得の待ち（`"initial"`）か、`yield_to_waiters`で譲った後の取り直しの待ち（`"reacquire"`）を表す。
    `sequence`は`initial`が0、`reacquire`が同じ`hold`の中で譲るたびに1から増える番号である。
    同じ`kind`・`sequence`の区間は、開始時に未完了の値（`finished_at`・`elapsed`・`outcome`が`None`）、
    終了時に完了した値で1回ずつ通知する。

    `outcome`は終了の理由で、`"acquired"`（取得）、`"inherited"`（祖先の所有を継承し、取得しなかった）、
    `"aborted"`（`should_abort`による中断）、`"interrupted"`（`KeyboardInterrupt`）、`"failed"`（その他の例外）のいずれかである。
    待機が無かった場合も、取得までの短い区間を通知する。待機なしの実行と未計測を区別するためである。
    """

    kind: typing.Literal["initial", "reacquire"]
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
    """取得待機の取消や協調区間の終了により処理を中断した。"""


class ParticipationRejected(WaitAborted):
    """参加券で協調区間へ参加できなかった。

    参加中の参加券を継承した子孫や、閉じられた協調区間の参加券で起きる。
    `coordinated_hold`はこの例外を受けると参加券を使わずに区間を開き、祖先の所有を継承した区間として続ける。
    """


@dataclasses.dataclass
class _Lease:
    lock: pyfltr.file_lock.FileLock
    owner_path: pathlib.Path
    label: str | None
    should_abort: collections.abc.Callable[[], bool] | None
    on_wait_interval: WaitIntervalCallback | None = None
    acquired: bool = False
    acquired_at: float = 0.0
    """取得（取り直し）した時点の`time.monotonic()`。最低保持時間の判定に使う。"""
    yields: int = 0
    coordinator: "SlotCoordinator | None" = None
    participant: "_Participant | None" = None

    def release(self) -> None:
        if self.acquired:
            self.acquired = False
            _release(self.lock, self.owner_path)


def default_lock_dir() -> pathlib.Path:
    """実行枠のロックを置くディレクトリを返す。"""
    return pyfltr.user_cache.default_cache_root() / "locks"


def receive_ticket() -> str | None:
    """親から環境変数`TICKET_ENV`で渡された参加券を受け取る。無ければ`None`を返す。

    受け取った参加券は`os.environ`から除き、このプロセスが以後に起動する子孫へ継承させない。
    同じ参加券で複数のプロセスが同時に参加すると、親が一方の停止だけを見て譲り、他方の実行と待機者が重なるためである。
    受け取った参加券は`hold(participant=...)`か`cooperative_hold(participant=...)`へ渡す。
    """
    return os.environ.pop(TICKET_ENV, None) or None


@contextlib.contextmanager
def bind_tool_ticket(ticket: str | None) -> collections.abc.Generator[None, None, None]:
    """呼出スレッドが起動するツールの子プロセスへ渡す参加券を設定する。pyfltrの段実行が使う。"""
    previous = getattr(_tool_ticket, "value", None)
    _tool_ticket.value = ticket
    try:
        yield
    finally:
        _tool_ticket.value = previous


def tool_environ(env: dict[str, str]) -> dict[str, str]:
    """ツールの子プロセスへ渡す環境変数を返す。

    呼出スレッドへ`bind_tool_ticket`で参加券が設定されていれば`TICKET_ENV`へ設定し、無ければ除く。
    除くのは、pyfltr自身へ渡された参加券を受け取らずに起動したツールへ継承させないためである。
    """
    ticket: str | None = getattr(_tool_ticket, "value", None)
    if ticket is None:
        if TICKET_ENV not in env:
            return env
        return {key: value for key, value in env.items() if key != TICKET_ENV}
    return {**env, TICKET_ENV: ticket}


@contextlib.contextmanager
def hold(
    *,
    label: str | None = None,
    should_abort: collections.abc.Callable[[], bool] | None = None,
    on_wait: collections.abc.Callable[[SlotOwner | None], None] | None = None,
    on_wait_interval: WaitIntervalCallback | None = None,
    lock_dir: pathlib.Path | None = None,
    participant: str | None = None,
) -> collections.abc.Generator[None, None, None]:
    """実行枠を所有する区間を開く。

    Args:
        label: 保持者記録へ残す任意の名前。待機する側の通知に使う。
        should_abort: 待機中に再試行のたびに呼ぶ判定。真を返すと`WaitAborted`を送出する。
            `yield_to_waiters`で譲った後の取り直しの待機にも使う。
            `KeyboardInterrupt`も待機を中断する。
        on_wait: 待機を始めるときに1回だけ呼ぶ通知。保持者記録を読めない場合は`None`を渡す。
        on_wait_interval: 待機区間（`SlotWait`）の開始と終了を受け取る通知。
            最初の取得と、この`hold`の内側の`yield_to_waiters`の取り直しごとに呼ぶ。
            入れ子の`hold`は取得しないため呼ばない。例外はそのまま呼出元へ伝わる。
        lock_dir: ロックを置くディレクトリ。省略時は`default_lock_dir()`。
        participant: `SlotCoordinator.participant()`が発行した参加券。同じlock_dirを指定する。
            明示参加した子は、回収済みの区切りで所有者へ停止を通知できる。

    区間の内側で起動した子プロセスは、区間を抜ける前に呼出元が終了・回収する。
    回収前に抜けると、子プロセスの実行中に次の検証が始まる。
    """
    if getattr(_local, "depth", 0):
        if participant is not None:
            raise ValueError("参加券は最外側のholdへ渡してください。")
        _local.depth += 1
        try:
            yield
        finally:
            _local.depth -= 1
        return

    directory = lock_dir if lock_dir is not None else default_lock_dir()
    lock = pyfltr.file_lock.FileLock(directory / _LOCK_FILENAME)
    owner_path = directory / _OWNER_FILENAME
    lease = _Lease(lock, owner_path, label, should_abort, on_wait_interval)
    try:
        joined = _Participant.join(directory, participant, should_abort) if participant is not None else None
    except WaitAborted as exc:
        raise ParticipationRejected(str(exc)) from exc
    lease.participant = joined
    lease.acquired = _observe(
        on_wait_interval,
        "initial",
        0,
        lambda: (
            False if joined is not None else _acquire(lock, owner_path, label=label, should_abort=should_abort, on_wait=on_wait)
        ),
        release=lambda: _release(lock, owner_path),
    )
    lease.acquired_at = time.monotonic()
    _local.depth = 1
    _local.lease = lease
    try:
        yield
    finally:
        _local.depth = 0
        _local.lease = None
        try:
            if joined is not None:
                joined.finish()
        finally:
            lease.release()


def yield_requested() -> bool:
    """呼出スレッドが譲れる保持者で、譲る相手の待機者がいるかを返す。

    通常は最外側の`hold`で取得したスレッドだけが真になりうる。
    協調所有者の入れ子と明示参加した子も待機者を確認できる。
    非協調の入れ子・継承区間、`hold`の外、別スレッドでは偽を返す。
    まだ譲っていない最初の区間の保持者は、取得から`_FIRST_SEGMENT_HOLD_SECONDS`秒未満の間、
    待機者が若い取り直し要求（新規要求が前へ入れる要求）だけなら偽を返す。参加した子は所有者の判定に委ねて待機者の有無だけを返す。
    """
    lease: _Lease | None = getattr(_local, "lease", None)
    if lease is not None and lease.participant is not None:
        with _queue_guard(lease.owner_path.parent):
            return bool(_waiters(lease.owner_path.parent))
    if lease is None or not lease.acquired:
        return False
    if lease.coordinator is None and getattr(_local, "depth", 0) != 1:
        return False
    with _queue_guard(lease.owner_path.parent):
        waiters = _waiters(lease.owner_path.parent)
    if not waiters:
        return False
    if lease.yields == 0 and time.monotonic() - lease.acquired_at < _FIRST_SEGMENT_HOLD_SECONDS:
        now_ms = int(time.time() * 1000)
        return not all(_bypassable(waiter, now_ms) for waiter in waiters)
    return True


def yield_to_waiters() -> bool:
    """待機者がいれば実行枠を譲り、先に並んだ待機者の終了後に取り直す。

    呼出元は、区間で起動した子プロセスを全て終了・回収した区切りで呼ぶ。
    子の稼働中に呼ぶと、子の実行中に待機者の処理が始まる。
    `yield_requested`が偽の場合は何もしない。

    解放より先に待機列の最後へ再登録するため、取り直しの順番は先に並んだ待機者の後になる。
    取り直し要求は登録から`_REACQUIRE_BYPASS_SECONDS`秒未満の間、後から並んだ新規の取得要求に後から並んだ新規要求が前へ入る。
    譲る相手の判定は`yield_requested`に従う。
    取り直しの待機には`hold`へ渡した`should_abort`を使い、`on_wait`は呼ばない。
    取り直しの待機を`WaitAborted`や`KeyboardInterrupt`で抜けた場合は、待機要求を回収し、枠を持たない状態で`hold`を抜ける。

    Returns:
        譲って取り直した場合は`True`、譲らなかった場合は`False`。
    """
    lease: _Lease | None = getattr(_local, "lease", None)
    # 中間の協調所有者は参加者でもある。自分の参加者の停止を確かめてから上位へ通知するため、協調制御を先に判定する。
    if lease is not None and lease.coordinator is not None:
        return lease.coordinator.checkpoint()
    if lease is not None and lease.participant is not None:
        return lease.participant.checkpoint()
    return _yield_lease()


def _yield_lease() -> bool:
    if not yield_requested():
        return False
    lease: _Lease = _local.lease
    lease.yields += 1
    # 解放より先に列末へ登録し、先行待機者より前へ再取得する競合を防ぐ。
    with _request(lease.owner_path.parent, reacquire=True) as request:
        lease.release()
        lease.acquired = _observe(
            lease.on_wait_interval,
            "reacquire",
            lease.yields,
            lambda: _acquire(
                lease.lock,
                lease.owner_path,
                label=lease.label,
                should_abort=lease.should_abort,
                on_wait=None,
                request=request,
            ),
            release=lambda: _release(lease.lock, lease.owner_path),
        )
    lease.acquired_at = time.monotonic()
    return True


@contextlib.contextmanager
def cooperative_hold(
    *,
    label: str | None = None,
    should_abort: collections.abc.Callable[[], bool] | None = None,
    on_wait: collections.abc.Callable[[SlotOwner | None], None] | None = None,
    on_wait_interval: WaitIntervalCallback | None = None,
    lock_dir: pathlib.Path | None = None,
    participant: str | None = None,
) -> collections.abc.Generator["SlotCoordinator", None, None]:
    """最外側で取得した枠を、明示参加した子と協調して譲る。

    所有者はparticipant区間を開いて参加券を子のholdへ渡し、子のpoll/waitの合間に
    checkpointを呼ぶ。子は全対象処理を回収した区切りでyield_to_waitersを呼ぶ。
    所有者はcheckpoint中に新しい対象処理を開始しない。例外・取消時もfinallyで
    子とその対象処理を終了・回収してからparticipant区間、最外側の区間の順に閉じる。
    子の生成・停止・回収は呼出元の責任であり、このAPIは代行しない。

    `participant`へ上位の参加券を渡すと、上位の参加者のまま自分の子の協調所有者になる（多段の協調）。
    この場合は枠を取得せず、checkpointは自分の参加者が全員停止した区切りで上位へ停止を通知し、
    上位の所有者が譲って取り直した後に自分の参加者を再開させる。
    """
    if getattr(_local, "depth", 0):
        raise ValueError("cooperative_holdは最外側で開いてください。")
    with coordinated_hold(
        label=label,
        should_abort=should_abort,
        on_wait=on_wait,
        on_wait_interval=on_wait_interval,
        lock_dir=lock_dir,
        participant=participant,
    ) as coordinator:
        if coordinator is None:
            raise ValueError("祖先から継承した枠は協調所有者へ変更できません。祖先の参加券を使ってください。")
        yield coordinator


@contextlib.contextmanager
def coordinated_hold(
    *,
    label: str | None = None,
    should_abort: collections.abc.Callable[[], bool] | None = None,
    on_wait: collections.abc.Callable[[SlotOwner | None], None] | None = None,
    on_wait_interval: WaitIntervalCallback | None = None,
    lock_dir: pathlib.Path | None = None,
    participant: str | None = None,
) -> collections.abc.Generator["SlotCoordinator | None", None, None]:
    """協調所有者になれる場合だけ協調区間を開き、なれない場合は通常の`hold`として区間を開く。

    最外側で枠を取得した場合と参加券で参加した場合は`SlotCoordinator`を返す。
    同じスレッドの入れ子と、祖先から所有を継承した区間は`None`を返す。
    これらの区間は子の区切りで譲れないため、呼出元は子へ参加券を渡さず、子の回収後の区切りだけで`yield_to_waiters`を呼ぶ。
    """
    if getattr(_local, "depth", 0):
        if participant is not None:
            raise ValueError("参加券は最外側のholdへ渡してください。")
        with hold(label=label, should_abort=should_abort, on_wait=on_wait, on_wait_interval=on_wait_interval):
            yield None
        return
    with contextlib.ExitStack() as stack:
        options: dict[str, typing.Any] = {
            "label": label,
            "should_abort": should_abort,
            "on_wait": on_wait,
            "on_wait_interval": on_wait_interval,
            "lock_dir": lock_dir,
        }
        try:
            stack.enter_context(hold(**options, participant=participant))
        except ParticipationRejected as exc:
            # 参加できない参加券は、参加券を受け取らなかった場合と同じ区間で続ける。
            # 祖先が所有していれば継承した区間となり、上位の所有者はこのプロセスを含むツールの停止を待つため排他は保たれる。
            logger.debug("参加券を使わずに実行枠の区間を開きます: %s", exc)
            stack.enter_context(hold(**options))
        lease: _Lease = _local.lease
        if not lease.acquired and lease.participant is None:
            yield None
            return
        coordinator = SlotCoordinator(lease)
        lease.coordinator = coordinator
        try:
            yield coordinator
        finally:
            coordinator.close()
            lease.coordinator = None


class SlotCoordinator:
    """所有スレッドで参加区間と譲渡・再開を管理する。cooperative_holdから取得する。"""

    def __init__(self, lease: _Lease) -> None:
        self._lease = lease
        self._thread = threading.get_ident()
        self._id = uuid.uuid4().hex
        self._path = lease.owner_path.parent / f"execution.cooperative-{self._id}.json"
        owner = read_owner(lease.owner_path.parent)
        if owner is None:
            raise WaitAborted("実行枠の保持者記録を読めません。" + _COOPERATIVE_ABORT_ACTION)
        current = psutil.Process()
        # ownerはロックの保持者（最上位の所有者）、coordinatorはこの制御区間を開いたプロセスを表す。
        # 多段の協調では両者が異なり、参加者は直近の協調所有者が祖先であることで生存を判定する。
        self._state: dict[str, typing.Any] = {
            "owner": dataclasses.asdict(owner),
            "coordinator": {"pid": current.pid, "create_time": current.create_time()},
            "generation": 0,
            "participants": {},
        }
        with _queue_guard(self._path.parent):
            self._save()

    def _check_thread(self) -> None:
        if threading.get_ident() != self._thread or getattr(_local, "lease", None) is not self._lease:
            raise RuntimeError("協調制御は保持中の所有スレッドから呼んでください。")

    def _save(self) -> None:
        self._path.write_text(json.dumps(self._state), encoding="utf-8")

    @contextlib.contextmanager
    def participant(self) -> collections.abc.Generator[str, None, None]:
        """子1件の参加券を登録する。子を終了・回収してから区間を閉じる。

        参加券はJSON文字列であり、環境変数から自動採用しない。
        子の終了通知だけでは回収を保証しないため、区間を閉じるまで登録を保持する。
        """
        self._check_thread()
        identifier = uuid.uuid4().hex
        with _queue_guard(self._path.parent):
            self._state = _read_control(self._path)
            self._state["participants"][identifier] = {"state": "registered"}
            self._save()
        try:
            yield json.dumps({"coordinator": self._id, "participant": identifier})
        finally:
            with _queue_guard(self._path.parent):
                if self._path.exists():
                    self._state = _read_control(self._path)
                    self._state["participants"].pop(identifier, None)
                    self._save()

    def checkpoint(self) -> bool:
        """全参加者が一時停止した区切りで譲渡し、再取得後に再開させる。

        登録中・稼働中・終了未回収の子があれば保持を続ける。
        停止を通知した後に終了した子は、回収されるまで停止扱いにしない。
        待機者が取り消された場合も停止済みの子を再開させる。
        参加券で参加した中間の協調所有者は、枠を解放せずに上位へ停止を通知し、上位の再開を待ってから子を再開させる。
        """
        self._check_thread()
        if not self._lease.acquired and self._lease.participant is None:
            raise WaitAborted("協調所有者が実行枠を保持していません。" + _COOPERATIVE_ABORT_ACTION)
        with _queue_guard(self._path.parent):
            self._state = _read_control(self._path)
            if any(value["state"] != "paused" or not _is_alive(value) for value in self._state["participants"].values()):
                return False
        try:
            yielded = self._lease.participant.checkpoint() if self._lease.participant is not None else _yield_lease()
        except BaseException:
            self.close()
            raise
        with _queue_guard(self._path.parent):
            self._state = _read_control(self._path)
            self._state["generation"] += 1
            self._state["yielded"] = yielded
            for value in self._state["participants"].values():
                if value["state"] == "paused":
                    value["state"] = "active"
            self._save()
        return yielded

    def close(self) -> None:
        """制御区間を閉じ、停止中の子の再開を拒否する。"""
        self._check_thread()
        with _queue_guard(self._path.parent):
            self._path.unlink(missing_ok=True)


def _read_control(path: pathlib.Path) -> dict[str, typing.Any]:
    try:
        value: dict[str, typing.Any] = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise WaitAborted("協調区間が閉じられました。" + _COOPERATIVE_ABORT_ACTION) from exc
    except (OSError, ValueError) as exc:
        raise WaitAborted("協調区間を読み取れません。" + _COOPERATIVE_ABORT_ACTION) from exc
    return value


@dataclasses.dataclass
class _Participant:
    path: pathlib.Path
    identifier: str
    coordinator: SlotOwner
    """直近の協調所有者のプロセス。祖先でなくなったら協調区間が失われたとみなす。"""
    should_abort: collections.abc.Callable[[], bool] | None

    @classmethod
    def join(
        cls,
        directory: pathlib.Path,
        ticket: str,
        should_abort: collections.abc.Callable[[], bool] | None,
    ) -> "_Participant":
        try:
            raw = json.loads(ticket)
            identifiers = (raw["coordinator"], raw["participant"])
            if any(
                not isinstance(value, str) or len(value) != 32 or uuid.UUID(hex=value).hex != value for value in identifiers
            ):
                raise ValueError
        except (ValueError, TypeError, KeyError, AttributeError) as exc:
            raise ValueError("参加券が不正です。participant区間が発行した文字列をそのまま渡してください。") from exc
        path = directory / f"execution.cooperative-{identifiers[0]}.json"
        with _queue_guard(directory):
            state = _read_control(path)
            owner = SlotOwner(**state["owner"])
            coordinator = _coordinator_of(state)
            current_owner = read_owner(directory)
            registration = state["participants"].get(identifiers[1])
            if (
                current_owner is None
                or current_owner.pid != owner.pid
                or current_owner.create_time != owner.create_time
                or not _is_ancestor(coordinator)
                or registration is None
            ):
                raise WaitAborted("参加区間と現在の所有者が一致しません。" + _COOPERATIVE_ABORT_ACTION)
            # 同じ参加券は、参加中のプロセスが生存する間は別のプロセスに使わせない（子孫による重複参加を含む）。
            # 参加を終えた後と、参加中のプロセスが異常終了した後は、同じツールが順に起動する次のプロセスが使える。
            if registration["state"] != "registered" and _is_alive(registration):
                raise WaitAborted(
                    "参加券は別のプロセスが参加中です。参加券を受け取ったプロセスは子孫へ同じ参加券を渡さないでください。"
                    + _COOPERATIVE_ABORT_ACTION
                )
            current = psutil.Process()
            registration.update(state="active", pid=current.pid, create_time=current.create_time())
            path.write_text(json.dumps(state), encoding="utf-8")
        return cls(path, identifiers[1], coordinator, should_abort)

    def checkpoint(self) -> bool:
        if self.should_abort is not None and self.should_abort():
            raise WaitAborted("協調参加した子の処理が取り消されました。" + _COOPERATIVE_ABORT_ACTION)
        with _queue_guard(self.path.parent):
            state = _read_control(self.path)
            if self.identifier not in state["participants"]:
                raise WaitAborted("協調参加の登録が失われました。" + _COOPERATIVE_ABORT_ACTION)
            if not _is_ancestor(self.coordinator):
                raise WaitAborted("協調所有者が終了しました。" + _COOPERATIVE_ABORT_ACTION)
            if not _waiters(self.path.parent):
                return False
            state["participants"][self.identifier]["state"] = "paused"
            generation = state["generation"]
            self.path.write_text(json.dumps(state), encoding="utf-8")
        while True:
            if self.should_abort is not None and self.should_abort():
                raise WaitAborted("協調参加した子の処理が取り消されました。" + _COOPERATIVE_ABORT_ACTION)
            with _queue_guard(self.path.parent):
                state = _read_control(self.path)
                registration = state["participants"].get(self.identifier)
                if registration is None:
                    raise WaitAborted("協調参加の登録が失われました。" + _COOPERATIVE_ABORT_ACTION)
                if state["generation"] > generation and registration["state"] == "active":
                    return bool(state["yielded"])
            if not _is_ancestor(self.coordinator):
                raise WaitAborted("協調所有者が終了しました。" + _COOPERATIVE_ABORT_ACTION)
            time.sleep(_POLL_INTERVAL)

    def finish(self) -> None:
        # 参加を終えた参加券は登録状態へ戻し、同じツールが順に起動する次のプロセスが同じ参加券で参加できるようにする。
        # 所有者は登録状態の参加者を稼働中とみなすため、参加者のいない区間でも譲らない。
        with _queue_guard(self.path.parent):
            if self.path.exists():
                state = _read_control(self.path)
                registration = state["participants"].get(self.identifier)
                if registration is not None:
                    registration["state"] = "registered"
                    registration.pop("pid", None)
                    registration.pop("create_time", None)
                    self.path.write_text(json.dumps(state), encoding="utf-8")


def _coordinator_of(state: dict[str, typing.Any]) -> SlotOwner:
    """制御区間を開いたプロセスを返す。協調所有者の記録を持たない旧形式は保持者とみなす。"""
    owner = SlotOwner(**state["owner"])
    coordinator = state.get("coordinator")
    if coordinator is None:
        return owner
    return dataclasses.replace(owner, pid=int(coordinator["pid"]), create_time=float(coordinator["create_time"]))


def _is_alive(registration: dict[str, typing.Any]) -> bool:
    """参加者の登録に記録したプロセスが生存するかを返す。プロセスを記録していない登録は生存とみなす。"""
    if "pid" not in registration:
        return True
    try:
        process = psutil.Process(int(registration["pid"]))
        return abs(process.create_time() - float(registration["create_time"])) < _CREATE_TIME_TOLERANCE
    except psutil.Error:
        return False


def _observe(
    callback: WaitIntervalCallback | None,
    kind: typing.Literal["initial", "reacquire"],
    sequence: int,
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
    started = SlotWait(kind=kind, sequence=sequence, started_at=_now_iso())
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
            started, finished_at=_now_iso(), elapsed=round(time.monotonic() - start, 3), outcome=outcome
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


def _now_iso() -> str:
    return dt.datetime.now(dt.UTC).isoformat()


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


def _bypassable(path: pathlib.Path, now_ms: int) -> bool:
    """新規要求が前へ入れる若い取り直し要求かを返す。旧版の要求と印の無い要求の前へは置かない。"""
    parts = path.name.split("-")
    mark = parts[2] if len(parts) > 2 else ""
    if not mark.startswith(_REACQUIRE_MARK) or not mark[1:].isdigit():
        return False
    return now_ms - int(mark[1:]) < _REACQUIRE_BYPASS_SECONDS * 1000


def _allocate_sequence(waiters: list[pathlib.Path], *, reacquire: bool, now_ms: int) -> int:
    """要求の連番を決める。新規要求は末尾側に続く若い取り直し要求の前へ置く。

    旧版は末尾の連番に1を足すため、末尾追加は間隔を空けて新規要求を間に置ける番号を残す。
    間に置ける番号が無い場合は末尾へ追加し、登録順の待ちへ戻す。
    """
    last = _sequence(waiters[-1]) + _SEQUENCE_STEP if waiters else _SEQUENCE_STEP
    if reacquire:
        return last
    index = len(waiters)
    while index > 0 and _bypassable(waiters[index - 1], now_ms):
        index -= 1
    if index == len(waiters):
        return last
    lower = _sequence(waiters[index - 1]) if index > 0 else -1
    upper = _sequence(waiters[index])
    if upper - lower < 2:
        return last
    return (lower + upper) // 2


@contextlib.contextmanager
def _request(directory: pathlib.Path, *, reacquire: bool = False) -> collections.abc.Generator[pathlib.Path, None, None]:
    with _queue_guard(directory):
        waiters = _waiters(directory)
        now_ms = int(time.time() * 1000)
        # 連番の割当と要求ロック取得を同じ区間で行い、他の走査による未登録要求の回収を防ぐ。
        sequence = _allocate_sequence(waiters, reacquire=reacquire, now_ms=now_ms)
        mark = f"{_REACQUIRE_MARK}{now_ms:013d}-" if reacquire else ""
        with tempfile.NamedTemporaryFile(prefix=f"execution.wait-{sequence:020d}-{mark}", dir=directory, delete=False) as file:
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
            raise WaitAborted("実行枠の取得待機が取り消されました。" + _COOPERATIVE_ABORT_ACTION)
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
