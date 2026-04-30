# pylint: disable=missing-module-docstring,missing-function-docstring  # テストはモジュール／関数docstringを省略する慣習

import concurrent.futures
import threading

import pyfltr.command.core_
import pyfltr.config.config
import pyfltr.state.stage_runner


def test_make_skipped_result_returns_command_result():
    """戻り値が CommandResult 型であること。"""
    config = pyfltr.config.config.create_default_config()
    result = pyfltr.state.stage_runner.make_skipped_result("mypy", config)
    assert isinstance(result, pyfltr.command.core_.CommandResult)


def test_make_skipped_result_status_is_skipped():
    """status が "skipped" であること（returncode=None が条件）。"""
    config = pyfltr.config.config.create_default_config()
    result = pyfltr.state.stage_runner.make_skipped_result("mypy", config)
    assert result.status == "skipped"
    assert result.returncode is None


def test_make_skipped_result_preserves_command_name():
    """command 名が保持されること。"""
    config = pyfltr.config.config.create_default_config()
    result = pyfltr.state.stage_runner.make_skipped_result("ruff-check", config)
    assert result.command == "ruff-check"


def test_make_skipped_result_has_no_error():
    """has_error=False であること（skipped は error 扱いしない）。"""
    config = pyfltr.config.config.create_default_config()
    result = pyfltr.state.stage_runner.make_skipped_result("mypy", config)
    assert result.has_error is False


def _make_pending_future() -> concurrent.futures.Future:
    """cancel 可能な pending 状態の Future を作る。"""
    # Future を直接インスタンス化し、set_result も cancel() も呼ばなければ pending 状態になる。
    f: concurrent.futures.Future = concurrent.futures.Future()
    return f


def _make_done_future() -> concurrent.futures.Future:
    """完了済みの Future を作る。"""
    f: concurrent.futures.Future = concurrent.futures.Future()
    f.set_result(None)
    return f


def test_cancel_pending_futures_cancels_pending_only():
    """done でない future だけが cancel() され、done な future は触らない。"""
    aborted: set[str] = set()

    done_future = _make_done_future()
    pending_future = _make_pending_future()

    future_to_command = {
        done_future: "done-cmd",
        pending_future: "pending-cmd",
    }

    pyfltr.state.stage_runner.cancel_pending_futures(future_to_command, aborted)

    # cancel が成功した future のコマンドだけ aborted に入る
    assert "done-cmd" not in aborted
    assert "pending-cmd" in aborted


def test_cancel_pending_futures_adds_cancelled_commands():
    """cancel() が成功した future のコマンド名が aborted_commands に追加される。"""
    aborted: set[str] = set()

    pending1 = _make_pending_future()
    pending2 = _make_pending_future()

    future_to_command = {
        pending1: "cmd-a",
        pending2: "cmd-b",
    }

    pyfltr.state.stage_runner.cancel_pending_futures(future_to_command, aborted)

    assert "cmd-a" in aborted
    assert "cmd-b" in aborted


def test_cancel_pending_futures_does_not_add_failed_cancel():
    """cancel() が False を返した（既に running/done）future は aborted_commands に入らない。"""
    aborted: set[str] = set()

    # running 状態（running=True で cancel 不可）を模倣するため、
    # ThreadPoolExecutor で実行中にする
    started = threading.Event()
    can_finish = threading.Event()

    def _blocking_task():
        started.set()
        can_finish.wait()

    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as executor:
        running_future = executor.submit(_blocking_task)
        started.wait()  # running 状態になるまで待つ

        future_to_command = {running_future: "running-cmd"}
        pyfltr.state.stage_runner.cancel_pending_futures(future_to_command, aborted)

        can_finish.set()

    # running future は cancel() が False を返すため aborted に入らない
    assert "running-cmd" not in aborted
