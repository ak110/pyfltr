"""UI関連のテストコード。"""

# pylint: disable=protected-access

import argparse
import unittest.mock

import pyfltr.command
import pyfltr.config.config
import pyfltr.output.ui
import pyfltr.state.stage_runner
import pyfltr.warnings_


def test_ctrl_c_double_press_handling() -> None:
    """Ctrl+Cの2回押し処理のテスト。

    協調中断方式のため、2回目Ctrl+Cでは`exit`は呼ばず
    `_interrupted=True`と`terminate_active_processes`呼び出しで停止する。
    """
    args = argparse.Namespace()
    args.targets = []
    args.verbose = False

    app = pyfltr.output.ui.UIApp(
        ["black"],
        args,
        pyfltr.command.ExecutionBaseContext(
            config=pyfltr.config.config.create_default_config(), all_files=[], cache_store=None, cache_run_id=None
        ),
    )

    assert app.last_ctrl_c_time == 0.0
    assert app.ctrl_c_timeout == 1.0
    assert app._interrupted is False

    mock_event = unittest.mock.MagicMock()
    mock_event.key = "ctrl+c"

    with (
        unittest.mock.patch.object(app, "exit") as mock_exit,
        unittest.mock.patch.object(app, "notify") as mock_notify,
        unittest.mock.patch("pyfltr.command.terminate_active_processes") as mock_terminate,
    ):
        # 1回目のCtrl+C: 2回目を促す通知
        app.on_key(mock_event)
        mock_exit.assert_not_called()
        mock_notify.assert_called_once_with("終了するには 1 秒以内にもう一度 Ctrl+C を押してください。")

        # 1秒以内の2回目のCtrl+C: 協調中断を開始（exitは呼ばれない）
        app.on_key(mock_event)
        mock_exit.assert_not_called()
        assert app._interrupted is True
        mock_terminate.assert_called_once()


def test_ctrl_c_force_exit_after_interrupted() -> None:
    """協調中断済みの状態でさらにCtrl+C×2を受けたら強制終了する。

    強制終了経路では`_exit_requested=True` → `terminate_active_processes()` →
    `exit(return_code=130)`の順で処理される。孫プロセスが残ってworkerが閉じた
    イベントループへ`call_from_thread`し続ける事象の退路として動く。
    """
    args = argparse.Namespace()
    args.targets = []
    args.verbose = False

    app = pyfltr.output.ui.UIApp(
        ["black"],
        args,
        pyfltr.command.ExecutionBaseContext(
            config=pyfltr.config.config.create_default_config(), all_files=[], cache_store=None, cache_run_id=None
        ),
    )
    # 協調中断済みの状態にする
    app._interrupted = True

    mock_event = unittest.mock.MagicMock()
    mock_event.key = "ctrl+c"

    call_order: list[str] = []
    with (
        unittest.mock.patch.object(app, "exit", side_effect=lambda **_kw: call_order.append("exit")) as mock_exit,
        unittest.mock.patch.object(app, "notify"),
        unittest.mock.patch(
            "pyfltr.command.terminate_active_processes",
            side_effect=lambda **_kw: call_order.append("terminate"),
        ) as mock_terminate,
    ):
        # 1回目: タイムアウト外なので通知のみ
        app.on_key(mock_event)
        mock_exit.assert_not_called()
        assert app._exit_requested is False

        # 2回目（1秒以内）: 強制終了（rc=130）。terminate → exit の順で呼ばれる。
        app.on_key(mock_event)
        mock_exit.assert_called_once_with(return_code=130)
        mock_terminate.assert_called_once()
        assert app._exit_requested is True
        assert call_order == ["terminate", "exit"]


def test_safe_call_from_thread_short_circuits_when_exit_requested() -> None:
    """`_exit_requested=True`のとき`_safe_call_from_thread`は`call_from_thread`を呼ばない。

    閉じつつあるイベントループへ`call_from_thread`が詰まり、workerが
    `ThreadPoolExecutor.shutdown(wait=True)`から抜けられなくなる病理の退路として動く。
    """
    args = argparse.Namespace()
    args.targets = []
    args.verbose = False

    app = pyfltr.output.ui.UIApp(
        ["black"],
        args,
        pyfltr.command.ExecutionBaseContext(
            config=pyfltr.config.config.create_default_config(), all_files=[], cache_store=None, cache_run_id=None
        ),
    )
    app._exit_requested = True

    callback = unittest.mock.MagicMock()
    with unittest.mock.patch.object(app, "call_from_thread") as mock_call:
        app._safe_call_from_thread(callback, 1, key="value")
        mock_call.assert_not_called()
    callback.assert_not_called()

    # 逆に_exit_requested=Falseなら通常通りcall_from_thread経由で呼ばれる。
    app._exit_requested = False
    with unittest.mock.patch.object(app, "call_from_thread") as mock_call:
        app._safe_call_from_thread(callback, 1, key="value")
        mock_call.assert_called_once_with(callback, 1, key="value")


def test_ctrl_c_timeout() -> None:
    """Ctrl+Cのタイムアウト処理のテスト。"""
    args = argparse.Namespace()
    args.targets = []
    args.verbose = False

    app = pyfltr.output.ui.UIApp(
        ["black"],
        args,
        pyfltr.command.ExecutionBaseContext(
            config=pyfltr.config.config.create_default_config(), all_files=[], cache_store=None, cache_run_id=None
        ),
    )

    mock_event = unittest.mock.MagicMock()
    mock_event.key = "ctrl+c"

    with unittest.mock.patch.object(app, "exit") as mock_exit, unittest.mock.patch.object(app, "notify") as mock_notify:
        app.on_key(mock_event)
        mock_exit.assert_not_called()

        # 1秒以上待機（time.timeをモック）
        with unittest.mock.patch("pyfltr.output.ui.time.time") as mock_time:
            mock_time.return_value = app.last_ctrl_c_time + 2.0  # 2秒後

            app.on_key(mock_event)

            # exitが呼ばれず、通知が再表示されることを確認
            mock_exit.assert_not_called()
            assert mock_notify.call_count == 2


def test_interrupt_preserves_completed_results(monkeypatch) -> None:
    """BGスレッド経路で、途中で中断されても完了分・skipped分がresultsに残る。

    `execute_command`をモンキーパッチで差し替え、「1本目のlinterは成功、2本目の実行前に
    `_interrupted=True`にして`InterruptedExecution`を模す」形で協調停止を再現する。
    resultに完了分とskipped分が揃うこと、warningsに中断通知行が入ることを検証する。
    """
    pyfltr.warnings_.clear()
    args = argparse.Namespace()
    args.targets = []
    args.verbose = False
    args.keep_ui = False
    args.include_fix_stage = False

    config = pyfltr.config.config.create_default_config()
    # 2本のlinterを有効化、それ以外は無効化。
    for name in config.command_names:
        info = config.commands[name]
        if info.type == "linter" and name in ("pylint", "mypy"):
            config.values[name] = True
        else:
            config.values[name] = False
    config.values["jobs"] = 1

    base_ctx = pyfltr.command.ExecutionBaseContext(config=config, all_files=[], cache_store=None, cache_run_id=None)
    app = pyfltr.output.ui.UIApp(["pylint", "mypy"], args, base_ctx)

    call_order: list[str] = []

    def _fake_execute_command(command, *_a, **_kw):
        del _a, _kw
        call_order.append(command)
        if command == "pylint":
            # 1本目: 成功を返す。
            return pyfltr.command.CommandResult(
                command=command,
                command_type="linter",
                commandline=[],
                returncode=0,
                has_error=False,
                files=0,
                output="",
                elapsed=0.1,
            )
        # 2本目: 協調停止を発火させる。
        app._interrupted = True
        raise pyfltr.command.InterruptedExecution

    monkeypatch.setattr(pyfltr.command, "execute_command", _fake_execute_command)
    # call_from_threadはUI起動前に呼ばれても落ちないようno-op化。
    monkeypatch.setattr(app, "call_from_thread", lambda *a, **kw: None)

    app._run_in_background()

    # resultsに完了分（pylint succeeded）とskipped分（mypy）が揃っている。
    by_command = {r.command: r for r in app.results}
    assert by_command["pylint"].status == "succeeded"
    assert by_command["mypy"].status == "skipped"
    assert "Ctrl+C により中断しました" in by_command["mypy"].output

    # warningsに中断通知が1行入っている。
    warning_messages = [w["message"] for w in pyfltr.warnings_.collected_warnings()]
    assert any("Ctrl+C により中断しました" in m for m in warning_messages)
    assert any("mypy" in m for m in warning_messages)


def test_can_use_ui() -> None:
    """UIが使用可能かどうかの判定テスト。"""
    with (
        unittest.mock.patch("sys.stdin.isatty", return_value=True),
        unittest.mock.patch("sys.stdout.isatty", return_value=True),
    ):
        assert pyfltr.output.ui.can_use_ui() is True

    with (
        unittest.mock.patch("sys.stdin.isatty", return_value=False),
        unittest.mock.patch("sys.stdout.isatty", return_value=True),
    ):
        assert pyfltr.output.ui.can_use_ui() is False

    with (
        unittest.mock.patch("sys.stdin.isatty", return_value=True),
        unittest.mock.patch("sys.stdout.isatty", return_value=False),
    ):
        assert pyfltr.output.ui.can_use_ui() is False
