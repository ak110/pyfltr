import json
import pathlib

import pytest

import pyfltr.diagnostics
import pyfltr.output.diagnostics
import pyfltr.parsing.entry
import pyfltr.parsing.pytest
import pyfltr.tools
import tests.parsing.conftest


def test_parse_ruff_check_json() -> None:
    """ruff check --output-format=json出力のパース。"""
    output = json.dumps(
        [
            {
                "code": "F401",
                "message": "`os` imported but unused",
                "filename": "src/foo.py",
                "location": {"row": 1, "column": 8},
                "end_location": {"row": 2, "column": 10},
                "severity": "error",
                "fix": {"applicability": "safe", "edits": []},
            },
        ]
    )
    errors = pyfltr.parsing.entry.parse_errors("ruff-check", output)
    assert len(errors) == 1
    assert tests.parsing.conftest.diagnostic_fields(errors[0]) == (
        "src/foo.py",
        1,
        8,
        "ruff-check",
        "`os` imported but unused",
        "F401",
        "error",
        "safe",
        2,
        10,
    )


def test_parse_ruff_check_json_fallback() -> None:
    """ruff-check: JSONでない出力はregexにフォールバックする。"""
    output = "src/foo.py:10:5: F401 `os` imported but unused"
    errors = pyfltr.parsing.entry.parse_errors("ruff-check", output)
    assert len(errors) == 1
    assert errors[0].file == "src/foo.py"
    assert errors[0].line == 10
    assert errors[0].col == 5
    assert errors[0].command == "ruff-check"
    assert errors[0].message == "F401 `os` imported but unused"
    assert errors[0].end_line is None
    assert errors[0].end_col is None


def test_parse_ruff_check_json_fix_none() -> None:
    """ruff-check: `fix`欠落エントリは`fix == "none"`として出力される。"""
    output = json.dumps(
        [
            {
                "code": "E501",
                "message": "line too long",
                "filename": "src/foo.py",
                "location": {"row": 2, "column": 1},
                "end_location": {"row": 2, "column": 130},
                "severity": "error",
            },
        ]
    )
    errors = pyfltr.parsing.entry.parse_errors("ruff-check", output)
    assert len(errors) == 1
    assert errors[0].fix == "none"


def test_parse_pylint_json() -> None:
    """pylint --output-format=json2出力のパース。

    ruleにはsymbol（公式ドキュメントURL基準）、messageにはmessageIdを保持する。
    """
    output = json.dumps(
        {
            "messages": [
                {
                    "messageId": "C0114",
                    "symbol": "missing-module-docstring",
                    "message": "Missing module docstring",
                    "path": "src/foo.py",
                    "line": 1,
                    "column": 0,
                    "endLine": 2,
                    "endColumn": 7,
                    "type": "convention",
                },
            ],
            "statistics": {},
        }
    )
    errors = pyfltr.parsing.entry.parse_errors("pylint", output)
    assert len(errors) == 1
    assert tests.parsing.conftest.diagnostic_fields(errors[0]) == (
        "src/foo.py",
        1,
        1,
        "pylint",
        "C0114: Missing module docstring",
        "missing-module-docstring",
        "warning",
        None,
        2,
        8,
    )
    assert errors[0].rule_url == (
        "https://pylint.readthedocs.io/en/stable/user_guide/messages/convention/missing-module-docstring.html"
    )


def test_parse_pylint_json_with_stderr_prefix() -> None:
    """pylint: JSON前にstderrの警告などが混ざっても最初の`{`以降をパースする。

    Windows + Python 3.14 + PYTHONDEVMODE=1でpylint_pydanticが大量の
    DeprecationWarningをemitし、pylintの出力先頭に紛れ込む現象への対処。
    """
    body = json.dumps(
        {
            "messages": [
                {
                    "messageId": "C0114",
                    "symbol": "missing-module-docstring",
                    "message": "Missing module docstring",
                    "path": "src/foo.py",
                    "line": 1,
                    "column": 0,
                    "type": "convention",
                },
            ],
            "statistics": {},
        }
    )
    prefix = (
        "Captured stderr while importing pylint_pydantic:\n"
        "site-packages/pylint_pydantic/__init__.py:2: DeprecationWarning: ...\n"
    )
    errors = pyfltr.parsing.entry.parse_errors("pylint", prefix + body)
    assert len(errors) == 1
    assert errors[0].rule == "missing-module-docstring"


def test_parse_pylint_json_keeps_inclusive_end_line() -> None:
    """終了行が最終行を指すツールでは値が変わらない。"""
    output = json.dumps(
        {
            "messages": [
                {
                    "messageId": "C0301",
                    "symbol": "line-too-long",
                    "message": "Line too long",
                    "path": "src/a.py",
                    "line": 3,
                    "column": 0,
                    "endLine": 5,
                    "endColumn": 4,
                    "type": "convention",
                }
            ]
        }
    )

    errors = pyfltr.parsing.entry.parse_errors("pylint", output)

    assert len(errors) == 1
    assert errors[0].end_line == 5


def test_parse_pylint_json_fallback() -> None:
    """pylint: JSONでない出力はregexにフォールバックする。"""
    output = "src/foo.py:10:5: C0114: Missing module docstring (missing-module-docstring)"
    errors = pyfltr.parsing.entry.parse_errors("pylint", output)
    assert len(errors) == 1
    assert errors[0].line == 10
    assert errors[0].col == 6
    assert errors[0].end_line is None
    assert errors[0].end_col is None


def test_parse_pyright_json() -> None:
    """pyright --outputjson出力のパース。"""
    output = json.dumps(
        {
            "version": "1.1.400",
            "generalDiagnostics": [
                {
                    "file": "src/foo.py",
                    "range": {"start": {"line": 9, "character": 4}, "end": {"line": 10, "character": 10}},
                    "severity": "error",
                    "rule": "reportAssignmentType",
                    "message": "Type mismatch",
                },
            ],
            "summary": {"errorCount": 1},
        }
    )
    errors = pyfltr.parsing.entry.parse_errors("pyright", output)
    assert len(errors) == 1
    assert tests.parsing.conftest.diagnostic_fields(errors[0]) == (
        "src/foo.py",
        10,
        5,
        "pyright",
        "Type mismatch",
        "reportAssignmentType",
        "error",
        None,
        11,
        11,
    )


def test_parse_pyright_json_fallback() -> None:
    """pyright: JSONでない出力はregexにフォールバックする。"""
    output = '  src/foo.py:10:5 - error: Type "int" is not assignable'
    errors = pyfltr.parsing.entry.parse_errors("pyright", output)
    assert len(errors) == 1
    assert errors[0].file == "src/foo.py"
    assert errors[0].line == 10
    assert errors[0].col == 5
    assert errors[0].command == "pyright"
    assert errors[0].message == 'Type "int" is not assignable'
    assert errors[0].end_line is None
    assert errors[0].end_col is None


def test_parse_pyright_json_normalizes_exclusive_end_line() -> None:
    """行末で終わる範囲の終了行は直前の行へ補正される。"""
    output = json.dumps(
        {
            "generalDiagnostics": [
                {
                    "file": "src/a.py",
                    "range": {"start": {"line": 0, "character": 4}, "end": {"line": 2, "character": 0}},
                    "message": "String literal is unterminated",
                    "severity": "error",
                }
            ]
        }
    )

    errors = pyfltr.parsing.entry.parse_errors("pyright", output)

    assert len(errors) == 1
    assert errors[0].line == 1
    assert errors[0].end_line == 2
    assert errors[0].end_col is None


def test_parse_pyright_json_keeps_inclusive_end_line() -> None:
    """行内で終わる範囲の終了行はそのまま保持される。"""
    output = json.dumps(
        {
            "generalDiagnostics": [
                {
                    "file": "src/a.py",
                    "range": {"start": {"line": 4, "character": 9}, "end": {"line": 8, "character": 1}},
                    "message": "Operator error",
                    "severity": "error",
                }
            ]
        }
    )

    errors = pyfltr.parsing.entry.parse_errors("pyright", output)

    assert len(errors) == 1
    assert errors[0].line == 5
    assert errors[0].end_line == 9
    assert errors[0].end_col == 2


def test_parse_pyright_json_keeps_zero_width_range() -> None:
    """開始行と終了行が同じゼロ幅の範囲は補正しない。"""
    output = json.dumps(
        {
            "generalDiagnostics": [
                {
                    "file": "src/a.py",
                    "range": {"start": {"line": 2, "character": 0}, "end": {"line": 2, "character": 0}},
                    "message": "Expected indented block",
                    "severity": "error",
                }
            ]
        }
    )

    errors = pyfltr.parsing.entry.parse_errors("pyright", output)

    assert len(errors) == 1
    assert errors[0].line == 3
    assert errors[0].end_line == 3
    assert errors[0].end_col == 1


def test_parse_arid_json_expands_all_locations() -> None:
    """aridの1つのfindingが持つ全locationを診断へ展開する。"""
    output = json.dumps(
        {
            "findings": [
                {
                    "code": "DUP001",
                    "lines": 12,
                    "context": "function",
                    "scope": "module",
                    "occurrences": 2,
                    "distribution": "cross-file",
                    "locations": [
                        {"path": "src/a.py", "start_line": 10, "end_line": 21},
                        {"path": "src/b.py", "start_line": 30, "end_line": 41},
                    ],
                }
            ]
        }
    )

    errors = pyfltr.parsing.entry.parse_errors("arid", output)

    assert [tests.parsing.conftest.diagnostic_fields(error) for error in errors] == [
        (
            "src/a.py",
            10,
            None,
            "arid",
            "12 duplicated lines (function/module, cross-file, 2 occurrences); other locations: src/b.py:30-41",
            "DUP001",
            "error",
            None,
            21,
            None,
        ),
        (
            "src/b.py",
            30,
            None,
            "arid",
            "12 duplicated lines (function/module, cross-file, 2 occurrences); other locations: src/a.py:10-21",
            "DUP001",
            "error",
            None,
            41,
            None,
        ),
    ]


def test_parse_arid_json_resolves_paths_from_subproject_cwd(monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path) -> None:
    """aridの相対診断パスを実行したサブプロジェクトcwdから解決する。"""
    monkeypatch.chdir(tmp_path)
    output = json.dumps(
        {
            "findings": [
                {
                    "code": "DUP001",
                    "lines": 12,
                    "context": "function",
                    "scope": "module",
                    "occurrences": 2,
                    "distribution": "cross-file",
                    "locations": [
                        {"path": "src/a.py", "start_line": 10, "end_line": 21},
                        {"path": "src/b.py", "start_line": 30, "end_line": 41},
                    ],
                }
            ]
        }
    )

    errors = pyfltr.parsing.entry.parse_errors("arid", output, path_base=tmp_path / "packages" / "app")

    assert [error.file for error in errors] == ["packages/app/src/a.py", "packages/app/src/b.py"]
    assert "other locations: packages/app/src/b.py:30-41" in errors[0].message
    assert "other locations: packages/app/src/a.py:10-21" in errors[1].message


@pytest.mark.parametrize(
    "output",
    [
        "not json",
        "[]",
        '{"findings": {}}',
        '{"findings": [{"lines": 12, "context": "function", "occurrences": 2, "distribution": "same-file"}]}',
        '{"findings": ["not a dict"]}',
        '{"findings": [{"lines": 12, "context": "function", "scope": "module",'
        ' "occurrences": 2, "distribution": "same-file", "locations": {}}]}',
        # 致命エラー時のaridは`findings`を持たない別スキーマを返す。
        '{"schema_version": 1, "tool_version": "2.2.2",'
        ' "error": {"kind": "parse", "message": "invalid Python syntax at line 1, column 7"}}',
    ],
)
def test_parse_arid_json_ignores_invalid_input(output: str) -> None:
    """aridのJSON構造が不正な場合は診断を生成しない。"""
    assert not pyfltr.parsing.entry.parse_errors("arid", output)


def test_parse_arid_json_keeps_absolute_location_path(monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path) -> None:
    """aridが絶対パスを返した場合は`path_base`と結合しない。"""
    monkeypatch.chdir(tmp_path)
    absolute_target = tmp_path / "external" / "sample.py"
    output = json.dumps(
        {
            "findings": [
                {
                    "code": "DUP001",
                    "lines": 12,
                    "context": "function",
                    "scope": "module",
                    "occurrences": 1,
                    "distribution": "same-file",
                    "locations": [{"path": str(absolute_target), "start_line": 10, "end_line": 21}],
                }
            ]
        }
    )

    errors = pyfltr.parsing.entry.parse_errors("arid", output, path_base=tmp_path / "packages" / "app")

    assert [error.file for error in errors] == ["external/sample.py"]


def test_parse_arid_json_single_location_omits_other_locations() -> None:
    """locationが1つだけのfindingでは他location一覧を付けない。"""
    output = json.dumps(
        {
            "findings": [
                {
                    "code": "DUP001",
                    "lines": 12,
                    "context": "function",
                    "scope": "module",
                    "occurrences": 1,
                    "distribution": "same-file",
                    "locations": [{"path": "src/a.py", "start_line": 10, "end_line": 21}],
                }
            ]
        }
    )

    errors = pyfltr.parsing.entry.parse_errors("arid", output)

    assert len(errors) == 1
    assert errors[0].message == "12 duplicated lines (function/module, same-file, 1 occurrences)"


def test_parse_arid_json_skips_invalid_locations_only() -> None:
    """不正なlocationだけを除外し、同じfindingの有効なlocationは診断へ変換する。"""
    output = json.dumps(
        {
            "findings": [
                {
                    "code": "DUP001",
                    "lines": 12,
                    "context": "function",
                    "scope": "module",
                    "occurrences": 3,
                    "distribution": "cross-file",
                    "locations": [
                        "not a dict",
                        {"path": "src/a.py", "start_line": 10},
                        {"path": "", "start_line": 10, "end_line": 21},
                        {"path": "src/b.py", "start_line": 30, "end_line": 41},
                    ],
                }
            ]
        }
    )

    errors = pyfltr.parsing.entry.parse_errors("arid", output)

    assert [error.file for error in errors] == ["src/b.py"]


def test_parse_arid_json_ignores_unknown_fields_of_real_output() -> None:
    """aridの実出力に含まれる未知フィールドを無視して診断へ変換する。

    テスト入力はarid 2.2.2のreport schema_version 4の実出力構造から採る。
    """
    output = json.dumps(
        {
            "schema_version": 4,
            "tool_version": "2.2.2",
            "complete": True,
            "analysis": {"min_lines": 10, "same_file": True, "exclude": []},
            "errors": [],
            "files": 2,
            "duplicate_groups": 1,
            "duplication_percent": 50.0,
            "findings": [
                {
                    "code": "DUP001",
                    "fingerprint": "arid-finding-v1:sha256:fa2c",
                    "lines": 10,
                    "context": "executable",
                    "scope": "function",
                    "occurrences": 2,
                    "files": 2,
                    "distribution": "cross-file",
                    "locations": [
                        {"path": "pkg/a.py", "start_line": 2, "end_line": 11},
                        {"path": "pkg/b.py", "start_line": 2, "end_line": 11},
                    ],
                }
            ],
        }
    )

    errors = pyfltr.parsing.entry.parse_errors("arid", output)

    assert [error.file for error in errors] == ["pkg/a.py", "pkg/b.py"]
    assert [error.rule for error in errors] == ["DUP001", "DUP001"]
    assert [(error.line, error.end_line) for error in errors] == [(2, 11), (2, 11)]


def test_parse_summary_pyright_json() -> None:
    """pyright: JSON出力のsummaryフィールドからサマリーを抽出する。"""
    output = json.dumps(
        {
            "version": "1.1.300",
            "generalDiagnostics": [],
            "summary": {
                "filesAnalyzed": 50,
                "errorCount": 0,
                "warningCount": 2,
                "informationCount": 0,
                "timeInSec": 1.5,
            },
        }
    )
    result = pyfltr.parsing.entry.parse_summary("pyright", output)
    assert result == "50 files analyzed, 0 errors, 2 warnings"


def test_parse_summary_pyright_json_no_summary() -> None:
    """pyright: summaryフィールドがない場合はNone。"""
    output = json.dumps({"generalDiagnostics": []})
    assert pyfltr.parsing.entry.parse_summary("pyright", output) is None


def test_parse_summary_pylint_json() -> None:
    """pylint: JSON出力のstatisticsフィールドからサマリーを抽出する。"""
    output = json.dumps(
        {
            "messages": [],
            "statistics": {
                "modulesLinted": 42,
                "score": 10.0,
                "messageTypeCount": {},
            },
        }
    )
    result = pyfltr.parsing.entry.parse_summary("pylint", output)
    assert result == "42 modules linted, score: 10.0"


def test_parse_summary_pylint_json_no_score() -> None:
    """pylint: scoreがない場合はモジュール数のみ。"""
    output = json.dumps({"messages": [], "statistics": {"modulesLinted": 10}})
    result = pyfltr.parsing.entry.parse_summary("pylint", output)
    assert result == "10 modules linted"


def test_parse_errors_ruff_rule_url_from_entry() -> None:
    """ruff JSONの`url`フィールドを最優先で採用する。"""
    output = json.dumps(
        [
            {
                "code": "F401",
                "message": "`os` imported but unused",
                "filename": "src/foo.py",
                "location": {"row": 1, "column": 8},
                "severity": "error",
                "url": "https://example.com/custom-ruff-url",
            },
        ]
    )
    errors = pyfltr.parsing.entry.parse_errors("ruff-check", output)
    assert len(errors) == 1
    assert errors[0].rule_url == "https://example.com/custom-ruff-url"


def test_parse_errors_ruff_rule_url_fallback() -> None:
    """ruff JSONに`url`が無い場合はテンプレートで生成する。"""
    output = json.dumps(
        [
            {
                "code": "F401",
                "message": "`os` imported but unused",
                "filename": "src/foo.py",
                "location": {"row": 1, "column": 8},
                "severity": "error",
            },
        ]
    )
    errors = pyfltr.parsing.entry.parse_errors("ruff-check", output)
    assert len(errors) == 1
    assert errors[0].rule_url == "https://docs.astral.sh/ruff/rules/F401/"


def test_parse_errors_pyright_rule_url() -> None:
    """pyrightのruleからrule_urlが生成される。"""
    output = json.dumps(
        {
            "version": "1.1.400",
            "generalDiagnostics": [
                {
                    "file": "src/foo.py",
                    "range": {"start": {"line": 9, "character": 4}, "end": {"line": 9, "character": 10}},
                    "severity": "error",
                    "rule": "reportAssignmentType",
                    "message": "Type mismatch",
                },
            ],
        }
    )
    errors = pyfltr.parsing.entry.parse_errors("pyright", output)
    assert errors[0].rule_url == "https://microsoft.github.io/pyright/#/configuration?id=reportAssignmentType"
