"""mcp_server.py のテスト。

`PYFLTR_CACHE_DIR` を `tmp_path` に固定することで、テストデータ生成に使う
`ArchiveStore(cache_root=tmp_path)` と MCP ツール内部で呼ぶ `ArchiveStore()`
（`default_cache_root()` 解決）が同一キャッシュを参照する。
"""

import inspect
import json
import pathlib
import shutil

import pytest

import pyfltr.cli.mcp_models
import pyfltr.cli.mcp_server
import pyfltr.state.archive
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
    result = await pyfltr.cli.mcp_server._tool_list_runs()
    assert result == []


@pytest.mark.asyncio
async def test_tool_list_runs_returns_summaries(tmp_path: pathlib.Path) -> None:
    run_id1 = _seed_run(tmp_path, commands=["ruff-check"], exit_code=0)
    run_id2 = _seed_run(tmp_path, commands=["mypy"], exit_code=1)

    result = await pyfltr.cli.mcp_server._tool_list_runs(limit=10)
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

    result = await pyfltr.cli.mcp_server._tool_list_runs(limit=2)
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

    result = await pyfltr.cli.mcp_server._tool_show_run(run_id)
    assert result.run_id == run_id
    assert "run_id" in result.meta
    command_names = [c.command for c in result.commands]
    assert "ruff-check" in command_names
    assert "mypy" in command_names


@pytest.mark.asyncio
async def test_tool_show_run_latest(tmp_path: pathlib.Path) -> None:
    _seed_run(tmp_path, commands=["ruff-check"])
    latest_id = _seed_run(tmp_path, commands=["mypy"])

    result = await pyfltr.cli.mcp_server._tool_show_run("latest")
    assert result.run_id == latest_id


@pytest.mark.asyncio
async def test_tool_show_run_prefix(tmp_path: pathlib.Path) -> None:
    run_id = _seed_run(tmp_path)
    result = await pyfltr.cli.mcp_server._tool_show_run(run_id[:8])
    assert result.run_id == run_id


@pytest.mark.asyncio
async def test_tool_show_run_not_found() -> None:
    with pytest.raises(ValueError, match="run_id"):
        await pyfltr.cli.mcp_server._tool_show_run("nonexistent")


@pytest.mark.asyncio
async def test_tool_show_run_latest_empty() -> None:
    with pytest.raises(ValueError, match="run"):
        await pyfltr.cli.mcp_server._tool_show_run("latest")


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
        await pyfltr.cli.mcp_server._tool_show_run(prefix)


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

    results = await pyfltr.cli.mcp_server._tool_show_run_diagnostics(run_id, ["mypy"])
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
    results = await pyfltr.cli.mcp_server._tool_show_run_diagnostics(run_id, ["textlint"])
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

    results = await pyfltr.cli.mcp_server._tool_show_run_diagnostics(run_id, ["mypy"])
    assert len(results) == 1
    assert results[0].hints is None


@pytest.mark.asyncio
async def test_tool_show_run_diagnostics_tool_not_found(tmp_path: pathlib.Path) -> None:
    run_id = _seed_run(tmp_path)
    with pytest.raises(ValueError, match="nonexistent"):
        await pyfltr.cli.mcp_server._tool_show_run_diagnostics(run_id, ["nonexistent"])


@pytest.mark.asyncio
async def test_tool_show_run_output(tmp_path: pathlib.Path) -> None:
    run_id = _seed_run(
        tmp_path,
        tool_results=[
            ("ruff-check", 0, "raw output line 1\nraw output line 2\n", []),
        ],
    )

    result = await pyfltr.cli.mcp_server._tool_show_run_output(run_id, ["ruff-check"])
    assert "ruff-check" in result
    assert "raw output line 1" in result["ruff-check"]
    assert "raw output line 2" in result["ruff-check"]


@pytest.mark.asyncio
async def test_tool_show_run_output_tool_not_found(tmp_path: pathlib.Path) -> None:
    run_id = _seed_run(tmp_path)
    with pytest.raises(ValueError, match="nonexistent"):
        await pyfltr.cli.mcp_server._tool_show_run_output(run_id, ["nonexistent"])


# ---------------------------------------------------------------------------
# FastMCP サーバー登録確認
# ---------------------------------------------------------------------------


def test_build_server_registers_eight_tools() -> None:
    server = pyfltr.cli.mcp_server._build_server()
    tools = server._tool_manager.list_tools()
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
    }
    assert tool_names == expected


# ---------------------------------------------------------------------------
# 実行系ツールのテスト
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.skipif(not shutil.which("typos"), reason="typos コマンドが環境にない")
async def test_tool_run_for_agent_with_typos(tmp_path: pathlib.Path) -> None:
    sample = tmp_path / "sample.txt"
    sample.write_text("hello world\n", encoding="utf-8")

    result = await pyfltr.cli.mcp_server._tool_run_for_agent(
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
    await pyfltr.cli.mcp_server._tool_run_for_agent(paths=[str(sample)], commands=["ec"])

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
    result = await pyfltr.cli.mcp_server._tool_run_for_agent(
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

    result = await pyfltr.cli.mcp_server._tool_run_for_agent(
        paths=[str(sample)],
        commands=["ec"],
    )

    assert isinstance(result.retry_commands, dict)
    # 成功したコマンドはキーに含まれない
    for key in result.retry_commands:
        assert key in result.failed


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

    result = await pyfltr.cli.mcp_server._tool_run_for_agent(
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
        await pyfltr.cli.mcp_server._tool_run_for_agent(
            paths=["dummy"],
            from_run="latest",
        )


@pytest.mark.asyncio
async def test_tool_run_for_agent_only_failed_no_previous_run(tmp_path: pathlib.Path) -> None:
    """only_failed=Trueで直前runがない場合はearly exit（run_id=None・skipped_reasonあり）。"""
    sample = tmp_path / "input.txt"
    sample.write_text("hello\n", encoding="utf-8")

    result = await pyfltr.cli.mcp_server._tool_run_for_agent(
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
    """`_tool_grep`が指定ファイル群から正しくマッチを抽出すること。"""
    target = tmp_path / "sample.txt"
    target.write_text("hello world\nfoo bar\nhello again\n", encoding="utf-8")

    result = await pyfltr.cli.mcp_server._tool_grep(
        pattern="hello",
        paths=[str(target)],
    )

    assert result.total_matches == 2
    assert result.files_scanned == 1
    assert result.exit_code == 0
    assert len(result.matches) == 2
    for match in result.matches:
        assert match.file == str(target)
        assert "hello" in match.line_text


@pytest.mark.asyncio
async def test_tool_grep_no_match_returns_exit_code_1(tmp_path: pathlib.Path) -> None:
    """`_tool_grep`がマッチ0件のとき`exit_code=1`を返すこと。"""
    target = tmp_path / "sample.txt"
    target.write_text("hello world\n", encoding="utf-8")

    result = await pyfltr.cli.mcp_server._tool_grep(
        pattern="notfound",
        paths=[str(target)],
    )

    assert result.total_matches == 0
    assert result.exit_code == 1
    assert not result.matches


@pytest.mark.asyncio
async def test_tool_grep_respects_exclude(tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """`_tool_grep`が`exclude`設定を尊重し、除外ディレクトリ配下のファイルを結果に含めないこと。"""
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

    result = await pyfltr.cli.mcp_server._tool_grep(
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

    result = await pyfltr.cli.mcp_server._tool_grep(
        pattern="hello",
        paths=[str(target)],
        max_total=5,
    )

    assert result.total_matches <= 5
    assert len(result.matches) <= 5


# ---------------------------------------------------------------------------
# replace ツールのテスト
# ---------------------------------------------------------------------------


def test_tool_replace_dry_run_default() -> None:
    """`_tool_replace`の`dry_run`引数の既定値が`True`であること。"""
    sig = inspect.signature(pyfltr.cli.mcp_server._tool_replace)
    assert sig.parameters["dry_run"].default is True


@pytest.mark.asyncio
async def test_tool_replace_dry_run_does_not_write(tmp_path: pathlib.Path) -> None:
    """`_tool_replace(dry_run=True)`がファイルを変更しないこと。"""
    target = tmp_path / "sample.txt"
    original = "hello world\n"
    target.write_text(original, encoding="utf-8")

    result = await pyfltr.cli.mcp_server._tool_replace(
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
async def test_tool_replace_writes_file_and_returns_replace_id(tmp_path: pathlib.Path) -> None:
    """`_tool_replace(dry_run=False)`がファイルを変更し`replace_id`を返すこと。"""
    target = tmp_path / "sample.txt"
    target.write_text("hello world\n", encoding="utf-8")

    result = await pyfltr.cli.mcp_server._tool_replace(
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

    result = await pyfltr.cli.mcp_server._tool_replace(
        pattern="hello",
        replacement="goodbye",
        paths=[str(target)],
        dry_run=True,
        show_changes=True,
    )

    assert len(result.changes) == 2
    for change in result.changes:
        assert "hello" in change.before_line
        assert "goodbye" in change.after_line


@pytest.mark.asyncio
async def test_tool_replace_paths_empty_raises() -> None:
    """`paths=[]`のとき`ValueError`が発生すること。"""
    with pytest.raises(ValueError, match="paths"):
        await pyfltr.cli.mcp_server._tool_replace(
            pattern="hello",
            replacement="goodbye",
            paths=[],
        )


# ---------------------------------------------------------------------------
# replace_undo ツールのテスト
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_tool_replace_undo_restores_file(tmp_path: pathlib.Path) -> None:
    """`_tool_replace_undo`が`replace_id`から正常に復元できること。"""
    target = tmp_path / "sample.txt"
    original = "hello world\n"
    target.write_text(original, encoding="utf-8")

    # まず実書き込みを行い replace_id を取得する
    replace_result = await pyfltr.cli.mcp_server._tool_replace(
        pattern="hello",
        replacement="goodbye",
        paths=[str(target)],
        dry_run=False,
    )
    assert replace_result.replace_id is not None
    assert target.read_text(encoding="utf-8") == "goodbye world\n"

    # undo を実行して元に戻す
    undo_result = await pyfltr.cli.mcp_server._tool_replace_undo(
        replace_id=replace_result.replace_id,
    )

    assert target.read_text(encoding="utf-8") == original
    assert str(target) in undo_result.restored
    assert not undo_result.skipped
    assert undo_result.exit_code == 0


@pytest.mark.asyncio
async def test_tool_replace_undo_hash_mismatch_skips_without_force(tmp_path: pathlib.Path) -> None:
    """`force=False`のときハッシュ不一致ファイルが`skipped`に集まり`exit_code=1`になること。"""
    target = tmp_path / "sample.txt"
    target.write_text("hello world\n", encoding="utf-8")

    replace_result = await pyfltr.cli.mcp_server._tool_replace(
        pattern="hello",
        replacement="goodbye",
        paths=[str(target)],
        dry_run=False,
    )
    assert replace_result.replace_id is not None

    # replace後にファイルを手動で編集する（ハッシュ不一致を発生させる）
    target.write_text("manually edited\n", encoding="utf-8")

    undo_result = await pyfltr.cli.mcp_server._tool_replace_undo(
        replace_id=replace_result.replace_id,
        force=False,
    )

    assert str(target) in undo_result.skipped
    assert not undo_result.restored
    assert undo_result.exit_code == 1


@pytest.mark.asyncio
async def test_tool_replace_undo_hash_mismatch_force_restores(tmp_path: pathlib.Path) -> None:
    """`force=True`のときハッシュ不一致でも復元されること。"""
    target = tmp_path / "sample.txt"
    original = "hello world\n"
    target.write_text(original, encoding="utf-8")

    replace_result = await pyfltr.cli.mcp_server._tool_replace(
        pattern="hello",
        replacement="goodbye",
        paths=[str(target)],
        dry_run=False,
    )
    assert replace_result.replace_id is not None

    # replace後にファイルを手動で編集する
    target.write_text("manually edited\n", encoding="utf-8")

    undo_result = await pyfltr.cli.mcp_server._tool_replace_undo(
        replace_id=replace_result.replace_id,
        force=True,
    )

    assert target.read_text(encoding="utf-8") == original
    assert str(target) in undo_result.restored
    assert not undo_result.skipped
    assert undo_result.exit_code == 0


@pytest.mark.asyncio
async def test_tool_replace_undo_not_found_raises() -> None:
    """`replace_id`が存在しない場合`ValueError`が発生すること。"""
    with pytest.raises(ValueError, match="replace_id"):
        await pyfltr.cli.mcp_server._tool_replace_undo(replace_id="NONEXISTENTID00000000000000")
