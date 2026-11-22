import concurrent.futures
import dataclasses
import pathlib
import threading
import time

import pyfltr.command.core_
import pyfltr.command.executor
import pyfltr.config.config


def test_split_commands_estimated_time_order() -> None:
    config = pyfltr.config.config.create_default_config()
    for cmd in ["ruff-check", "mypy", "pylint", "pytest"]:
        config.values[cmd] = True
    commands = ["ruff-check", "mypy", "pylint", "pytest"]
    all_files = [pathlib.Path("test.py")]
    _, _, linters_and_testers = pyfltr.command.executor.split_commands_for_execution(commands, config, all_files)

    assert linters_and_testers[0] == "pytest"
    assert linters_and_testers[1] == "pylint"
    assert linters_and_testers[-1] == "ruff-check"


def test_split_commands_estimated_time_scales_with_files() -> None:
    config = pyfltr.config.config.create_default_config()
    config.values["mypy"] = True
    config.values["textlint"] = True
    commands = ["mypy", "textlint"]

    all_files = [pathlib.Path("test.py")]
    _, _, linters = pyfltr.command.executor.split_commands_for_execution(commands, config, all_files)
    assert linters[0] == "textlint"

    all_files = [pathlib.Path(f"file{i}.py") for i in range(100)]
    _, _, linters = pyfltr.command.executor.split_commands_for_execution(commands, config, all_files)
    assert linters[0] == "mypy"


def test_split_commands_include_fix_stage() -> None:
    config = pyfltr.config.config.create_default_config()
    config.values["ruff-check"] = True
    config.values["ruff-format"] = True
    config.values["markdownlint"] = True
    config.values["mypy"] = True
    commands = ["ruff-format", "ruff-check", "mypy", "markdownlint"]
    all_files: list[pathlib.Path] = []
    fixers, formatters, linters = pyfltr.command.executor.split_commands_for_execution(
        commands, config, all_files, include_fix_stage=True
    )

    assert "ruff-check" in fixers
    assert "markdownlint" in fixers
    assert "ruff-format" not in fixers
    assert "mypy" not in fixers

    assert "ruff-format" in formatters
    assert "ruff-check" in linters
    assert "mypy" in linters


def test_split_commands_no_fix_stage_by_default() -> None:
    config = pyfltr.config.config.create_default_config()
    commands = ["ruff-check"]
    fixers, _, _ = pyfltr.command.executor.split_commands_for_execution(commands, config, [])
    assert not fixers


def _assert_serial_groups_can_overlap(first: str | None, second: str | None) -> None:
    entered = threading.Event()

    def _worker() -> None:
        with pyfltr.command.executor.serial_group_lock(second):
            entered.set()

    worker = threading.Thread(target=_worker)
    try:
        with pyfltr.command.executor.serial_group_lock(first):
            worker.start()
            assert entered.wait(timeout=2.0)
    finally:
        worker.join(timeout=2.0)
    assert not worker.is_alive()


def test_serial_group_lock_noop_for_none() -> None:
    _assert_serial_groups_can_overlap(None, None)


def test_serial_group_lock_mutual_exclusion() -> None:
    group = "test-mutex"
    concurrent_count = 0
    max_concurrent = 0
    state_lock = threading.Lock()

    def _worker(index: int) -> int:
        nonlocal concurrent_count, max_concurrent
        del index
        with pyfltr.command.executor.serial_group_lock(group):
            with state_lock:
                concurrent_count += 1
                max_concurrent = max(max_concurrent, concurrent_count)
            time.sleep(0.05)
            with state_lock:
                concurrent_count -= 1
        return max_concurrent

    with concurrent.futures.ThreadPoolExecutor(max_workers=4) as executor:
        list(executor.map(_worker, range(4)))

    assert max_concurrent == 1, f"serial_group={group!r}で同一グループが並列実行された（max_concurrent={max_concurrent}）"


def test_serial_group_lock_independent_groups() -> None:
    _assert_serial_groups_can_overlap("test-group-a", "test-group-b")


def test_has_no_candidate_targets(tmp_path: pathlib.Path) -> None:
    config = pyfltr.config.config.create_default_config()
    sub_config = pyfltr.config.config.create_default_config()
    base = pyfltr.command.core_.ExecutionBaseContext(
        config=config, all_files=[pathlib.Path("README.md")], start_cwd=tmp_path, cache_store=None, cache_run_id=None
    )
    assert pyfltr.command.executor.has_no_candidate_targets("ruff-check", base)
    assert not pyfltr.command.executor.has_no_candidate_targets("markdownlint", base)
    sub_config.commands["ruff-check"] = dataclasses.replace(sub_config.commands["ruff-check"], targets=["*.md"])
    base.subproject_configs = {tmp_path / "sub": sub_config}
    assert not pyfltr.command.executor.has_no_candidate_targets("ruff-check", base)
    pytest_base = dataclasses.replace(base, all_files=[pathlib.Path("tests/a_test.py")])
    base.pytest_base = pytest_base
    assert not pyfltr.command.executor.has_no_candidate_targets("pytest", base)
    assert pyfltr.command.executor.has_no_candidate_targets(
        "pytest", dataclasses.replace(base, pytest_base=None, subproject_configs={})
    )
