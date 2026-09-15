"""`pyfltr.command.subproject_loop`のテスト。

サブプロジェクト単位の並列実行について、同時実行数の決定、報告順の安定、
および警告の重複抑止が成立することを検証する。
"""

import argparse
import pathlib
import threading
import typing

import pytest

import pyfltr.command.core_
import pyfltr.command.subproject_loop
import pyfltr.command.subprojects
import pyfltr.command.tool_parallelism
import pyfltr.config.config
import pyfltr.warnings_
import tests.conftest as _testconf


def _make_context(
    tmp_path: pathlib.Path, names: list[str], *, values: dict[str, typing.Any] | None = None
) -> pyfltr.command.core_.ExecutionContext:
    """指定した名前のサブプロジェクトを持つモノレポ構成の`ExecutionContext`を組み立てる。"""
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
    """`resolve_subproject_workers`のテスト。"""

    def test_explicit_value_is_used_as_is(self, tmp_path: pathlib.Path) -> None:
        ctx = _make_context(tmp_path, ["a", "b"], values={"subproject-jobs": 3})
        cwds = [s.cwd for s in ctx.base.subprojects]
        assert pyfltr.command.subproject_loop.resolve_subproject_workers("ruff-check", ctx, cwds) == 3

    def test_one_means_sequential(self, tmp_path: pathlib.Path) -> None:
        ctx = _make_context(tmp_path, ["a", "b"], values={"subproject-jobs": 1})
        cwds = [s.cwd for s in ctx.base.subprojects]
        assert pyfltr.command.subproject_loop.resolve_subproject_workers("ruff-check", ctx, cwds) == 1

    def test_auto_uses_whole_budget_for_single_worker_tool(self, tmp_path: pathlib.Path) -> None:
        """ツール自身が並列化しない場合はホストの論理CPU数をそのまま同時実行数の上限にする。"""
        ctx = _make_context(tmp_path, ["a", "b"], values={"subproject-jobs": 0})
        cwds = [s.cwd for s in ctx.base.subprojects]
        expected = pyfltr.command.tool_parallelism.cpu_count()
        assert pyfltr.command.subproject_loop.resolve_subproject_workers("ruff-check", ctx, cwds) == expected

    def test_auto_divides_budget_by_tool_workers(self, tmp_path: pathlib.Path) -> None:
        """ツール側の並列度との積がホストの論理CPU数を超えないようにする。"""
        ctx = _make_context(tmp_path, ["a", "b"], values={"subproject-jobs": 0, "pytest-args": ["-n", "2"]})
        cwds = [s.cwd for s in ctx.base.subprojects]
        expected = max(1, pyfltr.command.tool_parallelism.cpu_count() // 2)
        assert pyfltr.command.subproject_loop.resolve_subproject_workers("pytest", ctx, cwds) == expected

    def test_auto_never_returns_zero(self, tmp_path: pathlib.Path) -> None:
        """ツール側の並列度がホストの論理CPU数以上でも1件は実行する。"""
        huge = str(pyfltr.command.tool_parallelism.cpu_count() * 4)
        ctx = _make_context(tmp_path, ["a", "b"], values={"subproject-jobs": 0, "pytest-args": ["-n", huge]})
        cwds = [s.cwd for s in ctx.base.subprojects]
        assert pyfltr.command.subproject_loop.resolve_subproject_workers("pytest", ctx, cwds) == 1

    def test_auto_uses_max_estimate_across_subprojects(self, tmp_path: pathlib.Path) -> None:
        """サブプロジェクトごとの推定値が異なる場合は大きい方を基準にする。"""
        ctx = _make_context(tmp_path, ["a", "b"], values={"subproject-jobs": 0})
        workers = pyfltr.command.tool_parallelism.cpu_count() * 2
        (tmp_path / "b" / "pyproject.toml").write_text(
            f'[project]\nname = "x"\nversion = "0"\n\n[tool.pytest.ini_options]\naddopts = "-n {workers}"\n',
            encoding="utf-8",
        )
        cwds = [s.cwd for s in ctx.base.subprojects]
        assert pyfltr.command.subproject_loop.resolve_subproject_workers("pytest", ctx, cwds) == 1


class TestRunSubprojectLoop:
    """`run_subproject_loop`の実行順と並列度のテスト。"""

    def test_sequential_when_workers_is_one(self, tmp_path: pathlib.Path) -> None:
        """`subproject-jobs = 1`ではサブプロジェクトが同時に進行しない。"""
        ctx = _make_context(tmp_path, ["a", "b", "c"], values={"subproject-jobs": 1})
        active = 0
        max_active = 0
        lock = threading.Lock()

        def _dispatch(
            command: str, args: argparse.Namespace, c: pyfltr.command.core_.ExecutionContext
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
        """`subproject-jobs = 2`では2件のサブプロジェクト実行が同時に進行する区間がある。"""
        ctx = _make_context(tmp_path, ["a", "b"], values={"subproject-jobs": 2})
        barrier = threading.Barrier(2, timeout=30)

        def _dispatch(
            command: str, args: argparse.Namespace, c: pyfltr.command.core_.ExecutionContext
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

    def test_output_order_follows_relative_path(self, tmp_path: pathlib.Path) -> None:
        """完了順が逆でも、区切り行の順序は相対パスの昇順で安定する。"""
        ctx = _make_context(tmp_path, ["b", "a"], values={"subproject-jobs": 2})
        started = threading.Event()

        def _dispatch(
            command: str, args: argparse.Namespace, c: pyfltr.command.core_.ExecutionContext
        ) -> pyfltr.command.core_.CommandResult:
            del args
            result = _testconf.make_succeeded_result(command=command)
            result.output = f"out for {c.subproject_cwd.name if c.subproject_cwd else 'root'}"
            # 先に開始した側を後から完了させ、完了順と報告順が一致しない状況を再現する。
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
        """並列実行でも、同一の`source`と`message`の警告は1件に収まる。"""
        pyfltr.warnings_.clear()
        ctx = _make_context(tmp_path, ["a", "b", "c"], values={"subproject-jobs": 3})

        def _dispatch(
            command: str, args: argparse.Namespace, c: pyfltr.command.core_.ExecutionContext
        ) -> pyfltr.command.core_.CommandResult:
            del args, c
            pyfltr.warnings_.emit_warning(source="dummy", message="同一の警告")
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


@pytest.mark.parametrize("subproject_jobs", [1, 2])
def test_all_subprojects_are_dispatched(tmp_path: pathlib.Path, subproject_jobs: int) -> None:
    """同時実行数にかかわらず、対象の全サブプロジェクトを1回ずつ実行する。"""
    ctx = _make_context(tmp_path, ["a", "b", "c"], values={"subproject-jobs": subproject_jobs})
    seen: list[str] = []
    lock = threading.Lock()

    def _dispatch(
        command: str, args: argparse.Namespace, c: pyfltr.command.core_.ExecutionContext
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
