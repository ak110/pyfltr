"""textlintのfixモード実行。"""

import dataclasses
import json
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


def _fix_cache_keys(
    request: pyfltr.command.core_.ExecutionRequest, argv: list[str], targets: list[pathlib.Path]
) -> dict[pathlib.Path, str]:
    store = request.ctx.cache_store
    prefix_len = len(request.params.commandline_prefix)
    # 不明な引数は設定の外部参照や検査範囲の変更を伴いうる。
    if store is None or "--fix" not in argv[prefix_len:] or not set(argv[prefix_len:]) <= {"--fix", "--cache", "--no-cache"}:
        return {}
    cwd = request.cwd or request.ctx.base.start_cwd
    configs = [cwd / name for name in request.params.command_info.config_files]
    if request.params.injected_config_path is not None:
        configs.append(request.params.injected_config_path)
    if any(path.is_file() for path in configs if path.suffix in {".js", ".cjs"}):
        return {}
    common_key = store.compute_key(
        command=request.command,
        commandline=argv,
        fix_stage=True,
        structured_output=True,
        target_files=[],
        config_files=configs,
        subproject_cwd=cwd,
    )
    return {
        target: store.compute_key(
            command=request.command,
            commandline=[common_key],
            fix_stage=True,
            structured_output=True,
            target_files=[target],
            config_files=[],
            target_base_cwd=request.ctx.base.start_cwd,
        )
        for target in targets
    }


def _read_fix_report(output: str, cwd: pathlib.Path) -> tuple[set[pathlib.Path], str]:
    """修正結果から違反の無いファイルを選び、本文を含まない診断を返す。"""
    clean: set[pathlib.Path] = set()
    diagnostics: list[str] = []
    for line in output.split("\n"):
        try:
            results = json.loads(line)
        except ValueError:
            if not line.lstrip().startswith("["):
                diagnostics.append(line)
            continue
        if not isinstance(results, list):
            continue
        for item in results:
            if not isinstance(item, dict) or not isinstance(item.get("filePath"), str):
                continue
            path = (cwd / item["filePath"]).resolve()
            if all(item.get(key) == [] for key in ("messages", "applyingMessages", "remainingMessages")):
                clean.add(path)
            for key in ("applyingMessages", "remainingMessages"):
                messages = item.get(key)
                if isinstance(messages, list):
                    for msg in messages:
                        if isinstance(msg, dict):
                            diagnostics.append(
                                f"{item['filePath']}:{msg.get('line')}:{msg.get('column')}: "
                                f"{msg.get('message')} ({msg.get('ruleId')})"
                            )
    return clean, "\n".join(diagnostics)


def lint_confirmation_keys(request: pyfltr.command.core_.ExecutionRequest) -> dict[pathlib.Path, str]:
    """通常段の検査条件を含め、違反ゼロの確認を修正段の記録と区別する。"""
    config = request.ctx.config
    lint_argv = build_invocation_argv(
        request.command, config, request.params.commandline_prefix, request.params.additional_args, fix_stage=False
    )
    if lint_argv[len(request.params.commandline_prefix) :] not in (
        ["--format", "json"],
        ["--cache", "--format", "json"],
        ["--no-cache", "--format", "json"],
    ):
        return {}
    fix_argv = build_invocation_argv(
        request.command, config, request.params.commandline_prefix, request.params.additional_args, fix_stage=True
    )
    return {target: key + "-lint" for target, key in _fix_cache_keys(request, fix_argv, request.params.targets).items()}


def record_lint_confirmations(
    request: pyfltr.command.core_.ExecutionRequest, keys: dict[pathlib.Path, str], output: str
) -> None:
    """実際に検査結果を得たファイルだけを違反ゼロとして記録する。"""
    store = request.ctx.cache_store
    if store is None or not keys:
        return
    try:
        results = json.loads(output)
    except ValueError:
        return
    if not isinstance(results, list):
        return
    cwd = request.cwd or request.ctx.base.start_cwd
    clean = {
        (cwd / item["filePath"]).resolve()
        for item in results
        if isinstance(item, dict) and isinstance(item.get("filePath"), str) and item.get("messages") == []
    }
    for target, key in lint_confirmation_keys(request).items():
        if key == keys.get(target) and (request.ctx.base.start_cwd / target).resolve() in clean:
            _store_clean_marker(request, key)


def _store_clean_marker(request: pyfltr.command.core_.ExecutionRequest, key: str) -> None:
    store = request.ctx.cache_store
    if store is not None:
        marker = CommandResult.from_run(
            command=request.command,
            command_type="linter",
            commandline=[],
            returncode=0,
            files=1,
            output="",
            elapsed=0,
        )
        store.put(request.command, key, marker, run_id=request.ctx.cache_run_id)


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

    pyfltrのキャッシュを使える場合は、前回の修正段と通常段で違反数ゼロを確認したファイルを
    対象から外す。残る対象の判定にはfixerのJSONを使うが、通常の段に渡す診断は変えない。
    全件を省けた場合は起動せずsucceededを返す。

    Step2: lintチェック （fail-fast指定時にStep1が非0で終わった場合だけ実行する）
        commandline_prefix + textlint-args + textlint-lint-args + additional_args + targets

    fail-fastでない実行では、fix段の後に同じ実行の通常の段が必ずtextlintを実行して同じ検査を行う。
    Step2はこれと重複する（pyfltr自身の文書27件で約13秒）ため実行しない。
    Step1が非0の場合はfix段の失敗となり、`pyfltr.command.stage_runner.merge_fix_failure`が
    通常の段の結果へ統合する。通常の段に違反が無ければ、統合結果は修正処理の失敗を示す。
    fail-fast指定時にStep1が非0の場合は通常の段へ進まないため、残存違反を結果へ残すために
    Step2を実行する。

    ステータス判定:
    - Step1がrc>=2 （致命的エラー）・timeout → failed
    - Step1 rc != 0 → failed （returncodeはStep1の値。Step2を実行した場合はStep2の結果も集約する）。
      textlintの終了1は残存違反に加えて予期しない例外も表すため、残存違反の有無は
      通常の段かStep2の結果で判定する
    - Step1 rc == 0かつ内容ハッシュに変化あり → formatted
    - Step1 rc == 0かつ変化なし → succeeded
    実行した段の出力はいずれも`output`へ保持する。

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
    step1_argv = build_invocation_argv(
        request.command,
        request.ctx.config,
        request.params.commandline_prefix,
        request.params.additional_args,
        fix_stage=True,
    )
    targets = request.params.targets
    keys = _fix_cache_keys(request, step1_argv, targets)
    store = request.ctx.cache_store
    lint_keys = lint_confirmation_keys(request) if keys else {}
    pending = [
        (target, token)
        for target, token in zip(targets, target_strs, strict=True)
        if store is None
        or target not in lint_keys
        or store.get(request.command, keys[target]) is None
        or store.get(request.command, lint_keys[target]) is None
    ]
    if not pending:
        return CommandResult.from_run(
            command=request.command,
            command_type="linter",
            commandline=[],
            returncode=0,
            files=0,
            output="",
            elapsed=time.perf_counter() - request.start_time,
            errors=[],
        )
    fix_targets = [target for target, _ in pending]
    report = bool(lint_keys)
    step1_commandline = [*step1_argv, *(["--format", "json"] if report else []), *(token for _, token in pending)]
    digests_before = snapshot_file_digests(fix_targets, base_cwd=request.ctx.base.start_cwd)
    # 保護対象識別子の事前検出 （Step1で破損するケースを捕捉するため）。
    # 空リスト設定時は計測を省略する。
    protected_identifiers: list[str] = list(request.ctx.config.values.get("textlint-protected-identifiers", []))
    contents_before: dict[pathlib.Path, str] = (
        snapshot_file_texts(fix_targets, base_cwd=request.ctx.base.start_cwd) if protected_identifiers else {}
    )

    if request.verbose and request.ctx.on_output is not None:
        request.ctx.on_output(f"commandline: {shlex.join(step1_commandline)}\n")

    # fixer のJSONには修正後のファイル本文が含まれる。本文を表示せず、実行アーカイブにも保存しない。
    step1_request = dataclasses.replace(request, verbose=False)
    if report:
        step1_request = dataclasses.replace(
            step1_request,
            ctx=dataclasses.replace(request.ctx, on_output=None),
        )
    step1_proc = pyfltr.command.process.run_process(step1_request, step1_commandline)
    clean, step1_output = (
        _read_fix_report(step1_proc.stdout, request.cwd or request.ctx.base.start_cwd) if report else (set(), step1_proc.stdout)
    )
    if report and step1_output and request.ctx.on_output is not None:
        request.ctx.on_output(step1_output + "\n")
    digests_after_step1 = snapshot_file_digests(fix_targets, base_cwd=request.ctx.base.start_cwd)
    step1_changed = digests_after_step1 != digests_before

    if protected_identifiers and step1_changed:
        warn_protected_identifier_corruption(
            contents_before,
            snapshot_file_texts(fix_targets, base_cwd=request.ctx.base.start_cwd),
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
    # 残存違反の検査は通常は直後の通常の段が行うため省き、fail-fastで通常の段へ進まない場合だけ実行する（docstring参照）。
    if request.ctx.base.fail_fast and step1_proc.returncode != 0 and not step1_proc.timeout_exceeded:
        if request.verbose and request.ctx.on_output is not None:
            request.ctx.on_output(f"commandline: {shlex.join(step2_commandline)}\n")
        step2_proc = pyfltr.command.process.run_process(dataclasses.replace(request, verbose=False), step2_commandline)
        procs.append(step2_proc)
        steps.append(step_result.classify(step2_proc.returncode, timeout=step2_proc.timeout_exceeded))

    output = "\n".join([step1_output, *(proc.stdout for proc in procs[1:])]).strip()
    elapsed = time.perf_counter() - request.start_time

    # 公開する残存違反は、従来どおりlint側（Step2または通常段）の出力から取得する。
    # Step2を省いた場合の残存違反は通常の段の結果が持つ。
    errors = request.parse_errors(procs[1].stdout.strip(), request.params.command_info.error_pattern) if len(procs) > 1 else []

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
        files=len(targets if len(procs) > 1 else fix_targets),
        output=output,
        elapsed=elapsed,
        errors=errors,
        timeout_exceeded=timeout_exceeded,
        retry_count=sum(proc.retry_count for proc in procs),
    )
    result.target_files = list(targets if len(procs) > 1 else fix_targets)
    if not step_failed and step1_changed:
        result.fixed_files = changed_files(digests_before, digests_after_step1)
    if store is not None and clean and not step1_proc.timeout_exceeded and step1_proc.returncode in (0, 1):
        current_keys = _fix_cache_keys(request, step1_argv, fix_targets)
        for target in fix_targets:
            if (
                (request.ctx.base.start_cwd / target).resolve() in clean
                and digests_before[target]
                and digests_before[target] == digests_after_step1[target]
                and current_keys.get(target) == keys[target]
            ):
                _store_clean_marker(request, keys[target])
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
