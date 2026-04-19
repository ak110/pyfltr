"""error_parserのテストコード。"""

import json
import pathlib

import pytest

import pyfltr.error_parser


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
        # biome --reporter=github (line と col の間に endLine が挟まる)
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
    errors = pyfltr.error_parser.parse_errors(command, output)
    assert len(errors) == expected_count
    if expected_count > 0:
        assert errors[0].file == expected_first_file
        assert errors[0].line == expected_first_line
        assert errors[0].command == command


def test_parse_errors_eslint_json() -> None:
    """ESLint --format json 出力のパース。"""
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
    errors = pyfltr.error_parser.parse_errors("eslint", output)
    assert len(errors) == 2
    assert errors[0].file == "src/foo.js"  # cwd 配下は相対パスに正規化される
    assert errors[0].line == 10
    assert errors[0].col == 5
    assert "no-unused-vars" in errors[0].message
    assert errors[0].command == "eslint"
    assert errors[1].line == 20


def test_parse_errors_eslint_json_empty_array() -> None:
    """空配列 `[]` は空リストを返す。"""
    errors = pyfltr.error_parser.parse_errors("eslint", "[]")
    assert errors == []


def test_parse_errors_eslint_json_empty_string() -> None:
    """空文字列は空リストを返す (例外なし)。"""
    errors = pyfltr.error_parser.parse_errors("eslint", "")
    assert errors == []


def test_parse_errors_eslint_json_invalid() -> None:
    """不正な JSON (stderr 混入等) は空リストを返す。"""
    errors = pyfltr.error_parser.parse_errors("eslint", "Warning: something\n[not json]")
    assert errors == []


def test_parse_errors_eslint_json_no_rule_id() -> None:
    """ruleId が null の場合でも message のみ格納する。"""
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
    errors = pyfltr.error_parser.parse_errors("eslint", output)
    assert len(errors) == 1
    assert errors[0].message == "Parsing error"


def test_parse_errors_custom_pattern() -> None:
    """カスタムerror-patternのテスト。"""
    pattern = r"(?P<file>[^:]+):(?P<line>\d+):(?P<col>\d+):\s*(?P<message>.+)"
    output = "src/foo.py:10:5: some error\nsrc/bar.py:20:3: another error"
    errors = pyfltr.error_parser.parse_errors("custom-tool", output, error_pattern=pattern)
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
        pyfltr.error_parser.ErrorLocation(file="src/bar.py", line=10, col=None, command="mypy", message="err1"),
        pyfltr.error_parser.ErrorLocation(file="src/bar.py", line=10, col=None, command="ruff-check", message="err2"),
        pyfltr.error_parser.ErrorLocation(file="src/foo.py", line=5, col=None, command="mypy", message="err3"),
    ]
    sorted_errors = pyfltr.error_parser.sort_errors(errors, command_names)

    # ファイル名でソート → 同一箇所はcommand_names順
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
    errors = pyfltr.error_parser.parse_errors("pyright", output)
    assert len(errors) == 1
    assert errors[0].file == "src/foo.py"  # 相対パスになっている


def test_format_error() -> None:
    """エラーフォーマットのテスト。"""
    error = pyfltr.error_parser.ErrorLocation(file="src/foo.py", line=10, col=5, command="mypy", message="some error")
    assert pyfltr.error_parser.format_error(error) == "src/foo.py:10:5: [mypy] some error"

    # colなし
    error_no_col = pyfltr.error_parser.ErrorLocation(
        file="src/foo.py", line=10, col=None, command="ruff-check", message="another error"
    )
    assert pyfltr.error_parser.format_error(error_no_col) == "src/foo.py:10: [ruff-check] another error"

    # ruleあり
    error_with_rule = pyfltr.error_parser.ErrorLocation(
        file="src/foo.py", line=10, col=5, command="ruff-check", message="`os` imported but unused", rule="F401"
    )
    assert pyfltr.error_parser.format_error(error_with_rule) == "src/foo.py:10:5: [ruff-check:F401] `os` imported but unused"


def test_parse_ruff_check_json() -> None:
    """ruff check --output-format=json 出力のパース。"""
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
    errors = pyfltr.error_parser.parse_errors("ruff-check", output)
    assert len(errors) == 1
    assert errors[0].file == "src/foo.py"
    assert errors[0].line == 1
    assert errors[0].col == 8
    assert errors[0].rule == "F401"
    assert errors[0].severity == "error"
    assert errors[0].fix == "safe"
    assert errors[0].message == "`os` imported but unused"


def test_parse_ruff_check_json_fallback() -> None:
    """ruff-check: JSON でない出力は regex にフォールバックする。"""
    output = "src/foo.py:10:5: F401 `os` imported but unused"
    errors = pyfltr.error_parser.parse_errors("ruff-check", output)
    assert len(errors) == 1
    assert errors[0].file == "src/foo.py"
    assert errors[0].line == 10


def test_parse_ruff_check_json_fix_none() -> None:
    """ruff-check: ``fix`` 欠落エントリは ``fix == "none"`` として出力される。"""
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
    errors = pyfltr.error_parser.parse_errors("ruff-check", output)
    assert len(errors) == 1
    assert errors[0].fix == "none"


def test_parse_typos_jsonl_no_corrections_is_none() -> None:
    """typos: corrections が空の場合は ``fix == "none"``。"""
    output = '{"path":"src/foo.py","line_num":3,"typo":"weirdword","corrections":[],"type":"typo"}\n'
    errors = pyfltr.error_parser.parse_errors("typos", output)
    assert len(errors) == 1
    assert errors[0].fix == "none"


def test_parse_textlint_json_fix_none() -> None:
    """textlint: ``fix`` 欠落メッセージは ``fix == "none"``。"""
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
    errors = pyfltr.error_parser.parse_errors("textlint", output)
    assert len(errors) == 1
    assert errors[0].fix == "none"


def test_parse_pylint_json() -> None:
    """pylint --output-format=json2 出力のパース。

    rule には symbol (公式ドキュメント URL 基準)、message には messageId を保持する。
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
    errors = pyfltr.error_parser.parse_errors("pylint", output)
    assert len(errors) == 1
    assert errors[0].rule == "missing-module-docstring"
    assert errors[0].severity == "warning"
    assert errors[0].message == "C0114: Missing module docstring"
    assert errors[0].rule_url == (
        "https://pylint.readthedocs.io/en/stable/user_guide/messages/convention/missing-module-docstring.html"
    )


def test_parse_pylint_json_with_stderr_prefix() -> None:
    """pylint: JSON 前にstderrの警告などが混ざっても最初の ``{`` 以降をパースする。

    Windows + Python 3.14 + PYTHONDEVMODE=1 で pylint_pydantic が大量の
    DeprecationWarning を emit し、pylint の出力先頭に紛れ込む現象への対処。
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
    errors = pyfltr.error_parser.parse_errors("pylint", prefix + body)
    assert len(errors) == 1
    assert errors[0].rule == "missing-module-docstring"


def test_parse_pylint_json_fallback() -> None:
    """pylint: JSON でない出力は regex にフォールバックする。"""
    output = "src/foo.py:10:5: C0114: Missing module docstring (missing-module-docstring)"
    errors = pyfltr.error_parser.parse_errors("pylint", output)
    assert len(errors) == 1
    assert errors[0].line == 10


def test_parse_pyright_json() -> None:
    """pyright --outputjson 出力のパース。"""
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
    errors = pyfltr.error_parser.parse_errors("pyright", output)
    assert len(errors) == 1
    assert errors[0].line == 10  # 0-based → 1-based
    assert errors[0].col == 5  # 0-based → 1-based
    assert errors[0].rule == "reportAssignmentType"
    assert errors[0].severity == "error"


def test_parse_pyright_json_fallback() -> None:
    """pyright: JSON でない出力は regex にフォールバックする。"""
    output = '  src/foo.py:10:5 - error: Type "int" is not assignable'
    errors = pyfltr.error_parser.parse_errors("pyright", output)
    assert len(errors) == 1
    assert errors[0].line == 10


def test_parse_shellcheck_json() -> None:
    """shellcheck -f json 出力のパース。"""
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
    errors = pyfltr.error_parser.parse_errors("shellcheck", output)
    assert len(errors) == 1
    assert errors[0].rule == "SC2086"
    assert errors[0].severity == "warning"
    assert errors[0].message == "Double quote to prevent globbing"


def test_parse_textlint_json() -> None:
    """textlint --format json 出力のパース。"""
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
    errors = pyfltr.error_parser.parse_errors("textlint", output)
    assert len(errors) == 1
    assert errors[0].rule == "ja-technical-writing/ja-no-mixed-period"
    assert errors[0].severity == "error"
    assert errors[0].fix == "safe"
    # 登録外ルールなので hint は付与されない
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
    errors = pyfltr.error_parser.parse_errors("textlint", output)
    assert len(errors) == 1
    assert errors[0].hint is not None
    assert "句点" in errors[0].hint


def test_parse_textlint_json_hint_for_known_rules() -> None:
    """textlint `max-ten` / `max-kanji-continuous-len` にもヒントが付く。"""
    for rule_id in (
        "ja-technical-writing/max-ten",
        "ja-technical-writing/max-kanji-continuous-len",
    ):
        output = json.dumps(
            [
                {
                    "filePath": "a.md",
                    "messages": [{"line": 1, "column": 1, "message": "x", "ruleId": rule_id, "severity": 2}],
                }
            ]
        )
        errors = pyfltr.error_parser.parse_errors("textlint", output)
        assert errors[0].hint is not None, f"{rule_id} にヒントが付与されていない"


def test_parse_typos_jsonl() -> None:
    """typos --format=json 出力（JSON Lines）のパース。"""
    output = (
        '{"path":"src/foo.py","line_num":3,"byte_offset":15,"typo":"teh","corrections":["the"],"type":"typo"}\n'
        '{"path":"src/bar.py","line_num":7,"byte_offset":20,"typo":"hte","corrections":["the","he"],"type":"typo"}\n'
    )
    errors = pyfltr.error_parser.parse_errors("typos", output)
    assert len(errors) == 2
    assert errors[0].file == "src/foo.py"
    assert errors[0].line == 3
    assert errors[0].message == "`teh` -> `the`"
    assert errors[0].severity == "warning"
    assert errors[0].fix == "safe"
    assert errors[1].message == "`hte` -> `the, he`"


def test_parse_typos_jsonl_fallback() -> None:
    """typos: JSON Lines でない出力は regex にフォールバックする。"""
    output = "src/foo.py:3:15: `teh` -> `the`"
    errors = pyfltr.error_parser.parse_errors("typos", output)
    assert len(errors) == 1
    assert errors[0].line == 3


def test_parse_pytest_tb_short_project_frame() -> None:
    """pytest --tb=short: プロジェクト内フレームが選択される。"""
    output = (
        "================================= FAILURES =================================\n"
        "_______________________________ test_bar ________________________________\n"
        "tests/foo_test.py:42: in test_bar\n"
        "    result = do_something()\n"
        "E   AssertionError: assert 1 == 2\n"
        "========================= short test summary info ==========================\n"
        "FAILED tests/foo_test.py::test_bar - AssertionError: assert 1 == 2\n"
    )
    errors = pyfltr.error_parser.parse_errors("pytest", output)
    assert len(errors) == 1
    assert errors[0].file == "tests/foo_test.py"
    assert errors[0].line == 42
    assert "assert 1 == 2" in errors[0].message


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
    errors = pyfltr.error_parser.parse_errors("pytest", output)
    assert len(errors) == 1
    assert errors[0].file == "tests/api_test.py"
    assert errors[0].line == 15
    assert "httpx.ConnectError" in errors[0].message


def test_parse_pytest_tb_short_stdlib_exception() -> None:
    """pytest --tb=short: 標準ライブラリで例外が発生した場合、プロジェクト内フレームが選択される。

    uv管理Pythonでは標準ライブラリが``..``始まりの相対パスで出力される。
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
    errors = pyfltr.error_parser.parse_errors("pytest", output)
    assert len(errors) == 1
    assert errors[0].file == "tests/path_test.py"
    assert errors[0].line == 10


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
    errors = pyfltr.error_parser.parse_errors("pytest", output)
    assert len(errors) == 1
    assert errors[0].line == 20
    assert "RuntimeError: fail" in errors[0].message


def test_parse_pytest_fallback() -> None:
    """pytest: --tb=line 形式がなければ FAILED 行にフォールバック（line=0）。"""
    output = (
        "FAILED tests/foo_test.py::test_bar - AssertionError: xxx\n"
        "========================= 1 failed in 0.5s =========================\n"
    )
    errors = pyfltr.error_parser.parse_errors("pytest", output)
    assert len(errors) == 1
    assert errors[0].file == "tests/foo_test.py"
    assert errors[0].line == 0


def test_get_custom_parser_commands() -> None:
    """カスタムパーサー登録コマンド一覧の取得。"""
    commands = pyfltr.error_parser.get_custom_parser_commands()
    assert "eslint" in commands
    assert "ruff-check" in commands
    assert "pytest" in commands
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
    result = pyfltr.error_parser.parse_summary("pyright", output)
    assert result == "50 files analyzed, 0 errors, 2 warnings"


def test_parse_summary_pyright_json_no_summary() -> None:
    """pyright: summaryフィールドがない場合はNone。"""
    output = json.dumps({"generalDiagnostics": []})
    assert pyfltr.error_parser.parse_summary("pyright", output) is None


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
    result = pyfltr.error_parser.parse_summary("pylint", output)
    assert result == "42 modules linted, score: 10.0"


def test_parse_summary_pylint_json_no_score() -> None:
    """pylint: scoreがない場合はモジュール数のみ。"""
    output = json.dumps({"messages": [], "statistics": {"modulesLinted": 10}})
    result = pyfltr.error_parser.parse_summary("pylint", output)
    assert result == "10 modules linted"


def test_parse_summary_pytest() -> None:
    """pytest: 末尾のサマリー行から=パディングを除去して抽出する。"""
    output = (
        "============================= test session starts ==============================\n"
        "collected 25 items\n"
        "\n"
        "tests/foo_test.py .........................                                [100%]\n"
        "\n"
        "============================== 25 passed in 1.23s ==============================\n"
    )
    result = pyfltr.error_parser.parse_summary("pytest", output)
    assert result == "25 passed in 1.23s"


def test_parse_summary_pytest_long_duration() -> None:
    """pytest: 長時間実行時の (H:MM:SS) 形式も正しく抽出する。"""
    output = "============================== 25 passed in 60.00s (0:01:00) ==============================\n"
    result = pyfltr.error_parser.parse_summary("pytest", output)
    assert result == "25 passed in 60.00s (0:01:00)"


def test_parse_summary_mypy_via_fallback() -> None:
    """mypy: 汎用フォールバックでSuccess行を抽出する。"""
    output = "Success: no issues found in 42 source files\n"
    result = pyfltr.error_parser.parse_summary("mypy", output)
    assert result == "Success: no issues found in 42 source files"


def test_parse_summary_json_output_returns_none() -> None:
    """JSON出力（[]等）は汎用フォールバックでNoneを返す。"""
    assert pyfltr.error_parser.parse_summary("ruff-check", "[]") is None
    assert pyfltr.error_parser.parse_summary("shellcheck", "[]") is None


def test_parse_summary_empty_output() -> None:
    """空出力はNoneを返す。"""
    assert pyfltr.error_parser.parse_summary("mypy", "") is None
    assert pyfltr.error_parser.parse_summary("mypy", "  \n  ") is None


def test_extract_last_line_skips_separators() -> None:
    """区切り線のみの行をスキップして意味のある行を返す。"""
    output = "Some useful info\n===========================\n"
    result = pyfltr.error_parser.parse_summary("unknown-tool", output)
    assert result == "Some useful info"


def test_parse_errors_mypy_extracts_rule() -> None:
    """mypy の末尾 `[error-code]` が rule グループで抽出され rule_url も付与される。"""
    output = 'src/foo.py:10: error: Name "x" is not defined  [name-defined]'
    errors = pyfltr.error_parser.parse_errors("mypy", output)
    assert len(errors) == 1
    assert errors[0].rule == "name-defined"
    assert errors[0].rule_url == "https://mypy.readthedocs.io/en/stable/_refs.html#code-name-defined"
    # message に末尾の [rule] は含めない
    assert errors[0].message == 'Name "x" is not defined'


def test_parse_errors_mypy_without_rule() -> None:
    """mypy で末尾 [code] が無い行は rule=None になる。"""
    output = "src/foo.py:10: error: Something went wrong"
    errors = pyfltr.error_parser.parse_errors("mypy", output)
    assert len(errors) == 1
    assert errors[0].rule is None
    assert errors[0].rule_url is None


def test_parse_errors_markdownlint_extracts_rule() -> None:
    """markdownlint の MDxxx が rule グループで抽出される。"""
    output = "docs/index.md:3 MD001/heading-increment Heading levels should only increment by one level at a time"
    errors = pyfltr.error_parser.parse_errors("markdownlint", output)
    assert len(errors) == 1
    assert errors[0].rule == "MD001"
    assert errors[0].rule_url == "https://github.com/DavidAnson/markdownlint/blob/main/doc/MD001.md"


def test_parse_errors_ruff_rule_url_from_entry() -> None:
    """ruff JSON の ``url`` フィールドを最優先で採用する。"""
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
    errors = pyfltr.error_parser.parse_errors("ruff-check", output)
    assert len(errors) == 1
    assert errors[0].rule_url == "https://example.com/custom-ruff-url"


def test_parse_errors_ruff_rule_url_fallback() -> None:
    """ruff JSON に ``url`` が無い場合はテンプレートで生成する。"""
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
    errors = pyfltr.error_parser.parse_errors("ruff-check", output)
    assert len(errors) == 1
    assert errors[0].rule_url == "https://docs.astral.sh/ruff/rules/F401/"


def test_parse_errors_pyright_rule_url() -> None:
    """pyright の rule から rule_url が生成される。"""
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
    errors = pyfltr.error_parser.parse_errors("pyright", output)
    assert errors[0].rule_url == "https://microsoft.github.io/pyright/#/configuration?id=reportAssignmentType"


def test_parse_errors_shellcheck_rule_url() -> None:
    """shellcheck の rule から rule_url が生成される。"""
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
    errors = pyfltr.error_parser.parse_errors("shellcheck", output)
    assert errors[0].rule_url == "https://www.shellcheck.net/wiki/SC2086"


def test_parse_errors_eslint_rule_url() -> None:
    """eslint の本体ルールから rule_url が生成される。プラグインルールは URL 無し。"""
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
    errors = pyfltr.error_parser.parse_errors("eslint", output)
    assert len(errors) == 2
    assert errors[0].rule_url == "https://eslint.org/docs/latest/rules/no-unused-vars"
    # プラグインルール (スラッシュ含む) は URL を返さない
    assert errors[1].rule_url is None


def test_parse_errors_textlint_no_rule_url() -> None:
    """textlint は rule_url 未サポート (常に None)。"""
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
    errors = pyfltr.error_parser.parse_errors("textlint", output)
    assert errors[0].rule_url is None


def test_parse_errors_shellcheck_severity_normalized() -> None:
    """shellcheck の level=STYLE などを正規化する。"""
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
    errors = pyfltr.error_parser.parse_errors("shellcheck", output)
    assert errors[0].severity == "info"
