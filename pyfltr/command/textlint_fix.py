"""textlintのfixモード実行。"""

import dataclasses
import pathlib
import shlex
import time

import pyfltr.command.core_
import pyfltr.command.process
import pyfltr.config.config
import pyfltr.config.model
import pyfltr.paths
import pyfltr.tools
from pyfltr.command import step_result
from pyfltr.command.core_ import CommandResult
from pyfltr.command.invocation import build_invocation_argv
from pyfltr.command.snapshot import (
    changed_files,
    snapshot_file_digests,
    snapshot_file_texts,
    warn_protected_identifier_corruption,
)

logger = __import__("logging").getLogger(__name__)


def execute_textlint_fix(request: pyfltr.command.core_.ExecutionRequest) -> pyfltr.command.core_.CommandResult:
    """Textlint fixモードの実行（fix適用と、必要な場合の残存違反の取得）。

    textlintはlint実行とfix実行でフォーマッタ解決に使うパッケージが異なり
    （`@textlint/linter-formatter` と `@textlint/fixer-formatter`）、fixer側は
    `compact` フォーマッタをサポートしない。このため `textlint --format compact --fix`
    がクラッシュする。また `textlint --fix` の既定出力 （stylish） は本ツールの
    パーサーで解析できないため、残存違反の取得には別途lint実行が要る。

    Step1: fix適用
        commandline_prefix + （textlint-argsから--formatペアを除去） + fix-args
        + additional_args + `--no-cache` + targets
        （textlintのキャッシュヒット結果は修正後の本文を持たず修正処理が例外終了するため、
        修正段だけキャッシュを無効化する。通常lint段は利用者のキャッシュ指定を保つ）

    Step2: lintチェック （Step1が非0で終わった場合だけ実行する）
        commandline_prefix + textlint-args + textlint-lint-args + additional_args + targets

    Step1が終了0の場合はStep2を省く。fix段の後には同じ実行の通常の段がtextlintを実行して
    同じ検査を行うため、Step2は通常の段と重複する（pyfltr自身の文書27件で約13秒）。
    Step1が非0の場合は、残存違反と予期しない例外を見分けるためにStep2を実行する。
    fail-fastで通常の段へ進まない場合も、この経路で残存違反を結果へ残す。

    ステータス判定:
    - Step1がrc>=2 （致命的エラー）・timeout → failed
    - Step1 rc != 0かつStep2 rc != 0 （残存違反あり） → failed （Errorsタブに反映される）
    - Step1 rc != 0かつStep2 rc == 0 → failed （returncodeはStep1の値）。
      textlintの終了1は残存違反に加えて予期しない例外も表し、修正段に違反が残れば
      Step2も非0になるため、この組合せは修正処理の失敗を示す
    - Step1 rc == 0かつ内容ハッシュに変化あり → formatted
    - Step1 rc == 0かつ変化なし → succeeded
    修正段とlint段の出力はいずれも`output`へ保持する。

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
    steps = [step_result.classify(step1_proc.returncode, timeout=step1_proc.timeout_exceeded, changed=step1_changed)]
    procs = [step1_proc]
    # Step1が終了0なら、残存違反の検査は直後の通常の段が行うため省く（docstring参照）。
    if step1_proc.returncode != 0 and not step1_proc.timeout_exceeded:
        if request.verbose and request.ctx.on_output is not None:
            request.ctx.on_output(f"commandline: {shlex.join(step2_commandline)}\n")
        step2_proc = pyfltr.command.process.run_process(dataclasses.replace(request, verbose=False), step2_commandline)
        procs.append(step2_proc)
        steps.append(step_result.classify(step2_proc.returncode, timeout=step2_proc.timeout_exceeded))

    output = "".join(proc.stdout for proc in procs).strip()
    elapsed = time.perf_counter() - request.start_time

    # Step2出力から残存違反をパースする（Step1の出力は解析できる形式を持たない）
    errors = request.parse_errors(output, request.params.command_info.error_pattern)

    # ステータス判定
    timeout_exceeded = any(proc.timeout_exceeded for proc in procs)
    combined = step_result.combine(*steps)
    step_failed = combined.failed
    returncode = combined.returncode
    result_command_type = "formatter" if combined.state is step_result.StepState.CHANGED else "linter"

    result = CommandResult.from_run(
        command=request.command,
        command_type=result_command_type,
        commandline=step2_commandline if len(procs) > 1 else step1_commandline,
        returncode=returncode,
        files=len(request.params.targets),
        output=output,
        elapsed=elapsed,
        errors=errors,
        timeout_exceeded=timeout_exceeded,
        retry_count=sum(proc.retry_count for proc in procs),
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
