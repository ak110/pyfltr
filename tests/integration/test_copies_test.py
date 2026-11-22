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
        'import { LABEL } from "./app";\n'
        'test("label", <A extends Map<string, number>, B,>() => (expect(LABEL).toBe("ok")), 100);\n',
        'test("double", () => expect(double(2)).toBe(4));\n',
        'test("bad", () => {\n',
        "//",
    ),
    (
        "sample.test.ts",
        'import { LABEL } from "./app";\ntest("label", <A, B>() => { expect(LABEL).toBe("ok"); });\n',
        'it("result", () => expect(render()).toHaveProperty("text"));\n',
        'it("bad", () => );\n',
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

    plain = _run(tmp_path, "pyfltr.test_copies", *names)
    assert plain.returncode == expected, plain.stderr
    input_action = (
        "UTF-8で保存し直すか、UTF-8で読めるファイルを指定する。" if case == "read" else "対象の構文・パス・読取権限を修正する。"
    )
    actions = {
        (name, rule): (input_action if rule == "input-error" else f"{comment} test-copy-contract: <保証する独立契約の理由>")
        for name, *_, comment in _CASES
        for rule in rules
    }
    for (name, rule), action in actions.items():
        assert any(
            line.startswith(f"{name}:") and f": error: {rule}: " in line and action in line
            for line in plain.stderr.splitlines()
        )
        assert action in json.loads(standalone.stdout)["next_action"]

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
        for record in records:
            for message in record["messages"]:
                assert actions[record["file"], message["rule"]] in message["msg"]
        if case == "syntax":
            assert {record["file"]: record["messages"][0]["line"] for record in records} == {
                item["path"]: item["line"] for item in diagnostics
            }


def test_disabled_without_opt_in(tmp_path: pathlib.Path) -> None:
    name, source, *_ = _CASES[0]
    (tmp_path / name).write_text(source, encoding="utf-8")
    result = _run(tmp_path, "pyfltr", "run", "--commands=test-copies", "--output-format=jsonl", "--no-quiet", name)
    assert result.returncode == 0, result.stderr
    assert not any(
        record.get("kind") == "command" and record.get("command") == "test-copies"
        for record in map(json.loads, result.stdout.splitlines())
    )


def test_non_test_files_are_ignored(tmp_path: pathlib.Path) -> None:
    (tmp_path / "app.ts").write_text("(", encoding="utf-8")
    (tmp_path / "app.rs").write_text("// #[test]\nfn app() {}\n", encoding="utf-8")
    result = _run(tmp_path, "pyfltr.test_copies", "app.ts", "app.rs")
    assert result.returncode == 0, result.stderr
    assert not result.stderr


def test_source_text_copy_reports_text_rule(tmp_path: pathlib.Path) -> None:
    name = "sample_spec.py"
    (tmp_path / name).write_text(
        "from pathlib import Path\ndef test_label():\n    assert 'ok' in Path('app.py').read_text()\n",
        encoding="utf-8",
    )
    result = _run(tmp_path, "pyfltr.test_copies", name)
    assert result.returncode == 1
    assert f"{name}:2: error: text-only:" in result.stderr
