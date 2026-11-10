import inspect
import json
import pathlib
import typing

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

_ToolError = mcp.server.mcpserver.exceptions.ToolError

pytestmark = pytest.mark.usefixtures("mcp_cache")


@pytest.mark.asyncio
async def test_tool_grep_finds_matches(tmp_path: pathlib.Path) -> None:
    """`tool_grep`が指定ファイル群から正しくマッチを抽出すること。"""
    target = tmp_path / "sample.txt"
    target.write_text("hello world\nfoo bar\nhello again\n", encoding="utf-8")

    result = await pyfltr.cli.mcp_grep_tools.tool_grep(
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

    result = await pyfltr.cli.mcp_grep_tools.tool_grep(
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

    result = await pyfltr.cli.mcp_grep_tools.tool_grep(paths=[str(target)], pattern="")

    assert result.total_matches > 0
    assert result.matches[0].match_text == ""


@pytest.mark.asyncio
async def test_tool_grep_reads_pattern_file(tmp_path: pathlib.Path) -> None:
    """パターンファイルの各行を検索パターンとして使用する。"""
    target = tmp_path / "sample.txt"
    target.write_text("alpha\nbeta\ngamma\n", encoding="utf-8")
    pattern_file = tmp_path / "patterns.txt"
    pattern_file.write_text("alpha\ngamma\n", encoding="utf-8")

    result = await pyfltr.cli.mcp_grep_tools.tool_grep(
        paths=[str(target)],
        pattern_file=str(pattern_file),
    )

    assert result.total_matches == 2
    assert {match.match_text for match in result.matches} == {"alpha", "gamma"}


@pytest.mark.asyncio
async def test_tool_grep_rejects_missing_pattern_with_argument_names(tmp_path: pathlib.Path) -> None:
    """パターン未指定の拒否では、指定に使える引数名を案内する。"""
    with pytest.raises(_ToolError, match="`pattern`・`patterns`・`pattern_file`"):
        await pyfltr.cli.mcp_grep_tools.tool_grep(paths=[str(tmp_path)])


@pytest.mark.asyncio
async def test_tool_grep_rejects_missing_pattern_file(tmp_path: pathlib.Path) -> None:
    """実在しないパターンファイルを拒否する。"""
    with pytest.raises(_ToolError, match="パターンファイル"):
        await pyfltr.cli.mcp_grep_tools.tool_grep(
            paths=[str(tmp_path)],
            pattern_file=str(tmp_path / "missing.txt"),
        )


def test_tool_grep_max_total_default_is_none() -> None:
    """全体上限の未指定状態を明示値と区別する。"""
    signature = inspect.signature(pyfltr.cli.mcp_grep_tools.tool_grep)
    assert signature.parameters["max_total"].default is None


def test_tool_grep_max_preview_chars_default_is_none() -> None:
    """本文上限の未指定状態を明示した既定値200と区別する。"""
    signature = inspect.signature(pyfltr.cli.mcp_grep_tools.tool_grep)
    assert signature.parameters["max_preview_chars"].default is None


@pytest.mark.asyncio
async def test_tool_grep_context_applies_to_both_directions(tmp_path: pathlib.Path) -> None:
    """一括コンテキスト値をマッチ行の前後へ適用する。"""
    target = tmp_path / "sample.txt"
    target.write_text("before 2\nbefore 1\nmatch\nafter 1\nafter 2\n", encoding="utf-8")

    result = await pyfltr.cli.mcp_grep_tools.tool_grep(
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

    result = await pyfltr.cli.mcp_grep_tools.tool_grep(
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

    result = await pyfltr.cli.mcp_grep_tools.tool_grep(
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

    result = await pyfltr.cli.mcp_grep_tools.tool_grep(
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

    result = await pyfltr.cli.mcp_grep_tools.tool_grep(
        pattern="hello",
        paths=[str(target)],
        max_total=5,
    )

    assert result.total_matches <= 5
    assert len(result.matches) <= 5


@pytest.mark.asyncio
async def test_tool_grep_truncates_long_line_and_warns(tmp_path: pathlib.Path) -> None:
    """巨大な単一行を有界化し、切り詰め情報と警告を返す。"""
    target = tmp_path / "long.txt"
    target.write_text("x" * 250 + "needle" + "y" * 250 + "\n", encoding="utf-8")

    result = await pyfltr.cli.mcp_grep_tools.tool_grep(
        pattern="needle",
        paths=[str(target)],
    )

    match = result.matches[0]
    assert len(match.line_text) <= pyfltr.grep_.preview.DEFAULT_MAX_PREVIEW_CHARS
    assert match.truncated == ["line_text"]
    assert match.line_text_offset > 0
    assert len(result.warnings) == 1
    assert "max_preview_chars=0" in result.warnings[0]


@pytest.mark.asyncio
async def test_tool_grep_zero_preview_limit_returns_full_text(tmp_path: pathlib.Path) -> None:
    """`max_preview_chars=0`は本文を切り詰めず警告も返さない。"""
    target = tmp_path / "long.txt"
    line_text = "x" * 250 + "needle" + "y" * 250
    target.write_text(line_text + "\n", encoding="utf-8")

    result = await pyfltr.cli.mcp_grep_tools.tool_grep(
        pattern="needle",
        paths=[str(target)],
        max_preview_chars=0,
    )

    assert result.matches[0].line_text == line_text
    assert not result.matches[0].truncated
    assert result.matches[0].line_text_offset == 0
    assert not result.warnings


@pytest.mark.asyncio
async def test_tool_grep_summary_count_is_independent_of_preview_limit(tmp_path: pathlib.Path) -> None:
    """集計は本文プレビュー上限を適用せず全文のマッチ件数を返す。"""
    target = tmp_path / "long.txt"
    target.write_text(("x" * 300 + "needle\n") * 3, encoding="utf-8")

    limited = await pyfltr.cli.mcp_grep_tools.tool_grep(
        pattern="needle",
        paths=[str(target)],
        summary_mode="count",
        max_preview_chars=10,
    )
    unlimited = await pyfltr.cli.mcp_grep_tools.tool_grep(
        pattern="needle",
        paths=[str(target)],
        summary_mode="count",
        max_preview_chars=0,
    )

    assert limited.file_counts == unlimited.file_counts
    assert limited.total_matches == unlimited.total_matches == 3
    assert not limited.warnings
    assert not unlimited.warnings


@pytest.mark.asyncio
async def test_tool_grep_truncates_multiline_match_text(tmp_path: pathlib.Path) -> None:
    """マルチライン一致の`match_text`を上限以内にし、対象フィールドを通知する。"""
    target = tmp_path / "multiline.txt"
    target.write_text("start\n" + "x" * 300 + "\nend\n", encoding="utf-8")

    result = await pyfltr.cli.mcp_grep_tools.tool_grep(
        pattern="start.*end",
        paths=[str(target)],
        multiline=True,
        max_preview_chars=20,
    )

    match = result.matches[0]
    assert len(match.match_text) == 20
    assert match.line_text == "start"
    assert match.truncated == ["match_text"]
    assert match.line_text_offset == 0
    assert len(result.warnings) == 1


@pytest.mark.asyncio
async def test_tool_grep_preserves_newline_started_match_origin(tmp_path: pathlib.Path) -> None:
    """改行から始まるマッチの原位置・本文上限・一致文字列・警告を返す。"""
    target = tmp_path / "multiline.txt"
    target.write_text("x" * 500 + "\nneedle\n", encoding="utf-8")

    result = await pyfltr.cli.mcp_grep_tools.tool_grep(
        pattern="\nneedle",
        paths=[str(target)],
        multiline=True,
        max_preview_chars=200,
    )

    match = result.matches[0]
    assert match.line == 1
    assert match.col == 501
    assert len(match.line_text) == 200
    assert match.line_text_offset == 300
    assert match.col - match.line_text_offset == len(match.line_text) + 1
    assert match.match_text == "\nneedle"
    assert match.truncated == ["line_text"]
    assert len(result.warnings) == 1


@pytest.mark.asyncio
async def test_tool_grep_summary_files_with_matches(tmp_path: pathlib.Path) -> None:
    """マッチを含むファイルだけを集計して個別マッチを省略する。"""
    matched = tmp_path / "matched.txt"
    matched.write_text("hello\n", encoding="utf-8")
    unmatched = tmp_path / "unmatched.txt"
    unmatched.write_text("goodbye\n", encoding="utf-8")

    result = await pyfltr.cli.mcp_grep_tools.tool_grep(
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

    result = await pyfltr.cli.mcp_grep_tools.tool_grep(
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

    result = await pyfltr.cli.mcp_grep_tools.tool_grep(
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

    with pytest.raises(_ToolError, match="max_total"):
        await pyfltr.cli.mcp_grep_tools.tool_grep(
            paths=[str(target)],
            pattern="hello",
            summary_mode="files_without_match",
            max_total=1,
        )


@pytest.mark.asyncio
async def test_tool_grep_rejects_invalid_summary_mode(tmp_path: pathlib.Path) -> None:
    """未対応の集計モードを拒否する。"""
    with pytest.raises(_ToolError, match="summary_mode"):
        await pyfltr.cli.mcp_grep_tools.tool_grep(
            paths=[str(tmp_path)],
            pattern="hello",
            summary_mode=typing.cast(typing.Any, "invalid"),
        )


@pytest.mark.asyncio
async def test_tool_grep_includes_hidden_files(tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """`tool_grep`がドット始まりファイルも対象に含めること（run系と統一）。"""
    hidden = tmp_path / ".hidden.py"
    hidden.write_text("foo here\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    result = await pyfltr.cli.mcp_grep_tools.tool_grep(pattern="foo", paths=[str(tmp_path)])
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
    first = await pyfltr.cli.mcp_grep_tools.tool_grep(pattern="foo", paths=[str(lock)])
    assert first.fully_excluded_files == ["uv.lock"]
    second = await pyfltr.cli.mcp_grep_tools.tool_grep(pattern="foo", paths=[str(normal)])
    assert not second.fully_excluded_files
    # 不在パスはmissing_targetsで通知し、前リクエストの除外は混入しない
    third = await pyfltr.cli.mcp_grep_tools.tool_grep(pattern="foo", paths=[str(tmp_path / "nope.py")])
    assert third.missing_targets == ["nope.py"]
    assert not third.fully_excluded_files


def test_tool_replace_dry_run_default() -> None:
    """`tool_replace`の`dry_run`引数の既定値が`True`であること。"""
    sig = inspect.signature(pyfltr.cli.mcp_grep_tools.tool_replace)
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
    excluded = await pyfltr.cli.mcp_grep_tools.tool_replace(pattern="foo", replacement="baz", paths=[str(lock)])
    assert excluded.fully_excluded_files == ["uv.lock"]
    assert excluded.files_changed == 0
    assert lock.read_text(encoding="utf-8") == "foo\n"
    # 不在パスはmissing_targetsで通知し、前リクエストの除外は混入しない
    missing = await pyfltr.cli.mcp_grep_tools.tool_replace(pattern="foo", replacement="baz", paths=[str(tmp_path / "nope.py")])
    assert missing.missing_targets == ["nope.py"]
    assert not missing.fully_excluded_files


@pytest.mark.asyncio
async def test_tool_replace_dry_run_does_not_write(tmp_path: pathlib.Path) -> None:
    """`tool_replace(dry_run=True)`がファイルを変更しないこと。"""
    target = tmp_path / "sample.txt"
    original = "hello world\n"
    target.write_text(original, encoding="utf-8")

    result = await pyfltr.cli.mcp_grep_tools.tool_replace(
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

    result = await pyfltr.cli.mcp_grep_tools.tool_replace(
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

    result = await pyfltr.cli.mcp_grep_tools.tool_replace(
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
    with pytest.raises(_ToolError, match="multiline"):
        await pyfltr.cli.mcp_grep_tools.tool_replace(
            pattern="foo", replacement="X", paths=[str(target)], within="KEY", multiline=True
        )


@pytest.mark.asyncio
async def test_tool_replace_context_without_within_raises(tmp_path: pathlib.Path) -> None:
    """`within`未指定で`before_context`/`after_context`指定はValueErrorになる。"""
    target = tmp_path / "sample.txt"
    target.write_text("foo\n", encoding="utf-8")
    with pytest.raises(_ToolError, match="within"):
        await pyfltr.cli.mcp_grep_tools.tool_replace(pattern="foo", replacement="X", paths=[str(target)], after_context=1)


@pytest.mark.asyncio
async def test_tool_replace_combined_context_without_within_raises(tmp_path: pathlib.Path) -> None:
    """`within`未指定の一括コンテキスト値を拒否する。"""
    target = tmp_path / "sample.txt"
    target.write_text("foo\n", encoding="utf-8")
    with pytest.raises(_ToolError, match="within"):
        await pyfltr.cli.mcp_grep_tools.tool_replace(
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

    result = await pyfltr.cli.mcp_grep_tools.tool_replace(
        pattern="foo",
        replacement="X",
        paths=[str(tmp_path)],
        from_grep=str(grep_output),
    )

    assert result.files_changed == 1
    assert [entry.file for entry in result.file_changes] == [allowed.as_posix()]


@pytest.mark.asyncio
async def test_tool_replace_rejects_adaptively_compressed_from_grep(tmp_path: pathlib.Path) -> None:
    """省略済みgrep出力をMCP置換でも拒否する。"""
    target = tmp_path / "target.txt"
    target.write_text("foo\n", encoding="utf-8")
    grep_output = tmp_path / "grep.jsonl"
    grep_output.write_text(
        json.dumps({"kind": "summary", "output_mode": "counts"}) + "\n",
        encoding="utf-8",
    )

    with pytest.raises(_ToolError, match="full出力"):
        await pyfltr.cli.mcp_grep_tools.tool_replace(
            pattern="foo",
            replacement="X",
            paths=[str(tmp_path)],
            from_grep=str(grep_output),
        )

    assert target.read_text(encoding="utf-8") == "foo\n"


@pytest.mark.asyncio
async def test_tool_replace_rejects_missing_from_grep_file(tmp_path: pathlib.Path) -> None:
    """読み込めないgrep出力を拒否する。"""
    with pytest.raises(_ToolError, match="from-grep"):
        await pyfltr.cli.mcp_grep_tools.tool_replace(
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

    result = await pyfltr.cli.mcp_grep_tools.tool_replace(
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

    result = await pyfltr.cli.mcp_grep_tools.tool_replace(
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
    with pytest.raises(_ToolError, match="paths"):
        await pyfltr.cli.mcp_grep_tools.tool_replace(
            pattern="hello",
            replacement="goodbye",
            paths=[],
        )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("original", "expected", "encoding"),
    [
        ("foo 日本語\r\n".encode("cp932"), "bar 日本語\r\n".encode("cp932"), "cp932"),
        (b"\xef\xbb\xbffoo\r\n", b"\xef\xbb\xbfbar\r\n", "utf-8-sig"),
    ],
)
async def test_tool_replace_preserves_bytes_round_trip(
    tmp_path: pathlib.Path, original: bytes, expected: bytes, encoding: str
) -> None:
    """MCP置換と取り消しで文字コード・BOM・改行を保持する。"""
    target = tmp_path / "sample.txt"
    target.write_bytes(original)
    replaced = await pyfltr.cli.mcp_grep_tools.tool_replace(
        pattern="foo", replacement="bar", paths=[str(target)], encoding=encoding, dry_run=False
    )
    assert target.read_bytes() == expected
    assert replaced.replace_id is not None
    undone = await pyfltr.cli.mcp_grep_tools.tool_replace_undo(replaced.replace_id)
    assert target.read_bytes() == original
    assert undone.exit_code == 0
    assert not undone.warnings


@pytest.mark.asyncio
async def test_tool_replace_within_preserves_bytes_round_trip(tmp_path: pathlib.Path) -> None:
    """MCPのブロック内置換は範囲外と混在改行を保持する。"""
    target = tmp_path / "sample.txt"
    original = b"foo\r\nanchor foo\nfoo\r"
    target.write_bytes(original)
    replaced = await pyfltr.cli.mcp_grep_tools.tool_replace(
        pattern="foo", replacement="bar", paths=[str(target)], within="anchor", dry_run=False
    )
    assert target.read_bytes() == b"foo\r\nanchor bar\nfoo\r"
    assert replaced.replace_id is not None
    undone = await pyfltr.cli.mcp_grep_tools.tool_replace_undo(replaced.replace_id)
    assert undone.exit_code == 0
    assert target.read_bytes() == original


@pytest.mark.parametrize(
    ("original", "pattern", "expected"),
    [
        (b"keep\r\nfoo\r\nlast\r", "foo\n", b"keep\r\nbar\nlast\r"),
        (b"keep\r\nfoo\rlast\r\n", "foo\n", b"keep\r\nbar\nlast\r\n"),
    ],
)
@pytest.mark.asyncio
async def test_tool_replace_searches_normalized_newlines(
    tmp_path: pathlib.Path, original: bytes, pattern: str, expected: bytes
) -> None:
    """MCPの通常置換はCRLFと単独CRをLFとして検索し、対象外の改行を保つ。"""
    target = tmp_path / "sample.txt"
    target.write_bytes(original)
    replaced = await pyfltr.cli.mcp_grep_tools.tool_replace(
        pattern=pattern, replacement="bar\n", paths=[str(target)], dry_run=False
    )
    assert target.read_bytes() == expected
    assert replaced.replace_id is not None
    undone = await pyfltr.cli.mcp_grep_tools.tool_replace_undo(replaced.replace_id)
    assert undone.exit_code == 0
    assert target.read_bytes() == original


@pytest.mark.asyncio
async def test_tool_replace_carriage_return_pattern_does_not_match(tmp_path: pathlib.Path) -> None:
    """MCPも従来どおりLF正規化後に照合し、CRを探す式は一致しない。"""
    target = tmp_path / "sample.txt"
    target.write_bytes(b"foo\r\n")
    result = await pyfltr.cli.mcp_grep_tools.tool_replace(pattern=r"\r", replacement="X", paths=[str(target)], dry_run=False)
    assert result.files_changed == 0
    assert target.read_bytes() == b"foo\r\n"


@pytest.mark.parametrize(
    ("original", "pattern", "ignore_case", "multiline", "line_regexp", "within", "after_context", "expected"),
    [
        (b"foo\r\n", "foo.*", False, False, False, None, 0, b"bar\r\n"),
        (b"FOO\r\nkeep\r\n", "foo", True, False, False, None, 0, b"bar\r\nkeep\r\n"),
        (b"foo\r\nkeep\r\n", "^foo$", False, True, False, None, 0, b"bar\r\nkeep\r\n"),
        (b"FOO\rkeep\r", "^foo$", True, True, False, None, 0, b"bar\rkeep\r"),
        (b"FOO\r\n", "foo", True, False, True, None, 0, b"bar\r\n"),
        (b"foo\rkeep\r", "foo", False, True, True, None, 0, b"bar\rkeep\r"),
        (b"lead\r\nANCHOR\r\nFOO\r\n", "foo", True, False, False, "anchor", 1, b"lead\r\nANCHOR\r\nbar\r\n"),
    ],
)
@pytest.mark.asyncio
async def test_tool_replace_legacy_regex_option_results(
    tmp_path: pathlib.Path,
    original: bytes,
    pattern: str,
    ignore_case: bool,
    multiline: bool,
    line_regexp: bool,
    within: str | None,
    after_context: int,
    expected: bytes,
) -> None:
    """MCPの主要オプションも従来のLF正規化後と同じ対象を置換する。"""
    target = tmp_path / "sample.txt"
    target.write_bytes(original)
    replaced = await pyfltr.cli.mcp_grep_tools.tool_replace(
        pattern=pattern,
        replacement="bar",
        paths=[str(target)],
        ignore_case=ignore_case,
        multiline=multiline,
        line_regexp=line_regexp,
        within=within,
        after_context=after_context,
        dry_run=False,
    )
    assert target.read_bytes() == expected
    assert replaced.replace_id is not None
    undone = await pyfltr.cli.mcp_grep_tools.tool_replace_undo(replaced.replace_id)
    assert undone.exit_code == 0
    assert target.read_bytes() == original


@pytest.mark.asyncio
async def test_tool_replace_matched_newline_uses_replacement_representation(tmp_path: pathlib.Path) -> None:
    """MCPで一致した元のCRLFは、置換式に明記したCRLFへ置き換わる。"""
    target = tmp_path / "sample.txt"
    original = b"head\r\nfoo\r\nbar\rkeep\r\n"
    target.write_bytes(original)
    replaced = await pyfltr.cli.mcp_grep_tools.tool_replace(
        pattern="foo.bar", replacement="X\r\nY", paths=[str(target)], multiline=True, dry_run=False
    )
    assert target.read_bytes() == b"head\r\nX\r\nY\rkeep\r\n"
    assert replaced.replace_id is not None
    undone = await pyfltr.cli.mcp_grep_tools.tool_replace_undo(replaced.replace_id)
    assert undone.exit_code == 0
    assert target.read_bytes() == original


@pytest.mark.asyncio
async def test_tool_replace_file_changes_omit_hashes(tmp_path: pathlib.Path) -> None:
    """replaceのファイル別変更は対象と件数だけを返し、取り消し照合用のハッシュをエージェントへ渡さない。"""
    target = tmp_path / "sample.txt"
    target.write_bytes(b"foo\r\nkeep\r\n")
    result = await pyfltr.cli.mcp_grep_tools.tool_replace(pattern="foo", replacement="bar", paths=[str(target)], dry_run=False)
    change = result.file_changes[0].model_dump()
    assert change == {"file": pyfltr.paths.normalize_separators(str(target)), "count": 1}
    assert result.replace_id is not None


@pytest.mark.parametrize(
    ("original", "pattern", "expected"),
    [
        (b"foo\r\nanchor\r\nfoo\r\nfoo\r", "foo\n", b"foo\r\nanchor\r\nbar\nfoo\r"),
        (b"foo\ranchor\rfoo\rfoo\r\n", "foo\n", b"foo\ranchor\rbar\nfoo\r\n"),
    ],
)
@pytest.mark.asyncio
async def test_tool_replace_within_searches_normalized_newlines(
    tmp_path: pathlib.Path, original: bytes, pattern: str, expected: bytes
) -> None:
    """MCPの行範囲は検索用のLFで区切り、範囲外の改行バイトを保つ。"""
    target = tmp_path / "sample.txt"
    target.write_bytes(original)
    replaced = await pyfltr.cli.mcp_grep_tools.tool_replace(
        pattern=pattern, replacement="bar\n", paths=[str(target)], within="anchor", after_context=1, dry_run=False
    )
    assert target.read_bytes() == expected
    assert replaced.replace_id is not None
    undone = await pyfltr.cli.mcp_grep_tools.tool_replace_undo(replaced.replace_id)
    assert undone.exit_code == 0
    assert target.read_bytes() == original


@pytest.mark.parametrize(
    ("original", "pattern", "expected"),
    [
        (b"foo\r\nkeep\r\n", "foo", b"bar\r\nkeep\r\n"),
        (b"foo\rkeep\r", "foo", b"bar\rkeep\r"),
        (b"foo\r\nkeep\r\n", "foo.*", b"bar\r\nkeep\r\n"),
        (b"foo\rkeep\r", "foo.*", b"bar\rkeep\r"),
    ],
)
@pytest.mark.asyncio
async def test_tool_replace_line_regexp_preserves_original_newlines(
    tmp_path: pathlib.Path, original: bytes, pattern: str, expected: bytes
) -> None:
    """MCPのline_regexpはCRLFと単独CRの行全体へ一致する。"""
    target = tmp_path / "sample.txt"
    target.write_bytes(original)
    replaced = await pyfltr.cli.mcp_grep_tools.tool_replace(
        pattern=pattern, replacement="bar", paths=[str(target)], line_regexp=True, dry_run=False
    )
    assert target.read_bytes() == expected
    assert replaced.replace_id is not None
    undone = await pyfltr.cli.mcp_grep_tools.tool_replace_undo(replaced.replace_id)
    assert undone.exit_code == 0
    assert target.read_bytes() == original


@pytest.mark.parametrize(
    ("original", "expected"),
    [
        (b"foo\r\nanchor\r\nfoo\r\nkeep\r\n", b"foo\r\nanchor\r\nbar\r\nkeep\r\n"),
        (b"foo\ranchor\rfoo\rkeep\r", b"foo\ranchor\rbar\rkeep\r"),
    ],
)
@pytest.mark.asyncio
async def test_tool_replace_within_line_regexp_preserves_original_newlines(
    tmp_path: pathlib.Path, original: bytes, expected: bytes
) -> None:
    """MCPのline_regexp付きwithinはCRLFと単独CRのアンカーを見失わない。"""
    target = tmp_path / "sample.txt"
    target.write_bytes(original)
    replaced = await pyfltr.cli.mcp_grep_tools.tool_replace(
        pattern="foo", replacement="bar", paths=[str(target)], line_regexp=True, within="anchor", after_context=1, dry_run=False
    )
    assert target.read_bytes() == expected
    assert replaced.replace_id is not None
    undone = await pyfltr.cli.mcp_grep_tools.tool_replace_undo(replaced.replace_id)
    assert undone.exit_code == 0
    assert target.read_bytes() == original


@pytest.mark.asyncio
async def test_tool_replace_dry_run_preserves_bytes(tmp_path: pathlib.Path) -> None:
    """MCPのdry-runはBOMと改行を含むファイルを変更しない。"""
    target = tmp_path / "sample.txt"
    original = b"\xef\xbb\xbffoo\r\n"
    target.write_bytes(original)
    result = await pyfltr.cli.mcp_grep_tools.tool_replace(pattern="foo", replacement="bar", paths=[str(target)])
    assert result.dry_run
    assert result.replace_id is None
    assert target.read_bytes() == original


@pytest.mark.asyncio
async def test_tool_replace_undo_detects_byte_only_edit(tmp_path: pathlib.Path) -> None:
    """BOMだけの手動編集を検出し、force時に元バイトへ戻す。"""
    target = tmp_path / "sample.txt"
    original = b"\xef\xbb\xbffoo\r\n"
    target.write_bytes(original)
    replaced = await pyfltr.cli.mcp_grep_tools.tool_replace(
        pattern="foo", replacement="bar", paths=[str(target)], dry_run=False
    )
    assert replaced.replace_id is not None
    target.write_bytes(b"bar\r\n")
    skipped = await pyfltr.cli.mcp_grep_tools.tool_replace_undo(replaced.replace_id)
    assert skipped.exit_code == 1
    assert target.read_bytes() == b"bar\r\n"
    restored = await pyfltr.cli.mcp_grep_tools.tool_replace_undo(replaced.replace_id, force=True)
    assert restored.exit_code == 0
    assert target.read_bytes() == original


@pytest.mark.asyncio
async def test_tool_replace_legacy_history_warning(tmp_path: pathlib.Path) -> None:
    """旧履歴は保存encodingで復元し、その操作だけへ警告を返す。"""
    target = tmp_path / "sample.txt"
    target.write_bytes("bar 日本語\n".encode("cp932"))
    store = pyfltr.grep_.history.ReplaceHistoryStore()
    replace_id = pyfltr.grep_.history.generate_replace_id()
    store.save_replace(
        replace_id,
        command_meta=pyfltr.grep_.types.ReplaceCommandMeta(
            replace_id=replace_id, dry_run=False, fixed_strings=False, pattern="foo", replacement="bar", encoding="cp932"
        ),
        file_changes=[
            {"file": target, "before_content": "foo 日本語\n", "after_hash": pyfltr.grep_.replacer.compute_hash("bar 日本語\n")}
        ],
    )
    listed = await pyfltr.cli.mcp_grep_tools.tool_replace_history(action="list")
    assert any(entry.replace_id == replace_id for entry in listed.entries)
    shown = await pyfltr.cli.mcp_grep_tools.tool_replace_history(action="show", replace_id=replace_id)
    assert shown.entries[0].replace_id == replace_id
    undone = await pyfltr.cli.mcp_grep_tools.tool_replace_undo(replace_id)
    assert undone.exit_code == 0
    assert target.read_text(encoding="cp932") == "foo 日本語\n"
    assert len(undone.warnings) == 1
    assert "旧形式" in undone.warnings[0]


@pytest.mark.asyncio
async def test_tool_replace_undo_restores_file(tmp_path: pathlib.Path) -> None:
    """`tool_replace_undo`が`replace_id`から正常に復元できること。"""
    target = tmp_path / "sample.txt"
    original = "hello world\n"
    target.write_text(original, encoding="utf-8")

    # まず実書き込みを行い replace_id を取得する
    replace_result = await pyfltr.cli.mcp_grep_tools.tool_replace(
        pattern="hello",
        replacement="goodbye",
        paths=[str(target)],
        dry_run=False,
    )
    assert replace_result.replace_id is not None
    assert target.read_text(encoding="utf-8") == "goodbye world\n"

    # undo を実行して元に戻す
    undo_result = await pyfltr.cli.mcp_grep_tools.tool_replace_undo(
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

    replace_result = await pyfltr.cli.mcp_grep_tools.tool_replace(
        pattern="hello",
        replacement="goodbye",
        paths=[str(target)],
        dry_run=False,
    )
    assert replace_result.replace_id is not None

    # replace後にファイルを手動で編集する（ハッシュ不一致を発生させる）
    target.write_text("manually edited\n", encoding="utf-8")

    undo_result = await pyfltr.cli.mcp_grep_tools.tool_replace_undo(
        replace_id=replace_result.replace_id,
        force=False,
    )

    assert target.as_posix() in undo_result.skipped
    assert not undo_result.restored
    assert undo_result.exit_code == 1
    # CLIと同じく、スキップの理由と強制復元の指定方法を応答の警告で示す
    assert any("`force=True`" in warning for warning in undo_result.warnings)


@pytest.mark.asyncio
async def test_tool_replace_undo_hash_mismatch_force_restores(tmp_path: pathlib.Path) -> None:
    """`force=True`のときハッシュ不一致でも復元されること。"""
    target = tmp_path / "sample.txt"
    original = "hello world\n"
    target.write_text(original, encoding="utf-8")

    replace_result = await pyfltr.cli.mcp_grep_tools.tool_replace(
        pattern="hello",
        replacement="goodbye",
        paths=[str(target)],
        dry_run=False,
    )
    assert replace_result.replace_id is not None

    # replace後にファイルを手動で編集する
    target.write_text("manually edited\n", encoding="utf-8")

    undo_result = await pyfltr.cli.mcp_grep_tools.tool_replace_undo(
        replace_id=replace_result.replace_id,
        force=True,
    )

    assert target.read_text(encoding="utf-8") == original
    assert target.as_posix() in undo_result.restored
    assert not undo_result.skipped
    assert undo_result.exit_code == 0


@pytest.mark.asyncio
async def test_tool_replace_reports_unreadable_file_warning_without_changing_exit_code(tmp_path: pathlib.Path) -> None:
    """読み込めないファイルは対処付きの警告で通知し、`exit_code`の意味は変えない。"""
    (tmp_path / "ok.txt").write_text("needle\n", encoding="utf-8")
    (tmp_path / "bad.txt").write_bytes(b"\x81needle\n")

    result = await pyfltr.cli.mcp_grep_tools.tool_replace(
        pattern="needle", replacement="x", paths=[str(tmp_path)], dry_run=True, no_gitignore=True
    )

    assert result.exit_code == 0
    assert any("bad.txt" in warning and "対処:" in warning and "`encoding`" in warning for warning in result.warnings)


@pytest.mark.asyncio
async def test_tool_replace_undo_not_found_raises() -> None:
    """`replace_id`が存在しない場合は、有効なIDを確認する手段を示してエラーにすること。"""
    with pytest.raises(_ToolError, match="`replace_history`ツール"):
        await pyfltr.cli.mcp_grep_tools.tool_replace_undo(replace_id="NONEXISTENTID00000000000000")


@pytest.mark.asyncio
async def test_tool_replace_history_lists_and_shows_without_file_contents(tmp_path: pathlib.Path) -> None:
    """保存済み履歴を一覧・単体参照し、変更前本文と置換レコードを応答から除外する。"""
    target = tmp_path / "sample.txt"
    target.write_text("hello world\n", encoding="utf-8")
    replaced = await pyfltr.cli.mcp_grep_tools.tool_replace(
        pattern="hello",
        replacement="goodbye",
        paths=[str(target)],
        dry_run=False,
    )
    assert replaced.replace_id is not None

    listed = await pyfltr.cli.mcp_grep_tools.tool_replace_history()
    assert listed.action == "list"
    assert len(listed.entries) == 1
    assert listed.entries[0].replace_id == replaced.replace_id
    # 履歴の`file`は保存時に区切りを`/`へ統一した表現である。
    # Windowsでも`C:/...`形式になるため、`as_posix()`での文字列比較で一致を検証する。
    assert listed.entries[0].files[0].file == target.as_posix()
    assert listed.entries[0].files[0].records_count == 1

    shown = await pyfltr.cli.mcp_grep_tools.tool_replace_history(action="show", replace_id=replaced.replace_id)
    payload = shown.model_dump()
    assert shown.action == "show"
    assert len(shown.entries) == 1
    assert "before_content" not in json.dumps(payload)
    assert "records" not in payload["entries"][0]["files"][0]


@pytest.mark.asyncio
async def test_tool_replace_history_empty() -> None:
    """履歴が存在しない場合は空の一覧を返す。"""
    result = await pyfltr.cli.mcp_grep_tools.tool_replace_history()
    assert result.action == "list"
    assert not result.entries


@pytest.mark.asyncio
async def test_tool_replace_history_rejects_invalid_requests() -> None:
    """未対応actionと識別子のない単体参照を拒否する。"""
    with pytest.raises(_ToolError, match="action"):
        await pyfltr.cli.mcp_grep_tools.tool_replace_history(action=typing.cast(typing.Any, "invalid"))
    with pytest.raises(_ToolError, match="replace_id"):
        await pyfltr.cli.mcp_grep_tools.tool_replace_history(action="show")


@pytest.mark.asyncio
async def test_tool_replace_history_show_rejects_unknown_id() -> None:
    """存在しないreplace識別子を拒否する。"""
    with pytest.raises(_ToolError, match="replace_id"):
        await pyfltr.cli.mcp_grep_tools.tool_replace_history(action="show", replace_id="unknown")


@pytest.mark.asyncio
async def test_new_tools_keep_stdout_clean(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """新設3ツールはstdioトランスポート用のstdoutへ出力しない。"""
    monkeypatch.chdir(tmp_path)
    (tmp_path / "pyproject.toml").write_text("[tool.pyfltr]\n", encoding="utf-8")

    await pyfltr.cli.mcp_grep_tools.tool_replace_history()
    await pyfltr.cli.mcp_server.tool_command_info("ec")
    await pyfltr.cli.mcp_server.tool_config(action="list")

    captured = capsys.readouterr()
    assert captured.out == ""
