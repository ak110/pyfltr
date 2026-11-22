import dataclasses
import json
import pathlib
import re
import typing

import pyfltr.parsing.pytest
import pyfltr.parsing.pytest_nested

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
    """対象の区間の所要秒数。"""

    def to_dict(self) -> dict[str, typing.Any]:
        return {"nodeid": self.nodeid, "phase": self.phase, "seconds": self.seconds}


def take_slowest(results: list[SlowTest]) -> list[SlowTest]:
    return sorted(results, key=lambda t: t.seconds, reverse=True)[:SLOW_TEST_LIMIT]


def parse_pytest_durations(output: str) -> list[SlowTest]:
    results: list[SlowTest] = []
    in_section = False
    for line in pyfltr.parsing.pytest_nested.mask_pytest_captured_child_runs(output).splitlines():
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
    if not file_path or base_cwd is None:
        return file_path
    try:
        return pathlib.Path(file_path).relative_to(base_cwd).as_posix()
    except ValueError:
        return file_path


def format_slow_tests(slow_tests: list[SlowTest]) -> list[str]:
    return [f"{t.seconds:.2f}s {t.phase} {t.nodeid}" for t in slow_tests]
