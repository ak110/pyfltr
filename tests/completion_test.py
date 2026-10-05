"""completion.py のテスト。

MCP `run`とCLI JSONLが共有する完了判定の区分表を固定する。
各出力経路への接続は`tests/mcp_test.py`と`tests/llm_output_test.py`が検証する。
"""

import dataclasses

import pytest

import pyfltr.command.completion
import pyfltr.command.core_
import pyfltr.config.config
from tests.conftest import make_command_result as _make_result


def _timeout(command: str) -> pyfltr.command.core_.CommandResult:
    result = _make_result(command, returncode=-9)
    result.timeout_exceeded = True
    return result


def _no_target(command: str) -> pyfltr.command.core_.CommandResult:
    """対象ファイルが無いため起動しなかった結果。"""
    result = _make_result(command, returncode=None, files=0)
    result.not_applicable = True
    return result


@dataclasses.dataclass(frozen=True)
class _Case:
    results: list[pyfltr.command.core_.CommandResult]
    files_reached: int
    expected: str
    expected_completed: list[str]
    expected_incomplete: list[str]
    requested: tuple[str, ...] = ()
    unmet: tuple[str, ...] = ()
    missing: tuple[str, ...] = ()
    excluded: tuple[str, ...] = ()


_CASES: dict[str, _Case] = {
    "診断なしの完了": _Case([_make_result("mypy", returncode=0)], 3, "completed", ["mypy"], []),
    "診断ありの完了": _Case([_make_result("mypy", returncode=1)], 3, "completed", ["mypy"], []),
    "warning格下げとformatterの書き換えも完了": _Case(
        [
            _make_result("ruff-format", returncode=1, command_type="formatter"),
            dataclasses.replace(_make_result("pylint", returncode=1), severity="warning"),
        ],
        2,
        "completed",
        ["ruff-format", "pylint"],
        [],
    ),
    "部分到達（不在対象あり）": _Case([_make_result("mypy", returncode=0)], 1, "incomplete", ["mypy"], [], missing=("a.py",)),
    "部分到達（全除外対象あり）": _Case(
        [_make_result("mypy", returncode=0)], 1, "incomplete", ["mypy"], [], excluded=("b.py",)
    ),
    "一部コマンドの中断によるskip": _Case(
        [_make_result("mypy", returncode=0), _make_result("pytest", returncode=None, files=0)],
        1,
        "incomplete",
        ["mypy"],
        ["pytest"],
    ),
    "一部コマンドのツール解決失敗": _Case(
        [_make_result("mypy", returncode=0), _make_result("textlint", returncode=None, files=0, resolution_failed=True)],
        1,
        "incomplete",
        ["mypy"],
        ["textlint"],
    ),
    "明示していないコマンドの対象なしskipは数えない": _Case(
        [_make_result("mypy", returncode=0), _no_target("textlint")], 1, "completed", ["mypy"], []
    ),
    "明示したコマンドの対象なしskipは未完了": _Case(
        [_make_result("mypy", returncode=0), _no_target("textlint")],
        1,
        "incomplete",
        ["mypy"],
        ["textlint"],
        requested=("mypy", "textlint"),
    ),
    "全コマンドが対象なし（拡張子の不一致）": _Case([_no_target("textlint")], 1, "not_reached", [], []),
    "時間上限超過だけでも開始済みならincomplete": _Case([_timeout("pytest")], 1, "incomplete", [], ["pytest"]),
    "無効化コマンドの指定": _Case([_make_result("mypy", returncode=0)], 1, "incomplete", ["mypy"], ["ec"], unmet=("ec",)),
    "全コマンドskip": _Case(
        [_make_result("mypy", returncode=None, files=0), _make_result("pylint", returncode=None, files=0)],
        0,
        "not_reached",
        [],
        ["pylint", "mypy"],
    ),
    "全対象不在（結果なし）": _Case([], 0, "not_reached", [], [], missing=("a.py",)),
    "全対象除外": _Case([_make_result("mypy", returncode=None, files=0)], 0, "not_reached", [], ["mypy"], excluded=("b.py",)),
    "ツール解決失敗のみ": _Case(
        [_make_result("textlint", returncode=None, files=0, resolution_failed=True)], 1, "not_reached", [], ["textlint"]
    ),
}


@pytest.mark.parametrize("case", list(_CASES.values()), ids=list(_CASES))
def test_evaluate_completion(case: _Case) -> None:
    """結果と対象の到達状況から、WIの区分表どおりの完了区分と一覧を導出する。"""
    config = pyfltr.config.config.create_default_config()

    completion = pyfltr.command.completion.evaluate_completion(
        case.results,
        config,
        files_reached=case.files_reached,
        requested_commands=list(case.requested),
        unmet_commands=list(case.unmet),
        missing_targets=list(case.missing),
        fully_excluded_files=list(case.excluded),
    )

    assert completion.completion == case.expected
    assert completion.files_reached == case.files_reached
    assert list(completion.completed_commands) == case.expected_completed
    assert list(completion.incomplete_commands) == case.expected_incomplete
    assert completion.missing_targets == case.missing
    assert completion.fully_excluded_files == case.excluded
