"""ツール固有の診断解析。"""

import json
import re

import pyfltr.link_checks
import pyfltr.paths
import pyfltr.rule_urls
import pyfltr.tools
from pyfltr.diagnostics import ErrorLocation
from pyfltr.parsing.common import (
    json_int,
    json_list_field,
    normalize_severity,
    one_based_offset,
    parse_file_messages_format,
    parse_with_pattern,
    to_inclusive_end_position,
    try_json_loads,
)


def parse_eslint_json(output: str) -> list[ErrorLocation]:
    """ESLint --format json出力をパース。

    ESLint 9系以降でcompact / unixなどのコアフォーマッタが除去されたため、
    pyfltrでは`--format json`を使う。出力は以下のような配列。

    [
      {
        "filePath": "/abs/src/foo.js",
        "messages": [
          {"line": 10, "column": 5, "message": "...", "ruleId": "no-unused-vars", "severity": 2}
        ]
      }
    ]

    stderr混入等でパースに失敗した場合は空リストを返す（regexパーサーが
    マッチしない時の挙動と揃える）。
    """
    data = try_json_loads(output)
    if not isinstance(data, list):
        return []

    def _msg_to_location(msg: dict, file_path: str) -> ErrorLocation | None:
        line = msg.get("line")
        if not isinstance(line, int):
            return None
        raw_col = msg.get("column")
        col = raw_col if isinstance(raw_col, int) else None
        end_line = json_int(msg.get("endLine"))
        end_col = json_int(msg.get("endColumn"))
        rule_id = str(msg.get("ruleId") or "")
        text = str(msg.get("message", ""))
        message = f"{text} ({rule_id})" if rule_id else text
        # ESLintのJSONはautofixがある場合のみ`fix`オブジェクトが付与される。
        # 自動修正情報の有無を報告するツールなので、欠落時は`"none"`として
        # 「自動修正不可」を明示する（`None`省略との区別を維持）。
        fix_value = "safe" if msg.get("fix") else "none"
        rule = rule_id or None
        end_line, end_col = to_inclusive_end_position(line, end_line, end_col)
        return ErrorLocation(
            file=pyfltr.paths.normalize_separators(file_path),
            line=line,
            col=col,
            command="eslint",
            message=message.strip(),
            rule=rule,
            severity=normalize_severity(msg.get("severity")),
            fix=fix_value,
            rule_url=pyfltr.rule_urls.build_rule_url("eslint", rule),
            end_line=end_line,
            end_col=end_col,
        )

    return parse_file_messages_format(data, _msg_to_location)


def parse_shellcheck_json(output: str) -> list[ErrorLocation]:
    """Shellcheck -f json出力をパース。JSON解析失敗時はregexにフォールバック。"""
    data = try_json_loads(output)
    if not isinstance(data, list):
        return parse_with_pattern(
            "shellcheck", output, pyfltr.tools.BUILTIN_COMMANDS["shellcheck"].require_diagnostic_pattern()
        )
    results: list[ErrorLocation] = []
    for entry in data:
        if not isinstance(entry, dict):
            continue
        line = entry.get("line")
        if not isinstance(line, int):
            continue
        raw_col = entry.get("column")
        col = raw_col if isinstance(raw_col, int) else None
        end_line = json_int(entry.get("endLine"))
        end_col = json_int(entry.get("endColumn"))
        code = entry.get("code")
        rule = f"SC{code}" if isinstance(code, int) else None
        # shellcheckはJSON出力で自動修正情報の有無を明示する。
        fix_value = "safe" if entry.get("fix") else "none"
        end_line, end_col = to_inclusive_end_position(line, end_line, end_col)
        results.append(
            ErrorLocation(
                file=pyfltr.paths.normalize_separators(str(entry.get("file", ""))),
                line=line,
                col=col,
                command="shellcheck",
                message=str(entry.get("message", "")),
                rule=rule,
                severity=normalize_severity(entry.get("level")),
                fix=fix_value,
                rule_url=pyfltr.rule_urls.build_rule_url("shellcheck", rule),
                end_line=end_line,
                end_col=end_col,
            )
        )
    return results


def parse_typos_jsonl(output: str) -> list[ErrorLocation]:
    """Typos --format=json出力をパース（JSON Lines形式）。解析失敗時はregexにフォールバック。"""
    results: list[ErrorLocation] = []
    any_parsed = False
    for line in output.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            entry = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(entry, dict):
            continue
        # typosのJSONエントリにはtypeフィールドがある。typo以外（binary等）はスキップ
        if entry.get("type") not in ("typo", None):
            continue
        any_parsed = True
        line_num = entry.get("line_num")
        if not isinstance(line_num, int):
            continue
        typo = str(entry.get("typo", ""))
        corrections = entry.get("corrections", [])
        if isinstance(corrections, list) and corrections:
            correction_str = ", ".join(str(c) for c in corrections)
            message = f"`{typo}` -> `{correction_str}`"
            fix_value: str | None = "safe"
        else:
            # typosは自動修正候補の有無を明示的に返すため、候補なしは`"none"`。
            message = f"`{typo}`"
            fix_value = "none"
        results.append(
            ErrorLocation(
                file=pyfltr.paths.normalize_separators(str(entry.get("path", ""))),
                line=line_num,
                col=None,
                command="typos",
                message=message,
                severity="warning",
                fix=fix_value,
            )
        )
    if not any_parsed and output.strip():
        return parse_with_pattern("typos", output, pyfltr.tools.BUILTIN_COMMANDS["typos"].require_diagnostic_pattern())
    return results


def parse_pinact_sarif(output: str) -> list[ErrorLocation]:
    """`pinact run --format sarif`のSARIF出力をパースする。

    出力例::

        {
          "runs": [
            {
              "results": [
                {
                  "ruleId": "parse-error",
                  "level": "error",
                  "message": {"text": "failed to handle a line: action can't be pinned"},
                  "locations": [
                    {"physicalLocation": {"artifactLocation": {"uri": ".github/workflows/ci.yaml"},
                                          "region": {"startLine": 7}}}
                  ]
                }
              ]
            }
          ]
        }

    pinactは標準エラーへ人間向けの行を書き、pyfltrは標準エラーを標準出力へ合流させるため、
    SARIFの前に別の行が並ぶ。JSON本体は`try_json_loads`で取り出す。
    JSON解析失敗時は空リストを返す。
    """
    runs = json_list_field(output, "runs")
    if runs is None:
        return []
    results: list[ErrorLocation] = []
    for run in runs:
        if not isinstance(run, dict) or not isinstance(run.get("results"), list):
            continue
        for entry in run["results"]:
            if not isinstance(entry, dict):
                continue
            locations = entry.get("locations")
            if not isinstance(locations, list) or not locations or not isinstance(locations[0], dict):
                continue
            physical = locations[0].get("physicalLocation")
            if not isinstance(physical, dict):
                continue
            artifact = physical.get("artifactLocation")
            region = physical.get("region")
            if not isinstance(artifact, dict) or not isinstance(region, dict):
                continue
            line = json_int(region.get("startLine"))
            if line is None:
                continue
            message = entry.get("message")
            rule = str(entry.get("ruleId", "") or "") or None
            results.append(
                ErrorLocation(
                    file=pyfltr.paths.normalize_separators(str(artifact.get("uri", ""))),
                    line=line,
                    col=json_int(region.get("startColumn")),
                    command="pinact",
                    message=str(message.get("text", "") or "") if isinstance(message, dict) else "",
                    rule=rule,
                    severity=normalize_severity(entry.get("level")),
                )
            )
    return results


def parse_designmd_json(output: str) -> list[ErrorLocation]:
    """`@google/design.md lint`のJSON出力をパースする。

    出力例::

        {
          "findings": [
            {
              "severity": "warning",
              "path": "components.button-primary",
              "message": "..."
            }
          ],
          "summary": {"errors": 0, "warnings": 1, "info": 1}
        }

    `path`はDESIGN.md内のJSONパス（プロパティ参照）であり、ファイルシステムのパスではない。
    対象ファイル名は仕様上`DESIGN.md`固定のため、`ErrorLocation.file`にはそれを格納し、
    JSONパスは`message`先頭へ併記する。`line`は仕様上提供されないため`0`を格納する。
    JSON解析失敗時は空リストを返す。
    """
    findings = json_list_field(output, "findings")
    if findings is None:
        return []
    results: list[ErrorLocation] = []
    for entry in findings:
        if not isinstance(entry, dict):
            continue
        json_path = str(entry.get("path", "") or "")
        text = str(entry.get("message", "") or "")
        message = f"{json_path}: {text}" if json_path else text
        results.append(
            ErrorLocation(
                file="DESIGN.md",
                line=0,
                col=None,
                command="designmd",
                message=message,
                severity=normalize_severity(entry.get("severity")),
            )
        )
    return results


def parse_lychee_json(output: str) -> list[ErrorLocation]:
    """`lychee --format json`のJSON出力をパースする。

    出力例::

        {
          "total": 100, "successful": 80, ...,
          "error_map": {
            "src/foo.md": [
              {
                "url": "https://example.com/dead",
                "status": {"text": "Error: 404 - Not Found", "code": 404},
                ...
              }
            ]
          }
        }

    `error_map`と`timeout_map`は「ファイルパス → 失敗レスポンス配列」のmap。各失敗から`url`/`status.text`を抽出し、
    `ErrorLocation.message`へ整形する。本パーサーは`line=1`固定とし、JSONの`span`による位置を取り込まない。
    HTTP(S)の5xxと応答タイムアウトだけをwarning、その他をerrorとして取り込む。
    JSON解析失敗時は空リストを返す。
    """
    data = try_json_loads(output)
    if not isinstance(data, dict):
        return []
    error_map = data.get("error_map", {})
    timeout_map = data.get("timeout_map", {})
    if not isinstance(error_map, dict):
        return []
    if not isinstance(timeout_map, dict):
        timeout_map = {}
    results: list[ErrorLocation] = []
    for file_path, entries, is_timeout in [(path, items, False) for path, items in error_map.items()] + [
        (path, items, True) for path, items in timeout_map.items()
    ]:
        if not isinstance(entries, list):
            continue
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            url = str(entry.get("url", "") or "")
            status_obj = entry.get("status")
            status_text = ""
            if isinstance(status_obj, dict):
                status_text = str(status_obj.get("text", "") or "")
            elif isinstance(status_obj, str):
                status_text = status_obj
            message = f"{url} -> {status_text}" if status_text else url
            results.append(
                ErrorLocation(
                    file=pyfltr.paths.normalize_separators(str(file_path)),
                    line=1,
                    col=None,
                    command="lychee",
                    message=message,
                    severity="warning" if pyfltr.link_checks.is_transient_failure(entry, timeout=is_timeout) else "error",
                )
            )
    return results


def parse_semgrep_json(output: str) -> list[ErrorLocation]:
    """`semgrep scan --json`のJSON出力をパースする。

    出力例::

        {
          "results": [
            {
              "check_id": "rules.foo",
              "path": "src/foo.py",
              "start": {"line": 18, "col": 9},
              "end": {...},
              "extra": {"severity": "ERROR", "message": "..."}
            }
          ],
          ...
        }

    JSON解析失敗時は空リストを返す。
    """
    raw_results = json_list_field(output, "results")
    if raw_results is None:
        return []
    results: list[ErrorLocation] = []
    for entry in raw_results:
        if not isinstance(entry, dict):
            continue
        start = entry.get("start", {})
        if not isinstance(start, dict):
            continue
        line = start.get("line")
        if not isinstance(line, int):
            continue
        raw_col = start.get("col")
        col = raw_col if isinstance(raw_col, int) else None
        end = entry.get("end")
        end_line = json_int(end.get("line")) if isinstance(end, dict) else None
        end_col = json_int(end.get("col")) if isinstance(end, dict) else None
        extra = entry.get("extra", {}) if isinstance(entry.get("extra"), dict) else {}
        rule = str(entry.get("check_id", "") or "") or None
        end_line, end_col = to_inclusive_end_position(line, end_line, end_col)
        results.append(
            ErrorLocation(
                file=pyfltr.paths.normalize_separators(str(entry.get("path", ""))),
                line=line,
                col=col,
                command="semgrep",
                message=str(extra.get("message", "") or ""),
                rule=rule,
                severity=normalize_severity(extra.get("severity")),
                end_line=end_line,
                end_col=end_col,
            )
        )
    return results


def parse_bandit_json(output: str) -> list[ErrorLocation]:
    """`bandit -f json`のJSON出力をパースする。

    出力例::

        {
          "results": [
            {
              "filename": "src/foo.py",
              "line_number": 10,
              "col_offset": 4,
              "test_id": "B101",
              "test_name": "assert_used",
              "issue_severity": "LOW",
              "issue_text": "Use of assert detected.",
              "more_info": "https://bandit.readthedocs.io/.../b101.html"
            }
          ]
        }

    JSON解析失敗時は空リストを返す。
    """
    raw_results = json_list_field(output, "results")
    if raw_results is None:
        return []
    results: list[ErrorLocation] = []
    for entry in raw_results:
        if not isinstance(entry, dict):
            continue
        line = entry.get("line_number")
        if not isinstance(line, int):
            continue
        col = one_based_offset(entry.get("col_offset"))
        line_range = entry.get("line_range")
        end_line = json_int(line_range[-1]) if isinstance(line_range, list) and line_range else None
        end_col = one_based_offset(entry.get("end_col_offset"))
        message = str(entry.get("issue_text", "") or "")
        more_info = entry.get("more_info")
        if isinstance(more_info, str) and more_info:
            message = f"{message} (see {more_info})" if message else f"(see {more_info})"
        rule = str(entry.get("test_id", "") or "") or None
        end_line, end_col = to_inclusive_end_position(line, end_line, end_col)
        results.append(
            ErrorLocation(
                file=pyfltr.paths.normalize_separators(str(entry.get("filename", ""))),
                line=line,
                col=col,
                command="bandit",
                message=message,
                rule=rule,
                severity=normalize_severity(entry.get("issue_severity")),
                end_line=end_line,
                end_col=end_col,
            )
        )
    return results


def parse_sqlfluff_json(output: str) -> list[ErrorLocation]:
    """`sqlfluff lint --format=json`のJSON出力をパースする。

    出力例::

        [
          {
            "filepath": "src/foo.sql",
            "violations": [
              {
                "start_line_no": 10,
                "start_line_pos": 5,
                "code": "L001",
                "name": "...",
                "description": "...",
                "warning": false
              }
            ]
          }
        ]

    JSON解析失敗時は空リストを返す。
    """
    data = try_json_loads(output)
    if not isinstance(data, list):
        return []

    def _msg_to_location(violation: dict, file_path: str) -> ErrorLocation | None:
        line = violation.get("start_line_no")
        if not isinstance(line, int):
            return None
        raw_col = violation.get("start_line_pos")
        col = raw_col if isinstance(raw_col, int) else None
        end_line = json_int(violation.get("end_line_no"))
        end_col = json_int(violation.get("end_line_pos"))
        rule = str(violation.get("code", "") or "") or None
        severity = "warning" if violation.get("warning") else "error"
        end_line, end_col = to_inclusive_end_position(line, end_line, end_col)
        return ErrorLocation(
            file=pyfltr.paths.normalize_separators(file_path),
            line=line,
            col=col,
            command="sqlfluff",
            message=str(violation.get("description", "") or ""),
            rule=rule,
            severity=severity,
            end_line=end_line,
            end_col=end_col,
        )

    return parse_file_messages_format(
        data,
        _msg_to_location,
        file_key="filepath",
        messages_key="violations",
        file_to_string=lambda value: str(value or ""),
    )


def parse_glab_ci_lint(output: str) -> list[ErrorLocation]:
    """`glab ci lint`出力をパース。

    glabは行番号を出力しないため、検出した各エラーメッセージを`line=1`固定の
    `ErrorLocation`として生成する。

    無効CI出力例::

        Validating...
        .gitlab-ci.yml is invalid

        - jobs:test config contains unknown keys: foo
        - root config contains unknown keys: bar

    有効CI出力では`✓ CI/CD YAML is valid!`のみが出力されるため空リストを返す。
    """
    results: list[ErrorLocation] = []
    file_path: str | None = None
    invalid_re = re.compile(r"^\s*(?P<file>\S+)\s+is\s+invalid\b")
    for raw_line in output.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        match = invalid_re.match(line)
        if match is not None:
            file_path = match.group("file")
            continue
        if file_path is None:
            continue
        # 番号付きエラー行（`- xxx` / `1. xxx`）のリストマーカーを除去する。
        message = re.sub(r"^(?:[-*•]|\d+[.)])\s+", "", line)
        if not message:
            continue
        results.append(
            ErrorLocation(
                file=pyfltr.paths.normalize_separators(file_path),
                line=1,
                col=None,
                command="glab-ci-lint",
                message=message,
            )
        )
    return results


def parse_vitest_json(output: str) -> list[ErrorLocation]:
    """Vitest `--reporter=json` 出力をパースする。

    入力はJest互換のJSONで、`pyfltr.command.vitest.execute_vitest`が
    `--outputFile.json=<tmpfile>` で取得したファイル内容を文字列として渡す。
    テキスト出力のregex解析より構造化情報が安定して得られる経路を採用する
    （Vitestデフォルト出力はバージョン差・カラー制御文字・テスト名のネスト表示など変動要素が多い）。

    出力構造の主要部分::

        {
          "testResults": [
            {
              "name": "/abs/path/to/foo.test.ts",
              "assertionResults": [
                {
                  "status": "failed",
                  "fullName": "Suite > nested > case",
                  "failureMessages": ["..."],
                  "location": {"line": 12, "column": 3}
                }
              ]
            }
          ]
        }

    各失敗 `assertionResult` を1件の `ErrorLocation` へ変換する。pytestのカスタムパーサーと
    同じく `fullName` を `message` 先頭へ併記する（locationだけでは識別性が乏しく、
    `expect`系のassertion失敗メッセージはテスト名と組み合わせて初めて意味を持つため）。

    `location` 欠落時は `line=1` 固定でフォールバックする
    （Vitestの新しいランナー以外では `includeTaskLocation` 設定次第で欠落するため）。
    `failureMessages` が空のときも空メッセージで1件を生成し、失敗の存在自体を保持する。

    テストファイルの評価時の例外（import・setupの失敗など）では、`testResults` 要素自体の
    `status` が `failed` で、失敗した `assertionResult` を持たない。この場合は要素の
    `message` 先頭行（空なら空文字列）を本文とするファイル診断を `line=1` で1件生成する。
    失敗 `assertionResult` を持つ要素では同じ失敗を二重に数えないよう要素単位の診断を追加しない。
    `name` を持たない要素はファイルを推定せず、元出力に失敗を委ねる。

    JSON解析失敗時は空リストを返す。`command.message` フォールバック経路で従来通り
    stdout末尾が `command.message` へ格納される。
    """
    test_results = json_list_field(output, "testResults")
    if test_results is None:
        return []
    results: list[ErrorLocation] = []
    for entry in test_results:
        if not isinstance(entry, dict):
            continue
        file_path = str(entry.get("name", "") or "")
        assertions = entry.get("assertionResults", [])
        if not isinstance(assertions, list):
            assertions = []
        has_failed_assertion = False
        for assertion in assertions:
            if not isinstance(assertion, dict):
                continue
            if assertion.get("status") != "failed":
                continue
            has_failed_assertion = True
            location = assertion.get("location")
            line: int = 1
            col: int | None = None
            if isinstance(location, dict):
                raw_line = location.get("line")
                if isinstance(raw_line, int):
                    line = raw_line
                raw_col = location.get("column")
                if isinstance(raw_col, int):
                    col = raw_col
            test_name = str(assertion.get("fullName", "") or "")
            failure_messages = assertion.get("failureMessages", [])
            raw_message = ""
            if isinstance(failure_messages, list) and failure_messages:
                first = failure_messages[0]
                if isinstance(first, str):
                    raw_message = first.splitlines()[0] if first else ""
            message = f"{test_name}: {raw_message}" if test_name else raw_message
            results.append(
                ErrorLocation(
                    file=pyfltr.paths.normalize_separators(file_path),
                    line=line,
                    col=col,
                    command="vitest",
                    message=message,
                )
            )
        if entry.get("status") == "failed" and file_path and not has_failed_assertion:
            suite_message = entry.get("message")
            first_line = suite_message.splitlines()[0] if isinstance(suite_message, str) and suite_message else ""
            results.append(
                ErrorLocation(
                    file=pyfltr.paths.normalize_separators(file_path),
                    line=1,
                    col=None,
                    command="vitest",
                    message=first_line,
                )
            )
    return results
