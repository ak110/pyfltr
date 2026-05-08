"""two-step formatter共通ヘルパー。

`_run_check_then_write` / `_run_fix_mode` / `_build_commandlines` の共通処理と、
taplo / shfmt から呼ばれる `execute_check_write_two_step` を集約する。
ruff-format専用処理は `ruff.py`、prettier専用処理は `prettier.py` に分離している。
"""

import argparse
import pathlib
import shlex
import time
import typing

import pyfltr.command.error_parser
import pyfltr.command.process
import pyfltr.command.runner
import pyfltr.config.config
from pyfltr.command.core_ import CommandResult
from pyfltr.command.snapshot import changed_files, snapshot_file_digests


def execute_check_write_two_step(
    command: str,
    command_info: pyfltr.config.config.CommandInfo,
    commandline_prefix: list[str],
    config: pyfltr.config.config.Config,
    targets: list[pathlib.Path],
    additional_args: list[str],
    *,
    fix_mode: bool,
    env: dict[str, str],
    on_output: typing.Callable[[str], None] | None,
    start_time: float,
    args: argparse.Namespace,
    is_interrupted: typing.Callable[[], bool] | None = None,
    on_subprocess_start: typing.Callable[[], None] | None = None,
    on_subprocess_end: typing.Callable[[], None] | None = None,
) -> CommandResult:
    """Taplo / shfmt用の2段階実行共通処理（check→writeパターン）。

    checkとwriteが排他のサブコマンド構成を持つツール向け。
    設定から `{command}-args` / `{command}-check-args` / `{command}-write-args` を参照して
    コマンドラインを組み立てる。

    通常モード（fix_mode=False）:

    - Step1: `prefix + args + check-args + additional + targets`を実行
    - Step1 rc == 0 → succeeded（整形不要）
    - Step1 rc != 0 → Step2 `prefix + args + write-args + additional + targets`を実行
      - Step2 rc == 0 → formatted（整形成功）
      - Step2 rc != 0 → failed

    fixモード（fix_mode=True）:

    - Step1をスキップし、直接write-args付きで実行
    - 内容ハッシュスナップショットで書き込みを検知

    `commandline_prefix` はrunner解決済みの実行プレフィックス（例: `["ruff"]`、
    `["uv", "run", "--frozen", "ruff"]`、`["mise", "exec", "--", "ruff"]`）を呼び出し側から渡す。
    Python系ツールの `{command}-path` 既定値は空文字列のため、`config["<tool>-path"]` を
    直接参照する旧実装は動作しなくなる。同種関数を追加する際は本引数経由でプレフィックスを受け取る形を踏襲する。
    """
    common_args: list[str] = pyfltr.command.runner.expanduser_args(list(config[f"{command}-args"]))
    check_commandline, write_commandline = _build_commandlines(
        commandline_prefix,
        common_args,
        pyfltr.command.runner.expanduser_args(list(config[f"{command}-check-args"])),
        pyfltr.command.runner.expanduser_args(list(config[f"{command}-write-args"])),
        additional_args,
        [str(t) for t in targets],
    )

    timeout = pyfltr.config.config.resolve_command_timeout(config.values, command)
    if fix_mode:
        return _run_fix_mode(
            command=command,
            command_info=command_info,
            write_commandline=write_commandline,
            targets=targets,
            env=env,
            args=args,
            start_time=start_time,
            timeout=timeout,
            parse_errors=False,
            is_interrupted=is_interrupted,
            on_output=on_output,
            on_subprocess_start=on_subprocess_start,
            on_subprocess_end=on_subprocess_end,
        )

    return _run_check_then_write(
        command=command,
        command_info=command_info,
        check_commandline=check_commandline,
        write_commandline=write_commandline,
        targets=targets,
        env=env,
        args=args,
        start_time=start_time,
        timeout=timeout,
        is_interrupted=is_interrupted,
        on_output=on_output,
        on_subprocess_start=on_subprocess_start,
        on_subprocess_end=on_subprocess_end,
    )


def _run_check_then_write(
    command: str,
    command_info: pyfltr.config.config.CommandInfo,
    check_commandline: list[str],
    write_commandline: list[str],
    targets: list[pathlib.Path],
    env: dict[str, str],
    args: argparse.Namespace,
    start_time: float,
    *,
    timeout: float | None,
    is_interrupted: typing.Callable[[], bool] | None = None,
    on_output: typing.Callable[[str], None] | None = None,
    on_subprocess_start: typing.Callable[[], None] | None = None,
    on_subprocess_end: typing.Callable[[], None] | None = None,
) -> CommandResult:
    """Taplo / shfmt用の通常モード共通処理。

    Step1（check）実行 → rc==0なら早期返却 → Step2（write）実行の順で処理する。
    error_parserは使用せず（taplo/shfmtはエラー解析不要のため）。
    prettier専用のrc>=2即failロジックは含まない（prettier用のヘルパーを別途使う）。
    """
    # Step1はread-onlyのため内容変化なし。変化検知のためStep1前にスナップショットを取る。
    digests_before = snapshot_file_digests(targets)
    if args.verbose and on_output is not None:
        on_output(f"commandline: {shlex.join(check_commandline)}\n")
    check_proc = pyfltr.command.process.run_subprocess_with_timeout(
        check_commandline,
        env,
        on_output,
        is_interrupted=is_interrupted,
        on_subprocess_start=on_subprocess_start,
        on_subprocess_end=on_subprocess_end,
        timeout=timeout,
    )
    check_rc = check_proc.returncode

    if check_rc == 0:
        # 整形不要
        output = check_proc.stdout.strip()
        elapsed = time.perf_counter() - start_time
        return CommandResult.from_run(
            command=command,
            command_info=command_info,
            commandline=check_commandline,
            returncode=0,
            files=len(targets),
            output=output,
            elapsed=elapsed,
            timeout_exceeded=check_proc.timeout_exceeded,
        )

    # check段でtimeout超過した場合はStep2をスキップして即座にfailedを返す
    # （同じハングが再現する確率が高く、検証時間を浪費するため）。
    if check_proc.timeout_exceeded:
        output = check_proc.stdout.strip()
        elapsed = time.perf_counter() - start_time
        return CommandResult.from_run(
            command=command,
            command_info=command_info,
            commandline=check_commandline,
            returncode=check_rc,
            has_error=True,
            files=len(targets),
            output=output,
            elapsed=elapsed,
            timeout_exceeded=True,
        )

    # Step2: 書き込み
    if args.verbose and on_output is not None:
        on_output(f"commandline: {shlex.join(write_commandline)}\n")
    write_proc = pyfltr.command.process.run_subprocess_with_timeout(
        write_commandline,
        env,
        on_output,
        is_interrupted=is_interrupted,
        on_subprocess_start=on_subprocess_start,
        on_subprocess_end=on_subprocess_end,
        timeout=timeout,
    )
    output = write_proc.stdout.strip()
    elapsed = time.perf_counter() - start_time

    has_error = write_proc.returncode != 0
    returncode = write_proc.returncode if has_error else 1

    result = CommandResult.from_run(
        command=command,
        command_info=command_info,
        commandline=write_commandline,
        returncode=returncode,
        has_error=has_error,
        files=len(targets),
        output=check_proc.stdout.strip() if not has_error else output,
        elapsed=elapsed,
        timeout_exceeded=write_proc.timeout_exceeded,
    )
    if not has_error:
        digests_after = snapshot_file_digests(targets)
        changed = digests_after != digests_before
        if changed:
            result.fixed_files = changed_files(digests_before, digests_after)
    return result


def _run_fix_mode(
    command: str,
    command_info: pyfltr.config.config.CommandInfo,
    write_commandline: list[str],
    targets: list[pathlib.Path],
    env: dict[str, str],
    args: argparse.Namespace,
    start_time: float,
    *,
    timeout: float | None,
    parse_errors: bool,
    command_type_override: typing.Callable[[bool, int], str] | None = None,
    is_interrupted: typing.Callable[[], bool] | None = None,
    on_output: typing.Callable[[str], None] | None = None,
    on_subprocess_start: typing.Callable[[], None] | None = None,
    on_subprocess_end: typing.Callable[[], None] | None = None,
) -> CommandResult:
    """fixモードの共通処理。

    write_commandlineを直接実行し、スナップショット比較で書き込みを検知する。
    taplo / shfmt / prettierのfixモードで使用する。

    command_type_override: `(has_error, returncode) -> command_type`の関数。
    Noneの場合は `command_info.type` を使う。
    prettierのfixモードはreturncode/has_errorに応じてtypeを切り替えるためこのcallbackで吸収する。
    parse_errors: Trueのとき `error_parser.parse_errors` を呼び出す。
    """
    digests_before = snapshot_file_digests(targets)
    if args.verbose and on_output is not None:
        on_output(f"commandline: {shlex.join(write_commandline)}\n")
    write_proc = pyfltr.command.process.run_subprocess_with_timeout(
        write_commandline,
        env,
        on_output,
        is_interrupted=is_interrupted,
        on_subprocess_start=on_subprocess_start,
        on_subprocess_end=on_subprocess_end,
        timeout=timeout,
    )
    write_rc = write_proc.returncode
    output = write_proc.stdout.strip()
    elapsed = time.perf_counter() - start_time
    digests_after = snapshot_file_digests(targets)
    changed = digests_after != digests_before

    if write_rc != 0:
        has_error = True
        returncode: int = write_rc
    elif changed:
        has_error = False
        returncode = 1
    else:
        has_error = False
        returncode = 0

    errors = pyfltr.command.error_parser.parse_errors(command, output, command_info.error_pattern) if parse_errors else []

    resolved_type = command_type_override(has_error, returncode) if command_type_override is not None else command_info.type
    result = CommandResult.from_run(
        command=command,
        command_type=resolved_type,
        commandline=write_commandline,
        returncode=returncode,
        has_error=has_error,
        files=len(targets),
        output=output,
        elapsed=elapsed,
        errors=errors,
        timeout_exceeded=write_proc.timeout_exceeded,
    )
    if not has_error and changed:
        result.fixed_files = changed_files(digests_before, digests_after)
    return result


def _build_commandlines(
    commandline_prefix: list[str],
    common_args: list[str],
    check_args: list[str],
    write_args: list[str],
    additional_args: list[str],
    target_strs: list[str],
) -> tuple[list[str], list[str]]:
    """check用・write用のコマンドラインを組み立てて返す。

    taplo / shfmt / prettierで共通のコマンドライン構築パターンをまとめる。
    戻り値は `(check_commandline, write_commandline)` のタプル。
    """
    check_commandline: list[str] = [
        *commandline_prefix,
        *common_args,
        *check_args,
        *additional_args,
        *target_strs,
    ]
    write_commandline: list[str] = [
        *commandline_prefix,
        *common_args,
        *write_args,
        *additional_args,
        *target_strs,
    ]
    return check_commandline, write_commandline
