"""pytestとvitestの遅いテスト一覧解析のテスト。"""

import json
import pathlib

import pytest

import pyfltr.command.slow_tests


def _pytest_output(*lines: str, header: str = "======= slowest 10 durations =======") -> str:
    """durations節を持つpytest出力を組み立てる。"""
    return "\n".join((header, *lines, "======= 3 passed in 1.23s ======="))


def _vitest_output(test_results: list[dict[str, object]]) -> str:
    """vitest JSON reporter出力を組み立てる。"""
    return json.dumps({"testResults": test_results})


def test_parse_numbered_header() -> None:
    """件数付き見出しのdurations節を抽出する。"""
    result = pyfltr.command.slow_tests.parse_pytest_durations(_pytest_output("0.51s call     tests/a_test.py::test_x"))
    assert result == [pyfltr.command.slow_tests.SlowTest("tests/a_test.py::test_x", "call", 0.51)]


def test_parse_unnumbered_header() -> None:
    """件数なし見出し（--durations=0）のdurations節を抽出する。"""
    result = pyfltr.command.slow_tests.parse_pytest_durations(
        _pytest_output("0.51s call     tests/a_test.py::test_x", header="======= slowest durations =======")
    )
    assert len(result) == 1


@pytest.mark.parametrize("phase", ["setup", "call", "teardown"])
def test_parse_each_phase(phase: str) -> None:
    """setup / call / teardown の各フェーズ行を抽出する。"""
    result = pyfltr.command.slow_tests.parse_pytest_durations(_pytest_output(f"0.10s {phase:<8} tests/a_test.py::test_x"))
    assert result[0].phase == phase


def test_parse_nodeid_with_space() -> None:
    """パラメトライズで空白を含むnodeidを保持する。"""
    result = pyfltr.command.slow_tests.parse_pytest_durations(_pytest_output("0.20s call     tests/a_test.py::test_x[a b]"))
    assert result[0].nodeid == "tests/a_test.py::test_x[a b]"


def test_parse_large_and_zero_seconds() -> None:
    """秒の桁が伸びる行と0埋めされる行を抽出する。"""
    result = pyfltr.command.slow_tests.parse_pytest_durations(
        _pytest_output(
            "123.45s call     tests/a_test.py::test_slow",
            "0.00s call     tests/a_test.py::test_fast",
        )
    )
    assert [test.seconds for test in result] == [123.45, 0.0]


def test_parse_hidden_only() -> None:
    """閾値未満で項目が隠された節は0件になる。"""
    result = pyfltr.command.slow_tests.parse_pytest_durations(_pytest_output("(10 durations < 5s hidden.)"))
    assert not result


def test_parse_hidden_only_without_durations_min() -> None:
    """--durations-min未指定時のhidden行（-vv案内付き）も0件になる。"""
    result = pyfltr.command.slow_tests.parse_pytest_durations(
        _pytest_output("(10 durations < 0.005s hidden.)  Use -vv to show these durations.")
    )
    assert not result


def test_parse_no_section() -> None:
    """durations節が無い出力は0件になる。"""
    assert not pyfltr.command.slow_tests.parse_pytest_durations("3 passed in 1.23s")


def test_parse_malformed_line_is_ignored() -> None:
    """秒が欠ける行は抽出対象から外れる。"""
    result = pyfltr.command.slow_tests.parse_pytest_durations(_pytest_output("call     tests/a_test.py::test_x"))
    assert not result


def test_parse_stops_at_next_separator() -> None:
    """次の区切り行で収集を終える。"""
    output = "\n".join(
        (
            "======= slowest 1 durations =======",
            "0.51s call     tests/a_test.py::test_x",
            "======= 1 passed in 0.51s =======",
            "9.99s call     tests/a_test.py::test_not_in_section",
        )
    )
    result = pyfltr.command.slow_tests.parse_pytest_durations(output)
    assert [test.nodeid for test in result] == ["tests/a_test.py::test_x"]


def test_parse_xdist_output() -> None:
    """xdist有効時の出力でも同じ結果を得る。"""
    output = "bringing up nodes...\n" + _pytest_output("0.51s call     tests/a_test.py::test_x")
    result = pyfltr.command.slow_tests.parse_pytest_durations(output)
    assert len(result) == 1


def test_parse_pytest_limits_entries() -> None:
    """上限を超えるduration行は秒数降順の上位のみを残す。"""
    lines = [f"{seconds:.2f}s call     tests/a_test.py::test_{seconds}" for seconds in range(7)]
    result = pyfltr.command.slow_tests.parse_pytest_durations(_pytest_output(*lines))
    assert [test.seconds for test in result] == [6.0, 5.0, 4.0, 3.0, 2.0]


def test_parse_pytest_uses_parent_section_after_captured_child_run() -> None:
    """捕捉された子pytestではなく、末尾にある親pytestのdurations節を採用する。"""
    output = "\n".join(
        (
            "---------------- Captured stdout call ----------------",
            "======= slowest 1 durations =======",
            "0.02s call     child_test.py::test_child",
            "1 passed in 0.02s",
            "======= slowest 1 durations =======",
            "0.77s call     outer_test.py::test_outer",
            "======= 1 failed in 0.80s =======",
        )
    )
    result = pyfltr.command.slow_tests.parse_pytest_durations(output)
    assert result == [pyfltr.command.slow_tests.SlowTest("outer_test.py::test_outer", "call", 0.77)]


def test_parse_vitest_basic(tmp_path: pathlib.Path) -> None:
    """vitestのJSONからミリ秒を秒へ換算し、nodeidを組み立てる。"""
    output = _vitest_output(
        [{"name": str(tmp_path / "src/a.test.ts"), "assertionResults": [{"fullName": "suite test", "duration": 1250}]}]
    )
    result = pyfltr.command.slow_tests.parse_vitest_durations(output, base_cwd=tmp_path)
    assert result == [pyfltr.command.slow_tests.SlowTest("src/a.test.ts::suite test", "test", 1.25)]


def test_parse_vitest_skips_entries_without_duration() -> None:
    """durationを欠く要素と数値でない要素を対象外とする。"""
    output = _vitest_output(
        [{"name": "a.test.ts", "assertionResults": [{"fullName": "missing"}, {"fullName": "bad", "duration": True}]}]
    )
    assert not pyfltr.command.slow_tests.parse_vitest_durations(output)


def test_parse_vitest_relativizes_path(tmp_path: pathlib.Path) -> None:
    """base_cwd配下の絶対パスを相対化し、配下でないパスは絶対のまま保持する。"""
    inside = tmp_path / "src/a.test.ts"
    outside = tmp_path.parent / "outside.test.ts"
    output = _vitest_output(
        [
            {"name": str(inside), "assertionResults": [{"fullName": "inside", "duration": 2000}]},
            {"name": str(outside), "assertionResults": [{"fullName": "outside", "duration": 1000}]},
        ]
    )
    result = pyfltr.command.slow_tests.parse_vitest_durations(output, base_cwd=tmp_path)
    assert result[0].nodeid == "src/a.test.ts::inside"
    assert result[1].nodeid == f"{outside}::outside"


def test_parse_vitest_empty_and_invalid() -> None:
    """テスト0件・JSON以外の入力はいずれも0件になる。"""
    assert not pyfltr.command.slow_tests.parse_vitest_durations('{"testResults": []}')
    assert not pyfltr.command.slow_tests.parse_vitest_durations("not json")


def test_parse_vitest_without_full_name() -> None:
    """fullNameが空のときはファイルパスのみをnodeidとする。"""
    output = _vitest_output([{"name": "a.test.ts", "assertionResults": [{"fullName": "", "duration": 1000}]}])
    result = pyfltr.command.slow_tests.parse_vitest_durations(output)
    assert result[0].nodeid == "a.test.ts"


def test_parse_vitest_limits_entries() -> None:
    """上限を超えるテストは秒数降順の上位のみを残す。"""
    assertions = [{"fullName": f"test {i}", "duration": i * 1000} for i in range(7)]
    output = _vitest_output([{"name": "a.test.ts", "assertionResults": assertions}])
    result = pyfltr.command.slow_tests.parse_vitest_durations(output)
    assert [test.seconds for test in result] == [6.0, 5.0, 4.0, 3.0, 2.0]


def test_format_slow_tests() -> None:
    """表示整形が秒数・区間名・識別子を1行へまとめる。"""
    slow_tests = [pyfltr.command.slow_tests.SlowTest("tests/a_test.py::test_x", "call", 1.5)]
    assert pyfltr.command.slow_tests.format_slow_tests(slow_tests) == ["1.50s call tests/a_test.py::test_x"]


def test_to_dict() -> None:
    """dict変換が識別子・区間・秒数を保持する。"""
    slow_test = pyfltr.command.slow_tests.SlowTest("tests/a_test.py::test_x", "call", 1.5)
    assert slow_test.to_dict() == {
        "nodeid": "tests/a_test.py::test_x",
        "phase": "call",
        "seconds": 1.5,
    }
