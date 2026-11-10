import pyfltr.diagnostics
import pyfltr.output.diagnostics
import pyfltr.parsing.entry
import pyfltr.parsing.pytest
import pyfltr.parsing.pytest_common
import pyfltr.parsing.pytest_config
import pyfltr.parsing.pytest_nested
import pyfltr.tools
import tests.parsing.conftest


def test_mask_pytest_captured_child_runs_keeps_line_structure() -> None:
    """pytest: 除外処理が`\\n`以外の制御文字で行を切らないことを検証する。

    `str.splitlines`はフォームフィード等でも分割するため、除外時に対象の位置へ改行が入り、
    以降の正規表現探索（`re.MULTILINE`は`\\n`のみを行区切りとする）と行の対応が崩れる。
    制御文字は除外対象の範囲の内側へ置く。範囲の外では除外時の置換が起こらず、
    分割の基準が違っても元の文字列が復元されるため、退行を検知できない。
    """
    child = tests.parsing.conftest.child_pytest_run(
        file="child/z_test.py", test="test_phantom", line=2, message="assert 1 == 2"
    ).replace("collected 1 item\n", "collected 1 item\nbefore\x0cafter\n")
    output = (
        "================================= FAILURES =================================\n"
        "_______________________________ test_p ________________________________\n"
        "tests/p_test.py:3: in test_p\n"
        "E   assert 1 == 2\n"
        "-------------------------- Captured stdout call ---------------------------\n"
        + child
        + "========================= short test summary info ==========================\n"
        "FAILED tests/p_test.py::test_p - assert 1 == 2\n"
        "================================= 1 failed in 0.30s =================================\n"
    )
    # pylint: disable=protected-access  # 行構造の保持は公開関数の戻り値へ現れないため直接検証する
    masked = pyfltr.parsing.pytest_nested.mask_pytest_captured_child_runs(output)  # noqa: SLF001
    assert masked.count("\n") == output.count("\n")
    assert len(masked.split("\n")) == len(output.split("\n"))
