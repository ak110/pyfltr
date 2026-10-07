"""`pyfltr config`サブコマンドのハンドラー実装。

`get` / `set` / `delete` / `list`の4ネストサブコマンドを担う。
本モジュールは`pyfltr/main.py`から呼び出される。
"""

import argparse
import pathlib
import sys
import typing

import pyfltr.cli.output_format
import pyfltr.config.config
import pyfltr.config.editing
import pyfltr.config.model
import pyfltr.config.operations
import pyfltr.config.validation
import pyfltr.output.auxiliary
import pyfltr.warnings_


def execute(parser: argparse.ArgumentParser, args: argparse.Namespace) -> int:
    """`pyfltr config`サブコマンドのディスパッチャ。"""
    action = args.config_action
    if action == "get":
        return _config_get(args)
    if action == "set":
        return _config_set(args)
    if action == "delete":
        return _config_delete(args)
    if action == "list":
        return _config_list(parser, args)
    # argparseのrequired=Trueにより到達しない想定。到達した場合は内部不整合のためfail-fast。
    raise AssertionError(f"未知のconfig action: {action!r}")


def _config_target_path(args: argparse.Namespace) -> pathlib.Path:
    """`--global`の有無に応じて対象ファイルパスを返す。"""
    if args.global_:
        return pyfltr.config.model.default_global_config_path()
    return pathlib.Path("pyproject.toml").absolute()


def _format_config_value_text(value: typing.Any) -> str:
    """`config get` / `config list`のtext出力向けに値を文字列化する。

    pnpm/npm config getの慣例に倣い、boolは小文字、listはカンマ区切り、
    その他は`str()`で表現する。
    """
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, list):
        return ",".join(str(item) for item in value)
    return str(value)


def _config_get(args: argparse.Namespace) -> int:
    """`pyfltr config get <key> [--global]`の実装。"""
    result = _operate(args, "get")
    if result.error is not None:
        return _print_operation_error(result)
    print(_format_config_value_text(result.value))
    return 0


def _config_set(args: argparse.Namespace) -> int:
    """`pyfltr config set <key> <value> [--global]`の実装。"""
    result = _operate(args, "set")
    if result.error is not None:
        return _print_operation_error(result)
    print(f"{args.key} = {_format_config_value_text(result.value)} を {_config_target_path(args)} に書き込みました")
    return 0


def _config_delete(args: argparse.Namespace) -> int:
    """`pyfltr config delete <key> [--global]`の実装。"""
    result = _operate(args, "delete")
    if result.error is not None:
        return _print_operation_error(result)
    path = _config_target_path(args)
    if not result.path_existed:
        print(f"対象ファイルが存在しないため削除対象がありません: {path}")
    elif not result.existed:
        print(f"{args.key} は {path} に書かれていません")
    else:
        print(f"{args.key} を {path} から削除しました")
    return 0


def _config_list(parser: argparse.ArgumentParser, args: argparse.Namespace) -> int:
    """`pyfltr config list [--global] [--all] [--output-format ...]`の実装。

    `--all`指定時はDEFAULT_CONFIGを起点にpyproject.toml値をマージし、
    既定値か明示値かを区別してキー昇順で出力する。
    未指定時は従来通りpyproject.tomlに明示された値のみを挿入順で出力する。
    """
    result = _operate(args, "list")
    if result.error is not None:
        return _print_operation_error(result)
    fmt = pyfltr.cli.output_format.resolve_output_format(
        parser,
        args.output_format,
        valid_values=pyfltr.output.auxiliary.VALID_OUTPUT_FORMATS,
        ai_agent_default="jsonl",
    ).format
    if args.all:
        return _print_all_config_values(fmt, result.values)
    return _print_explicit_config_values(fmt, result.values)


def _print_explicit_config_values(fmt: str, values: dict[str, typing.Any]) -> int:
    """pyproject.tomlに明示された値のみを挿入順で出力する。"""
    if fmt == "text":
        for key, value in values.items():
            print(f"{key} = {_format_config_value_text(value)}")
        return 0
    if fmt == "json":
        pyfltr.output.auxiliary.print_json({"values": values})
        return 0
    if fmt == "jsonl":
        for key, value in values.items():
            pyfltr.output.auxiliary.print_json({"key": key, "value": value})
        return 0
    # argparseのchoicesで除外される想定。到達した場合は内部不整合のためfail-fast。
    raise AssertionError(f"未知の出力形式: {fmt!r}")


def _print_all_config_values(fmt: str, values: dict[str, typing.Any]) -> int:
    """DEFAULT_CONFIG全件をキー昇順で出力する。既定値か明示値かを区別する。"""
    if fmt == "text":
        for key, entry in values.items():
            suffix = " (default)" if entry["default"] else ""
            print(f"{key} = {_format_config_value_text(entry['value'])}{suffix}")
        return 0
    if fmt == "json":
        pyfltr.output.auxiliary.print_json({"values": values})
        return 0
    if fmt == "jsonl":
        for key, entry in values.items():
            pyfltr.output.auxiliary.print_json({"key": key, **entry})
        return 0
    raise AssertionError(f"未知の出力形式: {fmt!r}")


def _operate(args: argparse.Namespace, action: str) -> pyfltr.config.operations.ConfigResult:
    """CLI入力から設定要求を作成し、共通の操作を呼び出す。"""
    return pyfltr.config.operations.execute(
        pyfltr.config.operations.ConfigRequest(
            action=action,
            path=_config_target_path(args),
            key=getattr(args, "key", None),
            value=getattr(args, "value", None),
            use_global=bool(args.global_),
            include_defaults=bool(getattr(args, "all", False)),
        )
    )


def _print_operation_error(result: pyfltr.config.operations.ConfigResult) -> int:
    """想定内の失敗をCLIの文面と終了1へ変換する。"""
    prefixes = {"read": "設定ファイル読込エラー: ", "write": "設定ファイル書き込みエラー: ", "value": "設定値が不正です: "}
    prefix = "" if result.io_error else prefixes.get(result.error_stage, "")
    print(f"{prefix}{result.error}", file=sys.stderr)
    return 1
