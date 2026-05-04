# pylint: disable=duplicate-code  # process.run_subprocess呼び出しの引数列が他経路と類似
"""fixモードでのlinter実行。"""

import argparse
import pathlib
import shlex
import time
import typing

import pyfltr.command.error_parser
import pyfltr.command.process
import pyfltr.config.config
from pyfltr.command.core_ import CommandResult
from pyfltr.command.snapshot import changed_files, snapshot_file_digests

logger = __import__("logging").getLogger(__name__)


def execute_linter_fix(
    command: str,
    command_info: "typing.Any",
    commandline: list[str],
    targets: list[pathlib.Path],
    config: pyfltr.config.config.Config,
    env: dict[str, str],
    on_output: typing.Callable[[str], None] | None,
    start_time: float,
    args: argparse.Namespace,
    *,
    is_interrupted: typing.Callable[[], bool] | None = None,
    on_subprocess_start: typing.Callable[[], None] | None = None,
    on_subprocess_end: typing.Callable[[], None] | None = None,
) -> CommandResult:
    """Fixモードでのlinter実行 （fix-argsを適用して単発実行）。

    ステータス判定:
    - returncode != 0 → failed （ファイル変化に関係なく、エラーを無視しない）
    - returncode == 0かつ内容ハッシュに変化あり → formatted（command_typeを
      "formatter"に差し替えて既存のstatusプロパティに委ねる）
    - returncode == 0かつ変化なし → succeeded

    ruff-checkは残存違反があるとrc=1を返すが、この設計ではfailedとして扱う。
    未修正の違反はユーザーが後段で認識すべき情報であり、成功へ統合しない方針。
    """
    del command_info  # 呼び出し側との引数形式揃えで受け取るのみ（使用しない）

    digests_before = snapshot_file_digests(targets)

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

    digests_after = snapshot_file_digests(targets)
    changed = digests_after != digests_before

    has_error = returncode != 0
    if not has_error and changed:
        # fixが適用されたのでformatter扱いでformattedにする
        result_command_type: str = "formatter"
        returncode = 1
    else:
        result_command_type = "linter"

    errors = pyfltr.command.error_parser.parse_errors(command, output, None)

    result = CommandResult.from_run(
        command=command,
        command_type=result_command_type,
        commandline=commandline,
        returncode=returncode,
        has_error=has_error,
        files=len(targets),
        output=output,
        elapsed=elapsed,
        errors=errors,
        timeout_exceeded=proc.timeout_exceeded,
    )
    if not has_error and changed:
        result.fixed_files = changed_files(digests_before, digests_after)
    return result
