"""`pyfltr config`サブコマンドのハンドラー実装。

`get` / `set` / `delete` / `list`の4ネストサブコマンドを担う。
本モジュールは`pyfltr/main.py`から呼び出される。
"""

import argparse
import json
import pathlib
import sys
import typing

import pyfltr.cli.output_format
import pyfltr.config.config
import pyfltr.warnings_

_LIST_OUTPUT_FORMATS: tuple[str, ...] = ("text", "json", "jsonl")
_VALID_LIST_OUTPUT_FORMATS: frozenset[str] = frozenset(_LIST_OUTPUT_FORMATS)


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
        return pyfltr.config.config.default_global_config_path()
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
    path = _config_target_path(args)
    try:
        values = pyfltr.config.config.read_config_values(path)
    except ValueError as e:
        print(f"設定ファイル読込エラー: {e}", file=sys.stderr)
        return 1
    key = args.key
    if key in values:
        print(_format_config_value_text(values[key]))
        return 0
    if key in pyfltr.config.config.DEFAULT_CONFIG:
        print(_format_config_value_text(pyfltr.config.config.DEFAULT_CONFIG[key]))
        return 0
    print(
        pyfltr.config.config.format_unknown_key_message(key, pyfltr.config.config.DEFAULT_CONFIG.keys()),
        file=sys.stderr,
    )
    return 1


def _config_set(args: argparse.Namespace) -> int:
    """`pyfltr config set <key> <value> [--global]`の実装。"""
    path = _config_target_path(args)
    use_global = bool(args.global_)
    if not use_global and not path.exists():
        print(
            f"pyproject.tomlが見つかりません: {path}。global設定（XDG準拠）に書く場合は `--global` を併用してください",
            file=sys.stderr,
        )
        return 1
    key = args.key
    if key not in pyfltr.config.config.DEFAULT_CONFIG:
        print(
            pyfltr.config.config.format_unknown_key_message(key, pyfltr.config.config.DEFAULT_CONFIG.keys()),
            file=sys.stderr,
        )
        return 1
    try:
        value = pyfltr.config.config.parse_config_value(key, args.value)
    except ValueError as e:
        print(f"設定値が不正です: {e}", file=sys.stderr)
        return 1

    # 警告分岐:
    # - archive/cache系をproject側にset → global集約推奨
    # - archive/cache以外をglobal側にset → 通常はproject優先のため上書きされる旨
    if key in pyfltr.config.config.GLOBAL_PRIORITY_KEYS and not use_global:
        pyfltr.warnings_.emit_warning(
            source="config",
            message=(
                f"{key} はarchive/cache系のキーです。マシン共通設定として"
                " --global での設定を推奨します（global側があればglobal優先になります）。"
            ),
        )
    elif key not in pyfltr.config.config.GLOBAL_PRIORITY_KEYS and use_global:
        pyfltr.warnings_.emit_warning(
            source="config",
            message=(
                f"{key} は通常キーのためproject側のpyproject.tomlが優先されます。"
                " globalに書いてもproject側に同じキーがあれば上書きされます。"
            ),
        )

    try:
        pyfltr.config.config.set_config_value(path, key, value, create_if_missing=use_global)
    except (FileNotFoundError, ValueError) as e:
        print(f"設定ファイル書き込みエラー: {e}", file=sys.stderr)
        return 1
    print(f"{key} = {_format_config_value_text(value)} を {path} に書き込みました")
    return 0


def _config_delete(args: argparse.Namespace) -> int:
    """`pyfltr config delete <key> [--global]`の実装。"""
    path = _config_target_path(args)
    key = args.key
    if key not in pyfltr.config.config.DEFAULT_CONFIG:
        print(
            pyfltr.config.config.format_unknown_key_message(key, pyfltr.config.config.DEFAULT_CONFIG.keys()),
            file=sys.stderr,
        )
        return 1
    if not path.exists():
        print(f"対象ファイルが存在しないため削除対象がありません: {path}")
        return 0
    try:
        existed = pyfltr.config.config.delete_config_value(path, key)
    except ValueError as e:
        print(f"設定ファイル読込エラー: {e}", file=sys.stderr)
        return 1
    if not existed:
        print(f"{key} は {path} に書かれていません")
        return 0
    print(f"{key} を {path} から削除しました")
    return 0


def _config_list(parser: argparse.ArgumentParser, args: argparse.Namespace) -> int:
    """`pyfltr config list [--global] [--all] [--output-format ...]`の実装。

    `--all`指定時はDEFAULT_CONFIGを起点にpyproject.toml値をマージし、
    既定値か明示値かを区別してキー昇順で出力する。
    未指定時は従来通りpyproject.tomlに明示された値のみを挿入順で出力する。
    """
    path = _config_target_path(args)
    try:
        values = pyfltr.config.config.read_config_values(path)
    except ValueError as e:
        print(f"設定ファイル読込エラー: {e}", file=sys.stderr)
        return 1
    fmt = pyfltr.cli.output_format.resolve_output_format(
        parser,
        args.output_format,
        valid_values=_VALID_LIST_OUTPUT_FORMATS,
        ai_agent_default="jsonl",
    ).format
    if args.all:
        return _print_all_config_values(fmt, values)
    return _print_explicit_config_values(fmt, values)


def _print_explicit_config_values(fmt: str, values: dict[str, typing.Any]) -> int:
    """pyproject.tomlに明示された値のみを挿入順で出力する。"""
    if fmt == "text":
        for key, value in values.items():
            print(f"{key} = {_format_config_value_text(value)}")
        return 0
    if fmt == "json":
        print(json.dumps({"values": values}, ensure_ascii=False))
        return 0
    if fmt == "jsonl":
        for key, value in values.items():
            print(json.dumps({"key": key, "value": value}, ensure_ascii=False))
        return 0
    # argparseのchoicesで除外される想定。到達した場合は内部不整合のためfail-fast。
    raise AssertionError(f"未知の出力形式: {fmt!r}")


def _print_all_config_values(fmt: str, values: dict[str, typing.Any]) -> int:
    """DEFAULT_CONFIG全件をキー昇順で出力する。既定値か明示値かを区別する。"""
    defaults = pyfltr.config.config.DEFAULT_CONFIG
    keys = sorted(defaults.keys())
    if fmt == "text":
        for key in keys:
            value = values[key] if key in values else defaults[key]
            suffix = "" if key in values else " (default)"
            print(f"{key} = {_format_config_value_text(value)}{suffix}")
        return 0
    if fmt == "json":
        payload = {
            key: {
                "value": values[key] if key in values else defaults[key],
                "default": key not in values,
            }
            for key in keys
        }
        print(json.dumps({"values": payload}, ensure_ascii=False))
        return 0
    if fmt == "jsonl":
        for key in keys:
            is_default = key not in values
            value = defaults[key] if is_default else values[key]
            print(json.dumps({"key": key, "value": value, "default": is_default}, ensure_ascii=False))
        return 0
    # argparseのchoicesで除外される想定。到達した場合は内部不整合のためfail-fast。
    raise AssertionError(f"未知の出力形式: {fmt!r}")
