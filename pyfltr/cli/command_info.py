"""`command-info` サブコマンドの実装。

設定済みツールの起動方式（runner種別・実行ファイルパス・最終起動コマンドライン）を
副作用無しで参照するための導入経路。`mise install` / `mise trust`等は引き起こさない。
`--check`を明示指定したときのみ`ensure_mise_available`を呼んで可用性確認する。
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
import typing

import pyfltr.cli.output_format
import pyfltr.command.dispatcher
import pyfltr.command.mise
import pyfltr.command.runner
import pyfltr.config.config

_OUTPUT_FORMATS: tuple[str, ...] = ("text", "json")
_VALID_OUTPUT_FORMATS: frozenset[str] = frozenset(_OUTPUT_FORMATS)


def register_subparsers(subparsers: typing.Any) -> None:
    """`command-info` サブパーサーを登録する。"""
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
        choices=_OUTPUT_FORMATS,
        default=None,
        help=(
            "出力形式を指定する（text / json、既定: text）。"
            f"未指定時は環境変数 {pyfltr.cli.output_format.OUTPUT_FORMAT_ENV} を、"
            f"{pyfltr.cli.output_format.AI_AGENT_ENV} が設定されていれば json を採用する"
            f"(優先順位: CLI > {pyfltr.cli.output_format.OUTPUT_FORMAT_ENV} > {pyfltr.cli.output_format.AI_AGENT_ENV} > text)。"
        ),
    )
    parser.add_argument(
        "--check",
        default=False,
        action="store_true",
        help="mise 経由ツールについて事前可用性確認 (mise exec --version) を行う。"
        "副作用 (mise install / mise trust) が発生する場合がある。",
    )


def execute_command_info(parser: argparse.ArgumentParser, args: argparse.Namespace) -> int:
    """`command-info` サブコマンドの処理本体。"""
    try:
        config = pyfltr.config.config.load_config()
    except (ValueError, OSError) as e:
        sys.stderr.write(f"設定エラー: {e}\n")
        return 1

    command: str = args.command
    if command not in config.commands:
        sys.stderr.write(f"エラー: 未知のコマンドです: {command}\n")
        return 1

    info = _collect_info(command, config, do_check=bool(args.check))

    output_format = pyfltr.cli.output_format.resolve_output_format(
        parser,
        args.output_format,
        valid_values=_VALID_OUTPUT_FORMATS,
        ai_agent_default="json",
    ).format
    if output_format == "json":
        json.dump(info, sys.stdout, ensure_ascii=False, indent=2)
        sys.stdout.write("\n")
    else:
        _print_text(info)
    return 0 if info.get("resolved", True) else 1


def _collect_info(command: str, config: pyfltr.config.config.Config, *, do_check: bool) -> dict[str, typing.Any]:
    """ツール解決情報を辞書で組み立てる。

    解決自体に失敗した場合も例外は外へ伝播させず、`error`キーを含めたdictを返す
    （`command-info`は調査用途のため、解決失敗そのものも観測したいケースが多い）。
    mise active tools取得状況（status/detail/active_keys）と、tool spec省略採用フラグ・
    判定キーをまとめて露出し、自己診断や名称ずれ検出に利用できるようにする。
    """
    enabled = bool(config.values.get(command, False))
    runner, source = pyfltr.command.runner.resolve_runner(command, config)

    base: dict[str, typing.Any] = {
        "command": command,
        "enabled": enabled,
        "runner": runner,
        "runner_source": source,
        "configured_path": config.values.get(f"{command}-path", ""),
        "configured_args": list(config.values.get(f"{command}-args", [])),
        "severity": pyfltr.config.config.resolve_severity(config.values, command),
        "hints": list(config.values.get(f"{command}-hints", [])),
        "version": config.values.get(f"{command}-version"),
        "dotnet_root": os.environ.get("DOTNET_ROOT"),
    }

    # mise active tools判定で照合に使うキーは、対象コマンドがmise backendに登録されている
    # 場合のみ意味があるため、登録外（python系・js系等）は省略する。
    active_tool_key = pyfltr.command.runner.get_mise_active_tool_key(command)
    if active_tool_key is not None:
        base["mise_active_tool_key"] = active_tool_key

    try:
        # `--check` 真時のみmise設定判定の副作用（trust経由再実行）も許可する。
        # `--check` 偽時は副作用なし契約を維持し、mise設定判定も副作用OFFで動かす
        # （未信頼config由来エラーや取得失敗を「記述なし」扱いとして従来形を返す）。
        resolved = pyfltr.command.runner.build_commandline(command, config, allow_side_effects=do_check)
    except (ValueError, FileNotFoundError) as e:
        base["resolved"] = False
        base["error"] = str(e)
        return base

    base["resolved"] = True
    base["effective_runner"] = resolved.effective_runner
    base["executable"] = resolved.executable
    base["mise_tool_spec_omitted"] = resolved.tool_spec_omitted

    # uv経路（{command}-runner = "python-runner" / "uv" / "uvx" 設定時）の診断情報を露出する。
    # uv/uv.lock/uvx の状態に応じて direct フォールバックが発生したかも観測可能にする。
    # 判定はper-tool設定値`runner`を参照する（`effective_runner`はフォールバック後の最終値で
    # `python-runner = "direct"`時もuv_info省略の判断に流用できないため）。
    if command in pyfltr.command.runner.PYTHON_TOOL_BIN and runner in {"python-runner", "uv", "uvx"}:
        # `mode`はeffective値変換と同義。runner == "python-runner"ならグローバル委譲先、
        # runner in {"uv", "uvx"}ならrunner値そのものを採用する。
        mode = str(config["python-runner"]) if runner == "python-runner" else runner
        # `mode == "direct"`の場合はuv経路を一切辿らないため、診断情報は不要として出力しない
        # （他カテゴリの「不要な情報を出力しない」方針と同じ扱い）。
        if mode != "direct":
            uv_present = pyfltr.command.runner.ensure_uv_available()
            uv_lock_present = pyfltr.command.runner.cwd_has_uv_lock()
            uvx_present = pyfltr.command.runner.ensure_uvx_available()
            # `direct_fallback`は「指定モードからdirectへフォールバックした」ことを意味する。
            # path-override経由でdirectに解決された場合はuv/uvx経路を辿っていないためFalseで揃える。
            # mode別の判定: "uv"はuvバイナリとuv.lockの両存在を要求、"uvx"はuvx shimの可用性のみ。
            if resolved.runner_source == "path-override":
                fallback = False
            elif mode == "uv":
                fallback = not (uv_present and uv_lock_present)
            else:
                # mode == "uvx"
                fallback = not uvx_present
            base["uv_info"] = {
                "mode": mode,
                "uv_available": uv_present,
                "uv_lock_present": uv_lock_present,
                "uvx_available": uvx_present,
                "direct_fallback": fallback,
                "python_tool_bin": pyfltr.command.runner.PYTHON_TOOL_BIN[command],
            }
    # 実際に実行されるargv全体（対象ファイル抜き）を表示する。
    # `build_commandline`の戻り値は実行プレフィックスのみで、`{command}-args`等が反映されないため、
    # `build_invocation_argv`経由で通常段の最終argvを組み立てる。
    base["commandline"] = pyfltr.command.runner.build_invocation_argv(
        command, config, list(resolved.commandline), additional_args=[], fix_stage=False
    )
    # fix-argsが定義されているコマンドでは、fix段でもargvが異なるため併記する。
    # textlintは`--format`ペアを除去する特殊経路となる（`build_invocation_argv`内で処理）。
    if config.values.get(f"{command}-fix-args"):
        base["fix_commandline"] = pyfltr.command.runner.build_invocation_argv(
            command, config, list(resolved.commandline), additional_args=[], fix_stage=True
        )
    # directモードではshutil.whichで絶対パスへ解決済み。それ以外（mise / pnpx等）は
    # 起動コマンド名（`mise` / `pnpx`等）がPATH上に存在するかどうかも参考情報として返す。
    base["executable_resolved"] = shutil.which(resolved.executable) or resolved.executable

    # mise経路（effective_runner == "mise"）のときのみ、取得状況を露出する。
    # mise配信外のコマンド（python系・js系・direct）には不要で、ノイズにしかならないため。
    if resolved.effective_runner == "mise":
        # `get_mise_active_tools`は`build_commandline`内で同じ`allow_side_effects`値で
        # キャッシュ済み。ここで再呼び出ししても副作用は再発生せず、直前の取得結果をそのまま得る。
        active_result = pyfltr.command.mise.get_mise_active_tools(config, allow_side_effects=do_check)
        mise_info: dict[str, typing.Any] = {"status": active_result.status}
        if active_result.detail is not None:
            mise_info["detail"] = active_result.detail
        # 取得成功時のみactive_keysを載せる。失敗時は空であるためノイズになる。
        if active_result.status == "ok":
            mise_info["active_keys"] = sorted(active_result.tools.keys())
        base["mise_active_tools"] = mise_info
        # `--check`無しかつ未信頼configでフォールバックした場合だけ、trust試行を発動できる旨を案内する。
        # 他のエラー要因では案内せず、ノイズを増やさない。
        # 他のセクション（`runner: ...`等）と文体を揃えるため常体で書く。
        if not do_check and active_result.status == "untrusted-no-side-effects":
            base["mise_check_hint"] = "`--check`を付けるとtrust試行を行う"

    if do_check:
        try:
            checked = pyfltr.command.runner.ensure_mise_available(resolved, config, command=command)
        except FileNotFoundError as e:
            base["check_passed"] = False
            base["check_error"] = str(e)
        else:
            base["check_passed"] = True
            # mise不在時のフォールバックなどでcommandlineが変化している可能性がある。
            base["check_commandline"] = checked.commandline
            base["check_effective_runner"] = checked.effective_runner

    return base


def _print_text(info: dict[str, typing.Any]) -> None:
    """text形式の出力。セクション見出し付きで関連項目をまとめる。

    情報のないセクションは省略する（常に空セクションを並べると分散感が再発するため）。
    """
    sections: list[tuple[str, list[str]]] = []

    # ## 実行コマンド: 最終的に実行されるargv（対象ファイル抜き）とexecutableパス。
    exec_lines: list[str] = []
    if info.get("resolved"):
        if info.get("fix_commandline"):
            exec_lines.append(f"commandline (fix step): {' '.join(info['fix_commandline'])}")
            exec_lines.append(f"commandline (check step): {' '.join(info['commandline'])}")
        else:
            exec_lines.append(f"commandline: {' '.join(info['commandline'])}")
        exec_lines.append(f"executable: {info['executable']}")
        exec_lines.append(f"executable_resolved: {info['executable_resolved']}")
    else:
        exec_lines.append(f"resolved: false (error: {info.get('error')})")
    sections.append(("## 実行コマンド", exec_lines))

    # ## ランナー解決: runner種別・解決経緯・check結果。
    runner_lines: list[str] = [f"runner: {info['runner']} ({info['runner_source']})"]
    if info.get("resolved"):
        runner_lines.append(f"effective_runner: {info['effective_runner']}")
        # mise経路でtool spec省略採用したかは見た目だけで判別しづらいため明示する。
        if info.get("effective_runner") == "mise":
            runner_lines.append(f"mise_tool_spec_omitted: {info.get('mise_tool_spec_omitted', False)}")
    if "check_passed" in info:
        runner_lines.append(f"check_passed: {info['check_passed']}")
        if not info["check_passed"]:
            runner_lines.append(f"check_error: {info.get('check_error')}")
        elif info.get("check_commandline") and info["check_commandline"] != info.get("commandline"):
            runner_lines.append(f"check_commandline: {' '.join(info['check_commandline'])}")
            runner_lines.append(f"check_effective_runner: {info.get('check_effective_runner')}")
    sections.append(("## ランナー解決", runner_lines))

    # ## uv診断: uv経路ツールのmode・uv/uvx可用性・uv.lock検出・フォールバック状態。uv_infoがある場合のみ表示。
    uv_info = info.get("uv_info")
    if isinstance(uv_info, dict):
        uv_lines: list[str] = [
            f"mode: {uv_info.get('mode')}",
            f"uv_available: {uv_info.get('uv_available')}",
            f"uv_lock_present: {uv_info.get('uv_lock_present')}",
            f"uvx_available: {uv_info.get('uvx_available')}",
            f"direct_fallback: {uv_info.get('direct_fallback')}",
            f"python_tool_bin: {uv_info.get('python_tool_bin')}",
        ]
        sections.append(("## uv診断", uv_lines))

    # ## mise診断: mise取得状況と判定キー。mise経路または該当キーがある場合のみ表示。
    mise_lines: list[str] = []
    if info.get("mise_active_tool_key") is not None:
        mise_lines.append(f"mise_active_tool_key: {info['mise_active_tool_key']}")
    mise_active = info.get("mise_active_tools")
    if isinstance(mise_active, dict):
        mise_lines.append(f"mise_active_tools.status: {mise_active.get('status')}")
        if mise_active.get("detail"):
            mise_lines.append(f"mise_active_tools.detail: {mise_active['detail']}")
        # 取得成功でキーが空の場合は行ごと省略する（他の任意フィールドの省略慣習と揃えるため）。
        keys = mise_active.get("active_keys")
        if keys:
            mise_lines.append(f"mise_active_tools.active_keys: {', '.join(keys)}")
    if info.get("mise_check_hint"):
        mise_lines.append(f"hint: {info['mise_check_hint']}")
    if mise_lines:
        sections.append(("## mise診断", mise_lines))

    # ## 設定: ユーザー設定で上書きされた値のみ表示（情報がない場合はセクション省略）。
    # severityは既定値 "error" の場合のみ表示を省略する（既定値は情報として冗長なため）。
    config_lines: list[str] = [f"enabled: {info['enabled']}"]
    if info.get("configured_path"):
        config_lines.append(f"configured_path: {info['configured_path']}")
    if info.get("configured_args"):
        config_lines.append(f"configured_args: {' '.join(info['configured_args'])}")
    if info.get("severity") and info["severity"] != "error":
        config_lines.append(f"severity: {info['severity']}")
    if info.get("hints"):
        for index, hint_text in enumerate(info["hints"]):
            config_lines.append(f"hints[{index}]: {hint_text}")
    if info.get("version") is not None:
        config_lines.append(f"version: {info['version']}")
    sections.append(("## 設定", config_lines))

    # ## 環境変数: 現状はDOTNET_ROOTのみ（未設定時はセクション省略）。
    env_lines: list[str] = []
    if info.get("dotnet_root"):
        env_lines.append(f"DOTNET_ROOT: {info['dotnet_root']}")
    if env_lines:
        sections.append(("## 環境変数", env_lines))

    # 先頭はどの命令の情報かを即座に示すためのh1見出し。
    print(f"# {info['command']}")
    for heading, lines in sections:
        if not lines:
            continue
        print()
        print(heading)
        print()
        for line in lines:
            print(line)
