import collections.abc
import concurrent.futures
import faulthandler
import pathlib
import time
import typing

import pytest

import pyfltr.cli.output_format
import pyfltr.command.core_
import pyfltr.command.execution_slot
import pyfltr.command.mise
import pyfltr.command.only_failed
import pyfltr.config.config
import pyfltr.config.model
import pyfltr.diagnostics
import pyfltr.parsing.entry
import pyfltr.run_options
import pyfltr.state.archive
import pyfltr.state.cache
import pyfltr.state.only_failed
import pyfltr.warnings_


@pytest.fixture(autouse=True)
def _clear_warnings_between_tests() -> None:
    pyfltr.warnings_.clear()


@pytest.fixture
def _disable_faulthandler_timeout() -> None:
    faulthandler.cancel_dump_traceback_later()


@pytest.fixture(autouse=True)
def _isolate_output_format_envs(monkeypatch: pytest.MonkeyPatch) -> None:
    for env_name in pyfltr.cli.output_format.AGENT_INDICATOR_ENVS:
        monkeypatch.delenv(env_name, raising=False)
    monkeypatch.delenv(pyfltr.cli.output_format.OUTPUT_FORMAT_ENV, raising=False)


@pytest.fixture(autouse=True)
def _isolate_execution_slots_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(pyfltr.command.execution_slot.SLOTS_ENV, raising=False)


real_get_mise_active_tools = pyfltr.command.mise.get_mise_active_tools


@pytest.fixture(autouse=True)
def _default_mise_active_tools_empty(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("pyfltr.command.mise._MISE_ACTIVE_TOOLS_CACHE", {}, raising=True)
    monkeypatch.setattr(
        "pyfltr.command.mise.get_mise_active_tools",
        lambda config, *, allow_side_effects=False, cwd=None: pyfltr.command.mise.MiseActiveToolsResult(status="ok"),
    )


@pytest.fixture(autouse=True)
def _default_mise_exec_check_success(request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch) -> None:
    if "real_mise_subprocess" in request.keywords:
        return
    monkeypatch.setattr(
        "pyfltr.command.mise.run_mise_with_trust",
        lambda args, mise_env, config, *, allow_side_effects, cwd=None: (0, "", "", False),
    )


@pytest.fixture(autouse=True)
def _isolate_global_config(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path_factory: pytest.TempPathFactory,
) -> pathlib.Path:
    isolation_dir = tmp_path_factory.mktemp("global_config_isolation")
    target = isolation_dir / "config.toml"
    monkeypatch.setenv("PYFLTR_GLOBAL_CONFIG", str(target))
    return target


@pytest.fixture(autouse=True)
def _isolate_cache_root(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path_factory: pytest.TempPathFactory,
) -> pathlib.Path:
    cache_root = tmp_path_factory.mktemp("cache_root_isolation")
    monkeypatch.setenv("PYFLTR_CACHE_DIR", str(cache_root))
    return cache_root


CARGO_CLIPPY_ARGS: list[str] = ["clippy", "--all-targets"]
CARGO_CLIPPY_LINT_ARGS: list[str] = ["--", "-D", "warnings"]
CARGO_CLIPPY_FIX_ARGS: list[str] = [
    "--fix",
    "--allow-staged",
    "--allow-dirty",
    "--",
    "-D",
    "warnings",
]
CARGO_CLIPPY_OUTPUT_ARGS: list[str] = ["--message-format=short", "--color=never"]
CARGO_CLIPPY_LINT_CMDLINE: list[str] = ["cargo", *CARGO_CLIPPY_ARGS, *CARGO_CLIPPY_OUTPUT_ARGS, *CARGO_CLIPPY_LINT_ARGS]
CARGO_CLIPPY_FIX_CMDLINE: list[str] = [
    "cargo",
    *CARGO_CLIPPY_ARGS,
    *CARGO_CLIPPY_FIX_ARGS[:3],
    *CARGO_CLIPPY_OUTPUT_ARGS,
    *CARGO_CLIPPY_FIX_ARGS[3:],
]


def make_execution_context(
    config: pyfltr.config.model.Config,
    all_files: list[pathlib.Path],
    *,
    cache_store: pyfltr.state.cache.CacheStore | None = None,
    cache_run_id: str | None = None,
    fix_stage: bool = False,
    only_failed_targets: pyfltr.command.only_failed.ToolTargets | None = None,
    start_cwd: pathlib.Path | None = None,
    fail_fast: bool = False,
) -> pyfltr.command.core_.ExecutionContext:
    base_kwargs: dict[str, typing.Any] = {
        "config": config,
        "all_files": all_files,
        "cache_store": cache_store,
        "cache_run_id": cache_run_id,
        "fail_fast": fail_fast,
    }
    if start_cwd is not None:
        base_kwargs["start_cwd"] = start_cwd
    base = pyfltr.command.core_.ExecutionBaseContext(**base_kwargs)
    return pyfltr.command.core_.ExecutionContext(
        base=base,
        fix_stage=fix_stage,
        only_failed_targets=only_failed_targets,
    )


def make_command_result(
    command: str,
    *,
    returncode: int | None,
    command_type: str = "linter",
    output: str = "",
    files: int = 1,
    elapsed: float = 0.1,
    errors: list[pyfltr.diagnostics.ErrorLocation] | None = None,
    formatter_failed: bool = False,
    archived: bool = True,
    retry_command: str | None = None,
    cached: bool = False,
    cached_from: str | None = None,
    target_files: list[pathlib.Path] | None = None,
    resolution_failed: bool = False,
    retry_count: int = 0,
) -> pyfltr.command.core_.CommandResult:
    return pyfltr.command.core_.CommandResult(
        command=command,
        command_type=command_type,
        commandline=[command],
        returncode=returncode,
        formatter_failed=formatter_failed,
        files=files,
        output=output,
        elapsed=elapsed,
        errors=list(errors) if errors else [],
        target_files=list(target_files) if target_files else [],
        archived=archived,
        retry_command=retry_command,
        cached=cached,
        cached_from=cached_from,
        resolution_failed=resolution_failed,
        retry_count=retry_count,
    )


def make_error_location(
    command: str,
    file: str,
    line: int,
    message: str,
    col: int | None = None,
) -> pyfltr.diagnostics.ErrorLocation:
    return pyfltr.diagnostics.ErrorLocation(
        file=file,
        line=line,
        col=col,
        command=command,
        message=message,
    )


def make_formatted_result(command: str = "ruff-format") -> pyfltr.command.core_.CommandResult:
    return pyfltr.command.core_.CommandResult(
        command, "formatter", [command], returncode=1, formatter_failed=False, files=1, output="", elapsed=0.01
    )


def make_succeeded_result(command: str = "ruff-check") -> pyfltr.command.core_.CommandResult:
    return pyfltr.command.core_.CommandResult(
        command, "linter", [command], returncode=0, formatter_failed=False, files=1, output="", elapsed=0.01
    )


def make_archive_store(tmp_path: pathlib.Path) -> pyfltr.state.archive.ArchiveStore:
    return pyfltr.state.archive.ArchiveStore(cache_root=tmp_path)


def make_args(*, no_exclude: bool = False, allow_external_paths: bool = False) -> pyfltr.run_options.RunOptions:
    return pyfltr.run_options.RunOptions(
        shuffle=False,
        verbose=False,
        no_exclude=no_exclude,
        allow_external_paths=allow_external_paths,
    )


@pytest.fixture
def _isolated_cache(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: pathlib.Path,
) -> pathlib.Path:
    monkeypatch.setenv("PYFLTR_CACHE_DIR", str(tmp_path))
    return tmp_path


def count_config_warnings(needle: str) -> int:
    return sum(1 for w in pyfltr.warnings_.collected_warnings() if w["source"] == "config" and needle in w["message"])


def shared_prefix_length(a: str, b: str) -> int:
    shared = 0
    for ca, cb in zip(a, b, strict=False):
        if ca != cb:
            break
        shared += 1
    return shared


_SLOT_WAIT_WORKER = """import pathlib
import sys

import pyfltr.cli.mcp_worker
import pyfltr.command.stage_runner

_notify = pyfltr.command.stage_runner._notify_slot_wait


def _notify_and_mark(owner):
    _notify(owner)
    pathlib.Path({marker!r}).touch()


pyfltr.command.stage_runner._notify_slot_wait = _notify_and_mark
sys.exit(pyfltr.cli.mcp_worker.main())
"""


def write_slot_wait_worker(script: pathlib.Path, marker: pathlib.Path) -> pathlib.Path:
    script.write_text(_SLOT_WAIT_WORKER.format(marker=str(marker)), encoding="utf-8")
    return script


def seed_archive_run(
    cache_root: pathlib.Path,
    *,
    commands: list[str] | None = None,
    files: int = 3,
    exit_code: int = 0,
    tool_results: list[tuple[str, int, str, list]] | None = None,
    resolution_failed_tools: set[str] | None = None,
    cwd: pathlib.Path | None = None,
) -> str:
    time.sleep(0.001)
    store = pyfltr.state.archive.ArchiveStore(cache_root=cache_root)
    run_id = store.start_run(commands=commands or ["ruff-check"], files=files, cwd=str(cwd) if cwd is not None else None)
    for tool, returncode, output, errors in tool_results or []:
        result = make_command_result(
            tool,
            returncode=returncode,
            output=output,
            errors=errors,
            resolution_failed=tool in (resolution_failed_tools or set()),
        )
        store.write_tool_result(run_id, result)
    store.finalize_run(run_id, exit_code=exit_code, commands=commands, files=files)
    return run_id


class StageExecutorControl:
    def __init__(self, run_on_submit: set[str]) -> None:
        self.run_on_submit = run_on_submit
        self.executed: list[str] = []
        self._pending: dict[concurrent.futures.Future, tuple[collections.abc.Callable, tuple]] = {}

    def install(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(concurrent.futures, "ThreadPoolExecutor", self._make_executor)
        monkeypatch.setattr(concurrent.futures, "as_completed", self._as_completed)

    def _make_executor(self, *args: typing.Any, **kwargs: typing.Any) -> "_ControlledExecutor":
        del args, kwargs
        return _ControlledExecutor(self)

    def submit(self, fn: collections.abc.Callable, *args: typing.Any) -> concurrent.futures.Future:
        future: concurrent.futures.Future = concurrent.futures.Future()
        if args[0] in self.run_on_submit:
            future.set_running_or_notify_cancel()
            self._run(future, fn, args)
        else:
            self._pending[future] = (fn, args)
        return future

    def _run(self, future: concurrent.futures.Future, fn: collections.abc.Callable, args: tuple) -> None:
        self.executed.append(args[0])
        try:
            future.set_result(fn(*args))
        except Exception as e:  # pylint: disable=broad-exception-caught  # 実行器と同じく例外をFutureへ渡す
            future.set_exception(e)

    def _as_completed(
        self, fs: collections.abc.Iterable[concurrent.futures.Future], timeout: float | None = None
    ) -> collections.abc.Iterator[concurrent.futures.Future]:
        del timeout
        futures = list(fs)
        yield from [future for future in futures if future not in self._pending]
        for future in futures:
            if future not in self._pending:
                continue
            fn, args = self._pending.pop(future)
            if future.set_running_or_notify_cancel():
                self._run(future, fn, args)
            yield future


class _ControlledExecutor:
    def __init__(self, control: StageExecutorControl) -> None:
        self._control = control

    def __enter__(self) -> "_ControlledExecutor":
        return self

    def __exit__(self, *exc_info: object) -> None:
        del exc_info

    def submit(self, fn: collections.abc.Callable, *args: typing.Any) -> concurrent.futures.Future:
        return self._control.submit(fn, *args)


@pytest.fixture
def _isolated_target(monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path) -> pathlib.Path:
    monkeypatch.setattr("pyfltr.command.runner.cwd_has_uv_lock", lambda *_args: True)
    (tmp_path / "pyproject.toml").write_text('[tool.pyfltr]\npreset = "latest"\npython = true\nrespect-gitignore = false\n')
    (tmp_path / "sample.py").write_text("x = 1\n")
    return tmp_path


@pytest.fixture(name="_only_failed_cache")
def _only_failed_cache_fixture(monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path) -> pathlib.Path:
    monkeypatch.setenv("PYFLTR_CACHE_DIR", str(tmp_path))
    return tmp_path
