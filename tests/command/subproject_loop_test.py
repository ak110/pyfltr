import pathlib
import threading
import typing

import pytest

import pyfltr.command.core_
import pyfltr.command.execution_slot
import pyfltr.command.subproject_loop
import pyfltr.command.subprojects
import pyfltr.config.config
import pyfltr.config.model
import pyfltr.run_options
import pyfltr.warnings_
import tests.conftest as _testconf


def _make_context(
    tmp_path: pathlib.Path, names: list[str], *, values: dict[str, typing.Any] | None = None
) -> pyfltr.command.core_.ExecutionContext:
    config = pyfltr.config.config.create_default_config()
    config.values["ruff-check"] = True
    for key, value in (values or {}).items():
        config.values[key] = value
    subs = []
    subproject_files: dict[pathlib.Path, list[pathlib.Path]] = {}
    all_files: list[pathlib.Path] = []
    for name in names:
        cwd = tmp_path / name
        cwd.mkdir(parents=True, exist_ok=True)
        (cwd / "pyproject.toml").write_text('[project]\nname = "x"\nversion = "0"\n', encoding="utf-8")
        subs.append(pyfltr.command.subprojects.Subproject(cwd=cwd, relative=name))
        target = pathlib.Path(name) / "mod.py"
        subproject_files[cwd] = [target]
        all_files.append(target)
    base = pyfltr.command.core_.ExecutionBaseContext(
        config=config,
        all_files=all_files,
        cache_store=None,
        cache_run_id=None,
        start_cwd=tmp_path,
        subprojects=subs,
        subproject_files=subproject_files,
        external_files=[],
        subproject_configs={s.cwd: config for s in subs},
    )
    return pyfltr.command.core_.ExecutionContext(base=base)


def _disabled_skip(command: str, ctx: pyfltr.command.core_.ExecutionContext) -> pyfltr.command.core_.CommandResult:
    del ctx
    return _testconf.make_succeeded_result(command=command)


class TestResolveSubprojectWorkers:
    def test_default_jobs_is_eight(self) -> None:
        assert pyfltr.config.model.DEFAULT_CONFIG["jobs"] == 8

    def test_uses_whole_budget_for_single_worker_tool(self, tmp_path: pathlib.Path) -> None:
        ctx = _make_context(tmp_path, ["a", "b"], values={"jobs": 6})
        cwds = [s.cwd for s in ctx.base.subprojects]
        assert pyfltr.command.subproject_loop.resolve_subproject_workers("ruff-check", ctx, cwds) == 6

    def test_divides_budget_by_tool_workers(self, tmp_path: pathlib.Path) -> None:
        ctx = _make_context(tmp_path, ["a", "b"], values={"jobs": 8, "pytest-args": ["-n", "2"]})
        cwds = [s.cwd for s in ctx.base.subprojects]
        assert pyfltr.command.subproject_loop.resolve_subproject_workers("pytest", ctx, cwds) == 4

    def test_never_returns_zero(self, tmp_path: pathlib.Path) -> None:
        ctx = _make_context(tmp_path, ["a", "b"], values={"jobs": 4, "pytest-args": ["-n", "8"]})
        cwds = [s.cwd for s in ctx.base.subprojects]
        assert pyfltr.command.subproject_loop.resolve_subproject_workers("pytest", ctx, cwds) == 1

    def test_uses_max_estimate_across_subprojects(self, tmp_path: pathlib.Path) -> None:
        ctx = _make_context(tmp_path, ["a", "b"], values={"jobs": 8})
        (tmp_path / "b" / "pyproject.toml").write_text(
            '[project]\nname = "x"\nversion = "0"\n\n[tool.pytest.ini_options]\naddopts = "-n 8"\n',
            encoding="utf-8",
        )
        cwds = [s.cwd for s in ctx.base.subprojects]
        assert pyfltr.command.subproject_loop.resolve_subproject_workers("pytest", ctx, cwds) == 1


class TestRunSubprojectLoop:
    def test_sequential_when_workers_is_one(self, tmp_path: pathlib.Path) -> None:
        ctx = _make_context(tmp_path, ["a", "b", "c"], values={"jobs": 1})
        active = 0
        max_active = 0
        lock = threading.Lock()

        def _dispatch(
            command: str, args: pyfltr.run_options.RunOptions, c: pyfltr.command.core_.ExecutionContext
        ) -> pyfltr.command.core_.CommandResult:
            del args, c
            nonlocal active, max_active
            with lock:
                active += 1
                max_active = max(max_active, active)
            with lock:
                active -= 1
            return _testconf.make_succeeded_result(command=command)

        pyfltr.command.subproject_loop.run_subproject_loop(
            "ruff-check",
            _testconf.make_args(),
            ctx,
            dispatch_fn=_dispatch,
            disabled_skip_fn=_disabled_skip,
        )
        assert max_active == 1

    def test_parallel_when_workers_is_greater_than_one(self, tmp_path: pathlib.Path) -> None:
        ctx = _make_context(tmp_path, ["a", "b"], values={"jobs": 2})
        barrier = threading.Barrier(2, timeout=30)

        def _dispatch(
            command: str, args: pyfltr.run_options.RunOptions, c: pyfltr.command.core_.ExecutionContext
        ) -> pyfltr.command.core_.CommandResult:
            del args, c
            barrier.wait()
            return _testconf.make_succeeded_result(command=command)

        pyfltr.command.subproject_loop.run_subproject_loop(
            "ruff-check",
            _testconf.make_args(),
            ctx,
            dispatch_fn=_dispatch,
            disabled_skip_fn=_disabled_skip,
        )
        assert barrier.n_waiting == 0

    def test_concurrency_is_capped_by_target_count(self, tmp_path: pathlib.Path) -> None:
        ctx = _make_context(tmp_path, ["a", "b"], values={"jobs": 6})
        active = 0
        max_active = 0
        lock = threading.Lock()
        started = threading.Barrier(2, timeout=30)

        def _dispatch(
            command: str, args: pyfltr.run_options.RunOptions, c: pyfltr.command.core_.ExecutionContext
        ) -> pyfltr.command.core_.CommandResult:
            del args, c
            nonlocal active, max_active
            with lock:
                active += 1
                max_active = max(max_active, active)
            started.wait()
            with lock:
                active -= 1
            return _testconf.make_succeeded_result(command=command)

        pyfltr.command.subproject_loop.run_subproject_loop(
            "ruff-check",
            _testconf.make_args(),
            ctx,
            dispatch_fn=_dispatch,
            disabled_skip_fn=_disabled_skip,
        )
        assert max_active == 2

    def test_output_order_follows_relative_path(self, tmp_path: pathlib.Path) -> None:
        ctx = _make_context(tmp_path, ["b", "a"], values={"jobs": 2})
        started = threading.Event()

        def _dispatch(
            command: str, args: pyfltr.run_options.RunOptions, c: pyfltr.command.core_.ExecutionContext
        ) -> pyfltr.command.core_.CommandResult:
            del args
            result = _testconf.make_succeeded_result(command=command)
            result.output = f"out for {c.subproject_cwd.name if c.subproject_cwd else 'root'}"
            if c.subproject_cwd is not None and c.subproject_cwd.name == "a":
                started.set()
            else:
                started.wait(timeout=30)
            return result

        merged = pyfltr.command.subproject_loop.run_subproject_loop(
            "ruff-check",
            _testconf.make_args(),
            ctx,
            dispatch_fn=_dispatch,
            disabled_skip_fn=_disabled_skip,
        )
        assert merged.output.index("# subproject: a") < merged.output.index("# subproject: b")

    def test_duplicate_warnings_are_suppressed_across_workers(self, tmp_path: pathlib.Path) -> None:
        pyfltr.warnings_.clear()
        ctx = _make_context(tmp_path, ["a", "b", "c"], values={"jobs": 3})

        def _dispatch(
            command: str, args: pyfltr.run_options.RunOptions, c: pyfltr.command.core_.ExecutionContext
        ) -> pyfltr.command.core_.CommandResult:
            del args, c
            pyfltr.warnings_.emit_warning(source="dummy", message="同一の警告", hint="対応不要です。")
            return _testconf.make_succeeded_result(command=command)

        pyfltr.command.subproject_loop.run_subproject_loop(
            "ruff-check",
            _testconf.make_args(),
            ctx,
            dispatch_fn=_dispatch,
            disabled_skip_fn=_disabled_skip,
        )
        collected = [w for w in pyfltr.warnings_.collected_warnings() if w["source"] == "dummy"]
        pyfltr.warnings_.clear()
        assert len(collected) == 1


def test_mypy_runs_subprojects_one_at_a_time_while_others_run_concurrently(tmp_path: pathlib.Path) -> None:
    ctx = _make_context(tmp_path, ["a", "b"], values={"mypy": True})
    assert ctx.config.values["jobs"] == 8

    def _max_concurrency(command: str) -> int:
        active = 0
        max_active = 0
        cond = threading.Condition()

        def _dispatch(
            command: str, args: pyfltr.run_options.RunOptions, c: pyfltr.command.core_.ExecutionContext
        ) -> pyfltr.command.core_.CommandResult:
            del args, c
            nonlocal active, max_active
            with cond:
                active += 1
                max_active = max(max_active, active)
                cond.notify_all()
                cond.wait_for(lambda: max_active >= 2, timeout=1.0)
                active -= 1
            return _testconf.make_succeeded_result(command=command)

        pyfltr.command.subproject_loop.run_subproject_loop(
            command,
            _testconf.make_args(),
            ctx,
            dispatch_fn=_dispatch,
            disabled_skip_fn=_disabled_skip,
        )
        return max_active

    assert _max_concurrency("mypy") == 1
    assert _max_concurrency("ruff-check") == 2


@pytest.mark.parametrize("jobs", [1, 2])
def test_all_subprojects_are_dispatched(tmp_path: pathlib.Path, jobs: int) -> None:
    ctx = _make_context(tmp_path, ["a", "b", "c"], values={"jobs": jobs})
    seen: list[str] = []
    lock = threading.Lock()

    def _dispatch(
        command: str, args: pyfltr.run_options.RunOptions, c: pyfltr.command.core_.ExecutionContext
    ) -> pyfltr.command.core_.CommandResult:
        del args
        with lock:
            seen.append(c.subproject_cwd.name if c.subproject_cwd else "root")
        return _testconf.make_succeeded_result(command=command)

    pyfltr.command.subproject_loop.run_subproject_loop(
        "ruff-check",
        _testconf.make_args(),
        ctx,
        dispatch_fn=_dispatch,
        disabled_skip_fn=_disabled_skip,
    )
    assert sorted(seen) == ["a", "b", "c"]


def test_merge_prefers_executed_subproject_result(tmp_path: pathlib.Path) -> None:
    ctx = _make_context(tmp_path, ["a", "b"], values={"jobs": 1})

    def _dispatch(
        command: str, args: pyfltr.run_options.RunOptions, c: pyfltr.command.core_.ExecutionContext
    ) -> pyfltr.command.core_.CommandResult:
        del args
        assert c.subproject_cwd is not None
        if c.subproject_cwd.name == "a":
            return pyfltr.command.core_.CommandResult(
                command, "linter", [], returncode=None, formatter_failed=False, files=0, output="", elapsed=0.0
            )
        return _testconf.make_succeeded_result(command=command)

    merged = pyfltr.command.subproject_loop.run_subproject_loop(
        "ruff-check",
        _testconf.make_args(),
        ctx,
        dispatch_fn=_dispatch,
        disabled_skip_fn=_disabled_skip,
    )
    assert merged.status == "succeeded"
    assert merged.returncode == 0
