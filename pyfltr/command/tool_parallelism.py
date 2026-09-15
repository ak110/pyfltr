"""ツール自身が使うワーカー数の推定。

サブプロジェクト単位の並列実行では、pyfltrが同時に起動するツール数と、
各ツールが内部で使うワーカー数の積がホストの負荷になる。
`pylint --jobs=4`のようにツール側で並列化しているツールを複数のサブプロジェクトで
同時に起動すると、利用者が`jobs`で指定した並列度を大きく超える。

本モジュールは、pyfltrの`{command}-args`系設定と、対象ディレクトリのツール設定ファイルから
当該ツールのワーカー数を推定する。推定は見積りであり、解釈できない指定は1件として扱う。
誤った推定は同時実行数の上下にとどまり、検査の成否を変えないため、警告は発行しない。
"""

import os
import pathlib
import re
import typing

import tomlkit

# コマンド名 -> ワーカー数を指定するオプション名の並び。
_PARALLEL_OPTIONS: dict[str, tuple[str, ...]] = {
    "pytest": ("-n", "--numprocesses"),
    "pylint": ("-j", "--jobs"),
}
# ホストの論理CPU数として解釈する値。
_AUTO_VALUES = frozenset({"auto", "logical", "0"})


def cpu_count() -> int:
    """ホストの論理CPU数。取得できない環境では1として扱う。"""
    return os.cpu_count() or 1


def _interpret(raw: str) -> int | None:
    """オプション値をワーカー数へ解釈する。解釈できない値は`None`を返す。"""
    value = raw.strip()
    if value in _AUTO_VALUES:
        return cpu_count()
    try:
        parsed = int(value)
    except ValueError:
        return None
    return parsed if parsed >= 1 else None


def _scan_argv(argv: typing.Sequence[str], options: tuple[str, ...]) -> int | None:
    """引数列からワーカー数の指定を探す。後方の指定を優先する。"""
    found: int | None = None
    for i, arg in enumerate(argv):
        for option in options:
            if arg == option:
                if i + 1 < len(argv):
                    interpreted = _interpret(argv[i + 1])
                    if interpreted is not None:
                        found = interpreted
            elif arg.startswith(f"{option}="):
                interpreted = _interpret(arg[len(option) + 1 :])
                if interpreted is not None:
                    found = interpreted
            elif option.startswith("-") and not option.startswith("--") and arg.startswith(option) and len(arg) > len(option):
                # `-n4`のような値密着形。
                interpreted = _interpret(arg[len(option) :])
                if interpreted is not None:
                    found = interpreted
    return found


def _read_pyproject_workers(cwd: pathlib.Path, command: str, options: tuple[str, ...]) -> int | None:
    """対象ディレクトリの`pyproject.toml`からツール自身の並列度指定を読む。"""
    path = cwd / "pyproject.toml"
    if not path.is_file():
        return None
    try:
        document = tomlkit.parse(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    tool_table = document.get("tool")
    if not isinstance(tool_table, dict):
        return None
    if command == "pytest":
        section = tool_table.get("pytest")
        if not isinstance(section, dict):
            return None
        ini_options = section.get("ini_options")
        if not isinstance(ini_options, dict):
            return None
        addopts = ini_options.get("addopts")
        if isinstance(addopts, str):
            return _scan_argv(re.split(r"\s+", addopts.strip()), options)
        if isinstance(addopts, list):
            return _scan_argv([str(item) for item in addopts], options)
        return None
    section = tool_table.get(command)
    if not isinstance(section, dict):
        return None
    for key in ("jobs", "j"):
        value = section.get(key)
        if value is not None:
            return _interpret(str(value))
    return None


def estimate_tool_workers(command: str, values: dict[str, typing.Any], cwd: pathlib.Path) -> int:
    """当該ツールが1回の起動で使うワーカー数を推定する。

    `{command}-args`と`{command}-extend-args`の指定を優先し、
    指定が無い場合に`cwd`のツール設定ファイルを読む。いずれも持たないツールは1を返す。
    """
    options = _PARALLEL_OPTIONS.get(command)
    if options is None:
        return 1
    argv: list[str] = []
    for key in (f"{command}-args", f"{command}-extend-args"):
        configured = values.get(key)
        if isinstance(configured, list):
            argv.extend(str(item) for item in configured)
    from_argv = _scan_argv(argv, options)
    if from_argv is not None:
        return from_argv
    from_config = _read_pyproject_workers(cwd, command, options)
    return from_config if from_config is not None else 1
