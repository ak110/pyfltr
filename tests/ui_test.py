"""UI関連のテストコード。"""

import argparse
import unittest.mock

import pyfltr.ui


def test_ctrl_c_double_press_handling() -> None:
    """Ctrl+Cの2回押し処理のテスト。"""
    # モックしたArgsを作成
    args = argparse.Namespace()
    args.targets = []
    args.verbose = False

    # PyfltrAppインスタンスを作成
    app = pyfltr.ui.UIApp(["black"], args)

    # 初期状態のテスト
    assert app.last_ctrl_c_time == 0.0
    assert app.ctrl_c_timeout == 1.0

    # モックイベント作成
    mock_event = unittest.mock.MagicMock()
    mock_event.key = "ctrl+c"

    # exitメソッドをモック
    with unittest.mock.patch.object(app, "exit") as mock_exit, unittest.mock.patch.object(app, "_write_log") as mock_update:
        # 1回目のCtrl+C
        app.on_key(mock_event)

        # exitが呼ばれていないことを確認
        mock_exit.assert_not_called()

        # メッセージが表示されることを確認
        mock_update.assert_called_once_with("#summary-content", "Press Ctrl+C again within 1 second to exit...\n")

        # 1秒以内の2回目のCtrl+C
        app.on_key(mock_event)

        # exitが呼ばれることを確認
        mock_exit.assert_called_once()


def test_ctrl_c_timeout() -> None:
    """Ctrl+Cのタイムアウト処理のテスト。"""
    args = argparse.Namespace()
    args.targets = []
    args.verbose = False

    app = pyfltr.ui.UIApp(["black"], args)

    mock_event = unittest.mock.MagicMock()
    mock_event.key = "ctrl+c"

    with unittest.mock.patch.object(app, "exit") as mock_exit, unittest.mock.patch.object(app, "_write_log") as mock_update:
        # 1回目のCtrl+C
        app.on_key(mock_event)
        mock_exit.assert_not_called()

        # 1秒以上待機（time.timeをモック）
        with unittest.mock.patch("pyfltr.ui.time.time") as mock_time:
            mock_time.return_value = app.last_ctrl_c_time + 2.0  # 2秒後

            # 2回目のCtrl+C（タイムアウト後）
            app.on_key(mock_event)

            # exitが呼ばれず、メッセージが再表示されることを確認
            mock_exit.assert_not_called()
            assert mock_update.call_count == 2


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
