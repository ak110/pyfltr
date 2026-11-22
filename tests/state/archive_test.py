import dataclasses
import datetime as dt
import json
import os
import pathlib

import pytest

import pyfltr.command.core_
import pyfltr.parsing.entry
import pyfltr.state.archive
from tests.conftest import make_archive_store as _make_store
from tests.conftest import make_command_result as _make_result
from tests.conftest import make_error_location as _make_error


def test_start_run_creates_directory_and_meta(tmp_path: pathlib.Path) -> None:
    store = _make_store(tmp_path)
    run_id = store.start_run(commands=["mypy", "ruff-check"], files=10)

    meta_path = tmp_path / "runs" / run_id / "meta.json"
    assert meta_path.exists()

    meta = json.loads(meta_path.read_text(encoding="utf-8"))
    assert meta["run_id"] == run_id
    assert "version" in meta
    assert "python" in meta
    assert "started_at" in meta
    assert meta["commands"] == ["mypy", "ruff-check"]
    assert meta["files"] == 10


def test_write_tool_result(tmp_path: pathlib.Path) -> None:
    store = _make_store(tmp_path)
    run_id = store.start_run(commands=["mypy"])

    errors = [_make_error("mypy", "src/a.py", 10, "型エラー", col=5)]
    result = _make_result("mypy", returncode=1, output="mypy output", errors=errors)
    store.write_tool_result(run_id, result)

    tool_dir = tmp_path / "runs" / run_id / "tools" / "mypy"
    assert (tool_dir / "output.log").exists()
    assert (tool_dir / "tool.json").exists()
    assert (tool_dir / "diagnostics.jsonl").exists()

    assert (tool_dir / "output.log").read_text(encoding="utf-8") == "mypy output"

    lines = [json.loads(line) for line in (tool_dir / "diagnostics.jsonl").read_text(encoding="utf-8").splitlines() if line]
    assert len(lines) == 1
    assert lines[0]["kind"] == "diagnostic"
    assert lines[0]["command"] == "mypy"
    assert lines[0]["file"] == "src/a.py"
    messages = lines[0]["messages"]
    assert len(messages) == 1
    assert messages[0]["line"] == 10
    assert messages[0]["col"] == 5
    assert messages[0]["msg"] == "型エラー"


def test_write_tool_result_stores_hint_urls(tmp_path: pathlib.Path) -> None:
    store = _make_store(tmp_path)
    run_id = store.start_run(commands=["ruff-check"])

    error = _make_error("ruff-check", "src/foo.py", 10, "unused import", col=5)
    error.rule = "F401"
    error.rule_url = "https://docs.astral.sh/ruff/rules/F401/"
    error.severity = "error"
    result = _make_result("ruff-check", returncode=1, output="ruff output", errors=[error])
    store.write_tool_result(run_id, result)

    tool_dir = tmp_path / "runs" / run_id / "tools" / "ruff-check"
    diagnostics_path = tool_dir / "diagnostics.jsonl"
    entries = [json.loads(line) for line in diagnostics_path.read_text(encoding="utf-8").splitlines() if line]
    assert entries[0]["file"] == "src/foo.py"
    assert entries[0]["messages"][0]["rule"] == "F401"
    assert "rule_url" not in entries[0]["messages"][0]

    tool_meta = json.loads((tool_dir / "tool.json").read_text(encoding="utf-8"))
    assert tool_meta["hint_urls"] == {"F401": "https://docs.astral.sh/ruff/rules/F401/"}


def test_write_tool_result_omits_hint_urls_when_no_urls(tmp_path: pathlib.Path) -> None:
    store = _make_store(tmp_path)
    run_id = store.start_run(commands=["mypy"])
    result = _make_result("mypy", returncode=1, errors=[_make_error("mypy", "a.py", 1, "boom")])
    store.write_tool_result(run_id, result)
    tool_meta = json.loads((tmp_path / "runs" / run_id / "tools" / "mypy" / "tool.json").read_text(encoding="utf-8"))
    assert "hint_urls" not in tool_meta


def test_write_tool_result_stores_hints(tmp_path: pathlib.Path) -> None:
    store = _make_store(tmp_path)
    run_id = store.start_run(commands=["textlint"])

    error = _make_error("textlint", "a.md", 1, "長い文", col=1)
    error.rule = "ja-technical-writing/sentence-length"
    error.hint = "句点で文を区切る"
    result = _make_result("textlint", returncode=1, errors=[error])
    store.write_tool_result(run_id, result)

    tool_meta = json.loads((tmp_path / "runs" / run_id / "tools" / "textlint" / "tool.json").read_text(encoding="utf-8"))
    assert tool_meta["hints"] == {"ja-technical-writing/sentence-length": "句点で文を区切る"}


def test_write_tool_result_omits_hints_when_no_hints(tmp_path: pathlib.Path) -> None:
    store = _make_store(tmp_path)
    run_id = store.start_run(commands=["mypy"])
    result = _make_result("mypy", returncode=1, errors=[_make_error("mypy", "a.py", 1, "boom")])
    store.write_tool_result(run_id, result)
    tool_meta = json.loads((tmp_path / "runs" / run_id / "tools" / "mypy" / "tool.json").read_text(encoding="utf-8"))
    assert "hints" not in tool_meta


def test_finalize_run(tmp_path: pathlib.Path) -> None:
    store = _make_store(tmp_path)
    run_id = store.start_run(commands=["ruff-check"])
    store.finalize_run(run_id, exit_code=1)

    meta = json.loads((tmp_path / "runs" / run_id / "meta.json").read_text(encoding="utf-8"))
    assert meta["exit_code"] == 1
    assert "finished_at" in meta


def test_list_runs_sorted_desc(tmp_path: pathlib.Path) -> None:
    store = _make_store(tmp_path)
    run_ids = [store.start_run() for _ in range(3)]

    summaries = store.list_runs()
    returned_ids = [s.run_id for s in summaries]
    assert returned_ids == sorted(run_ids, reverse=True)


def test_list_runs_limit(tmp_path: pathlib.Path) -> None:
    store = _make_store(tmp_path)
    for _ in range(5):
        store.start_run()

    summaries = store.list_runs(limit=3)
    assert len(summaries) == 3


def test_read_meta(tmp_path: pathlib.Path) -> None:
    store = _make_store(tmp_path)
    run_id = store.start_run(commands=["mypy"], files=5)

    meta = store.read_meta(run_id)
    assert meta["run_id"] == run_id
    assert meta["commands"] == ["mypy"]
    assert meta["files"] == 5


def test_read_meta_not_found(tmp_path: pathlib.Path) -> None:
    store = _make_store(tmp_path)
    with pytest.raises(FileNotFoundError):
        store.read_meta("nonexistent-run-id")


def test_read_tool_meta(tmp_path: pathlib.Path) -> None:
    store = _make_store(tmp_path)
    run_id = store.start_run()
    result = _make_result("ruff-check", returncode=0)
    store.write_tool_result(run_id, result)

    tool_meta = store.read_tool_meta(run_id, "ruff-check")
    assert tool_meta["command"] == "ruff-check"
    assert tool_meta["returncode"] == 0


def test_read_tool_meta_removes_retired_key_and_preserves_unknown_key(tmp_path: pathlib.Path) -> None:
    store = _make_store(tmp_path)
    run_id = store.start_run(commands=["ruff-check"])
    store.write_tool_result(run_id, _make_result("ruff-check", returncode=1))
    meta_path = tmp_path / "runs" / run_id / "tools" / "ruff-check" / "tool.json"
    stored = json.loads(meta_path.read_text(encoding="utf-8"))
    stored["has_error"] = True
    stored["future_key"] = "preserved"
    meta_path.write_text(json.dumps(stored), encoding="utf-8")

    tool_meta = store.read_tool_meta(run_id, "ruff-check")

    assert "has_error" not in tool_meta
    assert tool_meta["future_key"] == "preserved"


def test_read_tool_output(tmp_path: pathlib.Path) -> None:
    store = _make_store(tmp_path)
    run_id = store.start_run()
    result = _make_result("ruff-check", returncode=0, output="all clear")
    store.write_tool_result(run_id, result)

    output = store.read_tool_output(run_id, "ruff-check")
    assert output == "all clear"


def test_read_tool_diagnostics(tmp_path: pathlib.Path) -> None:
    store = _make_store(tmp_path)
    run_id = store.start_run()
    errors = [
        _make_error("mypy", "a.py", 1, "error A"),
        _make_error("mypy", "a.py", 3, "error A2"),
        _make_error("mypy", "b.py", 2, "error B"),
    ]
    result = _make_result("mypy", returncode=1, errors=errors)
    store.write_tool_result(run_id, result)

    diagnostics = store.read_tool_diagnostics(run_id, "mypy")
    assert len(diagnostics) == 2
    assert diagnostics[0]["file"] == "a.py"
    assert len(diagnostics[0]["messages"]) == 2
    assert diagnostics[1]["file"] == "b.py"
    assert len(diagnostics[1]["messages"]) == 1


def test_list_tools_empty_run(tmp_path: pathlib.Path) -> None:
    store = _make_store(tmp_path)
    run_id = store.start_run()

    assert store.list_tools(run_id) == []


def test_list_tools_multiple(tmp_path: pathlib.Path) -> None:
    store = _make_store(tmp_path)
    run_id = store.start_run()
    for tool in ("mypy", "ruff-check", "ruff-format"):
        store.write_tool_result(run_id, _make_result(tool, returncode=0))

    assert store.list_tools(run_id) == ["mypy", "ruff-check", "ruff-format"]


def test_list_tools_not_found(tmp_path: pathlib.Path) -> None:
    store = _make_store(tmp_path)
    with pytest.raises(FileNotFoundError):
        store.list_tools("nonexistent-run-id")


def _set_meta_times(
    tmp_path: pathlib.Path,
    run_id: str,
    *,
    started_days_ago: float | None = None,
    finished_days_ago: float | None = None,
) -> None:
    meta_path = tmp_path / "runs" / run_id / "meta.json"
    meta = json.loads(meta_path.read_text(encoding="utf-8"))
    now = dt.datetime.now(dt.UTC)
    if started_days_ago is not None:
        meta["started_at"] = (now - dt.timedelta(days=started_days_ago)).isoformat()
    if finished_days_ago is not None:
        meta["finished_at"] = (now - dt.timedelta(days=finished_days_ago)).isoformat()
    meta_path.write_text(json.dumps(meta), encoding="utf-8")


def test_cleanup_max_runs(tmp_path: pathlib.Path) -> None:
    store = _make_store(tmp_path)
    run_ids = []
    for _ in range(101):
        run_id = store.start_run()
        store.finalize_run(run_id, exit_code=0)
        run_ids.append(run_id)

    policy = pyfltr.state.archive.ArchivePolicy(max_runs=100, max_size_bytes=0, max_age_days=0)
    removed = store.cleanup(policy)

    assert removed == [min(run_ids)]
    assert len(store.list_runs()) == 100


def test_cleanup_max_size(tmp_path: pathlib.Path) -> None:
    store = _make_store(tmp_path)
    run_id = store.start_run()
    large_output = "x" * (2 * 1024 * 1024)
    result = _make_result("mypy", returncode=0, output=large_output)
    store.write_tool_result(run_id, result)
    store.finalize_run(run_id, exit_code=0)

    run_id2 = store.start_run()

    policy = pyfltr.state.archive.ArchivePolicy(max_runs=0, max_size_bytes=1 * 1024 * 1024, max_age_days=0)
    removed = store.cleanup(policy)

    assert run_id in removed
    remaining_ids = [s.run_id for s in store.list_runs()]
    assert run_id2 in remaining_ids


def test_cleanup_max_age(tmp_path: pathlib.Path) -> None:
    store = _make_store(tmp_path)
    run_id = store.start_run()
    store.finalize_run(run_id, exit_code=0)
    _set_meta_times(tmp_path, run_id, started_days_ago=60, finished_days_ago=60)

    run_id2 = store.start_run()

    policy = pyfltr.state.archive.ArchivePolicy(max_runs=0, max_size_bytes=0, max_age_days=30)
    removed = store.cleanup(policy)

    assert run_id in removed
    remaining_ids = [s.run_id for s in store.list_runs()]
    assert run_id2 in remaining_ids


@pytest.mark.parametrize(
    "policy",
    [
        pytest.param(pyfltr.state.archive.ArchivePolicy(max_runs=1, max_size_bytes=0, max_age_days=0), id="max_runs"),
        pytest.param(pyfltr.state.archive.ArchivePolicy(max_runs=0, max_size_bytes=1024 * 1024, max_age_days=0), id="max_size"),
        pytest.param(pyfltr.state.archive.ArchivePolicy(max_runs=0, max_size_bytes=0, max_age_days=30), id="max_age"),
    ],
)
def test_cleanup_keeps_active_run_on_every_axis(tmp_path: pathlib.Path, policy: pyfltr.state.archive.ArchivePolicy) -> None:
    running_store = _make_store(tmp_path)
    running = running_store.start_run(commands=["mypy"])
    running_store.write_tool_result(running, _make_result("mypy", returncode=1, output="x" * (2 * 1024 * 1024)))
    _set_meta_times(tmp_path, running, started_days_ago=60)

    other_store = _make_store(tmp_path)
    finished = other_store.start_run()
    other_store.finalize_run(finished, exit_code=0)
    _set_meta_times(tmp_path, finished, started_days_ago=60, finished_days_ago=60)
    removed = other_store.cleanup(policy)

    assert running not in removed
    running_store.finalize_run(running, exit_code=1)
    assert running_store.read_tool_output(running, "mypy").startswith("x")
    assert running_store.read_meta(running)["exit_code"] == 1


def test_cleanup_counts_active_run_but_removes_finished(tmp_path: pathlib.Path) -> None:
    store = _make_store(tmp_path)
    running = store.start_run()
    finished = store.start_run()
    store.finalize_run(finished, exit_code=0)

    removed = store.cleanup(pyfltr.state.archive.ArchivePolicy(max_runs=1, max_size_bytes=0, max_age_days=0))

    assert removed == [finished]
    assert [summary.run_id for summary in store.list_runs()] == [running]


def test_cleanup_orders_by_finished_at(tmp_path: pathlib.Path) -> None:
    store = _make_store(tmp_path)
    long_run = store.start_run()
    short_run = store.start_run()
    store.finalize_run(short_run, exit_code=0)
    _set_meta_times(tmp_path, short_run, finished_days_ago=1)
    store.finalize_run(long_run, exit_code=1)

    removed = store.cleanup(pyfltr.state.archive.ArchivePolicy(max_runs=1, max_size_bytes=0, max_age_days=0))

    assert removed == [short_run]
    assert store.read_meta(long_run)["exit_code"] == 1


def test_cleanup_removes_unfinished_run_without_holder(tmp_path: pathlib.Path) -> None:
    crashed_store = _make_store(tmp_path)
    crashed = crashed_store.start_run(commands=["mypy"])
    crashed_store.write_tool_result(crashed, _make_result("mypy", returncode=0, output="saved"))
    crashed_store.release_run(crashed)

    reader = _make_store(tmp_path)
    assert reader.read_tool_output(crashed, "mypy") == "saved"
    newer = reader.start_run()
    reader.finalize_run(newer, exit_code=0)
    removed = reader.cleanup(pyfltr.state.archive.ArchivePolicy(max_runs=1, max_size_bytes=0, max_age_days=0))

    assert removed == [crashed]
    assert not (tmp_path / "runs" / f"{crashed}.lock").exists()


def test_generate_run_id_unique_and_valid() -> None:
    ids = [pyfltr.state.archive.generate_run_id() for _ in range(10)]
    assert all(len(i) == 26 for i in ids)
    assert all(c in "0123456789ABCDEFGHJKMNPQRSTVWXYZ" for i in ids for c in i)
    assert len(set(ids)) == len(ids)


def _pytest_failure_output(test_file: str, line: int, test_name: str) -> str:
    return (
        "================================= FAILURES =================================\n"
        f"_______________________________ {test_name} ________________________________\n"
        f"{test_file}:{line}: in {test_name}\n"
        "    assert value\n"
        "E   AssertionError: assert 0\n"
        "========================= short test summary info ==========================\n"
        f"FAILED {test_file}::{test_name} - AssertionError: assert 0\n"
    )


def _start_legacy_run(store: pyfltr.state.archive.ArchiveStore, tmp_path: pathlib.Path, command: str = "pytest") -> str:
    run_id = store.start_run(commands=[command])
    meta_path = tmp_path / "runs" / run_id / "meta.json"
    meta = json.loads(meta_path.read_text(encoding="utf-8"))
    del meta[pyfltr.state.archive.DIAGNOSTIC_PATHS_META_KEY]
    meta_path.write_text(json.dumps(meta), encoding="utf-8")
    return run_id


def _legacy_pytest_result(sections: dict[str, str]) -> pyfltr.command.core_.CommandResult:
    return _legacy_result("pytest", sections)


def _legacy_result(command: str, sections: dict[str, str]) -> pyfltr.command.core_.CommandResult:
    output = "\n".join(f"# subproject: {relative}\n{body}" for relative, body in sections.items())
    errors = [error for body in sections.values() for error in pyfltr.parsing.entry.parse_errors(command, body)]
    return _make_result(command, returncode=1, output=output, errors=errors)


def _mypy_output(path: str, line: int, message: str = "Incompatible types in assignment") -> str:
    return f"{path}:{line}: error: {message}  [assignment]\nFound 1 error in 1 file (checked 1 source file)\n"


def _bandit_output(path: str, line: int) -> str:
    return json.dumps(
        {
            "results": [
                {
                    "filename": path,
                    "line_number": line,
                    "col_offset": 0,
                    "test_id": "B101",
                    "test_name": "assert_used",
                    "issue_severity": "LOW",
                    "issue_text": "Use of assert detected.",
                }
            ],
            "errors": [],
        }
    )


def test_read_tool_diagnostics_restores_legacy_mypy_paths(tmp_path: pathlib.Path) -> None:
    store = _make_store(tmp_path)
    run_id = _start_legacy_run(store, tmp_path, "mypy")
    store.write_tool_result(
        run_id, _legacy_result("mypy", {"app": _mypy_output("src/sample.py", 3), "lib": _mypy_output("src/other.py", 5)})
    )

    diagnostics = store.read_tool_diagnostics(run_id, "mypy")

    assert sorted(entry["file"] for entry in diagnostics) == ["app/src/sample.py", "lib/src/other.py"]
    raw = (tmp_path / "runs" / run_id / "tools" / "mypy" / "diagnostics.jsonl").read_text(encoding="utf-8")
    assert '"file": "src/sample.py"' in raw


def test_read_tool_diagnostics_restores_legacy_structured_paths(tmp_path: pathlib.Path) -> None:
    store = _make_store(tmp_path)
    run_id = _start_legacy_run(store, tmp_path, "bandit")
    store.write_tool_result(run_id, _legacy_result("bandit", {"pkg": _bandit_output("src/foo.py", 12)}))

    assert [entry["file"] for entry in store.read_tool_diagnostics(run_id, "bandit")] == ["pkg/src/foo.py"]


def test_read_tool_diagnostics_keeps_unrestorable_legacy_mypy_paths(tmp_path: pathlib.Path) -> None:
    store = _make_store(tmp_path)
    same = _mypy_output("src/sample.py", 3)
    ambiguous_run = _start_legacy_run(store, tmp_path, "mypy")
    store.write_tool_result(ambiguous_run, _legacy_result("mypy", {"app": same, "lib": same}))
    assert [entry["file"] for entry in store.read_tool_diagnostics(ambiguous_run, "mypy")] == ["src/sample.py"]

    absolute = (tmp_path / "abs" / "sample.py").as_posix()
    absolute_run = _start_legacy_run(store, tmp_path, "mypy")
    store.write_tool_result(absolute_run, _legacy_result("mypy", {"app": _mypy_output(absolute, 3)}))
    assert [entry["file"] for entry in store.read_tool_diagnostics(absolute_run, "mypy")] == [absolute]

    no_section_run = _start_legacy_run(store, tmp_path, "mypy")
    errors = pyfltr.parsing.entry.parse_errors("mypy", same)
    store.write_tool_result(no_section_run, _make_result("mypy", returncode=1, output=same, errors=errors))
    assert [entry["file"] for entry in store.read_tool_diagnostics(no_section_run, "mypy")] == ["src/sample.py"]

    no_output_run = _start_legacy_run(store, tmp_path, "mypy")
    store.write_tool_result(no_output_run, _legacy_result("mypy", {"app": same}))
    (tmp_path / "runs" / no_output_run / "tools" / "mypy" / "output.log").unlink()
    assert [entry["file"] for entry in store.read_tool_diagnostics(no_output_run, "mypy")] == ["src/sample.py"]


@pytest.mark.parametrize("tool", ["mypy", "bandit"])
@pytest.mark.parametrize(
    "path",
    ["/outside/sample.py", "C:/Users/test/sample.py", "D:/outside/sample.py", "C:sample.py", "//server/share/sample.py"],
)
def test_read_tool_diagnostics_keeps_legacy_paths_of_any_os_form(tmp_path: pathlib.Path, tool: str, path: str) -> None:
    store = _make_store(tmp_path)
    run_id = _start_legacy_run(store, tmp_path, tool)
    make_output = _mypy_output if tool == "mypy" else _bandit_output
    sections = {"app": make_output(path, 3), "lib": make_output("src/other.py", 5)}
    store.write_tool_result(run_id, _legacy_result(tool, sections))
    [saved] = [error.file for error in pyfltr.parsing.entry.parse_errors(tool, sections["app"])]
    if os.name != "nt":
        assert saved == path

    assert sorted(entry["file"] for entry in store.read_tool_diagnostics(run_id, tool)) == sorted([saved, "lib/src/other.py"])


def test_read_tool_diagnostics_keeps_current_mypy_paths(tmp_path: pathlib.Path) -> None:
    store = _make_store(tmp_path)
    run_id = store.start_run(commands=["mypy"])
    body = _mypy_output("src/sample.py", 3)
    errors = [dataclasses.replace(error, file=f"app/{error.file}") for error in pyfltr.parsing.entry.parse_errors("mypy", body)]
    store.write_tool_result(run_id, _make_result("mypy", returncode=1, output=f"# subproject: app\n{body}", errors=errors))

    assert [entry["file"] for entry in store.read_tool_diagnostics(run_id, "mypy")] == ["app/src/sample.py"]


def test_read_tool_diagnostics_restores_legacy_subproject_paths(tmp_path: pathlib.Path) -> None:
    store = _make_store(tmp_path)
    run_id = _start_legacy_run(store, tmp_path)
    store.write_tool_result(
        run_id,
        _legacy_pytest_result(
            {
                "pkg_a": _pytest_failure_output("tests/x_test.py", 3, "test_a"),
                "pkg_b": _pytest_failure_output("tests/y_test.py", 5, "test_b"),
            }
        ),
    )

    diagnostics = store.read_tool_diagnostics(run_id, "pytest")

    assert sorted(entry["file"] for entry in diagnostics) == ["pkg_a/tests/x_test.py", "pkg_b/tests/y_test.py"]
    raw = (tmp_path / "runs" / run_id / "tools" / "pytest" / "diagnostics.jsonl").read_text(encoding="utf-8")
    assert '"file": "tests/x_test.py"' in raw


def test_read_tool_diagnostics_keeps_ambiguous_and_current_paths(tmp_path: pathlib.Path) -> None:
    store = _make_store(tmp_path)
    same_failure = _pytest_failure_output("tests/x_test.py", 3, "test_a")
    ambiguous_run = _start_legacy_run(store, tmp_path)
    store.write_tool_result(ambiguous_run, _legacy_pytest_result({"pkg_a": same_failure, "pkg_b": same_failure}))

    current_run = store.start_run(commands=["pytest"])
    current_errors = [
        dataclasses.replace(error, file=f"pkg_a/{error.file}")
        for error in pyfltr.parsing.entry.parse_errors("pytest", same_failure)
    ]
    store.write_tool_result(
        current_run,
        _make_result("pytest", returncode=1, output=f"# subproject: pkg_a\n{same_failure}", errors=current_errors),
    )

    assert [entry["file"] for entry in store.read_tool_diagnostics(ambiguous_run, "pytest")] == ["tests/x_test.py"]
    assert [entry["file"] for entry in store.read_tool_diagnostics(current_run, "pytest")] == ["pkg_a/tests/x_test.py"]


def test_read_tool_diagnostics_keeps_current_root_and_subproject_paths(tmp_path: pathlib.Path) -> None:
    store = _make_store(tmp_path)
    same_failure = _pytest_failure_output("tests/x_test.py", 3, "test_a")
    root_errors = pyfltr.parsing.entry.parse_errors("pytest", same_failure)
    child_errors = [dataclasses.replace(error, file=f"pkg_a/{error.file}") for error in root_errors]
    run_id = store.start_run(commands=["pytest"])
    store.write_tool_result(
        run_id,
        _make_result(
            "pytest",
            returncode=1,
            output=f"{same_failure}\n# subproject: pkg_a\n{same_failure}",
            errors=[*root_errors, *child_errors],
        ),
    )

    assert sorted(entry["file"] for entry in store.read_tool_diagnostics(run_id, "pytest")) == [
        "pkg_a/tests/x_test.py",
        "tests/x_test.py",
    ]


def test_record_slot_wait_updates_interval_and_keeps_unfinished(tmp_path: pathlib.Path) -> None:
    store = _make_store(tmp_path)
    run_id = store.start_run(commands=["mypy"])
    assert store.read_meta(run_id)[pyfltr.state.archive.SLOT_WAITS_META_KEY] == []

    started = {"kind": "initial", "sequence": 0, "started_at": "2026-11-05T00:00:00+00:00", "finished_at": None}
    store.record_slot_wait(run_id, {**started, "elapsed": None, "outcome": None})
    store.record_slot_wait(
        run_id, {**started, "finished_at": "2026-11-05T00:00:02+00:00", "elapsed": 2.0, "outcome": "acquired"}
    )
    store.record_slot_wait(
        run_id,
        {"kind": "reacquire", "sequence": 1, "started_at": "2026-11-05T00:00:03+00:00", "finished_at": None, "elapsed": None},
    )
    waits = store.read_meta(run_id)[pyfltr.state.archive.SLOT_WAITS_META_KEY]
    assert [(wait["kind"], wait["sequence"], wait["elapsed"]) for wait in waits] == [
        ("initial", 0, 2.0),
        ("reacquire", 1, None),
    ]
    store.finalize_run(run_id, exit_code=0)
    assert len(store.read_meta(run_id)[pyfltr.state.archive.SLOT_WAITS_META_KEY]) == 2
    assert not list((tmp_path / "runs" / run_id).glob(".meta.json.*"))


def test_write_tool_result_stores_body_interval_only_when_executed(tmp_path: pathlib.Path) -> None:
    store = _make_store(tmp_path)
    run_id = store.start_run(commands=["mypy", "ruff-check"])
    executed = _make_result("mypy", returncode=0)
    executed.started_at = "2026-11-05T00:00:00+00:00"
    executed.finished_at = "2026-11-05T00:00:01+00:00"
    store.write_tool_result(run_id, executed)
    store.write_tool_result(run_id, _make_result("ruff-check", returncode=0))

    mypy_meta = store.read_tool_meta(run_id, "mypy")
    assert (mypy_meta["started_at"], mypy_meta["finished_at"]) == (executed.started_at, executed.finished_at)
    assert "started_at" not in store.read_tool_meta(run_id, "ruff-check")


def test_write_tool_result_stores_launch_boundary_only_when_recorded(tmp_path: pathlib.Path) -> None:
    store = _make_store(tmp_path)
    run_id = store.start_run(commands=["pytest", "mypy"])
    tester = _make_result("pytest", returncode=0)
    tester.launch_boundary_at = "2026-11-13T03:55:25.000100+00:00"
    store.write_tool_result(run_id, tester)
    store.write_tool_result(run_id, _make_result("mypy", returncode=0))

    assert store.read_tool_meta(run_id, "pytest")["launch_boundary_at"] == tester.launch_boundary_at
    assert "launch_boundary_at" not in store.read_tool_meta(run_id, "mypy")
