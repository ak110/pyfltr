"""pytestの失敗の生出力から再実行対象までの境界の結合試験。

例外が実装ファイルで発生した試験と、試験ファイル自身で失敗した試験が混在する出力を解析し、
`retry_command`と`--only-failed`が使う診断の`file`と対象の交差に、失敗した試験ファイルが全て残ることを確かめる。
例外位置を診断の`file`にすると試験ファイルが対象から抜け、失敗が残るのに再実行が成功を返すためである。
生出力はpytest 9.2で`--tb`ごとに採取したものを、作業ディレクトリだけ置き換えて使う。
"""

import pathlib

import pytest

import pyfltr.parsing.entry
import pyfltr.state.retry
from tests.conftest import make_command_result

_FAILURES = {
    "auto": """\
=================================== FAILURES ===================================
____________________________________ test_a ____________________________________

    def test_a():
>       pkg.impl.run()

tests/a_test.py:5: \n_ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ \n
    def run():
>       raise ValueError("impl boom")
E       ValueError: impl boom

pkg/impl.py:2: ValueError
____________________________________ test_b ____________________________________

    def test_b():
>       assert 1 == 2
E       assert 1 == 2

tests/b_test.py:2: AssertionError
""",
    "short": """\
=================================== FAILURES ===================================
____________________________________ test_a ____________________________________
tests/a_test.py:5: in test_a
    pkg.impl.run()
pkg/impl.py:2: in run
    raise ValueError("impl boom")
E   ValueError: impl boom
____________________________________ test_b ____________________________________
tests/b_test.py:2: in test_b
    assert 1 == 2
E   assert 1 == 2
""",
    "line": """\
=================================== FAILURES ===================================
E   ValueError: impl boom
__ROOT__/pkg/impl.py:2: ValueError: impl boom
E   assert 1 == 2
__ROOT__/tests/b_test.py:2: assert 1 == 2
""",
}

_SUMMARY = """\
=========================== short test summary info ============================
FAILED tests/a_test.py::test_a - ValueError: impl boom
FAILED tests/b_test.py::test_b - assert 1 == 2
2 failed, 1 passed in 0.02s
"""


@pytest.mark.parametrize("style", sorted(_FAILURES))
def test_failed_tests_stay_in_retry_targets(style: str, tmp_path: pathlib.Path) -> None:
    targets = [pathlib.Path("tests/a_test.py"), pathlib.Path("tests/b_test.py"), pathlib.Path("tests/c_test.py")]
    for target in [*targets, pathlib.Path("pkg/impl.py")]:
        (tmp_path / target).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / target).write_text("", encoding="utf-8")
    output = _FAILURES[style].replace("__ROOT__", tmp_path.as_posix()) + _SUMMARY

    errors = pyfltr.parsing.entry.parse_errors("pytest", output, path_base=tmp_path, start_cwd=tmp_path)

    impl_failure = next(error for error in errors if error.message.startswith("test_a:"))
    # 試験ファイル上に存在しない行番号を作らず、実装ファイルの例外位置はメッセージで示す
    assert (impl_failure.file, impl_failure.line) == ("tests/a_test.py", 0)
    assert impl_failure.message.endswith(" (pkg/impl.py:2)")
    result = make_command_result("pytest", returncode=1, errors=errors, target_files=targets)
    assert pyfltr.state.retry.filter_failed_files(result, start_cwd=tmp_path) == targets[:2]
