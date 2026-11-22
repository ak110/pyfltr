from __future__ import annotations

import argparse
import os
import shutil
import sys
import typing

import pyfltr.cli.command_selection
import pyfltr.cli.output_format
import pyfltr.command.dispatcher
import pyfltr.command.invocation
import pyfltr.command.mise
import pyfltr.command.runner
import pyfltr.command.tool_resolution
import pyfltr.command.tool_version
import pyfltr.config.config
import pyfltr.config.model
import pyfltr.output.auxiliary


def register_subparsers(subparsers: typing.Any) -> None:
    parser = subparsers.add_parser(
        "command-info",
        help="ツール起動方式（runner / 実行ファイル / 最終コマンドライン等）の解決結果を表示する。",
    )
    parser.add_argument(
        "command",
        help="情報を表示する対象のツール名（例: cargo-fmt / shellcheck / typos など）。",
    )
    parser.add_argument(
        "--output-format",
        dest="output_format",
        choices=pyfltr.output.auxiliary.OUTPUT_FORMATS,
        default=None,
        help=(
            "出力形式を指定する（text / json / jsonl、既定: text）。"
            f"未指定時は環境変数 {pyfltr.cli.output_format.OUTPUT_FORMAT_ENV} を採用し、"
            f"{' / '.join(pyfltr.cli.output_format.AGENT_INDICATOR_ENVS)} のいずれかが設定されていれば jsonl を採用する。"
            f"(優先順位: CLI > {pyfltr.cli.output_format.OUTPUT_FORMAT_ENV}"
            f" > {' / '.join(pyfltr.cli.output_format.AGENT_INDICATOR_ENVS)} > text)。"
        ),
    )
    parser.add_argument(
        "--check",
        default=False,
        action="store_true",
        help="実行経路と同じ事前確認を行う"
        " (mise 経由ツールの可用性確認 mise exec --version と、パッケージマネージャー系ツールの最低版確認)。"
        "事前確認に成功した場合は実行版を取得し check_installed_version へ出力する。"
        "副作用 (mise install / mise trust / --version 起動) が発生する場合がある。",
    )


def execute_command_info(parser: argparse.ArgumentParser, args: argparse.Namespace) -> int:
    try:
        config = pyfltr.config.config.load_config()
    except (ValueError, OSError) as e:
        sys.stderr.write(f"設定エラー: {pyfltr.config.config.format_config_error(e, retry='`pyfltr command-info`')}\n")
        return 1

    command: str = args.command
    if command not in config.commands:
        sys.stderr.write(f"エラー: {pyfltr.cli.command_selection.format_unknown_command_message(command, config)}\n")
        return 1

    info = collect_info(command, config, do_check=bool(args.check))

    output_format = pyfltr.cli.output_format.resolve_output_format(
        parser,
        args.output_format,
        valid_values=pyfltr.output.auxiliary.VALID_OUTPUT_FORMATS,
        ai_agent_default="jsonl",
    ).format
    if output_format == "json":
        pyfltr.output.auxiliary.print_json(info, indent=2)
    elif output_format == "jsonl":
        pyfltr.output.auxiliary.print_json(info)
    else:
        _print_text(info)
    return 0 if info.get("resolved", True) else 1


def collect_info(command: str, config: pyfltr.config.model.Config, *, do_check: bool) -> dict[str, typing.Any]:
    enabled = bool(config.values.get(command, False))
    runner, source = pyfltr.command.runner.resolve_runner(command, config)

    base: dict[str, typing.Any] = {
        "command": command,
        "enabled": enabled,
        "runner": runner,
        "runner_source": source,
        "configured_path": pyfltr.config.model.command_setting(config.values, command, "path", ""),
        "configured_args": list(pyfltr.config.model.command_setting(config.values, command, "args", [])),
        "configured_extend_args": list(pyfltr.config.model.command_setting(config.values, command, "extend-args", [])),
        "severity": pyfltr.config.model.resolve_severity(config.values, command),
        "hints": list(pyfltr.config.model.command_setting(config.values, command, "hints", [])),
        "version": pyfltr.config.model.command_setting(config.values, command, "version", None),
        "dotnet_root": os.environ.get("DOTNET_ROOT"),
    }

    active_tool_key = pyfltr.command.runner.get_mise_active_tool_key(command)
    if active_tool_key is not None:
        base["mise_active_tool_key"] = active_tool_key

    try:
        resolved = pyfltr.command.runner.build_commandline(command, config, allow_side_effects=do_check)
    except (ValueError, FileNotFoundError) as e:
        base["resolved"] = False
        base["error"] = (
            pyfltr.command.tool_resolution.format_tool_resolution_failure(command, str(e), config)
            if isinstance(e, FileNotFoundError)
            else str(e)
        )
        if isinstance(e, pyfltr.command.runner.RunnerMismatchError):
            base["error"] += f"。{e.hint}"
        return base

    base["resolved"] = True
    base["effective_runner"] = resolved.effective_runner
    base["executable"] = resolved.executable
    base["mise_tool_spec_omitted"] = resolved.tool_spec_omitted

    if command in pyfltr.command.runner.PYTHON_TOOL_BIN and runner in {"python-runner", "uv", "uvx"}:
        mode = str(config["python-runner"]) if runner == "python-runner" else runner
        if mode != "direct":
            uv_present = pyfltr.command.runner.ensure_uv_available()
            uv_lock_present = pyfltr.command.runner.cwd_has_uv_lock()
            uvx_present = pyfltr.command.runner.ensure_uvx_available()
            if resolved.runner_source == "path-override":
                fallback = False
            elif mode == "uv":
                fallback = not (uv_present and uv_lock_present)
            else:
                fallback = not uvx_present
            base["uv_info"] = {
                "mode": mode,
                "uv_available": uv_present,
                "uv_lock_present": uv_lock_present,
                "uvx_available": uvx_present,
                "direct_fallback": fallback,
                "python_tool_bin": pyfltr.command.runner.PYTHON_TOOL_BIN[command],
            }
    base["commandline"] = pyfltr.command.invocation.build_invocation_argv(
        command, config, list(resolved.commandline), additional_args=[], fix_stage=False
    )
    # fix-argsが定義されているコマンドでは、fix段でもargvが異なるため併記する。
    # textlintは`--format`ペアを除去する特殊経路となる（`build_invocation_argv`内で処理）。
    if pyfltr.config.model.command_setting(config.values, command, "fix-args", []):
        base["fix_commandline"] = pyfltr.command.invocation.build_invocation_argv(
            command, config, list(resolved.commandline), additional_args=[], fix_stage=True
        )
    # 公開論理値pnpxは互換なpnpm dlx形式へ写像されるため、確認対象の実行ファイルはpnpmとなる。
    base["executable_resolved"] = shutil.which(resolved.executable) or resolved.executable

    if resolved.effective_runner == "mise":
        active_result = pyfltr.command.mise.get_mise_active_tools(config, allow_side_effects=do_check)
        mise_info: dict[str, typing.Any] = {"status": active_result.status}
        if active_result.detail is not None:
            mise_info["detail"] = active_result.detail
        if active_result.status == "ok":
            mise_info["active_keys"] = sorted(active_result.tools.keys())
        mise_hints = {
            "mise-not-found": "miseを導入してPATHへ追加し、再実行してください。",
            "untrusted-no-side-effects": (
                "`mise trust`を実行するか、command-infoの --check（MCPではcheck=True）で再確認してください。"
            ),
            "trust-failed": "実行起点で `mise trust` を実行し、mise.tomlの読み取り権限を確認して再実行してください。",
            "exec-error": "実行起点で `mise ls --current --json` を実行し、表示された設定・ツールのエラーを修正してください。",
            "json-parse-error": "`mise ls --current --json`の出力とmiseの版を確認し、JSONを返すmiseを導入してください。",
            "unexpected-shape": (
                "`mise ls --current --json`の出力とmiseの版を確認し、オブジェクト形式を返すmiseを導入してください。"
            ),
        }
        if active_result.status in mise_hints:
            mise_info["hint"] = mise_hints[active_result.status]
        base["mise_active_tools"] = mise_info
        if not do_check and active_result.status == "untrusted-no-side-effects":
            base["mise_check_hint"] = "`--check`を付けるとtrust試行を行う"

    if do_check:
        try:
            checked = pyfltr.command.runner.ensure_mise_available(resolved, config, command=command)
            pyfltr.command.tool_version.ensure_package_manager_version(checked, config, command)
        except (FileNotFoundError, ValueError) as e:
            base["check_passed"] = False
            base["check_error"] = (
                pyfltr.command.tool_resolution.format_tool_resolution_failure(command, str(e), config)
                if isinstance(e, FileNotFoundError)
                else str(e)
            )
            if isinstance(e, pyfltr.command.runner.RunnerMismatchError):
                base["check_error"] += f"。{e.hint}"
        else:
            base["check_passed"] = True
            base["check_commandline"] = pyfltr.command.invocation.build_invocation_argv(
                command, config, list(checked.commandline), additional_args=[], fix_stage=False
            )
            base["check_effective_runner"] = checked.effective_runner
            base["check_installed_version"] = pyfltr.command.tool_version.probe_tool_version_text(checked, config, command)

    return base


def _print_text(info: dict[str, typing.Any]) -> None:
    sections: list[tuple[str, list[str]]] = []

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

    runner_lines: list[str] = [f"runner: {info['runner']} ({info['runner_source']})"]
    if info.get("resolved"):
        runner_lines.append(f"effective_runner: {info['effective_runner']}")
        if info.get("effective_runner") == "mise":
            runner_lines.append(f"mise_tool_spec_omitted: {info.get('mise_tool_spec_omitted', False)}")
    if "check_passed" in info:
        runner_lines.append(f"check_passed: {info['check_passed']}")
        if not info["check_passed"]:
            runner_lines.append(f"check_error: {info.get('check_error')}")
        elif info.get("check_commandline") and info["check_commandline"] != info.get("commandline"):
            runner_lines.append(f"check_commandline: {' '.join(info['check_commandline'])}")
            runner_lines.append(f"check_effective_runner: {info.get('check_effective_runner')}")
        if info.get("check_installed_version"):
            runner_lines.append(f"check_installed_version: {info['check_installed_version']}")
    sections.append(("## ランナー解決", runner_lines))

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

    mise_lines: list[str] = []
    if info.get("mise_active_tool_key") is not None:
        mise_lines.append(f"mise_active_tool_key: {info['mise_active_tool_key']}")
    mise_active = info.get("mise_active_tools")
    if isinstance(mise_active, dict):
        mise_lines.append(f"mise_active_tools.status: {mise_active.get('status')}")
        if mise_active.get("detail"):
            mise_lines.append(f"mise_active_tools.detail: {mise_active['detail']}")
        if mise_active.get("hint"):
            mise_lines.append(f"hint: {mise_active['hint']}")
        keys = mise_active.get("active_keys")
        if keys:
            mise_lines.append(f"mise_active_tools.active_keys: {', '.join(keys)}")
    if info.get("mise_check_hint"):
        mise_lines.append(f"hint: {info['mise_check_hint']}")
    if mise_lines:
        sections.append(("## mise診断", mise_lines))

    config_lines: list[str] = [f"enabled: {info['enabled']}"]
    if info.get("configured_path"):
        config_lines.append(f"configured_path: {info['configured_path']}")
    if info.get("configured_args"):
        config_lines.append(f"configured_args: {' '.join(info['configured_args'])}")
    if info.get("configured_extend_args"):
        config_lines.append(f"configured_extend_args: {' '.join(info['configured_extend_args'])}")
    if info.get("severity") and info["severity"] != "error":
        config_lines.append(f"severity: {info['severity']}")
    if info.get("hints"):
        for index, hint_text in enumerate(info["hints"]):
            config_lines.append(f"hints[{index}]: {hint_text}")
    if info.get("version") is not None:
        config_lines.append(f"version: {info['version']}")
    sections.append(("## 設定", config_lines))

    env_lines: list[str] = []
    if info.get("dotnet_root"):
        env_lines.append(f"DOTNET_ROOT: {info['dotnet_root']}")
    if env_lines:
        sections.append(("## 環境変数", env_lines))

    print(f"# {info['command']}")
    for heading, lines in sections:
        if not lines:
            continue
        print()
        print(heading)
        print()
        for line in lines:
            print(line)
