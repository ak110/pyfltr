# pylint: disable=duplicate-code,protected-access
"""textlintのfixモード実行。"""

import argparse
import pathlib
import shlex
import time
import typing

import pyfltr.command.error_parser
import pyfltr.command.process
import pyfltr.config.config
from pyfltr.command.core import CommandResult
from pyfltr.command.runner import build_invocation_argv
from pyfltr.command.snapshot import (
    _changed_files,
    _snapshot_file_digests,
    _snapshot_file_texts,
    _warn_protected_identifier_corruption,
)

logger = __import__("logging").getLogger(__name__)


def _execute_textlint_fix(
    command: str,
    command_info: pyfltr.config.config.CommandInfo,
    commandline_prefix: list[str],
    config: pyfltr.config.config.Config,
    targets: list[pathlib.Path],
    additional_args: list[str],
    env: dict[str, str],
    on_output: typing.Callable[[str], None] | None,
    start_time: float,
    args: argparse.Namespace,
    *,
    is_interrupted: typing.Callable[[], bool] | None = None,
    on_subprocess_start: typing.Callable[[], None] | None = None,
    on_subprocess_end: typing.Callable[[], None] | None = None,
) -> CommandResult:
    """Textlint fixモードの2段階実行 （fix適用 → lintチェック）。

    textlintはlint実行とfix実行でフォーマッタ解決に使うパッケージが異なり
    （`@textlint/linter-formatter` と `@textlint/fixer-formatter`）、fixer側は
    `compact` フォーマッタをサポートしない。このため `textlint --format compact --fix`
    がクラッシュする。また `textlint --fix` の既定出力 （stylish） は本ツールの
    builtinパーサ （compact前提） で解析できないため、残存違反を取得するには
    別途lint実行を行う必要がある。

    上記を両立させるため本関数では次の2段階を直列実行する。

    Step1: fix適用
        commandline_prefix + （textlint-argsから--formatペアを除去） + fix-args
        + additional_args + targets

    Step2: lintチェック （残存違反をcompact形式で取得）
        commandline_prefix + textlint-args + textlint-lint-args + additional_args + targets

    ステータス判定:
    -いずれかのステップがrc>=2 （致命的エラー） → failed
    - Step2 rc != 0 （残存違反あり） → failed （Errorsタブに反映される）
    - Step2 rc == 0かつStep1で内容ハッシュに変化あり → formatted
    - Step2 rc == 0かつ変化なし → succeeded

    textlint --fixは残存違反がなくても対象ファイルを書き戻すことがあり、
    mtimeベースの比較では偽陽性になる。このため内容ハッシュ
    （`_snapshot_file_digests`） で比較している。
    """
    target_strs = [str(t) for t in targets]

    # Step1: --format Xペアを除去した共通args + fix-argsでfix適用
    # `build_invocation_argv` のtextlint fix特殊経路と同じ規則を適用する。
    step1_commandline: list[str] = [
        *build_invocation_argv(command, config, commandline_prefix, additional_args, fix_stage=True),
        *target_strs,
    ]

    digests_before = _snapshot_file_digests(targets)
    # 保護対象識別子の事前検出 （Step1で破損するケースを捕捉するため）。
    # 空リスト設定時は計測を省略する。
    protected_identifiers: list[str] = list(config.values.get("textlint-protected-identifiers", []))
    contents_before: dict[pathlib.Path, str] = _snapshot_file_texts(targets) if protected_identifiers else {}

    if args.verbose and on_output is not None:
        on_output(f"commandline: {shlex.join(step1_commandline)}\n")
    step1_proc = pyfltr.command.process._run_subprocess(
        step1_commandline,
        env,
        on_output,
        is_interrupted=is_interrupted,
        on_subprocess_start=on_subprocess_start,
        on_subprocess_end=on_subprocess_end,
    )
    step1_rc = step1_proc.returncode
    # rc=0 （違反なし） / rc=1 （違反残存） は通常終了、rc>=2は致命的エラー扱い
    step1_fatal = step1_rc >= 2
    digests_after_step1 = _snapshot_file_digests(targets)
    step1_changed = digests_after_step1 != digests_before

    if protected_identifiers and step1_changed:
        _warn_protected_identifier_corruption(contents_before, _snapshot_file_texts(targets), protected_identifiers)

    # Step2: 通常lint実行 （残存違反を取得）
    # `build_invocation_argv` の通常段経路と同じ規則を適用する
    # （auto_argsはtextlintには未登録のため空。構造化出力引数もlint段なので通常通り適用される）。
    step2_commandline: list[str] = [
        *build_invocation_argv(command, config, commandline_prefix, additional_args, fix_stage=False),
        *target_strs,
    ]

    if args.verbose and on_output is not None:
        on_output(f"commandline: {shlex.join(step2_commandline)}\n")
    step2_proc = pyfltr.command.process._run_subprocess(
        step2_commandline,
        env,
        on_output,
        is_interrupted=is_interrupted,
        on_subprocess_start=on_subprocess_start,
        on_subprocess_end=on_subprocess_end,
    )
    step2_rc = step2_proc.returncode
    step2_fatal = step2_rc >= 2

    output = (step1_proc.stdout + step2_proc.stdout).strip()
    elapsed = time.perf_counter() - start_time

    # Step2出力 （compact形式） から残存違反をパースする
    errors = pyfltr.command.error_parser.parse_errors(command, output, command_info.error_pattern)

    # ステータス判定
    if step1_fatal or step2_fatal:
        has_error = True
        returncode: int = step1_rc if step1_fatal else step2_rc
        result_command_type: str = "linter"
    elif step2_rc != 0:
        has_error = True
        returncode = step2_rc
        result_command_type = "linter"
    elif step1_changed:
        # fix適用済み、残存違反なし → formatted扱いにする
        has_error = False
        returncode = 1
        result_command_type = "formatter"
    else:
        has_error = False
        returncode = 0
        result_command_type = "linter"

    result = CommandResult.from_run(
        command=command,
        command_type=result_command_type,
        commandline=step2_commandline,
        returncode=returncode,
        has_error=has_error,
        files=len(targets),
        output=output,
        elapsed=elapsed,
        errors=errors,
    )
    if not has_error and step1_changed:
        result.fixed_files = _changed_files(digests_before, digests_after_step1)
    return result
