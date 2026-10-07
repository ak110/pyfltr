"""two-step formatter共通ヘルパー。

`_run_check_then_write` / `_run_fix_mode` / `_build_commandlines` の共通処理と、
`execute_check_write_two_step` を集約する。
taplo / shfmtはdocstring以外が同一のラッパーだったためduplicate-code是正で統合し、
`dispatcher.py`から本関数を直接呼び出す構成へ変更した。
ruff-format専用処理は `ruff.py`、prettier専用処理は `prettier.py` に分離している。
"""

import functools
import pathlib
import time
import typing

import pyfltr.command.core_
import pyfltr.command.process
import pyfltr.command.runner
import pyfltr.config.config
import pyfltr.config.model
import pyfltr.parsing.entry
import pyfltr.paths
import pyfltr.tools
from pyfltr.command.core_ import CommandResult
from pyfltr.command.snapshot import changed_files, snapshot_file_digests


def execute_check_write_two_step(request: pyfltr.command.core_.ExecutionRequest) -> pyfltr.command.core_.CommandResult:
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
    check_commandline, write_commandline, run_step = _prepare_check_write_execution(request)
    if request.params.fix_mode:
        return _run_fix_mode(request, write_commandline=write_commandline, run_step=run_step, parse_errors=False)

    return _run_check_then_write(
        request, check_commandline=check_commandline, write_commandline=write_commandline, run_step=run_step
    )


def _relative_to_cwd(target: pathlib.Path, *, cwd: pathlib.Path, start_cwd: pathlib.Path) -> str:
    """起点 cwd 相対パスをサブプロジェクト cwd 相対パスへ変換する。"""
    abs_path = target if target.is_absolute() else (start_cwd / target)
    try:
        rel = abs_path.resolve().relative_to(cwd.resolve())
    except (OSError, ValueError):
        return pyfltr.paths.normalize_separators(target)
    return pyfltr.paths.normalize_separators(rel)


def _prepare_check_write_execution(
    request: pyfltr.command.core_.ExecutionRequest,
) -> tuple[list[str], list[str], typing.Callable[[list[str]], pyfltr.command.process.CompletedProcessWithTimeoutInfo]]:
    """同じ要求からcheck/writeの引数と実行器を組み立てる。"""
    common_args = pyfltr.command.runner.resolve_user_args(request.command, request.ctx.config)
    if request.cwd is not None:
        external_targets = [
            _relative_to_cwd(t, cwd=request.cwd, start_cwd=request.ctx.base.start_cwd) for t in request.params.targets
        ]
    else:
        external_targets = [str(t) for t in request.params.targets]
    check_commandline, write_commandline = _build_commandlines(
        request.params.commandline_prefix,
        common_args,
        pyfltr.command.runner.expanduser_args(
            list(pyfltr.config.model.required_command_setting(request.ctx.config.values, request.command, "check-args"))
        ),
        pyfltr.command.runner.expanduser_args(
            list(pyfltr.config.model.required_command_setting(request.ctx.config.values, request.command, "write-args"))
        ),
        request.params.additional_args,
        external_targets,
    )
    return check_commandline, write_commandline, functools.partial(pyfltr.command.process.run_process, request)


def _run_check_then_write(
    request: pyfltr.command.core_.ExecutionRequest,
    *,
    check_commandline: list[str],
    write_commandline: list[str],
    run_step: typing.Callable[[list[str]], "pyfltr.command.process.CompletedProcessWithTimeoutInfo"],
) -> CommandResult:
    """Taplo / shfmt用の通常モード共通処理。

    Step1（check）実行 → rc==0なら早期返却 → Step2（write）実行の順で処理する。
    error_parserは使用せず（taplo/shfmtはエラー解析不要のため）。
    prettier専用のrc>=2即failロジックは含まない（prettier用のヘルパーを別途使う）。
    `run_step`は`_prepare_check_write_execution`が組み立てたcallableで、env・on_output・
    timeout・retry設定を既に束縛済み（commandlineのみ差し替えて呼び出す）。
    """
    # Step1はread-onlyのため内容変化なし。変化検知のためStep1前にスナップショットを取る。
    digests_before = snapshot_file_digests(request.params.targets, base_cwd=request.ctx.base.start_cwd)
    check_proc = run_step(check_commandline)
    check_rc = check_proc.returncode

    if check_rc == 0:
        # 整形不要
        output = check_proc.stdout.strip()
        elapsed = time.perf_counter() - request.start_time
        return CommandResult.from_run(
            command=request.command,
            command_info=request.params.command_info,
            commandline=check_commandline,
            returncode=0,
            files=len(request.params.targets),
            output=output,
            elapsed=elapsed,
            timeout_exceeded=check_proc.timeout_exceeded,
            retry_count=check_proc.retry_count,
        )

    # check段でtimeout超過した場合はStep2をスキップして即座にfailedを返す
    # （同じハングが再現する確率が高く、検証時間を浪費するため）。
    if check_proc.timeout_exceeded:
        output = check_proc.stdout.strip()
        elapsed = time.perf_counter() - request.start_time
        return CommandResult.from_run(
            command=request.command,
            command_info=request.params.command_info,
            commandline=check_commandline,
            returncode=check_rc,
            formatter_failed=True,
            files=len(request.params.targets),
            output=output,
            elapsed=elapsed,
            timeout_exceeded=True,
            retry_count=check_proc.retry_count,
        )

    # Step2: 書き込み
    write_proc = run_step(write_commandline)
    output = write_proc.stdout.strip()
    elapsed = time.perf_counter() - request.start_time

    formatter_failed = write_proc.returncode != 0
    returncode = write_proc.returncode if formatter_failed else 1

    result = CommandResult.from_run(
        command=request.command,
        command_info=request.params.command_info,
        commandline=write_commandline,
        returncode=returncode,
        formatter_failed=formatter_failed,
        files=len(request.params.targets),
        output=check_proc.stdout.strip() if not formatter_failed else output,
        elapsed=elapsed,
        timeout_exceeded=write_proc.timeout_exceeded,
        retry_count=check_proc.retry_count + write_proc.retry_count,
    )
    if not formatter_failed:
        digests_after = snapshot_file_digests(request.params.targets, base_cwd=request.ctx.base.start_cwd)
        changed = digests_after != digests_before
        if changed:
            result.fixed_files = changed_files(digests_before, digests_after)
    return result


def _run_fix_mode(
    request: pyfltr.command.core_.ExecutionRequest,
    *,
    write_commandline: list[str],
    run_step: typing.Callable[[list[str]], "pyfltr.command.process.CompletedProcessWithTimeoutInfo"],
    parse_errors: bool,
    command_type_override: typing.Callable[[bool, int], str] | None = None,
) -> CommandResult:
    """fixモードの共通処理。

    write_commandlineを直接実行し、スナップショット比較で書き込みを検知する。
    taplo / shfmt / prettierのfixモードで使用する。
    `run_step`は`_prepare_check_write_execution`が組み立てたcallableで、env・on_output・
    timeout・retry設定を既に束縛済み（commandlineのみ差し替えて呼び出す）。

    command_type_override: `(formatter_failed, returncode) -> command_type`の関数。
    Noneの場合は `command_info.type` を使う。
    prettierのfixモードはreturncode/formatter_failedに応じてtypeを切り替えるためこのcallbackで吸収する。
    parse_errors: Trueのとき `error_parser.parse_errors` を呼び出す。
    """
    digests_before = snapshot_file_digests(request.params.targets, base_cwd=request.ctx.base.start_cwd)
    write_proc = run_step(write_commandline)
    write_rc = write_proc.returncode
    output = write_proc.stdout.strip()
    elapsed = time.perf_counter() - request.start_time
    digests_after = snapshot_file_digests(request.params.targets, base_cwd=request.ctx.base.start_cwd)
    changed = digests_after != digests_before

    if write_rc != 0:
        formatter_failed = True
        returncode: int = write_rc
    elif changed:
        formatter_failed = False
        returncode = 1
    else:
        formatter_failed = False
        returncode = 0

    errors = (
        pyfltr.parsing.entry.parse_errors(request.command, output, request.params.command_info.error_pattern)
        if parse_errors
        else []
    )

    resolved_type = (
        command_type_override(formatter_failed, returncode)
        if command_type_override is not None
        else request.params.command_info.type
    )
    result = CommandResult.from_run(
        command=request.command,
        command_type=resolved_type,
        commandline=write_commandline,
        returncode=returncode,
        formatter_failed=formatter_failed,
        files=len(request.params.targets),
        output=output,
        elapsed=elapsed,
        errors=errors,
        timeout_exceeded=write_proc.timeout_exceeded,
        retry_count=write_proc.retry_count,
    )
    if not formatter_failed and changed:
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
