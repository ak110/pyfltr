"""`pyfltr.command.tool_parallelism`のテスト。

ツール自身のワーカー数の推定が、`{command}-args`系の指定とツール設定ファイルの双方から
成立することを検証する。推定を誤ると、サブプロジェクトの同時実行数とツール側の並列度の積が
利用者の指定した`jobs`を超える。
"""

import pathlib

import pytest

import pyfltr.command.tool_parallelism


@pytest.mark.parametrize(
    ("command", "argv", "expected"),
    [
        ("pytest", ["-n", "4"], 4),
        ("pytest", ["--numprocesses=8"], 8),
        ("pytest", ["-n4"], 4),
        ("pytest", ["-n", "auto"], None),  # None はCPU数を意味する（後段で解決する）
        ("pytest", ["-n", "logical"], None),
        ("pylint", ["-j", "2"], 2),
        ("pylint", ["--jobs=3"], 3),
        ("pylint", ["--jobs=0"], None),
        ("pytest", ["-q"], 1),
        ("pytest", [], 1),
        ("mypy", ["--jobs=4"], 1),  # 並列度オプションを持たないツールは常に1
    ],
)
def test_estimate_from_args(tmp_path: pathlib.Path, command: str, argv: list[str], expected: int | None) -> None:
    values = {f"{command}-args": argv}
    actual = pyfltr.command.tool_parallelism.estimate_tool_workers(command, values, tmp_path)
    assert actual == (pyfltr.command.tool_parallelism.cpu_count() if expected is None else expected)


def test_extend_args_are_included(tmp_path: pathlib.Path) -> None:
    """`{command}-extend-args`の指定も推定の入力に含める。"""
    values = {"pytest-args": ["-q"], "pytest-extend-args": ["-n", "6"]}
    assert pyfltr.command.tool_parallelism.estimate_tool_workers("pytest", values, tmp_path) == 6


def test_unparsable_value_falls_back_to_one(tmp_path: pathlib.Path) -> None:
    """整数として解釈できない指定は1として扱い、実行の成否を変えない。"""
    values = {"pytest-args": ["-n", "many"]}
    assert pyfltr.command.tool_parallelism.estimate_tool_workers("pytest", values, tmp_path) == 1


def test_estimate_from_pytest_ini_options(tmp_path: pathlib.Path) -> None:
    """`{command}-args`に指定が無い場合は対象ディレクトリの`pyproject.toml`を読む。"""
    (tmp_path / "pyproject.toml").write_text(
        '[tool.pytest.ini_options]\naddopts = "-q -n 3"\n',
        encoding="utf-8",
    )
    assert pyfltr.command.tool_parallelism.estimate_tool_workers("pytest", {}, tmp_path) == 3


def test_estimate_from_pytest_ini_options_list(tmp_path: pathlib.Path) -> None:
    (tmp_path / "pyproject.toml").write_text(
        '[tool.pytest.ini_options]\naddopts = ["-q", "--numprocesses", "5"]\n',
        encoding="utf-8",
    )
    assert pyfltr.command.tool_parallelism.estimate_tool_workers("pytest", {}, tmp_path) == 5


def test_estimate_from_pylint_table(tmp_path: pathlib.Path) -> None:
    (tmp_path / "pyproject.toml").write_text("[tool.pylint]\njobs = 4\n", encoding="utf-8")
    assert pyfltr.command.tool_parallelism.estimate_tool_workers("pylint", {}, tmp_path) == 4


def test_args_take_precedence_over_config_file(tmp_path: pathlib.Path) -> None:
    """`{command}-args`の指定は設定ファイルより優先する。"""
    (tmp_path / "pyproject.toml").write_text(
        '[tool.pytest.ini_options]\naddopts = "-n 8"\n',
        encoding="utf-8",
    )
    values = {"pytest-args": ["-n", "2"]}
    assert pyfltr.command.tool_parallelism.estimate_tool_workers("pytest", values, tmp_path) == 2


def test_missing_pyproject_returns_one(tmp_path: pathlib.Path) -> None:
    assert pyfltr.command.tool_parallelism.estimate_tool_workers("pytest", {}, tmp_path) == 1
