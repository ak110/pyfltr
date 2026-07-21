"""two-step formatter共通ヘルパー。

`_run_check_then_write` / `_run_fix_mode` / `_build_commandlines` の共通処理と、
`execute_check_write_two_step` を集約する。
taplo / shfmtはdocstring以外が同一のラッパーだったためduplicate-code是正で統合し、
`dispatcher.py`から本関数を直接呼び出す構成へ変更した。
ruff-format専用処理は `ruff.py`、prettier専用処理は `prettier.py` に分離している。
"""

import argparse
import pathlib
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
    cwd: pathlib.Path | None = None,
    start_cwd: pathlib.Path | None = None,
) -> CommandResult:
    """Taplo / shfmt用の2段階実行共通処理（check→writeパターン）。

    checkとwriteが排他のサブコマンド構成を持つツール向け。
    設定から `{command}-args` / `{command}-extend-args` / `{command}-check-args` /
    `{command}-write-args` を参照してコマンドラインを組み立てる
    （`{command}-args` と `{command}-extend-args` の結合は `resolve_user_args` で集約）。

    通常モード（fix_mode=False）:

    - Step1: `prefix + args + extend-args + check-args + additional + targets`を実行
    - Step1 rc == 0 → succeeded（整形不要）
    - Step1 rc != 0 → Step2 `prefix + args + extend-args + write-args + additional + targets`を実行
      - Step2 rc == 0 → formatted（整形成功）
      - Step2 rc != 0 → failed

    fixモード（fix_mode=True）:

    - Step1をスキップし、`prefix + args + extend-args + write-args + additional + targets`を実行
    - 内容ハッシュスナップショットで書き込みを検知

    `commandline_prefix` はrunner解決済みの実行プレフィックス（例: `["ruff"]`、
    `["uv", "run", "--frozen", "ruff"]`、`["mise", "exec", "--", "ruff"]`）を呼び出し側から渡す。
    Python系ツールの `{command}-path` 既定値は空文字列のため、`config["<tool>-path"]` を
    直接参照する旧実装は動作しなくなる。同種関数を追加する際は本引数経由でプレフィックスを受け取る形を踏襲する。
    """
    # taplo/shfmt向けの本呼び出しとprettier.py側のexecute_prettier_two_stepは、
    # 分岐先ヘルパー（_run_check_then_write / _run_prettier_check_then_write）が異なる別実装のため
    # 統合できないが、_prepare_check_write_executionへの引数受け渡し部分は完全一致する。
    # pylint: disable=duplicate-code
    check_commandline, write_commandline, run_step = _prepare_check_write_execution(
        command,
        commandline_prefix,
        config,
        targets,
        additional_args,
        env,
        on_output,
        args,
        is_interrupted=is_interrupted,
        on_subprocess_start=on_subprocess_start,
        on_subprocess_end=on_subprocess_end,
        cwd=cwd,
        start_cwd=start_cwd,
    )
    # pylint: enable=duplicate-code
    if fix_mode:
        return _run_fix_mode(
            command=command,
            command_info=command_info,
            write_commandline=write_commandline,
            targets=targets,
            run_step=run_step,
            start_time=start_time,
            parse_errors=False,
            start_cwd=start_cwd,
        )

    return _run_check_then_write(
        command=command,
        command_info=command_info,
        check_commandline=check_commandline,
        write_commandline=write_commandline,
        targets=targets,
        run_step=run_step,
        start_time=start_time,
        start_cwd=start_cwd,
    )


def _relative_to_cwd(target: pathlib.Path, *, cwd: pathlib.Path, start_cwd: pathlib.Path) -> str:
    """起点 cwd 相対パスをサブプロジェクト cwd 相対パスへ変換する。"""
    abs_path = target if target.is_absolute() else (start_cwd / target)
    try:
        rel = abs_path.resolve().relative_to(cwd.resolve())
    except (OSError, ValueError):
        return str(target).replace("\\", "/")
    return str(rel).replace("\\", "/")


def _prepare_check_write_execution(
    command: str,
    commandline_prefix: list[str],
    config: pyfltr.config.config.Config,
    targets: list[pathlib.Path],
    additional_args: list[str],
    env: dict[str, str],
    on_output: typing.Callable[[str], None] | None,
    args: argparse.Namespace,
    *,
    is_interrupted: typing.Callable[[], bool] | None = None,
    on_subprocess_start: typing.Callable[[], None] | None = None,
    on_subprocess_end: typing.Callable[[], None] | None = None,
    cwd: pathlib.Path | None = None,
    start_cwd: pathlib.Path | None = None,
) -> tuple[list[str], list[str], typing.Callable[[list[str]], "pyfltr.command.process.CompletedProcessWithTimeoutInfo"]]:
    """check/writeコマンドラインと、それらを実行する`run_step`callableを組み立てる（taplo/shfmt/prettier共通）。

    `execute_check_write_two_step`（taplo/shfmt向け）と`execute_prettier_two_step`は
    通常モード・fixモードいずれの分岐先ヘルパーも異なるため`execute_check_write_two_step`自体は
    共通化できないが、コマンドライン・timeout・retry設定・`run_step`の組み立て部分は
    完全に同一のためここへ集約する。
    """
    common_args: list[str] = pyfltr.command.runner.resolve_user_args(command, config)
    if cwd is not None and start_cwd is not None:
        external_targets = [_relative_to_cwd(t, cwd=cwd, start_cwd=start_cwd) for t in targets]
    else:
        external_targets = [str(t) for t in targets]
    check_commandline, write_commandline = _build_commandlines(
        commandline_prefix,
        common_args,
        pyfltr.command.runner.expanduser_args(list(config[f"{command}-check-args"])),
        pyfltr.command.runner.expanduser_args(list(config[f"{command}-write-args"])),
        additional_args,
        external_targets,
    )
    timeout = pyfltr.config.config.resolve_command_timeout(config.values, command)
    retry_kwargs: dict[str, typing.Any] = pyfltr.config.config.resolve_retry_kwargs(config.values)
    run_step = pyfltr.command.process.traced_subprocess_runner(
        env,
        on_output,
        verbose=args.verbose,
        is_interrupted=is_interrupted,
        on_subprocess_start=on_subprocess_start,
        on_subprocess_end=on_subprocess_end,
        timeout=timeout,
        cwd=cwd,
        **retry_kwargs,
    )
    return check_commandline, write_commandline, run_step


def _run_check_then_write(
    command: str,
    command_info: pyfltr.config.config.CommandInfo,
    check_commandline: list[str],
    write_commandline: list[str],
    targets: list[pathlib.Path],
    run_step: typing.Callable[[list[str]], "pyfltr.command.process.CompletedProcessWithTimeoutInfo"],
    start_time: float,
    *,
    start_cwd: pathlib.Path | None = None,
) -> CommandResult:
    """Taplo / shfmt用の通常モード共通処理。

    Step1（check）実行 → rc==0なら早期返却 → Step2（write）実行の順で処理する。
    error_parserは使用せず（taplo/shfmtはエラー解析不要のため）。
    prettier専用のrc>=2即failロジックは含まない（prettier用のヘルパーを別途使う）。
    `run_step`は`_prepare_check_write_execution`が組み立てたcallableで、env・on_output・
    timeout・retry設定を既に束縛済み（commandlineのみ差し替えて呼び出す）。
    """
    # Step1はread-onlyのため内容変化なし。変化検知のためStep1前にスナップショットを取る。
    digests_before = snapshot_file_digests(targets, base_cwd=start_cwd)
    check_proc = run_step(check_commandline)
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
            retry_count=check_proc.retry_count,
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
            retry_count=check_proc.retry_count,
        )

    # Step2: 書き込み
    write_proc = run_step(write_commandline)
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
        retry_count=check_proc.retry_count + write_proc.retry_count,
    )
    if not has_error:
        digests_after = snapshot_file_digests(targets, base_cwd=start_cwd)
        changed = digests_after != digests_before
        if changed:
            result.fixed_files = changed_files(digests_before, digests_after)
    return result


def _run_fix_mode(
    command: str,
    command_info: pyfltr.config.config.CommandInfo,
    write_commandline: list[str],
    targets: list[pathlib.Path],
    run_step: typing.Callable[[list[str]], "pyfltr.command.process.CompletedProcessWithTimeoutInfo"],
    start_time: float,
    *,
    parse_errors: bool,
    command_type_override: typing.Callable[[bool, int], str] | None = None,
    start_cwd: pathlib.Path | None = None,
) -> CommandResult:
    """fixモードの共通処理。

    write_commandlineを直接実行し、スナップショット比較で書き込みを検知する。
    taplo / shfmt / prettierのfixモードで使用する。
    `run_step`は`_prepare_check_write_execution`が組み立てたcallableで、env・on_output・
    timeout・retry設定を既に束縛済み（commandlineのみ差し替えて呼び出す）。

    command_type_override: `(has_error, returncode) -> command_type`の関数。
    Noneの場合は `command_info.type` を使う。
    prettierのfixモードはreturncode/has_errorに応じてtypeを切り替えるためこのcallbackで吸収する。
    parse_errors: Trueのとき `error_parser.parse_errors` を呼び出す。
    """
    digests_before = snapshot_file_digests(targets, base_cwd=start_cwd)
    write_proc = run_step(write_commandline)
    write_rc = write_proc.returncode
    output = write_proc.stdout.strip()
    elapsed = time.perf_counter() - start_time
    digests_after = snapshot_file_digests(targets, base_cwd=start_cwd)
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
        retry_count=write_proc.retry_count,
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
