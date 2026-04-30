"""プロセス管理。"""

import atexit
import contextlib
import os
import shutil
import signal
import subprocess
import threading
import typing

import psutil

from pyfltr.command.env import _get_env_path

logger = __import__("logging").getLogger(__name__)


class ProcessRegistry:
    """実行中サブプロセスのスレッドセーフな登録簿。

    グローバル変数による直接管理を本クラスに集約し、テストから差し替え可能な構造にする。
    """

    def __init__(self) -> None:
        # サブプロセスのリストとロック。_active_processes / _active_processes_lockとして
        # モジュール外からも参照できるよう公開属性として定義する。
        self.processes: list[subprocess.Popen[str]] = []
        self.lock = threading.Lock()

    def add(self, proc: "subprocess.Popen[str]") -> None:
        """ロック下でプロセスをリストに追加する。"""
        with self.lock:
            self.processes.append(proc)

    def remove(self, proc: "subprocess.Popen[str]") -> None:
        """ロック下でプロセスをリストから削除する。存在しない場合は無視する。"""
        with self.lock, contextlib.suppress(ValueError):
            self.processes.remove(proc)

    def snapshot(self) -> "list[subprocess.Popen[str]]":
        """ロック下でリストのコピーを返す（terminate_all用）。"""
        with self.lock:
            return list(self.processes)

    def terminate_all(self, *, timeout: float) -> None:
        """全プロセスとその子孫を停止する。

        snapshotを取って各プロセスを `_kill_process_tree` で停止する。
        """
        for proc in self.snapshot():
            with contextlib.suppress(OSError):
                _kill_process_tree(proc, timeout=timeout)

    def cleanup(self) -> None:
        """Atexit用クリーンアップ（タイムアウト1秒で全プロセスを停止）。"""
        self.terminate_all(timeout=1.0)


_DEFAULT_REGISTRY = ProcessRegistry()

# 既存コードおよびテストコードとの互換性のため、ProcessRegistry内部の
# リストとロックをモジュール変数として公開する。
# テストが直接_active_processes.append() / remove() / _active_processes_lockを
# 使う箇所があるため、同一オブジェクトへの参照として維持する。
_active_processes = _DEFAULT_REGISTRY.processes
_active_processes_lock = _DEFAULT_REGISTRY.lock


def set_default_registry(registry: ProcessRegistry) -> None:
    """デフォルトのプロセスレジストリを差し替える（テスト用経路）。"""
    global _DEFAULT_REGISTRY, _active_processes, _active_processes_lock  # pylint: disable=global-statement
    _DEFAULT_REGISTRY = registry
    _active_processes = registry.processes
    _active_processes_lock = registry.lock


class InterruptedExecution(Exception):
    """TUIから協調停止が要求されたことを示す例外。

    `_run_subprocess` が `is_interrupted` コールバックで中断指示を検知した際に送出する。
    呼び出し側（`ui._execute_command`）で捕捉し、当該コマンドを `skipped` 結果として置き換える。
    """


def _kill_process_tree(proc: "subprocess.Popen[str]", *, timeout: float) -> None:
    """Procとその子孫をまとめて停止する。

    `_run_subprocess` はPOSIXでは `start_new_session=True`、Windowsでは
    `CREATE_NEW_PROCESS_GROUP` でPopenを起動している。pytest-xdistのように
    サブプロセスが更にサブプロセスをforkしてパイプを継承するツールでは、
    親だけ `terminate()` しても孫がstdoutを握り続け `for line in proc.stdout`
    がEOFを受け取れない。これを回避するため、親子孫を一括で停止する。

    POSIX: `os.killpg(pgid, SIGTERM)` → `timeout` 秒待機 → 残存に
    `os.killpg(pgid, SIGKILL)`。`start_new_session=True` によりpgidはproc.pidと
    一致するので、親が既にreapされていてもpid=pgidとして停止シグナルを届けられる。

    Windows: 完全なJob Objectを導入しない簡易実装。親消失後に `children(recursive=True)`
    では子孫を辿れないため、先に列挙して `terminate()` を送り、その後 `wait_procs` で
    残存に `kill()` を送る。サブプロセスが更に分離Job Objectを使う場合は取り逃すが、
    現状のpyfltr対応ツールでは問題にならない範囲とする。
    """
    targets: list[psutil.Process] = []
    if os.name == "nt":
        # 親消失後に辿れなくなるため、事前に子孫pid集合を取得する。
        with contextlib.suppress(psutil.NoSuchProcess, psutil.AccessDenied):
            parent = psutil.Process(proc.pid)
            targets = parent.children(recursive=True)
        with contextlib.suppress(OSError):
            proc.terminate()
        for child in targets:
            with contextlib.suppress(psutil.NoSuchProcess, psutil.AccessDenied):
                child.terminate()
    else:
        # os.killpg / os.getpgid / signal.SIGKILLはPOSIX専用でWindows型スタブに未定義。
        # os.nameガード下なので実行時は安全。型チェッカーの誤検知だけ局所コメントで抑止する。
        try:
            pgid = os.getpgid(proc.pid)  # type: ignore[attr-defined,unused-ignore]  # pyright: ignore[reportAttributeAccessIssue]  # ty: ignore  # pylint: disable=no-member
        except ProcessLookupError:
            # 親プロセスが既にreapされている。start_new_session=Trueにより
            # pgid == pidとして設定されていたはずなのでpidをそのまま使う。
            pgid = proc.pid
        with contextlib.suppress(ProcessLookupError, PermissionError):
            os.killpg(pgid, signal.SIGTERM)  # type: ignore[attr-defined,unused-ignore]  # pyright: ignore[reportAttributeAccessIssue]  # ty: ignore  # pylint: disable=no-member

    # psutil.Processは失敗時も自身を含めて扱うためNoneチェックのうえでwait対象に含める。
    wait_targets: list[psutil.Process] = list(targets)
    with contextlib.suppress(psutil.NoSuchProcess):
        wait_targets.append(psutil.Process(proc.pid))

    _, alive = psutil.wait_procs(wait_targets, timeout=timeout)

    # 残存プロセスへSIGKILL / killを送る。
    if alive:
        if os.name == "nt":
            for child in alive:
                with contextlib.suppress(psutil.NoSuchProcess, psutil.AccessDenied):
                    child.kill()
        else:
            try:
                pgid = os.getpgid(proc.pid)  # type: ignore[attr-defined,unused-ignore]  # pyright: ignore[reportAttributeAccessIssue]  # ty: ignore  # pylint: disable=no-member
            except ProcessLookupError:
                pgid = proc.pid
            with contextlib.suppress(ProcessLookupError, PermissionError):
                os.killpg(pgid, signal.SIGKILL)  # type: ignore[attr-defined,unused-ignore]  # pyright: ignore[reportAttributeAccessIssue]  # ty: ignore  # pylint: disable=no-member
        _, still_alive = psutil.wait_procs(alive, timeout=timeout)
        if still_alive:
            remaining_pids = [p.pid for p in still_alive]
            logger.warning("プロセスツリー停止後に残存するプロセスあり: pids=%s", remaining_pids)


atexit.register(_DEFAULT_REGISTRY.cleanup)


def terminate_active_processes(*, timeout: float = 5.0) -> None:
    """実行中のすべての子プロセスと子孫にterminate() → kill() を送る。

    --fail-fastやTUI Ctrl+C協調停止で、並列実行中の他ツールを止めるために呼ばれる。
    `_kill_process_tree` 経由でプロセスグループ単位 （POSIX） / 子孫pid列挙 （Windows）
    で停止するため、pytest-xdistのようにPopen子が更にサブプロセスをforkする
    ツールでも確実に停止する。
    """
    _DEFAULT_REGISTRY.terminate_all(timeout=timeout)


def _terminate_and_drop(proc: "subprocess.Popen[str]") -> None:
    """実行中procとその子孫を停止し `_active_processes` から外す。

    TUI協調停止経路で使う。`with subprocess.Popen(...)` の__exit__は子が残っていても
    `wait()` で止まってしまうため、`InterruptedExecution` を送出する前に本関数で
    確実に子を終了させる。pytest-xdistなど孫プロセスをforkするツールを想定し、
    `_kill_process_tree` でプロセスツリー単位で停止する。
    """
    with contextlib.suppress(OSError):
        _kill_process_tree(proc, timeout=5.0)
    with contextlib.suppress(subprocess.TimeoutExpired, OSError):
        proc.wait(timeout=5.0)
    _DEFAULT_REGISTRY.remove(proc)


def _run_subprocess(
    commandline: list[str],
    env: dict[str, str],
    on_output: typing.Callable[[str], None] | None = None,
    *,
    is_interrupted: typing.Callable[[], bool] | None = None,
    on_subprocess_start: typing.Callable[[], None] | None = None,
    on_subprocess_end: typing.Callable[[], None] | None = None,
) -> subprocess.CompletedProcess[str]:
    """サブプロセスの実行 （Popenベース）。

    --fail-fastで並列実行中の他プロセスを外部スレッドからterminate() できるよう、
    subprocess.runの経路もPopenに統一し `_active_processes` に登録する。
    `on_output` が指定されている場合は逐次コールバックを呼び、未指定時は最後に
    全出力をまとめて返す。

    `is_interrupted` が指定された場合、（1） `Popen` 呼び出し直前、（2） `Popen` 生成直後、
    （3） stdout読み出しループの各イテレーション冒頭の3点で中断指示を確認し、真なら
    当該procを確実に終了させてから `InterruptedExecution` を送出する。TUI協調停止経路で
    使う。`on_subprocess_start` / `on_subprocess_end` はsubprocessが実際に動いている
    区間を追跡するためのフック（UI側で「実行中コマンド集合」を正確に保つのに使う）。
    start後は必ずfinallyでendを呼ぶため、Ctrl+Cスナップショットにフック外の時間帯が
    混入しない。

    Windowsでは `subprocess.Popen` を `shell=False` でリスト渡しにすると
    `.exe` / `.cmd` 等の拡張子付きファイルをPATHから自動解決しないため、
    ここで `shutil.which` を使って `commandline[0]` をフルパスへ解決する。
    引数の `commandline` は書き換えず、Popenに渡す一時リストのみで差し替える
    （CommandResult.commandlineやretry_commandに解決後のフルパスが混入して
    ポータビリティが損なわれるのを避けるため）。解決探索対象PATHはPopenに
    渡す `env` のPATH値と一致させる（隔離したenvで見えない実行ファイルを
    起動したり、逆にenvでだけ見える実行ファイルを解決できない事故を避ける）。
    Windowsでは環境変数名が大文字小文字非区別のためenvキーを非依存探索する。
    解決できなかった場合は元のコマンド名のままPopenに渡し、既存の
    FileNotFoundError経路でrc=127の `CompletedProcess` に変換する。
    """
    popen_commandline = commandline
    env_path = _get_env_path(env)
    resolved = shutil.which(commandline[0], path=env_path)
    if resolved is not None and resolved != commandline[0]:
        popen_commandline = [resolved, *commandline[1:]]
    # （1） Popen直前の中断チェック。procがまだ存在しないのでそのまま送出できる。
    if is_interrupted is not None and is_interrupted():
        raise InterruptedExecution
    # OS別のプロセスグループ分離オプション。pytest-xdistなど孫プロセスを
    # forkするツールの中断時に、親子孫をまとめて停止できるようにする。
    popen_extra: dict[str, typing.Any] = {}
    if os.name == "nt":
        # CREATE_NEW_PROCESS_GROUPはWindows専用の定数。getattrの3引数形式を使うと
        # ruff B009のgetattr→属性アクセス変換対象外になるため、型チェッカー誤検知を回避できる。
        popen_extra["creationflags"] = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
    else:
        popen_extra["start_new_session"] = True
    try:
        with subprocess.Popen(
            popen_commandline,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            env=env,
            text=True,
            encoding="utf-8",
            errors="backslashreplace",
            **popen_extra,
        ) as proc:
            _DEFAULT_REGISTRY.add(proc)
            subprocess_started = False
            try:
                if on_subprocess_start is not None:
                    on_subprocess_start()
                subprocess_started = True
                # （2） Popen生成直後の中断チェック。_active_processes登録済みなので
                # _terminate_and_dropで自己登録を外してから送出する。
                if is_interrupted is not None and is_interrupted():
                    _terminate_and_drop(proc)
                    raise InterruptedExecution

                output_lines: list[str] = []
                assert proc.stdout is not None
                for line in proc.stdout:
                    # （3） 各イテレーション冒頭の中断チェック。
                    if is_interrupted is not None and is_interrupted():
                        _terminate_and_drop(proc)
                        raise InterruptedExecution
                    output_lines.append(line)
                    if on_output is not None:
                        on_output(line)
                proc.wait()
                return subprocess.CompletedProcess(
                    args=commandline,
                    returncode=proc.returncode,
                    stdout="".join(output_lines),
                )
            finally:
                if subprocess_started and on_subprocess_end is not None:
                    on_subprocess_end()
                _DEFAULT_REGISTRY.remove(proc)
    except FileNotFoundError as e:
        message = f"実行ファイルが見つかりません: {commandline[0]} ({e})\n"
        if on_output is not None:
            on_output(message)
        return subprocess.CompletedProcess(
            args=commandline,
            returncode=127,
            stdout=message,
        )
