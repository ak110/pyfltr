"""vitest実行。"""

import argparse
import pathlib
import shlex
import tempfile
import time
import typing

import pyfltr.command.error_parser
import pyfltr.command.process
import pyfltr.config.config
from pyfltr.command.core_ import CommandResult
from pyfltr.command.runner import build_invocation_argv

logger = __import__("logging").getLogger(__name__)


def _has_user_reporter_override(args_list: typing.Iterable[str]) -> bool:
    """利用者引数に `--reporter` または `--outputFile` 指定が含まれるかを判定する。

    vitestは `--outputFile.json=...` のようなドット記法やスペース区切り（`--reporter json`）も受け付ける。
    いずれの形式でも検出できるよう、`startswith` でフラグ名のプレフィクスを判定する。
    """
    for arg in args_list:
        if arg == "--reporter" or arg.startswith("--reporter="):
            return True
        if arg == "--outputFile" or arg.startswith("--outputFile="):
            return True
        if arg.startswith("--outputFile."):
            return True
    return False


def execute_vitest(
    command: str,
    command_info: pyfltr.config.config.CommandInfo,
    commandline: list[str],
    commandline_prefix: list[str],
    targets: list[pathlib.Path],
    config: pyfltr.config.config.Config,
    additional_args: list[str],
    env: dict[str, str],
    on_output: typing.Callable[[str], None] | None,
    start_time: float,
    args: argparse.Namespace,
    *,
    is_interrupted: typing.Callable[[], bool] | None = None,
    on_subprocess_start: typing.Callable[[], None] | None = None,
    on_subprocess_end: typing.Callable[[], None] | None = None,
) -> CommandResult:
    """vitestをJSON reporter併用で実行し、失敗を構造化diagnosticへ変換する。

    Vitestはデフォルトで `command.message` フォールバック経路（stdout末尾のtruncate）に倒れ、
    複数のテスト失敗が1つの文字列に結合されてエージェント側で個別解釈できない。
    `--reporter=default --reporter=json --outputFile.json=<tmpfile>` を末尾注入することで、
    利用者向けのデフォルトreporter出力（人間可読のテスト進捗・サマリ）を維持しつつ、
    Jest互換JSONをtmpfile経由で取得して `pyfltr.command.error_parser.parse_errors`
    に渡せるようにする。

    利用者の `vitest-args` または `additional_args` に `--reporter` または `--outputFile`
    指定が含まれる場合は、利用者の制御を尊重して注入をスキップする。
    その場合は `commandline` をそのまま実行し、stdout経由の従来経路で動作する。

    JSON出力からのdiagnostic生成は `_parse_vitest_json` が担い、失敗の `assertionResult` 単位で
    1つの `ErrorLocation` を生成する。
    """
    user_args = list(config.values.get(f"{command}-args", []))
    if _has_user_reporter_override(user_args) or _has_user_reporter_override(additional_args):
        return _run_vitest_subprocess(
            command,
            command_info,
            commandline,
            targets,
            config,
            env,
            on_output,
            start_time,
            args,
            json_output_path=None,
            is_interrupted=is_interrupted,
            on_subprocess_start=on_subprocess_start,
            on_subprocess_end=on_subprocess_end,
        )

    # tmpfileは `delete=False` で確保し、後始末をfinallyで明示する。
    # vitest側がtmpfileを書き込めるよう、Pythonからは開きっぱなしにしない。
    with tempfile.NamedTemporaryFile(prefix="pyfltr-vitest-", suffix=".json", delete=False) as tmp:
        json_path = pathlib.Path(tmp.name)
    try:
        injection_args = [
            "--reporter=default",
            "--reporter=json",
            f"--outputFile.json={json_path}",
        ]
        # `build_invocation_argv` で組み立てたargv末尾に注入引数とtargetsを追加する。
        # `_prepare_execution_params` で構築済みの `commandline` はtarget混入後のため、
        # 注入引数をtargetsより前に置く目的で再構築する。
        argv = build_invocation_argv(command, config, commandline_prefix, additional_args, fix_stage=False)
        argv.extend(injection_args)
        if config.values.get(f"{command}-pass-filenames", True):
            argv.extend(str(t) for t in targets)
        return _run_vitest_subprocess(
            command,
            command_info,
            argv,
            targets,
            config,
            env,
            on_output,
            start_time,
            args,
            json_output_path=json_path,
            is_interrupted=is_interrupted,
            on_subprocess_start=on_subprocess_start,
            on_subprocess_end=on_subprocess_end,
        )
    finally:
        json_path.unlink(missing_ok=True)


def _run_vitest_subprocess(
    command: str,
    command_info: pyfltr.config.config.CommandInfo,
    commandline: list[str],
    targets: list[pathlib.Path],
    config: pyfltr.config.config.Config,
    env: dict[str, str],
    on_output: typing.Callable[[str], None] | None,
    start_time: float,
    args: argparse.Namespace,
    *,
    json_output_path: pathlib.Path | None,
    is_interrupted: typing.Callable[[], bool] | None,
    on_subprocess_start: typing.Callable[[], None] | None,
    on_subprocess_end: typing.Callable[[], None] | None,
) -> CommandResult:
    """vitestをsubprocess起動し、JSON reporter出力をparse_errorsへ渡す。

    `json_output_path` が指定された場合は実行後に当該ファイルを読み込み、
    その内容を `parse_errors` のoutputとして渡す。指定が無い場合は
    stdoutを従来通り `parse_errors` に渡す。
    """
    if args.verbose and on_output is not None:
        on_output(f"commandline: {shlex.join(commandline)}\n")
    proc = pyfltr.command.process.run_subprocess_with_timeout(
        commandline,
        env,
        on_output,
        is_interrupted=is_interrupted,
        on_subprocess_start=on_subprocess_start,
        on_subprocess_end=on_subprocess_end,
        timeout=pyfltr.config.config.resolve_command_timeout(config.values, command),
    )
    returncode = proc.returncode
    output = proc.stdout.strip()
    elapsed = time.perf_counter() - start_time

    parse_source = output
    if json_output_path is not None:
        try:
            parse_source = json_output_path.read_text(encoding="utf-8")
        except OSError:
            # JSON reporter出力がtmpfileへ生成されていない場合（例: vitestがrcエラーで
            # 早期終了）はstdoutベースのフォールバックを使う。
            parse_source = output

    errors = pyfltr.command.error_parser.parse_errors(command, parse_source, command_info.error_pattern)
    return CommandResult.from_run(
        command=command,
        command_info=command_info,
        commandline=commandline,
        returncode=returncode,
        output=output,
        elapsed=elapsed,
        files=len(targets),
        errors=errors,
        timeout_exceeded=proc.timeout_exceeded,
    )
