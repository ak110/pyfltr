import dataclasses
import json
import pathlib

import mcp.server.mcpserver.exceptions
import pytest

import pyfltr.cli.main
import pyfltr.cli.mcp_grep_tools
import pyfltr.cli.mcp_models
import pyfltr.cli.mcp_run_tools
import pyfltr.cli.mcp_runs_tools
import pyfltr.cli.mcp_server
import pyfltr.cli.pipeline
import pyfltr.command.completion
import pyfltr.command.slow_tests
import pyfltr.grep_.history
import pyfltr.grep_.preview
import pyfltr.grep_.replacer
import pyfltr.grep_.types
import pyfltr.paths
import pyfltr.state.archive
import pyfltr.state.runs
from tests import conftest as _testconf
from tests.conftest import make_command_result as _make_result
from tests.conftest import make_error_location as _make_error
from tests.conftest import seed_archive_run as _seed_run

_ToolError = mcp.server.mcpserver.exceptions.ToolError

pytestmark = pytest.mark.usefixtures("mcp_cache")


@pytest.mark.asyncio
async def test_tool_list_runs_empty() -> None:
    result = await pyfltr.cli.mcp_runs_tools.tool_list_runs()
    assert result == []


@pytest.mark.asyncio
async def test_tool_list_runs_returns_summaries(tmp_path: pathlib.Path) -> None:
    run_id1 = _seed_run(tmp_path, commands=["ruff-check"], exit_code=0)
    run_id2 = _seed_run(tmp_path, commands=["mypy"], exit_code=1)

    result = await pyfltr.cli.mcp_runs_tools.tool_list_runs(limit=10)
    assert len(result) == 2
    # 新しい順（降順）
    assert result[0].run_id == run_id2
    assert result[1].run_id == run_id1
    assert result[0].exit_code == 1
    assert result[1].exit_code == 0


@pytest.mark.asyncio
async def test_tool_list_runs_limit(tmp_path: pathlib.Path) -> None:
    for _ in range(5):
        _seed_run(tmp_path)

    result = await pyfltr.cli.mcp_runs_tools.tool_list_runs(limit=2)
    assert len(result) == 2


@pytest.mark.asyncio
async def test_tool_show_run_overview(tmp_path: pathlib.Path) -> None:
    run_id = _seed_run(
        tmp_path,
        commands=["ruff-check", "mypy"],
        tool_results=[
            ("ruff-check", 0, "clean", []),
            ("mypy", 1, "error", [_make_error("mypy", "a.py", 1, "boom")]),
        ],
    )

    result = await pyfltr.cli.mcp_runs_tools.tool_show_run(run_id)
    assert result.run_id == run_id
    # runの識別子は最上位、待機区間はtimingが持ち、実行環境の内部値と保存形式の印は返さない。
    assert set(result.meta) <= set(pyfltr.cli.mcp_runs_tools.SHOW_RUN_META_KEYS)
    assert {"commands", "files", "started_at"} <= set(result.meta)
    for key in ("run_id", "slot_waits", "diagnostic_paths", "python", "executable", "platform"):
        assert key not in result.meta
    command_names = [c.command for c in result.commands]
    assert "ruff-check" in command_names
    assert "mypy" in command_names


@pytest.mark.asyncio
async def test_show_run_includes_elapsed_and_slow_tests(tmp_path: pathlib.Path) -> None:
    """show_runの戻り値へ所要時間と遅いテスト一覧が現れる。"""
    store = pyfltr.state.archive.ArchiveStore(cache_root=tmp_path)
    run_id = store.start_run(commands=["pytest"])
    slow_tests = [pyfltr.command.slow_tests.SlowTest("tests/a_test.py::test_x", "call", 1.5)]
    result = _make_result("pytest", returncode=0, command_type="tester")
    result = dataclasses.replace(result, elapsed=2.3456, slow_tests=slow_tests)
    store.write_tool_result(run_id, result)
    store.finalize_run(run_id, exit_code=0)

    overview = await pyfltr.cli.mcp_runs_tools.tool_show_run(run_id)
    assert len(overview.commands) == 1
    command = overview.commands[0]
    assert command.elapsed == 2.346
    assert [test.model_dump() for test in command.slow_tests] == [test.to_dict() for test in slow_tests]


@pytest.mark.asyncio
async def test_show_run_returns_timing(tmp_path: pathlib.Path) -> None:
    """show_runは待機区間・本体区間・run全体の経過を返し、未計測と0秒を区別する。"""
    store = pyfltr.state.archive.ArchiveStore(cache_root=tmp_path)
    run_id = store.start_run(commands=["pytest"])
    wait = {
        "kind": "initial",
        "sequence": 0,
        "started_at": "2026-11-05T00:00:00+00:00",
        "finished_at": "2026-11-05T00:00:01.25+00:00",
        "elapsed": 1.25,
        "outcome": "acquired",
    }
    store.record_slot_wait(run_id, wait)
    result = _make_result("pytest", returncode=0, command_type="tester")
    result.started_at = "2026-11-05T00:00:01.3+00:00"
    result.finished_at = "2026-11-05T00:00:02+00:00"
    store.write_tool_result(run_id, result)
    store.finalize_run(run_id, exit_code=0)

    overview = await pyfltr.cli.mcp_runs_tools.tool_show_run(run_id)
    assert overview.timing.measured
    assert overview.timing.run_elapsed is not None
    assert [item.model_dump() for item in overview.timing.slot_waits or []] == [wait]
    assert overview.timing.slot_wait_total == 1.25
    assert overview.timing.unfinished_slot_waits == 0
    assert (overview.commands[0].started_at, overview.commands[0].finished_at) == (result.started_at, result.finished_at)

    old_run_id = _seed_run(tmp_path, tool_results=[("mypy", 0, "", [])])
    meta_path = tmp_path / "runs" / old_run_id / "meta.json"
    meta = json.loads(meta_path.read_text(encoding="utf-8"))
    del meta[pyfltr.state.archive.SLOT_WAITS_META_KEY]
    meta_path.write_text(json.dumps(meta), encoding="utf-8")
    old = await pyfltr.cli.mcp_runs_tools.tool_show_run(old_run_id)
    assert not old.timing.measured
    assert old.timing.slot_waits is None
    assert old.timing.slot_wait_total is None


@pytest.mark.asyncio
async def test_tool_show_run_latest(tmp_path: pathlib.Path) -> None:
    _seed_run(tmp_path, commands=["ruff-check"])
    latest_id = _seed_run(tmp_path, commands=["mypy"])

    result = await pyfltr.cli.mcp_runs_tools.tool_show_run("latest")
    assert result.run_id == latest_id


@pytest.mark.asyncio
async def test_tool_show_run_prefix(tmp_path: pathlib.Path) -> None:
    run_id = _seed_run(tmp_path)
    result = await pyfltr.cli.mcp_runs_tools.tool_show_run(run_id[:8])
    assert result.run_id == run_id


@pytest.mark.asyncio
async def test_tool_show_run_not_found() -> None:
    with pytest.raises(_ToolError, match="run_id"):
        await pyfltr.cli.mcp_runs_tools.tool_show_run("nonexistent")


@pytest.mark.asyncio
async def test_tool_show_run_latest_empty() -> None:
    with pytest.raises(_ToolError, match="run"):
        await pyfltr.cli.mcp_runs_tools.tool_show_run("latest")


@pytest.mark.asyncio
async def test_tool_show_run_ambiguous_prefix(tmp_path: pathlib.Path) -> None:
    run_ids = [_seed_run(tmp_path) for _ in range(2)]
    # ULIDの先頭は同じタイムスタンプ部分（ミリ秒単位）を共有する可能性が高いため、
    # 実際に共通する最長プレフィックスを算出してテストする。
    shared = _testconf.shared_prefix_length(run_ids[0], run_ids[1])
    if shared < 1:
        pytest.skip("shared prefixが無いケースでは曖昧判定にならない")
    prefix = run_ids[0][:shared]

    with pytest.raises(_ToolError, match="曖昧"):
        await pyfltr.cli.mcp_runs_tools.tool_show_run(prefix)


@pytest.mark.asyncio
async def test_tool_show_run_diagnostics(tmp_path: pathlib.Path) -> None:
    run_id = _seed_run(
        tmp_path,
        tool_results=[
            (
                "mypy",
                1,
                "mypy output",
                [_make_error("mypy", "src/a.py", 42, "型エラー", col=5)],
            ),
        ],
    )

    results = await pyfltr.cli.mcp_runs_tools.tool_show_run_diagnostics(run_id, ["mypy"])
    assert len(results) == 1
    result = results[0]
    assert result.command_meta.command == "mypy"
    assert result.command_meta.type == "linter"
    assert result.command_meta.status == "failed"
    assert result.command_meta.returncode == 1
    assert result.command_meta.files == 1
    assert result.command_meta.elapsed == 0.1
    assert result.command_meta.diagnostics == 1
    assert len(result.diagnostics) == 1
    diagnostic = result.diagnostics[0]
    assert diagnostic.file == "src/a.py"
    assert len(diagnostic.messages) == 1
    message = diagnostic.messages[0]
    assert message.line == 42
    assert message.col == 5
    assert message.msg == "型エラー"


@pytest.mark.asyncio
async def test_tool_show_run_diagnostics_omits_commandline(tmp_path: pathlib.Path) -> None:
    """検査対象ファイルの引数列を`command_meta`へ含めない。"""
    store = pyfltr.state.archive.ArchiveStore(cache_root=tmp_path)
    run_id = store.start_run(commands=["mypy"])
    result = dataclasses.replace(
        _make_result("mypy", returncode=1),
        commandline=["mypy", "src/first.py", "src/second.py"],
    )
    store.write_tool_result(run_id, result)

    results = await pyfltr.cli.mcp_runs_tools.tool_show_run_diagnostics(run_id, ["mypy"])
    command_meta = results[0].command_meta.model_dump()
    assert "commandline" not in command_meta
    assert "src/first.py" not in json.dumps(command_meta)
    assert "src/second.py" not in json.dumps(command_meta)


@pytest.mark.asyncio
async def test_tool_show_run_diagnostics_restores_hint_urls_and_hints(tmp_path: pathlib.Path) -> None:
    """`hint_urls`と`hints`を専用フィールドだけへ復元する。"""
    error = _make_error("textlint", "a.md", 1, "長い文", col=1)
    error.rule = "ja-technical-writing/sentence-length"
    error.rule_url = "https://example.com/sentence-length"
    error.hint = "句点で文を区切る"
    store = pyfltr.state.archive.ArchiveStore(cache_root=tmp_path)
    run_id = store.start_run(commands=["textlint"])
    result = _make_result("textlint", returncode=1, errors=[error])
    store.write_tool_result(run_id, result)
    store.finalize_run(run_id, exit_code=1)

    results = await pyfltr.cli.mcp_runs_tools.tool_show_run_diagnostics(run_id, ["textlint"])
    assert len(results) == 1
    command_diagnostics = results[0]
    assert command_diagnostics.hint_urls == {"ja-technical-writing/sentence-length": "https://example.com/sentence-length"}
    assert command_diagnostics.hints == {"ja-technical-writing/sentence-length": "句点で文を区切る"}
    assert "hint_urls" not in command_diagnostics.command_meta.model_dump()
    assert "hints" not in command_diagnostics.command_meta.model_dump()


@pytest.mark.asyncio
async def test_tool_show_run_diagnostics_restores_optional_meta(tmp_path: pathlib.Path) -> None:
    """遅いテスト一覧と再実行コマンドを`command_meta`へ復元する。"""
    store = pyfltr.state.archive.ArchiveStore(cache_root=tmp_path)
    run_id = store.start_run(commands=["pytest"])
    slow_tests = [pyfltr.command.slow_tests.SlowTest("tests/a_test.py::test_x", "call", 1.5)]
    result = dataclasses.replace(
        _make_result("pytest", returncode=1, command_type="tester"),
        slow_tests=slow_tests,
        retry_command="pyfltr run --commands=pytest tests/a_test.py",
    )
    store.write_tool_result(run_id, result)

    results = await pyfltr.cli.mcp_runs_tools.tool_show_run_diagnostics(run_id, ["pytest"])
    command_meta = results[0].command_meta
    assert [test.model_dump() for test in command_meta.slow_tests] == [test.to_dict() for test in slow_tests]
    assert command_meta.retry_command == "pyfltr run --commands=pytest tests/a_test.py"


@pytest.mark.asyncio
async def test_tool_show_run_diagnostics_skipped_command(tmp_path: pathlib.Path) -> None:
    """起動しなかったコマンドのNone終了コードを復元する。"""
    store = pyfltr.state.archive.ArchiveStore(cache_root=tmp_path)
    run_id = store.start_run(commands=["mypy"])
    store.write_tool_result(run_id, _make_result("mypy", returncode=None, files=0))

    results = await pyfltr.cli.mcp_runs_tools.tool_show_run_diagnostics(run_id, ["mypy"])
    assert results[0].command_meta.returncode is None


@pytest.mark.asyncio
async def test_tool_show_run_diagnostics_hints_none_when_absent(tmp_path: pathlib.Path) -> None:
    """tool.jsonにhintsキーが無い場合、`show_run_diagnostics`の`hints`はNoneになる。"""
    run_id = _seed_run(
        tmp_path,
        tool_results=[
            ("mypy", 0, "clean", []),
        ],
    )

    results = await pyfltr.cli.mcp_runs_tools.tool_show_run_diagnostics(run_id, ["mypy"])
    assert len(results) == 1
    assert results[0].hints is None


@pytest.mark.asyncio
async def test_tool_show_run_diagnostics_tool_not_found(tmp_path: pathlib.Path) -> None:
    run_id = _seed_run(tmp_path)
    with pytest.raises(_ToolError, match="nonexistent"):
        await pyfltr.cli.mcp_runs_tools.tool_show_run_diagnostics(run_id, ["nonexistent"])


@pytest.mark.asyncio
async def test_tool_show_run_output(tmp_path: pathlib.Path) -> None:
    run_id = _seed_run(
        tmp_path,
        tool_results=[
            ("ruff-check", 0, "raw output line 1\nraw output line 2\n", []),
            ("mypy", 1, "mypy output\n", [_make_error("mypy", "src/a.py", 1, "型エラー")]),
        ],
    )

    result = await pyfltr.cli.mcp_runs_tools.tool_show_run_output(run_id, ["ruff-check", "mypy"])
    assert list(result) == ["ruff-check", "mypy"]
    assert result["ruff-check"] == "raw output line 1\nraw output line 2\n"
    assert result["mypy"] == "mypy output\n"


@pytest.mark.asyncio
async def test_tool_show_run_output_tool_not_found(tmp_path: pathlib.Path) -> None:
    run_id = _seed_run(tmp_path)
    with pytest.raises(_ToolError, match="nonexistent"):
        await pyfltr.cli.mcp_runs_tools.tool_show_run_output(run_id, ["nonexistent"])


@pytest.mark.asyncio
async def test_show_run_diagnostics_reports_next_offset_when_messages_remain(tmp_path: pathlib.Path) -> None:
    """範囲の後に指摘が残る場合は、続きを取得する開始位置を返す。"""
    errors = [_make_error("mypy", "src/a.py", line, f"指摘{line}") for line in (1, 2, 3)]
    run_id = _seed_run(tmp_path, tool_results=[("mypy", 1, "out", errors)])

    (result,) = await pyfltr.cli.mcp_runs_tools.tool_show_run_diagnostics(run_id, ["mypy"], offset=0, limit=2)
    assert [message.msg for message in result.diagnostics[0].messages] == ["指摘1", "指摘2"]
    assert result.total_diagnostics == 3
    assert result.next_offset == 2
