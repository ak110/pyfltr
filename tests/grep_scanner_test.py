"""grep_/scanner.py のテスト。"""

import pathlib
import re

import pyfltr.grep_.scanner
import pyfltr.grep_.types
import pyfltr.warnings_


def _scan(
    files: list[pathlib.Path],
    pattern: re.Pattern[str],
    *,
    before_context: int = 0,
    after_context: int = 0,
    max_per_file: int = 0,
    max_total: int = 0,
    encoding: str = "utf-8",
    max_filesize: int | None = None,
    multiline: bool = False,
) -> list[pyfltr.grep_.types.MatchRecord | pyfltr.grep_.types.FileMatchSummary]:
    """テスト用ヘルパー。`scan_files`を全件消費してリスト化する。"""
    return list(
        pyfltr.grep_.scanner.scan_files(
            files,
            pattern,
            before_context=before_context,
            after_context=after_context,
            max_per_file=max_per_file,
            max_total=max_total,
            encoding=encoding,
            max_filesize=max_filesize,
            multiline=multiline,
        )
    )


def test_scan_files_basic_match(tmp_path: pathlib.Path) -> None:
    """基本マッチで行番号・列番号・本文が正しく取得できる。"""
    target = tmp_path / "a.py"
    target.write_text("alpha\nbeta foo\ngamma\n", encoding="utf-8")
    pattern = re.compile(r"foo")

    records = _scan([target], pattern)

    assert len(records) == 1
    rec = records[0]
    assert isinstance(rec, pyfltr.grep_.types.MatchRecord)
    assert rec.file == target
    assert rec.line == 2
    assert rec.col == 6
    assert rec.end_col == 9
    assert rec.line_text == "beta foo"
    assert rec.match_text == "foo"


def test_scan_files_before_after_context(tmp_path: pathlib.Path) -> None:
    """前後コンテキストが要求行数だけ含まれる。"""
    target = tmp_path / "a.txt"
    target.write_text("L1\nL2\nL3 foo\nL4\nL5\n", encoding="utf-8")
    pattern = re.compile(r"foo")

    records = _scan([target], pattern, before_context=2, after_context=1)

    assert len(records) == 1
    rec = records[0]
    assert isinstance(rec, pyfltr.grep_.types.MatchRecord)
    assert rec.before_lines == ["L1", "L2"]
    assert rec.after_lines == ["L4"]


def test_scan_files_context_deduplicates_overlap(tmp_path: pathlib.Path) -> None:
    """近接マッチで前マッチのafterと次マッチのbeforeが重複しない。"""
    target = tmp_path / "a.txt"
    target.write_text("L1\nfoo1\nL2\nfoo2\nL3\n", encoding="utf-8")
    pattern = re.compile(r"foo\d")

    records = _scan([target], pattern, before_context=2, after_context=1)

    assert len(records) == 2
    first, second = records
    assert isinstance(first, pyfltr.grep_.types.MatchRecord)
    assert isinstance(second, pyfltr.grep_.types.MatchRecord)
    # 1件目: before=["L1"], after=["L2"]
    assert first.before_lines == ["L1"]
    assert first.after_lines == ["L2"]
    # 2件目: 1件目のafter=「L2」と2件目のbefore=「L2」が重複しないよう
    # 2件目のbeforeは空となる（L2は1件目のafterで既に含まれている）
    assert second.before_lines == []


def test_scan_files_per_file_limit(tmp_path: pathlib.Path) -> None:
    """max_per_fileでファイルごとのマッチ数が制限される。"""
    target = tmp_path / "a.txt"
    target.write_text("foo\nfoo\nfoo\nfoo\n", encoding="utf-8")
    pattern = re.compile(r"foo")

    records = _scan([target], pattern, max_per_file=2)

    assert len(records) == 2


def test_scan_files_total_limit(tmp_path: pathlib.Path) -> None:
    """max_totalで全体のマッチ数が制限される。"""
    file_a = tmp_path / "a.txt"
    file_a.write_text("foo\nfoo\n", encoding="utf-8")
    file_b = tmp_path / "b.txt"
    file_b.write_text("foo\nfoo\n", encoding="utf-8")
    pattern = re.compile(r"foo")

    records = _scan([file_a, file_b], pattern, max_total=3)

    assert len(records) == 3


def test_scan_files_multiline_mode(tmp_path: pathlib.Path) -> None:
    """multiline=True で改行を跨ぐマッチを検出する。"""
    target = tmp_path / "a.txt"
    target.write_text("alpha\nfoo\nbar\nbeta\n", encoding="utf-8")
    pattern = re.compile(r"foo\nbar", re.DOTALL | re.MULTILINE)

    records = _scan([target], pattern, multiline=True)

    assert len(records) == 1
    rec = records[0]
    assert isinstance(rec, pyfltr.grep_.types.MatchRecord)
    assert rec.line == 2
    assert rec.match_text == "foo\nbar"


def test_scan_files_max_filesize_skips_large(tmp_path: pathlib.Path) -> None:
    """max_filesize超のファイルはスキップされる。"""
    small = tmp_path / "small.txt"
    small.write_text("foo\n", encoding="utf-8")
    big = tmp_path / "big.txt"
    big.write_text("foo " * 1000, encoding="utf-8")
    pattern = re.compile(r"foo")

    records = _scan([small, big], pattern, max_filesize=10)

    # smallのみマッチ。大きいbigはスキップされる
    assert len(records) == 1
    rec = records[0]
    assert isinstance(rec, pyfltr.grep_.types.MatchRecord)
    assert rec.file == small


def test_scan_files_decode_error_emits_warning(tmp_path: pathlib.Path) -> None:
    """デコードエラー発生時はスキップしつつwarningを発行する。"""
    target = tmp_path / "a.bin"
    target.write_bytes(b"\xff\xfe foo \xfd")
    pattern = re.compile(r"foo")

    records = _scan([target], pattern, encoding="utf-8")

    assert not records
    warnings = pyfltr.warnings_.collected_warnings()
    assert any(w["source"] == "grep" for w in warnings)


def test_filter_files_by_type_python() -> None:
    """python タイプで`.py`/`.pyi`ファイルのみ残る。"""
    files = [
        pathlib.Path("a.py"),
        pathlib.Path("b.pyi"),
        pathlib.Path("c.txt"),
        pathlib.Path("d.rs"),
    ]
    result = pyfltr.grep_.scanner.filter_files_by_type(files, ["python"])
    assert result == [pathlib.Path("a.py"), pathlib.Path("b.pyi")]


def test_filter_files_by_type_multiple_types() -> None:
    """複数タイプ指定でいずれかに一致するファイルが残る。"""
    files = [
        pathlib.Path("a.py"),
        pathlib.Path("b.ts"),
        pathlib.Path("c.tsx"),
        pathlib.Path("d.rs"),
        pathlib.Path("e.txt"),
    ]
    result = pyfltr.grep_.scanner.filter_files_by_type(files, ["python", "ts"])
    assert sorted(str(f) for f in result) == ["a.py", "b.ts", "c.tsx"]


def test_filter_files_by_type_all_nine_kinds(tmp_path: pathlib.Path) -> None:
    """python/rust/ts/js/md/json/toml/yaml/shellの9種が網羅される。"""
    del tmp_path  # 未使用
    samples: dict[str, list[str]] = {
        "python": ["a.py", "a.pyi"],
        "rust": ["a.rs"],
        "ts": ["a.ts", "a.tsx"],
        "js": ["a.js", "a.jsx", "a.mjs", "a.cjs"],
        "md": ["a.md", "a.markdown"],
        "json": ["a.json"],
        "toml": ["a.toml"],
        "yaml": ["a.yaml", "a.yml"],
        "shell": ["a.sh", "a.bash", "a.zsh"],
    }
    for type_name, expected_names in samples.items():
        files = [pathlib.Path(name) for name in expected_names]
        result = pyfltr.grep_.scanner.filter_files_by_type(files, [type_name])
        assert sorted(str(f) for f in result) == sorted(expected_names), f"type {type_name} で期待ファイルがフィルタ通過しない"


def test_filter_files_by_type_unknown_type() -> None:
    """未知のタイプ名は空リストになる。"""
    files = [pathlib.Path("a.py")]
    result = pyfltr.grep_.scanner.filter_files_by_type(files, ["unknown_lang"])
    assert result == []


def test_filter_files_by_type_empty_types_returns_all() -> None:
    """typesが空ならフィルタを行わない。"""
    files = [pathlib.Path("a.py"), pathlib.Path("b.txt")]
    result = pyfltr.grep_.scanner.filter_files_by_type(files, [])
    assert result == files


def test_filter_by_globs_basic() -> None:
    """globパターンでフィルタリングできる。"""
    files = [
        pathlib.Path("a.py"),
        pathlib.Path("b.txt"),
        pathlib.Path("c.py"),
    ]
    result = pyfltr.grep_.scanner.filter_by_globs(files, ["*.py"])
    assert result == [pathlib.Path("a.py"), pathlib.Path("c.py")]


def test_filter_by_globs_empty_returns_all() -> None:
    """globsが空なら全件返す。"""
    files = [pathlib.Path("a.py"), pathlib.Path("b.txt")]
    assert pyfltr.grep_.scanner.filter_by_globs(files, []) == files
