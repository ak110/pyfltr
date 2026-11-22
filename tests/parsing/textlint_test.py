import json

import pyfltr.diagnostics
import pyfltr.output.diagnostics
import pyfltr.parsing.entry
import pyfltr.parsing.pytest
import pyfltr.tools


def test_parse_textlint_json_fix_none() -> None:
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
    assert errors[0].hint is None


def test_parse_textlint_json_hint_for_sentence_length() -> None:
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
    assert errors[0].end_line is None
    assert errors[0].end_col is None


def test_parse_textlint_json_populates_end_position() -> None:
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
    assert errors[0].message == "Long sentence"


def test_parse_textlint_json_sentence_length_hint_excludes_col_note() -> None:
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
