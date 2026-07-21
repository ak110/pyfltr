"""fixモードでのlinter実行。"""

import argparse
import pathlib
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
    cwd: pathlib.Path | None = None,
    start_cwd: pathlib.Path | None = None,
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

    digests_before = snapshot_file_digests(targets, base_cwd=start_cwd)

    # dispatcher._run_plain_commandもこの単発実行の骨格（run_configured_subprocess呼び出し +
    # returncode/output/elapsedの取り出し）を共有するが、本関数はハッシュ差分によるfix検知を
    # 担う別責務のため統合しない。
    # pylint: disable=duplicate-code
    proc = pyfltr.command.process.run_configured_subprocess(
        command,
        commandline,
        config,
        env,
        on_output,
        verbose=args.verbose,
        is_interrupted=is_interrupted,
        on_subprocess_start=on_subprocess_start,
        on_subprocess_end=on_subprocess_end,
        cwd=cwd,
    )
    returncode = proc.returncode
    output = proc.stdout.strip()
    elapsed = time.perf_counter() - start_time
    # pylint: enable=duplicate-code

    digests_after = snapshot_file_digests(targets, base_cwd=start_cwd)
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
        retry_count=proc.retry_count,
    )
    if not has_error and changed:
        result.fixed_files = changed_files(digests_before, digests_after)
    return result
