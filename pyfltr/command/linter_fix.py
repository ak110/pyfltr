"""fixモードでのlinter実行。"""

import time

import pyfltr.command.core_
import pyfltr.command.process
import pyfltr.config.config
import pyfltr.config.model
from pyfltr.command.core_ import CommandResult
from pyfltr.command.snapshot import changed_files, snapshot_file_digests

logger = __import__("logging").getLogger(__name__)


def execute_linter_fix(request: pyfltr.command.core_.ExecutionRequest) -> pyfltr.command.core_.CommandResult:
    """Fixモードでのlinter実行 （fix-argsを適用して単発実行）。

    ステータス判定:
    - returncode != 0 → failed （ファイル変化に関係なく、エラーを無視しない）
    - returncode == 0かつ内容ハッシュに変化あり → formatted（command_typeを
      "formatter"に差し替えて既存のstatusプロパティに委ねる）
    - returncode == 0かつ変化なし → succeeded

    ruff-checkは残存違反があるとrc=1を返すが、この設計ではfailedとして扱う。
    未修正の違反はユーザーが後段で認識すべき情報であり、成功へ統合しない方針。
    """
    digests_before = snapshot_file_digests(request.params.targets, base_cwd=request.ctx.base.start_cwd)

    # dispatcher._run_plain_commandもこの単発実行の骨格（run_configured_subprocess呼び出し +
    # returncode/output/elapsedの取り出し）を共有するが、本関数はハッシュ差分によるfix検知を
    # 担う別責務のため統合しない。
    proc = pyfltr.command.process.run_process(request)
    returncode = proc.returncode
    output = proc.stdout.strip()
    elapsed = time.perf_counter() - request.start_time

    digests_after = snapshot_file_digests(request.params.targets, base_cwd=request.ctx.base.start_cwd)
    changed = digests_after != digests_before

    step_failed = returncode != 0
    if not step_failed and changed:
        # fixが適用されたのでformatter扱いでformattedにする
        result_command_type: str = "formatter"
        returncode = 1
    else:
        result_command_type = "linter"

    errors = request.parse_errors(output, None)

    result = CommandResult.from_run(
        command=request.command,
        command_type=result_command_type,
        commandline=request.params.commandline,
        returncode=returncode,
        files=len(request.params.targets),
        output=output,
        elapsed=elapsed,
        errors=errors,
        timeout_exceeded=proc.timeout_exceeded,
        retry_count=proc.retry_count,
    )
    if not step_failed and changed:
        result.fixed_files = changed_files(digests_before, digests_after)
    return result
