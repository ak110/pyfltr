"""grepの適応的な出力選択のテスト。"""

import json
import pathlib
import typing

import pytest

import pyfltr.cli.main
import pyfltr.cli.mcp_server
import pyfltr.grep_.adaptive


def _match(file: str, index: int, *, width: int = 80) -> dict[str, typing.Any]:
    return {
        "file": file,
        "line": index + 1,
        "col": 1,
        "end_col": 4,
        "match_text": "foo",
        "line_text": f"foo {index:04d} " + "x" * width,
    }


@pytest.mark.parametrize("dense_name", ["many.txt", "uv.lock"])
def test_select_output_collapses_only_high_density_file(dense_name: str) -> None:
    """300・1・2・1件の分布では高密度ファイルだけを件数表示へ縮約する。"""
    matches = [*[_match(dense_name, index) for index in range(300)]]
    matches.extend(_match("one.txt", index) for index in range(1))
    matches.extend(_match("two.txt", index) for index in range(2))
    matches.extend(_match("last.txt", index) for index in range(1))

    selection = pyfltr.grep_.adaptive.select_output(matches, output_format="json")

    assert selection.output_mode == "mixed"
    assert selection.total_matches == 304
    assert selection.returned_matches == 4
    assert selection.omitted_matches == 300
    assert selection.omitted_files == 0
    by_file = {result["file"]: result for result in selection.file_results}
    assert by_file[dense_name] == {"file": dense_name, "count": 300}
    assert len(by_file["one.txt"]["matches"]) == 1
    assert len(by_file["two.txt"]["matches"]) == 2
    assert len(by_file["last.txt"]["matches"]) == 1


def test_select_output_keeps_full_result_at_budget_boundary() -> None:
    """全件形式が予算と同じ長さならfullを維持する。"""
    matches = [_match("a.txt", 0)]
    budget = pyfltr.grep_.adaptive.serialized_length("full", matches, [], output_format="jsonl")

    selection = pyfltr.grep_.adaptive.select_output(matches, output_format="jsonl", budget=budget)

    assert selection.output_mode == "full"
    assert selection.matches == matches


def test_text_grouped_reports_count_and_representative_rows() -> None:
    """groupedのtext表示は省略前件数と代表行数をファイルごとに示す。"""
    matches = [_match("many.txt", index) for index in range(300)]

    selection = pyfltr.grep_.adaptive.select_output(matches, output_format="text", budget=180)
    lines = pyfltr.grep_.adaptive.text_result_lines(selection)

    assert selection.output_mode == "grouped"
    assert lines[0] == "many.txt:300 match(es) (1 representative match(es) shown)"
    assert lines[1:] == ["many.txt:1:1:foo 0000 " + "x" * 80]


def test_cli_agent_default_uses_mixed_output_for_dense_file(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """エージェント環境のCLIは高密度ファイルだけを縮約し、低密度本文を保持する。"""
    dense = tmp_path / "uv.lock"
    dense.write_text("".join(f"foo {index:04d} {'x' * 80}\n" for index in range(300)), encoding="utf-8")
    sparse = tmp_path / "note.txt"
    sparse.write_text("foo retained\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("AI_AGENT", "1")

    rc = pyfltr.cli.main.run(
        [
            "grep",
            "foo",
            "--output-format=json",
            "--no-exclude",
            "--no-gitignore",
            str(tmp_path),
        ]
    )

    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["output_mode"] == "mixed"
    assert payload["summary"]["total_matches"] == 301
    by_file = {result["file"]: result for result in payload["file_results"]}
    assert by_file["uv.lock"] == {"file": "uv.lock", "count": 300}
    assert by_file["note.txt"]["matches"][0]["line_text"] == "foo retained"
    assert any("--no-auto-summary" in line for line in payload["summary"]["guidance"])


def test_cli_explicit_preview_limit_disables_auto_summary(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """明示した出力上限は自動選択より優先する。"""
    target = tmp_path / "many.txt"
    target.write_text("".join(f"foo {index:04d} {'x' * 80}\n" for index in range(120)), encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("AI_AGENT", "1")

    rc = pyfltr.cli.main.run(
        [
            "grep",
            "foo",
            "--output-format=json",
            "--max-preview-chars=200",
            "--no-gitignore",
            str(target),
        ]
    )

    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["output_mode"] == "full"
    assert len(payload["matches"]) == 120
    assert payload["file_results"] == []


def test_cli_jsonl_warning_is_not_duplicated_to_stderr(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """JSONLへ含めた警告は同じ実行のstderrへ重複出力しない。"""
    missing = tmp_path / "missing.txt"
    monkeypatch.chdir(tmp_path)

    rc = pyfltr.cli.main.run(["grep", "foo", "--output-format=jsonl", str(missing)])

    captured = capsys.readouterr()
    assert rc == 1
    records = [json.loads(line) for line in captured.out.splitlines() if line]
    warning = next(record for record in records if record["kind"] == "warning")
    assert "missing.txt" in warning["msg"]
    assert "missing.txt" not in captured.err


@pytest.mark.asyncio
async def test_mcp_grep_collects_more_than_previous_default_limit(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """MCPの未指定max_totalは1000件で走査を打ち切らない。"""
    target = tmp_path / "many.txt"
    target.write_text("foo\n" * 1001, encoding="utf-8")
    monkeypatch.chdir(tmp_path)

    result = await pyfltr.cli.mcp_server.tool_grep(
        paths=[str(target)],
        pattern="foo",
        no_gitignore=True,
    )

    assert result.total_matches == 1001
    assert result.output_mode != "full"
    assert result.omitted_matches > 0
