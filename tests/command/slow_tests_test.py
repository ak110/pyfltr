import json
import pathlib

import pytest

import pyfltr.command.slow_tests


def _pytest_output(*lines: str, header: str = "======= slowest 10 durations =======") -> str:
    return "\n".join((header, *lines, "======= 3 passed in 1.23s ======="))


def _vitest_output(test_results: list[dict[str, object]]) -> str:
    return json.dumps({"testResults": test_results})


def test_parse_numbered_header() -> None:
    result = pyfltr.command.slow_tests.parse_pytest_durations(_pytest_output("0.51s call     tests/a_test.py::test_x"))
    assert result == [pyfltr.command.slow_tests.SlowTest("tests/a_test.py::test_x", "call", 0.51)]


def test_parse_unnumbered_header() -> None:
    result = pyfltr.command.slow_tests.parse_pytest_durations(
        _pytest_output("0.51s call     tests/a_test.py::test_x", header="======= slowest durations =======")
    )
    assert len(result) == 1


@pytest.mark.parametrize("phase", ["setup", "call", "teardown"])
def test_parse_each_phase(phase: str) -> None:
    result = pyfltr.command.slow_tests.parse_pytest_durations(_pytest_output(f"0.10s {phase:<8} tests/a_test.py::test_x"))
    assert result[0].phase == phase


def test_parse_nodeid_with_space() -> None:
    result = pyfltr.command.slow_tests.parse_pytest_durations(_pytest_output("0.20s call     tests/a_test.py::test_x[a b]"))
    assert result[0].nodeid == "tests/a_test.py::test_x[a b]"


def test_parse_large_and_zero_seconds() -> None:
    result = pyfltr.command.slow_tests.parse_pytest_durations(
        _pytest_output(
            "123.45s call     tests/a_test.py::test_slow",
            "0.00s call     tests/a_test.py::test_fast",
        )
    )
    assert [test.seconds for test in result] == [123.45, 0.0]


def test_parse_hidden_only() -> None:
    result = pyfltr.command.slow_tests.parse_pytest_durations(_pytest_output("(10 durations < 5s hidden.)"))
    assert not result


def test_parse_hidden_only_without_durations_min() -> None:
    result = pyfltr.command.slow_tests.parse_pytest_durations(
        _pytest_output("(10 durations < 0.005s hidden.)  Use -vv to show these durations.")
    )
    assert not result


def test_parse_no_section() -> None:
    assert not pyfltr.command.slow_tests.parse_pytest_durations("3 passed in 1.23s")


def test_parse_malformed_line_is_ignored() -> None:
    result = pyfltr.command.slow_tests.parse_pytest_durations(_pytest_output("call     tests/a_test.py::test_x"))
    assert not result


def test_parse_stops_at_next_separator() -> None:
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
    output = "bringing up nodes...\n" + _pytest_output("0.51s call     tests/a_test.py::test_x")
    result = pyfltr.command.slow_tests.parse_pytest_durations(output)
    assert len(result) == 1


def test_parse_pytest_limits_entries() -> None:
    lines = [f"{seconds:.2f}s call     tests/a_test.py::test_{seconds}" for seconds in range(7)]
    result = pyfltr.command.slow_tests.parse_pytest_durations(_pytest_output(*lines))
    assert [test.seconds for test in result] == [6.0, 5.0, 4.0, 3.0, 2.0]


def test_parse_pytest_uses_parent_section_after_captured_child_run() -> None:
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
    output = _vitest_output(
        [{"name": str(tmp_path / "src/a.test.ts"), "assertionResults": [{"fullName": "suite test", "duration": 1250}]}]
    )
    result = pyfltr.command.slow_tests.parse_vitest_durations(output, base_cwd=tmp_path)
    assert result == [pyfltr.command.slow_tests.SlowTest("src/a.test.ts::suite test", "test", 1.25)]


def test_parse_vitest_skips_entries_without_duration() -> None:
    output = _vitest_output(
        [{"name": "a.test.ts", "assertionResults": [{"fullName": "missing"}, {"fullName": "bad", "duration": True}]}]
    )
    assert not pyfltr.command.slow_tests.parse_vitest_durations(output)


def test_parse_vitest_relativizes_path(tmp_path: pathlib.Path) -> None:
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
    assert not pyfltr.command.slow_tests.parse_vitest_durations('{"testResults": []}')
    assert not pyfltr.command.slow_tests.parse_vitest_durations("not json")


def test_parse_vitest_without_full_name() -> None:
    output = _vitest_output([{"name": "a.test.ts", "assertionResults": [{"fullName": "", "duration": 1000}]}])
    result = pyfltr.command.slow_tests.parse_vitest_durations(output)
    assert result[0].nodeid == "a.test.ts"


def test_parse_vitest_limits_entries() -> None:
    assertions = [{"fullName": f"test {i}", "duration": i * 1000} for i in range(7)]
    output = _vitest_output([{"name": "a.test.ts", "assertionResults": assertions}])
    result = pyfltr.command.slow_tests.parse_vitest_durations(output)
    assert [test.seconds for test in result] == [6.0, 5.0, 4.0, 3.0, 2.0]


def test_format_slow_tests() -> None:
    slow_tests = [pyfltr.command.slow_tests.SlowTest("tests/a_test.py::test_x", "call", 1.5)]
    assert pyfltr.command.slow_tests.format_slow_tests(slow_tests) == ["1.50s call tests/a_test.py::test_x"]


def test_to_dict() -> None:
    slow_test = pyfltr.command.slow_tests.SlowTest("tests/a_test.py::test_x", "call", 1.5)
    assert slow_test.to_dict() == {
        "nodeid": "tests/a_test.py::test_x",
        "phase": "call",
        "seconds": 1.5,
    }
