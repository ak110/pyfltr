import inspect
import time
import unittest.mock

import pytest

import pyfltr.command.core_
import pyfltr.command.process
import pyfltr.command.slow_tests
import pyfltr.command.stage_runner
import pyfltr.config.config
import pyfltr.diagnostics
import pyfltr.output.ui
import pyfltr.parsing.entry
import pyfltr.run_options
import pyfltr.warnings_


def test_ctrl_c_operation_sequence(monkeypatch: pytest.MonkeyPatch) -> None:
    args = pyfltr.run_options.RunOptions.from_values({})
    app = pyfltr.output.ui.UIApp(
        ["black"],
        args,
        pyfltr.command.core_.ExecutionBaseContext(
            config=pyfltr.config.config.create_default_config(), all_files=[], cache_store=None, cache_run_id=None
        ),
    )
    event = unittest.mock.Mock(key="ctrl+c")
    clock = unittest.mock.Mock(side_effect=[0.0, 2.0, 2.5, 2.6, 2.7])
    monkeypatch.setattr(pyfltr.output.ui, "time", unittest.mock.Mock(wraps=time, monotonic=clock))
    terminate = unittest.mock.Mock()
    monkeypatch.setattr(pyfltr.command.process, "terminate_active_processes", terminate)
    notify = unittest.mock.Mock()
    exit_app = unittest.mock.Mock()
    call = unittest.mock.Mock()
    monkeypatch.setattr(app, "notify", notify)
    monkeypatch.setattr(app, "exit", exit_app)
    monkeypatch.setattr(app, "call_from_thread", call)
    # pylint: disable=protected-access  # 公開イベントの前後で内部フェーズを観測する。
    legal = {("_Running", "_Stopping"), ("_Stopping", "_Exited")}
    for phase, stops in [("_Running", 0), ("_Running", 0), ("_Stopping", 1), ("_Stopping", 1), ("_Exited", 2)]:
        before = type(app._phase).__name__
        app.on_key(event)
        after = type(app._phase).__name__
        assert before == after or (before, after) in legal
        assert after == phase
        assert terminate.call_count == stops
        if after != "_Exited":
            exit_app.assert_not_called()
    exit_app.assert_called_once_with(return_code=130)
    assert app.interruption == "unconfirmed"
    assert notify.call_count == 4
    app._safe_call_from_thread(notify, "反映されない")
    call.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("action_quit", [False, True])
async def test_closing_finished_ui_does_not_interrupt_run(monkeypatch: pytest.MonkeyPatch, action_quit: bool) -> None:
    args = pyfltr.run_options.RunOptions.from_values({"keep_ui": True})
    app = pyfltr.output.ui.UIApp(
        [],
        args,
        pyfltr.command.core_.ExecutionBaseContext(
            config=pyfltr.config.config.create_default_config(), all_files=[], cache_store=None, cache_run_id=None
        ),
    )
    monkeypatch.setattr(pyfltr.command.stage_runner, "run_stages", unittest.mock.Mock())
    monkeypatch.setattr(pyfltr.output.ui, "time", unittest.mock.Mock(wraps=time, monotonic=lambda: 1.0))
    monkeypatch.setattr(app, "notify", unittest.mock.Mock())
    exit_app = unittest.mock.Mock()
    monkeypatch.setattr(app, "exit", exit_app)

    app._run_in_background()  # pylint: disable=protected-access  # 完了後にキー入力する順序を固定する。
    if action_quit:
        await app.action_quit()
    else:
        event = unittest.mock.Mock(key="ctrl+c")
        app.on_key(event)
        app.on_key(event)
    assert app.interruption is None
    exit_app.assert_called_once_with()


@pytest.mark.asyncio
async def test_quit_requests_stop_without_claiming_worker_finished(monkeypatch: pytest.MonkeyPatch) -> None:
    args = pyfltr.run_options.RunOptions.from_values({})
    app = pyfltr.output.ui.UIApp(
        ["black"],
        args,
        pyfltr.command.core_.ExecutionBaseContext(
            config=pyfltr.config.config.create_default_config(), all_files=[], cache_store=None, cache_run_id=None
        ),
    )
    terminate = unittest.mock.Mock()
    monkeypatch.setattr(pyfltr.command.process, "terminate_active_processes", terminate)
    exit_app = unittest.mock.Mock()
    monkeypatch.setattr(app, "exit", exit_app)

    await app.action_quit()

    assert app.interruption == "unconfirmed"
    terminate.assert_called_once_with()
    exit_app.assert_called_once_with(return_code=130)


@pytest.mark.parametrize("interrupt_after_end", [False, True])
def test_interrupt_preserves_completed_results(monkeypatch: pytest.MonkeyPatch, interrupt_after_end: bool) -> None:
    pyfltr.warnings_.clear()
    args = pyfltr.run_options.RunOptions.from_values({})
    args.targets = []
    args.verbose = False
    args.keep_ui = False
    args.include_fix_stage = False

    config = pyfltr.config.config.create_default_config()
    for name in config.command_names:
        info = config.commands[name]
        if info.type == "linter" and name in ("pylint", "mypy", "ruff-check"):
            config.values[name] = True
        else:
            config.values[name] = False
    config.values["jobs"] = 1

    base_ctx = pyfltr.command.core_.ExecutionBaseContext(config=config, all_files=[], cache_store=None, cache_run_id=None)
    app = pyfltr.output.ui.UIApp(["pylint", "mypy", "ruff-check"], args, base_ctx)

    call_order: list[str] = []

    def _fake_execute_command(command, _args, ctx):
        assert ctx.on_subprocess_start is not None
        assert ctx.on_subprocess_end is not None
        ctx.on_subprocess_start()
        call_order.append(command)
        if command == "pylint":
            ctx.on_subprocess_end()
            return pyfltr.command.core_.CommandResult(
                command=command,
                command_type="linter",
                commandline=[],
                returncode=0,
                formatter_failed=False,
                files=0,
                output="",
                elapsed=0.1,
            )
        event = unittest.mock.Mock(key="ctrl+c")
        if interrupt_after_end:
            ctx.on_subprocess_end()
        app.on_key(event)
        app.on_key(event)
        if not interrupt_after_end:
            ctx.on_subprocess_end()
        return pyfltr.command.core_.CommandResult(
            command=command,
            command_type="linter",
            commandline=[],
            returncode=0,
            formatter_failed=False,
            files=0,
            output="",
            elapsed=0.1,
        )

    monkeypatch.setattr(pyfltr.output.ui, "time", unittest.mock.Mock(wraps=time, monotonic=lambda: 1.0))
    monkeypatch.setattr(pyfltr.command.process, "terminate_active_processes", lambda: None)
    monkeypatch.setattr(app, "notify", unittest.mock.Mock())
    monkeypatch.setattr("pyfltr.command.dispatcher.execute_command", _fake_execute_command)
    call = unittest.mock.Mock()
    monkeypatch.setattr(app, "call_from_thread", call)

    app._run_in_background()  # pylint: disable=protected-access  # タイミング制御困難

    by_command = {r.command: r for r in app.results}
    assert by_command["pylint"].status == "succeeded"
    assert by_command["mypy"].status == ("succeeded" if interrupt_after_end else "skipped")
    assert by_command["ruff-check"].status == "skipped"
    assert call_order == ["pylint", "mypy"]
    call.assert_called_with(app.exit, return_code=130)
    assert app.interruption == "confirmed"
    assert "Ctrl+C により中断しました" in by_command["ruff-check"].output

    warning_messages = [w["message"] for w in pyfltr.warnings_.collected_warnings()]
    assert any("Ctrl+C により中断しました" in m for m in warning_messages)
    assert any("ruff-check" in m for m in warning_messages)
    assert any("mypy" in m for m in warning_messages) is not interrupt_after_end


def test_tester_failure_writes_raw_output_in_addition_to_diagnostics(monkeypatch) -> None:
    args = pyfltr.run_options.RunOptions.from_values({})
    args.targets = []
    args.verbose = False
    args.keep_ui = False
    args.include_fix_stage = False

    config = pyfltr.config.config.create_default_config()
    for name in config.command_names:
        config.values[name] = name == "pytest"
    config.values["jobs"] = 1

    base_ctx = pyfltr.command.core_.ExecutionBaseContext(config=config, all_files=[], cache_store=None, cache_run_id=None)
    app = pyfltr.output.ui.UIApp(["pytest"], args, base_ctx)

    written: list[tuple[str, str]] = []

    def _call_from_thread(fn, *a, **kw):
        result = fn(*a, **kw)
        if inspect.iscoroutine(result):
            result.close()
            return None
        return result

    monkeypatch.setattr(app, "call_from_thread", _call_from_thread)
    monkeypatch.setattr(app, "_write_log", lambda log_id, text: written.append((log_id, text)))

    error = pyfltr.diagnostics.ErrorLocation(file="tests/a.py", line=0, col=None, command="pytest", message="test_a: crashed")

    def _fake_execute_command(command, *_a, **_kw):
        del _a, _kw
        return pyfltr.command.core_.CommandResult(
            command=command,
            command_type="tester",
            commandline=[],
            returncode=1,
            files=1,
            output="RAW CRASH OUTPUT",
            elapsed=0.1,
            errors=[error],
            slow_tests=[pyfltr.command.slow_tests.SlowTest("tests/a.py::test_a", "call", 1.5)],
        )

    monkeypatch.setattr("pyfltr.command.dispatcher.execute_command", _fake_execute_command)
    app._run_in_background()  # pylint: disable=protected-access  # タイミング制御困難

    log_texts = [text for log_id, text in written if log_id == "#output-pytest"]
    assert any("RAW CRASH OUTPUT" in t for t in log_texts)
    assert any("test_a: crashed" in t for t in log_texts)
    assert any("1.50s call tests/a.py::test_a" in t for t in log_texts)


def test_tester_success_writes_slow_tests(monkeypatch) -> None:
    args = pyfltr.run_options.RunOptions.from_values(
        {"targets": [], "verbose": False, "keep_ui": False, "include_fix_stage": False}
    )
    config = pyfltr.config.config.create_default_config()
    for name in config.command_names:
        config.values[name] = name == "pytest"
    config.values["jobs"] = 1
    base_ctx = pyfltr.command.core_.ExecutionBaseContext(config=config, all_files=[], cache_store=None, cache_run_id=None)
    app = pyfltr.output.ui.UIApp(["pytest"], args, base_ctx)
    written: list[tuple[str, str]] = []

    monkeypatch.setattr(app, "call_from_thread", lambda fn, *args, **kwargs: fn(*args, **kwargs))
    monkeypatch.setattr(app, "_write_log", lambda log_id, text: written.append((log_id, text)))
    result = pyfltr.command.core_.CommandResult(
        command="pytest",
        command_type="tester",
        commandline=[],
        returncode=0,
        formatter_failed=False,
        files=1,
        output="1 passed in 1.50s",
        elapsed=1.5,
        slow_tests=[pyfltr.command.slow_tests.SlowTest("tests/a.py::test_a", "call", 1.5)],
    )
    monkeypatch.setattr("pyfltr.command.dispatcher.execute_command", lambda *_args, **_kwargs: result)

    app._run_in_background()  # pylint: disable=protected-access  # Textual起動無しで完了表示を検証する

    log_texts = [text for log_id, text in written if log_id == "#output-pytest"]
    assert any("1.50s call tests/a.py::test_a" in text for text in log_texts)


def test_run_commands_with_ui_failure_guides_no_ui(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:

    def _raise(_self: object, *_args: object, **_kwargs: object) -> None:
        raise RuntimeError("terminal unsupported")

    monkeypatch.setattr(pyfltr.output.ui.UIApp, "run", _raise)
    args = pyfltr.run_options.RunOptions.from_values({"targets": [], "verbose": False})
    base_ctx = pyfltr.command.core_.ExecutionBaseContext(
        config=pyfltr.config.config.create_default_config(), all_files=[], cache_store=None, cache_run_id=None
    )
    with pytest.raises(SystemExit) as exc_info:
        pyfltr.output.ui.run_commands_with_ui(["mypy"], args, base_ctx)
    assert exc_info.value.code == 1
    err = capsys.readouterr().err
    assert "terminal unsupported" in err
    assert "`--no-ui`" in err


def test_run_commands_with_ui_uses_textual_exit_code(monkeypatch: pytest.MonkeyPatch) -> None:
    def exit_app(app: pyfltr.output.ui.UIApp) -> None:
        app.exit(return_code=130)

    monkeypatch.setattr(pyfltr.output.ui.UIApp, "run", exit_app)
    args = pyfltr.run_options.RunOptions.from_values({})
    base_ctx = pyfltr.command.core_.ExecutionBaseContext(
        config=pyfltr.config.config.create_default_config(), all_files=[], cache_store=None, cache_run_id=None
    )
    outcome = pyfltr.output.ui.run_commands_with_ui([], args, base_ctx)
    assert outcome.returncode == 130
