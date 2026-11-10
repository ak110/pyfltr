import json

import pyfltr.diagnostics
import pyfltr.output.diagnostics
import pyfltr.parsing.entry
import pyfltr.parsing.pytest
import pyfltr.tools


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
    """案内があるtextlint規則にヒントが付く。"""
    for rule_id in (
        "ja-technical-writing/max-comma",
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


def test_parse_textlint_json_hint_for_max_comma() -> None:
    """max-commaの位置と同じ段落内での探し方を案内する。"""
    output = json.dumps(
        [
            {
                "filePath": "a.md",
                "messages": [
                    {
                        "line": 1,
                        "column": 2,
                        "message": "Too many commas",
                        "ruleId": "ja-technical-writing/max-comma",
                        "severity": 2,
                    }
                ],
            }
        ]
    )
    errors = pyfltr.parsing.entry.parse_errors("textlint", output)
    assert len(errors) == 1
    assert (errors[0].line, errors[0].col) == (1, 2)
    assert errors[0].hint is not None
    assert "sentence-relative" in errors[0].hint
    assert "preceding line" in errors[0].hint
    assert "same paragraph" in errors[0].hint


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
