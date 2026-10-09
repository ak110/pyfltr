"""自ら起動したプロセスと子孫を追跡し、時間上限・中断時に停止して終端を確かめる。

`subprocess.run(timeout=...)`は直接の子だけを停止するため、pyfltrが新しいセッションで起動する
ツールの子孫を取り逃す。本モジュールは起動時に得たプロセスの子孫を実行中に定期列挙して保持し、
停止時は親を止める前に子孫を再列挙してから、保持した全プロセスを停止して終端を確認する。
停止対象は自ら起動したプロセスの子孫として観測したものに限り、名前検索では選ばない。
子孫の観測は定期列挙のため、列挙の間隔（`poll_interval`）より短い間に起動して親から切り離された子孫は追跡できない。
"""

import contextlib
import dataclasses
import os
import pathlib
import signal
import subprocess
import threading
import time
import typing

import psutil

Outcome = typing.Literal["exited", "timeout", "interrupted", "start_failed"]


@dataclasses.dataclass
class TrackedResult:
    """追跡実行の結果。"""

    argv: list[str]
    cwd: str
    outcome: Outcome
    """`exited`は自然終了、`timeout`は時間上限到達、`interrupted`は中断要求、`start_failed`は起動失敗。"""
    returncode: int | None
    elapsed: float
    stdout_path: str
    stderr_path: str
    descendants: list[dict[str, typing.Any]] = dataclasses.field(default_factory=list)
    """実行中に観測した子孫（pid・name・cmdline）。"""
    stopped: list[int] = dataclasses.field(default_factory=list)
    """停止処理の対象としたpid（自然終了後に残存していた子孫を含む）。"""
    survivors: list[int] = dataclasses.field(default_factory=list)
    """停止処理の後も終端を確認できなかったpid。空でなければ停止失敗。"""
    error: str | None = None

    @property
    def stop_failed(self) -> bool:
        """停止処理の後に終端を確認できないプロセスが残ったか。"""
        return bool(self.survivors)

    def to_dict(self) -> dict[str, typing.Any]:
        """JSONへ保存する辞書を返す。"""
        return dataclasses.asdict(self) | {"stop_failed": self.stop_failed}


class _Tracker:
    """起動したプロセスを根とする子孫の集合を保持する。

    中間のプロセスが先に終了すると孫は別の親へ付け替えられ、根からの列挙では辿れなくなる。
    そのため既知の生存プロセスそれぞれの子も列挙して集合へ加える。
    """

    def __init__(self, root: psutil.Process) -> None:
        self.root = root
        self.known: dict[tuple[int, float], psutil.Process] = {}
        self.info: dict[tuple[int, float], dict[str, typing.Any]] = {}

    def refresh(self) -> None:
        parents = [self.root, *self.known.values()]
        for parent in parents:
            with contextlib.suppress(psutil.Error):
                if not parent.is_running():
                    continue
                for child in parent.children(recursive=True):
                    self._add(child)

    def _add(self, process: psutil.Process) -> None:
        try:
            key = (process.pid, process.create_time())
        except psutil.Error:
            return
        if key in self.known:
            return
        self.known[key] = process
        record: dict[str, typing.Any] = {"pid": process.pid}
        with contextlib.suppress(psutil.Error):
            record["name"] = process.name()
            record["cmdline"] = process.cmdline()
        self.info[key] = record

    def alive(self) -> list[psutil.Process]:
        result: list[psutil.Process] = []
        for process in self.known.values():
            with contextlib.suppress(psutil.Error):
                if process.is_running() and process.status() != psutil.STATUS_ZOMBIE:
                    result.append(process)
        return result


def _stop(processes: list[psutil.Process], *, group_leader: int | None, grace: float) -> list[int]:
    """プロセスへ終了要求を送り、猶予後に強制終了して、終端を確認できないpidを返す。"""
    if group_leader is not None and os.name != "nt":
        # 起動時に作成した新しいセッション（pgid == 起動pid）だけを対象にする。
        with contextlib.suppress(ProcessLookupError, PermissionError):
            os.killpg(group_leader, signal.SIGTERM)  # type: ignore[attr-defined,unused-ignore]  # pyright: ignore[reportAttributeAccessIssue]  # ty: ignore  # pylint: disable=no-member
    for process in processes:
        with contextlib.suppress(psutil.Error):
            process.terminate()
    _, alive = psutil.wait_procs(processes, timeout=grace)
    for process in alive:
        with contextlib.suppress(psutil.Error):
            process.kill()
    _, still_alive = psutil.wait_procs(alive, timeout=grace)
    survivors: list[int] = []
    for process in still_alive:
        with contextlib.suppress(psutil.Error):
            if process.is_running() and process.status() != psutil.STATUS_ZOMBIE:
                survivors.append(process.pid)
    return survivors


def run_tracked(
    argv: list[str],
    *,
    cwd: pathlib.Path,
    env: dict[str, str] | None,
    timeout: float,
    stdout_path: pathlib.Path,
    stderr_path: pathlib.Path,
    stop_event: threading.Event | None = None,
    poll_interval: float = 0.1,
    grace: float = 5.0,
) -> TrackedResult:
    """コマンドを起動し、子孫を追跡しながら終了・時間上限・中断要求のいずれかまで待つ。

    標準出力・標準エラーは指定ファイルへ直接書き、途中で停止した場合も途中までの出力を残す。
    """
    started = time.monotonic()
    result = TrackedResult(
        argv=list(argv),
        cwd=str(cwd),
        outcome="exited",
        returncode=None,
        elapsed=0.0,
        stdout_path=str(stdout_path),
        stderr_path=str(stderr_path),
    )
    kwargs: dict[str, typing.Any] = {}
    if os.name == "nt":
        kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP  # type: ignore[attr-defined,unused-ignore]  # pyright: ignore[reportAttributeAccessIssue]  # ty: ignore  # pylint: disable=no-member
    else:
        kwargs["start_new_session"] = True
    with stdout_path.open("wb") as stdout, stderr_path.open("wb") as stderr:
        try:
            proc = subprocess.Popen(  # pylint: disable=consider-using-with
                argv, cwd=cwd, env=env, stdin=subprocess.DEVNULL, stdout=stdout, stderr=stderr, **kwargs
            )
        except OSError as exc:
            result.outcome = "start_failed"
            result.error = f"{type(exc).__name__}: {exc}"
            result.elapsed = time.monotonic() - started
            return result
        try:
            root = psutil.Process(proc.pid)
        except psutil.Error:
            root = None
        tracker = _Tracker(root) if root is not None else None
        deadline = started + timeout
        while True:
            if tracker is not None:
                tracker.refresh()
            if proc.poll() is not None:
                break
            if stop_event is not None and stop_event.is_set():
                result.outcome = "interrupted"
                break
            if time.monotonic() >= deadline:
                result.outcome = "timeout"
                break
            time.sleep(poll_interval)

        if tracker is not None:
            tracker.refresh()
        targets = tracker.alive() if tracker is not None else []
        if result.outcome != "exited":
            # 親を止める前に子孫を確定済みのため、親の消失後も子孫を停止できる。
            with contextlib.suppress(psutil.Error):
                if root is not None:
                    targets.append(root)
            result.stopped = [process.pid for process in targets]
            result.survivors = _stop(targets, group_leader=proc.pid, grace=grace)
            with contextlib.suppress(subprocess.TimeoutExpired):
                proc.wait(timeout=grace)
            if proc.poll() is None:
                result.survivors = sorted({*result.survivors, proc.pid})
        elif targets:
            # 自然終了後も残る子孫は停止し、停止したことを記録する。
            result.stopped = [process.pid for process in targets]
            result.survivors = _stop(targets, group_leader=None, grace=grace)
        result.returncode = proc.poll()
        if tracker is not None:
            result.descendants = list(tracker.info.values())
    result.elapsed = round(time.monotonic() - started, 3)
    return result
