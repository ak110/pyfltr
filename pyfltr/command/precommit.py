"""pre-commit・prek実行。

pre-commitとprekは共通の.pre-commit-config.yamlを参照して実行する。
共通の2段階実行ロジック（stage1で試行→失敗時にstage2で再実行）を備える。
"""

import dataclasses
import logging
import os
import pathlib
import shlex
import time
import typing

import pyfltr.command.core_
import pyfltr.command.precommit_guidance
import pyfltr.command.process
import pyfltr.config.config
import pyfltr.config.model
import pyfltr.tools
from pyfltr.command.core_ import CommandResult

logger = logging.getLogger(__name__)


def execute_pre_commit(request: pyfltr.command.core_.ExecutionRequest) -> pyfltr.command.core_.CommandResult:
    """pre-commit・prekの2段階実行。

    stage 1で変更ファイル指定で実行し、fixer系hookがファイルを修正しただけなら
    再実行で成功する（"formatted"）。checker系hookのエラーが残る場合は "failed"
    （formatter_failed=True）として返す。
    """
    # pre-commit・prek配下から起動された場合は自身を再帰実行しない。
    # git commitからフックを経由してpyfltr fastを起動した際の二重実行を防ぐ。
    if pyfltr.command.precommit_guidance.is_running_under_precommit():
        return CommandResult.from_run(
            command=request.command,
            command_info=request.params.command_info,
            commandline=request.params.commandline,
            returncode=None,
            output=f"pre-commit・prek配下で実行されたため{request.command}統合をスキップしました。",
            files=len(request.params.targets),
            elapsed=time.perf_counter() - request.start_time,
        )

    # .pre-commit-config.yamlが存在しなければスキップ
    config_dir = request.cwd if request.cwd is not None else pathlib.Path.cwd()
    config_path = config_dir / ".pre-commit-config.yaml"
    if not config_path.exists():
        return CommandResult.from_run(
            command=request.command,
            command_info=request.params.command_info,
            commandline=request.params.commandline,
            returncode=None,
            output=(
                f".pre-commit-config.yaml が見つからないため{request.command}統合をスキップしました。"
                "フックを使う場合は .pre-commit-config.yaml を作成し、"
                f"使わない場合は `{request.command} = false` で無効化してください。"
            ),
            files=len(request.params.targets),
            elapsed=time.perf_counter() - request.start_time,
        )

    # SKIP環境変数を構築（pyfltr関連hookを除外して再帰を防止）
    integration_command = typing.cast(typing.Literal["pre-commit", "prek"], request.command)
    skip_value = pyfltr.command.precommit_guidance.build_skip_value(request.ctx.config, config_dir, integration_command)
    pre_commit_env = dict(request.env) if request.env is not None else dict(os.environ)
    if skip_value:
        existing_skip = pre_commit_env.get("SKIP", "")
        if existing_skip:
            pre_commit_env["SKIP"] = f"{existing_skip},{skip_value}"
        else:
            pre_commit_env["SKIP"] = skip_value

    if request.verbose and request.ctx.on_output is not None:
        request.ctx.on_output(f"commandline: {shlex.join(request.params.commandline)}\n")
        if skip_value:
            request.ctx.on_output(f"SKIP={pre_commit_env.get('SKIP', '')}\n")

    # stage 1: 実行

    def _run_stage() -> pyfltr.command.process.CompletedProcessWithTimeoutInfo:
        return pyfltr.command.process.run_process(
            dataclasses.replace(request, verbose=False, env=pre_commit_env), request.params.commandline
        )

    proc = _run_stage()
    returncode = proc.returncode
    formatter_failed = False
    timeout_exceeded = proc.timeout_exceeded
    total_retry_count = proc.retry_count
    if timeout_exceeded:
        formatter_failed = True

    # stage 2: 失敗時は再実行（fixerが修正しただけなら2回目で成功する）
    # ただしstage 1でtimeout超過した場合は再実行しない（同じハングが再現する確率が高く時間を浪費するため）。
    if returncode != 0 and not timeout_exceeded:
        if request.verbose and request.ctx.on_output is not None:
            request.ctx.on_output(f"{request.command}: stage 2 再実行\n")
        proc = _run_stage()
        if proc.returncode != 0:
            returncode = proc.returncode
            formatter_failed = True
        if proc.timeout_exceeded:
            timeout_exceeded = True
        total_retry_count += proc.retry_count

    output = proc.stdout.strip()
    elapsed = time.perf_counter() - request.start_time

    return CommandResult.from_run(
        command=request.command,
        command_info=request.params.command_info,
        commandline=request.params.commandline,
        returncode=returncode,
        formatter_failed=formatter_failed,
        files=len(request.params.targets),
        output=output,
        elapsed=elapsed,
        timeout_exceeded=timeout_exceeded,
        retry_count=total_retry_count,
    )
