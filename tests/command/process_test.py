import contextlib
import os
import pathlib
import subprocess
import sys
import textwrap
import time
import typing

import psutil
import pytest

import pyfltr.command.core_
import pyfltr.command.dispatcher
import pyfltr.command.env
import pyfltr.command.glab
import pyfltr.command.mise
import pyfltr.command.only_failed
import pyfltr.command.process
import pyfltr.command.runner
import pyfltr.command.slow_tests
import pyfltr.command.subprojects
import pyfltr.command.targets
import pyfltr.command.tool_resolution
import pyfltr.command.two_step.base
import pyfltr.command.two_step.prettier
import pyfltr.command.two_step.ruff
import pyfltr.config.config
import pyfltr.config.model
import pyfltr.paths
import pyfltr.run_options
import pyfltr.state.cache
import pyfltr.state.only_failed
import pyfltr.tools
import pyfltr.warnings_


def test_run_subprocess_file_not_found_returns_127() -> None:
    """存在しない実行ファイルを指定しても例外を送出せずrc=127を返す。"""
    result = pyfltr.command.process.run_subprocess(
        ["this-command-definitely-does-not-exist-xyz-1234"],
        env={"PATH": "/nonexistent"},
    )
    assert result.returncode == 127
    assert "見つかりません" in result.stdout


class _FakePopen:
    """`subprocess.Popen`を差し替えるための最小スタブ。

    `run_subprocess`のテスト用。Popenのwith文経由での利用とstdout逐次読み込み・
    wait()までを満たす最小限の振る舞いを提供する。起動引数はクラス変数
    `last_args_holder`のリスト内に追記する（None判定を避けてpylintの型縮めに頼らない）。
    """

    last_args_holder: list[list[str]] = []

    def __init__(self, args: list[str], **kwargs: typing.Any) -> None:
        del kwargs  # Popen互換の追加引数を受け取るのみ
        _FakePopen.last_args_holder.append(list(args))
        self.returncode = 0
        self.stdout: typing.Iterator[str] = iter([])

    def __enter__(self) -> "_FakePopen":
        """with文のエントリー。"""
        return self

    def __exit__(self, exc_type: typing.Any, exc: typing.Any, tb: typing.Any) -> None:
        """with文のイグジット。"""
        del exc_type, exc, tb  # contextmanager互換の引数を受け取るのみ

    def wait(self) -> int:
        """プロセス終了待ち。ダミーで直ちにreturncodeを返す。"""
        return self.returncode


@pytest.mark.parametrize("command", ["pre-commit", "prek"])
def test_run_subprocess_resolves_command_via_shutil_which(mocker, command: str) -> None:
    """`commandline[0]`が`shutil.which`で解決されてPopenに渡る。"""
    _FakePopen.last_args_holder = []
    resolved = f"/resolved/{command}"
    mocker.patch("pyfltr.command.process.shutil.which", return_value=resolved)
    mocker.patch("pyfltr.command.process.subprocess.Popen", _FakePopen)

    pyfltr.command.process.run_subprocess([command, "run", "--files", "foo.py"], {"PATH": "/usr/bin"})

    assert _FakePopen.last_args_holder == [[resolved, "run", "--files", "foo.py"]]


def test_run_subprocess_keeps_original_name_when_unresolved(mocker) -> None:
    """`shutil.which`がNoneなら元のコマンド名のままPopenに渡る。"""
    _FakePopen.last_args_holder = []
    mocker.patch("pyfltr.command.process.shutil.which", return_value=None)
    mocker.patch("pyfltr.command.process.subprocess.Popen", _FakePopen)

    pyfltr.command.process.run_subprocess(["missing-tool", "arg"], {"PATH": "/usr/bin"})

    assert _FakePopen.last_args_holder == [["missing-tool", "arg"]]


def test_run_subprocess_resolves_via_env_path(mocker, tmp_path: pathlib.Path, monkeypatch) -> None:
    """`os.environ["PATH"]`では見えず`env["PATH"]`にだけある実行ファイルが解決される。

    解決探索対象PATHとPopenへ渡す`env["PATH"]`の一致要件に対するリグレッション防止。
    """
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    # WindowsのshUtil.whichはPATHEXTに列挙された拡張子で実行ファイルを判定するため、
    # ダミー実行ファイル名を`.bat`にする（POSIXでは実行属性0o755で判定される）。
    # 本テストの主眼はenv["PATH"]経由での解決可否であり、拡張子/実行属性の違いは付随的。
    if os.name == "nt":
        target = bin_dir / "faketool.bat"
        target.write_text("")
    else:
        target = bin_dir / "faketool"
        target.write_text("")
        target.chmod(0o755)

    # os.environのPATHからはbin_dirを除外する（env["PATH"]経由で解決することの検証）
    monkeypatch.setenv("PATH", "/nonexistent-pyfltr-test-path")

    _FakePopen.last_args_holder = []
    mocker.patch("pyfltr.command.process.subprocess.Popen", _FakePopen)

    pyfltr.command.process.run_subprocess(["faketool"], {"PATH": str(bin_dir)})

    # 解決されたパスが渡ること（先頭要素が/tmp/.../bin/faketool*を指す）
    assert len(_FakePopen.last_args_holder) == 1
    resolved = pathlib.Path(_FakePopen.last_args_holder[0][0])
    assert resolved.name.startswith("faketool")
    assert resolved.parent == bin_dir


def test_run_subprocess_does_not_mutate_commandline(mocker) -> None:
    """呼び出し側の`commandline`リストは書き換えない（retry_command等に影響するため）。"""
    _FakePopen.last_args_holder = []
    mocker.patch("pyfltr.command.process.shutil.which", return_value="/resolved/tool")
    mocker.patch("pyfltr.command.process.subprocess.Popen", _FakePopen)

    original = ["tool", "arg"]
    pyfltr.command.process.run_subprocess(original, {"PATH": "/usr/bin"})

    assert original == ["tool", "arg"]


def _spawn_parent_with_child(script: str) -> tuple[subprocess.Popen[str], int, int]:
    """Pythonスクリプトをsubprocessとして起動し親pidと子pidを取得する。

    スクリプトは最初の1行に自身と子のpidを空白区切りでprintする契約。
    Popenは`start_new_session=True`で起動する（本番と同じ条件）。
    """
    # pylint: disable=consider-using-with
    # テスト対象の`active_processes`へ外から登録するため、`with`構文では
    # スコープ外でprocを扱えない。各テストのfinallyでprocとstdoutを解放する。
    proc = subprocess.Popen(
        [sys.executable, "-u", "-c", script],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        start_new_session=True,
    )
    try:
        assert proc.stdout is not None
        line = proc.stdout.readline().strip()
        parent_pid_str, child_pid_str = line.split()
    except BaseException:
        # 起動直後の読み取りに失敗した場合、呼び出し側はprocを受け取れず解放できない。
        # ここでプロセスとパイプの双方を解放してから送出し直す。
        proc.kill()
        if proc.stdout is not None:
            proc.stdout.close()
        proc.wait(timeout=2.0)
        raise
    return proc, int(parent_pid_str), int(child_pid_str)


def _wait_gone(pids: list[int], *, timeout: float) -> list[int]:
    """`pids`が全て消滅するまで最大`timeout`秒待つ。残存するpidを返す。

    initを持たないコンテナー環境では親reapが行われずzombieが残存するため、
    zombie状態は消滅扱いとする（プロセスツリーは既に停止しており、
    `terminate_active_processes`の責務は果たされている）。
    """

    def _is_alive(pid: int) -> bool:
        try:
            return psutil.Process(pid).status() != psutil.STATUS_ZOMBIE
        except psutil.NoSuchProcess:
            return False

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        alive = [pid for pid in pids if _is_alive(pid)]
        if not alive:
            return []
        time.sleep(0.05)
    return [pid for pid in pids if _is_alive(pid)]


@pytest.mark.skipif(os.name == "nt", reason="POSIX 前提の killpg 経路を検証する")
def test_terminate_active_processes_kills_grandchild() -> None:
    """`terminate_active_processes`が孫プロセスまで確実に停止する。

    Popen子が更にサブプロセスをforkするpytest-xdist相当の構造で、
    `start_new_session=True`相当のpgid分離によりSIGTERMが全体へ届くことを検証する。
    """
    script = textwrap.dedent(
        """
        import os, time
        r, w = os.pipe()
        pid = os.fork()
        if pid == 0:
            # child: pidをpipeへ書き、あとは待機する（stdoutへは書かない）。
            os.close(r)
            os.write(w, str(os.getpid()).encode())
            os.close(w)
            while True:
                time.sleep(1)
        else:
            # 親: childのpidを読み取り、自身とchildのpidを1行にまとめて出力する。
            os.close(w)
            child_pid = int(os.read(r, 64).decode())
            os.close(r)
            print(f"{os.getpid()} {child_pid}", flush=True)
            while True:
                time.sleep(1)
        """
    )
    proc, parent_pid, child_pid = _spawn_parent_with_child(script)
    try:
        with pyfltr.command.process.active_processes_lock:
            pyfltr.command.process.active_processes.append(proc)
        assert psutil.pid_exists(parent_pid)
        assert psutil.pid_exists(child_pid)

        pyfltr.command.process.terminate_active_processes(timeout=3.0)

        remaining = _wait_gone([parent_pid, child_pid], timeout=3.0)
        assert remaining == [], f"停止できなかったpid: {remaining}"
    finally:
        with pyfltr.command.process.active_processes_lock:
            if proc in pyfltr.command.process.active_processes:
                pyfltr.command.process.active_processes.remove(proc)
        if proc.poll() is None:
            # POSIX限定パスのクリーンアップ。Windowsではskipifで到達しない。
            # 型チェッカー（pyright / ty）のattr-defined誤検知は局所コメントで抑止する。
            with contextlib.suppress(ProcessLookupError, PermissionError, OSError):
                os.killpg(os.getpgid(proc.pid), 9)  # type: ignore[attr-defined,unused-ignore]  # pyright: ignore[reportAttributeAccessIssue]  # ty: ignore  # pylint: disable=no-member
        # Popen.wait()はパイプを閉じないため、stdoutを明示的に閉じる。
        # 閉じないとGC時にFileIOが未クローズのままfinalizeされResourceWarningとなる。
        # wait()がTimeoutExpiredを送出しても解放されるよう、wait()より前に置く。
        if proc.stdout is not None:
            proc.stdout.close()
        proc.wait(timeout=2.0)


@pytest.mark.skipif(os.name == "nt", reason="POSIX 前提の killpg 経路を検証する")
def test_terminate_active_processes_parent_exited_grandchild_remains() -> None:
    """親が先にexitして孫だけがstdoutを握り残す構成でも停止できる。

    `start_new_session=True`によりpgidがproc.pidと一致するため、
    親reap後でも`os.killpg(proc.pid, SIGTERM)`で孫へ届くことを検証する。
    """
    script = textwrap.dedent(
        """
        import os, time
        pid = os.fork()
        if pid == 0:
            # grandchild役。stdoutを継承したまま待機する。
            while True:
                time.sleep(1)
        else:
            # 親だけがstdoutに出力してすぐexit。grandchildはstdoutを保持し続ける。
            print(f"{os.getpid()} {pid}", flush=True)
            os._exit(0)
        """
    )
    proc, _parent_pid, child_pid = _spawn_parent_with_child(script)
    try:
        with pyfltr.command.process.active_processes_lock:
            pyfltr.command.process.active_processes.append(proc)
        # 親は速やかにexitする。孫（子）は生存継続。
        proc.wait(timeout=2.0)
        assert psutil.pid_exists(child_pid), "孫プロセスが消えている"

        pyfltr.command.process.terminate_active_processes(timeout=3.0)

        remaining = _wait_gone([child_pid], timeout=3.0)
        assert remaining == [], f"停止できなかったpid: {remaining}"
    finally:
        with pyfltr.command.process.active_processes_lock:
            if proc in pyfltr.command.process.active_processes:
                pyfltr.command.process.active_processes.remove(proc)
        if psutil.pid_exists(child_pid):
            with contextlib.suppress(ProcessLookupError, PermissionError, OSError):
                os.kill(child_pid, 9)
        # Popen.wait()はパイプを閉じないため、stdoutを明示的に閉じる。
        if proc.stdout is not None:
            proc.stdout.close()


def _make_env() -> dict[str, str]:
    """run_subprocess に渡す最小 env。PATH 等を引き継いで Python インタープリターを解決可能にする。"""
    return dict(os.environ)


def test_run_subprocess_no_timeout_returns_completed_normally() -> None:
    """timeout=Noneでは即座に終了するsubprocessが正常完了することを確認する。"""
    proc = pyfltr.command.process.run_subprocess(
        [sys.executable, "-c", "print('ok')"],
        _make_env(),
    )
    assert isinstance(proc, subprocess.CompletedProcess)
    assert proc.returncode == 0
    assert "ok" in proc.stdout


def test_run_subprocess_raises_timeout_when_exceeded() -> None:
    """timeout超過時に `TimeoutExceededExecution` を送出することを確認する。

    `time.sleep(2)` するsubprocessを0.1秒のtimeoutで起動し、Timer発火で停止 → 例外送出に至る経路を検証する。
    """
    with pytest.raises(pyfltr.command.process.TimeoutExceededExecution) as exc_info:
        pyfltr.command.process.run_subprocess(
            [sys.executable, "-c", "import time; time.sleep(2)"],
            _make_env(),
            timeout=0.1,
        )
    # 例外オブジェクトが途中までのoutputとelapsed情報を保持する。
    assert exc_info.value.timeout == 0.1
    assert exc_info.value.elapsed >= 0.1
    # outputはsubprocessが何も出力していないため空文字列。
    assert isinstance(exc_info.value.output, str)


def test_run_process_loop_wrapper_returns_failed_completed_process() -> None:
    """`run_process_loop` がtimeout時に `returncode=124` の `CompletedProcess` を返すことを確認する。"""
    proc = pyfltr.command.process.run_process_loop(
        [sys.executable, "-c", "import time; time.sleep(2)"],
        _make_env(),
        timeout=0.1,
    )
    assert proc.returncode == pyfltr.command.process.TIMEOUT_RETURNCODE
    assert proc.returncode == 124
    assert proc.timeout_exceeded is True
    assert "Timeout exceeded after 0.1s" in proc.stdout
    # 生出力だけを読む経路（text表示・MCPのshow_run_output）でも上限の変更方法が分かる
    assert "command-timeout" in proc.stdout


def test_run_process_loop_wrapper_passthrough_normal() -> None:
    """`run_process_loop` は通常終了時に `timeout_exceeded=False` を保持する。"""
    proc = pyfltr.command.process.run_process_loop(
        [sys.executable, "-c", "print('ok')"],
        _make_env(),
        timeout=5.0,
    )
    assert proc.returncode == 0
    assert proc.timeout_exceeded is False
    assert "ok" in proc.stdout


def test_run_subprocess_interrupt_takes_priority_over_timeout() -> None:
    """`is_interrupted=True` 時はtimeout監視より先に `InterruptedExecution` が送出される。

    `is_interrupted` は `Popen` 直前にチェックされるため、timeout発火を待たずに送出される。
    既存の中断系挙動とtimeout系挙動が共存できることを確認する。
    """
    with pytest.raises(pyfltr.command.process.InterruptedExecution):
        pyfltr.command.process.run_subprocess(
            [sys.executable, "-c", "import time; time.sleep(2)"],
            _make_env(),
            is_interrupted=lambda: True,
            timeout=0.1,
        )


@pytest.mark.parametrize(
    ("returncode", "expected"),
    [
        (-9, True),
        (137, True),
        (0, False),
        (1, False),
        (124, False),
        (None, False),
    ],
)
def test_is_oom_returncode(returncode: int | None, expected: bool) -> None:
    """`is_oom_returncode` がOOM該当returncodeのみTrueを返すことを確認する。"""
    assert pyfltr.command.process.is_oom_returncode(returncode) == expected


def _make_fake_run_subprocess(returncodes: list[int]) -> typing.Callable[..., subprocess.CompletedProcess[str]]:
    """試行ごとに指定のreturncodeを返すfake `run_subprocess`。

    `returncodes` の要素を順番に消費し、尽きたら末尾の値を繰り返す。
    """
    remaining = list(returncodes)

    def fake(commandline: list[str], *_args: typing.Any, **_kwargs: typing.Any) -> subprocess.CompletedProcess[str]:
        rc = remaining.pop(0) if len(remaining) > 1 else remaining[0]
        return subprocess.CompletedProcess(args=commandline, returncode=rc, stdout="")

    return fake


def test_run_process_loop_retries_on_oom_and_succeeds(monkeypatch: pytest.MonkeyPatch) -> None:
    """OOM returncode後に成功する場合、合計2回実行されて `retry_count == 1` となることを確認する。"""
    monkeypatch.setattr(
        pyfltr.command.process,
        "run_subprocess",
        _make_fake_run_subprocess([137, 0]),
    )
    proc = pyfltr.command.process.run_process_loop(
        ["dummy"],
        _make_env(),
        retry_on_oom=True,
        retry_max_attempts=1,
    )
    assert proc.returncode == 0
    assert proc.retry_count == 1
    assert proc.timeout_exceeded is False


def test_run_process_loop_no_retry_when_retry_on_oom_false(monkeypatch: pytest.MonkeyPatch) -> None:
    """`retry_on_oom=False` のときOOM returncodeでもリトライしないことを確認する。"""
    monkeypatch.setattr(
        pyfltr.command.process,
        "run_subprocess",
        _make_fake_run_subprocess([137, 0]),
    )
    proc = pyfltr.command.process.run_process_loop(
        ["dummy"],
        _make_env(),
        retry_on_oom=False,
        retry_max_attempts=1,
    )
    assert proc.returncode == 137
    assert proc.retry_count == 0


def test_run_process_loop_no_retry_when_max_attempts_zero(monkeypatch: pytest.MonkeyPatch) -> None:
    """`retry_max_attempts=0` のときリトライしないことを確認する。"""
    monkeypatch.setattr(
        pyfltr.command.process,
        "run_subprocess",
        _make_fake_run_subprocess([137, 0]),
    )
    proc = pyfltr.command.process.run_process_loop(
        ["dummy"],
        _make_env(),
        retry_on_oom=True,
        retry_max_attempts=0,
    )
    assert proc.returncode == 137
    assert proc.retry_count == 0


def test_run_process_loop_retries_up_to_max_attempts(monkeypatch: pytest.MonkeyPatch) -> None:
    """`retry_max_attempts=2` で最大2回リトライ、3回OOMならfailed扱いになることを確認する。"""
    monkeypatch.setattr(
        pyfltr.command.process,
        "run_subprocess",
        _make_fake_run_subprocess([137, 137, 137]),
    )
    proc = pyfltr.command.process.run_process_loop(
        ["dummy"],
        _make_env(),
        retry_on_oom=True,
        retry_max_attempts=2,
    )
    assert proc.returncode == 137
    assert proc.retry_count == 2


def test_run_process_loop_no_retry_on_non_oom_returncode(monkeypatch: pytest.MonkeyPatch) -> None:
    """非OOM非ゼロreturncodeでリトライしないことを確認する。"""
    monkeypatch.setattr(
        pyfltr.command.process,
        "run_subprocess",
        _make_fake_run_subprocess([1, 0]),
    )
    proc = pyfltr.command.process.run_process_loop(
        ["dummy"],
        _make_env(),
        retry_on_oom=True,
        retry_max_attempts=1,
    )
    assert proc.returncode == 1
    assert proc.retry_count == 0


def test_run_process_loop_no_retry_count_on_normal_exit(monkeypatch: pytest.MonkeyPatch) -> None:
    """通常終了時に `retry_count == 0` であることを確認する。"""
    monkeypatch.setattr(
        pyfltr.command.process,
        "run_subprocess",
        _make_fake_run_subprocess([0]),
    )
    proc = pyfltr.command.process.run_process_loop(
        ["dummy"],
        _make_env(),
    )
    assert proc.returncode == 0
    assert proc.retry_count == 0


def test_run_process_loop_no_retry_when_timeout_exceeded(monkeypatch: pytest.MonkeyPatch) -> None:
    """timeout超過時はreturncodeがOOM相当値でもリトライしないことを確認する。

    `run_subprocess` が `TimeoutExceededExecution` を送出する経路はリトライ判定より前に
    `except` で早期returnするため、`retry_count` は0のままとなる。
    """

    def fake_timeout(commandline: list[str], *_args: typing.Any, **_kwargs: typing.Any) -> subprocess.CompletedProcess[str]:
        del commandline
        raise pyfltr.command.process.TimeoutExceededExecution(output="", elapsed=2.0, timeout=1.0)

    monkeypatch.setattr(pyfltr.command.process, "run_subprocess", fake_timeout)
    proc = pyfltr.command.process.run_process_loop(
        ["dummy"],
        _make_env(),
        retry_on_oom=True,
        retry_max_attempts=3,
    )
    assert proc.returncode == pyfltr.command.process.TIMEOUT_RETURNCODE
    assert proc.timeout_exceeded is True
    assert proc.retry_count == 0


def test_run_process_loop_oom_then_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    """OOM returncode後のリトライでtimeout超過が発生した場合の動作を確認する。

    1回目の `run_subprocess` でOOM returncode（137）を返し、
    2回目のリトライで `TimeoutExceededExecution` を送出するケースを検証する。
    期待値: `returncode == TIMEOUT_RETURNCODE`、`timeout_exceeded == True`、`retry_count == 1`。
    """
    call_count = 0

    def fake_oom_then_timeout(
        commandline: list[str], *_args: typing.Any, **_kwargs: typing.Any
    ) -> subprocess.CompletedProcess[str]:
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            return subprocess.CompletedProcess(args=commandline, returncode=137, stdout="")
        raise pyfltr.command.process.TimeoutExceededExecution(output="", elapsed=2.0, timeout=1.0)

    monkeypatch.setattr(pyfltr.command.process, "run_subprocess", fake_oom_then_timeout)
    proc = pyfltr.command.process.run_process_loop(
        ["dummy"],
        _make_env(),
        retry_on_oom=True,
        retry_max_attempts=1,
    )
    assert proc.returncode == pyfltr.command.process.TIMEOUT_RETURNCODE
    assert proc.timeout_exceeded is True
    assert proc.retry_count == 1


def test_timeout_reports_remaining_processes_with_action(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """停止を試みても残るプロセスがある場合は、そのPIDと停止を促す案内を出力する。"""

    class _RemainingProcess:
        pid = 424242

        def kill(self) -> None:
            pass

    def _wait_procs(procs: typing.Any, timeout: float | None = None) -> tuple[list[typing.Any], list[typing.Any]]:
        del procs, timeout
        # 停止待ちが親プロセスのEOFより遅れて終わる状況を再現し、
        # 警告の出力を待たずに戻らないことを確かめる。
        time.sleep(0.5)
        return [], [_RemainingProcess()]

    monkeypatch.setattr(pyfltr.command.process.psutil, "wait_procs", _wait_procs)
    with caplog.at_level("WARNING", logger="pyfltr.command.process"):
        proc = pyfltr.command.process.run_process_loop(
            [sys.executable, "-c", "import time; time.sleep(2)"],
            _make_env(),
            timeout=0.1,
        )
    assert proc.timeout_exceeded is True
    assert "424242" in caplog.text
    assert "表示したPIDのプロセスを停止してください" in caplog.text
