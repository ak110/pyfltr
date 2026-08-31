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


def test_grep_jsonl_truncates_long_line_and_warns(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    target = tmp_path / "long.txt"
    target.write_text("x" * 250 + "needle" + "y" * 250 + "\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)

    rc = pyfltr.cli.main.run(
        ["grep", "needle", "--output-format=jsonl", "--max-preview-chars=40", "--no-gitignore", str(target)]
    )

    assert rc == 0
    records = [json.loads(line) for line in capsys.readouterr().out.splitlines() if line.strip()]
    match = next(record for record in records if record["kind"] == "match")
    warnings = [record for record in records if record["kind"] == "warning"]
    assert len(match["line_text"]) == 40
    assert match["truncated"] == ["line_text"]
    assert match["line_text_offset"] > 0
    assert len(warnings) == 1
    assert "--max-preview-chars=0" in warnings[0]["msg"]
    assert records[-1]["warnings"] == len(warnings)


def test_grep_json_truncates_long_line_and_exposes_warnings(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    target = tmp_path / "long.txt"
    target.write_text("x" * 250 + "needle" + "y" * 250 + "\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)

    rc = pyfltr.cli.main.run(
        ["grep", "needle", "--output-format=json", "--max-preview-chars=40", "--no-gitignore", str(target)]
    )

    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    match = payload["matches"][0]
    assert len(match["line_text"]) == 40
    assert match["truncated"] == ["line_text"]
    assert match["line_text_offset"] > 0
    assert len(payload["warnings"]) == 1
    warning = payload["warnings"][0]
    assert set(warning) == {"source", "msg"}
    assert warning["source"] == "grep"
    assert "--max-preview-chars=0" in warning["msg"]
    assert payload["summary"]["warnings"] == len(payload["warnings"])


def test_grep_json_exposes_decode_and_truncation_warnings(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    target = tmp_path / "long.txt"
    target.write_text("x" * 250 + "needle" + "y" * 250 + "\n", encoding="utf-8")
    (tmp_path / "invalid.txt").write_bytes(b"\xff")
    monkeypatch.chdir(tmp_path)

    rc = pyfltr.cli.main.run(
        ["grep", "needle", "--output-format=json", "--max-preview-chars=40", "--no-gitignore", str(tmp_path)]
    )

    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert len(payload["warnings"]) == 2
    assert all(set(warning) == {"source", "msg"} for warning in payload["warnings"])
    assert all("message" not in warning for warning in payload["warnings"])
    assert payload["summary"]["warnings"] == len(payload["warnings"])


def test_grep_text_truncates_long_line_with_offset_and_warns(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    target = tmp_path / "long.txt"
    line_text = "x" * 250 + "needle" + "y" * 250
    target.write_text(line_text + "\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)

    rc = pyfltr.cli.main.run(
        ["grep", "needle", "--output-format=text", "--max-preview-chars=40", "--no-gitignore", str(target)]
    )

    assert rc == 0
    output_lines = capsys.readouterr().out.splitlines()
    match_line = next(line for line in output_lines if f"{target.name}:1:251:" in line)
    prefix, preview = match_line.split("] ", maxsplit=1)
    offset = int(prefix.rsplit("[+", maxsplit=1)[1])
    assert len(preview) == 40
    assert 251 - offset == preview.index("needle") + 1
    assert any("warnings" in line for line in output_lines)
    assert any("--max-preview-chars=0" in line for line in output_lines)


def test_grep_text_short_line_keeps_existing_format(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    target = tmp_path / "short.txt"
    target.write_text("hello needle world\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)

    rc = pyfltr.cli.main.run(
        ["grep", "needle", "--output-format=text", "--max-preview-chars=40", "--no-gitignore", str(target)]
    )

    assert rc == 0
    out = capsys.readouterr().out
    assert f"{target.name}:1:7:hello needle world" in out
    assert "[+" not in out
    assert "warnings" not in out


def test_grep_text_match_only_truncation_does_not_add_offset(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    target = tmp_path / "multiline.txt"
    target.write_text("start\n" + "x" * 30 + "\nend\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)

    rc = pyfltr.cli.main.run(
        ["grep", "start.*end", "-U", "--output-format=text", "--max-preview-chars=10", "--no-gitignore", str(target)]
    )

    assert rc == 0
    out = capsys.readouterr().out
    assert f"{target.name}:1:1:start" in out
    assert "[+" not in out
    assert "--max-preview-chars=0" in out


def test_grep_text_context_only_truncation_does_not_add_offset(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    target = tmp_path / "context.txt"
    target.write_text("a" * 30 + "\nneedle\n" + "b" * 30 + "\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)

    rc = pyfltr.cli.main.run(
        [
            "grep",
            "needle",
            "-A",
            "1",
            "-B",
            "1",
            "--output-format=text",
            "--max-preview-chars=10",
            "--no-gitignore",
            str(target),
        ]
    )

    assert rc == 0
    out = capsys.readouterr().out
    assert f"{target.name}:2:1:needle" in out
    assert "[+" not in out
    assert "--max-preview-chars=0" in out


def test_grep_text_long_line_near_end_keeps_match_in_preview(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    target = tmp_path / "long.txt"
    target.write_text("x" * 490 + "needle" + "tail\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)

    rc = pyfltr.cli.main.run(
        ["grep", "needle", "--output-format=text", "--max-preview-chars=40", "--no-gitignore", str(target)]
    )

    assert rc == 0
    match_line = next(line for line in capsys.readouterr().out.splitlines() if f"{target.name}:1:491:" in line)
    prefix, preview = match_line.split("] ", maxsplit=1)
    offset = int(prefix.rsplit("[+", maxsplit=1)[1])
    assert "needle" in preview
    assert 491 - offset == preview.index("needle") + 1


def test_grep_truncation_is_local_to_each_match(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    target = tmp_path / "mixed.txt"
    target.write_text("needle short\n" + "x" * 100 + "needle" + "\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)

    rc = pyfltr.cli.main.run(
        ["grep", "needle", "--output-format=json", "--max-preview-chars=20", "--no-gitignore", str(target)]
    )

    assert rc == 0
    matches = json.loads(capsys.readouterr().out)["matches"]
    assert "truncated" not in matches[0]
    assert matches[1]["truncated"] == ["line_text"]


def test_grep_zero_preview_limit_returns_full_text_without_warning(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    target = tmp_path / "long.txt"
    line_text = "x" * 250 + "needle" + "y" * 250
    target.write_text(line_text + "\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)

    rc = pyfltr.cli.main.run(["grep", "needle", "--output-format=json", "--max-preview-chars=0", "--no-gitignore", str(target)])

    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["matches"][0]["line_text"] == line_text
    assert "truncated" not in payload["matches"][0]
    assert "warnings" not in payload
    assert "warnings" not in payload["summary"]


def test_grep_no_match_does_not_emit_truncation_warning(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    target = tmp_path / "long.txt"
    target.write_text("x" * 500 + "\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)

    rc = pyfltr.cli.main.run(
        ["grep", "needle", "--output-format=json", "--max-preview-chars=10", "--no-gitignore", str(target)]
    )

    assert rc == 1
    payload = json.loads(capsys.readouterr().out)
    assert "warnings" not in payload
    assert "warnings" not in payload["summary"]


def test_grep_count_uses_full_matches_without_truncation_warning(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    target = tmp_path / "long.txt"
    target.write_text(("x" * 100 + "needle\n") * 3, encoding="utf-8")
    monkeypatch.chdir(tmp_path)

    rc = pyfltr.cli.main.run(
        ["grep", "needle", "--count", "--output-format=json", "--max-preview-chars=10", "--no-gitignore", str(target)]
    )

    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["counts"] == [{"file": target.name, "count": 3}]
    assert "warnings" not in payload


def test_grep_max_total_is_independent_of_preview_limit(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    target = tmp_path / "long.txt"
    target.write_text(("x" * 100 + "needle\n") * 5, encoding="utf-8")
    monkeypatch.chdir(tmp_path)

    rc = pyfltr.cli.main.run(
        [
            "grep",
            "needle",
            "-m=3",
            "--max-total=2",
            "--output-format=json",
            "--max-preview-chars=10",
            "--no-gitignore",
            str(target),
        ]
    )

    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["summary"]["total_matches"] == 2
    assert len(payload["matches"]) == 2


@pytest.mark.parametrize("output_format", ["json", "jsonl"])
def test_grep_structured_output_preserves_newline_started_match_origin(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    output_format: str,
) -> None:
    target = tmp_path / "multiline.txt"
    target.write_text("x" * 500 + "\nneedle\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)

    rc = pyfltr.cli.main.run(
        [
            "grep",
            "\nneedle",
            "-U",
            f"--output-format={output_format}",
            "--max-preview-chars=200",
            "--no-gitignore",
            str(target),
        ]
    )

    assert rc == 0
    output = capsys.readouterr().out
    if output_format == "json":
        payload = json.loads(output)
        match = payload["matches"][0]
        warnings = payload["warnings"]
    else:
        records = [json.loads(line) for line in output.splitlines() if line.strip()]
        match = next(record for record in records if record["kind"] == "match")
        warnings = [record for record in records if record["kind"] == "warning"]
    assert match["line"] == 1
    assert match["col"] == 501
    assert len(match["line_text"]) == 200
    assert match["line_text_offset"] == 300
    assert match["col"] - match["line_text_offset"] == len(match["line_text"]) + 1
    assert match["match_text"] == "\nneedle"
    assert match["truncated"] == ["line_text"]
    assert len(warnings) == 1


def test_grep_text_preserves_newline_started_match_origin(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    target = tmp_path / "multiline.txt"
    target.write_text("x" * 500 + "\nneedle\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)

    rc = pyfltr.cli.main.run(
        [
            "grep",
            "\nneedle",
            "-U",
            "--output-format=text",
            "--max-preview-chars=200",
            "--no-gitignore",
            str(target),
        ]
    )

    assert rc == 0
    out = capsys.readouterr().out
    match_line = next(line for line in out.splitlines() if line.startswith(f"{target.name}:1:501:"))
    assert match_line.startswith(f"{target.name}:1:501:[+300] ")
    assert len(match_line.split("] ", maxsplit=1)[1]) == 200
    assert "--max-preview-chars=0" in out
