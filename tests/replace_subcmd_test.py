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
