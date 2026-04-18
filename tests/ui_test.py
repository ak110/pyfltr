"""UI関連のテストコード。"""

# pylint: disable=protected-access

import argparse
import unittest.mock

import pyfltr.command
import pyfltr.config
import pyfltr.stage_runner
import pyfltr.ui
import pyfltr.warnings_


def test_ctrl_c_double_press_handling() -> None:
    """Ctrl+Cの2回押し処理のテスト。

    協調中断方式のため、2回目Ctrl+Cでは `exit` は呼ばず
    `_interrupted=True` と `terminate_active_processes` 呼び出しで停止する。
    """
    args = argparse.Namespace()
    args.targets = []
    args.verbose = False

    app = pyfltr.ui.UIApp(["black"], args, pyfltr.config.create_default_config(), [])

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
    """協調中断済みの状態でさらにCtrl+C×2を受けたら強制終了する。"""
    args = argparse.Namespace()
    args.targets = []
    args.verbose = False

    app = pyfltr.ui.UIApp(["black"], args, pyfltr.config.create_default_config(), [])
    # 協調中断済みの状態にする
    app._interrupted = True

    mock_event = unittest.mock.MagicMock()
    mock_event.key = "ctrl+c"

    with unittest.mock.patch.object(app, "exit") as mock_exit, unittest.mock.patch.object(app, "notify"):
        # 1回目: タイムアウト外なので通知のみ
        app.on_key(mock_event)
        mock_exit.assert_not_called()

        # 2回目（1秒以内）: 強制終了（rc=130）
        app.on_key(mock_event)
        mock_exit.assert_called_once_with(return_code=130)


def test_ctrl_c_timeout() -> None:
    """Ctrl+Cのタイムアウト処理のテスト。"""
    args = argparse.Namespace()
    args.targets = []
    args.verbose = False

    app = pyfltr.ui.UIApp(["black"], args, pyfltr.config.create_default_config(), [])

    mock_event = unittest.mock.MagicMock()
    mock_event.key = "ctrl+c"

    with unittest.mock.patch.object(app, "exit") as mock_exit, unittest.mock.patch.object(app, "notify") as mock_notify:
        app.on_key(mock_event)
        mock_exit.assert_not_called()

        # 1秒以上待機（time.timeをモック）
        with unittest.mock.patch("pyfltr.ui.time.time") as mock_time:
            mock_time.return_value = app.last_ctrl_c_time + 2.0  # 2秒後

            app.on_key(mock_event)

            # exitが呼ばれず、通知が再表示されることを確認
            mock_exit.assert_not_called()
            assert mock_notify.call_count == 2


def test_interrupt_preserves_completed_results(monkeypatch) -> None:
    """BG スレッド経路で、途中で中断されても完了分・skipped 分が results に残る。

    `execute_command` をモンキーパッチで差し替え、「1 本目の linter は成功、2 本目の実行前に
    `_interrupted=True` にして InterruptedExecution を模す」形で協調停止を再現する。
    result に完了分と skipped 分が揃うこと、warnings に中断通知行が入ることを検証する。
    """
    pyfltr.warnings_.clear()
    args = argparse.Namespace()
    args.targets = []
    args.verbose = False
    args.keep_ui = False
    args.include_fix_stage = False

    config = pyfltr.config.create_default_config()
    # 2本の linter を有効化、それ以外は無効化。
    for name in config.command_names:
        info = config.commands[name]
        if info.type == "linter" and name in ("pylint", "mypy"):
            config.values[name] = True
        else:
            config.values[name] = False
    config.values["jobs"] = 1

    app = pyfltr.ui.UIApp(["pylint", "mypy"], args, config, [])

    call_order: list[str] = []

    def _fake_execute_command(command, *_a, **_kw):
        del _a, _kw
        call_order.append(command)
        if command == "pylint":
            # 1 本目: 成功を返す。
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
        # 2 本目: 協調停止を発火させる。
        app._interrupted = True
        raise pyfltr.command.InterruptedExecution

    monkeypatch.setattr(pyfltr.command, "execute_command", _fake_execute_command)
    # call_from_thread は UI 起動前に呼ばれても落ちないよう no-op 化。
    monkeypatch.setattr(app, "call_from_thread", lambda *a, **kw: None)

    app._run_in_background()

    # results に完了分 (pylint succeeded) と skipped 分 (mypy) が揃っている。
    by_command = {r.command: r for r in app.results}
    assert by_command["pylint"].status == "succeeded"
    assert by_command["mypy"].status == "skipped"
    assert "Ctrl+C により中断しました" in by_command["mypy"].output

    # warnings に中断通知が 1 行入っている。
    warning_messages = [w["message"] for w in pyfltr.warnings_.collected_warnings()]
    assert any("Ctrl+C により中断しました" in m for m in warning_messages)
    assert any("mypy" in m for m in warning_messages)


def test_can_use_ui() -> None:
    """UIが使用可能かどうかの判定テスト。"""
    with (
        unittest.mock.patch("sys.stdin.isatty", return_value=True),
        unittest.mock.patch("sys.stdout.isatty", return_value=True),
    ):
        assert pyfltr.ui.can_use_ui() is True

    with (
        unittest.mock.patch("sys.stdin.isatty", return_value=False),
        unittest.mock.patch("sys.stdout.isatty", return_value=True),
    ):
        assert pyfltr.ui.can_use_ui() is False

    with (
        unittest.mock.patch("sys.stdin.isatty", return_value=True),
        unittest.mock.patch("sys.stdout.isatty", return_value=False),
    ):
        assert pyfltr.ui.can_use_ui() is False
