import json
import pathlib

import pytest

import pyfltr.diagnostics
import pyfltr.output.diagnostics
import pyfltr.parsing.entry
import pyfltr.parsing.pytest
import pyfltr.tools


def _diagnostic_fields(
    error: pyfltr.diagnostics.ErrorLocation,
) -> tuple[str, int, int | None, str, str, str | None, str | None, str | None, int | None, int | None]:
    """位置取り込み前から存在する診断情報と終了位置を比較用タプルで返す。"""
    return (
        error.file,
        error.line,
        error.col,
        error.command,
        error.message,
        error.rule,
        error.severity,
        error.fix,
        error.end_line,
        error.end_col,
    )


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
            "src/foo.py:3:1: warning[unused-ignore-comment] Unused `ty: ignore` directive",
            1,
            "src/foo.py",
            3,
        ),
        # ty check --output-format concise (info)
        (
            "ty",
            "src/reveal.py:2:13: info[revealed-type] Revealed type: `Literal[1]`",
            1,
            "src/reveal.py",
            2,
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
    errors = pyfltr.parsing.entry.parse_errors("biome", output)
    assert len(errors) == 1
    assert errors[0].severity == expected_severity
    assert errors[0].message == message_text


def test_parse_errors_biome_rule_and_url() -> None:
    """biomeのtitleを診断カテゴリーとして抽出しURLを補完する。"""
    output = (
        "::error title=lint/suspicious/noDoubleEquals,file=src/foo.ts,"
        "line=1,endLine=1,col=7,endColumn=9::Use === instead of ==\n"
    )
    errors = pyfltr.parsing.entry.parse_errors("biome", output)
    assert len(errors) == 1
    assert errors[0].rule == "lint/suspicious/noDoubleEquals"
    assert errors[0].rule_url == "https://biomejs.dev/linter/rules/no-double-equals/"


def test_parse_errors_biome_assist_rule_and_url() -> None:
    """biomeのassistカテゴリーを抽出しアクションURLを補完する。"""
    output = (
        "::error title=assist/source/organizeImports,file=src/foo.ts,line=1,endLine=1,col=1,endColumn=2::Organize imports\n"
    )
    errors = pyfltr.parsing.entry.parse_errors("biome", output)
    assert len(errors) == 1
    assert errors[0].rule == "assist/source/organizeImports"
    assert errors[0].rule_url == "https://biomejs.dev/assist/actions/organize-imports/"


@pytest.mark.parametrize("category", ["format", "parse"])
def test_parse_errors_biome_non_rule_category_has_no_url(category: str) -> None:
    """format・parseは診断カテゴリーとして保持しURLを持たない。"""
    output = f"::error title={category},file=package.json,line=1,endLine=1,col=2,endColumn=2::Diagnostic message\n"
    errors = pyfltr.parsing.entry.parse_errors("biome", output)
    assert len(errors) == 1
    assert errors[0].rule == category
    assert errors[0].rule_url is None


def test_parse_errors_biome_without_title_is_not_dropped() -> None:
    """title欄を持たない出力形態でも診断を保持する。"""
    output = "::error file=src/foo.ts,line=1,endLine=1,col=7,endColumn=9::message\n"
    errors = pyfltr.parsing.entry.parse_errors("biome", output)
    assert len(errors) == 1
    assert errors[0].rule is None
    assert errors[0].file.endswith("foo.ts")


def test_parse_errors_biome_end_position() -> None:
    """biomeのendLine・endColumnをend_line・end_colへ取り込む。"""
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
    """titleを持たない出力形態でも終了位置を取り込む。"""
    output = "::error file=src/foo.ts,line=1,endLine=1,col=7,endColumn=9::message\n"
    errors = pyfltr.parsing.entry.parse_errors("biome", output)
    assert len(errors) == 1
    assert errors[0].rule is None
    assert errors[0].end_line == 1
    assert errors[0].end_col == 9


def test_parse_errors_biome_without_end_position() -> None:
    """endLine・endColumnを持たない出力形態でも診断を保持する。"""
    output = "::error title=lint/style/useConst,file=src/bar.ts,line=5,col=3::Use const instead of let\n"
    errors = pyfltr.parsing.entry.parse_errors("biome", output)
    assert len(errors) == 1
    assert errors[0].line == 5
    assert errors[0].col == 3
    assert errors[0].end_line is None
    assert errors[0].end_col is None
    assert errors[0].rule == "lint/style/useConst"


def test_parse_errors_biome_line_does_not_match_parameter_suffix() -> None:
    """lineは別パラメーター名の末尾へ一致しない。"""
    output = "::error title=lint/style/useConst,file=src/bar.ts,baseline=5,col=3::message\n"
    assert pyfltr.parsing.entry.parse_errors("biome", output) == []


def test_parse_errors_biome_col_does_not_match_parameter_suffix() -> None:
    """colは別パラメーター名の末尾へ一致しない。"""
    output = "::error title=lint/style/useConst,file=src/bar.ts,line=5,protocol=3::message\n"
    assert pyfltr.parsing.entry.parse_errors("biome", output) == []


def test_parse_errors_ty_rule_without_url() -> None:
    """tyのrule識別子を抽出し、messageは診断本文のみとする。URLは生成しない。"""
    output = "src/foo.py:10:5: error[invalid-argument-type] Argument is incorrect\n"
    errors = pyfltr.parsing.entry.parse_errors("ty", output)
    assert len(errors) == 1
    assert errors[0].rule == "invalid-argument-type"
    assert errors[0].rule_url is None
    assert errors[0].severity == "error"
    assert errors[0].message == "Argument is incorrect"


def test_parse_errors_ty_warning_severity() -> None:
    """tyのwarning診断はseverityをwarningとして抽出する。"""
    output = "src/warn.py:1:13: warning[unused-ignore-comment] Unused `ty: ignore` directive\n"
    errors = pyfltr.parsing.entry.parse_errors("ty", output)
    assert len(errors) == 1
    assert errors[0].rule == "unused-ignore-comment"
    assert errors[0].severity == "warning"
    assert errors[0].message == "Unused `ty: ignore` directive"


def test_parse_errors_ty_info_severity() -> None:
    """tyのinfo診断を取りこぼさずseverityをinfoとして抽出する。"""
    output = "src/reveal.py:2:17: info[revealed-type] Revealed type: `int`\n"
    errors = pyfltr.parsing.entry.parse_errors("ty", output)
    assert len(errors) == 1
    assert errors[0].rule == "revealed-type"
    assert errors[0].severity == "info"
    assert errors[0].message == "Revealed type: `int`"
    # `revealed-type`はtyのルールに当たらずドキュメントのアンカーを持たない。
    # 識別子の字面からルールと区別できないため、tyはURLを生成しない。
    assert errors[0].rule_url is None


def test_parse_errors_ty_message_starting_with_info_keeps_severity() -> None:
    """診断本文の先頭が`info`でもseverityは角括弧直前の語から決まる。"""
    output = "src/a.py:1:1: error[invalid-assignment] info about the assignment\n"
    errors = pyfltr.parsing.entry.parse_errors("ty", output)
    assert len(errors) == 1
    assert errors[0].severity == "error"
    assert errors[0].message == "info about the assignment"


def test_parse_errors_ty_info_like_prefix_is_ignored() -> None:
    """`info`で始まる別語は診断種別として扱わない。"""
    output = "src/a.py:5:5: infomercial[foo] not a severity\n"
    errors = pyfltr.parsing.entry.parse_errors("ty", output)
    assert len(errors) == 0


def test_parse_errors_ty_summary_line_is_ignored() -> None:
    """tyの集計行は診断として扱わない。"""
    output = "Found 4 diagnostics\n"
    errors = pyfltr.parsing.entry.parse_errors("ty", output)
    assert len(errors) == 0


def test_parse_errors_actionlint_rule() -> None:
    """actionlintの末尾角括弧をruleとして抽出し、messageから除く。"""
    output = '.github/workflows/ci.yaml:7:24: property "nonexistent" is not defined in object type {} [expression]\n'
    errors = pyfltr.parsing.entry.parse_errors("actionlint", output)
    assert len(errors) == 1
    assert errors[0].rule == "expression"
    assert errors[0].message == 'property "nonexistent" is not defined in object type {}'
    # 公式ドキュメントの見出しがrule識別子と多対多で対応するためURLは生成しない。
    assert errors[0].rule_url is None


def test_parse_errors_actionlint_rule_with_brackets_in_message() -> None:
    """診断本文中の空白を含む角括弧はruleとして扱わない。"""
    output = ".github/workflows/ci.yaml:16:14: label is unknown. available labels are [a b] [runner-label]\n"
    errors = pyfltr.parsing.entry.parse_errors("actionlint", output)
    assert len(errors) == 1
    assert errors[0].rule == "runner-label"
    assert errors[0].message == "label is unknown. available labels are [a b]"


def test_parse_errors_actionlint_without_rule() -> None:
    """末尾に角括弧を持たない診断行でも本文を保持する。"""
    output = ".github/workflows/ci.yaml:10:5: something went wrong\n"
    errors = pyfltr.parsing.entry.parse_errors("actionlint", output)
    assert len(errors) == 1
    assert errors[0].rule is None
    assert errors[0].message == "something went wrong"


def test_parse_errors_actionlint_snippet_line_is_ignored() -> None:
    """スニペット行は診断として扱わない。"""
    output = '  |\n7 |       - run: echo "${{ matrix.nonexistent }}"\n'
    errors = pyfltr.parsing.entry.parse_errors("actionlint", output)
    assert len(errors) == 0


def test_parse_errors_yamllint_parsable() -> None:
    """yamllint -f parsableの各行から位置・重大度・ruleを抽出し、messageからruleを除く。"""
    # yamllint 1.38.0の実出力。構文エラーはrule`syntax`として出力される。
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
    """診断本文中の丸括弧はruleとして扱わず、行末の丸括弧だけをruleとする。"""
    output = "a.yaml:2:41: [error] line too long (65 > 40 characters) (line-length)\n"
    errors = pyfltr.parsing.entry.parse_errors("yamllint", output)
    assert len(errors) == 1
    assert errors[0].rule == "line-length"
    assert errors[0].message == "line too long (65 > 40 characters)"


def test_parse_errors_yamllint_standard_format_is_ignored() -> None:
    """parsable以外の標準形式（見出し行と字下げした違反行）は診断として扱わない。

    `yamllint-parsable = false`で注入を無効化した場合の挙動を固定する。
    """
    output = "bad.yaml\n  1:4       error    too many spaces after colon  (colons)\n"
    assert not pyfltr.parsing.entry.parse_errors("yamllint", output)


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
    assert _diagnostic_fields(errors[0]) == (
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
    """空配列 `[]` は空リストを返す。"""
    errors = pyfltr.parsing.entry.parse_errors("eslint", "[]")
    assert errors == []


def test_parse_errors_eslint_json_empty_string() -> None:
    """空文字列は空リストを返す (例外なし)。"""
    errors = pyfltr.parsing.entry.parse_errors("eslint", "")
    assert errors == []


def test_parse_errors_eslint_json_invalid() -> None:
    """不正なJSON（stderr混入等）は空リストを返す。"""
    errors = pyfltr.parsing.entry.parse_errors("eslint", "Warning: something\n[not json]")
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
    errors = pyfltr.parsing.entry.parse_errors("eslint", output)
    assert len(errors) == 1
    assert errors[0].message == "Parsing error"


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
    assert _diagnostic_fields(errors[0]) == (
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


def test_parse_typos_jsonl_no_corrections_is_none() -> None:
    """typos: correctionsが空の場合は`fix == "none"`。"""
    output = '{"path":"src/foo.py","line_num":3,"typo":"weirdword","corrections":[],"type":"typo"}\n'
    errors = pyfltr.parsing.entry.parse_errors("typos", output)
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
    errors = pyfltr.parsing.entry.parse_errors("textlint", output)
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
    assert _diagnostic_fields(errors[0]) == (
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
    """非ASCII行のUTF-8バイトオフセットは文字列を参照せず1起点へ補正する。"""
    errors = pyfltr.parsing.entry.parse_errors(command, output)

    assert len(errors) == 1
    assert _diagnostic_fields(errors[0]) == expected


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
    assert _diagnostic_fields(errors[0]) == (
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

    assert [_diagnostic_fields(error) for error in errors] == [
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
    """終了位置を格納する全経路で次行先頭を包含行と列不明へ正規化する。"""
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
    """終了列が行内を指す複数行範囲は全経路で終了行・終了列を保持する。"""
    errors = pyfltr.parsing.entry.parse_errors(command, output)

    assert len(errors) == 1
    assert errors[0].end_line == 3
    assert errors[0].end_col == 4


def test_parse_eslint_json_normalizes_real_world_end_position() -> None:
    """ESLintの実出力で観測した次行先頭終端の形を包含行へ補正する。

    ブロック末尾の空行を対象とするルールは、範囲が行末で終わるため
    `endLine`が次行・`endColumn`が1となる報告を返す。補正前は範囲外の行が終了行に入っていた。
    """
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
    """終了行が開始行と同じ場合は終了列が行頭でも補正しない。"""
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
    """終了行が無い場合は終了列が行頭でも補正せず値を保持する。"""
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
    """終了行が開始行より前の退化入力では補正せず値を保持する。"""
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
    """shellcheck -f json出力のパース。"""
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
    assert _diagnostic_fields(errors[0]) == (
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
    """shellcheck: JSONでない出力でも既存位置を保ち、終了位置は設定しない。"""
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
    """終了位置の欠落・null・非数値は診断を落とさずNoneへ縮退する。"""
    errors = pyfltr.parsing.entry.parse_errors(command, output)

    assert len(errors) == 3
    assert all(error.end_line is None and error.end_col is None for error in errors)


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
    errors = pyfltr.parsing.entry.parse_errors("textlint", output)
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
    errors = pyfltr.parsing.entry.parse_errors("textlint", output)
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
        errors = pyfltr.parsing.entry.parse_errors("textlint", output)
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
    errors = pyfltr.parsing.entry.parse_errors("textlint", output)
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
    errors = pyfltr.parsing.entry.parse_errors("textlint", output)
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
    errors = pyfltr.parsing.entry.parse_errors("textlint", output)
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
    errors = pyfltr.parsing.entry.parse_errors("textlint", output)
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
    errors = pyfltr.parsing.entry.parse_errors("textlint", output)
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
    errors = pyfltr.parsing.entry.parse_errors("textlint", output)
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
    errors = pyfltr.parsing.entry.parse_errors("textlint", output)
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
    errors = pyfltr.parsing.entry.parse_errors("textlint", output)
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
    errors = pyfltr.parsing.entry.parse_errors("textlint", output)
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
    errors = pyfltr.parsing.entry.parse_errors("textlint", output)
    assert errors[0].hint is not None
    assert "累積位置" not in errors[0].hint


def test_parse_typos_jsonl() -> None:
    """typos --format=json出力（JSON Lines）のパース。"""
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
    """typos: JSON Linesでない出力はregexにフォールバックする。"""
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
    errors = pyfltr.parsing.entry.parse_errors("vitest", output)
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
    assert pyfltr.parsing.entry.parse_errors("glab-ci-lint", output) == []


def test_parse_glab_ci_lint_invalid_multi() -> None:
    """無効CI出力から複数エラーをline=1固定で抽出する。"""
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
    """番号付きリスト形式 (`1. xxx`) のエラー行もリストマーカーを除去して取り込む。"""
    output = ".gitlab-ci.yml is invalid\n1. unknown key foo\n2. unknown key bar\n"
    errors = pyfltr.parsing.entry.parse_errors("glab-ci-lint", output)
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
    errors = pyfltr.parsing.entry.parse_errors("designmd", output)
    assert len(errors) == 2
    # 対象ファイルは仕様上DESIGN.md固定。
    assert all(e.file == "DESIGN.md" for e in errors)
    assert all(e.command == "designmd" for e in errors)
    assert errors[0].severity == "warning"
    assert errors[0].message.startswith("components.button-primary: ")
    assert errors[1].severity == "error"


def test_parse_designmd_json_empty() -> None:
    """findings空・無効JSONはいずれも空リストを返す。"""
    assert pyfltr.parsing.entry.parse_errors("designmd", json.dumps({"findings": []})) == []
    assert pyfltr.parsing.entry.parse_errors("designmd", "not json") == []
    assert pyfltr.parsing.entry.parse_errors("designmd", "") == []


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
    errors = pyfltr.parsing.entry.parse_errors("lychee", output)
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
    assert pyfltr.parsing.entry.parse_errors("lychee", output) == []


def test_parse_lychee_json_invalid() -> None:
    """JSON解析失敗時は空リストを返す。"""
    assert pyfltr.parsing.entry.parse_errors("lychee", "not json") == []
    assert pyfltr.parsing.entry.parse_errors("lychee", "") == []


def test_parse_colloquial_check_without_replacement() -> None:
    """colloquial-check: 置換候補なしの`path:line:col: [match] -> (言い換えの方針) excerpt`形式をパースする。"""
    output = "docs/index.md:3:5: [ちょっと] -> (文意を保ったまま書き言葉の表現へ言い換える) 本文にちょっと該当する"
    errors = pyfltr.parsing.entry.parse_errors("colloquial-check", output)
    assert len(errors) == 1
    assert errors[0].file == "docs/index.md"
    assert errors[0].line == 3
    assert errors[0].col == 5
    assert errors[0].message == "[ちょっと] -> (文意を保ったまま書き言葉の表現へ言い換える) 本文にちょっと該当する"


def test_parse_colloquial_check_ignores_skip_notice() -> None:
    """colloquial-check: 読み込めないファイルのスキップ通知（標準エラー由来）を診断として拾わない。"""
    output = (
        "C:/work/bad.md: 読み込めないため口語表現の検査をスキップしました:"
        " 'utf-8' codec can't decode byte 0xff in position 0: invalid start byte。"
        "UTF-8で保存されていることと読み取り権限を確認してください\n"
        "docs/a.md:3:1: [唐突感] -> [論理の飛躍] 唐突感が否めない。\n"
    )
    errors = pyfltr.parsing.entry.parse_errors("colloquial-check", output)
    assert [(e.file, e.line, e.col) for e in errors] == [("docs/a.md", 3, 1)]


def test_parse_colloquial_check_with_replacement() -> None:
    """colloquial-check: 置換候補ありの`path:line:col: [match] -> [replacement] excerpt`形式をパースする。"""
    output = "docs/index.md:10:1: [唐突感] -> [論理の飛躍] 唐突感が否めない"
    errors = pyfltr.parsing.entry.parse_errors("colloquial-check", output)
    assert len(errors) == 1
    assert errors[0].line == 10
    assert errors[0].col == 1
    assert errors[0].message == "[唐突感] -> [論理の飛躍] 唐突感が否めない"


def test_parse_colloquial_check_multiple_lines() -> None:
    """colloquial-check: 複数件（改行区切り）を全件パースする。"""
    output = "docs/a.md:1:1: [ちょっと] 該当箇所1\ndocs/b.md:2:3: [ぶっちゃけ] 該当箇所2\n"
    errors = pyfltr.parsing.entry.parse_errors("colloquial-check", output)
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
    assert _diagnostic_fields(errors[0]) == (
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
    """results空・無効JSONはいずれも空リストを返す。"""
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
    """pinactのSARIFから、標準エラーの行が前置されていても全resultsを抽出する。

    前置行に`[`・`{`を含めても、JSONの断片としてSARIF本体と取り違えないことを確かめる。
    """
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
    """results空・無効JSONはいずれも空リストを返す。"""
    empty = json.dumps({"version": "2.1.0", "runs": [{"tool": {"driver": {"name": "pinact"}}, "results": []}]})
    assert pyfltr.parsing.entry.parse_errors("pinact", empty) == []
    assert pyfltr.parsing.entry.parse_errors("pinact", "not json") == []


def test_parse_semgrep_json_with_progress_output() -> None:
    """JSON前後に進捗表示が混在してもfindingを保持する。"""
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
    assert _diagnostic_fields(errors[0]) == (
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
    # more_info欠落時はメッセージ末尾に`(see ...)`が付かない
    assert "(see " not in errors[2].message


def test_parse_bandit_json_line_range_shapes_and_negative_columns() -> None:
    """line_rangeの空・単一・複数要素と、負の列オフセットを正規化する。"""
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
    """results空・無効JSONはいずれも空リストを返す。"""
    assert pyfltr.parsing.entry.parse_errors("bandit", json.dumps({"results": [], "errors": []})) == []
    assert pyfltr.parsing.entry.parse_errors("bandit", "not json") == []


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
    assert _diagnostic_fields(errors[0]) == (
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
    """violations空・無効JSONはいずれも空リストを返す。"""
    assert pyfltr.parsing.entry.parse_errors("sqlfluff", json.dumps([])) == []
    assert pyfltr.parsing.entry.parse_errors("sqlfluff", "not json") == []
    assert pyfltr.parsing.entry.parse_errors("sqlfluff", "") == []


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
    errors = pyfltr.parsing.entry.parse_errors("uv-audit", output)
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
    errors = pyfltr.parsing.entry.parse_errors("uv-audit", output)
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
    errors = pyfltr.parsing.entry.parse_errors("uv-audit", output)
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
    assert pyfltr.parsing.entry.parse_errors("uv-audit", found_zero) == []
    assert pyfltr.parsing.entry.parse_errors("uv-audit", "") == []
    assert pyfltr.parsing.entry.parse_errors("uv-audit", "no relevant lines here") == []


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
    errors = pyfltr.parsing.entry.parse_errors("npm-audit", output)
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
    errors = pyfltr.parsing.entry.parse_errors("pnpm-audit", output)
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
    errors = pyfltr.parsing.entry.parse_errors("yarn-audit", "\n".join(lines))
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
    assert pyfltr.parsing.entry.parse_errors(command, empty_output) == []


@pytest.mark.parametrize("command", ["npm-audit", "pnpm-audit", "yarn-audit"])
def test_parse_js_audit_invalid_input(command: str) -> None:
    """不正JSON・空文字ではJavaScript系監査ツールは空リストを返す（uv-auditはテキストのため別テスト）。"""
    assert pyfltr.parsing.entry.parse_errors(command, "not json") == []
    assert pyfltr.parsing.entry.parse_errors(command, "") == []


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


def test_parse_errors_mypy_extracts_rule() -> None:
    """mypyの末尾`[error-code]`がruleグループで抽出されrule_urlも付与される。"""
    output = 'src/foo.py:10: error: Name "x" is not defined  [name-defined]'
    errors = pyfltr.parsing.entry.parse_errors("mypy", output)
    assert len(errors) == 1
    assert errors[0].rule == "name-defined"
    assert errors[0].rule_url == "https://mypy.readthedocs.io/en/stable/_refs.html#code-name-defined"
    # messageに末尾の[rule]は含めない
    assert errors[0].message == 'Name "x" is not defined'


def test_parse_errors_mypy_without_rule() -> None:
    """mypyで末尾[code]が無い行はrule=Noneになる。"""
    output = "src/foo.py:10: error: Something went wrong"
    errors = pyfltr.parsing.entry.parse_errors("mypy", output)
    assert len(errors) == 1
    assert errors[0].rule is None
    assert errors[0].rule_url is None


def test_parse_errors_markdownlint_extracts_rule() -> None:
    """markdownlintのMDxxxがruleグループで抽出される。"""
    output = "docs/index.md:3 MD001/heading-increment Heading levels should only increment by one level at a time"
    errors = pyfltr.parsing.entry.parse_errors("markdownlint", output)
    assert len(errors) == 1
    assert errors[0].rule == "MD001"
    assert errors[0].rule_url == "https://github.com/DavidAnson/markdownlint/blob/main/doc/MD001.md"


def test_parse_errors_markdownlint_with_column() -> None:
    """markdownlint: 列番号を報告するルールでもfile・lineを正しく抽出する。

    列番号を許容しないと`_FILE`のドライブレター表記がファイル名の末尾へ侵入し、
    file・lineともに架空の値になる。
    """
    output = 'docs/index.md:3:32 MD059/descriptive-link-text Link text should be descriptive [Context: "[here]"]'
    errors = pyfltr.parsing.entry.parse_errors("markdownlint", output)
    assert len(errors) == 1
    assert errors[0].file == "docs/index.md"
    assert errors[0].line == 3
    assert errors[0].col == 32
    assert errors[0].rule == "MD059"


def test_parse_errors_markdownlint_with_column_on_windows_path() -> None:
    """markdownlint: Windowsドライブレター表記でも列番号付きの位置を正しく抽出する。

    ドライブレター表記の侵入が本不具合の根本原因のため、対象の表記そのものを検証する。
    """
    output = "C:/proj/docs/index.md:5:45 MD009/no-trailing-spaces Trailing spaces [Expected: 0; Actual: 1]"
    errors = pyfltr.parsing.entry.parse_errors("markdownlint", output)
    assert len(errors) == 1
    assert errors[0].file.endswith("docs/index.md")
    assert errors[0].line == 5
    assert errors[0].col == 45


def test_parse_errors_markdownlint_with_column_and_severity() -> None:
    """markdownlint: 列番号とseverityが同時に介在する形でも正しく抽出する。"""
    output = "docs/index.md:5:1 error MD009/no-trailing-spaces Trailing spaces [Expected: 0; Actual: 1]"
    errors = pyfltr.parsing.entry.parse_errors("markdownlint", output)
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
    errors = pyfltr.parsing.entry.parse_errors("shellcheck", output)
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
    errors = pyfltr.parsing.entry.parse_errors("eslint", output)
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
    errors = pyfltr.parsing.entry.parse_errors("textlint", output)
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
    errors = pyfltr.parsing.entry.parse_errors("shellcheck", output)
    assert errors[0].severity == "info"
