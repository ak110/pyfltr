"""置換とundoのI/O失敗を、CLI・MCPの公開操作から検証する。"""

import asyncio
import json
import pathlib
import typing

import mcp.server.mcpserver.exceptions
import pytest

import pyfltr.cli.main
import pyfltr.cli.mcp_server
import pyfltr.grep_.history


@pytest.fixture(autouse=True)
def _workspace(tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("PYFLTR_CACHE_DIR", str(tmp_path / "cache"))


def _replace(mode: str, files: list[pathlib.Path], capsys: pytest.CaptureFixture[str]) -> tuple[int, str | None, str]:
    if mode == "cli":
        rc = pyfltr.cli.main.run(
            ["replace", "foo", "bar", "--no-exclude", "--no-gitignore", "--output-format=jsonl", *map(str, files)]
        )
        captured = capsys.readouterr()
        records = [json.loads(line) for line in captured.out.splitlines()]
        summary: dict[str, typing.Any] = next((record for record in records if record["kind"] == "summary"), {})
        return rc, summary.get("replace_id"), captured.err
    try:
        result = asyncio.run(
            pyfltr.cli.mcp_server.tool_replace(
                "foo", "bar", list(map(str, files)), dry_run=False, no_exclude=True, no_gitignore=True
            )
        )
    except mcp.server.mcpserver.exceptions.ToolError as exc:
        return 1, None, str(exc)
    return result.exit_code, result.replace_id, " ".join(result.warnings)


def _undo(mode: str, replace_id: str, capsys: pytest.CaptureFixture[str], *, force: bool = False) -> tuple[int, str]:
    if mode == "cli":
        rc = pyfltr.cli.main.run(["replace", "--undo", replace_id, "--output-format=jsonl", *(["--force"] if force else [])])
        return rc, capsys.readouterr().err
    try:
        result = asyncio.run(pyfltr.cli.mcp_server.tool_replace_undo(replace_id, force=force))
    except mcp.server.mcpserver.exceptions.ToolError as exc:
        return 1, str(exc)
    return result.exit_code, " ".join(result.warnings)


def _write_original(path: pathlib.Path, data: bytes) -> int:
    with path.open("wb") as stream:
        return stream.write(data)


@pytest.mark.parametrize("mode", ["cli", "mcp"])
@pytest.mark.parametrize("failed_name", ["before.bin", "changes.json", "meta.json"])
def test_replace_history_failure_keeps_all_targets(
    mode: str, failed_name: str, tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    files = [tmp_path / "a.txt", tmp_path / "b.txt"]
    for file in files:
        file.write_bytes(b"foo\r\n")

    def _bytes(path: pathlib.Path, data: bytes) -> int:
        if path.name == failed_name:
            raise OSError("保存失敗を注入")
        return _write_original(path, data)

    def _text(path: pathlib.Path, data: str, encoding=None, errors=None, newline=None) -> int:
        if path.name == failed_name:
            raise OSError("保存失敗を注入")
        with path.open("w", encoding=encoding, errors=errors, newline=newline) as stream:
            return stream.write(data)

    monkeypatch.setattr(pathlib.Path, "write_bytes", _bytes)
    monkeypatch.setattr(pathlib.Path, "write_text", _text)
    rc, _, error = _replace(mode, files, capsys)
    assert rc == 1
    assert "対象ファイルは変更していません" in error
    assert [file.read_bytes() for file in files] == [b"foo\r\n", b"foo\r\n"]


@pytest.mark.parametrize("mode", ["cli", "mcp"])
@pytest.mark.parametrize("rollback_fails", [False, True])
def test_replace_partial_write_recovers_or_keeps_undo_history(
    mode: str, rollback_fails: bool, tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    files = [tmp_path / "a.txt", tmp_path / "b.txt"]
    for file in files:
        file.write_bytes(b"foo\r\n")
    store = pyfltr.grep_.history.ReplaceHistoryStore()

    def _write(path: pathlib.Path, data: bytes) -> int:
        if path.resolve() in files and data == b"bar\r\n":
            # 対象を初めて書く時点で、全対象のバックアップが読めることを保証する。
            entries = store.list_replaces()
            assert len(store.load_replace(entries[0]["replace_id"])["files"]) == 2
        if path.resolve() == files[1]:
            if data == b"bar\r\n":
                _write_original(path, b"b")
                raise OSError("部分書き込み失敗を注入")
            if rollback_fails:
                raise OSError("復旧失敗を注入")
        return _write_original(path, data)

    with monkeypatch.context() as patch:
        patch.setattr(pathlib.Path, "write_bytes", _write)
        rc, _, error = _replace(mode, files, capsys)
    assert rc == 1
    assert files[0].read_bytes() == b"foo\r\n"
    entries = store.list_replaces()
    assert len(entries) == 1
    assert entries[0]["replace_id"] in error
    if rollback_fails:
        assert files[1].read_bytes() == b"b"
        assert "戻せなかった" in error
        assert _undo(mode, entries[0]["replace_id"], capsys, force=True)[0] == 0
    else:
        assert files[1].read_bytes() == b"foo\r\n"
    assert [file.read_bytes() for file in files] == [b"foo\r\n", b"foo\r\n"]


@pytest.mark.parametrize("mode", ["cli", "mcp"])
def test_undo_partial_write_restores_start_state_and_allows_retry(
    mode: str, tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    files = [tmp_path / "a.txt", tmp_path / "b.txt"]
    for file in files:
        file.write_bytes(b"foo\r\n")
    rc, replace_id, _ = _replace(mode, files, capsys)
    assert rc == 0 and replace_id is not None

    def _write(path: pathlib.Path, data: bytes) -> int:
        if path.resolve() == files[1] and data == b"foo\r\n":
            _write_original(path, b"f")
            raise OSError("undoの部分書き込み失敗を注入")
        return _write_original(path, data)

    with monkeypatch.context() as patch:
        patch.setattr(pathlib.Path, "write_bytes", _write)
        rc, error = _undo(mode, replace_id, capsys)
    assert rc == 1 and "開始前の状態へ戻しました" in error
    assert [file.read_bytes() for file in files] == [b"bar\r\n", b"bar\r\n"]
    assert _undo(mode, replace_id, capsys)[0] == 0
    assert [file.read_bytes() for file in files] == [b"foo\r\n", b"foo\r\n"]
