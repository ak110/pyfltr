"""文言・定数写し検査の単体起動と集約実行を確かめる。"""

import json
import pathlib
import subprocess
import sys

import pytest

_CASES = [
    (
        "sample_test.py",
        "from app import LABEL\ndef test_label():\n    assert LABEL == 'ok'\n",
        "def test_result():\n    assert str(5 + 1) == '6'\n",
        "def test_bad(\n",
        "#",
    ),
    (
        "sample.spec.tsx",
        'import { LABEL } from "./app";\ntest("label", () => { expect(LABEL).toBe("ok"); });\n',
        'test("result", () => { expect(render()).toHaveProperty("text"); });\n',
        'test("bad", () => {\n',
        "//",
    ),
    (
        "sample.test.ts",
        'import { LABEL } from "./app";\nit("label", () => { expect(LABEL).toBe("ok"); });\n',
        'it("result", () => { expect(render()).toHaveProperty("text"); });\n',
        'it("bad", () => {\n',
        "//",
    ),
    (
        "sample.rs",
        'use crate::LABEL;\n#[test]\nfn label() { assert_eq!(LABEL, "ok"); }\n',
        "#[test]\nfn result() { assert_eq!(transform(5), 6); }\n",
        "#[test]\nfn bad() {\n",
        "//",
    ),
]


def _run(root: pathlib.Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", *args],
        cwd=root,
        capture_output=True,
        encoding="utf-8",
        check=False,
        timeout=30,
    )


@pytest.mark.parametrize(
    ("case", "expected", "rules"),
    [
        ("copy", 1, ("constant-only",)),
        ("behavior", 0, ()),
        ("exempt", 0, ()),
        ("empty", 2, ("constant-only", "invalid-exception")),
        ("syntax", 2, ("input-error",)),
        ("read", 2, ("input-error",)),
    ],
)
def test_exit_status_and_diagnostics(tmp_path: pathlib.Path, case: str, expected: int, rules: tuple[str, ...]) -> None:
    """正常・候補・入力不良を各公開入口と各対象言語で区別する。"""
    names = []
    for name, copy, behavior, broken, comment in _CASES:
        names.append(name)
        first, rest = copy.split("\n", 1)
        marked = f"{first}\n{comment} test-copy-contract:"
        source = {
            "copy": copy,
            "behavior": behavior,
            "exempt": marked + " 外部仕様の互換性\n" + rest,
            "empty": marked + "\n" + rest,
            "syntax": "\n\n\n" + broken,
            "read": None,
        }[case]
        target = tmp_path / name
        if source is None:
            target.write_bytes(b"\xff")
        else:
            target.write_text(source, encoding="utf-8")
    (tmp_path / "pyproject.toml").write_text("[tool.pyfltr]\nrespect-gitignore = false\ntest-copies = true\n", encoding="utf-8")
    wanted = sorted((name, rule) for name in names for rule in rules)
    standalone = _run(tmp_path, "pyfltr.test_copies", "--json", *names)
    assert standalone.returncode == expected, standalone.stderr
    diagnostics = json.loads(standalone.stdout)["diagnostics"]
    assert sorted((item["path"], item["kind"]) for item in diagnostics) == wanted

    if case == "syntax":
        assert [item["line"] for item in diagnostics] == [4 if item["path"] != "sample.rs" else 5 for item in diagnostics]

    for mode in ("run", "ci"):
        wrapped = _run(tmp_path, "pyfltr", mode, "--commands=test-copies", "--output-format=jsonl", "--no-quiet", *names)
        assert wrapped.returncode == int(expected != 0), (wrapped.stdout, wrapped.stderr)
        records = [
            record
            for record in map(json.loads, wrapped.stdout.splitlines())
            if record.get("kind") == "diagnostic" and record.get("command") == "test-copies"
        ]
        reported = sorted((record["file"], message["rule"]) for record in records for message in record["messages"])
        assert reported == wanted
        if case == "syntax":
            assert {record["file"]: record["messages"][0]["line"] for record in records} == {
                item["path"]: item["line"] for item in diagnostics
            }


def test_disabled_without_opt_in(tmp_path: pathlib.Path) -> None:
    """設定しなければ候補があるファイルでも検査しない。"""
    name, source, *_ = _CASES[0]
    (tmp_path / name).write_text(source, encoding="utf-8")
    result = _run(tmp_path, "pyfltr", "run", "--commands=test-copies", "--output-format=jsonl", "--no-quiet", name)
    assert result.returncode == 0, result.stderr
    assert not any(
        record.get("kind") == "command" and record.get("command") == "test-copies"
        for record in map(json.loads, result.stdout.splitlines())
    )


def test_non_test_files_are_ignored(tmp_path: pathlib.Path) -> None:
    """対象外のTSや、テスト属性がコメントにしかないRustは診断しない。"""
    (tmp_path / "app.ts").write_text("(", encoding="utf-8")
    (tmp_path / "app.rs").write_text("// #[test]\nfn app() {}\n", encoding="utf-8")
    result = _run(tmp_path, "pyfltr.test_copies", "app.ts", "app.rs")
    assert result.returncode == 0, result.stderr
    assert not result.stderr


def test_source_text_copy_reports_text_rule(tmp_path: pathlib.Path) -> None:
    """定数の比較とは別に、実装から読み取った文字列の比較を報告する。"""
    name = "sample_spec.py"
    (tmp_path / name).write_text(
        "from pathlib import Path\ndef test_label():\n    assert 'ok' in Path('app.py').read_text()\n",
        encoding="utf-8",
    )
    result = _run(tmp_path, "pyfltr.test_copies", name)
    assert result.returncode == 1
    assert f"{name}:2: error: text-only:" in result.stderr
