"""error_parserのテストコード。"""

# pylint: disable=too-many-lines

import json
import pathlib

import pytest

import pyfltr.command.error_parser


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
        # ruff-check
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
        # markdownlint-cli2
        (
            "markdownlint",
            "docs/index.md:3 MD001/heading-increment Heading levels should only increment by one level at a time",
            1,
            "docs/index.md",
            3,
        ),
        # textlint --format compact
        (
            "textlint",
            "docs/index.md: line 5, col 1, Error - sentence error (ja-technical-writing/ja-no-mixed-period)",
            1,
            "docs/index.md",
            5,
        ),
        # ty check --output-format concise (error)
        (
            "ty",
            "src/foo.py:10:5: error[invalid-argument-type] Argument is incorrect",
            1,
            "src/foo.py",
            10,
        ),
        # ty check --output-format concise (warning)
        (
            "ty",
            "src/foo.py:3:1: warning[unused-variable] Variable `x` is unused",
            1,
            "src/foo.py",
            3,
        ),
        # pytest
        (
            "pytest",
            "FAILED tests/foo_test.py::test_bar - AssertionError: xxx",
            1,
            "tests/foo_test.py",
            0,  # pytestはline情報なし
        ),
        # biome --reporter=github（lineとcolの間にendLineが介在する）
        (
            "biome",
            "::error title=lint/suspicious/noDoubleEquals,file=src/foo.ts,"
            "line=1,endLine=1,col=7,endColumn=9::Use === instead of ==",
            1,
            "src/foo.ts",
            1,
        ),
        # biome --reporter=github (warning)
        (
            "biome",
            "::warning title=lint/style/useConst,file=src/bar.ts,line=5,endLine=5,col=3,endColumn=6::Use const instead of let",
            1,
            "src/bar.ts",
            5,
        ),
        # biome --reporter=github (notice = info)。unsafe fix提案はnoticeとして出力される
        (
            "biome",
            "::notice title=lint/complexity/useLiteralKeys,file=src/baz.ts,"
            "line=810,endLine=810,col=49,endColumn=56::Use a literal key instead.",
            1,
            "src/baz.ts",
            810,
        ),
        # biome --reporter=github (未知severity)。`error|warning|notice`以外はマッチしない
        (
            "biome",
            "::unknown title=lint/foo/bar,file=src/qux.ts,line=1,endLine=1,col=1,endColumn=2::msg",
            0,
            None,
            None,
        ),
        # パースできないコマンド
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
    """ビルトインパーサーのテスト。"""
    errors = pyfltr.command.error_parser.parse_errors(command, output)
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
    """biome `--reporter=github`のseverity（error/warning/notice）を3値モデルへ正規化する。

    `::notice`はbiomeがunsafe fix適用可能な診断に付与するseverityで、pyfltr側ではinfoとして公開する。
    あわせてmessage本文（unsafe fix提案文を含む）が欠落せず保持されることを全ケースで確認する。
    """
    message_text = "Use a literal key instead."
    output = (
        f"::{raw_severity} title=lint/complexity/useLiteralKeys,file=src/baz.ts,"
        f"line=810,endLine=810,col=49,endColumn=56::{message_text}"
    )
    errors = pyfltr.command.error_parser.parse_errors("biome", output)
    assert len(errors) == 1
    assert errors[0].severity == expected_severity
    assert errors[0].message == message_text


def test_parse_errors_eslint_json() -> None:
    """ESLint --format json出力のパース。"""
    output = json.dumps(
        [
            {
                "filePath": str(pathlib.Path.cwd() / "src" / "foo.js"),
                "messages": [
                    {
                        "line": 10,
                        "column": 5,
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
    errors = pyfltr.command.error_parser.parse_errors("eslint", output)
    assert len(errors) == 2
    assert errors[0].file == "src/foo.js"  # cwd配下は相対パスに正規化される
    assert errors[0].line == 10
    assert errors[0].col == 5
    assert "no-unused-vars" in errors[0].message
    assert errors[0].command == "eslint"
    assert errors[1].line == 20


def test_parse_errors_eslint_json_empty_array() -> None:
    """空配列 `[]` は空リストを返す。"""
    errors = pyfltr.command.error_parser.parse_errors("eslint", "[]")
    assert errors == []


def test_parse_errors_eslint_json_empty_string() -> None:
    """空文字列は空リストを返す (例外なし)。"""
    errors = pyfltr.command.error_parser.parse_errors("eslint", "")
    assert errors == []


def test_parse_errors_eslint_json_invalid() -> None:
    """不正なJSON（stderr混入等）は空リストを返す。"""
    errors = pyfltr.command.error_parser.parse_errors("eslint", "Warning: something\n[not json]")
    assert errors == []


def test_parse_errors_eslint_json_no_rule_id() -> None:
    """ruleIdがnullの場合でもmessageのみ格納する。"""
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
    errors = pyfltr.command.error_parser.parse_errors("eslint", output)
    assert len(errors) == 1
    assert errors[0].message == "Parsing error"


def test_parse_errors_custom_pattern() -> None:
    """カスタムerror-patternのテスト。"""
    pattern = r"(?P<file>[^:]+):(?P<line>\d+):(?P<col>\d+):\s*(?P<message>.+)"
    output = "src/foo.py:10:5: some error\nsrc/bar.py:20:3: another error"
    errors = pyfltr.command.error_parser.parse_errors("custom-tool", output, error_pattern=pattern)
    assert len(errors) == 2
    assert errors[0].file == "src/foo.py"
    assert errors[0].line == 10
    assert errors[0].col == 5
    assert errors[0].message == "some error"
    assert errors[1].file == "src/bar.py"


def test_sort_errors() -> None:
    """エラーソートのテスト。"""
    command_names = ["ruff-check", "mypy", "pylint"]
    errors = [
        pyfltr.command.error_parser.ErrorLocation(file="src/bar.py", line=10, col=None, command="mypy", message="err1"),
        pyfltr.command.error_parser.ErrorLocation(file="src/bar.py", line=10, col=None, command="ruff-check", message="err2"),
        pyfltr.command.error_parser.ErrorLocation(file="src/foo.py", line=5, col=None, command="mypy", message="err3"),
    ]
    sorted_errors = pyfltr.command.error_parser.sort_errors(errors, command_names)

    # ファイル名でソート→同一箇所はcommand_names順
    assert sorted_errors[0].file == "src/bar.py"
    assert sorted_errors[0].command == "ruff-check"  # command_namesで先
    assert sorted_errors[1].file == "src/bar.py"
    assert sorted_errors[1].command == "mypy"
    assert sorted_errors[2].file == "src/foo.py"


def test_parse_errors_normalizes_absolute_path() -> None:
    """絶対パスが相対パスに正規化されることのテスト。"""
    cwd = str(pathlib.Path.cwd())
    # pyright風の絶対パス出力
    output = f"  {cwd}/src/foo.py:10:5 - error: some type error"
    errors = pyfltr.command.error_parser.parse_errors("pyright", output)
    assert len(errors) == 1
    assert errors[0].file == "src/foo.py"  # 相対パスになっている


def test_format_error() -> None:
    """エラーフォーマットのテスト。"""
    error = pyfltr.command.error_parser.ErrorLocation(file="src/foo.py", line=10, col=5, command="mypy", message="some error")
    assert pyfltr.command.error_parser.format_error(error) == "src/foo.py:10:5: [mypy] some error"

    # colなし
    error_no_col = pyfltr.command.error_parser.ErrorLocation(
        file="src/foo.py", line=10, col=None, command="ruff-check", message="another error"
    )
    assert pyfltr.command.error_parser.format_error(error_no_col) == "src/foo.py:10: [ruff-check] another error"

    # ruleあり
    error_with_rule = pyfltr.command.error_parser.ErrorLocation(
        file="src/foo.py", line=10, col=5, command="ruff-check", message="`os` imported but unused", rule="F401"
    )
    assert (
        pyfltr.command.error_parser.format_error(error_with_rule)
        == "src/foo.py:10:5: [ruff-check:F401] `os` imported but unused"
    )


@pytest.mark.parametrize(
    "severity,expected_message",
    [
        ("error", "src/foo.py:10: [designmd] critical issue"),
        ("warning", "src/foo.py:10: [designmd] critical issue"),
        ("info", "src/foo.py:10: [designmd] [INFO] critical issue"),
        (None, "src/foo.py:10: [designmd] critical issue"),
    ],
)
def test_format_error_severity_info_prefix(severity: str | None, expected_message: str) -> None:
    """severity=="info"のときmessage先頭に[INFO] を付加し、他のseverityでは表記を変更しない。"""
    error = pyfltr.command.error_parser.ErrorLocation(
        file="src/foo.py",
        line=10,
        col=None,
        command="designmd",
        message="critical issue",
        severity=severity,
    )
    assert pyfltr.command.error_parser.format_error(error) == expected_message


def test_parse_ruff_check_json() -> None:
    """ruff check --output-format=json出力のパース。"""
    output = json.dumps(
        [
            {
                "code": "F401",
                "message": "`os` imported but unused",
                "filename": "src/foo.py",
                "location": {"row": 1, "column": 8},
                "end_location": {"row": 1, "column": 10},
                "severity": "error",
                "fix": {"applicability": "safe", "edits": []},
            },
        ]
    )
    errors = pyfltr.command.error_parser.parse_errors("ruff-check", output)
    assert len(errors) == 1
    assert errors[0].file == "src/foo.py"
    assert errors[0].line == 1
    assert errors[0].col == 8
    assert errors[0].rule == "F401"
    assert errors[0].severity == "error"
    assert errors[0].fix == "safe"
    assert errors[0].message == "`os` imported but unused"


def test_parse_ruff_check_json_fallback() -> None:
    """ruff-check: JSONでない出力はregexにフォールバックする。"""
    output = "src/foo.py:10:5: F401 `os` imported but unused"
    errors = pyfltr.command.error_parser.parse_errors("ruff-check", output)
    assert len(errors) == 1
    assert errors[0].file == "src/foo.py"
    assert errors[0].line == 10


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
    errors = pyfltr.command.error_parser.parse_errors("ruff-check", output)
    assert len(errors) == 1
    assert errors[0].fix == "none"


def test_parse_typos_jsonl_no_corrections_is_none() -> None:
    """typos: correctionsが空の場合は`fix == "none"`。"""
    output = '{"path":"src/foo.py","line_num":3,"typo":"weirdword","corrections":[],"type":"typo"}\n'
    errors = pyfltr.command.error_parser.parse_errors("typos", output)
    assert len(errors) == 1
    assert errors[0].fix == "none"


def test_parse_textlint_json_fix_none() -> None:
    """textlint: `fix`欠落メッセージは`fix == "none"`。"""
    output = json.dumps(
        [
            {
                "filePath": "docs/index.md",
                "messages": [
                    {
                        "line": 5,
                        "column": 1,
                        "message": "一般的な文体問題",
                        "ruleId": "ja-technical-writing/sentence-length",
                        "severity": 2,
                    },
                ],
            }
        ]
    )
    errors = pyfltr.command.error_parser.parse_errors("textlint", output)
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
                    "type": "convention",
                },
            ],
            "statistics": {},
        }
    )
    errors = pyfltr.command.error_parser.parse_errors("pylint", output)
    assert len(errors) == 1
    assert errors[0].rule == "missing-module-docstring"
    assert errors[0].severity == "warning"
    assert errors[0].message == "C0114: Missing module docstring"
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
    errors = pyfltr.command.error_parser.parse_errors("pylint", prefix + body)
    assert len(errors) == 1
    assert errors[0].rule == "missing-module-docstring"


def test_parse_pylint_json_fallback() -> None:
    """pylint: JSONでない出力はregexにフォールバックする。"""
    output = "src/foo.py:10:5: C0114: Missing module docstring (missing-module-docstring)"
    errors = pyfltr.command.error_parser.parse_errors("pylint", output)
    assert len(errors) == 1
    assert errors[0].line == 10


def test_parse_pyright_json() -> None:
    """pyright --outputjson出力のパース。"""
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
            "summary": {"errorCount": 1},
        }
    )
    errors = pyfltr.command.error_parser.parse_errors("pyright", output)
    assert len(errors) == 1
    assert errors[0].line == 10  # 0-based→1-based
    assert errors[0].col == 5  # 0-based→1-based
    assert errors[0].rule == "reportAssignmentType"
    assert errors[0].severity == "error"


def test_parse_pyright_json_fallback() -> None:
    """pyright: JSONでない出力はregexにフォールバックする。"""
    output = '  src/foo.py:10:5 - error: Type "int" is not assignable'
    errors = pyfltr.command.error_parser.parse_errors("pyright", output)
    assert len(errors) == 1
    assert errors[0].line == 10


def test_parse_shellcheck_json() -> None:
    """shellcheck -f json出力のパース。"""
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
    errors = pyfltr.command.error_parser.parse_errors("shellcheck", output)
    assert len(errors) == 1
    assert errors[0].rule == "SC2086"
    assert errors[0].severity == "warning"
    assert errors[0].message == "Double quote to prevent globbing"


def test_parse_textlint_json() -> None:
    """textlint --format json出力のパース。"""
    output = json.dumps(
        [
            {
                "filePath": "docs/index.md",
                "messages": [
                    {
                        "line": 5,
                        "column": 1,
                        "message": "文末が不統一です。",
                        "ruleId": "ja-technical-writing/ja-no-mixed-period",
                        "severity": 2,
                        "fix": {"range": [10, 11], "text": "。"},
                    },
                ],
            }
        ]
    )
    errors = pyfltr.command.error_parser.parse_errors("textlint", output)
    assert len(errors) == 1
    assert errors[0].rule == "ja-technical-writing/ja-no-mixed-period"
    assert errors[0].severity == "error"
    assert errors[0].fix == "safe"
    # 登録外ルールなのでhintは付与されない
    assert errors[0].hint is None


def test_parse_textlint_json_hint_for_sentence_length() -> None:
    """textlint `sentence-length` 違反には修正ヒントが付与される。"""
    output = json.dumps(
        [
            {
                "filePath": "docs/index.md",
                "messages": [
                    {
                        "line": 1,
                        "column": 1,
                        "message": "Line is too long",
                        "ruleId": "ja-technical-writing/sentence-length",
                        "severity": 2,
                    },
                ],
            }
        ]
    )
    errors = pyfltr.command.error_parser.parse_errors("textlint", output)
    assert len(errors) == 1
    assert errors[0].hint == (
        "textlint counts up to the period (。) as one sentence; bullet-line splits still count as one."
        " Split with periods to shorten."
    )


def test_parse_textlint_json_hint_for_known_rules() -> None:
    """textlint `max-ten` / `max-kanji-continuous-len` / `no-unmatched-pair` にもヒントが付く。"""
    for rule_id in (
        "ja-technical-writing/max-ten",
        "ja-technical-writing/max-kanji-continuous-len",
        "ja-technical-writing/no-unmatched-pair",
    ):
        output = json.dumps(
            [
                {
                    "filePath": "a.md",
                    "messages": [{"line": 1, "column": 1, "message": "x", "ruleId": rule_id, "severity": 2}],
                }
            ]
        )
        errors = pyfltr.command.error_parser.parse_errors("textlint", output)
        assert errors[0].hint is not None, f"{rule_id} にヒントが付与されていない"


def test_parse_textlint_json_hint_for_no_unmatched_pair() -> None:
    """no-unmatched-pairヒントが括弧対応と改行跨ぎの両論を含む。"""
    output = json.dumps(
        [
            {
                "filePath": "a.md",
                "messages": [
                    {
                        "line": 1,
                        "column": 1,
                        "message": "Unmatched pair",
                        "ruleId": "ja-technical-writing/no-unmatched-pair",
                        "severity": 2,
                    }
                ],
            }
        ]
    )
    errors = pyfltr.command.error_parser.parse_errors("textlint", output)
    assert errors[0].hint is not None
    hint = errors[0].hint.lower()
    assert "matched" in hint, "括弧対応そのものに言及するキーワードが含まれていない"
    assert "line break" in hint, "改行跨ぎに言及するキーワードが含まれていない"


def test_parse_textlint_json_normalizes_multiline_message() -> None:
    """textlintのmsgに含まれる改行は半角スペースに畳む。

    sentence-lengthでは`exceeds maximum sentence length of 120.\\nOver 3 characters.`形式で
    改行が含まれるため、JSONL `messages[].msg`を1行に保つ目的で前処理する。
    範囲表記`(L17:1〜23)`は1行化後の末尾に視認しやすく付加する。
    """
    output = json.dumps(
        [
            {
                "filePath": "a.md",
                "messages": [
                    {
                        "line": 17,
                        "column": 1,
                        "message": "Line 17 sentence length(123) exceeds maximum sentence length of 120.\nOver 3 characters.",
                        "ruleId": "ja-technical-writing/sentence-length",
                        "severity": 2,
                        "loc": {"start": {"line": 17, "column": 1}, "end": {"line": 17, "column": 23}},
                    }
                ],
            }
        ]
    )
    errors = pyfltr.command.error_parser.parse_errors("textlint", output)
    assert len(errors) == 1
    assert "\n" not in errors[0].message
    assert errors[0].message.endswith("Over 3 characters. (L17:1〜23)")


def test_parse_textlint_json_normalizes_multiline_message_other_rules() -> None:
    """sentence-length以外のルールでも改行を畳む（textlint側は他ルールも複数行msgを返し得るため）。"""
    output = json.dumps(
        [
            {
                "filePath": "a.md",
                "messages": [
                    {
                        "line": 1,
                        "column": 1,
                        "message": "First line.\n  Second line.",
                        "ruleId": "ja-technical-writing/max-ten",
                        "severity": 2,
                    }
                ],
            }
        ]
    )
    errors = pyfltr.command.error_parser.parse_errors("textlint", output)
    assert errors[0].message == "First line. Second line."


def test_parse_textlint_json_sentence_length_appends_range_single_line() -> None:
    """sentence-length違反ではlocから1行内範囲をmessage末尾へ併記する。"""
    output = json.dumps(
        [
            {
                "filePath": "a.md",
                "messages": [
                    {
                        "line": 17,
                        "column": 1,
                        "message": "Line 17 sentence length(134) exceeds...",
                        "ruleId": "ja-technical-writing/sentence-length",
                        "severity": 2,
                        "loc": {"start": {"line": 17, "column": 1}, "end": {"line": 17, "column": 23}},
                    }
                ],
            }
        ]
    )
    errors = pyfltr.command.error_parser.parse_errors("textlint", output)
    assert len(errors) == 1
    assert errors[0].message.endswith("(L17:1〜23)")


def test_parse_textlint_json_sentence_length_appends_range_multi_line() -> None:
    """複数行にまたがる場合は`(Lstart:col〜Lend:col)`形式で併記する。"""
    output = json.dumps(
        [
            {
                "filePath": "a.md",
                "messages": [
                    {
                        "line": 17,
                        "column": 1,
                        "message": "Long sentence",
                        "ruleId": "ja-technical-writing/sentence-length",
                        "severity": 2,
                        "loc": {"start": {"line": 17, "column": 1}, "end": {"line": 19, "column": 5}},
                    }
                ],
            }
        ]
    )
    errors = pyfltr.command.error_parser.parse_errors("textlint", output)
    assert errors[0].message.endswith("(L17:1〜L19:5)")


def test_parse_textlint_json_other_rules_do_not_get_range() -> None:
    """sentence-length以外のルールではlocがあっても範囲は付与されない。"""
    output = json.dumps(
        [
            {
                "filePath": "a.md",
                "messages": [
                    {
                        "line": 1,
                        "column": 1,
                        "message": "Original message",
                        "ruleId": "ja-technical-writing/max-ten",
                        "severity": 2,
                        "loc": {"start": {"line": 1, "column": 1}, "end": {"line": 1, "column": 5}},
                    }
                ],
            }
        ]
    )
    errors = pyfltr.command.error_parser.parse_errors("textlint", output)
    assert errors[0].message == "Original message"


def test_parse_textlint_json_sentence_length_without_loc() -> None:
    """`loc` フィールドが欠落していても従来通りパースでき、範囲表記は付かない。"""
    output = json.dumps(
        [
            {
                "filePath": "a.md",
                "messages": [
                    {
                        "line": 17,
                        "column": 1,
                        "message": "Long sentence",
                        "ruleId": "ja-technical-writing/sentence-length",
                        "severity": 2,
                    }
                ],
            }
        ]
    )
    errors = pyfltr.command.error_parser.parse_errors("textlint", output)
    assert len(errors) == 1
    assert errors[0].message == "Long sentence"
    # `loc`欠落時はend_line / end_colもNoneのまま
    assert errors[0].end_line is None
    assert errors[0].end_col is None


def test_parse_textlint_json_populates_end_position() -> None:
    """`loc.end`からend_line / end_colをErrorLocationに格納する。

    ルール種別を問わず、`loc.end`があれば共通で取り込む。
    """
    output = json.dumps(
        [
            {
                "filePath": "a.md",
                "messages": [
                    {
                        "line": 17,
                        "column": 1,
                        "message": "Long sentence",
                        "ruleId": "ja-technical-writing/sentence-length",
                        "severity": 2,
                        "loc": {"start": {"line": 17, "column": 1}, "end": {"line": 17, "column": 23}},
                    },
                    {
                        "line": 5,
                        "column": 1,
                        "message": "x",
                        "ruleId": "ja-technical-writing/max-ten",
                        "severity": 2,
                        "loc": {"start": {"line": 5, "column": 1}, "end": {"line": 6, "column": 4}},
                    },
                ],
            }
        ]
    )
    errors = pyfltr.command.error_parser.parse_errors("textlint", output)
    assert len(errors) == 2
    assert (errors[0].end_line, errors[0].end_col) == (17, 23)
    assert (errors[1].end_line, errors[1].end_col) == (6, 4)


def test_parse_textlint_json_end_only_loc_populates_end_position() -> None:
    """`loc.end`のみが提供された入力でもend_line/end_colを取り込み、範囲表記は付与しない。

    loc共通ヘルパーがstart/endを独立に検証する設計の保証用。
    """
    output = json.dumps(
        [
            {
                "filePath": "a.md",
                "messages": [
                    {
                        "line": 17,
                        "column": 1,
                        "message": "Long sentence",
                        "ruleId": "ja-technical-writing/sentence-length",
                        "severity": 2,
                        "loc": {"end": {"line": 17, "column": 23}},
                    }
                ],
            }
        ]
    )
    errors = pyfltr.command.error_parser.parse_errors("textlint", output)
    assert len(errors) == 1
    assert (errors[0].end_line, errors[0].end_col) == (17, 23)
    # `loc.start`が無いため範囲表記は付かない（startの値が決まらないため）
    assert errors[0].message == "Long sentence"


def test_parse_textlint_json_sentence_length_hint_excludes_col_note() -> None:
    """sentence-lengthのヒントは句点による文区切りの観点のみで、`messages[].col`が累積位置である注記は`command.hints`側で集約する。"""
    output = json.dumps(
        [
            {
                "filePath": "a.md",
                "messages": [
                    {
                        "line": 1,
                        "column": 1,
                        "message": "Long",
                        "ruleId": "ja-technical-writing/sentence-length",
                        "severity": 2,
                    }
                ],
            }
        ]
    )
    errors = pyfltr.command.error_parser.parse_errors("textlint", output)
    assert errors[0].hint is not None
    assert "累積位置" not in errors[0].hint


def test_parse_typos_jsonl() -> None:
    """typos --format=json出力（JSON Lines）のパース。"""
    output = (
        '{"path":"src/foo.py","line_num":3,"byte_offset":15,"typo":"teh","corrections":["the"],"type":"typo"}\n'
        '{"path":"src/bar.py","line_num":7,"byte_offset":20,"typo":"hte","corrections":["the","he"],"type":"typo"}\n'
    )
    errors = pyfltr.command.error_parser.parse_errors("typos", output)
    assert len(errors) == 2
    assert errors[0].file == "src/foo.py"
    assert errors[0].line == 3
    assert errors[0].message == "`teh` -> `the`"
    assert errors[0].severity == "warning"
    assert errors[0].fix == "safe"
    assert errors[1].message == "`hte` -> `the, he`"


def test_parse_typos_jsonl_fallback() -> None:
    """typos: JSON Linesでない出力はregexにフォールバックする。"""
    output = "src/foo.py:3:15: `teh` -> `the`"
    errors = pyfltr.command.error_parser.parse_errors("typos", output)
    assert len(errors) == 1
    assert errors[0].line == 3


def test_parse_pytest_tb_short_project_frame() -> None:
    """pytest --tb=short: プロジェクト内フレームが選択され、msg先頭にテスト名が併記される。"""
    output = (
        "================================= FAILURES =================================\n"
        "_______________________________ test_bar ________________________________\n"
        "tests/foo_test.py:42: in test_bar\n"
        "    result = do_something()\n"
        "E   AssertionError: assert 1 == 2\n"
        "========================= short test summary info ==========================\n"
        "FAILED tests/foo_test.py::test_bar - AssertionError: assert 1 == 2\n"
    )
    errors = pyfltr.command.error_parser.parse_errors("pytest", output)
    assert len(errors) == 1
    assert errors[0].file == "tests/foo_test.py"
    assert errors[0].line == 42
    assert errors[0].message.startswith("test_bar: ")
    assert "assert 1 == 2" in errors[0].message


def test_parse_pytest_tb_short_class_based_test() -> None:
    """pytest --tb=short: クラスベーステストでは`TestX.test_y`形式でmsg先頭に併記される。"""
    output = (
        "================================= FAILURES =================================\n"
        "_______________________ TestSomething.test_method ______________________\n"
        "tests/foo_test.py:30: in test_method\n"
        "    assert self.value == 0\n"
        "E   AssertionError: assert 1 == 0\n"
        "========================= short test summary info ==========================\n"
    )
    errors = pyfltr.command.error_parser.parse_errors("pytest", output)
    assert len(errors) == 1
    assert errors[0].message.startswith("TestSomething.test_method: ")


def test_parse_pytest_tb_short_library_exception() -> None:
    """pytest --tb=short: ライブラリ内部で例外が発生した場合、テスト関数フレームが選択される。"""
    output = (
        "================================= FAILURES =================================\n"
        "_______________________________ test_request ________________________________\n"
        "tests/api_test.py:15: in test_request\n"
        "    client.get('/api')\n"
        ".venv/lib/python3.14/site-packages/httpx/_transports/default.py:118: in handle_request\n"
        "    resp = self._pool.handle_request(request)\n"
        "E   httpx.ConnectError: connection refused\n"
        "========================= short test summary info ==========================\n"
    )
    errors = pyfltr.command.error_parser.parse_errors("pytest", output)
    assert len(errors) == 1
    assert errors[0].file == "tests/api_test.py"
    assert errors[0].line == 15
    assert errors[0].message.startswith("test_request: ")
    assert "httpx.ConnectError" in errors[0].message


def test_parse_pytest_tb_short_stdlib_exception() -> None:
    """pytest --tb=short: 標準ライブラリで例外が発生した場合、プロジェクト内フレームが選択される。

    uv管理Pythonでは標準ライブラリが`..`始まりの相対パスで出力される。
    """
    output = (
        "================================= FAILURES =================================\n"
        "_______________________________ test_path ________________________________\n"
        "tests/path_test.py:10: in test_path\n"
        "    pathlib.Path('/nonexistent').resolve(strict=True)\n"
        "../.local/share/uv/python/cpython-3.14.0-linux-x86_64/lib/python3.14/pathlib.py:881: in resolve\n"
        "    s = os.path.realpath(self, strict=strict)\n"
        "E   FileNotFoundError: [Errno 2] No such file or directory: '/nonexistent'\n"
        "========================= short test summary info ==========================\n"
    )
    errors = pyfltr.command.error_parser.parse_errors("pytest", output)
    assert len(errors) == 1
    assert errors[0].file == "tests/path_test.py"
    assert errors[0].line == 10
    assert errors[0].message.startswith("test_path: ")


def test_parse_pytest_tb_short_all_external() -> None:
    """pytest --tb=short: 全フレームがプロジェクト外の場合、最後のフレームにフォールバック。"""
    output = (
        "================================= FAILURES =================================\n"
        "_______________________________ test_ext ________________________________\n"
        ".venv/lib/python3.14/site-packages/somelib/core.py:50: in setup\n"
        "    do_init()\n"
        ".venv/lib/python3.14/site-packages/somelib/init.py:20: in do_init\n"
        "    raise RuntimeError('fail')\n"
        "E   RuntimeError: fail\n"
        "========================= short test summary info ==========================\n"
    )
    errors = pyfltr.command.error_parser.parse_errors("pytest", output)
    assert len(errors) == 1
    assert errors[0].line == 20
    assert errors[0].message.startswith("test_ext: ")
    assert "RuntimeError: fail" in errors[0].message


def test_parse_pytest_fallback() -> None:
    """pytest: --tb=line形式がなければFAILED行にフォールバック（line=0）。"""
    output = (
        "FAILED tests/foo_test.py::test_bar - AssertionError: xxx\n"
        "========================= 1 failed in 0.5s =========================\n"
    )
    errors = pyfltr.command.error_parser.parse_errors("pytest", output)
    assert len(errors) == 1
    assert errors[0].file == "tests/foo_test.py"
    assert errors[0].line == 0


def _vitest_assertion(
    *,
    status: str,
    full_name: str,
    failure_messages: list[str] | None = None,
    location: dict[str, int] | None = None,
) -> dict:
    """vitestのassertionResult dict を組み立てるテスト用ヘルパー。"""
    result: dict = {"status": status, "fullName": full_name}
    if failure_messages is not None:
        result["failureMessages"] = failure_messages
    if location is not None:
        result["location"] = location
    return result


def _vitest_output(test_results: list[dict]) -> str:
    """vitest JSON reporter出力相当の dict を JSON 文字列へ変換するテスト用ヘルパー。"""
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
    """vitest JSON reporter出力を失敗単位のdiagnosticへ変換する。

    JSTQB準拠の同値分割・境界値分析で以下のケースを網羅する。
    (a)単一テスト失敗、(b)複数テスト失敗（異なるファイル・異なるassertion）、
    (c)`location`欠落時のline=1フォールバック、(d)`describe`ネストでの`fullName`併記、
    (e)`failureMessages`空配列のフォールバック、(f)全件成功（空リスト返却）、
    (g)パース不能JSON（空リスト返却）。
    """
    del case_id
    errors = pyfltr.command.error_parser.parse_errors("vitest", output)
    assert len(errors) == len(expected)
    for actual, want in zip(errors, expected, strict=True):
        assert actual.command == "vitest"
        assert actual.line == want["line"]
        assert actual.col == want["col"]
        assert actual.message.startswith(want["message_prefix"])
        assert want["message_contains"] in actual.message


def test_parse_glab_ci_lint_valid() -> None:
    """有効CI出力 (Validating... + ✓ ...) では空リストを返す。"""
    output = "Validating...\n✓ CI/CD YAML is valid!\n"
    assert pyfltr.command.error_parser.parse_errors("glab-ci-lint", output) == []


def test_parse_glab_ci_lint_invalid_multi() -> None:
    """無効CI出力から複数エラーをline=1固定で抽出する。"""
    output = (
        "Validating...\n"
        ".gitlab-ci.yml is invalid\n"
        "\n"
        "- jobs:test config contains unknown keys: foo\n"
        "- root config contains unknown keys: bar\n"
    )
    errors = pyfltr.command.error_parser.parse_errors("glab-ci-lint", output)
    assert len(errors) == 2
    assert all(e.command == "glab-ci-lint" for e in errors)
    assert all(e.file == ".gitlab-ci.yml" for e in errors)
    assert all(e.line == 1 for e in errors)
    assert all(e.col is None for e in errors)
    assert errors[0].message == "jobs:test config contains unknown keys: foo"
    assert errors[1].message == "root config contains unknown keys: bar"


def test_parse_glab_ci_lint_invalid_numbered() -> None:
    """番号付きリスト形式 (`1. xxx`) のエラー行もリストマーカーを除去して取り込む。"""
    output = ".gitlab-ci.yml is invalid\n1. unknown key foo\n2. unknown key bar\n"
    errors = pyfltr.command.error_parser.parse_errors("glab-ci-lint", output)
    assert [e.message for e in errors] == ["unknown key foo", "unknown key bar"]


def test_parse_designmd_json() -> None:
    """`@google/design.md lint`のJSON出力から違反を抽出する。"""
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
    errors = pyfltr.command.error_parser.parse_errors("designmd", output)
    assert len(errors) == 2
    # 対象ファイルは仕様上DESIGN.md固定。
    assert all(e.file == "DESIGN.md" for e in errors)
    assert all(e.command == "designmd" for e in errors)
    assert errors[0].severity == "warning"
    assert errors[0].message.startswith("components.button-primary: ")
    assert errors[1].severity == "error"


def test_parse_designmd_json_empty() -> None:
    """findings空・無効JSONはいずれも空リストを返す。"""
    assert pyfltr.command.error_parser.parse_errors("designmd", json.dumps({"findings": []})) == []
    assert pyfltr.command.error_parser.parse_errors("designmd", "not json") == []
    assert pyfltr.command.error_parser.parse_errors("designmd", "") == []


def test_parse_lychee_json() -> None:
    """lychee --format json のerror_mapからエラー行を抽出する。"""
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
    errors = pyfltr.command.error_parser.parse_errors("lychee", output)
    assert len(errors) == 2
    assert all(e.command == "lychee" for e in errors)
    assert all(e.file == "docs/index.md" for e in errors)
    assert all(e.line == 1 for e in errors)
    assert all(e.severity == "error" for e in errors)
    assert "https://example.com/dead" in errors[0].message
    assert "404 Not Found" in errors[0].message


def test_parse_lychee_json_empty_error_map() -> None:
    """全リンクOK（error_mapが空）の場合は空リストを返す。"""
    output = json.dumps({"total": 5, "successful": 5, "errors": 0, "error_map": {}})
    assert pyfltr.command.error_parser.parse_errors("lychee", output) == []


def test_parse_lychee_json_invalid() -> None:
    """JSON解析失敗時は空リストを返す。"""
    assert pyfltr.command.error_parser.parse_errors("lychee", "not json") == []
    assert pyfltr.command.error_parser.parse_errors("lychee", "") == []


def test_parse_colloquial_check_without_replacement() -> None:
    """colloquial-check: 置換候補なしの`path:line:col: [match] excerpt`形式をパースする。"""
    output = "docs/index.md:3:5: [ちょっと] 本文にちょっと該当する"
    errors = pyfltr.command.error_parser.parse_errors("colloquial-check", output)
    assert len(errors) == 1
    assert errors[0].file == "docs/index.md"
    assert errors[0].line == 3
    assert errors[0].col == 5
    assert errors[0].message == "[ちょっと] 本文にちょっと該当する"


def test_parse_colloquial_check_with_replacement() -> None:
    """colloquial-check: 置換候補ありの`path:line:col: [match] -> [replacement] excerpt`形式をパースする。"""
    output = "docs/index.md:10:1: [唐突感] -> [論理の飛躍] 唐突感が否めない"
    errors = pyfltr.command.error_parser.parse_errors("colloquial-check", output)
    assert len(errors) == 1
    assert errors[0].line == 10
    assert errors[0].col == 1
    assert errors[0].message == "[唐突感] -> [論理の飛躍] 唐突感が否めない"


def test_parse_colloquial_check_multiple_lines() -> None:
    """colloquial-check: 複数件（改行区切り）を全件パースする。"""
    output = "docs/a.md:1:1: [ちょっと] 該当箇所1\ndocs/b.md:2:3: [ぶっちゃけ] 該当箇所2\n"
    errors = pyfltr.command.error_parser.parse_errors("colloquial-check", output)
    assert len(errors) == 2
    assert errors[0].file == "docs/a.md"
    assert errors[1].file == "docs/b.md"


def test_parse_semgrep_json() -> None:
    """semgrep scan --json のresultsから違反を抽出する。"""
    output = json.dumps(
        {
            "results": [
                {
                    "check_id": "rules.python.security.sql-injection",
                    "path": "src/foo.py",
                    "start": {"line": 18, "col": 9, "offset": 300},
                    "end": {"line": 18, "col": 82, "offset": 373},
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
    errors = pyfltr.command.error_parser.parse_errors("semgrep", output)
    assert len(errors) == 2
    assert errors[0].command == "semgrep"
    assert errors[0].file == "src/foo.py"
    assert errors[0].line == 18
    assert errors[0].col == 9
    assert errors[0].rule == "rules.python.security.sql-injection"
    assert errors[0].severity == "error"
    assert "SQL injection" in errors[0].message
    assert errors[1].severity == "warning"


def test_parse_semgrep_json_empty() -> None:
    """results空・無効JSONはいずれも空リストを返す。"""
    assert pyfltr.command.error_parser.parse_errors("semgrep", json.dumps({"results": [], "errors": []})) == []
    assert pyfltr.command.error_parser.parse_errors("semgrep", "not json") == []


def test_parse_bandit_json() -> None:
    """bandit -f json のresultsから違反を抽出する。

    HIGH/MEDIUM/LOWの3severityを網羅し、詳細情報（`more_info`）の有無も検証する。
    """
    output = json.dumps(
        {
            "results": [
                {
                    "filename": "src/foo.py",
                    "line_number": 12,
                    "col_offset": 0,
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
                    "test_id": "B101",
                    "test_name": "assert_used",
                    "issue_severity": "LOW",
                    "issue_text": "Use of assert detected.",
                },
            ],
            "errors": [],
        }
    )
    errors = pyfltr.command.error_parser.parse_errors("bandit", output)
    assert len(errors) == 3
    assert errors[0].command == "bandit"
    assert errors[0].file == "src/foo.py"
    assert errors[0].line == 12
    assert errors[0].col == 0
    assert errors[0].rule == "B602"
    assert errors[0].severity == "error"
    assert "shell=True" in errors[0].message
    assert "(see https://bandit.readthedocs.io/.../b602.html)" in errors[0].message
    assert errors[1].severity == "warning"
    assert errors[1].rule == "B105"
    assert errors[2].severity == "info"
    assert errors[2].rule == "B101"
    # more_info欠落時はメッセージ末尾に`(see ...)`が付かない
    assert "(see " not in errors[2].message


def test_parse_bandit_json_empty() -> None:
    """results空・無効JSONはいずれも空リストを返す。"""
    assert pyfltr.command.error_parser.parse_errors("bandit", json.dumps({"results": [], "errors": []})) == []
    assert pyfltr.command.error_parser.parse_errors("bandit", "not json") == []


def test_parse_sqlfluff_json() -> None:
    """sqlfluff lint --format=json のviolationsから違反を抽出する。"""
    output = json.dumps(
        [
            {
                "filepath": "src/foo.sql",
                "violations": [
                    {
                        "start_line_no": 10,
                        "start_line_pos": 5,
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
    errors = pyfltr.command.error_parser.parse_errors("sqlfluff", output)
    assert len(errors) == 2
    assert errors[0].command == "sqlfluff"
    assert errors[0].file == "src/foo.sql"
    assert errors[0].line == 10
    assert errors[0].col == 5
    assert errors[0].rule == "L001"
    assert errors[0].severity == "error"
    assert errors[1].severity == "warning"


def test_parse_sqlfluff_json_empty() -> None:
    """violations空・無効JSONはいずれも空リストを返す。"""
    assert pyfltr.command.error_parser.parse_errors("sqlfluff", json.dumps([])) == []
    assert pyfltr.command.error_parser.parse_errors("sqlfluff", "not json") == []
    assert pyfltr.command.error_parser.parse_errors("sqlfluff", "") == []


def test_parse_uv_audit() -> None:
    """uv auditのテキスト出力から脆弱性を抽出する（複数advisory・stderr由来ノイズ混在）。"""
    output = (
        "warning: `uv audit` is experimental and may change without warning.\n"
        "Found 2 known vulnerabilities and no adverse project statuses in 146 packages\n"
        "\n"
        "Vulnerabilities:\n"
        "\n"
        "starlette 1.0.0 has 1 known vulnerability:\n"
        "\n"
        "- PYSEC-2026-161: Missing Host header validation poisons request.url.path\n"
        "\n"
        "  Fixed in: 1.0.1\n"
        "\n"
        "  Advisory information: https://github.com/Kludex/starlette/security/advisories/GHSA-86qp-5c8j-p5mr\n"
        "\n"
        "requests 2.0.0 has 1 known vulnerability:\n"
        "\n"
        "- GHSA-9hjg-9r4m-mvj7: Session verification bypass\n"
        "\n"
        "  Fixed in: 2.32.0\n"
    )
    errors = pyfltr.command.error_parser.parse_errors("uv-audit", output)
    assert len(errors) == 2
    assert all(e.command == "uv-audit" for e in errors)
    assert all(e.file == "pyproject.toml" for e in errors)
    assert all(e.line == 1 for e in errors)
    assert all(e.severity == "error" for e in errors)
    assert errors[0].rule == "PYSEC-2026-161"
    assert errors[0].message.startswith("starlette 1.0.0: ")
    assert "Missing Host header validation" in errors[0].message
    assert errors[1].rule == "GHSA-9hjg-9r4m-mvj7"
    assert errors[1].message.startswith("requests 2.0.0: ")


def test_parse_uv_audit_advisory_without_package_header() -> None:
    """package見出し行が先行しないadvisory行は説明のみをmessageへ格納する（フォールバック分岐）。"""
    output = "- PYSEC-2026-999: Some isolated advisory\n"
    errors = pyfltr.command.error_parser.parse_errors("uv-audit", output)
    assert len(errors) == 1
    assert errors[0].command == "uv-audit"
    assert errors[0].file == "pyproject.toml"
    assert errors[0].line == 1
    assert errors[0].rule == "PYSEC-2026-999"
    # package見出しが無いためパッケージ名の前置きは付かない。
    assert errors[0].message == "Some isolated advisory"


def test_parse_uv_audit_same_id_across_packages_not_deduplicated() -> None:
    """同一advisory IDが別パッケージ見出し配下に出た場合、別診断として両方保持する（重複排除しない）。"""
    output = (
        "starlette 1.0.0 has 1 known vulnerability:\n"
        "\n"
        "- GHSA-aaaa-bbbb-cccc: Shared transitive advisory\n"
        "\n"
        "requests 2.0.0 has 1 known vulnerability:\n"
        "\n"
        "- GHSA-aaaa-bbbb-cccc: Shared transitive advisory\n"
    )
    errors = pyfltr.command.error_parser.parse_errors("uv-audit", output)
    # パッケージ単位で列挙されるため同一IDでも2件保持する。
    assert len(errors) == 2
    assert all(e.rule == "GHSA-aaaa-bbbb-cccc" for e in errors)
    assert errors[0].message.startswith("starlette 1.0.0: ")
    assert errors[1].message.startswith("requests 2.0.0: ")


def test_parse_uv_audit_no_advisories() -> None:
    """脆弱性なし出力（Found 0行のみ）・空文字・advisory非該当テキストはいずれも空リストを返す。"""
    found_zero = (
        "warning: `uv audit` is experimental and may change without warning.\n"
        "Found 0 known vulnerabilities and no adverse project statuses in 146 packages\n"
    )
    assert pyfltr.command.error_parser.parse_errors("uv-audit", found_zero) == []
    assert pyfltr.command.error_parser.parse_errors("uv-audit", "") == []
    assert pyfltr.command.error_parser.parse_errors("uv-audit", "no relevant lines here") == []


def test_parse_npm_audit_json() -> None:
    """npm audit --json（auditReportVersion 2）でvia文字列要素のスキップとsource重複排除を確認する。"""
    output = json.dumps(
        {
            "auditReportVersion": 2,
            "vulnerabilities": {
                "minimist": {
                    "name": "minimist",
                    "severity": "critical",
                    "via": [
                        {
                            "source": 1096466,
                            "name": "minimist",
                            "title": "Prototype Pollution in minimist",
                            "url": "https://github.com/advisories/GHSA-vh95-rmgr-6w4m",
                            "severity": "moderate",
                            "range": "<0.2.1",
                        },
                        {
                            "source": 1097677,
                            "name": "minimist",
                            "title": "Prototype Pollution in minimist",
                            "url": "https://github.com/advisories/GHSA-xvch-5gv4-984h",
                            "severity": "critical",
                            "range": "<0.2.4",
                        },
                        "another-package",
                    ],
                    "range": "<=0.2.3",
                },
                "another-package": {
                    "name": "another-package",
                    "severity": "critical",
                    "via": [
                        {
                            "source": 1097677,
                            "name": "minimist",
                            "title": "Prototype Pollution in minimist",
                            "url": "https://github.com/advisories/GHSA-xvch-5gv4-984h",
                            "severity": "critical",
                            "range": "<0.2.4",
                        }
                    ],
                },
            },
            "metadata": {"vulnerabilities": {"total": 2}},
        }
    )
    errors = pyfltr.command.error_parser.parse_errors("npm-audit", output)
    # 文字列要素スキップ・source重複排除によりsource 1096466 / 1097677の2件のみ。
    assert len(errors) == 2
    assert all(e.command == "npm-audit" for e in errors)
    assert all(e.file == "package.json" for e in errors)
    assert all(e.line == 1 for e in errors)
    by_rule = {e.rule: e for e in errors}
    assert set(by_rule) == {"GHSA-vh95-rmgr-6w4m", "GHSA-xvch-5gv4-984h"}
    assert by_rule["GHSA-vh95-rmgr-6w4m"].severity == "warning"  # moderate
    assert by_rule["GHSA-xvch-5gv4-984h"].severity == "error"  # critical
    assert "minimist" in by_rule["GHSA-vh95-rmgr-6w4m"].message
    assert "(<0.2.1)" in by_rule["GHSA-vh95-rmgr-6w4m"].message
    assert by_rule["GHSA-xvch-5gv4-984h"].rule_url == "https://github.com/advisories/GHSA-xvch-5gv4-984h"


def test_parse_pnpm_audit_json() -> None:
    """pnpm audit --json（advisories形式）から脆弱性を抽出する。"""
    output = json.dumps(
        {
            "advisories": {
                "1096466": {
                    "id": 1096466,
                    "title": "Prototype Pollution in minimist",
                    "module_name": "minimist",
                    "severity": "moderate",
                    "vulnerable_versions": "<0.2.1",
                    "github_advisory_id": "GHSA-vh95-rmgr-6w4m",
                    "url": "https://github.com/advisories/GHSA-vh95-rmgr-6w4m",
                },
                "1097677": {
                    "id": 1097677,
                    "title": "Prototype Pollution in minimist",
                    "module_name": "minimist",
                    "severity": "critical",
                    "vulnerable_versions": "<0.2.4",
                    "github_advisory_id": "GHSA-xvch-5gv4-984h",
                    "url": "https://github.com/advisories/GHSA-xvch-5gv4-984h",
                },
            },
            "metadata": {"vulnerabilities": {"moderate": 1, "critical": 1}},
        }
    )
    errors = pyfltr.command.error_parser.parse_errors("pnpm-audit", output)
    assert len(errors) == 2
    assert all(e.command == "pnpm-audit" for e in errors)
    assert all(e.file == "package.json" for e in errors)
    assert errors[0].rule == "GHSA-vh95-rmgr-6w4m"
    assert errors[0].severity == "warning"
    assert errors[0].message.startswith("minimist: ")
    assert "(<0.2.1)" in errors[0].message
    assert errors[1].rule == "GHSA-xvch-5gv4-984h"
    assert errors[1].severity == "error"
    assert errors[1].rule_url == "https://github.com/advisories/GHSA-xvch-5gv4-984h"


def test_parse_yarn_audit_jsonl() -> None:
    """yarn audit --json（JSON Lines）でauditAdvisory抽出・id重複排除・summary行スキップを確認する。"""
    lines = [
        json.dumps(
            {
                "type": "auditAdvisory",
                "data": {
                    "advisory": {
                        "id": 1096466,
                        "title": "Prototype Pollution in minimist",
                        "module_name": "minimist",
                        "severity": "moderate",
                        "vulnerable_versions": "<0.2.1",
                        "github_advisory_id": "GHSA-vh95-rmgr-6w4m",
                        "url": "https://github.com/advisories/GHSA-vh95-rmgr-6w4m",
                    }
                },
            }
        ),
        json.dumps(
            {
                "type": "auditAdvisory",
                "data": {
                    "advisory": {
                        "id": 1097677,
                        "title": "Prototype Pollution in minimist",
                        "module_name": "minimist",
                        "severity": "critical",
                        "vulnerable_versions": "<0.2.4",
                        "github_advisory_id": "GHSA-xvch-5gv4-984h",
                        "url": "https://github.com/advisories/GHSA-xvch-5gv4-984h",
                    }
                },
            }
        ),
        # 同一advisory（id重複）→ 重複排除される。
        json.dumps(
            {
                "type": "auditAdvisory",
                "data": {
                    "advisory": {
                        "id": 1097677,
                        "title": "Prototype Pollution in minimist",
                        "module_name": "minimist",
                        "severity": "critical",
                        "vulnerable_versions": "<0.2.4",
                        "github_advisory_id": "GHSA-xvch-5gv4-984h",
                        "url": "https://github.com/advisories/GHSA-xvch-5gv4-984h",
                    }
                },
            }
        ),
        # auditSummary行は集計のためスキップされる。
        json.dumps({"type": "auditSummary", "data": {"vulnerabilities": {"moderate": 1, "critical": 1}}}),
    ]
    errors = pyfltr.command.error_parser.parse_errors("yarn-audit", "\n".join(lines))
    assert len(errors) == 2
    assert all(e.command == "yarn-audit" for e in errors)
    assert all(e.file == "package.json" for e in errors)
    assert errors[0].rule == "GHSA-vh95-rmgr-6w4m"
    assert errors[0].severity == "warning"
    assert errors[1].rule == "GHSA-xvch-5gv4-984h"
    assert errors[1].severity == "error"
    assert errors[1].message.startswith("minimist: ")


@pytest.mark.parametrize(
    "command,empty_output",
    [
        ("npm-audit", json.dumps({"auditReportVersion": 2, "vulnerabilities": {}, "metadata": {}})),
        ("pnpm-audit", json.dumps({"advisories": {}, "metadata": {}})),
        ("yarn-audit", json.dumps({"type": "auditSummary", "data": {"vulnerabilities": {}}})),
    ],
)
def test_parse_js_audit_no_vulnerabilities(command: str, empty_output: str) -> None:
    """脆弱性なしのJSON出力ではJavaScript系監査ツールは空リストを返す（uv-auditはテキストのため別テスト）。"""
    assert pyfltr.command.error_parser.parse_errors(command, empty_output) == []


@pytest.mark.parametrize("command", ["npm-audit", "pnpm-audit", "yarn-audit"])
def test_parse_js_audit_invalid_input(command: str) -> None:
    """不正JSON・空文字ではJavaScript系監査ツールは空リストを返す（uv-auditはテキストのため別テスト）。"""
    assert pyfltr.command.error_parser.parse_errors(command, "not json") == []
    assert pyfltr.command.error_parser.parse_errors(command, "") == []


def test_get_custom_parser_commands() -> None:
    """カスタムパーサー登録コマンド一覧の取得。"""
    commands = pyfltr.command.error_parser.get_custom_parser_commands()
    assert "eslint" in commands
    assert "ruff-check" in commands
    assert "pytest" in commands
    assert "designmd" in commands
    assert "lychee" in commands
    assert "uv-audit" in commands
    assert "npm-audit" in commands
    assert "pnpm-audit" in commands
    assert "yarn-audit" in commands
    assert "semgrep" in commands
    assert "sqlfluff" in commands
    assert "mypy" not in commands


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
    result = pyfltr.command.error_parser.parse_summary("pyright", output)
    assert result == "50 files analyzed, 0 errors, 2 warnings"


def test_parse_summary_pyright_json_no_summary() -> None:
    """pyright: summaryフィールドがない場合はNone。"""
    output = json.dumps({"generalDiagnostics": []})
    assert pyfltr.command.error_parser.parse_summary("pyright", output) is None


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
    result = pyfltr.command.error_parser.parse_summary("pylint", output)
    assert result == "42 modules linted, score: 10.0"


def test_parse_summary_pylint_json_no_score() -> None:
    """pylint: scoreがない場合はモジュール数のみ。"""
    output = json.dumps({"messages": [], "statistics": {"modulesLinted": 10}})
    result = pyfltr.command.error_parser.parse_summary("pylint", output)
    assert result == "10 modules linted"


def test_parse_summary_pytest() -> None:
    """pytest: 末尾のサマリー行の=パディングを除去して取り出す。"""
    output = (
        "============================= test session starts ==============================\n"
        "collected 25 items\n"
        "\n"
        "tests/foo_test.py .........................                                [100%]\n"
        "\n"
        "============================== 25 passed in 1.23s ==============================\n"
    )
    result = pyfltr.command.error_parser.parse_summary("pytest", output)
    assert result == "25 passed in 1.23s"


def test_parse_summary_pytest_long_duration() -> None:
    """pytest: 長時間実行時の (H:MM:SS) 形式も正しく抽出する。"""
    output = "============================== 25 passed in 60.00s (0:01:00) ==============================\n"
    result = pyfltr.command.error_parser.parse_summary("pytest", output)
    assert result == "25 passed in 60.00s (0:01:00)"


def test_parse_summary_mypy_via_fallback() -> None:
    """mypy: 汎用フォールバックでSuccess行を抽出する。"""
    output = "Success: no issues found in 42 source files\n"
    result = pyfltr.command.error_parser.parse_summary("mypy", output)
    assert result == "Success: no issues found in 42 source files"


def test_parse_summary_json_output_returns_none() -> None:
    """JSON出力（[]等）は汎用フォールバックでNoneを返す。"""
    assert pyfltr.command.error_parser.parse_summary("ruff-check", "[]") is None
    assert pyfltr.command.error_parser.parse_summary("shellcheck", "[]") is None


def test_parse_summary_empty_output() -> None:
    """空出力はNoneを返す。"""
    assert pyfltr.command.error_parser.parse_summary("mypy", "") is None
    assert pyfltr.command.error_parser.parse_summary("mypy", "  \n  ") is None


def test_extract_last_line_skips_separators() -> None:
    """区切り線のみの行をスキップして意味のある行を返す。"""
    output = "Some useful info\n===========================\n"
    result = pyfltr.command.error_parser.parse_summary("unknown-tool", output)
    assert result == "Some useful info"


def test_parse_errors_mypy_extracts_rule() -> None:
    """mypyの末尾`[error-code]`がruleグループで抽出されrule_urlも付与される。"""
    output = 'src/foo.py:10: error: Name "x" is not defined  [name-defined]'
    errors = pyfltr.command.error_parser.parse_errors("mypy", output)
    assert len(errors) == 1
    assert errors[0].rule == "name-defined"
    assert errors[0].rule_url == "https://mypy.readthedocs.io/en/stable/_refs.html#code-name-defined"
    # messageに末尾の[rule]は含めない
    assert errors[0].message == 'Name "x" is not defined'


def test_parse_errors_mypy_without_rule() -> None:
    """mypyで末尾[code]が無い行はrule=Noneになる。"""
    output = "src/foo.py:10: error: Something went wrong"
    errors = pyfltr.command.error_parser.parse_errors("mypy", output)
    assert len(errors) == 1
    assert errors[0].rule is None
    assert errors[0].rule_url is None


def test_parse_errors_markdownlint_extracts_rule() -> None:
    """markdownlintのMDxxxがruleグループで抽出される。"""
    output = "docs/index.md:3 MD001/heading-increment Heading levels should only increment by one level at a time"
    errors = pyfltr.command.error_parser.parse_errors("markdownlint", output)
    assert len(errors) == 1
    assert errors[0].rule == "MD001"
    assert errors[0].rule_url == "https://github.com/DavidAnson/markdownlint/blob/main/doc/MD001.md"


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
    errors = pyfltr.command.error_parser.parse_errors("ruff-check", output)
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
    errors = pyfltr.command.error_parser.parse_errors("ruff-check", output)
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
    errors = pyfltr.command.error_parser.parse_errors("pyright", output)
    assert errors[0].rule_url == "https://microsoft.github.io/pyright/#/configuration?id=reportAssignmentType"


def test_parse_errors_shellcheck_rule_url() -> None:
    """shellcheckのruleからrule_urlが生成される。"""
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
    errors = pyfltr.command.error_parser.parse_errors("shellcheck", output)
    assert errors[0].rule_url == "https://www.shellcheck.net/wiki/SC2086"


def test_parse_errors_eslint_rule_url() -> None:
    """eslintの本体ルールからrule_urlが生成される。プラグインルールはURL無し。"""
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
    errors = pyfltr.command.error_parser.parse_errors("eslint", output)
    assert len(errors) == 2
    assert errors[0].rule_url == "https://eslint.org/docs/latest/rules/no-unused-vars"
    # プラグインルール（スラッシュ含む）はURLを返さない
    assert errors[1].rule_url is None


def test_parse_errors_textlint_no_rule_url() -> None:
    """textlintはrule_url未サポート（常にNone）。"""
    output = json.dumps(
        [
            {
                "filePath": "docs/index.md",
                "messages": [
                    {
                        "line": 5,
                        "column": 1,
                        "message": "x",
                        "ruleId": "some-rule",
                        "severity": 2,
                    },
                ],
            }
        ]
    )
    errors = pyfltr.command.error_parser.parse_errors("textlint", output)
    assert errors[0].rule_url is None


def test_parse_errors_shellcheck_severity_normalized() -> None:
    """shellcheckのlevel=STYLEなどを正規化する。"""
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
    errors = pyfltr.command.error_parser.parse_errors("shellcheck", output)
    assert errors[0].severity == "info"
