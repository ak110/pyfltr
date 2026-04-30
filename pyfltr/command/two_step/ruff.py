# pylint: disable=duplicate-code,protected-access
"""ruff-formatの2段階実行。"""

import argparse
import pathlib
import shlex
import time
import typing

import pyfltr.command.error_parser
import pyfltr.command.process
import pyfltr.config.config
from pyfltr.command.core import CommandResult
from pyfltr.command.snapshot import _changed_files, _snapshot_file_digests

logger = __import__("logging").getLogger(__name__)


def _execute_ruff_format_two_step(
    command: str,
    command_info: pyfltr.config.config.CommandInfo,
    format_commandline: list[str],
    targets: list[pathlib.Path],
    config: pyfltr.config.config.Config,
    args: argparse.Namespace,
    env: dict[str, str],
    on_output: typing.Callable[[str], None] | None,
    start_time: float,
    *,
    is_interrupted: typing.Callable[[], bool] | None = None,
    on_subprocess_start: typing.Callable[[], None] | None = None,
    on_subprocess_end: typing.Callable[[], None] | None = None,
) -> CommandResult:
    """ruff-formatの2段階実行 （ruff check --fix → ruff format）。

    ステップ1 （ruff check --fix --unsafe-fixes） の未修正lint violationは無視する。
    別途ruff-checkコマンドで検出される前提。ただしexit >= 2 （設定ミス等） はfailed扱い。
    ステップ1の成否にかかわらずステップ2 （ruff format） は実行する
    （対象ファイル全体のformat適用を止めないため）。
    """
    # ステップ1のコマンドライン組立
    check_commandline: list[str] = [config["ruff-format-path"]]
    check_commandline.extend(config["ruff-format-check-args"])
    check_commandline.extend(str(t) for t in targets)

    # ステップ1実行前の内容ハッシュを記録 （修正適用検知用）
    digests_before = _snapshot_file_digests(targets)

    # ステップ1実行
    if args.verbose and on_output is not None:
        on_output(f"commandline: {shlex.join(check_commandline)}\n")
    step1_proc = pyfltr.command.process._run_subprocess(
        check_commandline,
        env,
        on_output,
        is_interrupted=is_interrupted,
        on_subprocess_start=on_subprocess_start,
        on_subprocess_end=on_subprocess_end,
    )
    step1_rc = step1_proc.returncode
    step1_failed = step1_rc >= 2  # exit 0/1は無視、2以上 （abrupt termination） のみ失敗扱い
    digests_after_step1 = _snapshot_file_digests(targets)
    step1_changed = digests_after_step1 != digests_before

    # ステップ2実行 （常に実行）
    if args.verbose and on_output is not None:
        on_output(f"commandline: {shlex.join(format_commandline)}\n")
    step2_proc = pyfltr.command.process._run_subprocess(
        format_commandline,
        env,
        on_output,
        is_interrupted=is_interrupted,
        on_subprocess_start=on_subprocess_start,
        on_subprocess_end=on_subprocess_end,
    )
    step2_rc = step2_proc.returncode
    step2_formatted = step2_rc == 1
    step2_failed = step2_rc >= 2

    # 出力の合成
    output = (step1_proc.stdout + step2_proc.stdout).strip()
    elapsed = time.perf_counter() - start_time

    # 最終判定
    has_error = step1_failed or step2_failed
    if has_error:
        returncode = step1_rc if step1_failed else step2_rc
    elif step1_changed or step2_formatted:
        returncode = 1
    else:
        returncode = 0

    errors = pyfltr.command.error_parser.parse_errors(command, output, command_info.error_pattern)

    # commandlineは代表として「最後に実行したステップ」（= ruff format） を格納。
    # 両ステップ分のcommandlineはverbose出力で確認可能。
    result = CommandResult.from_run(
        command=command,
        command_info=command_info,
        commandline=format_commandline,
        returncode=returncode,
        has_error=has_error,
        files=len(targets),
        output=output,
        elapsed=elapsed,
        errors=errors,
    )
    if not has_error and (step1_changed or step2_formatted):
        # digests_beforeはStep1前のスナップショット（関数冒頭で取得済み）。
        # Step1（ruff --checkによる暗黙fix）とStep2（ruff format）の累積差分を一括で取る。
        digests_after_step2 = _snapshot_file_digests(targets)
        result.fixed_files = _changed_files(digests_before, digests_after_step2)
    return result
