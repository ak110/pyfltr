"""ruff-formatの2段階実行。"""

import functools
import time

import pyfltr.command.core_
import pyfltr.command.process
import pyfltr.command.runner
import pyfltr.config.config
import pyfltr.config.model
import pyfltr.parsing.entry
import pyfltr.tools
from pyfltr.command.core_ import CommandResult
from pyfltr.command.snapshot import changed_files, snapshot_file_digests
from pyfltr.command.two_step.base import _relative_to_cwd


def execute_ruff_format_two_step(request: pyfltr.command.core_.ExecutionRequest) -> pyfltr.command.core_.CommandResult:
    """ruff-formatの2段階実行（ruff check --fix → ruff format）。

    ステップ1（ruff check --fix --unsafe-fixes）の未修正lint violationは無視する。
    別途ruff-checkコマンドで検出される前提。ただしexit >= 2（設定ミス等）はfailed扱い。
    ステップ1の成否にかかわらずステップ2（ruff format）は実行する
    （対象ファイル全体のformat適用を止めないため）。
    `commandline_prefix` は `ruff` 単体（またはuv経路では `["uv", "run", "--frozen", "ruff"]`）を渡す。

    `commandline_prefix` はrunner解決済みの実行プレフィックス（例: `["ruff"]`、
    `["uv", "run", "--frozen", "ruff"]`、`["mise", "exec", "--", "ruff"]`）を呼び出し側から渡す。
    Python系ツールの `{command}-path` 既定値は空文字列のため、`config["<tool>-path"]` を
    直接参照する旧実装は動作しなくなる。同種関数を追加する際は本引数経由でプレフィックスを受け取る形を踏襲する。
    """
    check_commandline: list[str] = list(request.params.commandline_prefix)
    check_commandline.extend(pyfltr.command.runner.expanduser_args(list(request.ctx.config["ruff-format-check-args"])))
    if request.cwd is not None and request.ctx.base.start_cwd is not None:
        check_commandline.extend(
            _relative_to_cwd(t, cwd=request.cwd, start_cwd=request.ctx.base.start_cwd) for t in request.params.targets
        )
    else:
        check_commandline.extend(str(t) for t in request.params.targets)

    return _run_ruff_two_step(request, check_commandline=check_commandline)


def _run_ruff_two_step(request: pyfltr.command.core_.ExecutionRequest, *, check_commandline: list[str]) -> CommandResult:
    """ruff-format専用の2段階処理。

    Step1（ruff check --fix --unsafe-fixes）はrc>=2のみ失敗扱いとし、
    rc 0/1に関わらずStep2（ruff format）を常時実行する。
    step1の未修正lint violationは無視し、別途ruff-checkコマンドで検出する前提。
    """
    # ステップ1実行前の内容ハッシュを記録（修正適用検知用）
    digests_before = snapshot_file_digests(request.params.targets, base_cwd=request.ctx.base.start_cwd)

    run_step = functools.partial(pyfltr.command.process.run_process, request)
    step1_proc = run_step(check_commandline)
    step1_rc = step1_proc.returncode
    step1_failed = step1_rc >= 2  # exit 0/1は無視、2以上（abrupt termination）のみ失敗扱い
    digests_after_step1 = snapshot_file_digests(request.params.targets, base_cwd=request.ctx.base.start_cwd)
    step1_changed = digests_after_step1 != digests_before

    # ステップ2実行（常に実行）
    step2_proc = run_step(request.params.commandline)
    step2_rc = step2_proc.returncode
    step2_formatted = step2_rc == 1
    step2_failed = step2_rc >= 2

    # 出力の合成
    output = (step1_proc.stdout + step2_proc.stdout).strip()
    elapsed = time.perf_counter() - request.start_time

    # 最終判定
    timeout_exceeded = step1_proc.timeout_exceeded or step2_proc.timeout_exceeded
    formatter_failed = step1_failed or step2_failed
    if formatter_failed:
        returncode: int = step1_rc if step1_failed else step2_rc
    elif step1_changed or step2_formatted:
        returncode = 1
    else:
        returncode = 0

    errors = pyfltr.parsing.entry.parse_errors(request.command, output, request.params.command_info.error_pattern)

    # commandlineは代表として「最後に実行したステップ」（= ruff format）を格納。
    # 両ステップ分のcommandlineはverbose出力で確認可能。
    result = CommandResult.from_run(
        command=request.command,
        command_info=request.params.command_info,
        commandline=request.params.commandline,
        returncode=returncode,
        formatter_failed=formatter_failed,
        files=len(request.params.targets),
        output=output,
        elapsed=elapsed,
        errors=errors,
        timeout_exceeded=timeout_exceeded,
        retry_count=step1_proc.retry_count + step2_proc.retry_count,
    )
    if not formatter_failed and (step1_changed or step2_formatted):
        # digests_beforeはStep1前のスナップショット（関数冒頭で取得済み）。
        # Step1（ruff --checkによる暗黙fix）とStep2（ruff format）の累積差分を一括で取る。
        digests_after_step2 = snapshot_file_digests(request.params.targets, base_cwd=request.ctx.base.start_cwd)
        result.fixed_files = changed_files(digests_before, digests_after_step2)
    return result
