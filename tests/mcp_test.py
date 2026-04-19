"""mcp_.py のテスト。

``PYFLTR_CACHE_DIR`` を ``tmp_path`` に固定することで、テストデータ生成に使う
``ArchiveStore(cache_root=tmp_path)`` と MCPツール内部で呼ぶ ``ArchiveStore()``
（``default_cache_root()`` 解決）が同一キャッシュを参照する状態を作る。
"""

# pylint: disable=missing-function-docstring,protected-access,duplicate-code

import pathlib
import shutil

import pytest

import pyfltr.archive
import pyfltr.mcp_
from tests.conftest import make_error_location as _make_error
from tests.conftest import seed_archive_run as _seed_run


@pytest.fixture(autouse=True)
def _isolated_cache(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: pathlib.Path,
) -> pathlib.Path:
    """全テストで ``PYFLTR_CACHE_DIR`` を ``tmp_path`` に固定する。"""
    monkeypatch.setenv("PYFLTR_CACHE_DIR", str(tmp_path))
    return tmp_path


# ---------------------------------------------------------------------------
# Pydantic モデルのテスト
# ---------------------------------------------------------------------------


def test_run_summary_model_fields() -> None:
    model = pyfltr.mcp_.RunSummaryModel(
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
    model = pyfltr.mcp_.DiagnosticModel()
    assert model.tool is None
    assert model.file is None
    assert not model.messages


def test_diagnostic_message_model_all_optional() -> None:
    # DiagnosticMessageModel も全フィールド省略可能
    model = pyfltr.mcp_.DiagnosticMessageModel()
    assert model.line is None
    assert model.severity is None
    assert model.msg is None


# ---------------------------------------------------------------------------
# 読み取り系ツールのテスト
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_tool_list_runs_empty() -> None:
    result = await pyfltr.mcp_._tool_list_runs()
    assert result == []


@pytest.mark.asyncio
async def test_tool_list_runs_returns_summaries(tmp_path: pathlib.Path) -> None:
    run_id1 = _seed_run(tmp_path, commands=["ruff-check"], exit_code=0)
    run_id2 = _seed_run(tmp_path, commands=["mypy"], exit_code=1)

    result = await pyfltr.mcp_._tool_list_runs(limit=10)
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

    result = await pyfltr.mcp_._tool_list_runs(limit=2)
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

    result = await pyfltr.mcp_._tool_show_run(run_id)
    assert result.run_id == run_id
    assert "run_id" in result.meta
    tool_names = [t.tool for t in result.tools]
    assert "ruff-check" in tool_names
    assert "mypy" in tool_names


@pytest.mark.asyncio
async def test_tool_show_run_latest(tmp_path: pathlib.Path) -> None:
    _seed_run(tmp_path, commands=["ruff-check"])
    latest_id = _seed_run(tmp_path, commands=["mypy"])

    result = await pyfltr.mcp_._tool_show_run("latest")
    assert result.run_id == latest_id


@pytest.mark.asyncio
async def test_tool_show_run_prefix(tmp_path: pathlib.Path) -> None:
    run_id = _seed_run(tmp_path)
    result = await pyfltr.mcp_._tool_show_run(run_id[:8])
    assert result.run_id == run_id


@pytest.mark.asyncio
async def test_tool_show_run_not_found() -> None:
    with pytest.raises(ValueError, match="run_id"):
        await pyfltr.mcp_._tool_show_run("nonexistent")


@pytest.mark.asyncio
async def test_tool_show_run_latest_empty() -> None:
    with pytest.raises(ValueError, match="run"):
        await pyfltr.mcp_._tool_show_run("latest")


@pytest.mark.asyncio
async def test_tool_show_run_ambiguous_prefix(tmp_path: pathlib.Path) -> None:
    run_ids = [_seed_run(tmp_path) for _ in range(2)]
    # ULID の先頭は同じタイムスタンプ部分 (ミリ秒単位) を共有する可能性が高いため、
    # 実際に共通する最長プレフィックスを算出してテストする。
    shared = 0
    for a, b in zip(run_ids[0], run_ids[1], strict=False):
        if a != b:
            break
        shared += 1
    if shared < 1:
        pytest.skip("shared prefix が無いケースでは曖昧判定にならない")
    prefix = run_ids[0][:shared]

    with pytest.raises(ValueError, match="曖昧"):
        await pyfltr.mcp_._tool_show_run(prefix)


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

    result = await pyfltr.mcp_._tool_show_run_diagnostics(run_id, "mypy")
    assert result.tool_meta["tool"] == "mypy"
    assert len(result.diagnostics) == 1
    diagnostic = result.diagnostics[0]
    assert diagnostic.file == "src/a.py"
    assert len(diagnostic.messages) == 1
    message = diagnostic.messages[0]
    assert message.line == 42
    assert message.col == 5
    assert message.msg == "型エラー"


@pytest.mark.asyncio
async def test_tool_show_run_diagnostics_tool_not_found(tmp_path: pathlib.Path) -> None:
    run_id = _seed_run(tmp_path)
    with pytest.raises(ValueError, match="nonexistent"):
        await pyfltr.mcp_._tool_show_run_diagnostics(run_id, "nonexistent")


@pytest.mark.asyncio
async def test_tool_show_run_output(tmp_path: pathlib.Path) -> None:
    run_id = _seed_run(
        tmp_path,
        tool_results=[
            ("ruff-check", 0, "raw output line 1\nraw output line 2\n", []),
        ],
    )

    result = await pyfltr.mcp_._tool_show_run_output(run_id, "ruff-check")
    assert "raw output line 1" in result
    assert "raw output line 2" in result


@pytest.mark.asyncio
async def test_tool_show_run_output_tool_not_found(tmp_path: pathlib.Path) -> None:
    run_id = _seed_run(tmp_path)
    with pytest.raises(ValueError, match="nonexistent"):
        await pyfltr.mcp_._tool_show_run_output(run_id, "nonexistent")


# ---------------------------------------------------------------------------
# FastMCP サーバー登録確認
# ---------------------------------------------------------------------------


def test_build_server_registers_five_tools() -> None:
    server = pyfltr.mcp_._build_server()
    tools = server._tool_manager.list_tools()
    tool_names = {t.name for t in tools}
    expected = {"list_runs", "show_run", "show_run_diagnostics", "show_run_output", "run_for_agent"}
    assert tool_names == expected


# ---------------------------------------------------------------------------
# 実行系ツールのテスト
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.skipif(not shutil.which("typos"), reason="typos コマンドが環境にない")
async def test_tool_run_for_agent_with_typos(tmp_path: pathlib.Path) -> None:
    sample = tmp_path / "sample.txt"
    sample.write_text("hello world\n", encoding="utf-8")

    result = await pyfltr.mcp_._tool_run_for_agent(
        paths=[str(sample)],
        commands=["typos"],
    )

    assert result.run_id is not None
    assert len(result.run_id) > 0
    assert isinstance(result.exit_code, int)
    assert isinstance(result.failed, list)
    assert isinstance(result.tools, list)


@pytest.mark.asyncio
async def test_tool_run_for_agent_returns_run_id(tmp_path: pathlib.Path) -> None:
    """``run_for_agent`` が run_id を含む結果を返すことを確認する。

    ``commands=None`` でプロジェクト設定のコマンドを使用し、アーカイブに記録されることを検証する。
    実際のツール実行を避けるため ``commands=[]`` に近いケースとして ``typos`` を条件付きで使用するか、
    ここでは ``typos`` が利用可能なら 1 件実行する形で確認する。
    """
    # 最小限の入力ファイルを用意する
    sample = tmp_path / "input.txt"
    sample.write_text("This is a simple test file.\n", encoding="utf-8")

    # typos が使えない環境でも動作させるため、利用可能なコマンドを選ぶ。
    # ec は設定不要で動作するため使用する。
    result = await pyfltr.mcp_._tool_run_for_agent(
        paths=[str(sample)],
        commands=["ec"],
    )

    assert result.run_id is not None
    assert len(result.run_id) == 26  # ULID は 26 文字
    assert isinstance(result.exit_code, int)

    # アーカイブに保存されていることを確認する
    store = pyfltr.archive.ArchiveStore()
    summaries = store.list_runs(limit=1)
    assert len(summaries) == 1
    assert summaries[0].run_id == result.run_id
