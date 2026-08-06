"""mcp_server.py のテスト。

`PYFLTR_CACHE_DIR` を `tmp_path` に固定することで、テストデータ生成に使う
`ArchiveStore(cache_root=tmp_path)` と MCP ツール内部で呼ぶ `ArchiveStore()`
（`default_cache_root()` 解決）が同一キャッシュを参照する。
"""

import argparse
import dataclasses
import importlib.metadata
import inspect
import json
import pathlib
import shutil
import typing

import pytest

import pyfltr.cli.mcp_models
import pyfltr.cli.mcp_server
import pyfltr.command.slow_tests
import pyfltr.state.archive
import pyfltr.state.runs
from tests import conftest as _testconf
from tests.conftest import make_command_result as _make_result
from tests.conftest import make_error_location as _make_error
from tests.conftest import seed_archive_run as _seed_run


@pytest.fixture(autouse=True)
def _isolated_cache(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: pathlib.Path,
) -> pathlib.Path:
    """`PYFLTR_CACHE_DIR`を`tmp_path`に固定するフィクスチャ。"""
    monkeypatch.setenv("PYFLTR_CACHE_DIR", str(tmp_path))
    return tmp_path


# ---------------------------------------------------------------------------
# Pydantic モデルのテスト
# ---------------------------------------------------------------------------


def test_run_summary_model_fields() -> None:
    model = pyfltr.cli.mcp_models.RunSummaryModel(
        run_id="abc123",
        started_at="2026-01-01T00:00:00",
        finished_at="2026-01-01T00:01:00",
        exit_code=0,
        commands=["ruff-check"],
        files=5,
    )
    assert model.run_id == "abc123"
    assert model.exit_code == 0
    assert model.commands == ["ruff-check"]
    assert model.files == 5


def test_diagnostic_model_all_optional() -> None:
    # 全フィールド省略可能であることを確認する
    model = pyfltr.cli.mcp_models.DiagnosticModel()
    assert model.command is None
    assert model.file is None
    assert not model.messages


def test_diagnostic_message_model_all_optional() -> None:
    # DiagnosticMessageModelも全フィールド省略可能
    model = pyfltr.cli.mcp_models.DiagnosticMessageModel()
    assert model.line is None
    assert model.severity is None
    assert model.msg is None


# ---------------------------------------------------------------------------
# 読み取り系ツールのテスト
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_tool_list_runs_empty() -> None:
    result = await pyfltr.cli.mcp_server.tool_list_runs()
    assert result == []


@pytest.mark.asyncio
async def test_tool_list_runs_returns_summaries(tmp_path: pathlib.Path) -> None:
    run_id1 = _seed_run(tmp_path, commands=["ruff-check"], exit_code=0)
    run_id2 = _seed_run(tmp_path, commands=["mypy"], exit_code=1)

    result = await pyfltr.cli.mcp_server.tool_list_runs(limit=10)
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

    result = await pyfltr.cli.mcp_server.tool_list_runs(limit=2)
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

    result = await pyfltr.cli.mcp_server.tool_show_run(run_id)
    assert result.run_id == run_id
    assert "run_id" in result.meta
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

    overview = await pyfltr.cli.mcp_server.tool_show_run(run_id)
    assert len(overview.commands) == 1
    command = overview.commands[0]
    assert command.elapsed == 2.346
    assert [test.model_dump() for test in command.slow_tests] == [test.to_dict() for test in slow_tests]


def test_collect_tool_summaries_includes_elapsed(tmp_path: pathlib.Path) -> None:
    """collect_tool_summariesがtool.jsonのelapsedを読み取る。"""
    store = pyfltr.state.archive.ArchiveStore(cache_root=tmp_path)
    run_id = store.start_run(commands=["pytest"])
    result = dataclasses.replace(_make_result("pytest", returncode=0, command_type="tester"), elapsed=1.2345)
    store.write_tool_result(run_id, result)
    summaries = pyfltr.state.runs.collect_tool_summaries(store, run_id)
    assert summaries[0]["elapsed"] == 1.234


def test_collect_tool_summaries_omits_slow_tests_when_absent(tmp_path: pathlib.Path) -> None:
    """slow_testsを持たないtool.jsonでは当該キーを付けない。"""
    store = pyfltr.state.archive.ArchiveStore(cache_root=tmp_path)
    run_id = store.start_run(commands=["pytest"])
    store.write_tool_result(run_id, _make_result("pytest", returncode=0, command_type="tester"))
    summaries = pyfltr.state.runs.collect_tool_summaries(store, run_id)
    assert "slow_tests" not in summaries[0]


@pytest.mark.asyncio
async def test_tool_show_run_latest(tmp_path: pathlib.Path) -> None:
    _seed_run(tmp_path, commands=["ruff-check"])
    latest_id = _seed_run(tmp_path, commands=["mypy"])

    result = await pyfltr.cli.mcp_server.tool_show_run("latest")
    assert result.run_id == latest_id


@pytest.mark.asyncio
async def test_tool_show_run_prefix(tmp_path: pathlib.Path) -> None:
    run_id = _seed_run(tmp_path)
    result = await pyfltr.cli.mcp_server.tool_show_run(run_id[:8])
    assert result.run_id == run_id


@pytest.mark.asyncio
async def test_tool_show_run_not_found() -> None:
    with pytest.raises(ValueError, match="run_id"):
        await pyfltr.cli.mcp_server.tool_show_run("nonexistent")


@pytest.mark.asyncio
async def test_tool_show_run_latest_empty() -> None:
    with pytest.raises(ValueError, match="run"):
        await pyfltr.cli.mcp_server.tool_show_run("latest")


@pytest.mark.asyncio
async def test_tool_show_run_ambiguous_prefix(tmp_path: pathlib.Path) -> None:
    run_ids = [_seed_run(tmp_path) for _ in range(2)]
    # ULIDの先頭は同じタイムスタンプ部分（ミリ秒単位）を共有する可能性が高いため、
    # 実際に共通する最長プレフィックスを算出してテストする。
    shared = _testconf.shared_prefix_length(run_ids[0], run_ids[1])
    if shared < 1:
        pytest.skip("shared prefixが無いケースでは曖昧判定にならない")
    prefix = run_ids[0][:shared]

    with pytest.raises(ValueError, match="曖昧"):
        await pyfltr.cli.mcp_server.tool_show_run(prefix)


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

    results = await pyfltr.cli.mcp_server.tool_show_run_diagnostics(run_id, ["mypy"])
    assert len(results) == 1
    result = results[0]
    assert result.command_meta["command"] == "mypy"
    assert len(result.diagnostics) == 1
    diagnostic = result.diagnostics[0]
    assert diagnostic.file == "src/a.py"
    assert len(diagnostic.messages) == 1
    message = diagnostic.messages[0]
    assert message.line == 42
    assert message.col == 5
    assert message.msg == "型エラー"


@pytest.mark.asyncio
async def test_tool_show_run_diagnostics_restores_hints(tmp_path: pathlib.Path) -> None:
    """tool.jsonにhintsが含まれる場合、`show_run_diagnostics`の戻り値に復元される。"""
    # hintを持つErrorLocationでアーカイブを作成する
    error = _make_error("textlint", "a.md", 1, "長い文", col=1)
    error.rule = "ja-technical-writing/sentence-length"
    error.hint = "句点で文を区切る"
    store = pyfltr.state.archive.ArchiveStore(cache_root=tmp_path)
    run_id = store.start_run(commands=["textlint"])
    result = _make_result("textlint", returncode=1, errors=[error])
    store.write_tool_result(run_id, result)
    store.finalize_run(run_id, exit_code=1)

    # hintsがtool.jsonに保存されているか確認する
    tool_json_path = tmp_path / "runs" / run_id / "tools" / "textlint" / "tool.json"
    tool_meta = json.loads(tool_json_path.read_text(encoding="utf-8"))
    assert "hints" in tool_meta
    assert "ja-technical-writing/sentence-length" in tool_meta["hints"]

    # show_run_diagnosticsでhintsが復元されることを確認する
    results = await pyfltr.cli.mcp_server.tool_show_run_diagnostics(run_id, ["textlint"])
    assert len(results) == 1
    assert results[0].hints is not None
    assert "ja-technical-writing/sentence-length" in results[0].hints


@pytest.mark.asyncio
async def test_tool_show_run_diagnostics_hints_none_when_absent(tmp_path: pathlib.Path) -> None:
    """tool.jsonにhintsキーが無い場合、`show_run_diagnostics`の`hints`はNoneになる。"""
    run_id = _seed_run(
        tmp_path,
        tool_results=[
            ("mypy", 0, "clean", []),
        ],
    )

    results = await pyfltr.cli.mcp_server.tool_show_run_diagnostics(run_id, ["mypy"])
    assert len(results) == 1
    assert results[0].hints is None


@pytest.mark.asyncio
async def test_tool_show_run_diagnostics_tool_not_found(tmp_path: pathlib.Path) -> None:
    run_id = _seed_run(tmp_path)
    with pytest.raises(ValueError, match="nonexistent"):
        await pyfltr.cli.mcp_server.tool_show_run_diagnostics(run_id, ["nonexistent"])


@pytest.mark.asyncio
async def test_tool_show_run_output(tmp_path: pathlib.Path) -> None:
    run_id = _seed_run(
        tmp_path,
        tool_results=[
            ("ruff-check", 0, "raw output line 1\nraw output line 2\n", []),
        ],
    )

    result = await pyfltr.cli.mcp_server.tool_show_run_output(run_id, ["ruff-check"])
    assert "ruff-check" in result
    assert "raw output line 1" in result["ruff-check"]
    assert "raw output line 2" in result["ruff-check"]


@pytest.mark.asyncio
async def test_tool_show_run_output_tool_not_found(tmp_path: pathlib.Path) -> None:
    run_id = _seed_run(tmp_path)
    with pytest.raises(ValueError, match="nonexistent"):
        await pyfltr.cli.mcp_server.tool_show_run_output(run_id, ["nonexistent"])


# ---------------------------------------------------------------------------
# MCPServerのサーバー登録確認
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_build_server_registers_eleven_tools() -> None:
    server = pyfltr.cli.mcp_server.build_server()
    tools = await server.list_tools()
    tool_names = {t.name for t in tools}
    expected = {
        "list_runs",
        "show_run",
        "show_run_diagnostics",
        "show_run_output",
        "run_for_agent",
        "grep",
        "replace",
        "replace_undo",
        "replace_history",
        "command_info",
        "config",
    }
    assert tool_names == expected


def test_build_server_reports_name_and_version() -> None:
    """初期化応答へ返すサーバー名と版が欠落しないことを検証する。

    `version`はMCPプロトコルの必須フィールドだが、SDKのコンストラクタは既定値を
    空文字列としており、指定を忘れても起動もツール呼び出しも成立する。
    配布パッケージの版と一致することを確かめ、既定値のまま公開される状態を検出する。
    """
    server = pyfltr.cli.mcp_server.build_server()
    assert server.name == "pyfltr"
    assert server.version == importlib.metadata.version("pyfltr")


def test_execute_mcp_reports_missing_dependency(monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture) -> None:
    """MCPサーバー機能の依存が解決できない場合は終了コード1と説明を返す。"""
    monkeypatch.setattr(pyfltr.cli.mcp_server, "_mcpserver", None)
    monkeypatch.setattr(pyfltr.cli.mcp_server, "_MCP_IMPORT_ERROR", ImportError("missing API"))
    with caplog.at_level("ERROR", logger="pyfltr.cli.mcp_server"):
        result = pyfltr.cli.mcp_server.execute_mcp(argparse.Namespace())
    assert result == 1
    assert "必要な依存が解決されていません" in caplog.text


# ---------------------------------------------------------------------------
# 実行系ツールのテスト
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_tool_run_for_agent_rejects_invalid_mode() -> None:
    """未対応の実行モードを拒否する。"""
    with pytest.raises(ValueError, match="mode"):
        await pyfltr.cli.mcp_server.tool_run_for_agent(paths=["dummy"], mode="invalid")


@pytest.mark.asyncio
async def test_tool_run_for_agent_rejects_missing_work_dir(tmp_path: pathlib.Path) -> None:
    """実在しない作業ディレクトリを拒否する。"""
    with pytest.raises(ValueError, match="work_dir"):
        await pyfltr.cli.mcp_server.tool_run_for_agent(
            paths=["dummy"],
            work_dir=str(tmp_path / "missing"),
        )


@pytest.mark.asyncio
async def test_tool_run_for_agent_resolves_cli_parameters(tmp_path: pathlib.Path, mocker) -> None:
    """実行モード、対象パス、設定上書き、再実行引数をCLIと同じ形へ解決する。"""
    target = tmp_path / "src" / "sample.py"
    target.parent.mkdir()
    target.write_text("value = 1\n", encoding="utf-8")
    mock_run = mocker.patch("pyfltr.cli.pipeline.run_pipeline", return_value=(0, None))

    await pyfltr.cli.mcp_server.tool_run_for_agent(
        paths=["src/sample.py"],
        mode="ci",
        commands=["mypy"],
        enable=["mypy"],
        disable=["ec"],
        no_fix=True,
        fail_fast=True,
        only_failed=True,
        changed_since="HEAD",
        work_dir=str(tmp_path),
        shuffle=True,
        exit_zero_even_if_formatted=True,
        jobs=2,
    )

    args, commands, config = mock_run.call_args.args
    kwargs = mock_run.call_args.kwargs
    assert args.include_fix_stage is False
    assert args.shuffle is True
    assert args.exit_zero_even_if_formatted is True
    assert args.targets == [target]
    assert commands == ["mypy"]
    assert config.values["mypy"] is True
    assert config.values["ec"] is False
    assert kwargs["start_cwd"] == tmp_path.resolve()
    assert kwargs["original_cwd"] == str(tmp_path.resolve())
    assert kwargs["original_sys_args"] == [
        "ci",
        f"--work-dir={tmp_path.resolve()}",
        "--no-fix",
        "--commands=mypy",
        "--enable=mypy",
        "--disable=ec",
        "--shuffle",
        "--exit-zero-even-if-formatted",
        "--jobs=2",
    ]
    assert "--only-failed" not in kwargs["original_sys_args"]
    assert "--changed-since=HEAD" not in kwargs["original_sys_args"]
    assert "--fail-fast" not in kwargs["original_sys_args"]


@pytest.mark.asyncio
async def test_tool_run_for_agent_flattens_commands(tmp_path: pathlib.Path, mocker) -> None:
    """複数回指定とカンマ区切りを同じコマンド一覧へ展開する。"""
    target = tmp_path / "sample.txt"
    target.write_text("hello\n", encoding="utf-8")
    mock_run = mocker.patch("pyfltr.cli.pipeline.run_pipeline", return_value=(0, None))

    await pyfltr.cli.mcp_server.tool_run_for_agent(
        paths=[str(target)],
        commands=["ec,typos", "ec"],
    )

    assert mock_run.call_args.args[1] == ["ec", "typos"]


@pytest.mark.asyncio
async def test_tool_run_for_agent_no_fix_disables_run_fix_stage(tmp_path: pathlib.Path, mocker) -> None:
    """runモードでもno_fix指定時はfixステージを無効化する。"""
    target = tmp_path / "sample.txt"
    target.write_text("hello\n", encoding="utf-8")
    mock_run = mocker.patch("pyfltr.cli.pipeline.run_pipeline", return_value=(0, None))

    await pyfltr.cli.mcp_server.tool_run_for_agent(
        paths=[str(target)],
        mode="run",
        commands=["ec"],
        no_fix=True,
    )

    assert mock_run.call_args.args[0].include_fix_stage is False


@pytest.mark.asyncio
async def test_tool_run_for_agent_rejects_unknown_command(tmp_path: pathlib.Path) -> None:
    """未知コマンドを共通の検証経路で拒否する。"""
    target = tmp_path / "sample.txt"
    target.write_text("hello\n", encoding="utf-8")

    with pytest.raises(ValueError, match="コマンドが見つかりません"):
        await pyfltr.cli.mcp_server.tool_run_for_agent(paths=[str(target)], commands=["unknown-command"])


@pytest.mark.asyncio
async def test_tool_run_for_agent_changed_since_uses_work_dir(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
    mocker,
) -> None:
    """差分抽出用のgit実行ディレクトリへ作業ディレクトリを渡す。"""
    work_dir = tmp_path / "project"
    work_dir.mkdir()
    target = work_dir / "sample.txt"
    target.write_text("hello\n", encoding="utf-8")
    server_cwd = tmp_path / "server"
    server_cwd.mkdir()
    monkeypatch.chdir(server_cwd)
    filter_changed = mocker.patch(
        "pyfltr.command.targets.filter_by_changed_since",
        return_value=[target],
    )
    mocker.patch("pyfltr.cli.pipeline.run_commands_with_cli", return_value=[])

    await pyfltr.cli.mcp_server.tool_run_for_agent(
        paths=["sample.txt"],
        commands=["ec"],
        changed_since="HEAD",
        work_dir=str(work_dir),
    )

    assert filter_changed.call_args.kwargs["cwd"] == work_dir.resolve()


@pytest.mark.asyncio
async def test_tool_run_for_agent_scans_and_deduplicates_from_work_dir(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
    mocker,
) -> None:
    """サーバーcwdと異なる作業ディレクトリを基準に走査し、同一実体を重複排除する。"""
    work_dir = tmp_path / "project"
    source_dir = work_dir / "src"
    source_dir.mkdir(parents=True)
    included = source_dir / "included.txt"
    included.write_text("included\n", encoding="utf-8")
    excluded = source_dir / "excluded.txt"
    excluded.write_text("excluded\n", encoding="utf-8")
    (work_dir / "src_alias").symlink_to("src", target_is_directory=True)
    (work_dir / "pyproject.toml").write_text(
        '[tool.pyfltr]\nec = true\nextend-exclude = ["*/excluded.txt"]\n',
        encoding="utf-8",
    )
    server_cwd = tmp_path / "server"
    server_cwd.mkdir()
    monkeypatch.chdir(server_cwd)
    scanned: list[pathlib.Path] = []

    def capture_files(
        _commands: list[str],
        _args: argparse.Namespace,
        base_ctx: typing.Any,
        **_kwargs: typing.Any,
    ) -> list[typing.Any]:
        scanned.extend(base_ctx.all_files)
        return []

    mocker.patch("pyfltr.cli.pipeline.run_commands_with_cli", side_effect=capture_files)

    await pyfltr.cli.mcp_server.tool_run_for_agent(
        paths=["src", "src_alias"],
        commands=["ec"],
        work_dir=str(work_dir),
        no_gitignore=True,
    )

    assert scanned == [pathlib.Path("src/included.txt")]


@pytest.mark.asyncio
@pytest.mark.skipif(not shutil.which("typos"), reason="typos コマンドが環境にない")
async def test_tool_run_for_agent_with_typos(tmp_path: pathlib.Path) -> None:
    sample = tmp_path / "sample.txt"
    sample.write_text("hello world\n", encoding="utf-8")

    result = await pyfltr.cli.mcp_server.tool_run_for_agent(
        paths=[str(sample)],
        commands=["typos"],
    )

    assert result.run_id is not None
    assert len(result.run_id) > 0
    assert isinstance(result.exit_code, int)
    assert isinstance(result.failed, list)
    assert isinstance(result.commands, list)


@pytest.mark.asyncio
async def test_tool_run_for_agent_keeps_stdout_clean_and_text_on_stderr(
    tmp_path: pathlib.Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """`run_for_agent`実行中はstdoutがJSON-RPC用に空のまま、text整形出力はstderrに出力される。

    `force_text_on_stderr=True`がrun_pipeline側で有効化されてtext_loggerがstderrに向き、
    構造化出力は一時ファイルへ退避するためstdoutへは何も書かれない契約を固定する。
    """
    sample = tmp_path / "input.txt"
    sample.write_text("hello\n", encoding="utf-8")

    # typos が利用可能なら 1 件だけ実行する。未導入環境でも ec で確実に通す。
    await pyfltr.cli.mcp_server.tool_run_for_agent(paths=[str(sample)], commands=["ec"])

    captured = capsys.readouterr()
    assert captured.out == "", f"stdout に漏れている: {captured.out!r}"
    # stderr には text_logger 由来の区切り線などが出る（実行アーカイブ有効化なので run_id 行も）
    assert "----- pyfltr" in captured.err


@pytest.mark.asyncio
async def test_tool_run_for_agent_returns_run_id(tmp_path: pathlib.Path) -> None:
    """`run_for_agent`がrun_idを含む結果を返すことを確認する。

    `commands=None`でプロジェクト設定のコマンドを使用し、アーカイブに記録されることを検証する。
    実際のツール実行を避けるため`commands=[]`に近いケースとして`typos`を条件付きで使用するか、
    ここでは`typos`が利用可能なら1件実行する形で確認する。
    """
    # 最小限の入力ファイルを用意する
    sample = tmp_path / "input.txt"
    sample.write_text("This is a simple test file.\n", encoding="utf-8")

    # typosが利用できない環境でも動作させるため、利用可能なコマンドを選ぶ。
    # ecは設定不要で動作するため使用する。
    result = await pyfltr.cli.mcp_server.tool_run_for_agent(
        paths=[str(sample)],
        commands=["ec"],
    )

    assert result.run_id is not None
    assert len(result.run_id) == 26  # ULIDは26文字
    assert isinstance(result.exit_code, int)

    # アーカイブに保存されていることを確認する
    store = pyfltr.state.archive.ArchiveStore()
    summaries = store.list_runs(limit=1)
    assert len(summaries) == 1
    assert summaries[0].run_id == result.run_id


# ---------------------------------------------------------------------------
# RunForAgentResult モデルの新フィールドのテスト
# ---------------------------------------------------------------------------


def test_run_for_agent_result_new_fields_defaults() -> None:
    """RunForAgentResultの新フィールドのデフォルト値を確認する。"""
    result = pyfltr.cli.mcp_models.RunForAgentResult(
        run_id="01TESTULID1234567890123456",
        exit_code=0,
        failed=[],
    )
    assert result.run_id is not None
    assert result.skipped_reason is None
    assert isinstance(result.retry_commands, dict)
    # schema_hintsは廃止済みのため存在しない
    assert not hasattr(result, "schema_hints")


def test_run_for_agent_result_nullable_run_id() -> None:
    """RunForAgentResultのrun_idがNoneを許容する（early exit時）。"""
    result = pyfltr.cli.mcp_models.RunForAgentResult(
        run_id=None,
        exit_code=0,
        failed=[],
        skipped_reason="失敗ツールなし",
    )
    assert result.run_id is None
    assert result.skipped_reason == "失敗ツールなし"
    assert result.exit_code == 0
    assert not result.failed
    assert not result.commands


@pytest.mark.asyncio
async def test_tool_run_for_agent_returns_retry_commands(tmp_path: pathlib.Path) -> None:
    """run_for_agentの戻り値にretry_commandsが含まれることを確認する（失敗なしの場合は空辞書）。"""
    sample = tmp_path / "input.txt"
    sample.write_text("hello\n", encoding="utf-8")

    result = await pyfltr.cli.mcp_server.tool_run_for_agent(
        paths=[str(sample)],
        commands=["ec"],
    )

    assert isinstance(result.retry_commands, dict)
    # 成功したコマンドはキーに含まれない
    for key in result.retry_commands:
        assert key in result.failed


@pytest.mark.asyncio
async def test_tool_run_for_agent_retry_command_uses_cli_arguments(tmp_path: pathlib.Path, mocker) -> None:
    """再実行コマンドをMCPサーバー起動引数ではなくCLI引数から生成する。"""
    sample = tmp_path / "failure.txt"
    sample.write_text("failure\n", encoding="utf-8")
    failed = _make_result(
        "ec",
        returncode=1,
        has_error=True,
        archived=False,
        target_files=[sample],
    )

    def fake_run_commands(*_args: typing.Any, **kwargs: typing.Any) -> list[typing.Any]:
        kwargs["archive_hook"](failed)
        kwargs["on_result"](failed)
        return [failed]

    mocker.patch("pyfltr.cli.pipeline.run_commands_with_cli", side_effect=fake_run_commands)

    result = await pyfltr.cli.mcp_server.tool_run_for_agent(
        paths=[str(sample)],
        mode="ci",
        commands=["ec"],
        no_fix=True,
    )

    assert result.failed == ["ec"]
    retry_command = result.retry_commands["ec"]
    assert " ci " in retry_command
    assert "--commands=ec" in retry_command
    assert "--no-fix" in retry_command
    assert " mcp " not in retry_command


@pytest.mark.asyncio
async def test_tool_run_for_agent_retry_commands_includes_failed(tmp_path: pathlib.Path) -> None:
    """失敗コマンドが存在する場合にretry_commandsにキーが入る経路をカバーする。

    ruff-checkでエラーのあるPythonファイルを実行し、失敗した場合に
    retry_commands["ruff-check"]が設定されることを確認する。
    retry_commandはアーカイブのtool.jsonから読み取るため、
    archive.write_tool_resultがretry_commandを保存していることも兼ねて検証する。
    """
    # ruff-check でエラーになる Python ファイルを用意する
    bad_py = tmp_path / "bad.py"
    bad_py.write_text("import os\n", encoding="utf-8")  # F401: imported but unused

    result = await pyfltr.cli.mcp_server.tool_run_for_agent(
        paths=[str(bad_py)],
        commands=["ruff-check"],
    )

    assert isinstance(result.retry_commands, dict)
    if "ruff-check" in result.failed:
        # 失敗コマンドにはretry_commandsキーが含まれる
        assert "ruff-check" in result.retry_commands
        assert isinstance(result.retry_commands["ruff-check"], str)
        assert len(result.retry_commands["ruff-check"]) > 0
    # 成功したコマンドはキーに含まれない
    for key in result.retry_commands:
        assert key in result.failed


@pytest.mark.asyncio
async def test_tool_run_for_agent_from_run_without_only_failed_raises() -> None:
    """only_failed=Falseのままfrom_runを指定するとValueErrorが発生する。"""
    with pytest.raises(ValueError, match="only_failed"):
        await pyfltr.cli.mcp_server.tool_run_for_agent(
            paths=["dummy"],
            from_run="latest",
        )


@pytest.mark.asyncio
async def test_tool_run_for_agent_only_failed_no_previous_run(tmp_path: pathlib.Path) -> None:
    """only_failed=Trueで直前runがない場合はearly exit（run_id=None・skipped_reasonあり）。"""
    sample = tmp_path / "input.txt"
    sample.write_text("hello\n", encoding="utf-8")

    result = await pyfltr.cli.mcp_server.tool_run_for_agent(
        paths=[str(sample)],
        commands=["ec"],
        only_failed=True,
    )

    # 直前runなしなのでearly exit
    assert result.run_id is None
    assert result.exit_code == 0
    assert not result.failed
    assert not result.commands
    assert result.skipped_reason is not None
    assert len(result.skipped_reason) > 0


# ---------------------------------------------------------------------------
# grep ツールのテスト
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_tool_grep_finds_matches(tmp_path: pathlib.Path) -> None:
    """`tool_grep`が指定ファイル群から正しくマッチを抽出すること。"""
    target = tmp_path / "sample.txt"
    target.write_text("hello world\nfoo bar\nhello again\n", encoding="utf-8")

    result = await pyfltr.cli.mcp_server.tool_grep(
        pattern="hello",
        paths=[str(target)],
    )

    assert result.total_matches == 2
    assert result.files_scanned == 1
    assert result.exit_code == 0
    assert len(result.matches) == 2
    for match in result.matches:
        assert match.file == target.as_posix()
        assert "hello" in match.line_text


@pytest.mark.asyncio
async def test_tool_grep_combines_multiple_patterns(tmp_path: pathlib.Path) -> None:
    """複数パターンをOR条件として検索する。"""
    target = tmp_path / "sample.txt"
    target.write_text("alpha\nbeta\ngamma\n", encoding="utf-8")

    result = await pyfltr.cli.mcp_server.tool_grep(
        paths=[str(target)],
        patterns=["alpha", "beta"],
    )

    assert result.total_matches == 2
    assert {match.match_text for match in result.matches} == {"alpha", "beta"}


@pytest.mark.asyncio
async def test_tool_grep_accepts_empty_pattern(tmp_path: pathlib.Path) -> None:
    """空文字列を有効な正規表現として検索する。"""
    target = tmp_path / "sample.txt"
    target.write_text("alpha\n", encoding="utf-8")

    result = await pyfltr.cli.mcp_server.tool_grep(paths=[str(target)], pattern="")

    assert result.total_matches > 0
    assert result.matches[0].match_text == ""


@pytest.mark.asyncio
async def test_tool_grep_reads_pattern_file(tmp_path: pathlib.Path) -> None:
    """パターンファイルの各行を検索パターンとして使用する。"""
    target = tmp_path / "sample.txt"
    target.write_text("alpha\nbeta\ngamma\n", encoding="utf-8")
    pattern_file = tmp_path / "patterns.txt"
    pattern_file.write_text("alpha\ngamma\n", encoding="utf-8")

    result = await pyfltr.cli.mcp_server.tool_grep(
        paths=[str(target)],
        pattern_file=str(pattern_file),
    )

    assert result.total_matches == 2
    assert {match.match_text for match in result.matches} == {"alpha", "gamma"}


@pytest.mark.asyncio
async def test_tool_grep_rejects_missing_pattern_file(tmp_path: pathlib.Path) -> None:
    """実在しないパターンファイルを拒否する。"""
    with pytest.raises(ValueError, match="パターンファイル"):
        await pyfltr.cli.mcp_server.tool_grep(
            paths=[str(tmp_path)],
            pattern_file=str(tmp_path / "missing.txt"),
        )


def test_tool_grep_max_total_default_is_none() -> None:
    """全体上限の未指定状態を明示値と区別する。"""
    signature = inspect.signature(pyfltr.cli.mcp_server.tool_grep)
    assert signature.parameters["max_total"].default is None


@pytest.mark.asyncio
async def test_tool_grep_context_applies_to_both_directions(tmp_path: pathlib.Path) -> None:
    """一括コンテキスト値をマッチ行の前後へ適用する。"""
    target = tmp_path / "sample.txt"
    target.write_text("before 2\nbefore 1\nmatch\nafter 1\nafter 2\n", encoding="utf-8")

    result = await pyfltr.cli.mcp_server.tool_grep(
        paths=[str(target)],
        pattern="match",
        context=2,
    )

    assert result.matches[0].before == ["before 2", "before 1"]
    assert result.matches[0].after == ["after 1", "after 2"]


@pytest.mark.asyncio
async def test_tool_grep_context_merges_with_individual_values(tmp_path: pathlib.Path) -> None:
    """一括コンテキスト値を未指定の方向だけへ適用する。"""
    target = tmp_path / "sample.txt"
    target.write_text("before 2\nbefore 1\nmatch\nafter 1\nafter 2\n", encoding="utf-8")

    result = await pyfltr.cli.mcp_server.tool_grep(
        paths=[str(target)],
        pattern="match",
        context=2,
        before_context=1,
    )

    assert result.matches[0].before == ["before 1"]
    assert result.matches[0].after == ["after 1", "after 2"]


@pytest.mark.asyncio
async def test_tool_grep_no_match_returns_exit_code_1(tmp_path: pathlib.Path) -> None:
    """`tool_grep`がマッチ0件のとき`exit_code=1`を返すこと。"""
    target = tmp_path / "sample.txt"
    target.write_text("hello world\n", encoding="utf-8")

    result = await pyfltr.cli.mcp_server.tool_grep(
        pattern="notfound",
        paths=[str(target)],
    )

    assert result.total_matches == 0
    assert result.exit_code == 1
    assert not result.matches


@pytest.mark.asyncio
async def test_tool_grep_respects_exclude(tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """`tool_grep`が`exclude`設定を尊重し、除外ディレクトリ配下のファイルを結果に含めないこと。"""
    # 除外対象ディレクトリとそれ以外を作成する
    excluded_dir = tmp_path / "node_modules"
    excluded_dir.mkdir()
    excluded_file = excluded_dir / "lib.js"
    excluded_file.write_text("hello from excluded\n", encoding="utf-8")

    included_file = tmp_path / "main.py"
    included_file.write_text("hello from included\n", encoding="utf-8")

    # pyproject.toml を作成して node_modules を exclude 設定に追加する
    pyproject = tmp_path / "pyproject.toml"
    pyproject.write_text('[tool.pyfltr]\nexclude = ["node_modules"]\n', encoding="utf-8")
    monkeypatch.chdir(tmp_path)

    result = await pyfltr.cli.mcp_server.tool_grep(
        pattern="hello",
        paths=[str(tmp_path)],
    )

    # expand_all_filesが返すパスは相対または絶対になるため、resolve後のベース名で比較する
    matched_resolved = {pathlib.Path(m.file).resolve() for m in result.matches}
    assert excluded_file.resolve() not in matched_resolved
    assert included_file.resolve() in matched_resolved


@pytest.mark.asyncio
async def test_tool_grep_max_total_limits_results(tmp_path: pathlib.Path) -> None:
    """`max_total`が有効に機能しマッチ件数が上限で打ち切られること。"""
    target = tmp_path / "sample.txt"
    target.write_text("\n".join(f"hello {i}" for i in range(20)) + "\n", encoding="utf-8")

    result = await pyfltr.cli.mcp_server.tool_grep(
        pattern="hello",
        paths=[str(target)],
        max_total=5,
    )

    assert result.total_matches <= 5
    assert len(result.matches) <= 5


@pytest.mark.asyncio
async def test_tool_grep_summary_files_with_matches(tmp_path: pathlib.Path) -> None:
    """マッチを含むファイルだけを集計して個別マッチを省略する。"""
    matched = tmp_path / "matched.txt"
    matched.write_text("hello\n", encoding="utf-8")
    unmatched = tmp_path / "unmatched.txt"
    unmatched.write_text("goodbye\n", encoding="utf-8")

    result = await pyfltr.cli.mcp_server.tool_grep(
        paths=[str(tmp_path)],
        pattern="hello",
        summary_mode="files_with_matches",
    )

    assert result.summary_mode == "files_with_matches"
    assert result.files_with_matches == [matched.as_posix()]
    assert not result.matches


@pytest.mark.asyncio
async def test_tool_grep_summary_count_is_unlimited_by_default(tmp_path: pathlib.Path) -> None:
    """件数集計では未指定の全体上限を無制限として扱う。"""
    target = tmp_path / "matched.txt"
    target.write_text("hello\n" * 1001, encoding="utf-8")

    result = await pyfltr.cli.mcp_server.tool_grep(
        paths=[str(target)],
        pattern="hello",
        summary_mode="count",
    )

    assert result.total_matches == 1001
    assert [(entry.file, entry.count) for entry in result.file_counts] == [(target.as_posix(), 1001)]
    assert not result.matches


@pytest.mark.asyncio
async def test_tool_grep_summary_files_without_match(tmp_path: pathlib.Path) -> None:
    """走査対象のうちマッチを含まないファイルだけを集計する。"""
    matched = tmp_path / "matched.txt"
    matched.write_text("hello\n", encoding="utf-8")
    unmatched = tmp_path / "unmatched.txt"
    unmatched.write_text("goodbye\n", encoding="utf-8")

    result = await pyfltr.cli.mcp_server.tool_grep(
        paths=[str(tmp_path)],
        pattern="hello",
        summary_mode="files_without_match",
    )

    assert result.files_without_match == [unmatched.as_posix()]
    assert not result.matches


@pytest.mark.asyncio
async def test_tool_grep_rejects_limited_files_without_match_summary(tmp_path: pathlib.Path) -> None:
    """未走査ファイルを不一致扱いしないよう正の全体上限との併用を拒否する。"""
    target = tmp_path / "sample.txt"
    target.write_text("hello\n", encoding="utf-8")

    with pytest.raises(ValueError, match="max_total"):
        await pyfltr.cli.mcp_server.tool_grep(
            paths=[str(target)],
            pattern="hello",
            summary_mode="files_without_match",
            max_total=1,
        )


@pytest.mark.asyncio
async def test_tool_grep_rejects_invalid_summary_mode(tmp_path: pathlib.Path) -> None:
    """未対応の集計モードを拒否する。"""
    with pytest.raises(ValueError, match="summary_mode"):
        await pyfltr.cli.mcp_server.tool_grep(
            paths=[str(tmp_path)],
            pattern="hello",
            summary_mode="invalid",
        )


@pytest.mark.asyncio
async def test_tool_grep_includes_hidden_files(tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """`tool_grep`がドット始まりファイルも対象に含めること（run系と統一）。"""
    hidden = tmp_path / ".hidden.py"
    hidden.write_text("foo here\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    result = await pyfltr.cli.mcp_server.tool_grep(pattern="foo", paths=[str(tmp_path)])
    assert result.total_matches == 1
    assert any(pathlib.Path(m.file).name == ".hidden.py" for m in result.matches)


@pytest.mark.asyncio
async def test_tool_grep_reports_excluded_and_clears_between_requests(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """除外ファイルをfully_excluded_filesで通知し、連続リクエストで前回分が混入しないこと。"""
    lock = tmp_path / "uv.lock"
    lock.write_text("foo\n", encoding="utf-8")
    normal = tmp_path / "a.py"
    normal.write_text("foo\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    first = await pyfltr.cli.mcp_server.tool_grep(pattern="foo", paths=[str(lock)])
    assert first.fully_excluded_files == ["uv.lock"]
    second = await pyfltr.cli.mcp_server.tool_grep(pattern="foo", paths=[str(normal)])
    assert not second.fully_excluded_files
    # 不在パスはmissing_targetsで通知し、前リクエストの除外は混入しない
    third = await pyfltr.cli.mcp_server.tool_grep(pattern="foo", paths=[str(tmp_path / "nope.py")])
    assert third.missing_targets == ["nope.py"]
    assert not third.fully_excluded_files


# ---------------------------------------------------------------------------
# replace ツールのテスト
# ---------------------------------------------------------------------------


def test_tool_replace_dry_run_default() -> None:
    """`tool_replace`の`dry_run`引数の既定値が`True`であること。"""
    sig = inspect.signature(pyfltr.cli.mcp_server.tool_replace)
    assert sig.parameters["dry_run"].default is True


@pytest.mark.asyncio
async def test_tool_replace_reports_filtered_and_clears_between_requests(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`tool_replace`が除外・不在を通知し、連続リクエストで前回分が混入しないこと。"""
    lock = tmp_path / "uv.lock"
    lock.write_text("foo\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    # exclude該当ファイルを明示 → fully_excluded_filesに載り、書き換えは発生しない
    excluded = await pyfltr.cli.mcp_server.tool_replace(pattern="foo", replacement="baz", paths=[str(lock)])
    assert excluded.fully_excluded_files == ["uv.lock"]
    assert excluded.files_changed == 0
    assert lock.read_text(encoding="utf-8") == "foo\n"
    # 不在パスはmissing_targetsで通知し、前リクエストの除外は混入しない
    missing = await pyfltr.cli.mcp_server.tool_replace(pattern="foo", replacement="baz", paths=[str(tmp_path / "nope.py")])
    assert missing.missing_targets == ["nope.py"]
    assert not missing.fully_excluded_files


@pytest.mark.asyncio
async def test_tool_replace_dry_run_does_not_write(tmp_path: pathlib.Path) -> None:
    """`tool_replace(dry_run=True)`がファイルを変更しないこと。"""
    target = tmp_path / "sample.txt"
    original = "hello world\n"
    target.write_text(original, encoding="utf-8")

    result = await pyfltr.cli.mcp_server.tool_replace(
        pattern="hello",
        replacement="goodbye",
        paths=[str(target)],
        dry_run=True,
    )

    # ファイルは変更されていない
    assert target.read_text(encoding="utf-8") == original
    # dry_run=TrueなのでreplacE_idはNone
    assert result.replace_id is None
    assert result.dry_run is True
    assert result.files_changed == 1
    assert result.total_replacements == 1
    assert result.exit_code == 0


@pytest.mark.asyncio
async def test_tool_replace_within_limits_region(tmp_path: pathlib.Path) -> None:
    """`within`はdry-run結果を領域内へ限定する（領域外fooは件数に含めない）。"""
    target = tmp_path / "sample.txt"
    target.write_text("foo\nKEY foo\n", encoding="utf-8")

    result = await pyfltr.cli.mcp_server.tool_replace(
        pattern="foo",
        replacement="X",
        paths=[str(target)],
        within="KEY",
    )

    assert result.dry_run is True
    # 領域はKEY行のみ（before/after=0）。領域内のfoo1件のみが対象。
    assert result.total_replacements == 1


@pytest.mark.asyncio
async def test_tool_replace_context_expands_within_region(tmp_path: pathlib.Path) -> None:
    """一括コンテキスト値でアンカー行の前後を置換対象へ含める。"""
    target = tmp_path / "sample.txt"
    target.write_text("foo before\nKEY\nfoo after\n", encoding="utf-8")

    result = await pyfltr.cli.mcp_server.tool_replace(
        pattern="foo",
        replacement="X",
        paths=[str(target)],
        within="KEY",
        context=1,
    )

    assert result.total_replacements == 2


@pytest.mark.asyncio
async def test_tool_replace_within_with_multiline_raises(tmp_path: pathlib.Path) -> None:
    """`within`と`multiline`の併用はValueErrorになる。"""
    target = tmp_path / "sample.txt"
    target.write_text("KEY foo\n", encoding="utf-8")
    with pytest.raises(ValueError, match="multiline"):
        await pyfltr.cli.mcp_server.tool_replace(
            pattern="foo", replacement="X", paths=[str(target)], within="KEY", multiline=True
        )


@pytest.mark.asyncio
async def test_tool_replace_context_without_within_raises(tmp_path: pathlib.Path) -> None:
    """`within`未指定で`before_context`/`after_context`指定はValueErrorになる。"""
    target = tmp_path / "sample.txt"
    target.write_text("foo\n", encoding="utf-8")
    with pytest.raises(ValueError, match="within"):
        await pyfltr.cli.mcp_server.tool_replace(pattern="foo", replacement="X", paths=[str(target)], after_context=1)


@pytest.mark.asyncio
async def test_tool_replace_combined_context_without_within_raises(tmp_path: pathlib.Path) -> None:
    """`within`未指定の一括コンテキスト値を拒否する。"""
    target = tmp_path / "sample.txt"
    target.write_text("foo\n", encoding="utf-8")
    with pytest.raises(ValueError, match="within"):
        await pyfltr.cli.mcp_server.tool_replace(
            pattern="foo",
            replacement="X",
            paths=[str(target)],
            context=1,
        )


@pytest.mark.asyncio
async def test_tool_replace_from_grep_limits_files(tmp_path: pathlib.Path) -> None:
    """grepのJSONL出力に現れるファイルだけを置換対象にする。"""
    allowed = tmp_path / "allowed.txt"
    allowed.write_text("foo\n", encoding="utf-8")
    omitted = tmp_path / "omitted.txt"
    omitted.write_text("foo\n", encoding="utf-8")
    grep_output = tmp_path / "grep.jsonl"
    grep_output.write_text(json.dumps({"kind": "match", "file": str(allowed)}) + "\n", encoding="utf-8")

    result = await pyfltr.cli.mcp_server.tool_replace(
        pattern="foo",
        replacement="X",
        paths=[str(tmp_path)],
        from_grep=str(grep_output),
    )

    assert result.files_changed == 1
    assert [entry.file for entry in result.file_changes] == [allowed.as_posix()]


@pytest.mark.asyncio
async def test_tool_replace_rejects_missing_from_grep_file(tmp_path: pathlib.Path) -> None:
    """読み込めないgrep出力を拒否する。"""
    with pytest.raises(ValueError, match="from-grep"):
        await pyfltr.cli.mcp_server.tool_replace(
            pattern="foo",
            replacement="X",
            paths=[str(tmp_path)],
            from_grep=str(tmp_path / "missing.jsonl"),
        )


@pytest.mark.asyncio
async def test_tool_replace_writes_file_and_returns_replace_id(tmp_path: pathlib.Path) -> None:
    """`tool_replace(dry_run=False)`がファイルを変更し`replace_id`を返すこと。"""
    target = tmp_path / "sample.txt"
    target.write_text("hello world\n", encoding="utf-8")

    result = await pyfltr.cli.mcp_server.tool_replace(
        pattern="hello",
        replacement="goodbye",
        paths=[str(target)],
        dry_run=False,
    )

    assert target.read_text(encoding="utf-8") == "goodbye world\n"
    assert result.replace_id is not None
    assert len(result.replace_id) == 26  # ULIDは26文字
    assert result.dry_run is False
    assert result.files_changed == 1
    assert result.total_replacements == 1
    assert result.exit_code == 0


@pytest.mark.asyncio
async def test_tool_replace_show_changes(tmp_path: pathlib.Path) -> None:
    """`show_changes=True`のとき`changes`フィールドに変更前後が含まれること。"""
    target = tmp_path / "sample.txt"
    target.write_text("hello world\nhello again\n", encoding="utf-8")

    result = await pyfltr.cli.mcp_server.tool_replace(
        pattern="hello",
        replacement="goodbye",
        paths=[str(target)],
        dry_run=True,
        show_changes=True,
    )

    assert len(result.changes) == 2
    for change in result.changes:
        assert change.file == target.as_posix()
        assert "hello" in change.before_line
        assert "goodbye" in change.after_line


@pytest.mark.asyncio
async def test_tool_replace_paths_empty_raises() -> None:
    """`paths=[]`のとき`ValueError`が発生すること。"""
    with pytest.raises(ValueError, match="paths"):
        await pyfltr.cli.mcp_server.tool_replace(
            pattern="hello",
            replacement="goodbye",
            paths=[],
        )


# ---------------------------------------------------------------------------
# replace_undo ツールのテスト
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_tool_replace_undo_restores_file(tmp_path: pathlib.Path) -> None:
    """`tool_replace_undo`が`replace_id`から正常に復元できること。"""
    target = tmp_path / "sample.txt"
    original = "hello world\n"
    target.write_text(original, encoding="utf-8")

    # まず実書き込みを行い replace_id を取得する
    replace_result = await pyfltr.cli.mcp_server.tool_replace(
        pattern="hello",
        replacement="goodbye",
        paths=[str(target)],
        dry_run=False,
    )
    assert replace_result.replace_id is not None
    assert target.read_text(encoding="utf-8") == "goodbye world\n"

    # undo を実行して元に戻す
    undo_result = await pyfltr.cli.mcp_server.tool_replace_undo(
        replace_id=replace_result.replace_id,
    )

    assert target.read_text(encoding="utf-8") == original
    assert target.as_posix() in undo_result.restored
    assert not undo_result.skipped
    assert undo_result.exit_code == 0


@pytest.mark.asyncio
async def test_tool_replace_undo_hash_mismatch_skips_without_force(tmp_path: pathlib.Path) -> None:
    """`force=False`のときハッシュ不一致ファイルが`skipped`に集まり`exit_code=1`になること。"""
    target = tmp_path / "sample.txt"
    target.write_text("hello world\n", encoding="utf-8")

    replace_result = await pyfltr.cli.mcp_server.tool_replace(
        pattern="hello",
        replacement="goodbye",
        paths=[str(target)],
        dry_run=False,
    )
    assert replace_result.replace_id is not None

    # replace後にファイルを手動で編集する（ハッシュ不一致を発生させる）
    target.write_text("manually edited\n", encoding="utf-8")

    undo_result = await pyfltr.cli.mcp_server.tool_replace_undo(
        replace_id=replace_result.replace_id,
        force=False,
    )

    assert target.as_posix() in undo_result.skipped
    assert not undo_result.restored
    assert undo_result.exit_code == 1


@pytest.mark.asyncio
async def test_tool_replace_undo_hash_mismatch_force_restores(tmp_path: pathlib.Path) -> None:
    """`force=True`のときハッシュ不一致でも復元されること。"""
    target = tmp_path / "sample.txt"
    original = "hello world\n"
    target.write_text(original, encoding="utf-8")

    replace_result = await pyfltr.cli.mcp_server.tool_replace(
        pattern="hello",
        replacement="goodbye",
        paths=[str(target)],
        dry_run=False,
    )
    assert replace_result.replace_id is not None

    # replace後にファイルを手動で編集する
    target.write_text("manually edited\n", encoding="utf-8")

    undo_result = await pyfltr.cli.mcp_server.tool_replace_undo(
        replace_id=replace_result.replace_id,
        force=True,
    )

    assert target.read_text(encoding="utf-8") == original
    assert target.as_posix() in undo_result.restored
    assert not undo_result.skipped
    assert undo_result.exit_code == 0


@pytest.mark.asyncio
async def test_tool_replace_undo_not_found_raises() -> None:
    """`replace_id`が存在しない場合`ValueError`が発生すること。"""
    with pytest.raises(ValueError, match="replace_id"):
        await pyfltr.cli.mcp_server.tool_replace_undo(replace_id="NONEXISTENTID00000000000000")


# ---------------------------------------------------------------------------
# replace_history ツールのテスト
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_tool_replace_history_lists_and_shows_without_file_contents(tmp_path: pathlib.Path) -> None:
    """保存済み履歴を一覧・単体参照し、変更前本文と置換レコードを応答から除外する。"""
    target = tmp_path / "sample.txt"
    target.write_text("hello world\n", encoding="utf-8")
    replaced = await pyfltr.cli.mcp_server.tool_replace(
        pattern="hello",
        replacement="goodbye",
        paths=[str(target)],
        dry_run=False,
    )
    assert replaced.replace_id is not None

    listed = await pyfltr.cli.mcp_server.tool_replace_history()
    assert listed.action == "list"
    assert len(listed.entries) == 1
    assert listed.entries[0].replace_id == replaced.replace_id
    # 履歴の`file`は保存時に区切りを`/`へ統一した表現である。
    # Windowsでも`C:/...`形式になるため、`as_posix()`での文字列比較で一致を検証する。
    assert listed.entries[0].files[0].file == target.as_posix()
    assert listed.entries[0].files[0].records_count == 1

    shown = await pyfltr.cli.mcp_server.tool_replace_history(action="show", replace_id=replaced.replace_id)
    payload = shown.model_dump()
    assert shown.action == "show"
    assert len(shown.entries) == 1
    assert "before_content" not in json.dumps(payload)
    assert "records" not in payload["entries"][0]["files"][0]


@pytest.mark.asyncio
async def test_tool_replace_history_empty() -> None:
    """履歴が存在しない場合は空の一覧を返す。"""
    result = await pyfltr.cli.mcp_server.tool_replace_history()
    assert result.action == "list"
    assert not result.entries


@pytest.mark.asyncio
async def test_tool_replace_history_rejects_invalid_requests() -> None:
    """未対応actionと識別子のない単体参照を拒否する。"""
    with pytest.raises(ValueError, match="action"):
        await pyfltr.cli.mcp_server.tool_replace_history(action="invalid")
    with pytest.raises(ValueError, match="replace_id"):
        await pyfltr.cli.mcp_server.tool_replace_history(action="show")


@pytest.mark.asyncio
async def test_tool_replace_history_show_rejects_unknown_id() -> None:
    """存在しないreplace識別子を拒否する。"""
    with pytest.raises(ValueError, match="replace_id"):
        await pyfltr.cli.mcp_server.tool_replace_history(action="show", replace_id="unknown")


# ---------------------------------------------------------------------------
# command_info ツールのテスト
# ---------------------------------------------------------------------------


def test_tool_command_info_check_default_is_false() -> None:
    """副作用を伴う事前確認を既定では実行しない。"""
    signature = inspect.signature(pyfltr.cli.mcp_server.tool_command_info)
    assert signature.parameters["check"].default is False


@pytest.mark.asyncio
async def test_tool_command_info_resolves_known_command() -> None:
    """既知ツールの起動方式を公開関数経由で解決する。"""
    result = await pyfltr.cli.mcp_server.tool_command_info("ec")
    assert result.command == "ec"
    assert result.resolved is True
    assert result.info["command"] == "ec"
    assert result.info["resolved"] is True


@pytest.mark.asyncio
async def test_tool_command_info_rejects_unknown_command() -> None:
    """未知ツールを共通のコマンド検証経路で拒否する。"""
    with pytest.raises(ValueError, match="コマンドが見つかりません"):
        await pyfltr.cli.mcp_server.tool_command_info("unknown-command")


# ---------------------------------------------------------------------------
# config ツールのテスト
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_tool_config_crud_and_list(tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """プロジェクト設定の一覧・既定値参照・設定・削除を一連の操作として実行する。"""
    monkeypatch.chdir(tmp_path)
    config_path = tmp_path / "pyproject.toml"
    config_path.write_text("[tool.pyfltr]\njobs = 2\n", encoding="utf-8")

    listed = await pyfltr.cli.mcp_server.tool_config(action="list")
    assert listed.path == str(config_path)
    assert listed.values == {"jobs": 2}

    default_value = await pyfltr.cli.mcp_server.tool_config(action="get", key="archive")
    assert default_value.value is True
    assert default_value.is_default is True

    set_result = await pyfltr.cli.mcp_server.tool_config(action="set", key="jobs", value="4")
    assert set_result.value == 4
    explicit_value = await pyfltr.cli.mcp_server.tool_config(action="get", key="jobs")
    assert explicit_value.value == 4
    assert explicit_value.is_default is False

    deleted = await pyfltr.cli.mcp_server.tool_config(action="delete", key="jobs")
    assert deleted.existed is True
    deleted_again = await pyfltr.cli.mcp_server.tool_config(action="delete", key="jobs")
    assert deleted_again.existed is False


@pytest.mark.asyncio
async def test_tool_config_lists_defaults(tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """既定値を含む一覧では明示値と既定値を区別する。"""
    monkeypatch.chdir(tmp_path)
    (tmp_path / "pyproject.toml").write_text("[tool.pyfltr]\njobs = 3\n", encoding="utf-8")

    result = await pyfltr.cli.mcp_server.tool_config(action="list", include_defaults=True)

    assert result.values["jobs"] == {"value": 3, "default": False}
    assert result.values["archive"] == {"value": True, "default": True}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("kwargs", "message"),
    [
        ({"action": "invalid"}, "action"),
        ({"action": "get"}, "key"),
        ({"action": "get", "key": "jobs", "value": "1"}, "value"),
        ({"action": "set", "key": "jobs"}, "key と value"),
        ({"action": "delete"}, "key"),
        ({"action": "delete", "key": "jobs", "value": "1"}, "value"),
        ({"action": "list", "key": "jobs"}, "key と value"),
        ({"action": "get", "key": "jobs", "include_defaults": True}, "include_defaults"),
    ],
)
async def test_tool_config_rejects_invalid_parameter_combinations(
    kwargs: dict[str, typing.Any],
    message: str,
) -> None:
    """actionごとの必須引数と禁止引数を実行時に検証する。"""
    with pytest.raises(ValueError, match=message):
        await pyfltr.cli.mcp_server.tool_config(**kwargs)


@pytest.mark.asyncio
async def test_tool_config_rejects_unknown_key(tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """未知設定キーを候補案内付きの共通文面で拒否する。"""
    monkeypatch.chdir(tmp_path)
    (tmp_path / "pyproject.toml").write_text("[tool.pyfltr]\n", encoding="utf-8")
    with pytest.raises(ValueError, match="認識できません"):
        await pyfltr.cli.mcp_server.tool_config(action="get", key="unknown-key")


@pytest.mark.asyncio
async def test_tool_config_returns_current_request_warnings_only(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """設定警告を戻り値へ収集し、後続リクエストへ持ち越さない。"""
    monkeypatch.chdir(tmp_path)
    (tmp_path / "pyproject.toml").write_text("[tool.pyfltr]\n", encoding="utf-8")

    warned = await pyfltr.cli.mcp_server.tool_config(action="set", key="archive", value="false")
    assert len(warned.warnings) == 1
    assert "archive/cache系" in warned.warnings[0]

    clean = await pyfltr.cli.mcp_server.tool_config(action="set", key="jobs", value="2")
    assert not clean.warnings


@pytest.mark.asyncio
async def test_tool_config_global_set_creates_file_and_warns(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """グローバル設定を新規作成し、通常キーの優先順位に関する警告を返す。"""
    global_path = tmp_path / "config" / "config.toml"
    monkeypatch.setenv("PYFLTR_GLOBAL_CONFIG", str(global_path))

    result = await pyfltr.cli.mcp_server.tool_config(
        action="set",
        key="jobs",
        value="3",
        use_global=True,
    )

    assert result.path == str(global_path)
    assert global_path.is_file()
    assert len(result.warnings) == 1
    assert "通常キー" in result.warnings[0]


@pytest.mark.asyncio
async def test_new_tools_keep_stdout_clean(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """新設3ツールはstdioトランスポート用のstdoutへ出力しない。"""
    monkeypatch.chdir(tmp_path)
    (tmp_path / "pyproject.toml").write_text("[tool.pyfltr]\n", encoding="utf-8")

    await pyfltr.cli.mcp_server.tool_replace_history()
    await pyfltr.cli.mcp_server.tool_command_info("ec")
    await pyfltr.cli.mcp_server.tool_config(action="list")

    captured = capsys.readouterr()
    assert captured.out == ""
