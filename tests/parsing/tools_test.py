import json
import pathlib

import pytest

import pyfltr.diagnostics
import pyfltr.output.diagnostics
import pyfltr.parsing.entry
import pyfltr.parsing.pytest
import pyfltr.tools
import tests.parsing.conftest


@pytest.mark.parametrize(
    "command,output,expected_count,expected_first_file,expected_first_line",
    [
        # mypy
        (
            "mypy",
            'src/foo.py:10: error: Name "x" is not defined  [name-defined]\nsrc/bar.py:20: error: Missing return  [return]',
            2,
            "src/foo.py",
            10,
        ),
        # pylint
        (
            "pylint",
            "src/foo.py:10:5: C0114: Missing module docstring (missing-module-docstring)",
            1,
            "src/foo.py",
            10,
        ),
        (
            "ruff-check",
            "src/foo.py:10:5: F401 `os` imported but unused\nsrc/bar.py:3:1: E302 Expected 2 blank lines",
            2,
            "src/foo.py",
            10,
        ),
        # pyright
        (
            "pyright",
            '  src/foo.py:10:5 - error: Type "int" is not assignable',
            1,
            "src/foo.py",
            10,
        ),
        (
            "markdownlint",
            "docs/index.md:3 MD001/heading-increment Heading levels should only increment by one level at a time",
            1,
            "docs/index.md",
            3,
        ),
        (
            "textlint",
            "docs/index.md: line 5, col 1, Error - sentence error (ja-technical-writing/ja-no-mixed-period)",
            1,
            "docs/index.md",
            5,
        ),
        (
            "ty",
            "src/foo.py:10:5: error[invalid-argument-type] Argument is incorrect",
            1,
            "src/foo.py",
            10,
        ),
        (
            "ty",
            "src/foo.py:3:1: warning[unused-ignore-comment] Unused `ty: ignore` directive",
            1,
            "src/foo.py",
            3,
        ),
        (
            "ty",
            "src/reveal.py:2:13: info[revealed-type] Revealed type: `Literal[1]`",
            1,
            "src/reveal.py",
            2,
        ),
        (
            "pytest",
            "FAILED tests/foo_test.py::test_bar - AssertionError: xxx",
            1,
            "tests/foo_test.py",
            0,
        ),
        (
            "biome",
            "::error title=lint/suspicious/noDoubleEquals,file=src/foo.ts,"
            "line=1,endLine=1,col=7,endColumn=9::Use === instead of ==",
            1,
            "src/foo.ts",
            1,
        ),
        (
            "biome",
            "::warning title=lint/style/useConst,file=src/bar.ts,line=5,endLine=5,col=3,endColumn=6::Use const instead of let",
            1,
            "src/bar.ts",
            5,
        ),
        (
            "biome",
            "::notice title=lint/complexity/useLiteralKeys,file=src/baz.ts,"
            "line=810,endLine=810,col=49,endColumn=56::Use a literal key instead.",
            1,
            "src/baz.ts",
            810,
        ),
        (
            "biome",
            "::unknown title=lint/foo/bar,file=src/qux.ts,line=1,endLine=1,col=1,endColumn=2::msg",
            0,
            None,
            None,
        ),
        (
            "unknown",
            "some output",
            0,
            None,
            None,
        ),
    ],
)
def test_parse_errors(
    command: str,
    output: str,
    expected_count: int,
    expected_first_file: str | None,
    expected_first_line: int | None,
) -> None:
    errors = pyfltr.parsing.entry.parse_errors(command, output)
    assert len(errors) == expected_count
    if expected_count > 0:
        assert errors[0].file == expected_first_file
        assert errors[0].line == expected_first_line
        assert errors[0].command == command


@pytest.mark.parametrize(
    "raw_severity,expected_severity",
    [
        ("error", "error"),
        ("warning", "warning"),
        ("notice", "info"),
    ],
)
def test_parse_errors_biome_severity(raw_severity: str, expected_severity: str) -> None:
    message_text = "Use a literal key instead."
    output = (
        f"::{raw_severity} title=lint/complexity/useLiteralKeys,file=src/baz.ts,"
        f"line=810,endLine=810,col=49,endColumn=56::{message_text}"
    )
    errors = pyfltr.parsing.entry.parse_errors("biome", output)
    assert len(errors) == 1
    assert errors[0].severity == expected_severity
    assert errors[0].message == message_text


def test_parse_errors_biome_rule_and_url() -> None:
    output = (
        "::error title=lint/suspicious/noDoubleEquals,file=src/foo.ts,"
        "line=1,endLine=1,col=7,endColumn=9::Use === instead of ==\n"
    )
    errors = pyfltr.parsing.entry.parse_errors("biome", output)
    assert len(errors) == 1
    assert errors[0].rule == "lint/suspicious/noDoubleEquals"
    assert errors[0].rule_url == "https://biomejs.dev/linter/rules/no-double-equals/"


def test_parse_errors_biome_assist_rule_and_url() -> None:
    output = (
        "::error title=assist/source/organizeImports,file=src/foo.ts,line=1,endLine=1,col=1,endColumn=2::Organize imports\n"
    )
    errors = pyfltr.parsing.entry.parse_errors("biome", output)
    assert len(errors) == 1
    assert errors[0].rule == "assist/source/organizeImports"
    assert errors[0].rule_url == "https://biomejs.dev/assist/actions/organize-imports/"


@pytest.mark.parametrize("category", ["format", "parse"])
def test_parse_errors_biome_non_rule_category_has_no_url(category: str) -> None:
    output = f"::error title={category},file=package.json,line=1,endLine=1,col=2,endColumn=2::Diagnostic message\n"
    errors = pyfltr.parsing.entry.parse_errors("biome", output)
    assert len(errors) == 1
    assert errors[0].rule == category
    assert errors[0].rule_url is None


def test_parse_errors_biome_without_title_is_not_dropped() -> None:
    output = "::error file=src/foo.ts,line=1,endLine=1,col=7,endColumn=9::message\n"
    errors = pyfltr.parsing.entry.parse_errors("biome", output)
    assert len(errors) == 1
    assert errors[0].rule is None
    assert errors[0].file.endswith("foo.ts")


def test_parse_errors_biome_end_position() -> None:
    output = (
        "::error title=lint/suspicious/noDoubleEquals,file=src/foo.ts,"
        "line=1,endLine=2,col=7,endColumn=9::Use === instead of ==\n"
    )
    errors = pyfltr.parsing.entry.parse_errors("biome", output)
    assert len(errors) == 1
    assert errors[0].line == 1
    assert errors[0].end_line == 2
    assert errors[0].col == 7
    assert errors[0].end_col == 9


def test_parse_errors_biome_end_position_without_title() -> None:
    output = "::error file=src/foo.ts,line=1,endLine=1,col=7,endColumn=9::message\n"
    errors = pyfltr.parsing.entry.parse_errors("biome", output)
    assert len(errors) == 1
    assert errors[0].rule is None
    assert errors[0].end_line == 1
    assert errors[0].end_col == 9


def test_parse_errors_biome_without_end_position() -> None:
    output = "::error title=lint/style/useConst,file=src/bar.ts,line=5,col=3::Use const instead of let\n"
    errors = pyfltr.parsing.entry.parse_errors("biome", output)
    assert len(errors) == 1
    assert errors[0].line == 5
    assert errors[0].col == 3
    assert errors[0].end_line is None
    assert errors[0].end_col is None
    assert errors[0].rule == "lint/style/useConst"


def test_parse_errors_biome_line_does_not_match_parameter_suffix() -> None:
    output = "::error title=lint/style/useConst,file=src/bar.ts,baseline=5,col=3::message\n"
    assert pyfltr.parsing.entry.parse_errors("biome", output) == []


def test_parse_errors_biome_col_does_not_match_parameter_suffix() -> None:
    output = "::error title=lint/style/useConst,file=src/bar.ts,line=5,protocol=3::message\n"
    assert pyfltr.parsing.entry.parse_errors("biome", output) == []


def test_parse_errors_ty_rule_without_url() -> None:
    output = "src/foo.py:10:5: error[invalid-argument-type] Argument is incorrect\n"
    errors = pyfltr.parsing.entry.parse_errors("ty", output)
    assert len(errors) == 1
    assert errors[0].rule == "invalid-argument-type"
    assert errors[0].rule_url is None
    assert errors[0].severity == "error"
    assert errors[0].message == "Argument is incorrect"


def test_parse_errors_ty_warning_severity() -> None:
    output = "src/warn.py:1:13: warning[unused-ignore-comment] Unused `ty: ignore` directive\n"
    errors = pyfltr.parsing.entry.parse_errors("ty", output)
    assert len(errors) == 1
    assert errors[0].rule == "unused-ignore-comment"
    assert errors[0].severity == "warning"
    assert errors[0].message == "Unused `ty: ignore` directive"


def test_parse_errors_ty_info_severity() -> None:
    output = "src/reveal.py:2:17: info[revealed-type] Revealed type: `int`\n"
    errors = pyfltr.parsing.entry.parse_errors("ty", output)
    assert len(errors) == 1
    assert errors[0].rule == "revealed-type"
    assert errors[0].severity == "info"
    assert errors[0].message == "Revealed type: `int`"
    assert errors[0].rule_url is None


def test_parse_errors_ty_message_starting_with_info_keeps_severity() -> None:
    output = "src/a.py:1:1: error[invalid-assignment] info about the assignment\n"
    errors = pyfltr.parsing.entry.parse_errors("ty", output)
    assert len(errors) == 1
    assert errors[0].severity == "error"
    assert errors[0].message == "info about the assignment"


def test_parse_errors_ty_info_like_prefix_is_ignored() -> None:
    output = "src/a.py:5:5: infomercial[foo] not a severity\n"
    errors = pyfltr.parsing.entry.parse_errors("ty", output)
    assert len(errors) == 0


def test_parse_errors_ty_summary_line_is_ignored() -> None:
    output = "Found 4 diagnostics\n"
    errors = pyfltr.parsing.entry.parse_errors("ty", output)
    assert len(errors) == 0


def test_parse_errors_actionlint_rule() -> None:
    output = '.github/workflows/ci.yaml:7:24: property "nonexistent" is not defined in object type {} [expression]\n'
    errors = pyfltr.parsing.entry.parse_errors("actionlint", output)
    assert len(errors) == 1
    assert errors[0].rule == "expression"
    assert errors[0].message == 'property "nonexistent" is not defined in object type {}'
    # 公式ドキュメントの見出しがrule識別子と多対多で対応するためURLは生成しない。
    assert errors[0].rule_url is None


def test_parse_errors_actionlint_rule_with_brackets_in_message() -> None:
    output = ".github/workflows/ci.yaml:16:14: label is unknown. available labels are [a b] [runner-label]\n"
    errors = pyfltr.parsing.entry.parse_errors("actionlint", output)
    assert len(errors) == 1
    assert errors[0].rule == "runner-label"
    assert errors[0].message == "label is unknown. available labels are [a b]"


def test_parse_errors_actionlint_without_rule() -> None:
    output = ".github/workflows/ci.yaml:10:5: something went wrong\n"
    errors = pyfltr.parsing.entry.parse_errors("actionlint", output)
    assert len(errors) == 1
    assert errors[0].rule is None
    assert errors[0].message == "something went wrong"


def test_parse_errors_actionlint_snippet_line_is_ignored() -> None:
    output = '  |\n7 |       - run: echo "${{ matrix.nonexistent }}"\n'
    errors = pyfltr.parsing.entry.parse_errors("actionlint", output)
    assert len(errors) == 0


def test_parse_errors_yamllint_parsable() -> None:
    output = (
        "bad.yaml:1:4: [error] too many spaces after colon (colons)\n"
        "bad.yaml:3:4: [warning] truthy value should be one of [false, true] (truthy)\n"
        "syn.yaml:3:1: [error] syntax error: expected the node content, but found '<stream end>' (syntax)\n"
    )
    errors = pyfltr.parsing.entry.parse_errors("yamllint", output)
    assert [(e.file, e.line, e.col, e.severity, e.rule, e.message) for e in errors] == [
        ("bad.yaml", 1, 4, "error", "colons", "too many spaces after colon"),
        ("bad.yaml", 3, 4, "warning", "truthy", "truthy value should be one of [false, true]"),
        ("syn.yaml", 3, 1, "error", "syntax", "syntax error: expected the node content, but found '<stream end>'"),
    ]


def test_parse_errors_yamllint_parentheses_in_message() -> None:
    output = "a.yaml:2:41: [error] line too long (65 > 40 characters) (line-length)\n"
    errors = pyfltr.parsing.entry.parse_errors("yamllint", output)
    assert len(errors) == 1
    assert errors[0].rule == "line-length"
    assert errors[0].message == "line too long (65 > 40 characters)"


def test_parse_errors_yamllint_standard_format_is_ignored() -> None:
    output = "bad.yaml\n  1:4       error    too many spaces after colon  (colons)\n"
    assert not pyfltr.parsing.entry.parse_errors("yamllint", output)


def test_parse_errors_eslint_json() -> None:
    output = json.dumps(
        [
            {
                "filePath": str(pathlib.Path.cwd() / "src" / "foo.js"),
                "messages": [
                    {
                        "line": 10,
                        "column": 5,
                        "endLine": 11,
                        "endColumn": 6,
                        "message": "'x' is defined but never used.",
                        "ruleId": "no-unused-vars",
                        "severity": 2,
                    },
                    {
                        "line": 20,
                        "column": 1,
                        "message": "Missing semicolon.",
                        "ruleId": "semi",
                        "severity": 2,
                    },
                ],
            },
            {
                "filePath": str(pathlib.Path.cwd() / "src" / "bar.js"),
                "messages": [],
            },
        ]
    )
    errors = pyfltr.parsing.entry.parse_errors("eslint", output)
    assert len(errors) == 2
    assert tests.parsing.conftest.diagnostic_fields(errors[0]) == (
        "src/foo.js",
        10,
        5,
        "eslint",
        "'x' is defined but never used. (no-unused-vars)",
        "no-unused-vars",
        "error",
        "none",
        11,
        6,
    )
    assert errors[1].line == 20


def test_parse_errors_eslint_json_empty_array() -> None:
    errors = pyfltr.parsing.entry.parse_errors("eslint", "[]")
    assert errors == []


def test_parse_errors_eslint_json_empty_string() -> None:
    errors = pyfltr.parsing.entry.parse_errors("eslint", "")
    assert errors == []


def test_parse_errors_eslint_json_invalid() -> None:
    errors = pyfltr.parsing.entry.parse_errors("eslint", "Warning: something\n[not json]")
    assert errors == []


def test_parse_errors_eslint_json_no_rule_id() -> None:
    output = json.dumps(
        [
            {
                "filePath": "/abs/src/foo.js",
                "messages": [
                    {
                        "line": 1,
                        "column": 1,
                        "message": "Parsing error",
                        "ruleId": None,
                        "severity": 2,
                    },
                ],
            }
        ]
    )
    errors = pyfltr.parsing.entry.parse_errors("eslint", output)
    assert len(errors) == 1
    assert errors[0].message == "Parsing error"


def test_parse_typos_jsonl_no_corrections_is_none() -> None:
    output = '{"path":"src/foo.py","line_num":3,"typo":"weirdword","corrections":[],"type":"typo"}\n'
    errors = pyfltr.parsing.entry.parse_errors("typos", output)
    assert len(errors) == 1
    assert errors[0].fix == "none"


@pytest.mark.parametrize(
    ("command", "output", "expected"),
    [
        (
            "pylint",
            json.dumps(
                {
                    "messages": [
                        {
                            "messageId": "C0103",
                            "symbol": "invalid-name",
                            "message": "変数éの名前が不正です",
                            "path": "src/non_ascii.py",
                            "line": 1,
                            "column": 4,
                            "endLine": 1,
                            "endColumn": 6,
                            "type": "convention",
                        }
                    ]
                }
            ),
            (
                "src/non_ascii.py",
                1,
                5,
                "pylint",
                "C0103: 変数éの名前が不正です",
                "invalid-name",
                "warning",
                None,
                1,
                7,
            ),
        ),
        (
            "bandit",
            json.dumps(
                {
                    "results": [
                        {
                            "filename": "src/non_ascii.py",
                            "line_number": 1,
                            "col_offset": 4,
                            "end_col_offset": 6,
                            "line_range": [1],
                            "issue_text": "変数éを検出",
                            "code": "é = 1",
                        }
                    ]
                }
            ),
            ("src/non_ascii.py", 1, 5, "bandit", "変数éを検出", None, None, None, 1, 7),
        ),
    ],
)
def test_parse_non_ascii_byte_offsets_remain_approximate(
    command: str,
    output: str,
    expected: tuple[str, int, int | None, str, str, str | None, str | None, str | None, int | None, int | None],
) -> None:
    errors = pyfltr.parsing.entry.parse_errors(command, output)

    assert len(errors) == 1
    assert tests.parsing.conftest.diagnostic_fields(errors[0]) == expected


@pytest.mark.parametrize(
    ("command", "output"),
    [
        pytest.param(
            "eslint",
            json.dumps(
                [
                    {
                        "filePath": "src/a.js",
                        "messages": [{"line": 1, "column": 2, "endLine": 3, "endColumn": 1, "message": "msg"}],
                    }
                ]
            ),
            id="eslint",
        ),
        pytest.param(
            "ruff-check",
            json.dumps(
                [
                    {
                        "filename": "src/a.py",
                        "location": {"row": 1, "column": 2},
                        "end_location": {"row": 3, "column": 1},
                        "message": "msg",
                    }
                ]
            ),
            id="ruff-check",
        ),
        pytest.param(
            "pylint",
            json.dumps(
                {
                    "messages": [
                        {
                            "path": "src/a.py",
                            "line": 1,
                            "column": 1,
                            "endLine": 3,
                            "endColumn": 0,
                            "message": "msg",
                        }
                    ]
                }
            ),
            id="pylint",
        ),
        pytest.param(
            "pyright",
            json.dumps(
                {
                    "generalDiagnostics": [
                        {
                            "file": "src/a.py",
                            "range": {
                                "start": {"line": 0, "character": 1},
                                "end": {"line": 2, "character": 0},
                            },
                            "message": "msg",
                        }
                    ]
                }
            ),
            id="pyright",
        ),
        pytest.param(
            "shellcheck",
            json.dumps([{"file": "src/a.sh", "line": 1, "column": 2, "endLine": 3, "endColumn": 1, "message": "msg"}]),
            id="shellcheck",
        ),
        pytest.param(
            "textlint",
            json.dumps(
                [
                    {
                        "filePath": "docs/a.md",
                        "messages": [
                            {
                                "line": 1,
                                "column": 2,
                                "loc": {"end": {"line": 3, "column": 1}},
                                "message": "msg",
                            }
                        ],
                    }
                ]
            ),
            id="textlint",
        ),
        pytest.param(
            "semgrep",
            json.dumps(
                {
                    "results": [
                        {
                            "path": "src/a.py",
                            "start": {"line": 1, "col": 2},
                            "end": {"line": 3, "col": 1},
                            "extra": {"message": "msg"},
                        }
                    ]
                }
            ),
            id="semgrep",
        ),
        pytest.param(
            "bandit",
            json.dumps(
                {
                    "results": [
                        {
                            "filename": "src/a.py",
                            "line_number": 1,
                            "col_offset": 1,
                            "line_range": [1, 2, 3],
                            "end_col_offset": 0,
                            "issue_text": "msg",
                        }
                    ]
                }
            ),
            id="bandit",
        ),
        pytest.param(
            "sqlfluff",
            json.dumps(
                [
                    {
                        "filepath": "src/a.sql",
                        "violations": [
                            {
                                "start_line_no": 1,
                                "start_line_pos": 2,
                                "end_line_no": 3,
                                "end_line_pos": 1,
                                "description": "msg",
                            }
                        ],
                    }
                ]
            ),
            id="sqlfluff",
        ),
        pytest.param(
            "biome",
            "::error file=src/a.ts,line=1,endLine=3,col=2,endColumn=1::msg\n",
            id="pattern",
        ),
    ],
)
def test_end_position_paths_normalize_next_line_start(command: str, output: str) -> None:
    errors = pyfltr.parsing.entry.parse_errors(command, output)

    assert len(errors) == 1
    assert errors[0].end_line == 2
    assert errors[0].end_col is None


@pytest.mark.parametrize(
    ("command", "output"),
    [
        pytest.param(
            "eslint",
            json.dumps(
                [
                    {
                        "filePath": "src/a.js",
                        "messages": [{"line": 1, "column": 2, "endLine": 3, "endColumn": 4, "message": "msg"}],
                    }
                ]
            ),
            id="eslint",
        ),
        pytest.param(
            "ruff-check",
            json.dumps(
                [
                    {
                        "filename": "src/a.py",
                        "location": {"row": 1, "column": 2},
                        "end_location": {"row": 3, "column": 4},
                        "message": "msg",
                    }
                ]
            ),
            id="ruff-check",
        ),
        pytest.param(
            "pylint",
            json.dumps(
                {
                    "messages": [
                        {
                            "path": "src/a.py",
                            "line": 1,
                            "column": 1,
                            "endLine": 3,
                            "endColumn": 3,
                            "message": "msg",
                        }
                    ]
                }
            ),
            id="pylint",
        ),
        pytest.param(
            "pyright",
            json.dumps(
                {
                    "generalDiagnostics": [
                        {
                            "file": "src/a.py",
                            "range": {
                                "start": {"line": 0, "character": 1},
                                "end": {"line": 2, "character": 3},
                            },
                            "message": "msg",
                        }
                    ]
                }
            ),
            id="pyright",
        ),
        pytest.param(
            "shellcheck",
            json.dumps([{"file": "src/a.sh", "line": 1, "column": 2, "endLine": 3, "endColumn": 4, "message": "msg"}]),
            id="shellcheck",
        ),
        pytest.param(
            "textlint",
            json.dumps(
                [
                    {
                        "filePath": "docs/a.md",
                        "messages": [
                            {
                                "line": 1,
                                "column": 2,
                                "loc": {"end": {"line": 3, "column": 4}},
                                "message": "msg",
                            }
                        ],
                    }
                ]
            ),
            id="textlint",
        ),
        pytest.param(
            "semgrep",
            json.dumps(
                {
                    "results": [
                        {
                            "path": "src/a.py",
                            "start": {"line": 1, "col": 2},
                            "end": {"line": 3, "col": 4},
                            "extra": {"message": "msg"},
                        }
                    ]
                }
            ),
            id="semgrep",
        ),
        pytest.param(
            "bandit",
            json.dumps(
                {
                    "results": [
                        {
                            "filename": "src/a.py",
                            "line_number": 1,
                            "col_offset": 1,
                            "line_range": [1, 2, 3],
                            "end_col_offset": 3,
                            "issue_text": "msg",
                        }
                    ]
                }
            ),
            id="bandit",
        ),
        pytest.param(
            "sqlfluff",
            json.dumps(
                [
                    {
                        "filepath": "src/a.sql",
                        "violations": [
                            {
                                "start_line_no": 1,
                                "start_line_pos": 2,
                                "end_line_no": 3,
                                "end_line_pos": 4,
                                "description": "msg",
                            }
                        ],
                    }
                ]
            ),
            id="sqlfluff",
        ),
        pytest.param(
            "biome",
            "::error file=src/a.ts,line=1,endLine=3,col=2,endColumn=4::msg\n",
            id="pattern",
        ),
    ],
)
def test_end_position_paths_keep_in_line_end(command: str, output: str) -> None:
    errors = pyfltr.parsing.entry.parse_errors(command, output)

    assert len(errors) == 1
    assert errors[0].end_line == 3
    assert errors[0].end_col == 4


def test_parse_eslint_json_normalizes_real_world_end_position() -> None:
    output = json.dumps(
        [
            {
                "filePath": "src/a.js",
                "messages": [
                    {
                        "line": 12,
                        "column": 4,
                        "endLine": 13,
                        "endColumn": 1,
                        "ruleId": "no-multiple-empty-lines",
                        "severity": 2,
                        "message": "More than 1 blank line not allowed.",
                    }
                ],
            }
        ]
    )

    errors = pyfltr.parsing.entry.parse_errors("eslint", output)

    assert len(errors) == 1
    assert errors[0].line == 12
    assert errors[0].end_line == 12
    assert errors[0].end_col is None


def test_parse_eslint_json_keeps_line_head_end_on_same_line() -> None:
    output = json.dumps(
        [
            {
                "filePath": "src/a.js",
                "messages": [{"line": 1, "column": 1, "endLine": 1, "endColumn": 1, "message": "msg"}],
            }
        ]
    )

    errors = pyfltr.parsing.entry.parse_errors("eslint", output)

    assert len(errors) == 1
    assert errors[0].line == 1
    assert errors[0].end_line == 1
    assert errors[0].end_col == 1


def test_parse_eslint_json_keeps_line_head_end_without_end_line() -> None:
    output = json.dumps(
        [
            {
                "filePath": "src/a.js",
                "messages": [{"line": 1, "column": 2, "endColumn": 1, "message": "msg"}],
            }
        ]
    )

    errors = pyfltr.parsing.entry.parse_errors("eslint", output)

    assert len(errors) == 1
    assert errors[0].end_line is None
    assert errors[0].end_col == 1


def test_parse_eslint_json_keeps_end_line_before_start() -> None:
    output = json.dumps(
        [
            {
                "filePath": "src/a.js",
                "messages": [{"line": 5, "column": 2, "endLine": 3, "endColumn": 1, "message": "msg"}],
            }
        ]
    )

    errors = pyfltr.parsing.entry.parse_errors("eslint", output)

    assert len(errors) == 1
    assert errors[0].line == 5
    assert errors[0].end_line == 3
    assert errors[0].end_col == 1


def test_parse_shellcheck_json() -> None:
    output = json.dumps(
        [
            {
                "file": "src/foo.sh",
                "line": 10,
                "column": 5,
                "endLine": 11,
                "endColumn": 12,
                "level": "warning",
                "code": 2086,
                "message": "Double quote to prevent globbing",
            },
        ]
    )
    errors = pyfltr.parsing.entry.parse_errors("shellcheck", output)
    assert len(errors) == 1
    assert tests.parsing.conftest.diagnostic_fields(errors[0]) == (
        "src/foo.sh",
        10,
        5,
        "shellcheck",
        "Double quote to prevent globbing",
        "SC2086",
        "warning",
        "none",
        11,
        12,
    )


def test_parse_shellcheck_json_fallback() -> None:
    output = "src/foo.sh:10:5: warning: Double quote to prevent globbing [SC2086]"
    errors = pyfltr.parsing.entry.parse_errors("shellcheck", output)

    assert len(errors) == 1
    assert errors[0].file == "src/foo.sh"
    assert errors[0].line == 10
    assert errors[0].col == 5
    assert errors[0].command == "shellcheck"
    assert errors[0].message == "Double quote to prevent globbing [SC2086]"
    assert errors[0].end_line is None
    assert errors[0].end_col is None


@pytest.mark.parametrize(
    ("command", "output"),
    [
        (
            "ruff-check",
            json.dumps(
                [
                    {"filename": "a.py", "location": {"row": 1, "column": 1}},
                    {"filename": "b.py", "location": {"row": 1, "column": 1}, "end_location": None},
                    {
                        "filename": "c.py",
                        "location": {"row": 1, "column": 1},
                        "end_location": {"row": "bad", "column": []},
                    },
                ]
            ),
        ),
        (
            "pylint",
            json.dumps(
                {
                    "messages": [
                        {"path": "a.py", "line": 1, "column": 0},
                        {"path": "b.py", "line": 1, "column": 0, "endLine": None, "endColumn": None},
                        {"path": "c.py", "line": 1, "column": 0, "endLine": "bad", "endColumn": []},
                    ]
                }
            ),
        ),
        (
            "pyright",
            json.dumps(
                {
                    "generalDiagnostics": [
                        {"file": "a.py", "range": {"start": {"line": 0, "character": 0}}},
                        {"file": "b.py", "range": {"start": {"line": 0, "character": 0}, "end": None}},
                        {
                            "file": "c.py",
                            "range": {
                                "start": {"line": 0, "character": 0},
                                "end": {"line": "bad", "character": []},
                            },
                        },
                    ]
                }
            ),
        ),
        (
            "shellcheck",
            json.dumps(
                [
                    {"file": "a.sh", "line": 1, "column": 1},
                    {"file": "b.sh", "line": 1, "column": 1, "endLine": None, "endColumn": None},
                    {"file": "c.sh", "line": 1, "column": 1, "endLine": "bad", "endColumn": []},
                ]
            ),
        ),
        (
            "eslint",
            json.dumps(
                [
                    {
                        "filePath": "a.js",
                        "messages": [
                            {"line": 1, "column": 1},
                            {"line": 2, "column": 1, "endLine": None, "endColumn": None},
                            {"line": 3, "column": 1, "endLine": "bad", "endColumn": []},
                        ],
                    }
                ]
            ),
        ),
        (
            "semgrep",
            json.dumps(
                {
                    "results": [
                        {"path": "a.py", "start": {"line": 1, "col": 1}},
                        {"path": "b.py", "start": {"line": 1, "col": 1}, "end": None},
                        {"path": "c.py", "start": {"line": 1, "col": 1}, "end": {"line": "bad", "col": []}},
                    ]
                }
            ),
        ),
        (
            "bandit",
            json.dumps(
                {
                    "results": [
                        {"filename": "a.py", "line_number": 1, "col_offset": 0},
                        {
                            "filename": "b.py",
                            "line_number": 1,
                            "col_offset": 0,
                            "line_range": None,
                            "end_col_offset": None,
                        },
                        {
                            "filename": "c.py",
                            "line_number": 1,
                            "col_offset": 0,
                            "line_range": ["bad"],
                            "end_col_offset": "bad",
                        },
                    ]
                }
            ),
        ),
        (
            "sqlfluff",
            json.dumps(
                [
                    {
                        "filepath": "a.sql",
                        "violations": [
                            {"start_line_no": 1, "start_line_pos": 1},
                            {
                                "start_line_no": 2,
                                "start_line_pos": 1,
                                "end_line_no": None,
                                "end_line_pos": None,
                            },
                            {
                                "start_line_no": 3,
                                "start_line_pos": 1,
                                "end_line_no": "bad",
                                "end_line_pos": [],
                            },
                        ],
                    }
                ]
            ),
        ),
    ],
)
def test_parse_json_end_position_missing_null_and_invalid(command: str, output: str) -> None:
    errors = pyfltr.parsing.entry.parse_errors(command, output)

    assert len(errors) == 3
    assert all(error.end_line is None and error.end_col is None for error in errors)


def test_parse_typos_jsonl() -> None:
    output = (
        '{"path":"src/foo.py","line_num":3,"byte_offset":15,"typo":"teh","corrections":["the"],"type":"typo"}\n'
        '{"path":"src/bar.py","line_num":7,"byte_offset":20,"typo":"hte","corrections":["the","he"],"type":"typo"}\n'
    )
    errors = pyfltr.parsing.entry.parse_errors("typos", output)
    assert len(errors) == 2
    assert errors[0].file == "src/foo.py"
    assert errors[0].line == 3
    assert errors[0].message == "`teh` -> `the`"
    assert errors[0].severity == "warning"
    assert errors[0].fix == "safe"
    assert errors[1].message == "`hte` -> `the, he`"


def test_parse_typos_jsonl_fallback() -> None:
    output = "src/foo.py:3:15: `teh` -> `the`"
    errors = pyfltr.parsing.entry.parse_errors("typos", output)
    assert len(errors) == 1
    assert errors[0].line == 3


def _vitest_assertion(
    *,
    status: str,
    full_name: str,
    failure_messages: list[str] | None = None,
    location: dict[str, int] | None = None,
) -> dict:
    result: dict = {"status": status, "fullName": full_name}
    if failure_messages is not None:
        result["failureMessages"] = failure_messages
    if location is not None:
        result["location"] = location
    return result


def _vitest_output(test_results: list[dict]) -> str:
    return json.dumps({"testResults": test_results})


_VITEST_SINGLE_FAILURE = _vitest_output(
    [
        {
            "name": "/abs/proj/tests/foo.test.ts",
            "assertionResults": [
                _vitest_assertion(
                    status="failed",
                    full_name="adds correctly",
                    failure_messages=["AssertionError: expected 3 to equal 4"],
                    location={"line": 7, "column": 5},
                )
            ],
        }
    ]
)


_VITEST_MULTI_FILE_FAILURE = _vitest_output(
    [
        {
            "name": "/abs/proj/tests/foo.test.ts",
            "assertionResults": [
                _vitest_assertion(
                    status="failed",
                    full_name="adds correctly",
                    failure_messages=["AssertionError: expected 3 to equal 4"],
                    location={"line": 7, "column": 5},
                ),
                _vitest_assertion(
                    status="passed",
                    full_name="subtracts correctly",
                    location={"line": 12, "column": 5},
                ),
            ],
        },
        {
            "name": "/abs/proj/tests/bar.test.ts",
            "assertionResults": [
                _vitest_assertion(
                    status="failed",
                    full_name="divides correctly",
                    failure_messages=["TypeError: divisor is zero"],
                    location={"line": 20, "column": 1},
                )
            ],
        },
    ]
)


_VITEST_LOCATION_MISSING = _vitest_output(
    [
        {
            "name": "/abs/proj/tests/foo.test.ts",
            "assertionResults": [
                _vitest_assertion(
                    status="failed",
                    full_name="no location",
                    failure_messages=["AssertionError: boom"],
                )
            ],
        }
    ]
)


_VITEST_NESTED_FULLNAME = _vitest_output(
    [
        {
            "name": "/abs/proj/tests/foo.test.ts",
            "assertionResults": [
                _vitest_assertion(
                    status="failed",
                    full_name="Calculator > addition > positive numbers",
                    failure_messages=["AssertionError: expected 3 to equal 4"],
                    location={"line": 9, "column": 3},
                )
            ],
        }
    ]
)


_VITEST_EMPTY_FAILURE_MESSAGES = _vitest_output(
    [
        {
            "name": "/abs/proj/tests/foo.test.ts",
            "assertionResults": [
                _vitest_assertion(
                    status="failed",
                    full_name="no failure messages",
                    failure_messages=[],
                    location={"line": 1, "column": 1},
                )
            ],
        }
    ]
)


_VITEST_ALL_PASSED = _vitest_output(
    [
        {
            "name": "/abs/proj/tests/foo.test.ts",
            "assertionResults": [
                _vitest_assertion(
                    status="passed",
                    full_name="adds correctly",
                    location={"line": 7, "column": 5},
                )
            ],
        }
    ]
)


@pytest.mark.parametrize(
    ("case_id", "output", "expected"),
    [
        (
            "single_failure",
            _VITEST_SINGLE_FAILURE,
            [
                {
                    "line": 7,
                    "col": 5,
                    "message_prefix": "adds correctly: ",
                    "message_contains": "expected 3 to equal 4",
                }
            ],
        ),
        (
            "multi_file_failure",
            _VITEST_MULTI_FILE_FAILURE,
            [
                {
                    "line": 7,
                    "col": 5,
                    "message_prefix": "adds correctly: ",
                    "message_contains": "expected 3 to equal 4",
                },
                {
                    "line": 20,
                    "col": 1,
                    "message_prefix": "divides correctly: ",
                    "message_contains": "divisor is zero",
                },
            ],
        ),
        (
            "location_missing_fallback",
            _VITEST_LOCATION_MISSING,
            [
                {
                    "line": 1,
                    "col": None,
                    "message_prefix": "no location: ",
                    "message_contains": "boom",
                }
            ],
        ),
        (
            "nested_describe_fullname",
            _VITEST_NESTED_FULLNAME,
            [
                {
                    "line": 9,
                    "col": 3,
                    "message_prefix": "Calculator > addition > positive numbers: ",
                    "message_contains": "expected 3 to equal 4",
                }
            ],
        ),
        (
            "empty_failure_messages",
            _VITEST_EMPTY_FAILURE_MESSAGES,
            [
                {
                    "line": 1,
                    "col": 1,
                    "message_prefix": "no failure messages: ",
                    "message_contains": "",
                }
            ],
        ),
        ("all_passed", _VITEST_ALL_PASSED, []),
        ("invalid_json", "not json", []),
    ],
)
def test_parse_vitest_json(case_id: str, output: str, expected: list[dict]) -> None:
    del case_id
    errors = pyfltr.parsing.entry.parse_errors("vitest", output)
    assert len(errors) == len(expected)
    for actual, want in zip(errors, expected, strict=True):
        assert actual.command == "vitest"
        assert actual.line == want["line"]
        assert actual.col == want["col"]
        assert actual.message.startswith(want["message_prefix"])
        assert want["message_contains"] in actual.message


@pytest.mark.parametrize(
    ("name_form", "base_parts", "expected_prefix"),
    [
        ("absolute", (), "tests"),
        ("relative", (), "tests"),
        ("absolute", ("pkg",), "pkg/tests"),
        ("relative", ("pkg",), "pkg/tests"),
    ],
)
def test_parse_vitest_json_suite_failure(
    tmp_path: pathlib.Path, name_form: str, base_parts: tuple[str, ...], expected_prefix: str
) -> None:
    path_base = tmp_path.joinpath(*base_parts)

    def name(file_name: str) -> str:
        relative = f"tests/{file_name}"
        return str(path_base / relative) if name_form == "absolute" else relative

    output = _vitest_output(
        [
            {
                "name": name("load.test.ts"),
                "status": "failed",
                "message": "Error: sample failure\n    at /abs/proj/tests/load.test.ts:1:7",
                "assertionResults": [],
            },
            {
                "name": name("mixed.test.ts"),
                "status": "failed",
                "message": "",
                "assertionResults": [
                    _vitest_assertion(
                        status="failed",
                        full_name="adds correctly",
                        failure_messages=["AssertionError: expected 3 to equal 4"],
                        location={"line": 7, "column": 5},
                    )
                ],
            },
            {"name": name("empty.test.ts"), "status": "failed", "message": "", "assertionResults": []},
            {
                "name": name("hook.test.ts"),
                "status": "failed",
                "message": "hook failure",
                "assertionResults": [_vitest_assertion(status="passed", full_name="passes", location={"line": 3, "column": 1})],
            },
            {"name": name("bad.test.ts"), "status": "failed", "message": "Error: x", "assertionResults": None},
            {"name": name("ok.test.ts"), "status": "passed", "message": "", "assertionResults": []},
            {"name": name("todo.test.ts"), "status": "pending", "message": "", "assertionResults": []},
            {"name": name("skip.test.ts"), "status": "skipped", "message": "", "assertionResults": []},
            {"name": "", "status": "failed", "message": "Error: startup failure", "assertionResults": []},
        ]
    )
    errors = pyfltr.parsing.entry.parse_errors("vitest", output, path_base=path_base, start_cwd=tmp_path)
    assert [(error.file, error.line, error.col, error.message) for error in errors] == [
        (f"{expected_prefix}/load.test.ts", 1, None, "Error: sample failure"),
        (f"{expected_prefix}/mixed.test.ts", 7, 5, "adds correctly: AssertionError: expected 3 to equal 4"),
        (f"{expected_prefix}/empty.test.ts", 1, None, ""),
        (f"{expected_prefix}/hook.test.ts", 1, None, "hook failure"),
        (f"{expected_prefix}/bad.test.ts", 1, None, "Error: x"),
    ]


def test_parse_glab_ci_lint_valid() -> None:
    output = "Validating...\n✓ CI/CD YAML is valid!\n"
    assert pyfltr.parsing.entry.parse_errors("glab-ci-lint", output) == []


def test_parse_glab_ci_lint_invalid_multi() -> None:
    output = (
        "Validating...\n"
        ".gitlab-ci.yml is invalid\n"
        "\n"
        "- jobs:test config contains unknown keys: foo\n"
        "- root config contains unknown keys: bar\n"
    )
    errors = pyfltr.parsing.entry.parse_errors("glab-ci-lint", output)
    assert len(errors) == 2
    assert all(e.command == "glab-ci-lint" for e in errors)
    assert all(e.file == ".gitlab-ci.yml" for e in errors)
    assert all(e.line == 1 for e in errors)
    assert all(e.col is None for e in errors)
    assert errors[0].message == "jobs:test config contains unknown keys: foo"
    assert errors[1].message == "root config contains unknown keys: bar"


def test_parse_glab_ci_lint_invalid_numbered() -> None:
    output = ".gitlab-ci.yml is invalid\n1. unknown key foo\n2. unknown key bar\n"
    errors = pyfltr.parsing.entry.parse_errors("glab-ci-lint", output)
    assert [e.message for e in errors] == ["unknown key foo", "unknown key bar"]


def test_parse_designmd_json() -> None:
    output = json.dumps(
        {
            "findings": [
                {
                    "severity": "warning",
                    "path": "components.button-primary",
                    "message": "contrast ratio 15.42:1",
                },
                {
                    "severity": "error",
                    "path": "tokens.color.primary",
                    "message": "missing definition",
                },
            ],
            "summary": {"errors": 1, "warnings": 1, "info": 0},
        }
    )
    errors = pyfltr.parsing.entry.parse_errors("designmd", output)
    assert len(errors) == 2
    assert all(e.file == "DESIGN.md" for e in errors)
    assert all(e.command == "designmd" for e in errors)
    assert errors[0].severity == "warning"
    assert errors[0].message.startswith("components.button-primary: ")
    assert errors[1].severity == "error"


def test_parse_designmd_json_empty() -> None:
    assert pyfltr.parsing.entry.parse_errors("designmd", json.dumps({"findings": []})) == []
    assert pyfltr.parsing.entry.parse_errors("designmd", "not json") == []
    assert pyfltr.parsing.entry.parse_errors("designmd", "") == []


def test_parse_lychee_json() -> None:
    output = json.dumps(
        {
            "total": 5,
            "successful": 3,
            "errors": 2,
            "error_map": {
                "docs/index.md": [
                    {
                        "url": "https://example.com/dead",
                        "status": {"text": "404 Not Found", "code": 404},
                    },
                    {
                        "url": "https://example.com/timeout",
                        "status": {"text": "Network error", "code": None},
                    },
                ],
            },
        }
    )
    errors = pyfltr.parsing.entry.parse_errors("lychee", output)
    assert len(errors) == 2
    assert all(e.command == "lychee" for e in errors)
    assert all(e.file == "docs/index.md" for e in errors)
    assert all(e.line == 1 for e in errors)
    assert all(e.severity == "error" for e in errors)
    assert "https://example.com/dead" in errors[0].message
    assert "404 Not Found" in errors[0].message


def test_parse_lychee_json_empty_error_map() -> None:
    output = json.dumps({"total": 5, "successful": 5, "errors": 0, "error_map": {}})
    assert pyfltr.parsing.entry.parse_errors("lychee", output) == []


def test_parse_lychee_json_invalid() -> None:
    assert pyfltr.parsing.entry.parse_errors("lychee", "not json") == []
    assert pyfltr.parsing.entry.parse_errors("lychee", "") == []


def test_parse_colloquial_check_without_replacement() -> None:
    output = "docs/index.md:3:5: [ちょっと] -> (文意を保ったまま書き言葉の表現へ言い換える) 本文にちょっと該当する"
    errors = pyfltr.parsing.entry.parse_errors("colloquial-check", output)
    assert len(errors) == 1
    assert errors[0].file == "docs/index.md"
    assert errors[0].line == 3
    assert errors[0].col == 5
    assert errors[0].message == "[ちょっと] -> (文意を保ったまま書き言葉の表現へ言い換える) 本文にちょっと該当する"


def test_parse_colloquial_check_ignores_skip_notice() -> None:
    output = (
        "C:/work/bad.md: 読み込めないため口語表現の検査をスキップしました:"
        " 'utf-8' codec can't decode byte 0xff in position 0: invalid start byte。"
        "UTF-8で保存されていることと読み取り権限を確認してください\n"
        "docs/a.md:3:1: [唐突感] -> [論理の飛躍] 唐突感が否めない。\n"
    )
    errors = pyfltr.parsing.entry.parse_errors("colloquial-check", output)
    assert [(e.file, e.line, e.col) for e in errors] == [("docs/a.md", 3, 1)]


def test_parse_colloquial_check_with_replacement() -> None:
    output = "docs/index.md:10:1: [唐突感] -> [論理の飛躍] 唐突感が否めない"
    errors = pyfltr.parsing.entry.parse_errors("colloquial-check", output)
    assert len(errors) == 1
    assert errors[0].line == 10
    assert errors[0].col == 1
    assert errors[0].message == "[唐突感] -> [論理の飛躍] 唐突感が否めない"


def test_parse_colloquial_check_multiple_lines() -> None:
    output = "docs/a.md:1:1: [ちょっと] 該当箇所1\ndocs/b.md:2:3: [ぶっちゃけ] 該当箇所2\n"
    errors = pyfltr.parsing.entry.parse_errors("colloquial-check", output)
    assert len(errors) == 2
    assert errors[0].file == "docs/a.md"
    assert errors[1].file == "docs/b.md"


def test_parse_semgrep_json() -> None:
    output = json.dumps(
        {
            "results": [
                {
                    "check_id": "rules.python.security.sql-injection",
                    "path": "src/foo.py",
                    "start": {"line": 18, "col": 9, "offset": 300},
                    "end": {"line": 19, "col": 82, "offset": 373},
                    "extra": {
                        "severity": "ERROR",
                        "message": "Using variable interpolation could allow SQL injection",
                    },
                },
                {
                    "check_id": "rules.python.style.use-fstring",
                    "path": "src/bar.py",
                    "start": {"line": 3, "col": 5},
                    "end": {"line": 3, "col": 20},
                    "extra": {"severity": "WARNING", "message": "Use f-string"},
                },
            ],
            "errors": [],
        }
    )
    errors = pyfltr.parsing.entry.parse_errors("semgrep", output)
    assert len(errors) == 2
    assert tests.parsing.conftest.diagnostic_fields(errors[0]) == (
        "src/foo.py",
        18,
        9,
        "semgrep",
        "Using variable interpolation could allow SQL injection",
        "rules.python.security.sql-injection",
        "error",
        None,
        19,
        82,
    )
    assert errors[1].severity == "warning"


def test_parse_semgrep_json_empty() -> None:
    assert pyfltr.parsing.entry.parse_errors("semgrep", json.dumps({"results": [], "errors": []})) == []
    assert pyfltr.parsing.entry.parse_errors("semgrep", "not json") == []


def _pinact_sarif_result(uri: str, line: int, text: str) -> dict:
    return {
        "ruleId": "parse-error",
        "level": "error",
        "message": {"text": text},
        "locations": [{"physicalLocation": {"artifactLocation": {"uri": uri}, "region": {"startLine": line}}}],
    }


def test_parse_pinact_sarif() -> None:
    sarif = {
        "version": "2.1.0",
        "runs": [
            {
                "tool": {"driver": {"name": "pinact", "rules": [{"id": "parse-error"}]}},
                "results": [
                    _pinact_sarif_result(".github/workflows/ci.yaml", 7, "failed to handle a line: action can't be pinned"),
                    _pinact_sarif_result(
                        ".github/workflows/ci.yaml",
                        9,
                        "failed to handle a line: SHA-pinned action requires a version comment for verifiability",
                    ),
                ],
            }
        ],
    }
    output = (
        "ERROR failed to handle a line: action can't be pinned\n"
        ".github/workflows/ci.yaml:7\n"
        "      - uses: actions/checkout@v4  # [note] {x} ${{ matrix.os }}\n" + json.dumps(sarif, indent=2)
    )
    errors = pyfltr.parsing.entry.parse_errors("pinact", output)
    assert [(e.file, e.line, e.rule, e.severity, e.message) for e in errors] == [
        (".github/workflows/ci.yaml", 7, "parse-error", "error", "failed to handle a line: action can't be pinned"),
        (
            ".github/workflows/ci.yaml",
            9,
            "parse-error",
            "error",
            "failed to handle a line: SHA-pinned action requires a version comment for verifiability",
        ),
    ]
    assert all(e.command == "pinact" for e in errors)


def test_parse_pinact_sarif_empty() -> None:
    empty = json.dumps({"version": "2.1.0", "runs": [{"tool": {"driver": {"name": "pinact"}}, "results": []}]})
    assert pyfltr.parsing.entry.parse_errors("pinact", empty) == []
    assert pyfltr.parsing.entry.parse_errors("pinact", "not json") == []


def test_parse_semgrep_json_with_progress_output() -> None:
    payload = json.dumps(
        {
            "results": [
                {
                    "check_id": "rules.example",
                    "path": "src/foo.py",
                    "start": {"line": 1, "col": 1},
                    "end": {"line": 2, "col": 3},
                    "extra": {"severity": "ERROR", "message": "Example finding"},
                }
            ]
        }
    )
    output = f"Scanning 1 file.\n{payload}\nRan 1 rule on 1 file: 1 finding.\n"

    errors = pyfltr.parsing.entry.parse_errors("semgrep", output)

    assert len(errors) == 1
    assert errors[0].rule == "rules.example"
    assert errors[0].end_line == 2
    assert errors[0].end_col == 3


def test_parse_bandit_json() -> None:
    output = json.dumps(
        {
            "results": [
                {
                    "filename": "src/foo.py",
                    "line_number": 12,
                    "col_offset": 0,
                    "end_col_offset": 4,
                    "line_range": [12],
                    "test_id": "B602",
                    "test_name": "subprocess_popen_with_shell_equals_true",
                    "issue_severity": "HIGH",
                    "issue_text": "subprocess call with shell=True identified.",
                    "more_info": "https://bandit.readthedocs.io/.../b602.html",
                },
                {
                    "filename": "src/bar.py",
                    "line_number": 3,
                    "col_offset": 4,
                    "end_col_offset": 12,
                    "line_range": [3, 4],
                    "test_id": "B105",
                    "test_name": "hardcoded_password_string",
                    "issue_severity": "MEDIUM",
                    "issue_text": "Possible hardcoded password.",
                    "more_info": "https://bandit.readthedocs.io/.../b105.html",
                },
                {
                    "filename": "src/baz.py",
                    "line_number": 7,
                    "col_offset": 8,
                    "end_col_offset": 14,
                    "line_range": [7],
                    "test_id": "B101",
                    "test_name": "assert_used",
                    "issue_severity": "LOW",
                    "issue_text": "Use of assert detected.",
                },
            ],
            "errors": [],
        }
    )
    errors = pyfltr.parsing.entry.parse_errors("bandit", output)
    assert len(errors) == 3
    assert tests.parsing.conftest.diagnostic_fields(errors[0]) == (
        "src/foo.py",
        12,
        1,
        "bandit",
        "subprocess call with shell=True identified. (see https://bandit.readthedocs.io/.../b602.html)",
        "B602",
        "error",
        None,
        12,
        5,
    )
    assert errors[1].severity == "warning"
    assert errors[1].rule == "B105"
    assert errors[1].col == 5
    assert errors[1].end_line == 4
    assert errors[1].end_col == 13
    assert errors[2].severity == "info"
    assert errors[2].rule == "B101"
    assert "(see " not in errors[2].message


def test_parse_bandit_json_line_range_shapes_and_negative_columns() -> None:
    output = json.dumps(
        {
            "results": [
                {
                    "filename": "src/empty.py",
                    "line_number": 1,
                    "line_range": [],
                    "col_offset": -1,
                    "end_col_offset": -1,
                },
                {
                    "filename": "src/single.py",
                    "line_number": 2,
                    "line_range": [2],
                    "col_offset": 0,
                    "end_col_offset": 3,
                },
                {
                    "filename": "src/multiple.py",
                    "line_number": 3,
                    "line_range": [3, 4, 5],
                    "col_offset": 2,
                    "end_col_offset": 8,
                },
            ]
        }
    )

    errors = pyfltr.parsing.entry.parse_errors("bandit", output)

    assert [(error.col, error.end_line, error.end_col) for error in errors] == [
        (None, None, None),
        (1, 2, 4),
        (3, 5, 9),
    ]


def test_parse_bandit_json_empty() -> None:
    assert pyfltr.parsing.entry.parse_errors("bandit", json.dumps({"results": [], "errors": []})) == []
    assert pyfltr.parsing.entry.parse_errors("bandit", "not json") == []


def test_parse_sqlfluff_json() -> None:
    output = json.dumps(
        [
            {
                "filepath": "src/foo.sql",
                "violations": [
                    {
                        "start_line_no": 10,
                        "start_line_pos": 5,
                        "end_line_no": 11,
                        "end_line_pos": 12,
                        "code": "L001",
                        "name": "layout.trailing_whitespace",
                        "description": "Unnecessary trailing whitespace.",
                        "warning": False,
                    },
                    {
                        "start_line_no": 12,
                        "start_line_pos": 1,
                        "code": "L010",
                        "name": "capitalisation.keywords",
                        "description": "Keywords must be consistently upper case.",
                        "warning": True,
                    },
                ],
            },
            {
                "filepath": "src/bar.sql",
                "violations": [],
            },
        ]
    )
    errors = pyfltr.parsing.entry.parse_errors("sqlfluff", output)
    assert len(errors) == 2
    assert tests.parsing.conftest.diagnostic_fields(errors[0]) == (
        "src/foo.sql",
        10,
        5,
        "sqlfluff",
        "Unnecessary trailing whitespace.",
        "L001",
        "error",
        None,
        11,
        12,
    )
    assert errors[1].severity == "warning"


def test_parse_sqlfluff_json_empty() -> None:
    assert pyfltr.parsing.entry.parse_errors("sqlfluff", json.dumps([])) == []
    assert pyfltr.parsing.entry.parse_errors("sqlfluff", "not json") == []
    assert pyfltr.parsing.entry.parse_errors("sqlfluff", "") == []


def test_parse_errors_mypy_extracts_rule() -> None:
    output = 'src/foo.py:10: error: Name "x" is not defined  [name-defined]'
    errors = pyfltr.parsing.entry.parse_errors("mypy", output)
    assert len(errors) == 1
    assert errors[0].rule == "name-defined"
    assert errors[0].rule_url == "https://mypy.readthedocs.io/en/stable/_refs.html#code-name-defined"
    assert errors[0].message == 'Name "x" is not defined'


def test_parse_errors_mypy_without_rule() -> None:
    output = "src/foo.py:10: error: Something went wrong"
    errors = pyfltr.parsing.entry.parse_errors("mypy", output)
    assert len(errors) == 1
    assert errors[0].rule is None
    assert errors[0].rule_url is None


def test_parse_errors_markdownlint_extracts_rule() -> None:
    output = "docs/index.md:3 MD001/heading-increment Heading levels should only increment by one level at a time"
    errors = pyfltr.parsing.entry.parse_errors("markdownlint", output)
    assert len(errors) == 1
    assert errors[0].rule == "MD001"
    assert errors[0].rule_url == "https://github.com/DavidAnson/markdownlint/blob/main/doc/MD001.md"


def test_parse_errors_markdownlint_with_column() -> None:
    output = 'docs/index.md:3:32 MD059/descriptive-link-text Link text should be descriptive [Context: "[here]"]'
    errors = pyfltr.parsing.entry.parse_errors("markdownlint", output)
    assert len(errors) == 1
    assert errors[0].file == "docs/index.md"
    assert errors[0].line == 3
    assert errors[0].col == 32
    assert errors[0].rule == "MD059"


def test_parse_errors_markdownlint_with_column_on_windows_path() -> None:
    output = "C:/proj/docs/index.md:5:45 MD009/no-trailing-spaces Trailing spaces [Expected: 0; Actual: 1]"
    errors = pyfltr.parsing.entry.parse_errors("markdownlint", output)
    assert len(errors) == 1
    assert errors[0].file.endswith("docs/index.md")
    assert errors[0].line == 5
    assert errors[0].col == 45


def test_parse_errors_markdownlint_with_column_and_severity() -> None:
    output = "docs/index.md:5:1 error MD009/no-trailing-spaces Trailing spaces [Expected: 0; Actual: 1]"
    errors = pyfltr.parsing.entry.parse_errors("markdownlint", output)
    assert len(errors) == 1
    assert errors[0].file == "docs/index.md"
    assert errors[0].line == 5
    assert errors[0].col == 1
    assert errors[0].rule == "MD009"


def test_parse_errors_shellcheck_rule_url() -> None:
    output = json.dumps(
        [
            {
                "file": "src/foo.sh",
                "line": 10,
                "column": 5,
                "level": "warning",
                "code": 2086,
                "message": "Double quote to prevent globbing",
            },
        ]
    )
    errors = pyfltr.parsing.entry.parse_errors("shellcheck", output)
    assert errors[0].rule_url == "https://www.shellcheck.net/wiki/SC2086"


def test_parse_errors_eslint_rule_url() -> None:
    output = json.dumps(
        [
            {
                "filePath": "/abs/src/foo.js",
                "messages": [
                    {
                        "line": 1,
                        "column": 1,
                        "message": "x",
                        "ruleId": "no-unused-vars",
                        "severity": 2,
                    },
                    {
                        "line": 2,
                        "column": 1,
                        "message": "y",
                        "ruleId": "@typescript-eslint/no-explicit-any",
                        "severity": 2,
                    },
                ],
            }
        ]
    )
    errors = pyfltr.parsing.entry.parse_errors("eslint", output)
    assert len(errors) == 2
    assert errors[0].rule_url == "https://eslint.org/docs/latest/rules/no-unused-vars"
    assert errors[1].rule_url is None


def test_parse_errors_shellcheck_severity_normalized() -> None:
    output = json.dumps(
        [
            {
                "file": "src/foo.sh",
                "line": 10,
                "column": 5,
                "level": "style",
                "code": 2086,
                "message": "Suggestion",
            },
        ]
    )
    errors = pyfltr.parsing.entry.parse_errors("shellcheck", output)
    assert errors[0].severity == "info"


@pytest.mark.parametrize("file", [None, False, 0, "", "None", "path.sql"])
def test_sqlfluff_preserves_falsey_file_name(file: object) -> None:
    output = json.dumps([{"filepath": file, "violations": [{"start_line_no": 1, "description": "failure"}]}])
    errors = pyfltr.parsing.entry.parse_errors("sqlfluff", output)
    assert [(error.file, error.message) for error in errors] == [(str(file or ""), "failure")]
