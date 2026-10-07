"""textlintのfixモード実行。"""

import dataclasses
import pathlib
import shlex
import time

import pyfltr.command.core_
import pyfltr.command.process
import pyfltr.config.config
import pyfltr.config.model
import pyfltr.parsing.entry
import pyfltr.paths
import pyfltr.tools
from pyfltr.command.core_ import CommandResult
from pyfltr.command.runner import build_invocation_argv
from pyfltr.command.snapshot import (
    changed_files,
    snapshot_file_digests,
    snapshot_file_texts,
    warn_protected_identifier_corruption,
)

logger = __import__("logging").getLogger(__name__)


def execute_textlint_fix(request: pyfltr.command.core_.ExecutionRequest) -> pyfltr.command.core_.CommandResult:
    """Textlint fixモードの2段階実行 （fix適用 → lintチェック）。

    textlintはlint実行とfix実行でフォーマッタ解決に使うパッケージが異なり
    （`@textlint/linter-formatter` と `@textlint/fixer-formatter`）、fixer側は
    `compact` フォーマッタをサポートしない。このため `textlint --format compact --fix`
    がクラッシュする。また `textlint --fix` の既定出力 （stylish） は本ツールの
    パーサーで解析できないため、残存違反を取得するには別途lint実行を行う必要がある。
    lint段の出力形式は`textlint-lint-args`の既定値が`compact`を指定するが、
    既定で有効な`textlint-json`が`--format json`を注入して対象の指定を除去するため、
    既定構成ではJSONとなる。いずれの形式もパーサーが解析する。

    上記を両立させるため本関数では次の2段階を直列実行する。

    Step1: fix適用
        commandline_prefix + （textlint-argsから--formatペアを除去） + fix-args
        + additional_args + targets

    Step2: lintチェック （残存違反を取得）
        commandline_prefix + textlint-args + textlint-lint-args + additional_args + targets

    ステータス判定:
    -いずれかのステップがrc>=2 （致命的エラー） → failed
    - Step2 rc != 0 （残存違反あり） → failed （Errorsタブに反映される）
    - Step2 rc == 0かつStep1で内容ハッシュに変化あり → formatted
    - Step2 rc == 0かつ変化なし → succeeded

    textlint --fixは残存違反がなくても対象ファイルを書き戻すことがあり、
    mtimeベースの比較では偽陽性になる。このため内容ハッシュ
    （`snapshot_file_digests`） で比較している。
    """
    # ツール起動コマンドラインに渡すパスはサブプロジェクト cwd 相対へ変換する。
    # pyfltr 内部の `snapshot_file_digests` 等は起点 cwd 相対のまま読み込むため、
    # 引数は外部用と内部用で別経路で扱う。
    if request.cwd is not None and request.ctx.base.start_cwd is not None:
        target_strs = [
            _relative_to_cwd(t, cwd=request.cwd, start_cwd=request.ctx.base.start_cwd) for t in request.params.targets
        ]
    else:
        target_strs = [str(t) for t in request.params.targets]

    # Step1: --format Xペアを除去した共通args + fix-argsでfix適用
    # `build_invocation_argv` のtextlint fix特殊経路と同じ規則を適用する。
    step1_commandline: list[str] = [
        *build_invocation_argv(
            request.command,
            request.ctx.config,
            request.params.commandline_prefix,
            request.params.additional_args,
            fix_stage=True,
        ),
        *target_strs,
    ]

    digests_before = snapshot_file_digests(request.params.targets, base_cwd=request.ctx.base.start_cwd)
    # 保護対象識別子の事前検出 （Step1で破損するケースを捕捉するため）。
    # 空リスト設定時は計測を省略する。
    protected_identifiers: list[str] = list(request.ctx.config.values.get("textlint-protected-identifiers", []))
    contents_before: dict[pathlib.Path, str] = (
        snapshot_file_texts(request.params.targets, base_cwd=request.ctx.base.start_cwd) if protected_identifiers else {}
    )

    if request.verbose and request.ctx.on_output is not None:
        request.ctx.on_output(f"commandline: {shlex.join(step1_commandline)}\n")

    step1_proc = pyfltr.command.process.run_process(dataclasses.replace(request, verbose=False), step1_commandline)
    step1_rc = step1_proc.returncode
    # rc=0 （違反なし） / rc=1 （違反残存） は通常終了、rc>=2は致命的エラー扱い
    step1_fatal = step1_rc >= 2
    digests_after_step1 = snapshot_file_digests(request.params.targets, base_cwd=request.ctx.base.start_cwd)
    step1_changed = digests_after_step1 != digests_before

    if protected_identifiers and step1_changed:
        warn_protected_identifier_corruption(
            contents_before,
            snapshot_file_texts(request.params.targets, base_cwd=request.ctx.base.start_cwd),
            protected_identifiers,
        )

    # Step2: 通常lint実行 （残存違反を取得）
    # `build_invocation_argv` の通常段経路と同じ規則を適用する
    # （auto_argsはtextlintには未登録のため空。構造化出力引数もlint段なので通常通り適用される）。
    step2_commandline: list[str] = [
        *build_invocation_argv(
            request.command,
            request.ctx.config,
            request.params.commandline_prefix,
            request.params.additional_args,
            fix_stage=False,
        ),
        *target_strs,
    ]

    if request.verbose and request.ctx.on_output is not None:
        request.ctx.on_output(f"commandline: {shlex.join(step2_commandline)}\n")
    step2_proc = pyfltr.command.process.run_process(dataclasses.replace(request, verbose=False), step2_commandline)
    step2_rc = step2_proc.returncode
    step2_fatal = step2_rc >= 2

    output = (step1_proc.stdout + step2_proc.stdout).strip()
    elapsed = time.perf_counter() - request.start_time

    # Step2出力から残存違反をパースする
    errors = pyfltr.parsing.entry.parse_errors(request.command, output, request.params.command_info.error_pattern)

    # ステータス判定
    timeout_exceeded = step1_proc.timeout_exceeded or step2_proc.timeout_exceeded
    if step1_fatal or step2_fatal:
        step_failed = True
        returncode: int = step1_rc if step1_fatal else step2_rc
        result_command_type: str = "linter"
    elif step2_rc != 0:
        step_failed = True
        returncode = step2_rc
        result_command_type = "linter"
    elif step1_changed:
        # fix適用済み、残存違反なし → formatted扱いにする
        step_failed = False
        returncode = 1
        result_command_type = "formatter"
    else:
        step_failed = False
        returncode = 0
        result_command_type = "linter"

    result = CommandResult.from_run(
        command=request.command,
        command_type=result_command_type,
        commandline=step2_commandline,
        returncode=returncode,
        files=len(request.params.targets),
        output=output,
        elapsed=elapsed,
        errors=errors,
        timeout_exceeded=timeout_exceeded,
        retry_count=step1_proc.retry_count + step2_proc.retry_count,
    )
    if not step_failed and step1_changed:
        result.fixed_files = changed_files(digests_before, digests_after_step1)
    return result


def _relative_to_cwd(target: pathlib.Path, *, cwd: pathlib.Path, start_cwd: pathlib.Path) -> str:
    """起点 cwd 相対パスをサブプロジェクト cwd 相対パスへ変換する。

    `target` が絶対パスならそのまま、相対なら起点 cwd を起点として絶対化してから
    サブプロジェクト cwd 相対へ変換する。POSIX 区切りに揃える。
    """
    abs_path = target if target.is_absolute() else (start_cwd / target)
    try:
        rel = abs_path.resolve().relative_to(cwd.resolve())
    except (OSError, ValueError):
        return pyfltr.paths.normalize_separators(target)
    return pyfltr.paths.normalize_separators(rel)
