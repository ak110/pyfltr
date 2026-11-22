import hashlib
import json
import pathlib

import pytest

import pyfltr.cli.main
import pyfltr.cli.replace_subcmd
import pyfltr.grep_.history
import pyfltr.grep_.operations
import pyfltr.grep_.replacer
import pyfltr.grep_.types


@pytest.fixture(autouse=True)
def _isolated_replace_cache(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path_factory: pytest.TempPathFactory,
) -> pathlib.Path:
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

    rc = pyfltr.cli.main.run(["replace", "--undo", replace_id, "--output-format=jsonl"])
    assert rc == 0
    assert target.read_text(encoding="utf-8") == "foo bar\n"

    capsys.readouterr()


@pytest.mark.parametrize(
    ("original", "expected", "encoding"),
    [
        (b"foo\r\nkeep\r\n", b"bar\r\nkeep\r\n", "utf-8"),
        (b"foo\r\nkeep\nlast\r", b"bar\r\nkeep\nlast\r", "utf-8"),
        (b"foo\rkeep\r", b"bar\rkeep\r", "utf-8"),
        (b"foo\nkeep", b"bar\nkeep", "utf-8"),
        (b"\xef\xbb\xbffoo\r\n", b"\xef\xbb\xbfbar\r\n", "utf-8-sig"),
        (b"\xef\xbb\xbffoo\r\n", b"\xef\xbb\xbfbar\r\n", "utf-8"),
        (b"\xff\xfe" + "foo\r\n".encode("utf-16-le"), b"\xff\xfe" + "bar\r\n".encode("utf-16-le"), "utf-16"),
        (b"\xfe\xff" + "foo\r\n".encode("utf-16-be"), b"\xfe\xff" + "bar\r\n".encode("utf-16-be"), "utf-16"),
        ("foo 日本語\r\n".encode("cp932"), "bar 日本語\r\n".encode("cp932"), "cp932"),
        (b"foo\x87\x90\r\n", b"bar\x87\x90\r\n", "cp932"),
    ],
)
def test_replace_preserves_bytes_round_trip(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    original: bytes,
    expected: bytes,
    encoding: str,
) -> None:
    target = tmp_path / "sample.txt"
    target.write_bytes(original)
    monkeypatch.chdir(tmp_path)
    assert pyfltr.cli.main.run(["replace", "foo", "bar", "--encoding", encoding, "--output-format=jsonl", str(target)]) == 0
    assert target.read_bytes() == expected
    records = [json.loads(line) for line in capsys.readouterr().out.splitlines() if line.strip()]
    replace_id = records[-1]["replace_id"]
    assert pyfltr.cli.main.run(["replace", "--undo", replace_id, "--output-format=jsonl"]) == 0
    assert target.read_bytes() == original
    undo_records = [json.loads(line) for line in capsys.readouterr().out.splitlines() if line.strip()]
    assert not any(record["kind"] == "warning" for record in undo_records)


def test_replace_within_preserves_bytes_round_trip(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    target = tmp_path / "sample.txt"
    original = b"foo\r\nanchor foo\nfoo\r"
    target.write_bytes(original)
    monkeypatch.chdir(tmp_path)
    assert pyfltr.cli.main.run(["replace", "foo", "bar", "--within", "anchor", "--output-format=jsonl", str(target)]) == 0
    assert target.read_bytes() == b"foo\r\nanchor bar\nfoo\r"
    records = [json.loads(line) for line in capsys.readouterr().out.splitlines() if line.strip()]
    assert pyfltr.cli.main.run(["replace", "--undo", records[-1]["replace_id"], "--output-format=jsonl"]) == 0
    assert target.read_bytes() == original


@pytest.mark.parametrize(
    ("original", "pattern", "expected"),
    [
        (b"keep\r\nfoo\r\nlast\r", "foo\n", b"keep\r\nbar\nlast\r"),
        (b"keep\r\nfoo\rlast\r\n", "foo\n", b"keep\r\nbar\nlast\r\n"),
    ],
)
def test_replace_searches_normalized_newlines(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    original: bytes,
    pattern: str,
    expected: bytes,
) -> None:
    target = tmp_path / "sample.txt"
    target.write_bytes(original)
    monkeypatch.chdir(tmp_path)
    assert pyfltr.cli.main.run(["replace", pattern, "bar\n", "--output-format=jsonl", str(target)]) == 0
    assert target.read_bytes() == expected
    records = [json.loads(line) for line in capsys.readouterr().out.splitlines() if line.strip()]
    assert pyfltr.cli.main.run(["replace", "--undo", records[-1]["replace_id"], "--output-format=jsonl"]) == 0
    assert target.read_bytes() == original


def test_replace_carriage_return_pattern_does_not_match(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    target = tmp_path / "sample.txt"
    target.write_bytes(b"foo\r\n")
    monkeypatch.chdir(tmp_path)
    assert pyfltr.cli.main.run(["replace", r"\r", "X", "--output-format=jsonl", str(target)]) == 0
    assert target.read_bytes() == b"foo\r\n"
    records = [json.loads(line) for line in capsys.readouterr().out.splitlines() if line.strip()]
    assert records[-1]["files_changed"] == 0


@pytest.mark.parametrize(
    ("original", "pattern", "options", "expected"),
    [
        (b"foo\r\n", "foo.*", [], b"bar\r\n"),
        (b"FOO\r\nkeep\r\n", "foo", ["-i"], b"bar\r\nkeep\r\n"),
        (b"foo\r\nkeep\r\n", "^foo$", ["-U"], b"bar\r\nkeep\r\n"),
        (b"FOO\rkeep\r", "^foo$", ["-U", "-i"], b"bar\rkeep\r"),
        (b"FOO\r\n", "foo", ["-x", "-i"], b"bar\r\n"),
        (b"foo\rkeep\r", "foo", ["-x", "-U"], b"bar\rkeep\r"),
        (b"lead\r\nANCHOR\r\nFOO\r\n", "foo", ["--within", "anchor", "-A", "1", "-i"], b"lead\r\nANCHOR\r\nbar\r\n"),
    ],
)
def test_replace_legacy_regex_option_results(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    original: bytes,
    pattern: str,
    options: list[str],
    expected: bytes,
) -> None:
    target = tmp_path / "sample.txt"
    target.write_bytes(original)
    monkeypatch.chdir(tmp_path)
    assert pyfltr.cli.main.run(["replace", pattern, "bar", *options, "--output-format=jsonl", str(target)]) == 0
    assert target.read_bytes() == expected
    records = [json.loads(line) for line in capsys.readouterr().out.splitlines() if line.strip()]
    assert pyfltr.cli.main.run(["replace", "--undo", records[-1]["replace_id"], "--output-format=jsonl"]) == 0
    assert target.read_bytes() == original


@pytest.mark.parametrize(
    ("pattern", "replacement", "expected"),
    [
        ("foo.bar", "X\nY", b"head\r\nX\nY\rkeep\r\n"),
        ("foo.bar", "X\rY", b"head\r\nX\rY\rkeep\r\n"),
        (r"(foo)(.)(bar)", r"\1X\2\3", b"head\r\nfooX\nbar\rkeep\r\n"),
    ],
)
def test_replace_matched_newline_uses_replacement_representation(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    pattern: str,
    replacement: str,
    expected: bytes,
) -> None:
    target = tmp_path / "sample.txt"
    original = b"head\r\nfoo\r\nbar\rkeep\r\n"
    target.write_bytes(original)
    monkeypatch.chdir(tmp_path)
    assert pyfltr.cli.main.run(["replace", pattern, replacement, "-U", "--output-format=jsonl", str(target)]) == 0
    assert target.read_bytes() == expected
    records = [json.loads(line) for line in capsys.readouterr().out.splitlines() if line.strip()]
    assert pyfltr.cli.main.run(["replace", "--undo", records[-1]["replace_id"], "--output-format=jsonl"]) == 0
    assert target.read_bytes() == original


def test_replace_public_hashes_keep_logical_text_meaning(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    target = tmp_path / "sample.txt"
    target.write_bytes(b"foo\r\nkeep\r\n")
    monkeypatch.chdir(tmp_path)
    assert pyfltr.cli.main.run(["replace", "foo", "bar", "--output-format=jsonl", str(target)]) == 0
    records = [json.loads(line) for line in capsys.readouterr().out.splitlines() if line.strip()]
    change = next(record for record in records if record["kind"] == "file_change")
    assert change["before_hash"] == hashlib.sha256(b"foo\nkeep\n").hexdigest()
    assert change["after_hash"] == hashlib.sha256(b"bar\nkeep\n").hexdigest()


@pytest.mark.parametrize(
    ("original", "pattern", "expected"),
    [
        (b"foo\r\nanchor\r\nfoo\r\nfoo\r", "foo\n", b"foo\r\nanchor\r\nbar\nfoo\r"),
        (b"foo\ranchor\rfoo\rfoo\r\n", "foo\n", b"foo\ranchor\rbar\nfoo\r\n"),
    ],
)
def test_replace_within_searches_normalized_newlines(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    original: bytes,
    pattern: str,
    expected: bytes,
) -> None:
    target = tmp_path / "sample.txt"
    target.write_bytes(original)
    monkeypatch.chdir(tmp_path)
    assert (
        pyfltr.cli.main.run(
            ["replace", pattern, "bar\n", "--within", "anchor", "-A", "1", "--output-format=jsonl", str(target)]
        )
        == 0
    )
    assert target.read_bytes() == expected
    records = [json.loads(line) for line in capsys.readouterr().out.splitlines() if line.strip()]
    assert pyfltr.cli.main.run(["replace", "--undo", records[-1]["replace_id"], "--output-format=jsonl"]) == 0
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
def test_replace_line_regexp_preserves_original_newlines(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    original: bytes,
    pattern: str,
    expected: bytes,
) -> None:
    target = tmp_path / "sample.txt"
    target.write_bytes(original)
    monkeypatch.chdir(tmp_path)
    assert pyfltr.cli.main.run(["replace", pattern, "bar", "-x", "--output-format=jsonl", str(target)]) == 0
    assert target.read_bytes() == expected
    records = [json.loads(line) for line in capsys.readouterr().out.splitlines() if line.strip()]
    assert pyfltr.cli.main.run(["replace", "--undo", records[-1]["replace_id"], "--output-format=jsonl"]) == 0
    assert target.read_bytes() == original


@pytest.mark.parametrize(
    ("original", "expected"),
    [
        (b"foo\r\nanchor\r\nfoo\r\nkeep\r\n", b"foo\r\nanchor\r\nbar\r\nkeep\r\n"),
        (b"foo\ranchor\rfoo\rkeep\r", b"foo\ranchor\rbar\rkeep\r"),
    ],
)
def test_replace_within_line_regexp_preserves_original_newlines(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    original: bytes,
    expected: bytes,
) -> None:
    target = tmp_path / "sample.txt"
    target.write_bytes(original)
    monkeypatch.chdir(tmp_path)
    assert (
        pyfltr.cli.main.run(
            ["replace", "foo", "bar", "-x", "--within", "anchor", "-A", "1", "--output-format=jsonl", str(target)]
        )
        == 0
    )
    assert target.read_bytes() == expected
    records = [json.loads(line) for line in capsys.readouterr().out.splitlines() if line.strip()]
    assert pyfltr.cli.main.run(["replace", "--undo", records[-1]["replace_id"], "--output-format=jsonl"]) == 0
    assert target.read_bytes() == original


def test_replace_dry_run_preserves_bytes(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    target = tmp_path / "sample.txt"
    original = b"\xef\xbb\xbffoo\r\n"
    target.write_bytes(original)
    monkeypatch.chdir(tmp_path)
    assert pyfltr.cli.main.run(["replace", "foo", "bar", "--dry-run", "--output-format=jsonl", str(target)]) == 0
    assert target.read_bytes() == original
    records = [json.loads(line) for line in capsys.readouterr().out.splitlines() if line.strip()]
    assert "replace_id" not in records[-1]


def test_replace_undo_detects_byte_only_edit(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    first = tmp_path / "first.txt"
    second = tmp_path / "second.txt"
    first.write_bytes(b"foo\r\n")
    second.write_bytes(b"foo\r\n")
    monkeypatch.chdir(tmp_path)
    assert pyfltr.cli.main.run(["replace", "foo", "bar", "--output-format=jsonl", str(first), str(second)]) == 0
    records = [json.loads(line) for line in capsys.readouterr().out.splitlines() if line.strip()]
    replace_id = records[-1]["replace_id"]
    first.write_bytes(b"bar\n")
    assert pyfltr.cli.main.run(["replace", "--undo", replace_id, "--output-format=jsonl"]) == 1
    assert first.read_bytes() == b"bar\n"
    assert second.read_bytes() == b"bar\r\n"
    capsys.readouterr()
    assert pyfltr.cli.main.run(["replace", "--undo", replace_id, "--force", "--output-format=jsonl"]) == 0
    assert first.read_bytes() == b"foo\r\n"
    assert second.read_bytes() == b"foo\r\n"


def test_replace_legacy_history_warning(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
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
    monkeypatch.chdir(tmp_path)
    assert pyfltr.cli.main.run(["replace", "--list-history", "--output-format=jsonl"]) == 0
    listed = [json.loads(line) for line in capsys.readouterr().out.splitlines() if line.strip()]
    assert any(record.get("replace_id") == replace_id for record in listed)
    assert pyfltr.cli.main.run(["replace", "--show-history", replace_id, "--output-format=jsonl"]) == 0
    shown = [json.loads(line) for line in capsys.readouterr().out.splitlines() if line.strip()]
    assert any(record.get("replace_id") == replace_id for record in shown)
    assert pyfltr.cli.main.run(["replace", "--undo", replace_id, "--output-format=jsonl"]) == 0
    assert target.read_text(encoding="cp932") == "foo 日本語\n"
    undone = [json.loads(line) for line in capsys.readouterr().out.splitlines() if line.strip()]
    assert any(record["kind"] == "warning" and "旧形式" in record["msg"] for record in undone)


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

    target.write_text("manually edited\n", encoding="utf-8")

    rc = pyfltr.cli.main.run(["replace", "--undo", replace_id, "--output-format=jsonl"])
    assert rc == 1
    captured = capsys.readouterr()
    records = [json.loads(line) for line in captured.out.splitlines() if line.strip()]
    warning = next(record for record in records if record["kind"] == "warning")
    assert "--force" in warning["msg"]
    assert "--force" not in captured.err
    assert target.read_text(encoding="utf-8") == "manually edited\n"

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
    assert record["file"] == target.relative_to(tmp_path).as_posix()
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
    assert target_b.read_text(encoding="utf-8") == "foo\n"
    capsys.readouterr()


def test_replace_from_grep_rejects_adaptively_compressed_output(tmp_path: pathlib.Path) -> None:
    grep_jsonl = tmp_path / "grep.jsonl"
    grep_jsonl.write_text(
        "\n".join(
            [
                json.dumps({"kind": "header", "output_mode": "mixed"}),
                json.dumps({"kind": "file", "file": "dense.txt", "count": 300, "matches": []}),
                json.dumps({"kind": "summary", "output_mode": "mixed"}),
            ]
        )
        + "\n",
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="full出力"):
        pyfltr.grep_.operations.read_from_grep(grep_jsonl)


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
    monkeypatch.chdir(tmp_path)
    rc = pyfltr.cli.main.run(["replace", "--undo", "NONEXISTENT", "--output-format=jsonl"])
    assert rc == 1
    err = capsys.readouterr().err
    assert "NONEXISTENT" in err
    assert "pyfltr replace --list-history" in err


def test_replace_undo_unreadable_history_reports_directory(
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
    saved_id = lines[-1]["replace_id"]
    history_dir = pyfltr.grep_.history.ReplaceHistoryStore().history_root / saved_id
    (history_dir / "meta.json").write_bytes(b"\xff\xfe broken")

    rc = pyfltr.cli.main.run(["replace", "--undo", saved_id, "--output-format=jsonl"])

    assert rc == 1
    err = capsys.readouterr().err
    assert str(history_dir) in err
    assert "取り消しに使えない" in err


def test_replace_show_history_returns_meta(
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
    lock = tmp_path / "uv.lock"
    lock.write_text("foo\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    rc = pyfltr.cli.main.run(["replace", "foo", "baz", "--dry-run", f"--output-format={output_format}", str(lock)])
    assert rc == 0
    out = capsys.readouterr().out
    if output_format == "jsonl":
        records = [json.loads(line) for line in out.splitlines() if line.strip()]
        summary = records[-1]
        assert summary["warnings"] == len([r for r in records if r["kind"] == "warning"])
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
    monkeypatch.chdir(tmp_path)
    rc = pyfltr.cli.main.run(
        ["replace", "foo", "baz", "--dry-run", f"--output-format={output_format}", str(tmp_path / "nope.py")]
    )
    assert rc == 0
    out = capsys.readouterr().out
    if output_format == "jsonl":
        records = [json.loads(line) for line in out.splitlines() if line.strip()]
        summary = records[-1]
        assert summary["warnings"] == len([r for r in records if r["kind"] == "warning"])
    else:
        summary = json.loads(out)["summary"]
    assert summary["missing_targets"] == ["nope.py"]


def test_replace_jsonl_summary_omits_warnings_when_none(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    target = tmp_path / "a.txt"
    target.write_text("foo bar\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    rc = pyfltr.cli.main.run(["replace", "foo", "baz", "--dry-run", "--output-format=jsonl", "--no-gitignore", str(target)])
    assert rc == 0
    records = [json.loads(line) for line in capsys.readouterr().out.splitlines() if line.strip()]
    assert not [r for r in records if r["kind"] == "warning"]
    assert "warnings" not in records[-1]


def test_replace_within_limits_to_region(tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch) -> None:
    target = tmp_path / "a.txt"
    target.write_text("foo\nL1\nKEY foo\nL3\nfoo\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)

    rc = pyfltr.cli.main.run(["replace", "foo", "X", str(target), "--within", "KEY", "-C", "1"])

    assert rc == 0
    assert target.read_text(encoding="utf-8") == "foo\nL1\nKEY X\nL3\nfoo\n"


def test_replace_without_within_replaces_whole_file(tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch) -> None:
    target = tmp_path / "a.txt"
    target.write_text("foo\nfoo\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)

    rc = pyfltr.cli.main.run(["replace", "foo", "X", str(target)])

    assert rc == 0
    assert target.read_text(encoding="utf-8") == "X\nX\n"


def test_replace_within_with_multiline_rejected(tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch) -> None:
    target = tmp_path / "a.txt"
    target.write_text("KEY foo\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    with pytest.raises(SystemExit) as exc_info:
        pyfltr.cli.main.run(["replace", "foo", "X", str(target), "--within", "KEY", "-U"])
    assert exc_info.value.code == 2


def test_replace_context_without_within_rejected(tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch) -> None:
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
    target = tmp_path / "a.txt"
    original = "foo\nKEY foo\n"
    target.write_text(original, encoding="utf-8")
    monkeypatch.chdir(tmp_path)

    rc = pyfltr.cli.main.run(["replace", "foo", "X", str(target), "--within", "KEY", "--dry-run", "--output-format=json"])

    assert rc == 0
    assert target.read_text(encoding="utf-8") == original
    summary = json.loads(capsys.readouterr().out)["summary"]
    assert summary["total_replacements"] == 1


def test_replace_within_context_expands_region(tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch) -> None:
    target = tmp_path / "a.txt"
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
    target = tmp_path / "a.txt"
    target.write_text("foo\nKEY foo\nfoo\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)

    rc = pyfltr.cli.main.run(["replace", "foo", "X", str(target), "--within", "KEY", "--show-changes", "--output-format=json"])

    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["changes"][0]["file"] == target.relative_to(tmp_path).as_posix()
    changes = payload["changes"][0]["changes"]
    assert [c["line"] for c in changes] == [2]


def test_replace_undo_json_normalizes_restored_file(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    target = tmp_path / "sub" / "a.txt"
    target.parent.mkdir()
    target.write_text("foo\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    rc = pyfltr.cli.main.run(["replace", "foo", "X", "--output-format=json", str(target)])
    assert rc == 0
    replace_id = json.loads(capsys.readouterr().out)["summary"]["replace_id"]

    rc = pyfltr.cli.main.run(["replace", "--undo", replace_id, "--output-format=json"])

    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["restored"] == [target.relative_to(tmp_path).as_posix()]
    assert payload["skipped"] == []


def test_replace_undo_json_normalizes_skipped_file(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    target = tmp_path / "sub" / "a.txt"
    target.parent.mkdir()
    target.write_text("foo\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    rc = pyfltr.cli.main.run(["replace", "foo", "X", "--output-format=json", str(target)])
    assert rc == 0
    replace_id = json.loads(capsys.readouterr().out)["summary"]["replace_id"]
    target.write_text("手動編集\n", encoding="utf-8")

    rc = pyfltr.cli.main.run(["replace", "--undo", replace_id, "--output-format=json"])

    assert rc == 1
    payload = json.loads(capsys.readouterr().out)
    assert payload["restored"] == []
    assert payload["skipped"] == [target.relative_to(tmp_path).as_posix()]


def test_replace_within_excludes_match_crossing_region_boundary(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = tmp_path / "a.txt"
    target.write_text("a\nKEY o\nb foo\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)

    rc = pyfltr.cli.main.run(["replace", "o\\nb", "X", str(target), "--within", "KEY"])

    assert rc == 0
    assert target.read_text(encoding="utf-8") == "a\nKEY o\nb foo\n"


def test_replace_within_undo_round_trip(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    target = tmp_path / "a.txt"
    original = "foo\nKEY foo\n"
    target.write_text(original, encoding="utf-8")
    monkeypatch.chdir(tmp_path)

    rc = pyfltr.cli.main.run(["replace", "foo", "X", str(target), "--within", "KEY", "--output-format=json"])
    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    replace_id = payload["summary"]["replace_id"]
    assert target.read_text(encoding="utf-8") == "foo\nKEY X\n"

    rc = pyfltr.cli.main.run(["replace", "--undo", replace_id])
    assert rc == 0
    assert target.read_text(encoding="utf-8") == original
