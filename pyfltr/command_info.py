"""``command-info`` サブコマンドの実装。

設定済みツールの起動方式（runner 種別・実行ファイルパス・最終起動コマンドライン）を
副作用無しで参照するための導入経路。``mise install`` / ``mise trust`` 等は引き起こさない。
``--check`` を明示指定したときのみ ``ensure_mise_available`` を呼んで可用性確認する。
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
import typing

import pyfltr.command
import pyfltr.config


def register_subparsers(subparsers: typing.Any) -> None:
    """``command-info`` サブパーサーを登録する。"""
    parser = subparsers.add_parser(
        "command-info",
        help="ツール起動方式（runner / 実行ファイル / 最終コマンドライン等）の解決結果を表示する。",
    )
    parser.add_argument(
        "command",
        help="情報を表示する対象のツール名（例: cargo-fmt / shellcheck / typos など）。",
    )
    parser.add_argument(
        "--format",
        dest="output_format",
        choices=("text", "json"),
        default="text",
        help="出力形式を指定する（既定: text）。",
    )
    parser.add_argument(
        "--check",
        default=False,
        action="store_true",
        help="mise 経由ツールについて事前可用性確認 (mise exec --version) を行う。"
        "副作用 (mise install / mise trust) が発生する場合がある。",
    )


def execute_command_info(args: argparse.Namespace) -> int:
    """``command-info`` サブコマンドの処理本体。"""
    try:
        config = pyfltr.config.load_config()
    except (ValueError, OSError) as e:
        sys.stderr.write(f"設定エラー: {e}\n")
        return 1

    command: str = args.command
    if command not in config.commands:
        sys.stderr.write(f"エラー: 未知のコマンドです: {command}\n")
        return 1

    info = _collect_info(command, config, do_check=bool(args.check))

    output_format: str = args.output_format
    if output_format == "json":
        json.dump(info, sys.stdout, ensure_ascii=False, indent=2)
        sys.stdout.write("\n")
    else:
        _print_text(info)
    return 0 if info.get("resolved", True) else 1


def _collect_info(command: str, config: pyfltr.config.Config, *, do_check: bool) -> dict[str, typing.Any]:
    """ツール解決情報を辞書で組み立てる。

    解決自体に失敗した場合も例外は外へ伝播させず、``error`` キーを含めた dict を返す
    （``command-info`` は調査用途のため、解決失敗そのものも観測したいケースが多い）。
    """
    enabled = bool(config.values.get(command, False))
    runner, source = pyfltr.command.resolve_runner(command, config)

    base: dict[str, typing.Any] = {
        "command": command,
        "enabled": enabled,
        "runner": runner,
        "runner_source": source,
        "configured_path": config.values.get(f"{command}-path", ""),
        "configured_args": list(config.values.get(f"{command}-args", [])),
        "version": config.values.get(f"{command}-version"),
        "dotnet_root": os.environ.get("DOTNET_ROOT"),
    }

    try:
        resolved = pyfltr.command.build_commandline(command, config)
    except (ValueError, FileNotFoundError) as e:
        base["resolved"] = False
        base["error"] = str(e)
        return base

    base["resolved"] = True
    base["effective_runner"] = resolved.effective_runner
    base["executable"] = resolved.executable
    base["commandline"] = resolved.commandline
    # direct モードでは shutil.which で絶対パスへ解決済み。それ以外（mise / pnpx 等）は
    # 起動コマンド名（``mise`` / ``pnpx`` 等）が PATH 上に存在するかどうかも参考情報として返す。
    base["executable_resolved"] = shutil.which(resolved.executable) or resolved.executable

    if do_check:
        try:
            checked = pyfltr.command.ensure_mise_available(resolved, config, command=command)
        except FileNotFoundError as e:
            base["check_passed"] = False
            base["check_error"] = str(e)
        else:
            base["check_passed"] = True
            # mise 不在時のフォールバックなどで commandline が変化している可能性がある。
            base["check_commandline"] = checked.commandline
            base["check_effective_runner"] = checked.effective_runner

    return base


def _print_text(info: dict[str, typing.Any]) -> None:
    """Text 形式の出力。``key: value`` 形式の行を順に並べる。"""
    print(f"command: {info['command']}")
    print(f"enabled: {info['enabled']}")
    print(f"runner: {info['runner']} ({info['runner_source']})")
    if info.get("resolved"):
        print(f"effective_runner: {info['effective_runner']}")
        print(f"executable: {info['executable']}")
        print(f"executable_resolved: {info['executable_resolved']}")
        print(f"commandline: {' '.join(info['commandline'])}")
    else:
        print(f"resolved: false (error: {info.get('error')})")
    if info.get("configured_path"):
        print(f"configured_path: {info['configured_path']}")
    if info.get("configured_args"):
        print(f"configured_args: {' '.join(info['configured_args'])}")
    if info.get("version") is not None:
        print(f"version: {info['version']}")
    if info.get("dotnet_root"):
        print(f"DOTNET_ROOT: {info['dotnet_root']}")
    if "check_passed" in info:
        print(f"check_passed: {info['check_passed']}")
        if not info["check_passed"]:
            print(f"check_error: {info.get('check_error')}")
        else:
            if info.get("check_commandline") and info["check_commandline"] != info.get("commandline"):
                print(f"check_commandline: {' '.join(info['check_commandline'])}")
                print(f"check_effective_runner: {info.get('check_effective_runner')}")
