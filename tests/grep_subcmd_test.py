"""`grep`サブコマンドのCLIテスト。"""

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
    assert matches[0]["file"] == target.relative_to(tmp_path).as_posix()
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


@pytest.mark.parametrize(
    "summary_flag,summary_key",
    [
        (None, None),
        ("--files-with-matches", "files"),
        ("--count", "counts"),
    ],
)
def test_grep_json_normalizes_file_separators(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    summary_flag: str | None,
    summary_key: str | None,
) -> None:
    """JSONのマッチと集計でファイル位置の区切り文字を`/`へ統一する。"""
    target = pathlib.Path(r"sub\target.py")
    filesystem_target = tmp_path / target
    filesystem_target.parent.mkdir(parents=True, exist_ok=True)
    filesystem_target.write_text("foo\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    args = ["grep", "foo", "--output-format=json"]
    if summary_flag is not None:
        args.append(summary_flag)
    args.append(str(target))

    rc = pyfltr.cli.main.run(args)

    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    normalized_target = target.as_posix().replace("\\", "/")
    if summary_key is None:
        assert [match["file"] for match in payload["matches"]] == [normalized_target]
    elif summary_key == "files":
        assert payload[summary_key] == [normalized_target]
    else:
        assert payload[summary_key] == [{"file": normalized_target, "count": 1}]


def test_grep_includes_hidden_files(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """ドット始まりのファイル・ディレクトリも対象に含める（run系と統一）。"""
    (tmp_path / ".hidden.py").write_text("foo here\n", encoding="utf-8")
    hidden_dir = tmp_path / ".config"
    hidden_dir.mkdir()
    (hidden_dir / "settings.py").write_text("foo there\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    rc = pyfltr.cli.main.run(["grep", "foo", str(tmp_path)])
    assert rc == 0
    out = capsys.readouterr().out
    assert ".hidden.py" in out
    assert "settings.py" in out


@pytest.mark.parametrize("output_format", ["jsonl", "json"])
def test_grep_notifies_excluded_explicit_file(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    output_format: str,
) -> None:
    """直接指定したexclude該当ファイルをsummaryのfully_excluded_filesで通知する。"""
    lock = tmp_path / "uv.lock"
    lock.write_text("foo\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    rc = pyfltr.cli.main.run(["grep", "foo", f"--output-format={output_format}", str(lock)])
    assert rc == 1  # 除外され対象0件のためマッチ無し
    out = capsys.readouterr().out
    if output_format == "jsonl":
        records = [json.loads(line) for line in out.splitlines() if line.strip()]
        summary = records[-1]
        assert summary["warnings"] == len([r for r in records if r["kind"] == "warning"])
    else:
        summary = json.loads(out)["summary"]
    assert summary["fully_excluded_files"] == ["uv.lock"]


@pytest.mark.parametrize("output_format", ["jsonl", "json"])
def test_grep_notifies_missing_explicit_file(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    output_format: str,
) -> None:
    """直接指定した不在ファイルをsummaryのmissing_targetsで通知する。"""
    monkeypatch.chdir(tmp_path)
    rc = pyfltr.cli.main.run(["grep", "foo", f"--output-format={output_format}", str(tmp_path / "nope.py")])
    assert rc == 1
    out = capsys.readouterr().out
    if output_format == "jsonl":
        records = [json.loads(line) for line in out.splitlines() if line.strip()]
        summary = records[-1]
        assert summary["warnings"] == len([r for r in records if r["kind"] == "warning"])
    else:
        summary = json.loads(out)["summary"]
    assert summary["missing_targets"] == ["nope.py"]


def test_grep_jsonl_summary_omits_warnings_when_none(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """警告が1件も発生しない実行ではsummaryに`warnings`キーが現れない。"""
    _make_sample_files(tmp_path)
    monkeypatch.chdir(tmp_path)
    # 一時ディレクトリはgit管理外のため、`.gitignore`判定を無効化して当該警告の発生を避ける。
    rc = pyfltr.cli.main.run(["grep", "foo", "--output-format=jsonl", "--no-gitignore", str(tmp_path / "a.py")])
    assert rc == 0
    records = [json.loads(line) for line in capsys.readouterr().out.splitlines() if line.strip()]
    assert not [r for r in records if r["kind"] == "warning"]
    assert "warnings" not in records[-1]


def test_grep_text_notifies_excluded(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """text出力でも除外ファイルをfully-excluded-filesセクションで通知する。"""
    lock = tmp_path / "uv.lock"
    lock.write_text("foo\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    rc = pyfltr.cli.main.run(["grep", "foo", str(lock)])
    assert rc == 1
    out = capsys.readouterr().out
    assert "fully-excluded-files" in out
    assert "uv.lock" in out
