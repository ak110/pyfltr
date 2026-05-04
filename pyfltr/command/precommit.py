# pylint: disable=duplicate-code  # process.run_subprocess呼び出しの引数列が他経路と類似
"""pre-commit実行。"""

import argparse
import os
import pathlib
import shlex
import time
import typing

import pyfltr.cli.precommit_guidance
import pyfltr.command.process
import pyfltr.config.config
from pyfltr.command.core_ import CommandResult

logger = __import__("logging").getLogger(__name__)


def execute_pre_commit(
    command: str,
    command_info: pyfltr.config.config.CommandInfo,
    commandline: list[str],
    targets: list[pathlib.Path],
    config: pyfltr.config.config.Config,
    args: argparse.Namespace,
    env: dict[str, str] | None,
    on_output: typing.Callable[[str], None] | None,
    start_time: float,
    *,
    is_interrupted: typing.Callable[[], bool] | None = None,
    on_subprocess_start: typing.Callable[[], None] | None = None,
    on_subprocess_end: typing.Callable[[], None] | None = None,
) -> CommandResult:
    """pre-commitの2段階実行。

    stage 1でpre-commit run --all-filesを実行し、fixer系hookがファイルを
    修正しただけなら再実行で成功する（"formatted"）。checker系hookのエラーが
    残る場合は "failed"（has_error=True）として返す。
    """
    # pre-commit配下から起動された場合は自身を再帰実行しない。
    # git commit → pre-commit → pyfltr fast → pre-commitの二重実行を防ぐ。
    if pyfltr.cli.precommit_guidance.is_running_under_precommit():
        return CommandResult.from_run(
            command=command,
            command_info=command_info,
            commandline=commandline,
            returncode=None,
            output="pre-commit 配下で実行されたため pre-commit 統合をスキップしました。",
            files=len(targets),
            elapsed=time.perf_counter() - start_time,
        )

    # .pre-commit-config.yamlが存在しなければスキップ
    config_dir = pathlib.Path.cwd()
    config_path = config_dir / ".pre-commit-config.yaml"
    if not config_path.exists():
        return CommandResult.from_run(
            command=command,
            command_info=command_info,
            commandline=commandline,
            returncode=None,
            output=".pre-commit-config.yaml が見つかりません。",
            files=len(targets),
            elapsed=time.perf_counter() - start_time,
        )

    # SKIP環境変数を構築（pyfltr関連hookを除外して再帰を防止）
    skip_value = pyfltr.cli.precommit_guidance.build_skip_value(config, config_dir)
    pre_commit_env = dict(env) if env is not None else dict(os.environ)
    if skip_value:
        existing_skip = pre_commit_env.get("SKIP", "")
        if existing_skip:
            pre_commit_env["SKIP"] = f"{existing_skip},{skip_value}"
        else:
            pre_commit_env["SKIP"] = skip_value

    if args.verbose and on_output is not None:
        on_output(f"commandline: {shlex.join(commandline)}\n")
        if skip_value:
            on_output(f"SKIP={pre_commit_env.get('SKIP', '')}\n")

    # stage 1: 実行
    timeout = pyfltr.config.config.resolve_command_timeout(config.values, command)
    proc = pyfltr.command.process.run_subprocess_with_timeout(
        commandline,
        pre_commit_env,
        on_output,
        is_interrupted=is_interrupted,
        on_subprocess_start=on_subprocess_start,
        on_subprocess_end=on_subprocess_end,
        timeout=timeout,
    )
    returncode = proc.returncode
    has_error = False
    timeout_exceeded = proc.timeout_exceeded
    if timeout_exceeded:
        has_error = True

    # stage 2: 失敗時は再実行（fixerが修正しただけなら2回目で成功する）
    # ただしstage 1でtimeout超過した場合は再実行しない（同じハングが再現する確率が高く時間を浪費するため）。
    if returncode != 0 and not timeout_exceeded:
        if args.verbose and on_output is not None:
            on_output("pre-commit: stage 2 再実行\n")
        proc = pyfltr.command.process.run_subprocess_with_timeout(
            commandline,
            pre_commit_env,
            on_output,
            is_interrupted=is_interrupted,
            on_subprocess_start=on_subprocess_start,
            on_subprocess_end=on_subprocess_end,
            timeout=timeout,
        )
        if proc.returncode != 0:
            returncode = proc.returncode
            has_error = True
        if proc.timeout_exceeded:
            timeout_exceeded = True

    output = proc.stdout.strip()
    elapsed = time.perf_counter() - start_time

    return CommandResult.from_run(
        command=command,
        command_info=command_info,
        commandline=commandline,
        returncode=returncode,
        has_error=has_error,
        files=len(targets),
        output=output,
        elapsed=elapsed,
        timeout_exceeded=timeout_exceeded,
    )
