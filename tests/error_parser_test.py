"""error_parserのテストコード。"""

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
        # biome --reporter=github (notice = info)。severity infoのルールがnoticeとして出力される
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

    `::notice`はbiomeがseverity infoの診断へ用いる出力形式で、pyfltr側ではinfoとして公開する。
    severityは各ルールの既定値で決まり、fixのsafe/unsafeとは独立である。
    あわせてmessage本文が欠落せず保持されることを全ケースで確認する。
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


def test_parse_pytest_worker_crash_generates_diagnostic_with_message() -> None:
    """pytest: xdistワーカークラッシュを表すブロックからmessageを保持した診断を生成する。

    Windows CIのpytest-timeout（thread方式）でワーカーが強制終了した際の実測出力を再現する。
    """
    output = (
        "====================================== FAILURES ======================================\n"
        "_________________________________ test_normal_fail __________________________________\n"
        "[gw1] linux -- Python 3.13.3 /path/to/python3\n"
        "tbexp/test_slow.py:9: in test_normal_fail\n"
        "    assert 1 == 2\n"
        "E   assert 1 == 2\n"
        "________________________________ tbexp/test_slow.py _________________________________\n"
        "[gw0] linux -- Python 3.13.3 /path/to/python3\n"
        "worker 'gw0' crashed while running 'tbexp/test_slow.py::test_slow'\n"
        "============================== short test summary info ===============================\n"
        "FAILED tbexp/test_slow.py::test_normal_fail - assert 1 == 2\n"
        "FAILED tbexp/test_slow.py::test_slow - worker 'gw0' crashed while running "
        "'tbexp/test_slow.py::test_slow'\n"
    )
    errors = pyfltr.command.error_parser.parse_errors("pytest", output)
    assert len(errors) == 2
    crashed = next(e for e in errors if e.line == 0)
    assert crashed.file == "tbexp/test_slow.py"
    assert "worker 'gw0' crashed" in crashed.message
    assert "test_slow" in crashed.message
    normal = next(e for e in errors if e.line == 9)
    assert normal.file == "tbexp/test_slow.py"
    assert "test_normal_fail" in normal.message


def test_parse_pytest_tb_line_format() -> None:
    """pytest: --tb=line形式は`<file>:<line>: <message>`行から行番号付きの診断を生成する。"""
    abs_file = pathlib.Path.cwd() / "tbexp" / "test_line.py"
    output = (
        "====================================== FAILURES ======================================\n"
        "E   assert 1 == 2\n"
        f"{abs_file}:2: assert 1 == 2\n"
        "E   RuntimeError: boom\n"
        f"{abs_file}:6: RuntimeError: boom\n"
        "============================== short test summary info ===============================\n"
        "FAILED tbexp/test_line.py::test_a - assert 1 == 2\n"
        "FAILED tbexp/test_line.py::test_b - RuntimeError: boom\n"
    )
    errors = pyfltr.command.error_parser.parse_errors("pytest", output)
    assert len(errors) == 2
    err_a = next(e for e in errors if e.line == 2)
    assert err_a.file == "tbexp/test_line.py"
    assert err_a.message == "test_a: assert 1 == 2"
    err_b = next(e for e in errors if e.line == 6)
    assert err_b.message == "test_b: RuntimeError: boom"


def test_parse_pytest_tb_short_class_based_test_not_double_counted_with_summary() -> None:
    """pytest --tb=short: クラスベーステストの`::`区切りテスト名を`.`区切りへ正規化して
    ブロック側と突合し、summary残余補完で二重生成されないことを検証する。

    実測出力ではブロック見出しが`TestSomething.test_method`（`.`区切り）、summaryの
    `FAILED`行が`tbexp/test_cls.py::TestSomething::test_method`（`::`区切り）となり
    表記が一致しない。正規化を欠くと診断が2件生成される。
    """
    output = (
        "====================================== FAILURES ======================================\n"
        "_____________________________ TestSomething.test_method ______________________________\n"
        "tbexp/test_cls.py:3: in test_method\n"
        "    assert 1 == 2\n"
        "E   assert 1 == 2\n"
        "============================== short test summary info ===============================\n"
        "FAILED tbexp/test_cls.py::TestSomething::test_method - assert 1 == 2\n"
    )
    errors = pyfltr.command.error_parser.parse_errors("pytest", output)
    assert len(errors) == 1
    assert errors[0].message.startswith("TestSomething.test_method: ")


def test_parse_pytest_summary_only_handles_parametrized_id_with_space() -> None:
    """pytest: summary行のみ（FAILURESセクション無し）でも、パラメータIDの空白を
    テスト名の一部として取り込み、診断を生成する。

    `_PYTEST_SUMMARY_RE`のテスト名部分が`\\S+?`のままだと空白で分割されてしまい
    行全体が一致せず、このテストの失敗が診断として一切出てこなくなる。
    """
    output = (
        "============================== short test summary info ===============================\n"
        "FAILED tbexp/test_param.py::test_a[param with space] - AssertionError: "
        "assert 'param with space' == 'x'\n"
    )
    errors = pyfltr.command.error_parser.parse_errors("pytest", output)
    assert len(errors) == 1
    assert errors[0].file == "tbexp/test_param.py"
    assert errors[0].line == 0
    assert errors[0].message.startswith("test_a[param with space]: ")


def test_parse_pytest_fallback() -> None:
    """pytest: FAILURESセクションが無い場合、summary行から`line=0`の診断を生成する。"""
    output = (
        "FAILED tests/foo_test.py::test_bar - AssertionError: xxx\n"
        "========================= 1 failed in 0.5s =========================\n"
    )
    errors = pyfltr.command.error_parser.parse_errors("pytest", output)
    assert len(errors) == 1
    assert errors[0].file == "tests/foo_test.py"
    assert errors[0].line == 0


def test_parse_pytest_parametrized_id_with_hyphen_not_double_counted() -> None:
    """pytest --tb=short: パラメータIDが` - `を含むテストでも、ブロックとsummaryが正しく突合され
    診断が二重生成されないことを検証する。

    `_PYTEST_SUMMARY_RE`のテスト名部分が非貪欲な`.+?`のままだと、summary行の最初の` - `区切りで
    テスト名が`test_param[b`まで切れてしまい、ブロック側の`test_param[b - c]`と突合できず
    summary残余補完で余分な診断が生成される。
    """
    output = (
        "================================= FAILURES =================================\n"
        "_______________________________ test_param[b - c] ________________________________\n"
        "tests/a_test.py:5: in test_param\n"
        "    assert value == 'x'\n"
        "E   AssertionError: assert 'b - c' == 'x'\n"
        "========================= short test summary info ==========================\n"
        "FAILED tests/a_test.py::test_param[b - c] - AssertionError: assert 'b - c' == 'x'\n"
    )
    errors = pyfltr.command.error_parser.parse_errors("pytest", output)
    assert len(errors) == 1
    assert errors[0].file == "tests/a_test.py"
    assert errors[0].line == 5
    assert errors[0].message.startswith("test_param[b - c]: ")


def test_parse_pytest_parametrized_id_with_brackets_is_not_dropped() -> None:
    """pytest: パラメータIDが閉じ角括弧・入れ子の角括弧を含んでも失敗が取りこぼされないことを検証する。

    `ids`へリストや型注釈風の文字列を渡すと`test_listid[['a', 'b']]`・`test_nested[list[int] and str]`
    のようなIDが生成される。`_PYTEST_SUMMARY_RE`の角括弧部分を`\\[[^\\]]*\\]`のように閉じ括弧を
    越えられない表現にすると、これらのsummary行が一切マッチせず、summary行のみが情報源となる
    `--tb=no`等では当該失敗が診断から完全に消える。
    """
    output = (
        "========================= short test summary info ==========================\n"
        "FAILED tests/e_test.py::test_listid[['a', 'b']] - AssertionError: assert ['a', 'b'] == ['x']\n"
        "FAILED tests/e_test.py::test_nested[list[int] and str] - AssertionError: assert 'z' == 'x'\n"
        "FAILED tests/e_test.py::test_deep[[[x y]]] - AssertionError: deep\n"
    )
    errors = pyfltr.command.error_parser.parse_errors("pytest", output)
    assert len(errors) == 3
    messages = sorted(e.message for e in errors)
    assert messages[0].startswith("test_deep[[[x y]]]: ")
    assert messages[1].startswith("test_listid[['a', 'b']]: ")
    assert messages[2].startswith("test_nested[list[int] and str]: ")
    assert all(e.file == "tests/e_test.py" for e in errors)


def test_parse_pytest_default_traceback_single_frame_has_line_number() -> None:
    """pytest --tb=auto: フレームが1つだけの失敗でも行番号を報告することを検証する。

    既定のトレースバック形式はフレーム行（`<file>:<line>: in <func>`）を出さず、
    エントリーの末尾へ`<file>:<line>: <例外名>`の位置行を出力する。位置行を拾わないと
    集計行由来の`line=0`へ落ち、継続的インテグレーションの注釈が該当行を指さない。
    検体はpytest 9.1.1の実出力から採る。
    """
    output = (
        "================================= FAILURES =================================\n"
        "_______________________________ test_simple ________________________________\n"
        "\n"
        "    def test_simple():\n"
        ">       assert 1 == 2\n"
        "E       assert 1 == 2\n"
        "\n"
        "sample_test.py:2: AssertionError\n"
        "========================= short test summary info ==========================\n"
        "FAILED sample_test.py::test_simple - assert 1 == 2\n"
        "================================= 1 failed in 0.03s =================================\n"
    )
    errors = pyfltr.command.error_parser.parse_errors("pytest", output)
    assert len(errors) == 1
    assert errors[0].file == "sample_test.py"
    assert errors[0].line == 2
    assert errors[0].message == "test_simple: assert 1 == 2"


def test_parse_pytest_default_traceback_chained_exception_uses_last_location() -> None:
    """pytest --tb=auto: 例外の連鎖では最後の位置行とその例外を採ることを検証する。

    連鎖したエントリーが並ぶ場合、実際に失敗を起こしたのは最後のエントリーであり、
    集計行のメッセージとも一致する。先頭のエラー行を採ると内側の例外を報告する。
    """
    output = (
        "================================= FAILURES =================================\n"
        "_______________________________ test_chained ________________________________\n"
        "\n"
        "    def test_chained():\n"
        "        try:\n"
        ">           raise KeyError('k')\n"
        "E           KeyError: 'k'\n"
        "\n"
        "sample_test.py:14: KeyError\n"
        "\n"
        "The above exception was the direct cause of the following exception:\n"
        "\n"
        "    def test_chained():\n"
        ">           raise RuntimeError('wrapped') from e\n"
        "E           RuntimeError: wrapped\n"
        "\n"
        "sample_test.py:16: RuntimeError\n"
        "========================= short test summary info ==========================\n"
        "FAILED sample_test.py::test_chained - RuntimeError: wrapped\n"
        "================================= 1 failed in 0.03s =================================\n"
    )
    errors = pyfltr.command.error_parser.parse_errors("pytest", output)
    assert len(errors) == 1
    assert errors[0].line == 16
    assert errors[0].message == "test_chained: RuntimeError: wrapped"


def test_parse_pytest_short_traceback_chained_exception_uses_outer_message() -> None:
    """pytest --tb=short: 例外の連鎖で最後のエントリーの例外をメッセージに採ることを検証する。

    ブロック先頭のエラー行を採ると内側の例外（`KeyError`）を報告し、集計行が示す
    実際の失敗理由（`RuntimeError`）と食い違う。
    """
    output = (
        "================================= FAILURES =================================\n"
        "_______________________________ test_chained ________________________________\n"
        "sample_test.py:14: in test_chained\n"
        "    raise KeyError('k')\n"
        "E   KeyError: 'k'\n"
        "\n"
        "The above exception was the direct cause of the following exception:\n"
        "sample_test.py:16: in test_chained\n"
        "    raise RuntimeError('wrapped') from e\n"
        "E   RuntimeError: wrapped\n"
        "========================= short test summary info ==========================\n"
        "FAILED sample_test.py::test_chained - RuntimeError: wrapped\n"
        "================================= 1 failed in 0.03s =================================\n"
    )
    errors = pyfltr.command.error_parser.parse_errors("pytest", output)
    assert len(errors) == 1
    assert errors[0].line == 16
    assert errors[0].message == "test_chained: RuntimeError: wrapped"


def test_parse_pytest_default_traceback_mixed_frames_prefers_last_location() -> None:
    """pytest --tb=auto: フレーム行と位置行が混在する場合に位置行を優先することを検証する。

    既定のトレースバック形式は最初と最後のエントリーを詳細形式で、中間のエントリーを
    簡略形式（フレーム行）で出力する。フレーム行だけを見ると中間エントリーの位置を報告し、
    実際に例外が発生した位置を外す。中間エントリーの区切り行をブロック見出しとして
    誤って拾わないことも併せて確認する。
    """
    separator = "_ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ \n"
    output = (
        "================================= FAILURES =================================\n"
        "_______________________________ test_deep ________________________________\n"
        "\n"
        "    def test_deep():\n"
        ">       a()\n"
        "\n"
        "deep_test.py:14: \n" + separator + "deep_test.py:2: in a\n"
        "    b()\n"
        "deep_test.py:6: in b\n"
        "    c()\n" + separator + "\n"
        "    def c():\n"
        ">       raise ValueError('deep')\n"
        "E       ValueError: deep\n"
        "\n"
        "deep_test.py:10: ValueError\n"
        "========================= short test summary info ==========================\n"
        "FAILED deep_test.py::test_deep - ValueError: deep\n"
        "================================= 1 failed in 0.06s =================================\n"
    )
    errors = pyfltr.command.error_parser.parse_errors("pytest", output)
    assert len(errors) == 1
    assert errors[0].file == "deep_test.py"
    assert errors[0].line == 10
    assert errors[0].message == "test_deep: ValueError: deep"


def test_parse_pytest_default_traceback_ignores_location_line_in_captured_output() -> None:
    """pytest --tb=auto: 捕捉出力の中にある位置行を失敗の位置として採らないことを検証する。

    捕捉出力は任意のテキストであり、位置行と同じ書式の行が現れても失敗の位置ではない。
    節見出しより後まで走査すると、捕捉出力の側の行を失敗の位置として報告する。
    """
    output = (
        "================================= FAILURES =================================\n"
        "_______________________________ test_prints ________________________________\n"
        "\n"
        "    def test_prints():\n"
        ">       assert False\n"
        "E       assert False\n"
        "\n"
        "tests/x_test.py:5: AssertionError\n"
        "-------------------------- Captured stdout call ---------------------------\n"
        "other/noise.py:99: RuntimeError\n"
        "========================= short test summary info ==========================\n"
        "FAILED tests/x_test.py::test_prints - assert False\n"
        "================================= 1 failed in 0.10s =================================\n"
    )
    errors = pyfltr.command.error_parser.parse_errors("pytest", output)
    assert len(errors) == 1
    assert errors[0].file == "tests/x_test.py"
    assert errors[0].line == 5


def test_parse_pytest_default_traceback_external_location_keeps_project_frame() -> None:
    """pytest --tb=auto: 位置行がプロジェクト外を指す場合はフレーム選択を維持することを検証する。

    テスト関数が中継関数を経てサードパーティー内で例外になる形では、簡略形式の
    フレーム行がプロジェクト内を指し、末尾の位置行がプロジェクト外を指す。
    `--tb=short`が選ぶ位置と揃えるため、フレーム行の選択を優先する。
    """
    separator = "_ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ \n"
    output = (
        "================================= FAILURES =================================\n"
        "_______________________________ test_ext3 ________________________________\n"
        "\n"
        "    def test_ext3():\n"
        ">       helper()\n"
        "\n"
        "tests/e_test.py:13: \n" + separator + "tests/e_test.py:9: in helper\n"
        "    somelib.boom()\n" + separator + "\n"
        "    def boom():\n"
        ">       raise ValueError('boom')\n"
        "E       ValueError: boom\n"
        "\n"
        ".venv/lib/python3.13/site-packages/somelib/core.py:2: ValueError\n"
        "========================= short test summary info ==========================\n"
        "FAILED tests/e_test.py::test_ext3 - ValueError: boom\n"
        "================================= 1 failed in 0.10s =================================\n"
    )
    errors = pyfltr.command.error_parser.parse_errors("pytest", output)
    assert len(errors) == 1
    assert errors[0].file == "tests/e_test.py"
    assert errors[0].line == 9


def test_parse_pytest_default_traceback_external_location_without_frames() -> None:
    """pytest --tb=auto: フレーム行を持たずプロジェクト外で終わる失敗の位置を検証する。

    テスト関数が直接サードパーティー関数を呼んで例外になる形ではエントリーが2つとなり、
    簡略形式のフレーム行が現れない。プロジェクト内の位置行は例外名を伴わないが、
    `--tb=short`が選ぶプロジェクト内フレームと同じ位置を指すためこれを採る。
    """
    separator = "_ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ \n"
    output = (
        "================================= FAILURES =================================\n"
        "_______________________________ test_ext ________________________________\n"
        "\n"
        "    def test_ext():\n"
        ">       somelib.boom()\n"
        "\n"
        "tests/e_test.py:9: \n" + separator + "\n"
        "    def boom():\n"
        ">       raise ValueError('boom')\n"
        "E       ValueError: boom\n"
        "\n"
        ".venv/lib/python3.13/site-packages/somelib/core.py:2: ValueError\n"
        "========================= short test summary info ==========================\n"
        "FAILED tests/e_test.py::test_ext - ValueError: boom\n"
        "================================= 1 failed in 0.10s =================================\n"
    )
    errors = pyfltr.command.error_parser.parse_errors("pytest", output)
    assert len(errors) == 1
    assert errors[0].file == "tests/e_test.py"
    assert errors[0].line == 9
    assert errors[0].message == "test_ext: ValueError: boom"


def test_parse_pytest_doctest_failure_is_not_double_counted() -> None:
    """pytest --doctest-modules: doctestの失敗で診断が二重生成されないことを検証する。

    ブロック見出しは`[doctest] <モジュール>.<関数>`形式で、集計行のテスト名と一致しない。
    接頭辞を除かずに突合すると、位置行由来の診断と残余補完の`line=0`の診断が並ぶ。
    エラー行を持たないため、メッセージはテスト名のみとなる。
    """
    output = (
        "================================= FAILURES =================================\n"
        "_______________________ [doctest] doc_test.add _________________________\n"
        "002 加算する。\n"
        "003 \n"
        "004     >>> add(1, 2)\n"
        "Expected:\n"
        "    4\n"
        "Got:\n"
        "    3\n"
        "\n"
        "doc_test.py:4: DocTestFailure\n"
        "========================= short test summary info ==========================\n"
        "FAILED doc_test.py::doc_test.add\n"
        "================================= 1 failed in 0.02s =================================\n"
    )
    errors = pyfltr.command.error_parser.parse_errors("pytest", output)
    assert len(errors) == 1
    assert errors[0].file == "doc_test.py"
    assert errors[0].line == 4
    assert errors[0].message == "doc_test.add"


def test_parse_pytest_worker_crash_class_based_not_double_counted() -> None:
    """pytest: クラスベーステストのxdistワーカークラッシュで診断が二重生成されないことを検証する。

    クラッシュ行のテスト名はpytestのnodeid表記（`TestCrash::test_method_crash`）だが、summary辞書の
    キーは`.`区切りへ正規化済みである。突合前に同じ正規化を施さないとキーが一致せず、
    summary残余補完で同一テストの診断が二重生成される。
    """
    output = (
        "================================= FAILURES =================================\n"
        "_______________________________ tests/c_test.py ________________________________\n"
        "[gw0] linux -- Python 3.14.0 /path/to/python3\n"
        "worker 'gw0' crashed while running 'tests/c_test.py::TestCrash::test_method_crash'\n"
        "_______________________________ tests/c_test.py ________________________________\n"
        "[gw1] linux -- Python 3.14.0 /path/to/python3\n"
        "worker 'gw1' crashed while running 'tests/c_test.py::test_plain_crash'\n"
        "========================= short test summary info ==========================\n"
        "FAILED tests/c_test.py::TestCrash::test_method_crash - worker 'gw0' crashed while running "
        "'tests/c_test.py::TestCrash::test_method_crash'\n"
        "FAILED tests/c_test.py::test_plain_crash - worker 'gw1' crashed while running "
        "'tests/c_test.py::test_plain_crash'\n"
    )
    errors = pyfltr.command.error_parser.parse_errors("pytest", output)
    assert len(errors) == 2
    assert all(e.file == "tests/c_test.py" for e in errors)
    assert all(e.line == 0 for e in errors)
    assert any(e.message.startswith("TestCrash::test_method_crash: ") for e in errors)
    assert any(e.message.startswith("test_plain_crash: ") for e in errors)


def test_parse_pytest_keeps_line_numbers_when_child_summary_is_captured() -> None:
    """pytest: 捕捉出力に子プロセスの集計見出しが混入しても実在失敗の行番号を失わないことを検証する。

    pytestを子プロセスとして起動し出力を取り込むテストでは、親の失敗欄の内側に子の
    `short test summary info`見出しが現れる。失敗欄の終端を先頭一致で決めると解析範囲が
    そこで打ち切られ、以降の実在失敗がブロック解析されず行番号を失う。
    """
    output = (
        "================================= FAILURES =================================\n"
        "_______________________________ test_runs_child ________________________________\n"
        "tests/d_test.py:11: in test_runs_child\n"
        "E   AssertionError: assert 1 == 0\n"
        "-------------------------- Captured stdout call ---------------------------\n"
        "========================= short test summary info ==========================\n"
        "FAILED child/z_test.py::test_child - assert 1 == 2\n"
        "_______________________________ test_later_real ________________________________\n"
        "tests/d_test.py:15: in test_later_real\n"
        "E   AssertionError: later\n"
        "========================= short test summary info ==========================\n"
        "FAILED tests/d_test.py::test_runs_child - AssertionError: assert 1 == 0\n"
        "FAILED tests/d_test.py::test_later_real - AssertionError: later\n"
        "================================= 2 failed in 0.86s =================================\n"
    )
    errors = pyfltr.command.error_parser.parse_errors("pytest", output)
    by_test = {e.message.split(":")[0]: e for e in errors}
    assert by_test["test_runs_child"].line == 11
    assert by_test["test_later_real"].line == 15


def test_parse_pytest_tb_line_external_frame_not_double_counted() -> None:
    """pytest --tb=line: 位置行がプロジェクト外パス（site-packages等）へ落ちる失敗でも、
    ファイル不一致時はメッセージのみでsummaryと突合し診断が二重生成されないことを検証する。
    """
    abs_file = pathlib.Path.cwd() / ".venv" / "lib" / "python3.13" / "site-packages" / "somelib" / "core.py"
    output = (
        "================================= FAILURES =================================\n"
        "E   RuntimeError: boom\n"
        f"{abs_file}:50: RuntimeError: boom\n"
        "========================= short test summary info ==========================\n"
        "FAILED tests/a_test.py::test_ext - RuntimeError: boom\n"
    )
    errors = pyfltr.command.error_parser.parse_errors("pytest", output)
    assert len(errors) == 1
    assert errors[0].file == ".venv/lib/python3.13/site-packages/somelib/core.py"
    assert errors[0].line == 50
    assert "RuntimeError: boom" in errors[0].message


def test_parse_pytest_tb_line_truncated_summary_message_not_double_counted() -> None:
    """pytest --tb=line: 集計行のメッセージが省略記号で切り詰められても診断が二重生成されない。

    pytestは失敗理由が端末幅に収まらないとき末尾を`...`へ置き換える。
    完全一致だけで突合すると位置行由来の診断と残余補完の診断が同一の失敗に対して並ぶ。
    """
    long_message = "json.decoder.JSONDecodeError: Expecting property name enclosed in double quotes: line 1 column 2"
    output = (
        "================================= FAILURES =================================\n"
        f"E   {long_message}\n"
        f".venv/lib/python3.13/site-packages/json/decoder.py:353: {long_message}\n"
        "E   assert 1 == 2\n"
        "tests/outer_test.py:9: assert 1 == 2\n"
        "========================= short test summary info ==========================\n"
        "FAILED tests/outer_test.py::test_long_message - json.decoder.JSONDecodeError: Expecting property...\n"
        "FAILED tests/outer_test.py::test_other - assert 1 == 2\n"
    )
    errors = pyfltr.command.error_parser.parse_errors("pytest", output)
    assert len(errors) == 2
    assert not any(e.line == 0 for e in errors)
    truncated = next(e for e in errors if e.file.endswith("json/decoder.py"))
    assert truncated.line == 353
    assert truncated.message.startswith("test_long_message: ")


def test_parse_pytest_tb_line_ambiguous_truncated_summary_is_not_matched() -> None:
    """pytest --tb=line: 切り詰め後の接頭辞が複数の失敗へ一致する場合は突合しない。

    前方一致で取り違えると片方の失敗を取りこぼすため、候補が1件に定まる場合のみ突合する。
    """
    output = (
        "================================= FAILURES =================================\n"
        "E   AssertionError: same prefix but different tail A\n"
        "tests/a_test.py:5: AssertionError: same prefix but different tail A\n"
        "========================= short test summary info ==========================\n"
        "FAILED tests/a_test.py::test_one - AssertionError: same prefix...\n"
        "FAILED tests/a_test.py::test_two - AssertionError: same prefix...\n"
    )
    errors = pyfltr.command.error_parser.parse_errors("pytest", output)
    assert len(errors) == 3
    assert sum(1 for e in errors if e.line == 0) == 2


def test_parse_pytest_same_test_name_in_different_files_not_dropped() -> None:
    """pytest --tb=short: 別ファイルの同名テストが、突合キーへのファイル込み化により
    取りこぼされず両方診断されることを検証する。
    """
    output = (
        "================================= FAILURES =================================\n"
        "_______________________________ test_boom ________________________________\n"
        "tests/a_test.py:5: in test_boom\n"
        "E   AssertionError: a\n"
        "_______________________________ test_boom ________________________________\n"
        "========================= short test summary info ==========================\n"
        "FAILED tests/a_test.py::test_boom - AssertionError: a\n"
        "FAILED tests/b_test.py::test_boom - AssertionError: b\n"
    )
    errors = pyfltr.command.error_parser.parse_errors("pytest", output)
    assert len(errors) == 2
    a_error = next(e for e in errors if e.file == "tests/a_test.py")
    assert a_error.line == 5
    b_error = next(e for e in errors if e.file == "tests/b_test.py")
    assert b_error.line == 0
    assert "test_boom" in b_error.message


def test_parse_pytest_ignores_failed_line_in_captured_output() -> None:
    """pytest: テストの捕捉出力に混入した`FAILED ...`行を実在の失敗として診断化しないことを検証する。

    pytestを子プロセス起動するテストの標準出力キャプチャに、子プロセスの
    `short test summary info`相当の`FAILED`行が含まれても、実際の見出し以降のみを
    走査対象とし誤検出しない。
    """
    output = (
        "================================= FAILURES =================================\n"
        "_______________________________ test_runs_pytest ________________________________\n"
        "tests/a_test.py:9: in test_runs_pytest\n"
        "E   AssertionError: subprocess failed\n"
        "--- Captured stdout call ---\n"
        "FAILED other/y_test.py::test_phantom - boom\n"
        "========================= short test summary info ==========================\n"
        "FAILED tests/a_test.py::test_runs_pytest - AssertionError: subprocess failed\n"
    )
    errors = pyfltr.command.error_parser.parse_errors("pytest", output)
    assert len(errors) == 1
    assert errors[0].file == "tests/a_test.py"
    assert errors[0].line == 9
    assert errors[0].message.startswith("test_runs_pytest: ")


def _pytest_child_run(*, file: str, test: str, line: int, message: str, terminated: bool = True) -> str:
    """捕捉出力へ混入する子プロセスのpytest実行1回分を組み立てる。

    実出力と同じく`test session starts`見出しで始まり、終了集計行で終わる形とする。
    除外の判定は当該2つのマーカーに依存するため、検体からいずれも省略しない。
    `terminated=False`は子プロセスが異常終了・打ち切りで終了集計行を欠く形を表す。
    """
    body = "================================= test session starts =================================\ncollected 1 item\n\n"
    if not terminated:
        return body + f"{file} \n"
    return (
        body + "================================= FAILURES =================================\n"
        f"_______________________________ {test} ________________________________\n"
        f"{file}:{line}: in {test}\n"
        f"E   {message}\n"
        "========================= short test summary info ==========================\n"
        f"FAILED {file}::{test} - {message}\n"
        "================================= 1 failed in 0.04s =================================\n"
    )


def test_parse_pytest_ignores_child_failure_block_in_captured_output() -> None:
    """pytest --tb=short: 捕捉出力に混入した子プロセスの失敗欄を診断化しないことを検証する。

    子のブロック見出しは親のものと書式が同一のため、除外は子プロセスの実行マーカーで
    判定する。子のファイルパスが診断へ現れないことを確認する。
    """
    output = (
        "================================= FAILURES =================================\n"
        "_______________________________ test_runs_child ________________________________\n"
        "tests/d_test.py:11: in test_runs_child\n"
        "E   AssertionError: assert 1 == 0\n"
        "-------------------------- Captured stdout call ---------------------------\n"
        + _pytest_child_run(file="child/z_test.py", test="test_phantom", line=2, message="assert 1 == 2")
        + "_______________________________ test_later_real ________________________________\n"
        "tests/d_test.py:15: in test_later_real\n"
        "E   AssertionError: later\n"
        "========================= short test summary info ==========================\n"
        "FAILED tests/d_test.py::test_runs_child - AssertionError: assert 1 == 0\n"
        "FAILED tests/d_test.py::test_later_real - AssertionError: later\n"
        "================================= 2 failed in 0.86s =================================\n"
    )
    errors = pyfltr.command.error_parser.parse_errors("pytest", output)
    assert len(errors) == 2
    assert all(e.file == "tests/d_test.py" for e in errors)
    by_test = {e.message.split(":")[0]: e for e in errors}
    assert by_test["test_runs_child"].line == 11
    assert by_test["test_later_real"].line == 15


def test_parse_pytest_ignores_child_failure_block_with_same_test_name() -> None:
    """pytest: 親子で同名のテストが失敗しても子の失敗欄を診断化しないことを検証する。

    テスト名の突合で除外の終端を決めると親子同名の構成で架空の診断が残るため、
    除外は子プロセスの実行マーカーで判定する。
    """
    output = (
        "================================= FAILURES =================================\n"
        "_______________________________ test_runs_child ________________________________\n"
        "tests/d_test.py:11: in test_runs_child\n"
        "E   AssertionError: assert 1 == 0\n"
        "-------------------------- Captured stdout call ---------------------------\n"
        + _pytest_child_run(file="child/z_test.py", test="test_dup", line=2, message="assert 1 == 2")
        + "_______________________________ test_dup ________________________________\n"
        "tests/d_test.py:15: in test_dup\n"
        "E   AssertionError: parent dup\n"
        "========================= short test summary info ==========================\n"
        "FAILED tests/d_test.py::test_runs_child - AssertionError: assert 1 == 0\n"
        "FAILED tests/d_test.py::test_dup - AssertionError: parent dup\n"
        "================================= 2 failed in 0.86s =================================\n"
    )
    errors = pyfltr.command.error_parser.parse_errors("pytest", output)
    assert len(errors) == 2
    assert all(e.file == "tests/d_test.py" for e in errors)
    by_test = {e.message.split(":")[0]: e for e in errors}
    assert by_test["test_runs_child"].line == 11
    assert by_test["test_dup"].line == 15


def test_parse_pytest_ignores_child_failure_block_without_parent_summary() -> None:
    """pytest: 親が集計行を持たず子だけが持つ場合に子の集計行を採用しないことを検証する。

    集計行の走査は出力の末尾側の見出しを起点とするため、除外しないと子の集計行を
    親のものとして採用し、親の失敗を取りこぼす。
    """
    output = (
        "================================= FAILURES =================================\n"
        "_______________________________ test_runs_child ________________________________\n"
        "tests/d_test.py:11: in test_runs_child\n"
        "E   AssertionError: assert 1 == 0\n"
        "-------------------------- Captured stdout call ---------------------------\n"
        + _pytest_child_run(file="child/z_test.py", test="test_dup", line=2, message="assert 1 == 2")
        + "_______________________________ test_dup ________________________________\n"
        "tests/d_test.py:15: in test_dup\n"
        "E   AssertionError: parent dup\n"
        "================================= 2 failed in 1.32s =================================\n"
    )
    errors = pyfltr.command.error_parser.parse_errors("pytest", output)
    assert len(errors) == 2
    assert all(e.file == "tests/d_test.py" for e in errors)
    by_test = {e.message.split(":")[0]: e for e in errors}
    assert by_test["test_runs_child"].line == 11
    assert by_test["test_dup"].line == 15


def test_parse_pytest_tb_line_ignores_child_failure_block_in_captured_output() -> None:
    """pytest --tb=line: 捕捉出力に子の失敗欄が混入しても架空の診断を生成しないことを検証する。

    親が`--tb=line`形式でもブロック見出しを持たないだけで捕捉出力の混入は起こる。
    子のファイルパスが診断へ現れず、後続の実在失敗が行番号を保つことを確認する。
    """
    output = (
        "================================= FAILURES =================================\n"
        "tests/d_test.py:11: AssertionError: assert 1 == 0\n"
        "-------------------------- Captured stdout call ---------------------------\n"
        + _pytest_child_run(file="child/z_test.py", test="test_phantom", line=2, message="assert 1 == 2")
        + "tests/d_test.py:15: AssertionError: later\n"
        "========================= short test summary info ==========================\n"
        "FAILED tests/d_test.py::test_runs_child - AssertionError: assert 1 == 0\n"
        "FAILED tests/d_test.py::test_later_real - AssertionError: later\n"
        "================================= 2 failed in 0.86s =================================\n"
    )
    errors = pyfltr.command.error_parser.parse_errors("pytest", output)
    assert len(errors) == 2
    assert all(e.file == "tests/d_test.py" for e in errors)
    by_test = {e.message.split(":")[0]: e for e in errors}
    assert by_test["test_runs_child"].line == 11
    assert by_test["test_later_real"].line == 15


def test_parse_pytest_tb_line_captured_output_keeps_following_line_numbers() -> None:
    """pytest --tb=line: 子プロセスを含まない捕捉出力で後続の行番号を失わないことを検証する。

    `--tb=line`形式では位置行が当該テストの捕捉出力より後に現れる。
    子プロセスの実行を含まない捕捉出力を除外対象にすると、失敗した本人の位置行まで失う。
    """
    output = (
        "================================= FAILURES =================================\n"
        "E   assert 1 == 0\n"
        "-------------------------- Captured stdout call ---------------------------\n"
        "noise\n"
        "tests/aa_test.py:3: assert 1 == 0\n"
        "E   assert 2 == 0\n"
        "tests/aa_test.py:7: assert 2 == 0\n"
        "========================= short test summary info ==========================\n"
        "FAILED tests/aa_test.py::test_1 - assert 1 == 0\n"
        "FAILED tests/aa_test.py::test_2 - assert 2 == 0\n"
    )
    errors = pyfltr.command.error_parser.parse_errors("pytest", output)
    assert len(errors) == 2
    by_test = {e.message.split(":")[0]: e for e in errors}
    assert by_test["test_1"].line == 3
    assert by_test["test_2"].line == 7


def test_parse_pytest_worker_crash_after_captured_output_keeps_message() -> None:
    """pytest -n: 捕捉出力の後に続くワーカー異常終了のブロックを失わないことを検証する。

    当該ブロックの見出しはテスト名ではなくファイルパスであり、テスト名の突合では
    除外の終端を判定できない。除外を子プロセスの実行マーカーで判定することで
    失敗理由の全文が保持される。
    """
    output = (
        "================================= FAILURES =================================\n"
        "_______________________ test_prints_then_fails ________________________\n"
        "cr/crash_test.py:7: in test_prints_then_fails\n"
        "E   assert 1 == 0\n"
        "-------------------------- Captured stdout call ---------------------------\n"
        "noise\n"
        "_______________________________ cr/crash_test.py ________________________________\n"
        "worker 'gw1' crashed while running 'cr/crash_test.py::test_func_crash'\n"
        "========================= short test summary info ==========================\n"
        "FAILED cr/crash_test.py::test_prints_then_fails - assert 1 == 0\n"
        "FAILED cr/crash_test.py::test_func_crash - worker 'gw1' crashed\n"
    )
    errors = pyfltr.command.error_parser.parse_errors("pytest", output)
    assert len(errors) == 2
    crashed = next(e for e in errors if e.line == 0)
    assert "worker 'gw1' crashed while running cr/crash_test.py::test_func_crash" in crashed.message


def test_parse_pytest_captured_section_without_child_run_keeps_following_block() -> None:
    """pytest: 子プロセスの実行を含まない捕捉出力では後続ブロックを除外しないことを検証する。

    除外の開始は捕捉出力の節見出しだけでは決まらず、子プロセスの実行開始行を伴う場合に限る。
    """
    output = (
        "================================= FAILURES =================================\n"
        "_______________________________ test_a ________________________________\n"
        "tests/a_test.py:3: in test_a\n"
        "E   AssertionError: a\n"
        "-------------------------- Captured stdout call ---------------------------\n"
        "hello\n"
        "_______________________________ test_b ________________________________\n"
        "tests/a_test.py:7: in test_b\n"
        "E   AssertionError: b\n"
    )
    errors = pyfltr.command.error_parser.parse_errors("pytest", output)
    assert len(errors) == 2
    by_test = {e.message.split(":")[0]: e for e in errors}
    assert by_test["test_a"].line == 3
    assert by_test["test_b"].line == 7


def test_parse_pytest_child_run_without_summary_line_keeps_following_failures() -> None:
    """pytest: 子プロセスが集計行を出さずに終わっても親の後続の失敗を失わないことを検証する。

    子プロセスが異常終了・打ち切りで集計行を欠くと、除外の終端として親自身の最終集計行を
    採りかねない。その間にある親の失敗と失敗一覧を失わないことを確認する。
    """
    output = (
        "================================= FAILURES =================================\n"
        "_______________________________ test_runs_child ________________________________\n"
        "tests/c_test.py:11: in test_runs_child\n"
        "E   AssertionError: assert 1 == 0\n"
        "-------------------------- Captured stdout call ---------------------------\n"
        "================================= test session starts =================================\n"
        "collected 1 item\n"
        "\n"
        "child/z_test.py \n"
        "_______________________________ test_normal_after ________________________________\n"
        "tests/c_test.py:15: in test_normal_after\n"
        "E   AssertionError: after\n"
        "========================= short test summary info ==========================\n"
        "FAILED tests/c_test.py::test_runs_child - AssertionError: assert 1 == 0\n"
        "FAILED tests/c_test.py::test_normal_after - AssertionError: after\n"
        "================================= 2 failed in 0.75s =================================\n"
    )
    errors = pyfltr.command.error_parser.parse_errors("pytest", output)
    assert len(errors) == 2
    by_test = {e.message.split(":")[0]: e for e in errors}
    assert by_test["test_runs_child"].line == 11
    assert by_test["test_normal_after"].line == 15


def test_parse_pytest_tb_line_child_run_without_summary_line_keeps_line_numbers() -> None:
    """pytest --tb=line: 子プロセスが集計行を欠く場合に後続の行番号を失わないことを検証する。

    `--tb=line`形式では位置行が捕捉出力より後に現れるため、除外範囲の誤延長が
    行番号の消失として現れる。
    """
    output = (
        "================================= FAILURES =================================\n"
        "E   AssertionError: assert 1 == 0\n"
        "-------------------------- Captured stdout call ---------------------------\n"
        "================================= test session starts =================================\n"
        "collected 1 item\n"
        "\n"
        "child/z_test.py \n"
        "tests/c_test.py:11: AssertionError: assert 1 == 0\n"
        "E   AssertionError: after\n"
        "tests/c_test.py:15: AssertionError: after\n"
        "========================= short test summary info ==========================\n"
        "FAILED tests/c_test.py::test_runs_child - AssertionError: assert 1 == 0\n"
        "FAILED tests/c_test.py::test_normal_after - AssertionError: after\n"
        "================================= 2 failed in 0.75s =================================\n"
    )
    errors = pyfltr.command.error_parser.parse_errors("pytest", output)
    assert len(errors) == 2
    by_test = {e.message.split(":")[0]: e for e in errors}
    assert by_test["test_runs_child"].line == 11
    assert by_test["test_normal_after"].line == 15


def _pytest_two_child_runs_output(*, tb_line: bool, summary_list: bool) -> str:
    """子プロセスの実行が2回現れ、1回目が終了集計行を欠く形の出力を組み立てる。

    1回目の実行の開始位置を保持したままにすると2回目の終了集計行と対になり、
    その間にある親の失敗まで除外される。当該構成の検体を共通化する。
    """
    crashed = _pytest_child_run(file="child/dies_test.py", test="test_dies", line=0, message="", terminated=False)
    completed = _pytest_child_run(file="child/fails_test.py", test="test_fails", line=2, message="assert 1 == 2")
    captured = "-------------------------- Captured stdout call ---------------------------\n"
    if tb_line:
        failures = (
            "E   AssertionError: assert 1 == 0\n" + captured + crashed + "tests/c_test.py:15: AssertionError: crashed\n"
            "E   AssertionError: plain\n"
            "tests/c_test.py:19: AssertionError: plain\n"
            "E   AssertionError: assert 1 == 0\n" + captured + completed + "tests/c_test.py:23: AssertionError: failed\n"
        )
    else:
        failures = (
            "_______________________________ test_a_child_crashes ________________________________\n"
            "tests/c_test.py:15: in test_a_child_crashes\n"
            "E   AssertionError: crashed\n"
            + captured
            + crashed
            + "_______________________________ test_b_child_normal ________________________________\n"
            "tests/c_test.py:19: in test_b_child_normal\n"
            "E   AssertionError: plain\n"
            "_______________________________ test_c_child_fails ________________________________\n"
            "tests/c_test.py:23: in test_c_child_fails\n"
            "E   AssertionError: failed\n" + captured + completed
        )
    summary = (
        "========================= short test summary info ==========================\n"
        "FAILED tests/c_test.py::test_a_child_crashes - AssertionError: crashed\n"
        "FAILED tests/c_test.py::test_b_child_normal - AssertionError: plain\n"
        "FAILED tests/c_test.py::test_c_child_fails - AssertionError: failed\n"
    )
    return (
        "================================= FAILURES =================================\n"
        + failures
        + (summary if summary_list else "")
        + "================================= 3 failed in 1.20s =================================\n"
    )


def test_parse_pytest_unterminated_child_run_does_not_extend_to_next_child_run() -> None:
    """pytest: 集計行を欠く子の実行が後続の子の実行まで除外範囲を延ばさないことを検証する。

    終了集計行を欠いた実行の開始位置を保持したままにすると、後続の別の実行の
    終了集計行と対になり、その間にある親の失敗まで除外する。
    """
    errors = pyfltr.command.error_parser.parse_errors("pytest", _pytest_two_child_runs_output(tb_line=False, summary_list=True))
    assert len(errors) == 3
    assert all(e.file == "tests/c_test.py" for e in errors)
    by_test = {e.message.split(":")[0]: e for e in errors}
    assert by_test["test_a_child_crashes"].line == 15
    assert by_test["test_b_child_normal"].line == 19
    assert by_test["test_c_child_fails"].line == 23


def test_parse_pytest_tb_line_unterminated_child_run_does_not_extend_to_next_child_run() -> None:
    """pytest --tb=line: 同じ構成で位置行を失わないことを検証する。

    `--tb=line`形式では位置行が捕捉出力より後に現れるため、除外範囲の誤延長が
    行番号の消失として現れる。
    """
    errors = pyfltr.command.error_parser.parse_errors("pytest", _pytest_two_child_runs_output(tb_line=True, summary_list=True))
    assert len(errors) == 3
    assert all(e.file == "tests/c_test.py" for e in errors)
    assert {e.line for e in errors} == {15, 19, 23}


def test_parse_pytest_unterminated_child_run_without_summary_list_keeps_failures() -> None:
    """pytest: 同じ構成で失敗一覧を欠く場合に失敗が消えないことを検証する。

    失敗一覧が無い構成では残余補完による救済が働かないため、除外範囲の誤延長が
    失敗そのものの消失として現れる。
    """
    errors = pyfltr.command.error_parser.parse_errors(
        "pytest", _pytest_two_child_runs_output(tb_line=False, summary_list=False)
    )
    assert len(errors) == 3
    assert all(e.file == "tests/c_test.py" for e in errors)
    assert {e.line for e in errors} == {15, 19, 23}


def test_parse_pytest_two_complete_child_runs_are_masked_independently() -> None:
    """pytest: 完結した子プロセスの実行が2回続く場合に双方を除外することを検証する。"""
    first = _pytest_child_run(file="child/a_test.py", test="test_a", line=2, message="assert 1 == 2")
    second = _pytest_child_run(file="child/b_test.py", test="test_b", line=3, message="assert 2 == 3")
    captured = "-------------------------- Captured stdout call ---------------------------\n"
    output = (
        "================================= FAILURES =================================\n"
        "_______________________________ test_first ________________________________\n"
        "tests/c_test.py:11: in test_first\n"
        "E   AssertionError: first\n"
        + captured
        + first
        + "_______________________________ test_second ________________________________\n"
        "tests/c_test.py:15: in test_second\n"
        "E   AssertionError: second\n"
        + captured
        + second
        + "========================= short test summary info ==========================\n"
        "FAILED tests/c_test.py::test_first - AssertionError: first\n"
        "FAILED tests/c_test.py::test_second - AssertionError: second\n"
        "================================= 2 failed in 0.90s =================================\n"
    )
    errors = pyfltr.command.error_parser.parse_errors("pytest", output)
    assert len(errors) == 2
    assert all(e.file == "tests/c_test.py" for e in errors)
    assert {e.line for e in errors} == {11, 15}


def _pytest_unterminated_then_tail_output(*, summary_list: bool) -> str:
    """未終端の子の実行の後に、開始行を持たない終了集計行が現れる形の出力を組み立てる。

    親テストが子の出力の末尾だけを表示した場合に生じる。未終端の実行の開始位置を
    節をまたいで保持すると、当該終了集計行と対になり間の親の失敗を除外する。
    """
    captured = "-------------------------- Captured stdout call ---------------------------\n"
    unterminated = "================================= test session starts =================================\ncollected 1 item\n"
    tail_only = (
        "FAILED child/y_test.py::test_y - assert 1 == 2\n"
        "================================= 1 failed in 0.03s =================================\n"
    )
    summary = (
        "========================= short test summary info ==========================\n"
        "FAILED tests/c_test.py::test_a_unterminated - AssertionError: a\n"
        "FAILED tests/c_test.py::test_b_plain - AssertionError: b\n"
        "FAILED tests/c_test.py::test_c_tail_only - AssertionError: c\n"
    )
    return (
        "================================= FAILURES =================================\n"
        "_______________________________ test_a_unterminated ________________________________\n"
        "tests/c_test.py:12: in test_a_unterminated\n"
        "E   AssertionError: a\n"
        + captured
        + unterminated
        + "_______________________________ test_b_plain ________________________________\n"
        "tests/c_test.py:16: in test_b_plain\n"
        "E   AssertionError: b\n"
        "_______________________________ test_c_tail_only ________________________________\n"
        "tests/c_test.py:25: in test_c_tail_only\n"
        "E   AssertionError: c\n"
        + captured
        + tail_only
        + (summary if summary_list else "")
        + "================================= 3 failed in 1.10s =================================\n"
    )


def test_parse_pytest_unterminated_child_run_does_not_pair_with_later_section() -> None:
    """pytest: 未終端の子の実行が別の節の終了集計行と対にならないことを検証する。

    捕捉出力の節をまたいで開始位置を保持すると、間にある親の失敗を除外する。
    """
    errors = pyfltr.command.error_parser.parse_errors("pytest", _pytest_unterminated_then_tail_output(summary_list=True))
    assert len(errors) == 3
    assert all(e.file == "tests/c_test.py" for e in errors)
    assert {e.line for e in errors} == {12, 16, 25}


def test_parse_pytest_unterminated_child_run_without_summary_list_does_not_pair() -> None:
    """pytest: 同じ構成で失敗一覧を欠く場合に失敗が消えないことを検証する。

    失敗一覧が無い構成では残余補完による救済が働かないため、除外範囲の誤延長が
    失敗そのものの消失として現れる。
    """
    errors = pyfltr.command.error_parser.parse_errors("pytest", _pytest_unterminated_then_tail_output(summary_list=False))
    parent_errors = [e for e in errors if e.file == "tests/c_test.py"]
    assert {e.line for e in parent_errors} == {12, 16, 25}


def test_parse_pytest_child_run_masked_for_stderr_and_log_sections() -> None:
    """pytest: 標準エラー・ログの捕捉出力でも子の実行を除外することを検証する。

    除外の開始判定は節の種別に依存しない。
    """
    child = _pytest_child_run(file="child/z_test.py", test="test_phantom", line=2, message="assert 1 == 2")
    for section in ("Captured stderr call", "Captured log call"):
        output = (
            "================================= FAILURES =================================\n"
            "_______________________________ test_runs_child ________________________________\n"
            "tests/d_test.py:11: in test_runs_child\n"
            "E   AssertionError: assert 1 == 0\n"
            f"-------------------------- {section} ---------------------------\n"
            + child
            + "========================= short test summary info ==========================\n"
            "FAILED tests/d_test.py::test_runs_child - AssertionError: assert 1 == 0\n"
            "================================= 1 failed in 0.50s =================================\n"
        )
        errors = pyfltr.command.error_parser.parse_errors("pytest", output)
        assert len(errors) == 1, section
        assert errors[0].file == "tests/d_test.py", section
        assert errors[0].line == 11, section


@pytest.mark.parametrize(
    "tail_line",
    [
        "2 failed in 0.92s",
        "1 failed, 2 passed, 1 skipped, 1 xfailed, 1 xpassed, 1 warning in 0.06s",
        "no tests ran in 0.00s",
        "3 passed in 61.00s (0:01:01)",
        "3 passed in 90061.00s (1 day, 1:01:01)",
    ],
)
def test_parse_pytest_child_run_masked_with_quiet_parent(tail_line: str) -> None:
    """pytest -q: 親の最終集計行が`=`の埋めを伴わない場合でも子の実行を除外することを検証する。

    `=`の埋めを伴う形式だけを上限として探すと、出力中で最後に一致するのが捕捉出力へ
    混入した子の集計行となり、上限が子の終端そのものを指して除外が成立しない。
    集計行の分類の連結・件数を持たない形・経過時間の併記の各形をpytestの実出力から採る。
    """
    output = (
        "================================= FAILURES =================================\n"
        "_______________________________ test_runs_child ________________________________\n"
        "tests/q_test.py:11: in test_runs_child\n"
        "E   AssertionError: assert 1 == 0\n"
        "-------------------------- Captured stdout call ---------------------------\n"
        + _pytest_child_run(file="child/inner_test.py", test="test_inner", line=2, message="assert 1 == 2")
        + "_______________________________ test_after ________________________________\n"
        "tests/q_test.py:15: in test_after\n"
        "E   AssertionError: after\n"
        "========================= short test summary info ==========================\n"
        "FAILED tests/q_test.py::test_runs_child - AssertionError: assert 1 == 0\n"
        "FAILED tests/q_test.py::test_after - AssertionError: after\n"
        f"{tail_line}\n"
    )
    errors = pyfltr.command.error_parser.parse_errors("pytest", output)
    assert len(errors) == 2
    assert all(e.file == "tests/q_test.py" for e in errors)
    assert {e.line for e in errors} == {11, 15}


def test_parse_pytest_captured_text_is_not_taken_as_parent_summary_line() -> None:
    """pytest: 集計行に似た任意のテキスト行を除外範囲の上限として採らないことを検証する。

    上限が親の集計行を越えると、終端を欠いた子の実行が親自身の最終集計行と対になり、
    その間の親の失敗と失敗一覧をまとめて除外する。
    """
    output = (
        "================================= FAILURES =================================\n"
        "_______________________________ test_p ________________________________\n"
        "tests/p_test.py:3: in test_p\n"
        "E   assert 1 == 2\n"
        "-------------------------- Captured stdout call ---------------------------\n"
        "================================= test session starts =================================\n"
        "collected 1 item\n"
        "_______________________________ test_q ________________________________\n"
        "tests/p_test.py:7: in test_q\n"
        "E   assert 5 == 6\n"
        "========================= short test summary info ==========================\n"
        "FAILED tests/p_test.py::test_p - assert 1 == 2\n"
        "FAILED tests/p_test.py::test_q - assert 5 == 6\n"
        "================================= 2 failed in 0.30s =================================\n"
        "12 files processed in 2.5s\n"
    )
    errors = pyfltr.command.error_parser.parse_errors("pytest", output)
    assert len(errors) == 2
    assert {e.line for e in errors} == {3, 7}


def test_mask_pytest_captured_child_runs_keeps_line_structure() -> None:
    """pytest: 除外処理が`\\n`以外の制御文字で行を切らないことを検証する。

    `str.splitlines`はフォームフィード等でも分割するため、除外時に当該位置へ改行が入り、
    以降の正規表現探索（`re.MULTILINE`は`\\n`のみを行区切りとする）と行の対応が崩れる。
    制御文字は除外対象の範囲の内側へ置く。範囲の外では除外時の置換が起こらず、
    分割の基準が違っても元の文字列が復元されるため、退行を検知できない。
    """
    child = _pytest_child_run(file="child/z_test.py", test="test_phantom", line=2, message="assert 1 == 2").replace(
        "collected 1 item\n", "collected 1 item\nbefore\x0cafter\n"
    )
    output = (
        "================================= FAILURES =================================\n"
        "_______________________________ test_p ________________________________\n"
        "tests/p_test.py:3: in test_p\n"
        "E   assert 1 == 2\n"
        "-------------------------- Captured stdout call ---------------------------\n"
        + child
        + "========================= short test summary info ==========================\n"
        "FAILED tests/p_test.py::test_p - assert 1 == 2\n"
        "================================= 1 failed in 0.30s =================================\n"
    )
    # pylint: disable=protected-access  # 行構造の保持は公開関数の戻り値へ現れないため直接検証する
    masked = pyfltr.command.error_parser._mask_pytest_captured_child_runs(output)  # noqa: SLF001
    assert masked.count("\n") == output.count("\n")
    assert len(masked.split("\n")) == len(output.split("\n"))


def test_parse_pytest_child_run_masked_without_parent_failures_section() -> None:
    """pytest: 親に失敗欄が無く捕捉出力へ子の実行だけが現れる場合に除外することを検証する。

    親が全通過し通過テストの捕捉出力だけが表示される構成では、失敗欄を持たないため
    集計行のみを情報源とする経路へ入る。除外しないと子の失敗が親の失敗として報告される。
    """
    output = (
        "-------------------------- Captured stdout call ---------------------------\n"
        + _pytest_child_run(file="child/inner_test.py", test="test_inner", line=2, message="assert 1 == 2")
        + "================================= 2 passed in 0.90s =================================\n"
    )
    errors = pyfltr.command.error_parser.parse_errors("pytest", output)
    assert errors == []


def test_parse_pytest_child_run_is_not_masked_without_parent_summary_line() -> None:
    """pytest: 親が最終集計行を持たない出力では子の実行を除外しないことを検証する。

    除外範囲の上限を確定できないため、親の失敗を巻き込まない側へ縮退する。
    """
    output = (
        "================================= FAILURES =================================\n"
        "_______________________________ test_runs_child ________________________________\n"
        "tests/d_test.py:11: in test_runs_child\n"
        "E   AssertionError: assert 1 == 0\n"
        "-------------------------- Captured stdout call ---------------------------\n"
        + _pytest_child_run(file="child/z_test.py", test="test_phantom", line=2, message="assert 1 == 2")
    )
    errors = pyfltr.command.error_parser.parse_errors("pytest", output)
    assert next(e for e in errors if e.file == "tests/d_test.py").line == 11
    assert any(e.file == "child/z_test.py" for e in errors)


def test_parse_pytest_child_run_with_nested_captured_section_is_not_diagnosed() -> None:
    """pytest: 子の失敗したテストが出力を持つ場合に子の失敗を診断化しないことを検証する。

    子自身の捕捉出力の節見出しが親の節見出しと同一書式で現れるため、実行マーカーによる
    除外は終端を確定できず成立しない。親の失敗一覧に載らないテスト名の失敗欄を除外する
    安全網により架空の診断を防ぐ。子プロセスの失敗したテストが標準出力へ書き込む構成で
    日常的に生じる。
    """
    child = (
        "================================= test session starts =================================\n"
        "collected 1 item\n"
        "\n"
        "================================= FAILURES =================================\n"
        "_______________________________ test_inner ________________________________\n"
        "child/inner_test.py:3: in test_inner\n"
        "E   assert 1 == 2\n"
        "-------------------------- Captured stdout call ---------------------------\n"
        "inner noise\n"
        "========================= short test summary info ==========================\n"
        "FAILED child/inner_test.py::test_inner - assert 1 == 2\n"
        "================================= 1 failed in 0.04s =================================\n"
    )
    output = (
        "================================= FAILURES =================================\n"
        "_______________________________ test_runs_child ________________________________\n"
        "tests/p_test.py:11: in test_runs_child\n"
        "E   AssertionError: assert 1 == 0\n"
        "-------------------------- Captured stdout call ---------------------------\n"
        + child
        + "_______________________________ test_after ________________________________\n"
        "tests/p_test.py:15: in test_after\n"
        "E   AssertionError: after\n"
        "========================= short test summary info ==========================\n"
        "FAILED tests/p_test.py::test_runs_child - AssertionError: assert 1 == 0\n"
        "FAILED tests/p_test.py::test_after - AssertionError: after\n"
        "================================= 2 failed in 0.90s =================================\n"
    )
    errors = pyfltr.command.error_parser.parse_errors("pytest", output)
    assert len(errors) == 2
    assert all(e.file == "tests/p_test.py" for e in errors)
    assert {e.line for e in errors} == {11, 15}


def test_parse_pytest_unterminated_child_run_with_failures_section_keeps_parent_lines() -> None:
    """pytest: 子自身の失敗欄を含む未終端の実行が親のブロックを巻き込まないことを検証する。

    子の失敗欄より後の節見出しを開始位置の破棄から除くと、終端未確定の開始位置が
    親のブロック境界を越えて生き残り、別の親のテストが表示した子の出力の末尾にある
    終了集計行と対になる。その間にある親の失敗が行番号を失う。
    """
    captured = "-------------------------- Captured stdout call ---------------------------\n"
    unterminated = (
        "================================= test session starts =================================\n"
        "collected 1 item\n"
        "================================= FAILURES =================================\n"
        "_______________________________ test_inner ________________________________\n"
        "child/inner_test.py:3: in test_inner\n"
        "E   assert 1 == 2\n"
        "-------------------------- Captured stdout call ---------------------------\n"
        "inner noise\n"
    )
    tail_only = (
        "FAILED child/y_test.py::test_y - assert 1 == 2\n"
        "================================= 1 failed in 0.03s =================================\n"
    )
    output = (
        "================================= FAILURES =================================\n"
        "_______________________________ test_a_child ________________________________\n"
        "tests/c_test.py:12: in test_a_child\n"
        "E   AssertionError: a\n"
        + captured
        + unterminated
        + "_______________________________ test_b_plain ________________________________\n"
        "tests/c_test.py:18: in test_b_plain\n"
        "E   AssertionError: b\n"
        "_______________________________ test_c_tail ________________________________\n"
        "tests/c_test.py:24: in test_c_tail\n"
        "E   AssertionError: c\n"
        + captured
        + tail_only
        + "========================= short test summary info ==========================\n"
        "FAILED tests/c_test.py::test_a_child - AssertionError: a\n"
        "FAILED tests/c_test.py::test_b_plain - AssertionError: b\n"
        "FAILED tests/c_test.py::test_c_tail - AssertionError: c\n"
        "================================= 3 failed in 1.10s =================================\n"
    )
    errors = pyfltr.command.error_parser.parse_errors("pytest", output)
    assert len(errors) == 3
    assert all(e.file == "tests/c_test.py" for e in errors)
    assert {e.line for e in errors} == {12, 18, 24}


def test_parse_pytest_quiet_child_failure_block_is_dropped_by_summary_safety_net() -> None:
    """pytest: 実行マーカーで除外できない子の失敗欄を失敗一覧との突合で除外することを検証する。

    子プロセスを`-q`で起動すると実行開始行が出ず、終了集計行も`=`の埋めを伴わないため
    実行マーカーによる除外が成立しない。親の失敗一覧に載らないテスト名の失敗欄を除外する
    安全網により、子プロセス側の失敗が親の失敗として報告されないことを確認する。
    """
    output = (
        "================================= FAILURES =================================\n"
        "_______________________________ test_runs_child ________________________________\n"
        "tests/p_test.py:11: in test_runs_child\n"
        "E   AssertionError: assert 1 == 0\n"
        "-------------------------- Captured stdout call ---------------------------\n"
        "================================= FAILURES =================================\n"
        "_______________________________ test_inner ________________________________\n"
        "child/inner_test.py:3: in test_inner\n"
        "E   assert 1 == 2\n"
        "========================= short test summary info ==========================\n"
        "FAILED child/inner_test.py::test_inner - assert 1 == 2\n"
        "1 failed in 0.03s\n"
        "_______________________________ test_after ________________________________\n"
        "tests/p_test.py:15: in test_after\n"
        "E   AssertionError: after\n"
        "========================= short test summary info ==========================\n"
        "FAILED tests/p_test.py::test_runs_child - AssertionError: assert 1 == 0\n"
        "FAILED tests/p_test.py::test_after - AssertionError: after\n"
        "================================= 2 failed in 0.90s =================================\n"
    )
    errors = pyfltr.command.error_parser.parse_errors("pytest", output)
    assert len(errors) == 2
    assert all(e.file == "tests/p_test.py" for e in errors)
    assert {e.line for e in errors} == {11, 15}


def test_parse_pytest_quiet_child_crash_block_is_dropped_by_summary_safety_net() -> None:
    """pytest: 子のワーカー異常終了のブロックも失敗一覧との突合で除外することを検証する。

    当該ブロックの見出しはテスト名ではなくファイルパスのため、突合には異常終了行の
    テスト名を用いる。親の失敗一覧に載らない場合は親の失敗ではない。
    """
    output = (
        "================================= FAILURES =================================\n"
        "_______________________________ test_runs_child ________________________________\n"
        "tests/p_test.py:11: in test_runs_child\n"
        "E   AssertionError: assert 1 == 0\n"
        "-------------------------- Captured stdout call ---------------------------\n"
        "_______________________________ child/inner_test.py ________________________________\n"
        "worker 'gw0' crashed while running 'child/inner_test.py::test_inner'\n"
        "========================= short test summary info ==========================\n"
        "FAILED tests/p_test.py::test_runs_child - AssertionError: assert 1 == 0\n"
        "================================= 1 failed in 0.90s =================================\n"
    )
    errors = pyfltr.command.error_parser.parse_errors("pytest", output)
    assert len(errors) == 1
    assert errors[0].file == "tests/p_test.py"
    assert errors[0].line == 11


def test_parse_pytest_quiet_child_with_same_test_name_is_not_dropped() -> None:
    """pytest: 親子で同名のテストが失敗する場合に安全網が働かない現状を仕様として固定する。

    安全網はテスト名のみで親の失敗かを判定する。子のテスト名が親の失敗一覧の
    テスト名と一致する構成では判定できず、実行マーカーによる除外も成立しないため
    子プロセス側の失敗が残る。除外の判定条件を変える際に影響範囲を明示する。
    """
    output = (
        "================================= FAILURES =================================\n"
        "_______________________________ test_runs_child ________________________________\n"
        "tests/p_test.py:11: in test_runs_child\n"
        "E   AssertionError: assert 1 == 0\n"
        "-------------------------- Captured stdout call ---------------------------\n"
        "================================= FAILURES =================================\n"
        "_______________________________ test_dup ________________________________\n"
        "child/inner_test.py:3: in test_dup\n"
        "E   assert 1 == 2\n"
        "1 failed in 0.03s\n"
        "_______________________________ test_dup ________________________________\n"
        "tests/p_test.py:15: in test_dup\n"
        "E   AssertionError: parent dup\n"
        "========================= short test summary info ==========================\n"
        "FAILED tests/p_test.py::test_runs_child - AssertionError: assert 1 == 0\n"
        "FAILED tests/p_test.py::test_dup - AssertionError: parent dup\n"
        "================================= 2 failed in 0.90s =================================\n"
    )
    errors = pyfltr.command.error_parser.parse_errors("pytest", output)
    parent_errors = [e for e in errors if e.file == "tests/p_test.py"]
    assert {e.line for e in parent_errors} == {11, 15}
    assert any(e.file == "child/inner_test.py" for e in errors)


def test_parse_pytest_unterminated_child_run_is_not_masked() -> None:
    """pytest: 子プロセスの出力が途中で欠けている場合に除外しないことを検証する。

    終了集計行を欠く領域を除外すると、以降の親の失敗をすべて失う。
    安全側へ倒し、当該領域は除外しない。
    """
    output = (
        "================================= FAILURES =================================\n"
        "_______________________________ test_runs_child ________________________________\n"
        "tests/d_test.py:11: in test_runs_child\n"
        "E   AssertionError: assert 1 == 0\n"
        "-------------------------- Captured stdout call ---------------------------\n"
        "================================= test session starts =================================\n"
        "collected 1 item\n"
        "_______________________________ test_later_real ________________________________\n"
        "tests/d_test.py:15: in test_later_real\n"
        "E   AssertionError: later\n"
        "========================= short test summary info ==========================\n"
        "FAILED tests/d_test.py::test_runs_child - AssertionError: assert 1 == 0\n"
        "FAILED tests/d_test.py::test_later_real - AssertionError: later\n"
        "================================= 2 failed in 0.86s =================================\n"
    )
    errors = pyfltr.command.error_parser.parse_errors("pytest", output)
    assert len(errors) == 2
    by_test = {e.message.split(":")[0]: e for e in errors}
    assert by_test["test_runs_child"].line == 11
    assert by_test["test_later_real"].line == 15


def test_parse_pytest_quiet_child_summary_is_not_taken_as_parent_summary() -> None:
    """pytest: 親が失敗一覧を持たない構成で子の失敗一覧を親のものとして採らないことを検証する。

    親が失敗一覧を出力しない構成（`-rN`等）で子プロセスを`-q`で起動して出力を取り込むと、
    出力中で最後に現れる失敗一覧の見出しが子のものになる。見出しを無条件に採ると失敗欄の
    解析範囲がそこで打ち切られ、以降にある親の実在する失敗が診断から消える。あわせて
    子の失敗一覧が親のものとして扱われ、子の失敗が親の失敗として報告される。
    検体はpytest 9.1.1で親を`-rN --tb=short`、子を`-q`で起動した実出力の構造から採る。
    """
    output = (
        "================================= FAILURES =================================\n"
        "_______________________________ test_runs_child ________________________________\n"
        "tests/p_test.py:13: in test_runs_child\n"
        "E   AssertionError: assert 1 == 0\n"
        "-------------------------- Captured stdout call ---------------------------\n"
        "================================= FAILURES =================================\n"
        "_______________________________ test_inner ________________________________\n"
        "child/inner_test.py:3: in test_inner\n"
        "E   assert 1 == 2\n"
        "========================= short test summary info ==========================\n"
        "FAILED child/inner_test.py::test_inner - assert 1 == 2\n"
        "1 failed in 0.03s\n"
        "_______________________________ test_parent_later ________________________________\n"
        "tests/p_test.py:17: in test_parent_later\n"
        "E   AssertionError: assert 'a' == 'b'\n"
        "================================= 2 failed in 0.89s =================================\n"
    )
    errors = pyfltr.command.error_parser.parse_errors("pytest", output)
    parent_errors = [e for e in errors if e.file == "tests/p_test.py"]
    assert {e.line for e in parent_errors} == {13, 17}
    # 親が失敗一覧を持たない構成では失敗一覧との突合による安全網も働かないため、
    # 子の失敗欄は診断として残る。当該構成の縮退を仕様として固定する。
    assert any(e.file == "child/inner_test.py" for e in errors)


def test_parse_pytest_warnings_summary_after_summary_list_keeps_parent_summary() -> None:
    """pytest: 失敗一覧の後に警告の集計が続いても親の失敗一覧を採ることを検証する。

    `_pytest/terminal.py`は失敗一覧の出力後にも後追いの警告の集計を出力する。当該見出しと
    本文を入れ子の実行の標識に含めると、親自身の失敗一覧を子のものと誤判定し、失敗一覧のみを
    情報源とする失敗が診断から消える。検体はpytest 9.1.1で`pytest_terminal_summary`フックから
    警告を送出した実出力から採る。
    """
    output = (
        "============================== short test summary info ===============================\n"
        "FAILED w_test.py::test_w - AssertionError: boom\n"
        "================================== warnings summary ==================================\n"
        "conftest.py:5\n"
        "  /tmp/warn/conftest.py:5: DeprecationWarning: late warning\n"
        '    warnings.warn("late warning", DeprecationWarning, stacklevel=1)\n'
        "\n"
        "-- Docs: https://docs.pytest.org/en/stable/how-to/capture-warnings.html\n"
        "============================ 1 failed, 1 warning in 0.01s ============================\n"
    )
    errors = pyfltr.command.error_parser.parse_errors("pytest", output)
    assert len(errors) == 1
    assert errors[0].file == "w_test.py"
    assert errors[0].line == 0
    assert errors[0].message.startswith("test_w: ")


def test_parse_pytest_child_summary_is_not_taken_without_parent_tail_line() -> None:
    """pytest: 親の最終集計行が無い出力で子の見出しを親のものと誤認しないことを検証する。

    親の実行が途中で終わると最終集計行が出ない。所属判定の上限は出力中で最後に現れる
    集計行を採るため、当該構成では捕捉出力へ混入した子の集計行が上限になる。上限より前の
    子の見出しは標識を伴わず親のものとして採られ、以降にある親の失敗が診断から消える。
    上限として採った集計行より後に標識が現れる場合は上限を出力の末尾へ広げて探し直すため、
    当該構成では子の見出しが標識を伴うようになり、いずれの見出しも親のものと判定されない。
    """
    output = (
        "================================= FAILURES =================================\n"
        "_______________________________ test_runs_child ________________________________\n"
        "tests/p_test.py:13: in test_runs_child\n"
        "E   AssertionError: assert 1 == 0\n"
        "-------------------------- Captured stdout call ---------------------------\n"
        "================================= FAILURES =================================\n"
        "_______________________________ test_inner ________________________________\n"
        "child/inner_test.py:3: in test_inner\n"
        "E   assert 1 == 2\n"
        "========================= short test summary info ==========================\n"
        "FAILED child/inner_test.py::test_inner - assert 1 == 2\n"
        "1 failed in 0.03s\n"
        "_______________________________ test_parent_later ________________________________\n"
        "tests/p_test.py:17: in test_parent_later\n"
        "E   AssertionError: assert 'a' == 'b'\n"
    )
    errors = pyfltr.command.error_parser.parse_errors("pytest", output)
    parent_errors = [e for e in errors if e.file == "tests/p_test.py"]
    assert {e.line for e in parent_errors} == {13, 17}
    # 親が失敗一覧を持たないため安全網が働かず、子の失敗欄は診断として残る。当該構成の縮退を固定する。
    assert [e.file for e in errors if e.file not in {"tests/p_test.py"}] == ["child/inner_test.py"]


def test_parse_pytest_parent_summary_without_tail_line_keeps_safety_net() -> None:
    """pytest: 親が失敗一覧を持ち最終集計行を欠く出力で安全網が働くことを検証する。

    上限として採る集計行が捕捉出力へ混入した子のものになる構成でも、親自身の失敗一覧は
    出力の末尾側に存在する。上限を確定できないことを理由に判別を諦めると失敗一覧が空となり、
    親の失敗一覧に載らないテスト名の失敗欄を除外する安全網が働かず、子の失敗が
    架空の診断として残る。子の失敗一覧の見出しを検体へ含め、上限より前の子の見出しと
    上限より後の親の見出しが競合する構成で後者を採ることを固定する。
    """
    output = (
        "================================= FAILURES =================================\n"
        "_______________________________ test_a ________________________________\n"
        "tests/p_test.py:3: in test_a\n"
        "E   assert 1 == 2\n"
        "-------------------------- Captured stdout call ---------------------------\n"
        "================================= test session starts =================================\n"
        "collected 1 item\n"
        "================================= FAILURES =================================\n"
        "_______________________________ test_inner ________________________________\n"
        "child/inner_test.py:2: in test_inner\n"
        "E   assert 1 == 2\n"
        "========================= short test summary info ==========================\n"
        "FAILED child/inner_test.py::test_inner - assert 1 == 2\n"
        "================================= 1 failed in 0.04s =================================\n"
        "_______________________________ test_b ________________________________\n"
        "tests/p_test.py:7: in test_b\n"
        "E   assert 3 == 4\n"
        "========================= short test summary info ==========================\n"
        "FAILED tests/p_test.py::test_a - assert 1 == 2\n"
        "FAILED tests/p_test.py::test_b - assert 3 == 4\n"
    )
    errors = pyfltr.command.error_parser.parse_errors("pytest", output)
    assert len(errors) == 2
    assert all(e.file == "tests/p_test.py" for e in errors)
    assert {e.line for e in errors} == {3, 7}


@pytest.mark.parametrize("with_failures_section", [False, True])
def test_parse_pytest_run_after_parent_tail_line_keeps_parent_failures(with_failures_section: bool) -> None:
    """pytest: 親の最終集計行の後に別の実行が続いても親の失敗を失わないことを検証する。

    pyfltrは子孫プロセスの出力をストリームの終端まで読むため、親の実行が終わった後に
    打ち切られた孫プロセスの実行が同じ出力へ続くことがある。当該実行の標識を根拠に
    上限を無効と判定すると、失敗一覧のみを情報源とする構成（`with_failures_section=False`）で
    親の失敗をすべて失う。
    """
    failures_section = (
        "================================= FAILURES =================================\n"
        "_______________________________ test_a ________________________________\n"
        "tests/p_test.py:3: in test_a\n"
        "E   assert 1 == 2\n"
        "_______________________________ test_b ________________________________\n"
        "tests/p_test.py:7: in test_b\n"
        "E   assert 3 == 4\n"
    )
    output = (
        (failures_section if with_failures_section else "")
        + "========================= short test summary info ==========================\n"
        "FAILED tests/p_test.py::test_a - assert 1 == 2\n"
        "FAILED tests/p_test.py::test_b - assert 3 == 4\n"
        "================================= 2 failed in 0.50s =================================\n"
        "================================= test session starts =================================\n"
        "collected 1 item\n"
    )
    errors = pyfltr.command.error_parser.parse_errors("pytest", output)
    assert len(errors) == 2
    assert all(e.file == "tests/p_test.py" for e in errors)
    assert {e.line for e in errors} == ({3, 7} if with_failures_section else {0})


def test_parse_pytest_parent_failure_in_shared_test_file_is_kept() -> None:
    """pytest: 親が子と同じテストファイルの同名テストで失敗しても失わないことを検証する。

    捕捉出力へ残った子の失敗一覧に載る`(ファイル, テスト名)`の失敗欄を除外する案は、
    子プロセスへ渡すテストファイルを親自身も収集する構成において、親の実在する失敗を
    除外する。当該案を採らないことを固定する。
    """
    output = (
        "================================= FAILURES =================================\n"
        "_______________________________ test_runs_child ________________________________\n"
        "tests/p_test.py:13: in test_runs_child\n"
        "E   AssertionError: assert 1 == 0\n"
        "-------------------------- Captured stdout call ---------------------------\n"
        "_______________________________ test_same ________________________________\n"
        "shared/s_test.py:3: in test_same\n"
        "E   assert 1 == 2\n"
        "========================= short test summary info ==========================\n"
        "FAILED shared/s_test.py::test_same - assert 1 == 2\n"
        "1 failed in 0.03s\n"
        "_______________________________ test_same ________________________________\n"
        "shared/s_test.py:3: in test_same\n"
        "E   assert 1 == 2\n"
        "================================= 2 failed in 0.89s =================================\n"
    )
    errors = pyfltr.command.error_parser.parse_errors("pytest", output)
    assert any(e.file == "tests/p_test.py" and e.line == 13 for e in errors)
    assert any(e.file == "shared/s_test.py" and e.line == 3 for e in errors)


# 既定のトレースバック形式が例外の連鎖のエントリー間へ出力する区切り行の2形。
# `_pytest/_io/terminalwriter.py`の`sep`は`_ `を並べたのち行幅の余りへ`_`を1文字足すため、
# 行幅が偶数だと末尾が空白、奇数だと末尾が`_`になる。Windowsでは同関数が行幅を1減らすため
# 常に奇数側へ寄る。ブロック見出しの照合はいずれの形も見出しとして採ってはならない。
_PYTEST_ENTRY_SEPARATORS = ["_ " * 39, "_ " * 39 + "_"]


@pytest.mark.parametrize("separator", _PYTEST_ENTRY_SEPARATORS)
@pytest.mark.parametrize("with_failures_section", [False, True])
def test_parse_pytest_interrupt_traceback_after_summary_keeps_parent_summary(
    with_failures_section: bool, separator: str
) -> None:
    """pytest --full-trace: 失敗一覧の後に中断のトレースバックが続いても親の一覧を採ることを検証する。

    `_pytest/terminal.py`は失敗一覧の出力後に中断・停止の報告を出す。`--full-trace`では
    完全なトレースバックが続き、例外の連鎖のエントリー間へ区切り行が現れる。当該区切り行を
    ブロック見出しとして採ると、親自身の失敗一覧を子のものと誤判定する。誤判定すると
    失敗一覧のみを情報源とする失敗が消え（`with_failures_section=False`）、失敗欄を持つ場合も
    解析範囲が中断のトレースバックまで延びて行番号と本文が別の失敗のものへ差し替わる
    （`with_failures_section=True`）。区切り行の2形をいずれも検証する。
    検体はpytest 9.1.1で`--full-trace`付きの中断を起こした実出力の構造から採る。
    """
    failures_section = (
        "====================================== FAILURES ======================================\n"
        "_______________________________________ test_a _______________________________________\n"
        "\n"
        "    def test_a():\n"
        ">       assert 1 == 2\n"
        "E       assert 1 == 2\n"
        "\n"
        "k_test.py:2: AssertionError\n"
    )
    output = (
        (failures_section if with_failures_section else "")
        + "============================== short test summary info ===============================\n"
        "FAILED k_test.py::test_a - assert 1 == 2\n"
        "!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!! KeyboardInterrupt !!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!\n"
        "cls = <class '_pytest.runner.CallInfo'>, when = 'call'\n"
        "\n"
        "    @classmethod\n"
        "    def from_call(cls, func, when):\n"
        ">       result: TResult | None = func()\n"
        "\n"
        "/home/aki/pyfltr/.venv/lib/python3.11/site-packages/_pytest/runner.py:346: \n"
        f"{separator}\n"
        "\n"
        "    def test_b():\n"
        ">       raise KeyboardInterrupt\n"
        "E       KeyboardInterrupt\n"
        "\n"
        "k_test.py:6: KeyboardInterrupt\n"
        "================================= 1 failed in 0.46s ==================================\n"
    )
    errors = pyfltr.command.error_parser.parse_errors("pytest", output)
    assert len(errors) == 1
    assert errors[0].file == "k_test.py"
    assert errors[0].line == (2 if with_failures_section else 0)
    assert errors[0].message.startswith("test_a: assert 1 == 2")


@pytest.mark.parametrize("separator", _PYTEST_ENTRY_SEPARATORS)
def test_parse_pytest_entry_separator_does_not_split_failure_block(separator: str) -> None:
    """pytest --tb=auto: 例外の連鎖の区切り行で失敗欄のブロックが分割されないことを検証する。

    区切り行をブロック見出しとして採ると、1つの失敗が区切り行の数だけ分割され、
    テスト名が区切り行の一部へ化けた診断が水増しされる。区切り行の2形をいずれも検証する。
    """
    output = (
        "====================================== FAILURES ======================================\n"
        "____________________________________ test_chained ____________________________________\n"
        "\n"
        "    def inner():\n"
        '>       raise ValueError("inner")\n'
        "E       ValueError: inner\n"
        "\n"
        "c_test.py:4: ValueError\n"
        "\n"
        "The above exception was the direct cause of the following exception:\n"
        f"{separator}\n"
        "\n"
        "    def test_chained():\n"
        "        try:\n"
        "            inner()\n"
        "        except ValueError as exc:\n"
        '>           raise RuntimeError("outer") from exc\n'
        "E           RuntimeError: outer\n"
        "\n"
        "c_test.py:11: RuntimeError\n"
        "============================== short test summary info ===============================\n"
        "FAILED c_test.py::test_chained - RuntimeError: outer\n"
        "================================= 1 failed in 0.05s ==================================\n"
    )
    errors = pyfltr.command.error_parser.parse_errors("pytest", output)
    assert len(errors) == 1
    assert errors[0].file == "c_test.py"
    assert errors[0].line == 11
    assert errors[0].message == "test_chained: RuntimeError: outer"


def test_parse_pytest_child_summary_is_not_used_for_completion_when_parent_summary_follows() -> None:
    """pytest: 子と親の失敗一覧が並ぶ出力で親の一覧のみを補完・突合に使うことを検証する。

    末尾側の見出しから順に所属を判定するため、静音モードの子の失敗一覧が先に現れても
    親の失敗一覧が採られる。子の一覧のエントリが残余補完へ回り、実在しない失敗として
    診断化されないことを確認する。
    """
    output = (
        "================================= FAILURES =================================\n"
        "_______________________________ test_runs_child ________________________________\n"
        "tests/p_test.py:13: in test_runs_child\n"
        "E   AssertionError: assert 1 == 0\n"
        "-------------------------- Captured stdout call ---------------------------\n"
        "================================= FAILURES =================================\n"
        "_______________________________ test_inner ________________________________\n"
        "child/inner_test.py:3: in test_inner\n"
        "E   assert 1 == 2\n"
        "========================= short test summary info ==========================\n"
        "FAILED child/inner_test.py::test_inner - assert 1 == 2\n"
        "1 failed in 0.03s\n"
        "========================= short test summary info ==========================\n"
        "FAILED tests/p_test.py::test_runs_child - AssertionError: assert 1 == 0\n"
        "FAILED tests/p_test.py::test_no_block - AssertionError: no block\n"
        "================================= 2 failed in 0.89s =================================\n"
    )
    errors = pyfltr.command.error_parser.parse_errors("pytest", output)
    assert len(errors) == 2
    assert all(e.file == "tests/p_test.py" for e in errors)
    by_test = {e.message.split(":")[0]: e for e in errors}
    assert by_test["test_runs_child"].line == 13
    assert by_test["test_no_block"].line == 0


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


def test_parse_errors_markdownlint_with_column() -> None:
    """markdownlint: 列番号を報告するルールでもfile・lineを正しく抽出する。

    列番号を許容しないと`_FILE`のドライブレター表記がファイル名の末尾へ侵入し、
    file・lineともに架空の値になる。
    """
    output = 'docs/index.md:3:32 MD059/descriptive-link-text Link text should be descriptive [Context: "[here]"]'
    errors = pyfltr.command.error_parser.parse_errors("markdownlint", output)
    assert len(errors) == 1
    assert errors[0].file == "docs/index.md"
    assert errors[0].line == 3
    assert errors[0].col == 32
    assert errors[0].rule == "MD059"


def test_parse_errors_markdownlint_with_column_on_windows_path() -> None:
    """markdownlint: Windowsドライブレター表記でも列番号付きの位置を正しく抽出する。

    ドライブレター表記の侵入が本不具合の根本原因のため、当該表記そのものを検証する。
    """
    output = "C:/proj/docs/index.md:5:45 MD009/no-trailing-spaces Trailing spaces [Expected: 0; Actual: 1]"
    errors = pyfltr.command.error_parser.parse_errors("markdownlint", output)
    assert len(errors) == 1
    assert errors[0].file.endswith("docs/index.md")
    assert errors[0].line == 5
    assert errors[0].col == 45


def test_parse_errors_markdownlint_with_column_and_severity() -> None:
    """markdownlint: 列番号とseverityが同時に介在する形でも正しく抽出する。"""
    output = "docs/index.md:5:1 error MD009/no-trailing-spaces Trailing spaces [Expected: 0; Actual: 1]"
    errors = pyfltr.command.error_parser.parse_errors("markdownlint", output)
    assert len(errors) == 1
    assert errors[0].file == "docs/index.md"
    assert errors[0].line == 5
    assert errors[0].col == 1
    assert errors[0].rule == "MD009"


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
