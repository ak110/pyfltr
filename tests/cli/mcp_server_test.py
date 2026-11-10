import argparse
import contextlib
import dataclasses
import importlib.metadata
import inspect
import pathlib
import shutil
import sys
import typing

import mcp
import mcp.client.stdio
import mcp.server.mcpserver.exceptions
import mcp_types
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
from tests.conftest import make_command_result as _make_result
from tests.conftest import make_error_location as _make_error
from tests.conftest import seed_archive_run as _seed_run

# ツール関数が想定内の失敗を伝える例外。MCPServerはこの型の本文だけをクライアントへ届ける。


_COMPLETION_FIELDS: dict[str, typing.Any] = {
    "completion": "not_reached",
    "files_reached": 0,
    "completed_commands": [],
    "incomplete_commands": [],
    "missing_targets": [],
    "fully_excluded_files": [],
}


pytestmark = pytest.mark.usefixtures("mcp_cache")

_ToolError = mcp.server.mcpserver.exceptions.ToolError


# `run_pipeline`を差し替えるテストが返す、何も実行しなかった結果。


# `RunResult`を直接組み立てるテストで使う完了判定の6項目。


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


def test_collect_tool_summaries_includes_elapsed(tmp_path: pathlib.Path) -> None:
    """collect_tool_summariesがtool.jsonのelapsedを読み取る。"""
    store = pyfltr.state.archive.ArchiveStore(cache_root=tmp_path)
    run_id = store.start_run(commands=["pytest"])
    result = dataclasses.replace(_make_result("pytest", returncode=0, command_type="tester"), elapsed=1.2345)
    store.write_tool_result(run_id, result)
    summaries = pyfltr.state.runs.collect_tool_summaries(store, run_id)
    assert summaries[0]["elapsed"] == 1.234


def test_collect_tool_summaries_omits_slow_tests_when_absent(tmp_path: pathlib.Path) -> None:
    """slow_testsを持たないtool.jsonでは対象のキーを付けない。"""
    store = pyfltr.state.archive.ArchiveStore(cache_root=tmp_path)
    run_id = store.start_run(commands=["pytest"])
    store.write_tool_result(run_id, _make_result("pytest", returncode=0, command_type="tester"))
    summaries = pyfltr.state.runs.collect_tool_summaries(store, run_id)
    assert "slow_tests" not in summaries[0]


@pytest.mark.asyncio
async def test_show_run_diagnostics_serializes_optional_meta_conditionally(tmp_path: pathlib.Path) -> None:
    """保存元にある任意meta項目だけをMCP公開応答へ含める。"""
    store = pyfltr.state.archive.ArchiveStore(cache_root=tmp_path)
    run_id = store.start_run(commands=["mypy", "pytest"])
    store.write_tool_result(run_id, _make_result("mypy", returncode=0))
    slow_tests = [pyfltr.command.slow_tests.SlowTest("tests/a_test.py::test_x", "call", 1.5)]
    pytest_result = dataclasses.replace(
        _make_result("pytest", returncode=1, command_type="tester"),
        slow_tests=slow_tests,
        retry_command="pyfltr run --commands=pytest tests/a_test.py",
        started_at="2026-11-05T00:00:00+00:00",
        finished_at="2026-11-05T00:00:01+00:00",
    )
    store.write_tool_result(run_id, pytest_result)
    store.finalize_run(run_id, exit_code=1)

    result = await pyfltr.cli.mcp_server.build_server().call_tool(
        "show_run_diagnostics",
        {"run_id": run_id, "commands": ["mypy", "pytest"]},
    )
    assert isinstance(result, mcp_types.CallToolResult)
    assert result.structured_content is not None
    serialized_commands = result.structured_content["result"]
    assert isinstance(serialized_commands, list)
    mypy_meta = serialized_commands[0]["command_meta"]
    assert "slow_tests" not in mypy_meta
    assert "retry_command" not in mypy_meta
    assert "started_at" not in mypy_meta
    assert "finished_at" not in mypy_meta
    pytest_meta = serialized_commands[1]["command_meta"]
    assert (pytest_meta["started_at"], pytest_meta["finished_at"]) == (
        "2026-11-05T00:00:00+00:00",
        "2026-11-05T00:00:01+00:00",
    )
    assert pytest_meta["slow_tests"] == [test.to_dict() for test in slow_tests]
    assert pytest_meta["retry_command"] == "pyfltr run --commands=pytest tests/a_test.py"


@pytest.mark.asyncio
async def test_show_run_output_returns_requested_line_range(tmp_path: pathlib.Path) -> None:
    """行範囲を指定すると続きの開始位置で分割取得でき、範囲を省略すると従来どおり全文を返す。"""
    run_id = _seed_run(tmp_path, tool_results=[("pytest", 1, "l1\nl2\nl3\nl4\nl5\n", [])])

    async with mcp.Client(pyfltr.cli.mcp_server.build_server()) as client:
        first = await client.call_tool("show_run_output", {"run_id": run_id, "commands": ["pytest"], "limit": 2})
        assert first.structured_content is not None
        page = first.structured_content["pytest"]
        assert page == {"text": "l1\nl2\n", "offset": 0, "returned_lines": 2, "total_lines": 5, "next_offset": 2}

        texts = [page["text"]]
        while page["next_offset"] is not None:
            result = await client.call_tool(
                "show_run_output",
                {"run_id": run_id, "commands": ["pytest"], "offset": page["next_offset"], "limit": 2},
            )
            assert result.structured_content is not None
            page = result.structured_content["pytest"]
            texts.append(page["text"])
        assert "".join(texts) == "l1\nl2\nl3\nl4\nl5\n"
        assert page["offset"] == 4

        whole = await client.call_tool("show_run_output", {"run_id": run_id, "commands": ["pytest"]})
        assert whole.structured_content == {"pytest": "l1\nl2\nl3\nl4\nl5\n"}

        # 開始位置0だけを明示した呼び出しも範囲指定として扱い、範囲応答を返す。
        from_start = await client.call_tool("show_run_output", {"run_id": run_id, "commands": ["pytest"], "offset": 0})
        assert from_start.structured_content == {
            "pytest": {"text": "l1\nl2\nl3\nl4\nl5\n", "offset": 0, "returned_lines": 5, "total_lines": 5, "next_offset": None}
        }

        rejected = await client.call_tool("show_run_output", {"run_id": run_id, "commands": ["pytest"], "offset": -1})
    assert rejected.is_error
    rejected_texts = [content.text for content in rejected.content if isinstance(content, mcp_types.TextContent)]
    assert any("offset" in text for text in rejected_texts), rejected_texts


@pytest.mark.asyncio
async def test_show_run_diagnostics_returns_requested_message_range(tmp_path: pathlib.Path) -> None:
    """個別指摘の範囲を指定すると範囲内の指摘だけを元のファイルへ集約し、総数と続きの開始位置を返す。"""
    errors = [
        _make_error("mypy", "src/a.py", 1, "一件目"),
        _make_error("mypy", "src/a.py", 2, "二件目"),
        _make_error("mypy", "src/b.py", 3, "三件目"),
    ]
    run_id = _seed_run(tmp_path, tool_results=[("mypy", 1, "out", errors)])

    async with mcp.Client(pyfltr.cli.mcp_server.build_server()) as client:
        ranged = await client.call_tool(
            "show_run_diagnostics", {"run_id": run_id, "commands": ["mypy"], "offset": 1, "limit": 2}
        )
        whole = await client.call_tool("show_run_diagnostics", {"run_id": run_id, "commands": ["mypy"]})
        from_start = await client.call_tool("show_run_diagnostics", {"run_id": run_id, "commands": ["mypy"], "offset": 0})
    assert ranged.structured_content is not None
    (ranged_result,) = ranged.structured_content["result"]
    assert [
        (diagnostic["file"], [message["msg"] for message in diagnostic["messages"]])
        for diagnostic in ranged_result["diagnostics"]
    ] == [("src/a.py", ["二件目"]), ("src/b.py", ["三件目"])]
    assert ranged_result["total_diagnostics"] == 3
    assert ranged_result["next_offset"] is None

    assert whole.structured_content is not None
    (whole_result,) = whole.structured_content["result"]
    assert [len(diagnostic["messages"]) for diagnostic in whole_result["diagnostics"]] == [2, 1]
    assert "total_diagnostics" not in whole_result
    assert "next_offset" not in whole_result

    # 開始位置0だけを明示した呼び出しは全件を範囲応答として返し、総数と続きの有無を含める。
    assert from_start.structured_content is not None
    (from_start_result,) = from_start.structured_content["result"]
    assert from_start_result["diagnostics"] == whole_result["diagnostics"]
    assert from_start_result["total_diagnostics"] == 3
    assert from_start_result["next_offset"] is None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("tool_name", "use_existing_run", "expected_message"),
    [
        ("show_run_diagnostics", False, "run_id が見つかりません"),
        ("show_run_diagnostics", True, "`show_run`ツール でrun"),
        ("show_run_output", False, "run_id が見つかりません"),
        ("show_run_output", True, "`show_run`ツール でrun"),
    ],
    ids=["diagnostics-unknown-run", "diagnostics-unsaved-command", "output-unknown-run", "output-unsaved-command"],
)
async def test_tool_error_message_reaches_client(
    tmp_path: pathlib.Path, tool_name: str, use_existing_run: bool, expected_message: str
) -> None:
    """想定内のエラーでは、pyfltrのエラー文言が次の操作を含む応答本文としてクライアントへ届く。

    ツール関数を直接呼ぶ検査は例外型しか確かめられず、
    SDKが本文を`Error executing tool <ツール名>`へ置き換える事象を検出できないため、
    クライアント経由で応答本文を確かめる。
    """
    run_id = _seed_run(tmp_path) if use_existing_run else "nonexistent"
    async with mcp.Client(pyfltr.cli.mcp_server.build_server()) as client:
        result = await client.call_tool(tool_name, {"run_id": run_id, "commands": ["textlint"]})
    assert result.is_error
    texts = [content.text for content in result.content if isinstance(content, mcp_types.TextContent)]
    assert any(expected_message in text for text in texts), texts


# ---------------------------------------------------------------------------
# MCPServerのサーバー登録確認
# ---------------------------------------------------------------------------


def test_execute_mcp_start_failure_guides_troubleshooting(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """MCPサーバーの起動に失敗した場合は、原因とトラブルシューティングの参照先を示して1で終わる。"""

    def _raise() -> typing.NoReturn:
        raise RuntimeError("port busy")

    monkeypatch.setattr(pyfltr.cli.mcp_server, "build_server", _raise)
    with caplog.at_level("ERROR"):
        rc = pyfltr.cli.mcp_server.execute_mcp(argparse.Namespace())
    assert rc == 1
    assert "port busy" in caplog.text
    assert "guide/troubleshooting/" in caplog.text


@pytest.mark.asyncio
async def test_mcp_cli_starts_server_and_lists_tools() -> None:
    """CLIからstdioサーバーを起動して公開ツールを取得する。"""
    params = mcp.client.stdio.StdioServerParameters(
        command=sys.executable,
        args=["-m", "pyfltr", "mcp"],
        cwd=pathlib.Path(__file__).parents[2],
    )
    async with contextlib.AsyncExitStack() as stack:
        read_stream, write_stream = await stack.enter_async_context(mcp.client.stdio.stdio_client(params))
        session = await stack.enter_async_context(mcp.ClientSession(read_stream, write_stream))
        await session.initialize()
        result = await session.list_tools()
    assert "command_info" in {tool.name for tool in result.tools}


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
        "run",
        "grep",
        "replace",
        "replace_undo",
        "replace_history",
        "command_info",
        "config",
    }
    assert tool_names == expected


@pytest.mark.asyncio
async def test_tools_publish_metadata_for_argument_selection() -> None:
    """ツール一覧と初期化応答だけで、ツールの性質と全引数の意味・選択肢を判断できる。

    説明文はClaude Codeが初期送信で4,096文字に切り詰めるため、その上限内に収まることも確かめる。
    """
    async with mcp.Client(pyfltr.cli.mcp_server.build_server()) as client:
        tools = (await client.list_tools()).tools
        instructions = client.instructions
    assert instructions
    assert len(instructions) <= 4096

    annotations = {}
    for tool in tools:
        assert tool.title, tool.name
        assert tool.description is not None and len(tool.description) <= 4096, tool.name
        assert tool.annotations is not None, tool.name
        annotations[tool.name] = (tool.annotations.read_only_hint, tool.annotations.destructive_hint)
        for name, schema in tool.input_schema["properties"].items():
            assert schema.get("description"), f"{tool.name}.{name}"
    read_only = {"list_runs", "show_run", "show_run_diagnostics", "show_run_output", "grep", "replace_history"}
    writes_files = {"run", "replace", "replace_undo", "config"}
    assert {name for name, hints in annotations.items() if hints == (True, None)} == read_only
    assert {name for name, hints in annotations.items() if hints == (None, True)} == writes_files
    # `check=True`がmiseの導入と信頼設定を変えうるため、読み取り専用とも破壊的ともしない。
    assert annotations["command_info"] == (False, False)

    by_name = {tool.name: tool for tool in tools}
    assert by_name["run"].input_schema["properties"]["mode"]["enum"] == ["run", "fast", "ci"]
    assert by_name["replace_history"].input_schema["properties"]["action"]["enum"] == ["list", "show"]
    assert by_name["config"].input_schema["properties"]["action"]["enum"] == ["get", "set", "delete", "list"]


@pytest.mark.asyncio
async def test_tool_grep_publishes_summary_mode_enum() -> None:
    """集計モードの許容値をMCP入力スキーマから選択できる。"""
    tools = await pyfltr.cli.mcp_server.build_server().list_tools()
    grep_tool = next(tool for tool in tools if tool.name == "grep")
    summary_mode_schema = grep_tool.input_schema["properties"]["summary_mode"]
    assert summary_mode_schema["anyOf"] == [
        {"enum": ["files_with_matches", "count", "files_without_match"], "type": "string"},
        {"type": "null"},
    ]


@pytest.mark.asyncio
async def test_show_run_diagnostics_publishes_projected_contract() -> None:
    """公開説明と出力スキーマが射影後の`command_meta`契約を示す。"""
    tools = await pyfltr.cli.mcp_server.build_server().list_tools()
    tool = next(entry for entry in tools if entry.name == "show_run_diagnostics")
    assert tool.description is not None
    assert "検査対象ファイルの引数列を含まない" in tool.description
    assert tool.output_schema is not None
    command_meta_schema = tool.output_schema["$defs"]["CommandMetaModel"]
    assert set(command_meta_schema["properties"]) == {
        "command",
        "type",
        "status",
        "returncode",
        "files",
        "elapsed",
        "diagnostics",
        "slow_tests",
        "retry_command",
        "started_at",
        "finished_at",
    }
    assert "commandline" not in command_meta_schema["properties"]
    assert "hint_urls" not in command_meta_schema["properties"]
    assert "hints" not in command_meta_schema["properties"]


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
async def test_config_project_warning_uses_mcp_parameter(tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """プロジェクトへ共通設定を書くMCPの案内は、MCPで実行できる操作名を示す。"""
    (tmp_path / "pyproject.toml").write_text("[tool.pyfltr]\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    result = await pyfltr.cli.mcp_server.tool_config("set", key="archive-max-age-days", value="7", use_global=False)
    assert result.value == 7
    assert any("use_global=True" in warning for warning in result.warnings)
    assert all("--global" not in warning for warning in result.warnings)


@pytest.mark.asyncio
@pytest.mark.skipif(not shutil.which("yamllint"), reason="yamllint コマンドが環境にない")
async def test_tool_run_yamllint_returns_diagnostics(tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """MCPクライアントが`run`と`show_run_diagnostics`でyamllintの違反を診断として受け取る。

    yamllintの既定出力はGitHub Actions上で`::error`形式へ切り替わるため、同環境の変数を設定して実行する。
    """
    monkeypatch.setenv("GITHUB_ACTIONS", "true")
    monkeypatch.setenv("GITHUB_WORKFLOW", "ci")
    work_dir = tmp_path / "workspace"
    work_dir.mkdir()
    (work_dir / "pyproject.toml").write_text("[tool.pyfltr]\nyamllint = true\nrespect-gitignore = false\n", encoding="utf-8")
    # ルールを明示列挙し、yamllintの既定ルールの変更に依存させない。
    (work_dir / ".yamllint.yml").write_text(
        "rules:\n  colons: enable\n  commas: enable\n  truthy:\n    level: warning\n", encoding="utf-8"
    )
    (work_dir / "bad.yaml").write_text("a:  1\nb: [1,2]\nc: yes\n", encoding="utf-8")

    result = await pyfltr.cli.mcp_run_tools.tool_run(paths=["bad.yaml"], commands=["yamllint"], work_dir=str(work_dir))

    assert result.run_id is not None
    assert [(summary.command, summary.status, summary.diagnostics) for summary in result.commands] == [
        ("yamllint", "failed", 3)
    ]
    diagnostics = await pyfltr.cli.mcp_runs_tools.tool_show_run_diagnostics(result.run_id, ["yamllint"])
    messages = sorted(
        (diagnostic.file, message.line, message.col, message.rule, message.severity)
        for diagnostic in diagnostics[0].diagnostics
        for message in diagnostic.messages
    )
    assert messages == [
        ("bad.yaml", 1, 4, "colons", "error"),
        ("bad.yaml", 2, 7, "commas", "error"),
        ("bad.yaml", 3, 4, "truthy", "warning"),
    ]


# ---------------------------------------------------------------------------
# RunResult モデルの新フィールドのテスト
# ---------------------------------------------------------------------------


def test_run_result_new_fields_defaults() -> None:
    """RunResultの新フィールドのデフォルト値を確認する。"""
    result = pyfltr.cli.mcp_models.RunResult(
        run_id="01TESTULID1234567890123456",
        exit_code=0,
        failed=[],
        **_COMPLETION_FIELDS,
    )
    assert result.run_id is not None
    assert result.skipped_reason is None
    assert isinstance(result.retry_commands, dict)
    # schema_hintsは廃止済みのため存在しない
    assert not hasattr(result, "schema_hints")


def test_run_result_nullable_run_id() -> None:
    """RunResultのrun_idがNoneを許容する（early exit時）。"""
    result = pyfltr.cli.mcp_models.RunResult(
        run_id=None,
        exit_code=0,
        failed=[],
        skipped_reason="失敗ツールなし",
        **_COMPLETION_FIELDS,
    )
    assert result.run_id is None
    assert result.skipped_reason == "失敗ツールなし"
    assert result.exit_code == 0
    assert not result.failed
    assert not result.commands


# ---------------------------------------------------------------------------
# run ツールの完了判定
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_replace_undo_publishes_warnings_schema() -> None:
    """公開MCPスキーマが取り消し時の警告を応答に含める。"""
    tools = await pyfltr.cli.mcp_server.build_server().list_tools()
    tool = next(entry for entry in tools if entry.name == "replace_undo")
    assert tool.output_schema is not None
    assert "warnings" in tool.output_schema["properties"]
    assert tool.output_schema["properties"]["warnings"]["type"] == "array"


@pytest.mark.asyncio
async def test_mcp_show_run_and_replace_final_responses_omit_internal_values(tmp_path: pathlib.Path) -> None:
    """MCPクライアントへ届く最終応答で、show_runのmetaとreplaceのfile_changesが必要項目だけを持つ。"""
    run_id = _seed_run(
        tmp_path, commands=["mypy"], tool_results=[("mypy", 1, "error", [_make_error("mypy", "a.py", 1, "boom")])]
    )
    target = tmp_path / "sample.txt"
    target.write_text("foo\n", encoding="utf-8")
    async with mcp.Client(pyfltr.cli.mcp_server.build_server()) as client:
        overview = await client.call_tool("show_run", {"run_id": run_id})
        assert overview.structured_content is not None
        meta = overview.structured_content["meta"]
        assert set(meta) <= set(pyfltr.cli.mcp_runs_tools.SHOW_RUN_META_KEYS)
        assert {"commands", "files", "started_at"} <= set(meta)
        assert overview.structured_content["run_id"] == run_id
        assert overview.structured_content["timing"]["measured"] is True
        replaced = await client.call_tool("replace", {"pattern": "foo", "replacement": "bar", "paths": [str(target)]})
        assert replaced.structured_content is not None
        assert replaced.structured_content["file_changes"] == [
            {"file": pyfltr.paths.normalize_separators(str(target)), "count": 1}
        ]
        assert replaced.structured_content["total_replacements"] == 1


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
    # 最上位のcommand・resolvedと重複するキーはinfoへ含めない。
    assert "command" not in result.info
    assert "resolved" not in result.info
    assert "runner" in result.info


@pytest.mark.asyncio
async def test_tool_command_info_rejects_unknown_command() -> None:
    """未知ツールを共通のコマンド検証経路で拒否する。"""
    with pytest.raises(_ToolError, match="コマンドが見つかりません"):
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
    with pytest.raises(_ToolError, match=message):
        await pyfltr.cli.mcp_server.tool_config(**kwargs)


@pytest.mark.asyncio
async def test_tool_config_rejects_unknown_key(tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """未知設定キーを候補案内付きの共通文面で拒否する。"""
    monkeypatch.chdir(tmp_path)
    (tmp_path / "pyproject.toml").write_text("[tool.pyfltr]\n", encoding="utf-8")
    with pytest.raises(_ToolError, match="認識できません"):
        await pyfltr.cli.mcp_server.tool_config(action="get", key="unknown-key")


@pytest.mark.asyncio
async def test_tool_config_set_without_pyproject_guides_use_global(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """project側のpyproject.tomlが無いsetは、CLIと同じ文面でglobal設定の指定方法を案内する。"""
    monkeypatch.chdir(tmp_path)
    with pytest.raises(_ToolError, match="`use_global=True` を指定してください"):
        await pyfltr.cli.mcp_server.tool_config(action="set", key="jobs", value="2")


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
