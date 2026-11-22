import json
import pathlib

import pytest

import pyfltr.diagnostics
import pyfltr.output.diagnostics
import pyfltr.parsing.entry
import pyfltr.parsing.pytest
import pyfltr.parsing.pytest_common
import pyfltr.parsing.pytest_config
import pyfltr.parsing.pytest_nested
import pyfltr.tools
import tests.parsing.conftest


@pytest.mark.parametrize("form", ["own-body-short", "own-body-auto-rN", "own-body-line"])
def test_pytest_own_filename_body_is_not_traceback(form: str, tmp_path: pathlib.Path) -> None:
    fixtures = json.loads(pathlib.Path(__file__).with_name("data").joinpath("pytest_runs.json").read_text(encoding="utf-8"))
    output = fixtures[form].replace("__SAMPLE_DIR__", tmp_path.as_posix())
    errors = pyfltr.parsing.entry.parse_errors("pytest", output, path_base=tmp_path, start_cwd=tmp_path)
    assert all(error.line < 91 and not error.file.startswith("E ") for error in errors)
    expected = {
        "test_exc_body": 29,
        "test_args": 5,
        "test_locals": 38,
        "test_captured": 43,
        "test_real_assert": 56,
    }
    if form != "own-body-auto-rN":
        # shortの一覧には名前が出るが、行番号はline形式にしか出ない。
        summary_only = {
            "test_raw_plain": 9,
            "test_raw_eline": 13,
            "test_raw_ownframe": 17,
            "test_raw_owndef": 21,
            "test_raw_summary_eq": 25,
            "test_captured_raw": 48,
            "test_raw_eline_summary": 52,
        }
        expected.update(summary_only if form == "own-body-line" else dict.fromkeys(summary_only, 0))
    assert sorted((error.message.split(":", 1)[0], error.line) for error in errors) == sorted(expected.items())


@pytest.mark.parametrize(
    "body",
    [
        "boom\nsp ace_test.py:93: in test_x\n    x()\n",
        "boom\n    def test_x():\nsp ace_test.py:94: ValueError\n",
    ],
)
def test_pytest_raw_own_frame_and_def_do_not_enable_expansion(body: str) -> None:
    output = "===== FAILURES =====\n_____ test_x _____\n" + body
    output += "===== short test summary info =====\nFAILED sp ace_test.py::test_x - Failed: boom\n"
    errors = pyfltr.parsing.entry.parse_errors("pytest", output)
    assert [(error.file, error.line) for error in errors] == [("sp ace_test.py", 0)]


def test_pytest_eline_cannot_own_unlisted_expanded_frame() -> None:
    output = (
        "===== FAILURES =====\n_____ test_x _____\n    def test_x():\n>       raise ValueError\n"
        "E       sp ace_test.py:97: in test_x\nsp ace_test.py:29: ValueError\n"
    )
    errors = pyfltr.parsing.entry.parse_errors("pytest", output)
    assert [(error.file, error.line) for error in errors] == [("sp ace_test.py", 29)]


@pytest.mark.parametrize("captured", [False, True])
def test_pytest_line_body_cannot_consume_own_summary(captured: bool, tmp_path: pathlib.Path) -> None:
    heading = "---------------- Captured stdout call ----------------\n" if captured else "boom\n"
    output = (
        f"===== FAILURES =====\n{heading}E   Failed: boom\nsp ace_test.py:95: Failed: boom\n"
        f"{tmp_path.as_posix()}/sp ace_test.py:25: Failed: boom\n"
        "===== short test summary info =====\nFAILED sp ace_test.py::test_x - Failed: boom\n"
    )
    errors = pyfltr.parsing.entry.parse_errors("pytest", output, path_base=tmp_path, start_cwd=tmp_path)
    assert [(error.file, error.line, error.message) for error in errors] == [("sp ace_test.py", 25, "test_x: Failed: boom")]


def test_pytest_line_unmatched_eline_does_not_expand_filename() -> None:
    output = (
        "===== FAILURES =====\nE   M\nsp ace_test.py:92: M\n===== short test summary info =====\n"
        "FAILED sp ace_test.py::test_x - Failed: boom\n"
    )
    errors = pyfltr.parsing.entry.parse_errors("pytest", output)
    assert [(error.file, error.line) for error in errors] == [("sp ace_test.py", 0)]


def test_pytest_raw_block_cannot_borrow_expanded_worker_nodeid() -> None:
    output = (
        "===== FAILURES =====\n_____ test_raw _____\nboom\n"
        "worker 'gw0' crashed while running 'sp ace_test.py::test_other'\n"
        "===== short test summary info =====\nFAILED body_test.py::test_raw - Failed: boom\n"
        "FAILED sp ace_test.py::test_other - Failed: other\n"
    )
    errors = pyfltr.parsing.entry.parse_errors("pytest", output)
    assert {error.message for error in errors} == {"test_raw: Failed: boom", "test_other: Failed: other"}


@pytest.mark.parametrize("style", ["short", "auto"])
def test_pytest_expanded_own_test_without_summary(style: str) -> None:
    body = (
        "sp ace_test.py:2: in test_sample\n    assert False\nE   AssertionError\n"
        if style == "short"
        else "    def test_sample():\n>       assert False\nE   AssertionError\n\nsp ace_test.py:2: AssertionError\n"
    )
    errors = pyfltr.parsing.entry.parse_errors("pytest", "===== FAILURES =====\n_____ test_sample _____\n" + body)
    assert [(error.file, error.line) for error in errors] == [("sp ace_test.py", 2)]


@pytest.mark.parametrize("basis", [True, False])
@pytest.mark.parametrize("form", ["expanded-body-auto", "expanded-body-line", "expanded-doctest-line"])
def test_pytest_expanded_body_positions_are_excluded(form: str, basis: bool, tmp_path: pathlib.Path) -> None:
    fixtures = json.loads(pathlib.Path(__file__).with_name("data").joinpath("pytest_runs.json").read_text(encoding="utf-8"))
    (tmp_path / "my tool.py").write_text("pass\n", encoding="utf-8")
    output = fixtures[form].replace("__SAMPLE_DIR__", tmp_path.as_posix())
    base = tmp_path if basis else None
    errors = pyfltr.parsing.entry.parse_errors("pytest", output, path_base=base, start_cwd=base)
    assert not any(error.file.endswith(("my tool.py", "sp ace_test.py")) and error.line >= 77 for error in errors)
    if form == "expanded-body-auto":
        names = ("test_raw_indented", "test_raw_eline", "test_raw_gt", "test_raw_frame", "test_raw_expected")
        for name in names:
            assert [(error.file, error.line) for error in errors if error.message.startswith(name + ":")] == [
                ("body_test.py", 0)
            ]


@pytest.mark.parametrize("listed", [True, False])
@pytest.mark.parametrize("file", ["a:b.py", "scripttool", "gonescript"])
def test_pytest_legacy_names_without_basis(file: str, listed: bool) -> None:
    output = (
        "===== FAILURES =====\n_____ test_script _____\n"
        f"caller_test.py:8: in test_script\n    run()\n{file}:2: in boom\n    ???\nE   ValueError: gone\n"
    )
    if listed:
        output += "===== short test summary info =====\nFAILED caller_test.py::test_script - ValueError: gone\n"
    errors = pyfltr.parsing.entry.parse_errors("pytest", output)
    if listed:
        assert [(error.file, error.line, error.message) for error in errors] == [
            ("caller_test.py", 0, f"test_script: ValueError: gone ({file}:2)")
        ]
    else:
        assert [(error.file, error.line) for error in errors] == [(file, 2)]


def test_pytest_same_name_failure_in_other_test_file_keeps_owner() -> None:
    output = (
        "===== FAILURES =====\n_____ test_x _____\n"
        "tests/b_test.py:5: in test_x\n    helper()\ntests/a_test.py:9: in helper\n"
        '    raise ValueError("boom")\nE   ValueError: boom\n'
        "_____ test_x _____\ntests/a_test.py:3: in test_x\n    assert 0\nE   assert 0\n"
        "===== short test summary info =====\n"
        "FAILED tests/b_test.py::test_x - ValueError: boom\nFAILED tests/a_test.py::test_x - assert 0\n"
    )
    errors = pyfltr.parsing.entry.parse_errors("pytest", output)
    assert [(error.file, error.line, error.message) for error in errors] == [
        ("tests/b_test.py", 0, "test_x: ValueError: boom (tests/a_test.py:9)"),
        ("tests/a_test.py", 3, "test_x: assert 0"),
    ]


@pytest.mark.parametrize("style", ["auto", "line"])
@pytest.mark.parametrize("file", ["my tool.py", "sp ace_test.py"])
def test_pytest_raw_failure_does_not_expand_positions(file: str, style: str, tmp_path: pathlib.Path) -> None:
    (tmp_path / file).write_text("pass\n", encoding="utf-8")
    head = "_____ test_raw _____\n" if style == "auto" else ""
    tail = ": in fake" if file.startswith("sp") else ": ValueError"
    output = f"===== FAILURES =====\n{head}boom\n{file}:77{tail}\n"
    output += (
        "===== short test summary info =====\nFAILED raw_test.py::test_raw - Failed: boom\n"
        "FAILED sp ace_test.py::test_other - Failed: other\n"
    )
    errors = pyfltr.parsing.entry.parse_errors("pytest", output, path_base=tmp_path, start_cwd=tmp_path)
    assert [(error.file, error.line) for error in errors] == [("raw_test.py", 0), ("sp ace_test.py", 0)]


@pytest.mark.parametrize(
    "form",
    [
        "body-external-long",
        "body-doctest-line",
        "body-mixed-line",
        "body-doctest-outside",
        "body-short",
        "body-native",
        "body-line",
        "body-line-rN",
    ],
)
def test_pytest_requires_filename_evidence(form: str, tmp_path: pathlib.Path) -> None:
    fixtures = json.loads(pathlib.Path(__file__).with_name("data").joinpath("pytest_runs.json").read_text(encoding="utf-8"))
    sample = tmp_path / "sample"
    sample.mkdir()
    base = tmp_path / "other" if form == "body-doctest-outside" else sample
    base.mkdir(exist_ok=True)
    output = fixtures[form].replace("__SAMPLE_DIR__", sample.as_posix())
    errors = pyfltr.parsing.entry.parse_errors("pytest", output, path_base=base, start_cwd=base)
    positions = [(error.file, error.line) for error in errors]
    if form == "body-external-long":
        assert positions == [("v_test.py", 13)] * 3
    elif form in ("body-doctest-line", "body-doctest-outside"):
        prefix = sample.as_posix() + "/" if form.endswith("outside") else ""
        assert positions == [(prefix + "doc.txt", 1), (prefix + "docnoext", 1)]
    elif form == "body-mixed-line":
        assert positions == [("doc.txt", 1), ("docnoext", 1), ("u_test.py", 5), ("u_test.py", 9)]
    elif form in ("body-short", "body-line", "body-line-rN"):
        assert positions == [("w_test.py", line) for line in (14, 18, 22, 29, 10, 10, 43, 50)]
    else:
        assert positions == [("w_test.py", 0)] * 8


@pytest.mark.parametrize("style", ["short", "auto"])
def test_pytest_chained_native_traceback_keeps_pytest_position(style: str) -> None:
    body = (
        "y_test.py:15: in test_group\n    helper()\ny_test.py:10: in helper\n"
        "    raise ValueError('inner 10:20:30')\nE   ValueError: inner 10:20:30\n"
    )
    if style == "auto":
        body += "y_test.py:10: ValueError\n"
    body += (
        "\nDuring handling of the above exception, another exception occurred:\n"
        'Traceback (most recent call last):\n  File "y_test.py", line 10, in helper\n'
        "ValueError: inner 10:20:30\n  + Exception Group Traceback (most recent call last):\n"
        "  | ExceptionGroup: grp (2 sub-exceptions)\n"
    )
    output = "===== FAILURES =====\n_____ test_group _____\n" + body
    output += "===== short test summary info =====\nFAILED y_test.py::test_group - ExceptionGroup: grp (2 sub-exceptions)\n"
    errors = pyfltr.parsing.entry.parse_errors("pytest", output)
    assert [(error.file, error.line, error.message) for error in errors] == [
        ("y_test.py", 10, "test_group: ValueError: inner 10:20:30")
    ]


@pytest.mark.parametrize("style", ["short", "auto"])
def test_pytest_extensionless_frame_has_position(style: str, tmp_path: pathlib.Path) -> None:
    body = (
        "runpy_test.py:8: in test_script\n    run_script()\nscripttool:2: in <module>\nE   ValueError: from script\n"
        if style == "short"
        else "runpy_test.py:8: \n>   raise ValueError('from script')\nE   ValueError: from script\nscripttool:2: ValueError\n"
    )
    output = "===== FAILURES =====\n_____ test_script _____\n" + body
    output += "===== short test summary info =====\nFAILED runpy_test.py::test_script - ValueError: from script\n"
    (tmp_path / "scripttool").write_text("raise ValueError('from script')\n", encoding="utf-8")
    errors = pyfltr.parsing.entry.parse_errors("pytest", output, path_base=tmp_path, start_cwd=tmp_path)
    assert [(error.file, error.line, error.message) for error in errors] == [
        ("runpy_test.py", 0, "test_script: ValueError: from script (scripttool:2)")
    ]


def test_pytest_virtual_string_frame_keeps_real_caller() -> None:
    output = (
        "===== FAILURES =====\n_____ test_exec _____\nrunpy_test.py:15: in test_exec\n    call()\n"
        "<string>:2: in f\nE   ValueError: from string\n===== short test summary info =====\n"
        "FAILED runpy_test.py::test_exec - ValueError: from string\n"
    )
    errors = pyfltr.parsing.entry.parse_errors("pytest", output)
    assert [(error.file, error.line) for error in errors] == [("runpy_test.py", 15)]


@pytest.mark.parametrize(
    ("body", "file", "line"),
    [
        (
            "tests/a_test.py:5: in test_time\n"
            '    assert stamp() == "2024-01-01 12:00:00"\n'
            "E   AssertionError: assert '2024-01-01 12:34:56' == '2024-01-01 12:00:00'\n",
            "tests/a_test.py",
            5,
        ),
        (
            "tests/a_test.py:4: in test_time\n    lib.boom()\n"
            ">       raise ValueError('bad time')\nE       ValueError: bad time 10:20:30\n"
            ".venv/lib/python3.15/site-packages/lib.py:2: ValueError\n",
            "tests/a_test.py",
            4,
        ),
        (
            "tests/a_test.py:4: in test_time\n    lib.boom()\n"
            ">       x[1:3:2]\nE       ValueError: bad slice\n"
            ".venv/lib/python3.15/site-packages/lib.py:2: ValueError\n",
            "tests/a_test.py",
            4,
        ),
        (
            "tests/a_test.py:4: in test_time\n    lib.boom()\n"
            "data_string = '10:20:30', format = '%Y-%m-%d'\n"
            ">       raise ValueError(data_string)\nE       ValueError: bad time\n"
            ".venv/lib/python3.15/site-packages/lib.py:2: ValueError\n",
            "tests/a_test.py",
            4,
        ),
        (
            'Traceback (most recent call last):\n  File "time_native_test.py", line 2, in helper\n'
            '    raise ValueError(f"bad time {value}")\nValueError: bad time 10:20:30\n',
            "time_native_test.py",
            0,
        ),
        (
            "E   ValueError: failure\ntime_native_test.py:11: ValueError\n"
            "---------------- Captured stdout call ----------------\n2026-10-18 12:30:45: in progress\n",
            "time_native_test.py",
            11,
        ),
        (
            "E   ValueError: failure\ntime_native_test.py:11: ValueError\n"
            "---------------- Captured stdout call ----------------\nnoise/path.py:45: in progress\n",
            "time_native_test.py",
            11,
        ),
    ],
    ids=[
        "short-time",
        "auto-external-time",
        "auto-external-slice",
        "auto-external-arguments",
        "native-time",
        "captured-frame",
        "captured-path-frame",
    ],
)
def test_pytest_body_colons_are_not_positions(body: str, file: str, line: int) -> None:
    output = "===== FAILURES =====\n_____ test_time _____\n" + body
    output += f"===== short test summary info =====\nFAILED {file}::test_time - ValueError: bad time 10:20:30\n"
    errors = pyfltr.parsing.entry.parse_errors("pytest", output)
    assert [(error.file, error.line) for error in errors] == [(file, line)]
    if line == 0:
        assert errors[0].message == "test_time: ValueError: bad time 10:20:30"


def test_parse_pytest_tb_short_project_frame() -> None:
    output = (
        "================================= FAILURES =================================\n"
        "_______________________________ test_bar ________________________________\n"
        "tests/foo_test.py:42: in test_bar\n"
        "    result = do_something()\n"
        "E   AssertionError: assert 1 == 2\n"
        "========================= short test summary info ==========================\n"
        "FAILED tests/foo_test.py::test_bar - AssertionError: assert 1 == 2\n"
    )
    errors = pyfltr.parsing.entry.parse_errors("pytest", output)
    assert len(errors) == 1
    assert errors[0].file == "tests/foo_test.py"
    assert errors[0].line == 42
    assert errors[0].message.startswith("test_bar: ")
    assert "assert 1 == 2" in errors[0].message


def test_parse_pytest_tb_short_class_based_test() -> None:
    output = (
        "================================= FAILURES =================================\n"
        "_______________________ TestSomething.test_method ______________________\n"
        "tests/foo_test.py:30: in test_method\n"
        "    assert self.value == 0\n"
        "E   AssertionError: assert 1 == 0\n"
        "========================= short test summary info ==========================\n"
    )
    errors = pyfltr.parsing.entry.parse_errors("pytest", output)
    assert len(errors) == 1
    assert errors[0].message.startswith("TestSomething.test_method: ")


def test_parse_pytest_tb_short_library_exception() -> None:
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
    errors = pyfltr.parsing.entry.parse_errors("pytest", output)
    assert len(errors) == 1
    assert errors[0].file == "tests/api_test.py"
    assert errors[0].line == 15
    assert errors[0].message.startswith("test_request: ")
    assert "httpx.ConnectError" in errors[0].message


def test_parse_pytest_tb_short_stdlib_exception() -> None:
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
    errors = pyfltr.parsing.entry.parse_errors("pytest", output)
    assert len(errors) == 1
    assert errors[0].file == "tests/path_test.py"
    assert errors[0].line == 10
    assert errors[0].message.startswith("test_path: ")


def test_parse_pytest_tb_short_all_external() -> None:
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
    errors = pyfltr.parsing.entry.parse_errors("pytest", output)
    assert len(errors) == 1
    assert errors[0].line == 20
    assert errors[0].message.startswith("test_ext: ")
    assert "RuntimeError: fail" in errors[0].message


def test_parse_pytest_worker_crash_generates_diagnostic_with_message() -> None:
    output = (
        "====================================== FAILURES ======================================\n"
        "_________________________________ test_normal_fail __________________________________\n"
        "[gw1] linux -- Python 3.13.3 /path/to/python3\n"
        "tbexp/test_slow.py:9: in test_normal_fail\n"
        "    assert 1 == 2\n"
        "E   assert 1 == 2\n"
        "________________________________ tbexp/test_slow.py _________________________________\n"
        "[gw0] linux -- Python 3.13.3 /path/to/python3\n"
        "worker 'gw0' crashed while running 'tbexp/test_slow.py::test_slow'\n"
        "============================== short test summary info ===============================\n"
        "FAILED tbexp/test_slow.py::test_normal_fail - assert 1 == 2\n"
        "FAILED tbexp/test_slow.py::test_slow - worker 'gw0' crashed while running "
        "'tbexp/test_slow.py::test_slow'\n"
    )
    errors = pyfltr.parsing.entry.parse_errors("pytest", output)
    assert len(errors) == 2
    crashed = next(e for e in errors if e.line == 0)
    assert crashed.file == "tbexp/test_slow.py"
    assert "worker 'gw0' crashed" in crashed.message
    assert "test_slow" in crashed.message
    normal = next(e for e in errors if e.line == 9)
    assert normal.file == "tbexp/test_slow.py"
    assert "test_normal_fail" in normal.message


def test_parse_pytest_tb_line_format() -> None:
    abs_file = pathlib.Path.cwd() / "tbexp" / "test_line.py"
    output = (
        "====================================== FAILURES ======================================\n"
        "E   assert 1 == 2\n"
        f"{abs_file}:2: assert 1 == 2\n"
        "E   RuntimeError: boom\n"
        f"{abs_file}:6: RuntimeError: boom\n"
        "============================== short test summary info ===============================\n"
        "FAILED tbexp/test_line.py::test_a - assert 1 == 2\n"
        "FAILED tbexp/test_line.py::test_b - RuntimeError: boom\n"
    )
    errors = pyfltr.parsing.entry.parse_errors("pytest", output)
    assert len(errors) == 2
    err_a = next(e for e in errors if e.line == 2)
    assert err_a.file == "tbexp/test_line.py"
    assert err_a.message == "test_a: assert 1 == 2"
    err_b = next(e for e in errors if e.line == 6)
    assert err_b.message == "test_b: RuntimeError: boom"


def test_parse_pytest_tb_short_class_based_test_not_double_counted_with_summary() -> None:
    output = (
        "====================================== FAILURES ======================================\n"
        "_____________________________ TestSomething.test_method ______________________________\n"
        "tbexp/test_cls.py:3: in test_method\n"
        "    assert 1 == 2\n"
        "E   assert 1 == 2\n"
        "============================== short test summary info ===============================\n"
        "FAILED tbexp/test_cls.py::TestSomething::test_method - assert 1 == 2\n"
    )
    errors = pyfltr.parsing.entry.parse_errors("pytest", output)
    assert len(errors) == 1
    assert errors[0].message.startswith("TestSomething.test_method: ")


def test_parse_pytest_summary_only_handles_parametrized_id_with_space() -> None:
    output = (
        "============================== short test summary info ===============================\n"
        "FAILED tbexp/test_param.py::test_a[param with space] - AssertionError: "
        "assert 'param with space' == 'x'\n"
    )
    errors = pyfltr.parsing.entry.parse_errors("pytest", output)
    assert len(errors) == 1
    assert errors[0].file == "tbexp/test_param.py"
    assert errors[0].line == 0
    assert errors[0].message.startswith("test_a[param with space]: ")


def test_parse_pytest_fallback() -> None:
    output = (
        "FAILED tests/foo_test.py::test_bar - AssertionError: xxx\n"
        "========================= 1 failed in 0.5s =========================\n"
    )
    errors = pyfltr.parsing.entry.parse_errors("pytest", output)
    assert len(errors) == 1
    assert errors[0].file == "tests/foo_test.py"
    assert errors[0].line == 0


def test_parse_pytest_parametrized_id_with_hyphen_not_double_counted() -> None:
    output = (
        "================================= FAILURES =================================\n"
        "_______________________________ test_param[b - c] ________________________________\n"
        "tests/a_test.py:5: in test_param\n"
        "    assert value == 'x'\n"
        "E   AssertionError: assert 'b - c' == 'x'\n"
        "========================= short test summary info ==========================\n"
        "FAILED tests/a_test.py::test_param[b - c] - AssertionError: assert 'b - c' == 'x'\n"
    )
    errors = pyfltr.parsing.entry.parse_errors("pytest", output)
    assert len(errors) == 1
    assert errors[0].file == "tests/a_test.py"
    assert errors[0].line == 5
    assert errors[0].message.startswith("test_param[b - c]: ")


def test_parse_pytest_parametrized_id_with_brackets_is_not_dropped() -> None:
    output = (
        "========================= short test summary info ==========================\n"
        "FAILED tests/e_test.py::test_listid[['a', 'b']] - AssertionError: assert ['a', 'b'] == ['x']\n"
        "FAILED tests/e_test.py::test_nested[list[int] and str] - AssertionError: assert 'z' == 'x'\n"
        "FAILED tests/e_test.py::test_deep[[[x y]]] - AssertionError: deep\n"
    )
    errors = pyfltr.parsing.entry.parse_errors("pytest", output)
    assert len(errors) == 3
    messages = sorted(e.message for e in errors)
    assert messages[0].startswith("test_deep[[[x y]]]: ")
    assert messages[1].startswith("test_listid[['a', 'b']]: ")
    assert messages[2].startswith("test_nested[list[int] and str]: ")
    assert all(e.file == "tests/e_test.py" for e in errors)


def test_parse_pytest_default_traceback_single_frame_has_line_number() -> None:
    output = (
        "================================= FAILURES =================================\n"
        "_______________________________ test_simple ________________________________\n"
        "\n"
        "    def test_simple():\n"
        ">       assert 1 == 2\n"
        "E       assert 1 == 2\n"
        "\n"
        "sample_test.py:2: AssertionError\n"
        "========================= short test summary info ==========================\n"
        "FAILED sample_test.py::test_simple - assert 1 == 2\n"
        "================================= 1 failed in 0.03s =================================\n"
    )
    errors = pyfltr.parsing.entry.parse_errors("pytest", output)
    assert len(errors) == 1
    assert errors[0].file == "sample_test.py"
    assert errors[0].line == 2
    assert errors[0].message == "test_simple: assert 1 == 2"


def test_parse_pytest_default_traceback_chained_exception_uses_last_location() -> None:
    output = (
        "================================= FAILURES =================================\n"
        "_______________________________ test_chained ________________________________\n"
        "\n"
        "    def test_chained():\n"
        "        try:\n"
        ">           raise KeyError('k')\n"
        "E           KeyError: 'k'\n"
        "\n"
        "sample_test.py:14: KeyError\n"
        "\n"
        "The above exception was the direct cause of the following exception:\n"
        "\n"
        "    def test_chained():\n"
        ">           raise RuntimeError('wrapped') from e\n"
        "E           RuntimeError: wrapped\n"
        "\n"
        "sample_test.py:16: RuntimeError\n"
        "========================= short test summary info ==========================\n"
        "FAILED sample_test.py::test_chained - RuntimeError: wrapped\n"
        "================================= 1 failed in 0.03s =================================\n"
    )
    errors = pyfltr.parsing.entry.parse_errors("pytest", output)
    assert len(errors) == 1
    assert errors[0].line == 16
    assert errors[0].message == "test_chained: RuntimeError: wrapped"


def test_parse_pytest_short_traceback_chained_exception_uses_outer_message() -> None:
    output = (
        "================================= FAILURES =================================\n"
        "_______________________________ test_chained ________________________________\n"
        "sample_test.py:14: in test_chained\n"
        "    raise KeyError('k')\n"
        "E   KeyError: 'k'\n"
        "\n"
        "The above exception was the direct cause of the following exception:\n"
        "sample_test.py:16: in test_chained\n"
        "    raise RuntimeError('wrapped') from e\n"
        "E   RuntimeError: wrapped\n"
        "========================= short test summary info ==========================\n"
        "FAILED sample_test.py::test_chained - RuntimeError: wrapped\n"
        "================================= 1 failed in 0.03s =================================\n"
    )
    errors = pyfltr.parsing.entry.parse_errors("pytest", output)
    assert len(errors) == 1
    assert errors[0].line == 16
    assert errors[0].message == "test_chained: RuntimeError: wrapped"


def test_parse_pytest_default_traceback_mixed_frames_prefers_last_location() -> None:
    separator = "_ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ \n"
    output = (
        "================================= FAILURES =================================\n"
        "_______________________________ test_deep ________________________________\n"
        "\n"
        "    def test_deep():\n"
        ">       a()\n"
        "\n"
        "deep_test.py:14: \n" + separator + "deep_test.py:2: in a\n"
        "    b()\n"
        "deep_test.py:6: in b\n"
        "    c()\n" + separator + "\n"
        "    def c():\n"
        ">       raise ValueError('deep')\n"
        "E       ValueError: deep\n"
        "\n"
        "deep_test.py:10: ValueError\n"
        "========================= short test summary info ==========================\n"
        "FAILED deep_test.py::test_deep - ValueError: deep\n"
        "================================= 1 failed in 0.06s =================================\n"
    )
    errors = pyfltr.parsing.entry.parse_errors("pytest", output)
    assert len(errors) == 1
    assert errors[0].file == "deep_test.py"
    assert errors[0].line == 10
    assert errors[0].message == "test_deep: ValueError: deep"


def test_parse_pytest_default_traceback_ignores_location_line_in_captured_output() -> None:
    output = (
        "================================= FAILURES =================================\n"
        "_______________________________ test_prints ________________________________\n"
        "\n"
        "    def test_prints():\n"
        ">       assert False\n"
        "E       assert False\n"
        "\n"
        "tests/x_test.py:5: AssertionError\n"
        "-------------------------- Captured stdout call ---------------------------\n"
        "other/noise.py:99: RuntimeError\n"
        "========================= short test summary info ==========================\n"
        "FAILED tests/x_test.py::test_prints - assert False\n"
        "================================= 1 failed in 0.10s =================================\n"
    )
    errors = pyfltr.parsing.entry.parse_errors("pytest", output)
    assert len(errors) == 1
    assert errors[0].file == "tests/x_test.py"
    assert errors[0].line == 5


def test_parse_pytest_default_traceback_external_location_keeps_project_frame() -> None:
    separator = "_ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ \n"
    output = (
        "================================= FAILURES =================================\n"
        "_______________________________ test_ext3 ________________________________\n"
        "\n"
        "    def test_ext3():\n"
        ">       helper()\n"
        "\n"
        "tests/e_test.py:13: \n" + separator + "tests/e_test.py:9: in helper\n"
        "    somelib.boom()\n" + separator + "\n"
        "    def boom():\n"
        ">       raise ValueError('boom')\n"
        "E       ValueError: boom\n"
        "\n"
        ".venv/lib/python3.13/site-packages/somelib/core.py:2: ValueError\n"
        "========================= short test summary info ==========================\n"
        "FAILED tests/e_test.py::test_ext3 - ValueError: boom\n"
        "================================= 1 failed in 0.10s =================================\n"
    )
    errors = pyfltr.parsing.entry.parse_errors("pytest", output)
    assert len(errors) == 1
    assert errors[0].file == "tests/e_test.py"
    assert errors[0].line == 9


def test_parse_pytest_default_traceback_external_location_without_frames() -> None:
    separator = "_ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ \n"
    output = (
        "================================= FAILURES =================================\n"
        "_______________________________ test_ext ________________________________\n"
        "\n"
        "    def test_ext():\n"
        ">       somelib.boom()\n"
        "\n"
        "tests/e_test.py:9: \n" + separator + "\n"
        "    def boom():\n"
        ">       raise ValueError('boom')\n"
        "E       ValueError: boom\n"
        "\n"
        ".venv/lib/python3.13/site-packages/somelib/core.py:2: ValueError\n"
        "========================= short test summary info ==========================\n"
        "FAILED tests/e_test.py::test_ext - ValueError: boom\n"
        "================================= 1 failed in 0.10s =================================\n"
    )
    errors = pyfltr.parsing.entry.parse_errors("pytest", output)
    assert len(errors) == 1
    assert errors[0].file == "tests/e_test.py"
    assert errors[0].line == 9
    assert errors[0].message == "test_ext: ValueError: boom"


def test_parse_pytest_doctest_failure_is_not_double_counted() -> None:
    output = (
        "================================= FAILURES =================================\n"
        "_______________________ [doctest] doc_test.add _________________________\n"
        "002 加算する。\n"
        "003 \n"
        "004     >>> add(1, 2)\n"
        "Expected:\n"
        "    4\n"
        "Got:\n"
        "    3\n"
        "\n"
        "doc_test.py:4: DocTestFailure\n"
        "========================= short test summary info ==========================\n"
        "FAILED doc_test.py::doc_test.add\n"
        "================================= 1 failed in 0.02s =================================\n"
    )
    errors = pyfltr.parsing.entry.parse_errors("pytest", output)
    assert len(errors) == 1
    assert errors[0].file == "doc_test.py"
    assert errors[0].line == 4
    assert errors[0].message == "doc_test.add"


def test_parse_pytest_worker_crash_class_based_not_double_counted() -> None:
    output = (
        "================================= FAILURES =================================\n"
        "_______________________________ tests/c_test.py ________________________________\n"
        "[gw0] linux -- Python 3.14.0 /path/to/python3\n"
        "worker 'gw0' crashed while running 'tests/c_test.py::TestCrash::test_method_crash'\n"
        "_______________________________ tests/c_test.py ________________________________\n"
        "[gw1] linux -- Python 3.14.0 /path/to/python3\n"
        "worker 'gw1' crashed while running 'tests/c_test.py::test_plain_crash'\n"
        "========================= short test summary info ==========================\n"
        "FAILED tests/c_test.py::TestCrash::test_method_crash - worker 'gw0' crashed while running "
        "'tests/c_test.py::TestCrash::test_method_crash'\n"
        "FAILED tests/c_test.py::test_plain_crash - worker 'gw1' crashed while running "
        "'tests/c_test.py::test_plain_crash'\n"
    )
    errors = pyfltr.parsing.entry.parse_errors("pytest", output)
    assert len(errors) == 2
    assert all(e.file == "tests/c_test.py" for e in errors)
    assert all(e.line == 0 for e in errors)
    assert any(e.message.startswith("TestCrash::test_method_crash: ") for e in errors)
    assert any(e.message.startswith("test_plain_crash: ") for e in errors)


def test_parse_pytest_keeps_line_numbers_when_child_summary_is_captured() -> None:
    output = (
        "================================= FAILURES =================================\n"
        "_______________________________ test_runs_child ________________________________\n"
        "tests/d_test.py:11: in test_runs_child\n"
        "E   AssertionError: assert 1 == 0\n"
        "-------------------------- Captured stdout call ---------------------------\n"
        "========================= short test summary info ==========================\n"
        "FAILED child/z_test.py::test_child - assert 1 == 2\n"
        "_______________________________ test_later_real ________________________________\n"
        "tests/d_test.py:15: in test_later_real\n"
        "E   AssertionError: later\n"
        "========================= short test summary info ==========================\n"
        "FAILED tests/d_test.py::test_runs_child - AssertionError: assert 1 == 0\n"
        "FAILED tests/d_test.py::test_later_real - AssertionError: later\n"
        "================================= 2 failed in 0.86s =================================\n"
    )
    errors = pyfltr.parsing.entry.parse_errors("pytest", output)
    by_test = {e.message.split(":")[0]: e for e in errors}
    assert by_test["test_runs_child"].line == 11
    assert by_test["test_later_real"].line == 15


def test_parse_pytest_tb_line_external_frame_not_double_counted() -> None:
    abs_file = pathlib.Path.cwd() / ".venv" / "lib" / "python3.13" / "site-packages" / "somelib" / "core.py"
    output = (
        "================================= FAILURES =================================\n"
        "E   RuntimeError: boom\n"
        f"{abs_file}:50: RuntimeError: boom\n"
        "========================= short test summary info ==========================\n"
        "FAILED tests/a_test.py::test_ext - RuntimeError: boom\n"
    )
    errors = pyfltr.parsing.entry.parse_errors("pytest", output)
    assert len(errors) == 1
    assert errors[0].file == "tests/a_test.py"
    assert errors[0].line == 0
    assert "RuntimeError: boom" in errors[0].message


def test_parse_pytest_tb_line_truncated_summary_message_not_double_counted() -> None:
    long_message = "json.decoder.JSONDecodeError: Expecting property name enclosed in double quotes: line 1 column 2"
    output = (
        "================================= FAILURES =================================\n"
        f"E   {long_message}\n"
        f".venv/lib/python3.13/site-packages/json/decoder.py:353: {long_message}\n"
        "E   assert 1 == 2\n"
        "tests/outer_test.py:9: assert 1 == 2\n"
        "========================= short test summary info ==========================\n"
        "FAILED tests/outer_test.py::test_long_message - json.decoder.JSONDecodeError: Expecting property...\n"
        "FAILED tests/outer_test.py::test_other - assert 1 == 2\n"
    )
    errors = pyfltr.parsing.entry.parse_errors("pytest", output)
    assert len(errors) == 2
    truncated = next(e for e in errors if e.line == 0)
    assert truncated.file == "tests/outer_test.py"
    assert truncated.message.startswith("test_long_message: ")


def test_parse_pytest_tb_line_ambiguous_truncated_summary_is_not_matched() -> None:
    output = (
        "================================= FAILURES =================================\n"
        "E   AssertionError: same prefix but different tail A\n"
        "tests/a_test.py:5: AssertionError: same prefix but different tail A\n"
        "========================= short test summary info ==========================\n"
        "FAILED tests/a_test.py::test_one - AssertionError: same prefix...\n"
        "FAILED tests/a_test.py::test_two - AssertionError: same prefix...\n"
    )
    errors = pyfltr.parsing.entry.parse_errors("pytest", output)
    assert len(errors) == 3
    assert sum(1 for e in errors if e.line == 0) == 2


def test_parse_pytest_same_test_name_in_different_files_not_dropped() -> None:
    output = (
        "================================= FAILURES =================================\n"
        "_______________________________ test_boom ________________________________\n"
        "tests/a_test.py:5: in test_boom\n"
        "E   AssertionError: a\n"
        "_______________________________ test_boom ________________________________\n"
        "========================= short test summary info ==========================\n"
        "FAILED tests/a_test.py::test_boom - AssertionError: a\n"
        "FAILED tests/b_test.py::test_boom - AssertionError: b\n"
    )
    errors = pyfltr.parsing.entry.parse_errors("pytest", output)
    assert len(errors) == 2
    a_error = next(e for e in errors if e.file == "tests/a_test.py")
    assert a_error.line == 5
    b_error = next(e for e in errors if e.file == "tests/b_test.py")
    assert b_error.line == 0
    assert "test_boom" in b_error.message


def test_parse_pytest_ignores_failed_line_in_captured_output() -> None:
    output = (
        "================================= FAILURES =================================\n"
        "_______________________________ test_runs_pytest ________________________________\n"
        "tests/a_test.py:9: in test_runs_pytest\n"
        "E   AssertionError: subprocess failed\n"
        "--- Captured stdout call ---\n"
        "FAILED other/y_test.py::test_phantom - boom\n"
        "========================= short test summary info ==========================\n"
        "FAILED tests/a_test.py::test_runs_pytest - AssertionError: subprocess failed\n"
    )
    errors = pyfltr.parsing.entry.parse_errors("pytest", output)
    assert len(errors) == 1
    assert errors[0].file == "tests/a_test.py"
    assert errors[0].line == 9
    assert errors[0].message.startswith("test_runs_pytest: ")


def test_parse_pytest_ignores_child_failure_block_in_captured_output() -> None:
    output = (
        "================================= FAILURES =================================\n"
        "_______________________________ test_runs_child ________________________________\n"
        "tests/d_test.py:11: in test_runs_child\n"
        "E   AssertionError: assert 1 == 0\n"
        "-------------------------- Captured stdout call ---------------------------\n"
        + tests.parsing.conftest.child_pytest_run(file="child/z_test.py", test="test_phantom", line=2, message="assert 1 == 2")
        + "_______________________________ test_later_real ________________________________\n"
        "tests/d_test.py:15: in test_later_real\n"
        "E   AssertionError: later\n"
        "========================= short test summary info ==========================\n"
        "FAILED tests/d_test.py::test_runs_child - AssertionError: assert 1 == 0\n"
        "FAILED tests/d_test.py::test_later_real - AssertionError: later\n"
        "================================= 2 failed in 0.86s =================================\n"
    )
    errors = pyfltr.parsing.entry.parse_errors("pytest", output)
    assert len(errors) == 2
    assert all(e.file == "tests/d_test.py" for e in errors)
    by_test = {e.message.split(":")[0]: e for e in errors}
    assert by_test["test_runs_child"].line == 11
    assert by_test["test_later_real"].line == 15


def test_parse_pytest_ignores_child_failure_block_with_same_test_name() -> None:
    output = (
        "================================= FAILURES =================================\n"
        "_______________________________ test_runs_child ________________________________\n"
        "tests/d_test.py:11: in test_runs_child\n"
        "E   AssertionError: assert 1 == 0\n"
        "-------------------------- Captured stdout call ---------------------------\n"
        + tests.parsing.conftest.child_pytest_run(file="child/z_test.py", test="test_dup", line=2, message="assert 1 == 2")
        + "_______________________________ test_dup ________________________________\n"
        "tests/d_test.py:15: in test_dup\n"
        "E   AssertionError: parent dup\n"
        "========================= short test summary info ==========================\n"
        "FAILED tests/d_test.py::test_runs_child - AssertionError: assert 1 == 0\n"
        "FAILED tests/d_test.py::test_dup - AssertionError: parent dup\n"
        "================================= 2 failed in 0.86s =================================\n"
    )
    errors = pyfltr.parsing.entry.parse_errors("pytest", output)
    assert len(errors) == 2
    assert all(e.file == "tests/d_test.py" for e in errors)
    by_test = {e.message.split(":")[0]: e for e in errors}
    assert by_test["test_runs_child"].line == 11
    assert by_test["test_dup"].line == 15


def test_parse_pytest_ignores_child_failure_block_without_parent_summary() -> None:
    output = (
        "================================= FAILURES =================================\n"
        "_______________________________ test_runs_child ________________________________\n"
        "tests/d_test.py:11: in test_runs_child\n"
        "E   AssertionError: assert 1 == 0\n"
        "-------------------------- Captured stdout call ---------------------------\n"
        + tests.parsing.conftest.child_pytest_run(file="child/z_test.py", test="test_dup", line=2, message="assert 1 == 2")
        + "_______________________________ test_dup ________________________________\n"
        "tests/d_test.py:15: in test_dup\n"
        "E   AssertionError: parent dup\n"
        "================================= 2 failed in 1.32s =================================\n"
    )
    errors = pyfltr.parsing.entry.parse_errors("pytest", output)
    assert len(errors) == 2
    assert all(e.file == "tests/d_test.py" for e in errors)
    by_test = {e.message.split(":")[0]: e for e in errors}
    assert by_test["test_runs_child"].line == 11
    assert by_test["test_dup"].line == 15


def test_parse_pytest_tb_line_ignores_child_failure_block_in_captured_output() -> None:
    output = (
        "================================= FAILURES =================================\n"
        "tests/d_test.py:11: AssertionError: assert 1 == 0\n"
        "-------------------------- Captured stdout call ---------------------------\n"
        + tests.parsing.conftest.child_pytest_run(file="child/z_test.py", test="test_phantom", line=2, message="assert 1 == 2")
        + "tests/d_test.py:15: AssertionError: later\n"
        "========================= short test summary info ==========================\n"
        "FAILED tests/d_test.py::test_runs_child - AssertionError: assert 1 == 0\n"
        "FAILED tests/d_test.py::test_later_real - AssertionError: later\n"
        "================================= 2 failed in 0.86s =================================\n"
    )
    errors = pyfltr.parsing.entry.parse_errors("pytest", output)
    assert len(errors) == 2
    assert all(e.file == "tests/d_test.py" for e in errors)
    by_test = {e.message.split(":")[0]: e for e in errors}
    assert by_test["test_runs_child"].line == 11
    assert by_test["test_later_real"].line == 15


def test_parse_pytest_tb_line_captured_output_keeps_following_line_numbers() -> None:
    output = (
        "================================= FAILURES =================================\n"
        "E   assert 1 == 0\n"
        "-------------------------- Captured stdout call ---------------------------\n"
        "noise\n"
        "tests/aa_test.py:3: assert 1 == 0\n"
        "E   assert 2 == 0\n"
        "tests/aa_test.py:7: assert 2 == 0\n"
        "========================= short test summary info ==========================\n"
        "FAILED tests/aa_test.py::test_1 - assert 1 == 0\n"
        "FAILED tests/aa_test.py::test_2 - assert 2 == 0\n"
    )
    errors = pyfltr.parsing.entry.parse_errors("pytest", output)
    assert len(errors) == 2
    by_test = {e.message.split(":")[0]: e for e in errors}
    assert by_test["test_1"].line == 3
    assert by_test["test_2"].line == 7


def test_parse_pytest_worker_crash_after_captured_output_keeps_message() -> None:
    output = (
        "================================= FAILURES =================================\n"
        "_______________________ test_prints_then_fails ________________________\n"
        "cr/crash_test.py:7: in test_prints_then_fails\n"
        "E   assert 1 == 0\n"
        "-------------------------- Captured stdout call ---------------------------\n"
        "noise\n"
        "_______________________________ cr/crash_test.py ________________________________\n"
        "worker 'gw1' crashed while running 'cr/crash_test.py::test_func_crash'\n"
        "========================= short test summary info ==========================\n"
        "FAILED cr/crash_test.py::test_prints_then_fails - assert 1 == 0\n"
        "FAILED cr/crash_test.py::test_func_crash - worker 'gw1' crashed\n"
    )
    errors = pyfltr.parsing.entry.parse_errors("pytest", output)
    assert len(errors) == 2
    crashed = next(e for e in errors if e.line == 0)
    assert "worker 'gw1' crashed while running cr/crash_test.py::test_func_crash" in crashed.message


def test_parse_pytest_captured_section_without_child_run_keeps_following_block() -> None:
    output = (
        "================================= FAILURES =================================\n"
        "_______________________________ test_a ________________________________\n"
        "tests/a_test.py:3: in test_a\n"
        "E   AssertionError: a\n"
        "-------------------------- Captured stdout call ---------------------------\n"
        "hello\n"
        "_______________________________ test_b ________________________________\n"
        "tests/a_test.py:7: in test_b\n"
        "E   AssertionError: b\n"
    )
    errors = pyfltr.parsing.entry.parse_errors("pytest", output)
    assert len(errors) == 2
    by_test = {e.message.split(":")[0]: e for e in errors}
    assert by_test["test_a"].line == 3
    assert by_test["test_b"].line == 7


def test_parse_pytest_child_run_without_summary_line_keeps_following_failures() -> None:
    output = (
        "================================= FAILURES =================================\n"
        "_______________________________ test_runs_child ________________________________\n"
        "tests/c_test.py:11: in test_runs_child\n"
        "E   AssertionError: assert 1 == 0\n"
        "-------------------------- Captured stdout call ---------------------------\n"
        "================================= test session starts =================================\n"
        "collected 1 item\n"
        "\n"
        "child/z_test.py \n"
        "_______________________________ test_normal_after ________________________________\n"
        "tests/c_test.py:15: in test_normal_after\n"
        "E   AssertionError: after\n"
        "========================= short test summary info ==========================\n"
        "FAILED tests/c_test.py::test_runs_child - AssertionError: assert 1 == 0\n"
        "FAILED tests/c_test.py::test_normal_after - AssertionError: after\n"
        "================================= 2 failed in 0.75s =================================\n"
    )
    errors = pyfltr.parsing.entry.parse_errors("pytest", output)
    assert len(errors) == 2
    by_test = {e.message.split(":")[0]: e for e in errors}
    assert by_test["test_runs_child"].line == 11
    assert by_test["test_normal_after"].line == 15


def test_parse_pytest_tb_line_child_run_without_summary_line_keeps_line_numbers() -> None:
    output = (
        "================================= FAILURES =================================\n"
        "E   AssertionError: assert 1 == 0\n"
        "-------------------------- Captured stdout call ---------------------------\n"
        "================================= test session starts =================================\n"
        "collected 1 item\n"
        "\n"
        "child/z_test.py \n"
        "tests/c_test.py:11: AssertionError: assert 1 == 0\n"
        "E   AssertionError: after\n"
        "tests/c_test.py:15: AssertionError: after\n"
        "========================= short test summary info ==========================\n"
        "FAILED tests/c_test.py::test_runs_child - AssertionError: assert 1 == 0\n"
        "FAILED tests/c_test.py::test_normal_after - AssertionError: after\n"
        "================================= 2 failed in 0.75s =================================\n"
    )
    errors = pyfltr.parsing.entry.parse_errors("pytest", output)
    assert len(errors) == 2
    by_test = {e.message.split(":")[0]: e for e in errors}
    assert by_test["test_runs_child"].line == 11
    assert by_test["test_normal_after"].line == 15


def _pytest_two_child_runs_output(*, tb_line: bool, summary_list: bool) -> str:
    crashed = tests.parsing.conftest.child_pytest_run(
        file="child/dies_test.py", test="test_dies", line=0, message="", terminated=False
    )
    completed = tests.parsing.conftest.child_pytest_run(
        file="child/fails_test.py", test="test_fails", line=2, message="assert 1 == 2"
    )
    captured = "-------------------------- Captured stdout call ---------------------------\n"
    if tb_line:
        failures = (
            "E   AssertionError: assert 1 == 0\n" + captured + crashed + "tests/c_test.py:15: AssertionError: crashed\n"
            "E   AssertionError: plain\n"
            "tests/c_test.py:19: AssertionError: plain\n"
            "E   AssertionError: assert 1 == 0\n" + captured + completed + "tests/c_test.py:23: AssertionError: failed\n"
        )
    else:
        failures = (
            "_______________________________ test_a_child_crashes ________________________________\n"
            "tests/c_test.py:15: in test_a_child_crashes\n"
            "E   AssertionError: crashed\n"
            + captured
            + crashed
            + "_______________________________ test_b_child_normal ________________________________\n"
            "tests/c_test.py:19: in test_b_child_normal\n"
            "E   AssertionError: plain\n"
            "_______________________________ test_c_child_fails ________________________________\n"
            "tests/c_test.py:23: in test_c_child_fails\n"
            "E   AssertionError: failed\n" + captured + completed
        )
    summary = (
        "========================= short test summary info ==========================\n"
        "FAILED tests/c_test.py::test_a_child_crashes - AssertionError: crashed\n"
        "FAILED tests/c_test.py::test_b_child_normal - AssertionError: plain\n"
        "FAILED tests/c_test.py::test_c_child_fails - AssertionError: failed\n"
    )
    return (
        "================================= FAILURES =================================\n"
        + failures
        + (summary if summary_list else "")
        + "================================= 3 failed in 1.20s =================================\n"
    )


def test_parse_pytest_unterminated_child_run_does_not_extend_to_next_child_run() -> None:
    errors = pyfltr.parsing.entry.parse_errors("pytest", _pytest_two_child_runs_output(tb_line=False, summary_list=True))
    assert len(errors) == 3
    assert all(e.file == "tests/c_test.py" for e in errors)
    by_test = {e.message.split(":")[0]: e for e in errors}
    assert by_test["test_a_child_crashes"].line == 15
    assert by_test["test_b_child_normal"].line == 19
    assert by_test["test_c_child_fails"].line == 23


def test_parse_pytest_tb_line_unterminated_child_run_does_not_extend_to_next_child_run() -> None:
    errors = pyfltr.parsing.entry.parse_errors("pytest", _pytest_two_child_runs_output(tb_line=True, summary_list=True))
    assert len(errors) == 3
    assert all(e.file == "tests/c_test.py" for e in errors)
    assert {e.line for e in errors} == {15, 19, 23}


def test_parse_pytest_unterminated_child_run_without_summary_list_keeps_failures() -> None:
    errors = pyfltr.parsing.entry.parse_errors("pytest", _pytest_two_child_runs_output(tb_line=False, summary_list=False))
    assert len(errors) == 3
    assert all(e.file == "tests/c_test.py" for e in errors)
    assert {e.line for e in errors} == {15, 19, 23}


def test_parse_pytest_two_complete_child_runs_are_masked_independently() -> None:
    first = tests.parsing.conftest.child_pytest_run(file="child/a_test.py", test="test_a", line=2, message="assert 1 == 2")
    second = tests.parsing.conftest.child_pytest_run(file="child/b_test.py", test="test_b", line=3, message="assert 2 == 3")
    captured = "-------------------------- Captured stdout call ---------------------------\n"
    output = (
        "================================= FAILURES =================================\n"
        "_______________________________ test_first ________________________________\n"
        "tests/c_test.py:11: in test_first\n"
        "E   AssertionError: first\n"
        + captured
        + first
        + "_______________________________ test_second ________________________________\n"
        "tests/c_test.py:15: in test_second\n"
        "E   AssertionError: second\n"
        + captured
        + second
        + "========================= short test summary info ==========================\n"
        "FAILED tests/c_test.py::test_first - AssertionError: first\n"
        "FAILED tests/c_test.py::test_second - AssertionError: second\n"
        "================================= 2 failed in 0.90s =================================\n"
    )
    errors = pyfltr.parsing.entry.parse_errors("pytest", output)
    assert len(errors) == 2
    assert all(e.file == "tests/c_test.py" for e in errors)
    assert {e.line for e in errors} == {11, 15}


def _pytest_unterminated_then_tail_output(*, summary_list: bool) -> str:
    captured = "-------------------------- Captured stdout call ---------------------------\n"
    unterminated = "================================= test session starts =================================\ncollected 1 item\n"
    tail_only = (
        "FAILED child/y_test.py::test_y - assert 1 == 2\n"
        "================================= 1 failed in 0.03s =================================\n"
    )
    summary = (
        "========================= short test summary info ==========================\n"
        "FAILED tests/c_test.py::test_a_unterminated - AssertionError: a\n"
        "FAILED tests/c_test.py::test_b_plain - AssertionError: b\n"
        "FAILED tests/c_test.py::test_c_tail_only - AssertionError: c\n"
    )
    return (
        "================================= FAILURES =================================\n"
        "_______________________________ test_a_unterminated ________________________________\n"
        "tests/c_test.py:12: in test_a_unterminated\n"
        "E   AssertionError: a\n"
        + captured
        + unterminated
        + "_______________________________ test_b_plain ________________________________\n"
        "tests/c_test.py:16: in test_b_plain\n"
        "E   AssertionError: b\n"
        "_______________________________ test_c_tail_only ________________________________\n"
        "tests/c_test.py:25: in test_c_tail_only\n"
        "E   AssertionError: c\n"
        + captured
        + tail_only
        + (summary if summary_list else "")
        + "================================= 3 failed in 1.10s =================================\n"
    )


def test_parse_pytest_unterminated_child_run_does_not_pair_with_later_section() -> None:
    errors = pyfltr.parsing.entry.parse_errors("pytest", _pytest_unterminated_then_tail_output(summary_list=True))
    assert len(errors) == 3
    assert all(e.file == "tests/c_test.py" for e in errors)
    assert {e.line for e in errors} == {12, 16, 25}


def test_parse_pytest_unterminated_child_run_without_summary_list_does_not_pair() -> None:
    errors = pyfltr.parsing.entry.parse_errors("pytest", _pytest_unterminated_then_tail_output(summary_list=False))
    parent_errors = [e for e in errors if e.file == "tests/c_test.py"]
    assert {e.line for e in parent_errors} == {12, 16, 25}


def test_parse_pytest_child_run_masked_for_stderr_and_log_sections() -> None:
    child = tests.parsing.conftest.child_pytest_run(
        file="child/z_test.py", test="test_phantom", line=2, message="assert 1 == 2"
    )
    for section in ("Captured stderr call", "Captured log call"):
        output = (
            "================================= FAILURES =================================\n"
            "_______________________________ test_runs_child ________________________________\n"
            "tests/d_test.py:11: in test_runs_child\n"
            "E   AssertionError: assert 1 == 0\n"
            f"-------------------------- {section} ---------------------------\n"
            + child
            + "========================= short test summary info ==========================\n"
            "FAILED tests/d_test.py::test_runs_child - AssertionError: assert 1 == 0\n"
            "================================= 1 failed in 0.50s =================================\n"
        )
        errors = pyfltr.parsing.entry.parse_errors("pytest", output)
        assert len(errors) == 1, section
        assert errors[0].file == "tests/d_test.py", section
        assert errors[0].line == 11, section


@pytest.mark.parametrize(
    "tail_line",
    [
        "2 failed in 0.92s",
        "1 failed, 2 passed, 1 skipped, 1 xfailed, 1 xpassed, 1 warning in 0.06s",
        "no tests ran in 0.00s",
        "3 passed in 61.00s (0:01:01)",
        "3 passed in 90061.00s (1 day, 1:01:01)",
    ],
)
def test_parse_pytest_child_run_masked_with_quiet_parent(tail_line: str) -> None:
    output = (
        "================================= FAILURES =================================\n"
        "_______________________________ test_runs_child ________________________________\n"
        "tests/q_test.py:11: in test_runs_child\n"
        "E   AssertionError: assert 1 == 0\n"
        "-------------------------- Captured stdout call ---------------------------\n"
        + tests.parsing.conftest.child_pytest_run(
            file="child/inner_test.py", test="test_inner", line=2, message="assert 1 == 2"
        )
        + "_______________________________ test_after ________________________________\n"
        "tests/q_test.py:15: in test_after\n"
        "E   AssertionError: after\n"
        "========================= short test summary info ==========================\n"
        "FAILED tests/q_test.py::test_runs_child - AssertionError: assert 1 == 0\n"
        "FAILED tests/q_test.py::test_after - AssertionError: after\n"
        f"{tail_line}\n"
    )
    errors = pyfltr.parsing.entry.parse_errors("pytest", output)
    assert len(errors) == 2
    assert all(e.file == "tests/q_test.py" for e in errors)
    assert {e.line for e in errors} == {11, 15}


def test_parse_pytest_captured_text_is_not_taken_as_parent_summary_line() -> None:
    output = (
        "================================= FAILURES =================================\n"
        "_______________________________ test_p ________________________________\n"
        "tests/p_test.py:3: in test_p\n"
        "E   assert 1 == 2\n"
        "-------------------------- Captured stdout call ---------------------------\n"
        "================================= test session starts =================================\n"
        "collected 1 item\n"
        "_______________________________ test_q ________________________________\n"
        "tests/p_test.py:7: in test_q\n"
        "E   assert 5 == 6\n"
        "========================= short test summary info ==========================\n"
        "FAILED tests/p_test.py::test_p - assert 1 == 2\n"
        "FAILED tests/p_test.py::test_q - assert 5 == 6\n"
        "================================= 2 failed in 0.30s =================================\n"
        "12 files processed in 2.5s\n"
    )
    errors = pyfltr.parsing.entry.parse_errors("pytest", output)
    assert len(errors) == 2
    assert {e.line for e in errors} == {3, 7}


def test_parse_pytest_child_run_masked_without_parent_failures_section() -> None:
    output = (
        "-------------------------- Captured stdout call ---------------------------\n"
        + tests.parsing.conftest.child_pytest_run(
            file="child/inner_test.py", test="test_inner", line=2, message="assert 1 == 2"
        )
        + "================================= 2 passed in 0.90s =================================\n"
    )
    errors = pyfltr.parsing.entry.parse_errors("pytest", output)
    assert errors == []


def test_parse_pytest_child_run_is_not_masked_without_parent_summary_line() -> None:
    output = (
        "================================= FAILURES =================================\n"
        "_______________________________ test_runs_child ________________________________\n"
        "tests/d_test.py:11: in test_runs_child\n"
        "E   AssertionError: assert 1 == 0\n"
        "-------------------------- Captured stdout call ---------------------------\n"
        + tests.parsing.conftest.child_pytest_run(file="child/z_test.py", test="test_phantom", line=2, message="assert 1 == 2")
    )
    errors = pyfltr.parsing.entry.parse_errors("pytest", output)
    assert next(e for e in errors if e.file == "tests/d_test.py").line == 11
    assert any(e.file == "child/z_test.py" for e in errors)


def test_parse_pytest_child_run_with_nested_captured_section_is_not_diagnosed() -> None:
    child = (
        "================================= test session starts =================================\n"
        "collected 1 item\n"
        "\n"
        "================================= FAILURES =================================\n"
        "_______________________________ test_inner ________________________________\n"
        "child/inner_test.py:3: in test_inner\n"
        "E   assert 1 == 2\n"
        "-------------------------- Captured stdout call ---------------------------\n"
        "inner noise\n"
        "========================= short test summary info ==========================\n"
        "FAILED child/inner_test.py::test_inner - assert 1 == 2\n"
        "================================= 1 failed in 0.04s =================================\n"
    )
    output = (
        "================================= FAILURES =================================\n"
        "_______________________________ test_runs_child ________________________________\n"
        "tests/p_test.py:11: in test_runs_child\n"
        "E   AssertionError: assert 1 == 0\n"
        "-------------------------- Captured stdout call ---------------------------\n"
        + child
        + "_______________________________ test_after ________________________________\n"
        "tests/p_test.py:15: in test_after\n"
        "E   AssertionError: after\n"
        "========================= short test summary info ==========================\n"
        "FAILED tests/p_test.py::test_runs_child - AssertionError: assert 1 == 0\n"
        "FAILED tests/p_test.py::test_after - AssertionError: after\n"
        "================================= 2 failed in 0.90s =================================\n"
    )
    errors = pyfltr.parsing.entry.parse_errors("pytest", output)
    assert len(errors) == 2
    assert all(e.file == "tests/p_test.py" for e in errors)
    assert {e.line for e in errors} == {11, 15}


def test_parse_pytest_unterminated_child_run_with_failures_section_keeps_parent_lines() -> None:
    captured = "-------------------------- Captured stdout call ---------------------------\n"
    unterminated = (
        "================================= test session starts =================================\n"
        "collected 1 item\n"
        "================================= FAILURES =================================\n"
        "_______________________________ test_inner ________________________________\n"
        "child/inner_test.py:3: in test_inner\n"
        "E   assert 1 == 2\n"
        "-------------------------- Captured stdout call ---------------------------\n"
        "inner noise\n"
    )
    tail_only = (
        "FAILED child/y_test.py::test_y - assert 1 == 2\n"
        "================================= 1 failed in 0.03s =================================\n"
    )
    output = (
        "================================= FAILURES =================================\n"
        "_______________________________ test_a_child ________________________________\n"
        "tests/c_test.py:12: in test_a_child\n"
        "E   AssertionError: a\n"
        + captured
        + unterminated
        + "_______________________________ test_b_plain ________________________________\n"
        "tests/c_test.py:18: in test_b_plain\n"
        "E   AssertionError: b\n"
        "_______________________________ test_c_tail ________________________________\n"
        "tests/c_test.py:24: in test_c_tail\n"
        "E   AssertionError: c\n"
        + captured
        + tail_only
        + "========================= short test summary info ==========================\n"
        "FAILED tests/c_test.py::test_a_child - AssertionError: a\n"
        "FAILED tests/c_test.py::test_b_plain - AssertionError: b\n"
        "FAILED tests/c_test.py::test_c_tail - AssertionError: c\n"
        "================================= 3 failed in 1.10s =================================\n"
    )
    errors = pyfltr.parsing.entry.parse_errors("pytest", output)
    assert len(errors) == 3
    assert all(e.file == "tests/c_test.py" for e in errors)
    assert {e.line for e in errors} == {12, 18, 24}


def test_parse_pytest_quiet_child_failure_block_is_dropped_by_summary_safety_net() -> None:
    output = (
        "================================= FAILURES =================================\n"
        "_______________________________ test_runs_child ________________________________\n"
        "tests/p_test.py:11: in test_runs_child\n"
        "E   AssertionError: assert 1 == 0\n"
        "-------------------------- Captured stdout call ---------------------------\n"
        "================================= FAILURES =================================\n"
        "_______________________________ test_inner ________________________________\n"
        "child/inner_test.py:3: in test_inner\n"
        "E   assert 1 == 2\n"
        "========================= short test summary info ==========================\n"
        "FAILED child/inner_test.py::test_inner - assert 1 == 2\n"
        "1 failed in 0.03s\n"
        "_______________________________ test_after ________________________________\n"
        "tests/p_test.py:15: in test_after\n"
        "E   AssertionError: after\n"
        "========================= short test summary info ==========================\n"
        "FAILED tests/p_test.py::test_runs_child - AssertionError: assert 1 == 0\n"
        "FAILED tests/p_test.py::test_after - AssertionError: after\n"
        "================================= 2 failed in 0.90s =================================\n"
    )
    errors = pyfltr.parsing.entry.parse_errors("pytest", output)
    assert len(errors) == 2
    assert all(e.file == "tests/p_test.py" for e in errors)
    assert {e.line for e in errors} == {11, 15}


def test_parse_pytest_quiet_child_crash_block_is_dropped_by_summary_safety_net() -> None:
    output = (
        "================================= FAILURES =================================\n"
        "_______________________________ test_runs_child ________________________________\n"
        "tests/p_test.py:11: in test_runs_child\n"
        "E   AssertionError: assert 1 == 0\n"
        "-------------------------- Captured stdout call ---------------------------\n"
        "_______________________________ child/inner_test.py ________________________________\n"
        "worker 'gw0' crashed while running 'child/inner_test.py::test_inner'\n"
        "========================= short test summary info ==========================\n"
        "FAILED tests/p_test.py::test_runs_child - AssertionError: assert 1 == 0\n"
        "================================= 1 failed in 0.90s =================================\n"
    )
    errors = pyfltr.parsing.entry.parse_errors("pytest", output)
    assert len(errors) == 1
    assert errors[0].file == "tests/p_test.py"
    assert errors[0].line == 11


def test_parse_pytest_quiet_child_with_same_test_name_is_not_dropped() -> None:
    output = (
        "================================= FAILURES =================================\n"
        "_______________________________ test_runs_child ________________________________\n"
        "tests/p_test.py:11: in test_runs_child\n"
        "E   AssertionError: assert 1 == 0\n"
        "-------------------------- Captured stdout call ---------------------------\n"
        "================================= FAILURES =================================\n"
        "_______________________________ test_dup ________________________________\n"
        "child/inner_test.py:3: in test_dup\n"
        "E   assert 1 == 2\n"
        "1 failed in 0.03s\n"
        "_______________________________ test_dup ________________________________\n"
        "tests/p_test.py:15: in test_dup\n"
        "E   AssertionError: parent dup\n"
        "========================= short test summary info ==========================\n"
        "FAILED tests/p_test.py::test_runs_child - AssertionError: assert 1 == 0\n"
        "FAILED tests/p_test.py::test_dup - AssertionError: parent dup\n"
        "================================= 2 failed in 0.90s =================================\n"
    )
    errors = pyfltr.parsing.entry.parse_errors("pytest", output)
    parent_errors = [e for e in errors if e.file == "tests/p_test.py"]
    assert {e.line for e in parent_errors} == {0, 11, 15}
    assert any(e.line == 0 and e.message.endswith("(child/inner_test.py:3)") for e in parent_errors)


def test_parse_pytest_unterminated_child_run_is_not_masked() -> None:
    output = (
        "================================= FAILURES =================================\n"
        "_______________________________ test_runs_child ________________________________\n"
        "tests/d_test.py:11: in test_runs_child\n"
        "E   AssertionError: assert 1 == 0\n"
        "-------------------------- Captured stdout call ---------------------------\n"
        "================================= test session starts =================================\n"
        "collected 1 item\n"
        "_______________________________ test_later_real ________________________________\n"
        "tests/d_test.py:15: in test_later_real\n"
        "E   AssertionError: later\n"
        "========================= short test summary info ==========================\n"
        "FAILED tests/d_test.py::test_runs_child - AssertionError: assert 1 == 0\n"
        "FAILED tests/d_test.py::test_later_real - AssertionError: later\n"
        "================================= 2 failed in 0.86s =================================\n"
    )
    errors = pyfltr.parsing.entry.parse_errors("pytest", output)
    assert len(errors) == 2
    by_test = {e.message.split(":")[0]: e for e in errors}
    assert by_test["test_runs_child"].line == 11
    assert by_test["test_later_real"].line == 15


_QUIET_NESTED_FAILURE = (
    "================================= FAILURES =================================\n"
    "_______________________________ test_runs_child ________________________________\n"
    "tests/p_test.py:13: in test_runs_child\n"
    "E   AssertionError: assert 1 == 0\n"
    "-------------------------- Captured stdout call ---------------------------\n"
    "================================= FAILURES =================================\n"
    "_______________________________ test_inner ________________________________\n"
    "child/inner_test.py:3: in test_inner\n"
    "E   assert 1 == 2\n"
    "========================= short test summary info ==========================\n"
    "FAILED child/inner_test.py::test_inner - assert 1 == 2\n"
    "1 failed in 0.03s\n"
    "_______________________________ test_parent_later ________________________________\n"
    "tests/p_test.py:17: in test_parent_later\n"
    "E   AssertionError: assert 'a' == 'b'\n"
)


_CAPTURED_PARENT_FAILURE = (
    "============================= test session starts ==============================\n"
    "platform linux -- Python 3.15.2, pytest-9.2.0, pluggy-1.7.0\n"
    "rootdir: /tmp/sample\n"
    "configfile: pyproject.toml\n"
    "plugins: timeout-2.4.0, xdist-3.8.0\n"
    "collected 1 item\n"
    "\n"
    "parent_test.py F                                                         [100%]\n"
    "\n"
    "=================================== FAILURES ===================================\n"
    "_______________________________ test_runs_child ________________________________\n"
    "parent_test.py:6: in test_runs_child\n"
    "    assert 1 == 0\n"
    "E   assert 1 == 0\n"
    "----------------------------- Captured stdout call -----------------------------\n"
)


_CHILD_RUN_FAILURE = (
    "============================= test session starts ==============================\n"
    "platform linux -- Python 3.15.2, pytest-9.2.0, pluggy-1.7.0\n"
    "rootdir: /tmp/sample/child\n"
    "configfile: pytest.ini\n"
    "plugins: timeout-2.4.0, xdist-3.8.0\n"
    "collected 1 item\n"
    "\n"
    "child_test.py F                                                          [100%]\n"
    "\n"
    "=================================== FAILURES ===================================\n"
    "__________________________________ test_child __________________________________\n"
    "child_test.py:2: in test_child\n"
    "    assert 1 == 2\n"
    "E   assert 1 == 2\n"
    "=========================== short test summary info ============================\n"
    "FAILED child_test.py::test_child - assert 1 == 2\n"
)


def test_parse_pytest_quiet_child_summary_is_not_taken_as_parent_summary() -> None:
    output = _QUIET_NESTED_FAILURE + "================================= 2 failed in 0.89s =================================\n"
    errors = pyfltr.parsing.entry.parse_errors("pytest", output)
    parent_errors = [e for e in errors if e.file == "tests/p_test.py"]
    assert {e.line for e in parent_errors} == {13, 17}
    assert any(e.file == "child/inner_test.py" for e in errors)


@pytest.mark.parametrize(
    "output",
    [
        pytest.param(
            (
                _CAPTURED_PARENT_FAILURE + "F                                                                        [100%]\n"
                "=================================== FAILURES ===================================\n"
                "__________________________________ test_child __________________________________\n"
                "child/child_fail_test.py:2: in test_child\n"
                "    assert 1 == 2\n"
                "E   assert 1 == 2\n"
                "=========================== short test summary info ============================\n"
                "FAILED child/child_fail_test.py::test_child - assert 1 == 2\n"
                "1 failed in 0.02s\n"
                "\n"
                "============================== 1 failed in 0.86s ===============================\n"
            ),
            id="child-failed",
        ),
        pytest.param(
            (
                _CAPTURED_PARENT_FAILURE + "E                                                                        [100%]\n"
                "==================================== ERRORS ====================================\n"
                "_________________________ ERROR at setup of test_child _________________________\n"
                "child/child_fail_test.py:6: in broken\n"
                "    raise RuntimeError('boom')\n"
                "E   RuntimeError: boom\n"
                "=========================== short test summary info ============================\n"
                "ERROR child/child_fail_test.py::test_child - RuntimeError: boom\n"
                "1 error in 0.02s\n"
                "\n"
                "============================== 1 failed in 0.94s ===============================\n"
            ),
            id="child-error",
        ),
    ],
)
def test_parse_pytest_quiet_child_does_not_hide_parent_failure_without_summary_list(output: str) -> None:
    errors = pyfltr.parsing.entry.parse_errors("pytest", output)
    assert any(e.file == "parent_test.py" and e.line == 6 for e in errors)
    preamble = (
        "/path/to/pytest_asyncio/plugin.py:299: PytestDeprecationWarning: The configuration option is unset.\n"
        "\n"
        "  warnings.warn(PytestDeprecationWarning(_DEFAULT_FIXTURE_LOOP_SCOPE_UNSET))\n"
    )
    errors = pyfltr.parsing.entry.parse_errors("pytest", preamble + output)
    assert any(e.file == "parent_test.py" and e.line == 6 for e in errors)


def test_parse_pytest_double_quiet_parent_with_two_child_runs_keeps_first_child_excluded() -> None:
    output = (
        _CHILD_RUN_FAILURE
        + "============================== 1 failed in 0.03s ===============================\n"
        + _CHILD_RUN_FAILURE.replace("child_test.py", "second_test.py")
        + "============================== 1 failed in 0.02s ===============================\n"
        ".\n"
    )
    errors = pyfltr.parsing.entry.parse_errors("pytest", output)
    # 親の最終集計がないため末尾の子を判別できない場合がある。残存は契約にしない。
    assert all(error.file != "child_test.py" for error in errors)


def test_parse_pytest_warnings_summary_after_summary_list_keeps_parent_summary() -> None:
    output = (
        "============================== short test summary info ===============================\n"
        "FAILED w_test.py::test_w - AssertionError: boom\n"
        "================================== warnings summary ==================================\n"
        "conftest.py:5\n"
        "  /tmp/warn/conftest.py:5: DeprecationWarning: late warning\n"
        '    warnings.warn("late warning", DeprecationWarning, stacklevel=1)\n'
        "\n"
        "-- Docs: https://docs.pytest.org/en/stable/how-to/capture-warnings.html\n"
        "============================ 1 failed, 1 warning in 0.01s ============================\n"
    )
    errors = pyfltr.parsing.entry.parse_errors("pytest", output)
    assert len(errors) == 1
    assert errors[0].file == "w_test.py"
    assert errors[0].line == 0
    assert errors[0].message.startswith("test_w: ")


def test_parse_pytest_child_summary_is_not_taken_without_parent_tail_line() -> None:
    output = _QUIET_NESTED_FAILURE
    errors = pyfltr.parsing.entry.parse_errors("pytest", output)
    parent_errors = [e for e in errors if e.file == "tests/p_test.py"]
    assert {e.line for e in parent_errors} == {13, 17}
    assert [e.file for e in errors if e.file not in {"tests/p_test.py"}] == ["child/inner_test.py"]


def test_parse_pytest_parent_summary_without_tail_line_keeps_safety_net() -> None:
    output = (
        "================================= FAILURES =================================\n"
        "_______________________________ test_a ________________________________\n"
        "tests/p_test.py:3: in test_a\n"
        "E   assert 1 == 2\n"
        "-------------------------- Captured stdout call ---------------------------\n"
        "================================= test session starts =================================\n"
        "collected 1 item\n"
        "================================= FAILURES =================================\n"
        "_______________________________ test_inner ________________________________\n"
        "child/inner_test.py:2: in test_inner\n"
        "E   assert 1 == 2\n"
        "========================= short test summary info ==========================\n"
        "FAILED child/inner_test.py::test_inner - assert 1 == 2\n"
        "================================= 1 failed in 0.04s =================================\n"
        "_______________________________ test_b ________________________________\n"
        "tests/p_test.py:7: in test_b\n"
        "E   assert 3 == 4\n"
        "========================= short test summary info ==========================\n"
        "FAILED tests/p_test.py::test_a - assert 1 == 2\n"
        "FAILED tests/p_test.py::test_b - assert 3 == 4\n"
    )
    errors = pyfltr.parsing.entry.parse_errors("pytest", output)
    assert len(errors) == 2
    assert all(e.file == "tests/p_test.py" for e in errors)
    assert {e.line for e in errors} == {3, 7}


@pytest.mark.parametrize("with_failures_section", [False, True])
def test_parse_pytest_run_after_parent_tail_line_keeps_parent_failures(with_failures_section: bool) -> None:
    failures_section = (
        "================================= FAILURES =================================\n"
        "_______________________________ test_a ________________________________\n"
        "tests/p_test.py:3: in test_a\n"
        "E   assert 1 == 2\n"
        "_______________________________ test_b ________________________________\n"
        "tests/p_test.py:7: in test_b\n"
        "E   assert 3 == 4\n"
    )
    output = (
        (failures_section if with_failures_section else "")
        + "========================= short test summary info ==========================\n"
        "FAILED tests/p_test.py::test_a - assert 1 == 2\n"
        "FAILED tests/p_test.py::test_b - assert 3 == 4\n"
        "================================= 2 failed in 0.50s =================================\n"
        "================================= test session starts =================================\n"
        "collected 1 item\n"
    )
    errors = pyfltr.parsing.entry.parse_errors("pytest", output)
    assert len(errors) == 2
    assert all(e.file == "tests/p_test.py" for e in errors)
    assert {e.line for e in errors} == ({3, 7} if with_failures_section else {0})


def test_parse_pytest_parent_failure_in_shared_test_file_is_kept() -> None:
    output = (
        "================================= FAILURES =================================\n"
        "_______________________________ test_runs_child ________________________________\n"
        "tests/p_test.py:13: in test_runs_child\n"
        "E   AssertionError: assert 1 == 0\n"
        "-------------------------- Captured stdout call ---------------------------\n"
        "_______________________________ test_same ________________________________\n"
        "shared/s_test.py:3: in test_same\n"
        "E   assert 1 == 2\n"
        "========================= short test summary info ==========================\n"
        "FAILED shared/s_test.py::test_same - assert 1 == 2\n"
        "1 failed in 0.03s\n"
        "_______________________________ test_same ________________________________\n"
        "shared/s_test.py:3: in test_same\n"
        "E   assert 1 == 2\n"
        "================================= 2 failed in 0.89s =================================\n"
    )
    errors = pyfltr.parsing.entry.parse_errors("pytest", output)
    assert any(e.file == "tests/p_test.py" and e.line == 13 for e in errors)
    assert any(e.file == "shared/s_test.py" and e.line == 3 for e in errors)


# 既定のトレースバック形式が例外の連鎖のエントリー間へ出力する区切り行の2形。
# `_pytest/_io/terminalwriter.py`の`sep`は`_ `を並べたのち行幅の余りへ`_`を1文字足すため、
# 行幅が偶数だと末尾が空白、奇数だと末尾が`_`になる。Windowsでは同関数が行幅を1減らすため
# 常に奇数側へ寄る。ブロック見出しの照合はいずれの形も見出しとして採ってはならない。
_PYTEST_ENTRY_SEPARATORS = ["_ " * 39, "_ " * 39 + "_"]


@pytest.mark.parametrize("separator", _PYTEST_ENTRY_SEPARATORS)
@pytest.mark.parametrize("with_failures_section", [False, True])
def test_parse_pytest_interrupt_traceback_after_summary_keeps_parent_summary(
    with_failures_section: bool, separator: str
) -> None:
    failures_section = (
        "====================================== FAILURES ======================================\n"
        "_______________________________________ test_a _______________________________________\n"
        "\n"
        "    def test_a():\n"
        ">       assert 1 == 2\n"
        "E       assert 1 == 2\n"
        "\n"
        "k_test.py:2: AssertionError\n"
    )
    output = (
        (failures_section if with_failures_section else "")
        + "============================== short test summary info ===============================\n"
        "FAILED k_test.py::test_a - assert 1 == 2\n"
        "!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!! KeyboardInterrupt !!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!\n"
        "cls = <class '_pytest.runner.CallInfo'>, when = 'call'\n"
        "\n"
        "    @classmethod\n"
        "    def from_call(cls, func, when):\n"
        ">       result: TResult | None = func()\n"
        "\n"
        "/home/aki/pyfltr/.venv/lib/python3.11/site-packages/_pytest/runner.py:346: \n"
        f"{separator}\n"
        "\n"
        "    def test_b():\n"
        ">       raise KeyboardInterrupt\n"
        "E       KeyboardInterrupt\n"
        "\n"
        "k_test.py:6: KeyboardInterrupt\n"
        "================================= 1 failed in 0.46s ==================================\n"
    )
    errors = pyfltr.parsing.entry.parse_errors("pytest", output)
    assert len(errors) == 1
    assert errors[0].file == "k_test.py"
    assert errors[0].line == (2 if with_failures_section else 0)
    assert errors[0].message.startswith("test_a: assert 1 == 2")


@pytest.mark.parametrize("separator", _PYTEST_ENTRY_SEPARATORS)
def test_parse_pytest_entry_separator_does_not_split_failure_block(separator: str) -> None:
    output = (
        "====================================== FAILURES ======================================\n"
        "____________________________________ test_chained ____________________________________\n"
        "\n"
        "    def inner():\n"
        '>       raise ValueError("inner")\n'
        "E       ValueError: inner\n"
        "\n"
        "c_test.py:4: ValueError\n"
        "\n"
        "The above exception was the direct cause of the following exception:\n"
        f"{separator}\n"
        "\n"
        "    def test_chained():\n"
        "        try:\n"
        "            inner()\n"
        "        except ValueError as exc:\n"
        '>           raise RuntimeError("outer") from exc\n'
        "E           RuntimeError: outer\n"
        "\n"
        "c_test.py:11: RuntimeError\n"
        "============================== short test summary info ===============================\n"
        "FAILED c_test.py::test_chained - RuntimeError: outer\n"
        "================================= 1 failed in 0.05s ==================================\n"
    )
    errors = pyfltr.parsing.entry.parse_errors("pytest", output)
    assert len(errors) == 1
    assert errors[0].file == "c_test.py"
    assert errors[0].line == 11
    assert errors[0].message == "test_chained: RuntimeError: outer"


def test_parse_pytest_child_summary_is_not_used_for_completion_when_parent_summary_follows() -> None:
    output = (
        "================================= FAILURES =================================\n"
        "_______________________________ test_runs_child ________________________________\n"
        "tests/p_test.py:13: in test_runs_child\n"
        "E   AssertionError: assert 1 == 0\n"
        "-------------------------- Captured stdout call ---------------------------\n"
        "================================= FAILURES =================================\n"
        "_______________________________ test_inner ________________________________\n"
        "child/inner_test.py:3: in test_inner\n"
        "E   assert 1 == 2\n"
        "========================= short test summary info ==========================\n"
        "FAILED child/inner_test.py::test_inner - assert 1 == 2\n"
        "1 failed in 0.03s\n"
        "========================= short test summary info ==========================\n"
        "FAILED tests/p_test.py::test_runs_child - AssertionError: assert 1 == 0\n"
        "FAILED tests/p_test.py::test_no_block - AssertionError: no block\n"
        "================================= 2 failed in 0.89s =================================\n"
    )
    errors = pyfltr.parsing.entry.parse_errors("pytest", output)
    assert len(errors) == 2
    assert all(e.file == "tests/p_test.py" for e in errors)
    by_test = {e.message.split(":")[0]: e for e in errors}
    assert by_test["test_runs_child"].line == 13
    assert by_test["test_no_block"].line == 0


def test_parse_summary_pytest() -> None:
    output = (
        "============================= test session starts ==============================\n"
        "collected 25 items\n"
        "\n"
        "tests/foo_test.py .........................                                [100%]\n"
        "\n"
        "============================== 25 passed in 1.23s ==============================\n"
    )
    result = pyfltr.parsing.entry.parse_summary("pytest", output)
    assert result == "25 passed in 1.23s"


def test_parse_summary_pytest_long_duration() -> None:
    output = "============================== 25 passed in 60.00s (0:01:00) ==============================\n"
    result = pyfltr.parsing.entry.parse_summary("pytest", output)
    assert result == "25 passed in 60.00s (0:01:00)"


_ERRORS_SHORT = """tests/setup_test.py EE                                                   [ 40%]
tests/teardown_test.py FE.E                                              [ 80%]
tests/ok_fail_test.py F                                                  [100%]

==================================== ERRORS ====================================
________________________ ERROR at setup of test_setup_a ________________________
conftest.py:5: in broken_setup
    raise RuntimeError("setup boom")
E   RuntimeError: setup boom
___________________ ERROR at teardown of test_teardown_fail ____________________
conftest.py:10: in broken_teardown
    raise RuntimeError("teardown boom")
E   RuntimeError: teardown boom
=================================== FAILURES ===================================
______________________________ test_teardown_fail ______________________________
tests/teardown_test.py:2: in test_teardown_fail
    assert 1 == 2
E   assert 1 == 2
_______________________________ test_plain_fail ________________________________
tests/ok_fail_test.py:2: in test_plain_fail
    assert 3 == 4
E   assert 3 == 4
=========================== short test summary info ============================
FAILED tests/teardown_test.py::test_teardown_fail - assert 1 == 2
FAILED tests/ok_fail_test.py::test_plain_fail - assert 3 == 4
ERROR tests/setup_test.py::test_setup_a - RuntimeError: setup boom
ERROR tests/teardown_test.py::test_teardown_fail - RuntimeError: teardown boom
==================== 2 failed, 1 passed, 2 errors in 0.05s =====================
"""


def test_pytest_setup_and_teardown_errors_keep_test_files() -> None:
    errors = pyfltr.parsing.pytest.parse_pytest(_ERRORS_SHORT)
    assert [(error.file, error.line, error.message) for error in errors] == [
        ("tests/teardown_test.py", 2, "test_teardown_fail: assert 1 == 2"),
        ("tests/ok_fail_test.py", 2, "test_plain_fail: assert 3 == 4"),
        ("tests/setup_test.py", 0, "test_setup_a: setup error: RuntimeError: setup boom (conftest.py:5)"),
        ("tests/teardown_test.py", 0, "test_teardown_fail: teardown error: RuntimeError: teardown boom (conftest.py:10)"),
    ]


def test_pytest_setup_error_only() -> None:
    output = """==================================== ERRORS ====================================
________________________ ERROR at setup of test_setup_a ________________________
conftest.py:5: in broken_setup
    raise RuntimeError("setup boom")
E   RuntimeError: setup boom
=========================== short test summary info ============================
ERROR tests/setup_test.py::test_setup_a - RuntimeError: setup boom
=============================== 1 error in 0.04s ===============================
"""
    errors = pyfltr.parsing.pytest.parse_pytest(output)
    assert [(error.file, error.line) for error in errors] == [("tests/setup_test.py", 0)]
    assert errors[0].message.startswith("test_setup_a: setup error: RuntimeError: setup boom")


def test_pytest_error_in_test_file_uses_line() -> None:
    output = """==================================== ERRORS ====================================
_______________________ ERROR at setup of TestX.test_y ________________________
tests/own_test.py:7: in own_fixture
    raise ValueError("bad")
E   ValueError: bad
=========================== short test summary info ============================
ERROR tests/own_test.py::TestX::test_y - ValueError: bad
=============================== 1 error in 0.04s ===============================
"""
    errors = pyfltr.parsing.pytest.parse_pytest(output)
    assert [(error.file, error.line, error.message) for error in errors] == [
        ("tests/own_test.py", 7, "TestX.test_y: setup error: ValueError: bad")
    ]


def test_pytest_collection_error() -> None:
    output = """==================================== ERRORS ====================================
____________________ ERROR collecting tests/collect_test.py ____________________
ImportError while importing test module '/proj/tests/collect_test.py'.
Hint: make sure your test modules/packages have valid Python names.
Traceback:
/usr/lib/python3.15/importlib/__init__.py:88: in import_module
    return _bootstrap._gcd_import(name[level:], package, level)
tests/collect_test.py:1: in <module>
    import not_existing_module_xyz
E   ModuleNotFoundError: No module named 'not_existing_module_xyz'
=========================== short test summary info ============================
ERROR tests/collect_test.py
!!!!!!!!!!!!!!!!!!!! Interrupted: 1 error during collection !!!!!!!!!!!!!!!!!!!!
=============================== 1 error in 0.20s ===============================
"""
    errors = pyfltr.parsing.pytest.parse_pytest(output)
    assert [(error.file, error.line, error.message) for error in errors] == [
        ("tests/collect_test.py", 1, "collection error: ModuleNotFoundError: No module named 'not_existing_module_xyz'")
    ]


def test_pytest_errors_from_summary_only() -> None:
    output = """=========================== short test summary info ============================
ERROR tests/setup_test.py::test_setup_a - RuntimeError: setup boom
ERROR tests/collect_test.py
=============================== 2 errors in 0.02s ==============================
"""
    errors = pyfltr.parsing.pytest.parse_pytest(output)
    assert [(error.file, error.line, error.message) for error in errors] == [
        ("tests/setup_test.py", 0, "test_setup_a: error: RuntimeError: setup boom"),
        ("tests/collect_test.py", 0, "collection error"),
    ]


def test_pytest_errors_without_summary_list_use_traceback_location() -> None:
    output = """==================================== ERRORS ====================================
________________________ ERROR at setup of test_setup_a ________________________
conftest.py:5: in broken_setup
    raise RuntimeError("setup boom")
E   RuntimeError: setup boom
------------------------------ Captured log setup ------------------------------
ERROR    root:conftest.py:4 preparing
=============================== 1 error in 0.04s ===============================
"""
    errors = pyfltr.parsing.pytest.parse_pytest(output)
    assert [(error.file, error.line, error.message) for error in errors] == [
        ("conftest.py", 5, "test_setup_a: setup error: RuntimeError: setup boom")
    ]


def test_pytest_child_errors_are_not_added() -> None:
    output = """==================================== ERRORS ====================================
________________________ ERROR at setup of test_parent ________________________
conftest.py:5: in broken_setup
    raise RuntimeError("setup boom")
E   RuntimeError: setup boom
----------------------------- Captured stdout setup ----------------------------
==================================== ERRORS ====================================
________________________ ERROR at setup of test_child _________________________
child/conftest.py:3: in child_fixture
    raise RuntimeError("child boom")
E   RuntimeError: child boom
=========================== short test summary info ============================
ERROR child/child_test.py::test_child - RuntimeError: child boom
=========================== short test summary info ============================
ERROR tests/parent_test.py::test_parent - RuntimeError: setup boom
=============================== 1 error in 0.30s ===============================
"""
    errors = pyfltr.parsing.pytest.parse_pytest(output)
    assert [(error.file, error.line) for error in errors] == [("tests/parent_test.py", 0)]


def test_pytest_child_errors_in_failure_capture_are_not_added() -> None:
    output = """parent_test.py F                                                         [100%]

=================================== FAILURES ===================================
_______________________________ test_runs_child ________________________________
parent_test.py:6: in test_runs_child
    assert 1 == 0
E   assert 1 == 0
----------------------------- Captured stdout call -----------------------------
E                                                                        [100%]
==================================== ERRORS ====================================
_______________________ ERROR at setup of test_child_err _______________________
child/child_err_test.py:6: in child_broken
    raise RuntimeError('child setup boom')
E   RuntimeError: child setup boom
=========================== short test summary info ============================
ERROR child/child_err_test.py::test_child_err - RuntimeError: child setup boom
1 error in 0.09s

=========================== short test summary info ============================
FAILED parent_test.py::test_runs_child - assert 1 == 0
============================== 1 failed in 5.54s ===============================
"""
    errors = pyfltr.parsing.pytest.parse_pytest(output)
    assert [(error.file, error.line, error.message) for error in errors] == [
        ("parent_test.py", 6, "test_runs_child: assert 1 == 0")
    ]


def test_pytest_parent_error_block_is_not_masked_by_quiet_child_end() -> None:
    output = """============================= test session starts ==============================
rootdir: /proj
collected 1 item

parent_test.py E                                                         [100%]

==================================== ERRORS ====================================
________________________ ERROR at setup of test_parent _________________________
conftest.py:8: in runs_child_then_fails
    raise RuntimeError('parent fixture boom')
E   RuntimeError: parent fixture boom
---------------------------- Captured stdout setup -----------------------------
E                                                                        [100%]
==================================== ERRORS ====================================
_______________________ ERROR at setup of test_child_err _______________________
child/child_err_test.py:6: in child_broken
    raise RuntimeError('child setup boom')
E   RuntimeError: child setup boom
=========================== short test summary info ============================
ERROR child/child_err_test.py::test_child_err - RuntimeError: child setup boom
1 error in 0.08s

=========================== short test summary info ============================
ERROR parent_test.py::test_parent - RuntimeError: parent fixture boom
=============================== 1 error in 6.13s ===============================
"""
    errors = pyfltr.parsing.pytest.parse_pytest(output)
    assert [(error.file, error.line, error.message) for error in errors] == [
        ("parent_test.py", 0, "test_parent: setup error: RuntimeError: parent fixture boom (conftest.py:8)")
    ]


def test_pytest_collection_syntax_error_uses_exception_reason() -> None:
    output = """==================================== ERRORS ====================================
__________________ ERROR collecting tests/sub/broken_test.py ___________________
/usr/lib/python3.15/ast.py:46: in parse
    return compile(source, filename, mode, flags,
E     File "/proj/tests/sub/broken_test.py", line 1
E       def test_x(:
E                  ^
E   SyntaxError: invalid syntax
=========================== short test summary info ============================
ERROR tests/sub/broken_test.py
=============================== 1 error in 0.10s ===============================
"""
    errors = pyfltr.parsing.pytest.parse_pytest(output)
    assert [(error.file, error.line, error.message) for error in errors] == [
        ("tests/sub/broken_test.py", 0, "collection error: SyntaxError: invalid syntax")
    ]


def test_pytest_parent_errors_survive_captured_child_failures() -> None:
    # 捕捉された子の診断が残る既知の制約と分け、親の診断だけを確かめる。
    output = """parent_test.py EEF                                                       [100%]

==================================== ERRORS ====================================
__________________________ ERROR at setup of test_p1 ___________________________
conftest.py:8: in runs_child_then_fails
    raise RuntimeError('parent fixture boom')
E   RuntimeError: parent fixture boom
---------------------------- Captured stdout setup -----------------------------
F                                                                        [100%]
=================================== FAILURES ===================================
_______________________________ test_child_fail ________________________________
child/child_fail_test.py:2: in test_child_fail
    assert 5 == 6
E   assert 5 == 6
=========================== short test summary info ============================
FAILED child/child_fail_test.py::test_child_fail - assert 5 == 6
1 failed in 0.21s

__________________________ ERROR at setup of test_p2 ___________________________
conftest.py:8: in runs_child_then_fails
    raise RuntimeError('parent fixture boom')
E   RuntimeError: parent fixture boom
---------------------------- Captured stdout setup -----------------------------
F                                                                        [100%]
=================================== FAILURES ===================================
_______________________________ test_child_fail ________________________________
child/child_fail_test.py:2: in test_child_fail
    assert 5 == 6
E   assert 5 == 6
=========================== short test summary info ============================
FAILED child/child_fail_test.py::test_child_fail - assert 5 == 6
1 failed in 0.19s

=================================== FAILURES ===================================
___________________________________ test_p3 ____________________________________
parent_test.py:10: in test_p3
    assert 7 == 8
E   assert 7 == 8
=========================== short test summary info ============================
FAILED parent_test.py::test_p3 - assert 7 == 8
ERROR parent_test.py::test_p1 - RuntimeError: parent fixture boom
ERROR parent_test.py::test_p2 - RuntimeError: parent fixture boom
========================= 1 failed, 2 errors in 18.00s ========================="""
    errors = [
        (error.file, error.line, error.message)
        for error in pyfltr.parsing.pytest.parse_pytest(output)
        if error.file != "child/child_fail_test.py"
    ]
    assert errors == [
        ("parent_test.py", 10, "test_p3: assert 7 == 8"),
        ("parent_test.py", 0, "test_p1: setup error: RuntimeError: parent fixture boom (conftest.py:8)"),
        ("parent_test.py", 0, "test_p2: setup error: RuntimeError: parent fixture boom (conftest.py:8)"),
    ]
