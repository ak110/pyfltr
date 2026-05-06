"""`grep`サブコマンドのCLIテスト。"""

# pylint: disable=missing-function-docstring  # テストは関数docstringを省略する慣習

import json
import pathlib

import pytest

import pyfltr.cli.main


def _make_sample_files(root: pathlib.Path) -> None:
    """テスト用のサンプルファイル群を作成する。"""
    (root / "a.py").write_text("foo bar\nbaz foo\n", encoding="utf-8")
    (root / "b.txt").write_text("hello\nfoo world\n", encoding="utf-8")
    (root / "c.md").write_text("nothing here\n", encoding="utf-8")


def test_grep_text_basic(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _make_sample_files(tmp_path)
    monkeypatch.chdir(tmp_path)
    rc = pyfltr.cli.main.run(["grep", "foo", str(tmp_path)])
    assert rc == 0
    out = capsys.readouterr().out
    # ripgrep流儀の path:line:col:line_text 形式
    assert "a.py:1:1:foo bar" in out
    assert "a.py:2:5:baz foo" in out
    assert "b.txt:2:1:foo world" in out
    # サマリ行
    assert "match(es)" in out


def test_grep_jsonl_records(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _make_sample_files(tmp_path)
    monkeypatch.chdir(tmp_path)
    rc = pyfltr.cli.main.run(["grep", "foo", "--output-format=jsonl", str(tmp_path)])
    assert rc == 0
    lines = [json.loads(line) for line in capsys.readouterr().out.splitlines() if line.strip()]
    kinds = [line["kind"] for line in lines]
    assert kinds[0] == "header"
    assert lines[0]["subcommand"] == "grep"
    assert "match" in kinds
    assert kinds[-1] == "summary"
    summary = lines[-1]
    assert summary["subcommand"] == "grep"
    assert summary["total_matches"] >= 3
    # マッチありなのでガイダンスにreplace起動コマンド案内が含まれる
    assert "guidance" in summary
    assert any("pyfltr replace" in g for g in summary["guidance"])


def test_grep_no_match_exit_1(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _make_sample_files(tmp_path)
    monkeypatch.chdir(tmp_path)
    rc = pyfltr.cli.main.run(["grep", "no_such_pattern_xyz", str(tmp_path)])
    assert rc == 1
    capsys.readouterr()


def test_grep_type_filter_python(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _make_sample_files(tmp_path)
    monkeypatch.chdir(tmp_path)
    rc = pyfltr.cli.main.run(["grep", "foo", "--type=python", str(tmp_path)])
    assert rc == 0
    out = capsys.readouterr().out
    assert "a.py" in out
    # b.txt は対象外
    assert "b.txt" not in out


def test_grep_glob_filter(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _make_sample_files(tmp_path)
    monkeypatch.chdir(tmp_path)
    rc = pyfltr.cli.main.run(["grep", "foo", "-g", "*.txt", str(tmp_path)])
    assert rc == 0
    out = capsys.readouterr().out
    assert "b.txt" in out
    assert "a.py" not in out


def test_grep_context_options(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    target = tmp_path / "ctx.txt"
    target.write_text("line1\nline2\nfoo here\nline4\nline5\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    rc = pyfltr.cli.main.run(["grep", "foo", "-A", "1", "-B", "1", "--output-format=jsonl", str(target)])
    assert rc == 0
    lines = [json.loads(line) for line in capsys.readouterr().out.splitlines() if line.strip()]
    matches = [line for line in lines if line["kind"] == "match"]
    assert len(matches) == 1
    assert matches[0]["before"] == ["line2"]
    assert matches[0]["after"] == ["line4"]


def test_grep_max_total(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    target = tmp_path / "many.txt"
    target.write_text("\n".join(f"foo{i}" for i in range(10)) + "\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    rc = pyfltr.cli.main.run(["grep", "foo", "--max-total=3", "--output-format=jsonl", str(target)])
    assert rc == 0
    lines = [json.loads(line) for line in capsys.readouterr().out.splitlines() if line.strip()]
    matches = [line for line in lines if line["kind"] == "match"]
    assert len(matches) == 3


def test_grep_json_output(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _make_sample_files(tmp_path)
    monkeypatch.chdir(tmp_path)
    rc = pyfltr.cli.main.run(["grep", "foo", "--output-format=json", str(tmp_path)])
    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert "matches" in payload
    assert "summary" in payload
    assert payload["summary"]["total_matches"] >= 3
    assert "guidance" in payload["summary"]
