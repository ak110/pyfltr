"""テスターが報告する遅いテスト一覧の解析。

pytestは`--durations`指定時に遅いテストを一覧出力し、vitestはJSON reporter出力の
各テストへ所要時間を含める。本モジュールは両者を共通の構造化データへ変換し、
`CommandResult.slow_tests`経由で`tool.json`・JSONL・MCP・端末表示の各経路へ同じ意味で渡す。
診断（`ErrorLocation`）とは別種の情報のため`error_parser`とは分離する。
"""

import dataclasses
import json
import pathlib
import re
import typing

SLOW_TEST_LIMIT = 5
"""保持・露出する遅いテストの件数上限。

抽出関数と`CommandResult.merge`が秒数降順の上位この件数へ制限する。
正規化後の各露出経路は追加の件数制限をしない。
各経路が個別に制限すると、更新漏れが生じた経路だけ件数が一致しなくなるためである。
pytestの全件は実行アーカイブの`output.log`から参照できる。
"""

_HEADER_RE = re.compile(r"^=+\s+slowest(\s+\d+)?\s+durations\s+=+$")
_DURATION_RE = re.compile(r"^(\d+\.\d+)s\s+(setup|call|teardown)\s+(\S.*)$")
_SEPARATOR_RE = re.compile(r"^=+.*=+$")


@dataclasses.dataclass(frozen=True)
class SlowTest:
    """遅いテスト1件分。"""

    nodeid: str
    """テストの識別子。

    pytestはnodeid（パラメトライズにより空白を含むことがある）、
    vitestは`<ファイルパス>::<fullName>`形式を用いる。
    """
    phase: str
    """ツールが報告する計測区間の名称。

    pytestは`setup` / `call` / `teardown`、vitestは区間の区別を持たないため`test`を用いる。
    """
    seconds: float
    """当該区間の所要秒数。"""

    def to_dict(self) -> dict[str, typing.Any]:
        """JSON化可能なdictへ変換する。"""
        return {"nodeid": self.nodeid, "phase": self.phase, "seconds": self.seconds}


def take_slowest(results: list[SlowTest]) -> list[SlowTest]:
    """秒数降順の上位`SLOW_TEST_LIMIT`件へ絞る。"""
    return sorted(results, key=lambda t: t.seconds, reverse=True)[:SLOW_TEST_LIMIT]


def parse_pytest_durations(output: str) -> list[SlowTest]:
    """pytest出力からdurations節を抽出する。

    見出し行（`===== slowest N durations =====`または`===== slowest durations =====`）以降の
    duration行を収集し、次の区切り行で当該節の収集を終える。
    捕捉出力へ子pytestのdurations節が混入する場合は、親pytestが末尾に出力する最後の節を採用する。
    節が無い場合・閾値未満で項目が隠された場合（`(N durations < Xs hidden.)`）は空リストを返す。
    行書式はpytestの`_pytest/runner.py`が生成する`f"{duration:02.2f}s {when:<8} {nodeid}"`に対応する。
    xdistの有無で書式は変わらない。
    """
    results: list[SlowTest] = []
    in_section = False
    for line in output.splitlines():
        stripped = line.strip()
        if _HEADER_RE.match(stripped):
            results = []
            in_section = True
            continue
        if not in_section:
            continue
        match = _DURATION_RE.match(stripped)
        if match is not None:
            results.append(SlowTest(nodeid=match.group(3), phase=match.group(2), seconds=float(match.group(1))))
            continue
        if not stripped:
            continue
        if _SEPARATOR_RE.match(stripped):
            in_section = False
    return take_slowest(results)


def parse_vitest_durations(output: str, *, base_cwd: pathlib.Path | None = None) -> list[SlowTest]:
    """vitestのJSON reporter出力から各テストの所要時間を抽出する。

    入力は`pyfltr.command.vitest.execute_vitest`が`--outputFile.json`で取得した内容であり、
    `testResults[].assertionResults[].duration`（ミリ秒）を秒へ換算する。
    `duration`を欠く要素は対象外とする（vitestの版・設定により欠落しうる）。
    nodeidは`<ファイルパス>::<fullName>`とし、ファイルパスは`base_cwd`基準で相対化する。
    `base_cwd`配下でないパスは絶対パスのまま保持する。
    JSON解析に失敗した場合は空リストを返す（stdoutフォールバック経路では解析対象にならない）。
    """
    try:
        data = json.loads(output)
    except (ValueError, TypeError):
        return []
    if not isinstance(data, dict):
        return []
    test_results = data.get("testResults", [])
    if not isinstance(test_results, list):
        return []
    results: list[SlowTest] = []
    for entry in test_results:
        if not isinstance(entry, dict):
            continue
        file_path = _relativize(str(entry.get("name", "") or ""), base_cwd)
        assertions = entry.get("assertionResults", [])
        if not isinstance(assertions, list):
            continue
        for assertion in assertions:
            if not isinstance(assertion, dict):
                continue
            duration = assertion.get("duration")
            if not isinstance(duration, (int, float)) or isinstance(duration, bool):
                continue
            full_name = str(assertion.get("fullName", "") or "")
            nodeid = f"{file_path}::{full_name}" if file_path and full_name else file_path or full_name
            if not nodeid:
                continue
            results.append(SlowTest(nodeid=nodeid, phase="test", seconds=float(duration) / 1000.0))
    return take_slowest(results)


def _relativize(file_path: str, base_cwd: pathlib.Path | None) -> str:
    """ファイルパスを`base_cwd`基準の相対パスへ変換する（配下でなければ元のまま返す）。"""
    if not file_path or base_cwd is None:
        return file_path
    try:
        return pathlib.Path(file_path).relative_to(base_cwd).as_posix()
    except ValueError:
        return file_path


def format_slow_tests(slow_tests: list[SlowTest]) -> list[str]:
    """text・TUI表示用に1行ずつ整形する（入力は正規化済み）。"""
    return [f"{t.seconds:.2f}s {t.phase} {t.nodeid}" for t in slow_tests]
