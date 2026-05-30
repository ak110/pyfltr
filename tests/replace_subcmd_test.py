"""`replace`サブコマンドのCLIテスト。"""

import json
import pathlib

import pytest

import pyfltr.cli.main


@pytest.fixture(autouse=True)
def _isolated_replace_cache(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path_factory: pytest.TempPathFactory,
) -> pathlib.Path:
    """`PYFLTR_CACHE_DIR`を一時ディレクトリへ向けるautouseフィクスチャ。

    `default_history_root()`が参照する`default_cache_root()`の解決先を固定し、
    開発機の実キャッシュへ書き込まないようにする。
    """
    cache_root = tmp_path_factory.mktemp("replace_cache")
    monkeypatch.setenv("PYFLTR_CACHE_DIR", str(cache_root))
    return cache_root


def test_replace_dry_run_does_not_write(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    target = tmp_path / "a.txt"
    target.write_text("foo bar\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    rc = pyfltr.cli.main.run(["replace", "foo", "baz", "--dry-run", "--output-format=jsonl", str(target)])
    assert rc == 0
    # 書き換えられていない
    assert target.read_text(encoding="utf-8") == "foo bar\n"
    capsys.readouterr()


def test_replace_writes_and_emits_replace_id(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    target = tmp_path / "a.txt"
    target.write_text("foo bar\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    rc = pyfltr.cli.main.run(["replace", "foo", "baz", "--output-format=jsonl", str(target)])
    assert rc == 0
    assert target.read_text(encoding="utf-8") == "baz bar\n"

    lines = [json.loads(line) for line in capsys.readouterr().out.splitlines() if line.strip()]
    summary = lines[-1]
    assert summary["kind"] == "summary"
    assert summary["files_changed"] == 1
    assert summary["total_replacements"] == 1
    assert "replace_id" in summary
    # undo案内
    assert "guidance" in summary
    assert any("--undo" in g for g in summary["guidance"])


def test_replace_undo_round_trip(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    target = tmp_path / "a.txt"
    target.write_text("foo bar\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    rc = pyfltr.cli.main.run(["replace", "foo", "baz", "--output-format=jsonl", str(target)])
    assert rc == 0
    assert target.read_text(encoding="utf-8") == "baz bar\n"
    lines = [json.loads(line) for line in capsys.readouterr().out.splitlines() if line.strip()]
    replace_id = lines[-1]["replace_id"]

    # undo
    rc = pyfltr.cli.main.run(["replace", "--undo", replace_id, "--output-format=jsonl"])
    assert rc == 0
    assert target.read_text(encoding="utf-8") == "foo bar\n"

    capsys.readouterr()


def test_replace_undo_warns_when_manually_edited(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    target = tmp_path / "a.txt"
    target.write_text("foo bar\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    rc = pyfltr.cli.main.run(["replace", "foo", "baz", "--output-format=jsonl", str(target)])
    assert rc == 0
    lines = [json.loads(line) for line in capsys.readouterr().out.splitlines() if line.strip()]
    replace_id = lines[-1]["replace_id"]

    # 手動編集
    target.write_text("manually edited\n", encoding="utf-8")

    # 通常 undo は警告で停止（exit 1）
    rc = pyfltr.cli.main.run(["replace", "--undo", replace_id, "--output-format=jsonl"])
    assert rc == 1
    captured = capsys.readouterr()
    assert "--force" in captured.err
    assert target.read_text(encoding="utf-8") == "manually edited\n"

    # --force で強制復元
    rc = pyfltr.cli.main.run(["replace", "--undo", replace_id, "--force", "--output-format=jsonl"])
    assert rc == 0
    assert target.read_text(encoding="utf-8") == "foo bar\n"
    capsys.readouterr()


def test_replace_show_changes_emits_before_after(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    target = tmp_path / "a.txt"
    target.write_text("foo bar\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    rc = pyfltr.cli.main.run(
        [
            "replace",
            "foo",
            "baz",
            "--show-changes",
            "--output-format=jsonl",
            str(target),
        ]
    )
    assert rc == 0
    lines = [json.loads(line) for line in capsys.readouterr().out.splitlines() if line.strip()]
    file_change_records = [line for line in lines if line["kind"] == "file_change"]
    assert len(file_change_records) == 1
    record = file_change_records[0]
    assert "changes" in record
    assert record["changes"][0]["before_line"] == "foo bar"
    assert record["changes"][0]["after_line"] == "baz bar"


def test_replace_from_grep_filters_files(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    target_a = tmp_path / "a.txt"
    target_b = tmp_path / "b.txt"
    target_a.write_text("foo\n", encoding="utf-8")
    target_b.write_text("foo\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)

    grep_jsonl = tmp_path / "grep.jsonl"
    rc = pyfltr.cli.main.run(["grep", "foo", "--output-format=jsonl", "--output-file", str(grep_jsonl), str(target_a)])
    assert rc == 0
    capsys.readouterr()

    # `--from-grep` で a.txt のみが対象になる
    rc = pyfltr.cli.main.run(
        [
            "replace",
            "foo",
            "baz",
            "--from-grep",
            str(grep_jsonl),
            "--output-format=jsonl",
            str(tmp_path),
        ]
    )
    assert rc == 0
    assert target_a.read_text(encoding="utf-8") == "baz\n"
    # b.txt は対象外なのでそのまま
    assert target_b.read_text(encoding="utf-8") == "foo\n"
    capsys.readouterr()


def test_replace_text_dry_run_summary(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    target = tmp_path / "a.txt"
    target.write_text("foo\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    rc = pyfltr.cli.main.run(["replace", "foo", "baz", "--dry-run", str(target)])
    assert rc == 0
    out = capsys.readouterr().out
    assert "(dry-run)" in out
    assert "1 replacement(s)" in out


def test_replace_list_history_returns_saved_id(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """履歴を1件保存後、`--list-history`が当該replace_idを返す。"""
    target = tmp_path / "a.txt"
    target.write_text("foo bar\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    rc = pyfltr.cli.main.run(["replace", "foo", "baz", "--output-format=jsonl", str(target)])
    assert rc == 0
    lines = [json.loads(line) for line in capsys.readouterr().out.splitlines() if line.strip()]
    saved_id = lines[-1]["replace_id"]

    rc = pyfltr.cli.main.run(["replace", "--list-history", "--output-format=jsonl"])
    assert rc == 0
    listed = [json.loads(line) for line in capsys.readouterr().out.splitlines() if line.strip()]
    listed_ids = [entry.get("replace_id") for entry in listed if entry.get("kind") == "replace_history"]
    assert saved_id in listed_ids


def test_replace_show_history_missing_id_emits_guidance(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """存在しないreplace_idへ`--show-history`を指定すると`--list-history`誘導を含むstderrを返す。"""
    monkeypatch.chdir(tmp_path)
    rc = pyfltr.cli.main.run(["replace", "--show-history", "NONEXISTENT", "--output-format=jsonl"])
    assert rc == 1
    err = capsys.readouterr().err
    assert "NONEXISTENT" in err
    assert "pyfltr replace --list-history" in err


def test_replace_undo_missing_id_emits_guidance(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """存在しないreplace_idへ`--undo`を指定すると`--list-history`誘導を含むstderrを返す。"""
    monkeypatch.chdir(tmp_path)
    rc = pyfltr.cli.main.run(["replace", "--undo", "NONEXISTENT", "--output-format=jsonl"])
    assert rc == 1
    err = capsys.readouterr().err
    assert "NONEXISTENT" in err
    assert "pyfltr replace --list-history" in err


def test_replace_show_history_returns_meta(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """履歴を1件保存後、`--show-history <id>`が当該履歴のmeta情報を返す。"""
    target = tmp_path / "a.txt"
    target.write_text("foo bar\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    rc = pyfltr.cli.main.run(["replace", "foo", "baz", "--output-format=jsonl", str(target)])
    assert rc == 0
    lines = [json.loads(line) for line in capsys.readouterr().out.splitlines() if line.strip()]
    saved_id = lines[-1]["replace_id"]

    rc = pyfltr.cli.main.run(["replace", "--show-history", saved_id, "--output-format=jsonl"])
    assert rc == 0
    shown = [json.loads(line) for line in capsys.readouterr().out.splitlines() if line.strip()]
    matched = [entry for entry in shown if entry.get("replace_id") == saved_id]
    assert matched
    meta = matched[0]
    assert meta["kind"] == "replace_history"
    assert meta["command"]["pattern"] == "foo"
    assert meta["command"]["replacement"] == "baz"
    assert any(file_entry["file"].endswith("a.txt") for file_entry in meta.get("files", []))


def test_replace_includes_hidden_files(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """ドット始まりファイルもdry-runで置換対象になる（run系と統一）。"""
    hidden = tmp_path / ".hidden.py"
    hidden.write_text("foo bar\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    rc = pyfltr.cli.main.run(["replace", "foo", "baz", "--dry-run", "--output-format=jsonl", str(tmp_path)])
    assert rc == 0
    lines = [json.loads(line) for line in capsys.readouterr().out.splitlines() if line.strip()]
    file_changes = [line for line in lines if line["kind"] == "file_change"]
    assert any(".hidden.py" in fc["file"] for fc in file_changes)


@pytest.mark.parametrize("output_format", ["jsonl", "json"])
def test_replace_notifies_excluded_explicit_file(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    output_format: str,
) -> None:
    """直接指定したexclude該当ファイルをsummaryのfully_excluded_filesで通知する。"""
    lock = tmp_path / "uv.lock"
    lock.write_text("foo\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    rc = pyfltr.cli.main.run(["replace", "foo", "baz", "--dry-run", f"--output-format={output_format}", str(lock)])
    assert rc == 0
    out = capsys.readouterr().out
    if output_format == "jsonl":
        summary = [json.loads(line) for line in out.splitlines() if line.strip()][-1]
    else:
        summary = json.loads(out)["summary"]
    assert summary["fully_excluded_files"] == ["uv.lock"]
    assert summary["files_changed"] == 0
    assert lock.read_text(encoding="utf-8") == "foo\n"


@pytest.mark.parametrize("output_format", ["jsonl", "json"])
def test_replace_notifies_missing_explicit_file(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    output_format: str,
) -> None:
    """直接指定した不在ファイルをsummaryのmissing_targetsで通知する。"""
    monkeypatch.chdir(tmp_path)
    rc = pyfltr.cli.main.run(
        ["replace", "foo", "baz", "--dry-run", f"--output-format={output_format}", str(tmp_path / "nope.py")]
    )
    assert rc == 0
    out = capsys.readouterr().out
    if output_format == "jsonl":
        summary = [json.loads(line) for line in out.splitlines() if line.strip()][-1]
    else:
        summary = json.loads(out)["summary"]
    assert summary["missing_targets"] == ["nope.py"]


def test_replace_within_limits_to_region(tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """--within領域内のみ置換し、領域外の同一文字列は変えない。"""
    target = tmp_path / "a.txt"
    # KEY行（3行目）の前後1行が領域。領域内のfooのみ置換し、1行目・5行目のfooは領域外で不変。
    target.write_text("foo\nL1\nKEY foo\nL3\nfoo\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)

    rc = pyfltr.cli.main.run(["replace", "foo", "X", str(target), "--within", "KEY", "-C", "1"])

    assert rc == 0
    assert target.read_text(encoding="utf-8") == "foo\nL1\nKEY X\nL3\nfoo\n"


def test_replace_without_within_replaces_whole_file(tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """--within省略時は全体置換（後方互換）。"""
    target = tmp_path / "a.txt"
    target.write_text("foo\nfoo\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)

    rc = pyfltr.cli.main.run(["replace", "foo", "X", str(target)])

    assert rc == 0
    assert target.read_text(encoding="utf-8") == "X\nX\n"


def test_replace_within_with_multiline_rejected(tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """--withinと-Uの併用はexit 2で拒否される。"""
    target = tmp_path / "a.txt"
    target.write_text("KEY foo\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    with pytest.raises(SystemExit) as exc_info:
        pyfltr.cli.main.run(["replace", "foo", "X", str(target), "--within", "KEY", "-U"])
    assert exc_info.value.code == 2


def test_replace_context_without_within_rejected(tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """--within未指定で-A/-B/-C指定はexit 2で拒否される。"""
    target = tmp_path / "a.txt"
    target.write_text("foo\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    with pytest.raises(SystemExit) as exc_info:
        pyfltr.cli.main.run(["replace", "foo", "X", str(target), "-A", "1"])
    assert exc_info.value.code == 2


def test_replace_within_dry_run_counts_without_write(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """--within + --dry-runは書き込まず領域内件数のみ報告する。"""
    target = tmp_path / "a.txt"
    original = "foo\nKEY foo\n"
    target.write_text(original, encoding="utf-8")
    monkeypatch.chdir(tmp_path)

    rc = pyfltr.cli.main.run(["replace", "foo", "X", str(target), "--within", "KEY", "--dry-run", "--output-format=json"])

    assert rc == 0
    assert target.read_text(encoding="utf-8") == original
    # 領域はKEY行のみ。領域内のfoo1件のみがカウントされ、行頭fooは含めない。
    summary = json.loads(capsys.readouterr().out)["summary"]
    assert summary["total_replacements"] == 1


def test_replace_within_context_expands_region(tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """-Cが-A/-Bへ展開され、アンカー前後行を領域へ含める。"""
    target = tmp_path / "a.txt"
    # KEYの前後1行（L1・L3）まで領域。L0・L4のfooは領域外で不変。
    target.write_text("foo\nfoo\nKEY\nfoo\nfoo\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)

    rc = pyfltr.cli.main.run(["replace", "foo", "X", str(target), "--within", "KEY", "-C", "1"])

    assert rc == 0
    assert target.read_text(encoding="utf-8") == "foo\nX\nKEY\nX\nfoo\n"


def test_replace_within_show_changes_limits_to_region(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """--show-changesが領域内の置換レコードのみを返す。"""
    target = tmp_path / "a.txt"
    target.write_text("foo\nKEY foo\nfoo\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)

    rc = pyfltr.cli.main.run(["replace", "foo", "X", str(target), "--within", "KEY", "--show-changes", "--output-format=json"])

    assert rc == 0
    changes = json.loads(capsys.readouterr().out)["changes"][0]["changes"]
    # 領域はKEY行（2行目）のみ。レコードも2行目1件に限定される。
    assert [c["line"] for c in changes] == [2]


def test_replace_within_excludes_match_crossing_region_boundary(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """領域境界を跨ぐマルチラインマッチは置換対象から除外される。"""
    target = tmp_path / "a.txt"
    # 領域はKEY行（2行目）のみ。改行を含む「o\nb」は2〜3行目に跨り領域外なので不変。
    target.write_text("a\nKEY o\nb foo\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)

    # `o\nb`は2行目末尾から3行目へ跨る。領域（2行目のみ）へ完全包含されないため置換されない。
    rc = pyfltr.cli.main.run(["replace", "o\\nb", "X", str(target), "--within", "KEY"])

    # 跨りマッチが除外され置換0件となる。replaceは置換0件でも正常終了（rc=0）し、ファイルは不変。
    assert rc == 0
    assert target.read_text(encoding="utf-8") == "a\nKEY o\nb foo\n"


def test_replace_within_undo_round_trip(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """--withinの実書き込みをundoで元の内容へ復元できる。"""
    target = tmp_path / "a.txt"
    original = "foo\nKEY foo\n"
    target.write_text(original, encoding="utf-8")
    monkeypatch.chdir(tmp_path)

    rc = pyfltr.cli.main.run(["replace", "foo", "X", str(target), "--within", "KEY", "--output-format=json"])
    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    replace_id = payload["summary"]["replace_id"]
    # 領域はKEY行のみ（before/after=0）。領域内のfooのみ置換、行頭fooは不変。
    assert target.read_text(encoding="utf-8") == "foo\nKEY X\n"

    rc = pyfltr.cli.main.run(["replace", "--undo", replace_id])
    assert rc == 0
    assert target.read_text(encoding="utf-8") == original
