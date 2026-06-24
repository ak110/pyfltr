"""ディスパッチャー。"""

import argparse
import pathlib
import random
import shlex
import time
import typing

import natsort

import pyfltr.command.env
import pyfltr.command.error_parser
import pyfltr.command.glab
import pyfltr.command.linter_fix
import pyfltr.command.precommit
import pyfltr.command.process
import pyfltr.command.runner
import pyfltr.command.structured_output
import pyfltr.command.subproject_loop
import pyfltr.command.targets
import pyfltr.command.textlint_fix
import pyfltr.command.tool_resolution
import pyfltr.command.two_step.prettier
import pyfltr.command.two_step.ruff
import pyfltr.command.two_step.shfmt
import pyfltr.command.two_step.taplo
import pyfltr.command.vitest
import pyfltr.config.config
import pyfltr.paths
import pyfltr.state.cache
import pyfltr.state.only_failed
import pyfltr.warnings_
from pyfltr.command.core_ import CacheContext, CommandResult, ExecutionContext, ExecutionParams

logger = __import__("logging").getLogger(__name__)


def _to_subproject_relative(
    target: pathlib.Path,
    *,
    subproject_cwd: pathlib.Path,
    start_cwd: pathlib.Path,
) -> str:
    """起点 cwd 相対パスをサブプロジェクト cwd 相対パスへ変換する。

    `target` が絶対パスの場合はそのまま、相対の場合は起点 cwd を起点として絶対化してから
    サブプロジェクト cwd 相対へ変換する。サブプロジェクト cwd の外側にあるパス
    （ファイル所属判定で外側に分類されたケース）は元の表記をそのまま使う。
    POSIX 区切りに揃える。
    """
    abs_path = target if target.is_absolute() else (start_cwd / target)
    try:
        rel = abs_path.resolve().relative_to(subproject_cwd.resolve())
    except (OSError, ValueError):
        return pyfltr.paths.normalize_separators(target)
    return pyfltr.paths.normalize_separators(str(rel))


def _is_external_path(target: pathlib.Path, *, start_cwd: pathlib.Path) -> bool:
    """`target` が起点cwd配下にない絶対パス（外部パス）かを判定する。

    相対パスは起点cwd配下扱いとして`False`を返す。絶対パスは実体パスを起点cwd配下と
    比較し、配下に含まれないか実体解決に失敗した場合に`True`を返す。
    """
    if not target.is_absolute():
        return False
    try:
        target.resolve().relative_to(start_cwd.resolve())
    except (OSError, ValueError):
        return True
    return False


def _resolve_config_inject_path(start_cwd: pathlib.Path, candidates: list[str]) -> pathlib.Path | None:
    """起点cwd直下を`candidates`順に走査し、最初に見つかった設定ファイルの絶対パスを返す。

    候補が見つからない場合は`None`を返す（呼び出し側は注入をスキップする）。
    返り値はシンボリックリンクを解決した実体パス。
    """
    for name in candidates:
        candidate = start_cwd / name
        if candidate.is_file():
            return candidate.resolve()
    return None


def _user_overrides_config(config_flag: str, *arg_lists: list[str]) -> bool:
    """利用者がツール固有の設定フラグを明示指定済みか判定する。

    対象は`{command}-args`・`{command}-extend-args`・CLI`--{command}-args`の合算。
    含まれている場合、`config_arg_template`による自動注入はスキップして利用者指定を優先する。
    重複指定で各ツールがエラー終了するのを避けるため。
    `config_flag`は`CommandInfo.config_arg_template[0]`由来（bandit→`--configfile`、markdownlint／textlint→`--config`等）。
    分離形（`<flag> <value>`）と等号形（`<flag>=<value>`）の両形式を判定する。
    """
    equals_prefix = f"{config_flag}="
    for args in arg_lists:
        for arg in args:
            if arg == config_flag or arg.startswith(equals_prefix):
                return True
    return False


def _prepare_execution_params(
    command: str,
    args: argparse.Namespace,
    config: pyfltr.config.config.Config,
    all_files: list[pathlib.Path],
    *,
    fix_stage: bool,
    only_failed_targets: "pyfltr.state.only_failed.ToolTargets | None",
    subproject_cwd: pathlib.Path | None = None,
    start_cwd: pathlib.Path | None = None,
) -> "ExecutionParams | CommandResult":
    """実行前の共通前処理を行い `ExecutionParams` を返す。

    ツールパス解決に失敗した場合は `CommandResult` を直接返す。
    ターゲット0件の場合は `ExecutionParams` を返し（targetsが空リスト）、
    呼び出し側でスキップ処理を行う。
    """
    command_info = config.commands[command]
    globs = command_info.target_globs()
    source_files = only_failed_targets.resolve_files(all_files) if only_failed_targets is not None else all_files
    targets: list[pathlib.Path] = pyfltr.command.targets.filter_by_globs(source_files, globs)

    # ツール別excludeの適用（--no-excludeが指定された場合はスキップ）
    if not args.no_exclude:
        tool_excludes: list[str] = config.values.get(f"{command}-exclude", [])
        if tool_excludes:
            targets = [t for t in targets if not pyfltr.command.targets.matches_exclude_patterns(t, tool_excludes)]

    # `allows_external_paths=False`のツールは外部パスを除外して警告発行する。
    if not command_info.allows_external_paths:
        start_for_filter = start_cwd if start_cwd is not None else pathlib.Path.cwd()
        kept: list[pathlib.Path] = []
        for t in targets:
            if _is_external_path(t, start_cwd=start_for_filter):
                pyfltr.warnings_.emit_warning(
                    source="external-path",
                    message=f"{command}: 起点cwd外のパスは対象から除外しました: {t}",
                )
                pyfltr.warnings_.add_filtered_direct_file(str(t), reason="external")
            else:
                kept.append(t)
        targets = kept

    # ファイルの順番をシャッフルまたはソート（fixステージは再現性重視でシャッフルを無効化）
    if args.shuffle and not fix_stage:
        random.shuffle(targets)
    else:
        targets = natsort.natsorted(targets, key=str)

    # fixステージでは当該コマンドのfix-argsを引用してfix経路に分岐する。
    # fix-args未定義のformatterは通常経路を通る（通常実行でもファイルを書き換えるため挙動は同じ）。
    fix_mode = fix_stage
    fix_args: list[str] | None = None
    if fix_mode:
        fix_args = config.values.get(f"{command}-fix-args")

    # 対象ファイル0件ならこの後の実行自体が行われないため、ツールパス解決を省略する。
    # mise等のbin-runner解決はネットワークやプラットフォーム制約で失敗し得るため、
    # 解決不要な状況で副作用的な失敗を発生させないよう早期返却する。
    if not targets:
        return ExecutionParams(
            command_info=command_info,
            targets=targets,
            commandline_prefix=[],
            commandline=[],
            additional_args=[],
            fix_mode=fix_mode,
            fix_args=fix_args,
            via_mise=False,
            effective_runner=None,
            runner_source=None,
            runner_fallback=None,
        )

    # `{command}-runner` および `{command}-path` 設定からツール起動コマンドラインを解決する。
    # bin-runner経路（mise / direct / グローバル `bin-runner` 委譲）とjs-runner経路、
    # 直接実行を統一的に扱う。mise経路では事前可用性チェック（mise exec --version）も実行する。
    try:
        # 実コマンド実行経路はmise副作用を許可し、mise設定判定の `mise ls --current --json` でも
        # `mise-auto-trust` に従ったtrust→再実行を可能にする。
        resolved = pyfltr.command.runner.build_commandline(command, config, allow_side_effects=True, cwd=subproject_cwd)
        resolved = pyfltr.command.runner.ensure_mise_available(resolved, config, command=command, cwd=subproject_cwd)
    except ValueError as e:
        message = str(e)
        # `{command}-runner = "uv"` または `"uvx"` をPython系以外のツールに指定した場合、`build_commandline` が
        # `PYTHON_TOOL_BIN` 未登録の旨を含むValueErrorを送出する。利用者向けに `runner` 設定の
        # 切り替え先を案内するヒントを併記する（uv / uvx経路で動かすdev依存追加は本ケースでは無関係）。
        # 列挙する`"direct"` / `"mise"` / `"bin-runner"` / `"js-runner"`は、Python系以外でも安全に動く代替値の集合。
        hint: str | None = None
        if "PYTHON_TOOL_BIN" in message:
            hint = (
                f'`{command}-runner` に `"direct"` / `"mise"` / `"bin-runner"` / `"js-runner"` '
                "などのいずれかを指定してください。"
            )
        return pyfltr.command.tool_resolution.failed_resolution_result(
            command, command_info, message, files=len(targets), hint=hint
        )
    except FileNotFoundError as e:
        # runner.pyは識別子のみを送出する契約のため、利用者向け文面はここで組み立てる。
        # Python系・JS系・ネイティブ系のツール分類に応じて探索経路と代替案内を切り替える。
        message = pyfltr.command.tool_resolution.format_tool_resolution_failure(command, str(e), config)
        return pyfltr.command.tool_resolution.failed_resolution_result(command, command_info, message, files=len(targets))
    commandline_prefix = resolved.commandline

    # 起動オプションからの追加引数 （--textlint-argsなど） をshlex分割しておく
    additional_args_str = getattr(args, f"{command.replace('-', '_')}_args", "")
    additional_args = shlex.split(additional_args_str) if additional_args_str else []

    # 対象ファイル抜きのargvを共通ヘルパーで組み立てる:
    #   [prefix] + [auto-args] + args + (lint-args or fix-args) + additional_args + structured_output適用
    # textlintのfix経路では `pyfltr.command.textlint_fix.execute_textlint_fix` 側が改めてargvを組み立てるため
    # ここでの値は実際には使われない（execute_commandのdispatchでtextlint fixは別経路へ分岐する）。
    commandline = pyfltr.command.runner.build_invocation_argv(
        command,
        config,
        commandline_prefix,
        additional_args,
        fix_stage=fix_args is not None,
    )

    # `config_arg_template`指定ツール（markdownlint・textlint・bandit等）は、起点cwd直下の設定ファイルを
    # `<flag> <絶対パス>`形式で明示注入する。フラグはツールごとに異なる（markdownlint／textlintは`--config`、
    # banditは`--configfile`）。内部パスのみの実行でも一律で適用し、外部パス指定時に暗黙のcwd探索（markdownlint-cli2が
    # 対象ファイルとCWDの共通親から探索する仕様等）が起こらないよう挙動を統一する。
    # 利用者が`{command}-args`で同等フラグを指定済みのときは重複指定を避けるため注入をスキップする。
    # 設定ファイルが起点cwd直下に見つからないときも注入をスキップしてツールの既定動作に委ねる。
    # 挿入位置は`commandline_prefix`直後。
    if command_info.config_arg_template and command_info.config_inject_candidates:
        user_args_list: list[str] = list(config.values.get(f"{command}-args", []))
        extend_args_list: list[str] = list(config.values.get(f"{command}-extend-args", []))
        config_flag = command_info.config_arg_template[0]
        if not _user_overrides_config(config_flag, user_args_list, extend_args_list, additional_args):
            start_for_inject = start_cwd if start_cwd is not None else pathlib.Path.cwd()
            config_path = _resolve_config_inject_path(start_for_inject, command_info.config_inject_candidates)
            if config_path is not None:
                injection = [tok.format(path=str(config_path)) for tok in command_info.config_arg_template]
                prefix_len = len(commandline_prefix)
                commandline = commandline[:prefix_len] + injection + commandline[prefix_len:]

    # pass-filenames = falseのツールはファイル引数を渡さない（tsc等）
    if config.values.get(f"{command}-pass-filenames", True):
        if subproject_cwd is not None and start_cwd is not None:
            commandline.extend(_to_subproject_relative(t, subproject_cwd=subproject_cwd, start_cwd=start_cwd) for t in targets)
        else:
            commandline.extend(str(t) for t in targets)

    # `pyfltr.command.runner.ensure_mise_available` を通過した後の `effective_runner` でmise経路かを判定する。
    # `pyfltr.command.runner.build_commandline` 直後はmise不在時のdirectフォールバック前の値が入っているため、
    # ここでは事後値を採用する（direct経路へtoolパス除外を誤適用しないため）。
    via_mise = resolved.effective_runner == "mise" or resolved.executable == "mise"

    return ExecutionParams(
        command_info=command_info,
        targets=targets,
        commandline_prefix=commandline_prefix,
        commandline=commandline,
        additional_args=additional_args,
        fix_mode=fix_mode,
        fix_args=fix_args,
        via_mise=via_mise,
        effective_runner=resolved.effective_runner,
        runner_source=resolved.runner_source,
        runner_fallback=resolved.runner_fallback,
    )


def _prepare_cache_context(
    command: str,
    command_info: pyfltr.config.config.CommandInfo,
    config: pyfltr.config.config.Config,
    commandline: list[str],
    targets: list[pathlib.Path],
    additional_args: list[str],
    *,
    fix_args: list[str] | None,
    cache_store: "pyfltr.state.cache.CacheStore | None",
    subproject_cwd: pathlib.Path | None = None,
) -> CacheContext | None:
    """キャッシュ参照用のキー算出。対象外の場合はNoneを返す。

    `subproject_cwd` を指定するとサブプロジェクト cwd の実体パスをキー要素に含める。
    同一相対パスがサブプロジェクトをまたいで存在する場合の誤ヒットを防ぐ。
    """
    if cache_store is None or not command_info.cacheable or fix_args is not None:
        return None
    if not pyfltr.state.cache.is_cacheable(command, config, additional_args):
        return None
    structured_spec = pyfltr.command.structured_output.get_structured_output_spec(command, config)
    key = cache_store.compute_key(
        command=command,
        commandline=commandline,
        fix_stage=False,
        structured_output=structured_spec is not None,
        target_files=targets,
        config_files=pyfltr.state.cache.resolve_config_files(command, config),
        subproject_cwd=subproject_cwd,
    )
    return CacheContext(cache_store=cache_store, command=command, key=key)


def _run_plain_command(
    command: str,
    command_info: pyfltr.config.config.CommandInfo,
    commandline: list[str],
    targets: list[pathlib.Path],
    additional_args: list[str],
    env: dict[str, str],
    on_output: typing.Callable[[str], None] | None,
    start_time: float,
    args: argparse.Namespace,
    config: pyfltr.config.config.Config,
    *,
    fix_args: list[str] | None,
    cache_store: "pyfltr.state.cache.CacheStore | None",
    cache_run_id: str | None,
    is_interrupted: typing.Callable[[], bool] | None = None,
    on_subprocess_start: typing.Callable[[], None] | None = None,
    on_subprocess_end: typing.Callable[[], None] | None = None,
    cwd: pathlib.Path | None = None,
) -> CommandResult:
    """通常のlinter/formatterを単発実行するplain経路。

    ファイルhashキャッシュの参照・書き込みを担う。cacheable=Trueの非fix実行のみ
    キャッシュを扱い、textlint fixなど特殊経路はこの関数を通らない。
    """
    has_error = False

    # ファイルhashキャッシュの参照 （cacheable=Trueの非fix実行のみ）。
    # キャッシュ対象判定 / キー算出 / 書き込みをbreak/resumeできるよう、結果を
    # 後段で差し替える設計とする。
    cache_context = _prepare_cache_context(
        command,
        command_info,
        config,
        commandline,
        targets,
        additional_args,
        fix_args=fix_args,
        cache_store=cache_store,
        subproject_cwd=cwd,
    )
    if cache_context is not None:
        cached_result = cache_context.lookup()
        if cached_result is not None:
            cached_result.target_files = list(targets)
            # 復元値のfiles / elapsedは過去実行時のもの。復元時の実ファイル数は
            # 現在のターゲットリストに合わせ直す （再実行時の対象件数表示のため）。
            cached_result.files = len(targets)
            return cached_result

    # verbose時はコマンドラインをon_output経由で出力
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
        cwd=cwd,
        **pyfltr.config.config.resolve_retry_kwargs(config.values),
    )
    returncode = proc.returncode

    output = proc.stdout.strip()
    elapsed = time.perf_counter() - start_time
    errors = pyfltr.command.error_parser.parse_errors(command, output, command_info.error_pattern)

    result = CommandResult.from_run(
        command=command,
        command_info=command_info,
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

    # キャッシュ書き込み （成功rc=0のみ）。失敗結果を記録すると再試行で同じ失敗が
    # 復元されて修正確認できなくなるため、成功時に限定する。
    if cache_context is not None and returncode == 0 and not has_error:
        cache_context.store(result, run_id=cache_run_id)

    return result


def execute_command(
    command: str,
    args: argparse.Namespace,
    ctx: ExecutionContext,
) -> CommandResult:
    """コマンドの実行。

    `fix_stage=True` の場合、当該コマンドがfix-argsを持っていればfix経路
    （`--fix` 付きの単発実行）で動作する。fix-args未定義のformatterでは
    通常経路と挙動が変わらないため、呼び出し側はfixステージで実行する対象を
    `split_commands_for_execution()` でフィルタリングしたうえで指定する前提。

    `cache_store` が指定され、かつ当該コマンドが `CommandInfo.cacheable=True` の
    非fixモード実行なら、ファイルhashキャッシュを参照して一致があれば実行を
    スキップし、過去の結果を復元して `cached=True` で返す。キャッシュミス時は
    通常実行のうえ、成功 （rc=0, has_error=False） に限り `cache_run_id` をソースとして
    書き込む。`cache_run_id` が `None` の場合はキャッシュ書き込みをスキップする
    （アーカイブ無効時に `cached_from` で参照させる元runが無いため）。

    `only_failed_targets` が指定された場合、`ToolTargets.resolve_files(all_files)`
    経由で実対象ファイルを取得する（`--only-failed` 経路でツール別の失敗ファイル集合を
    渡す用途）。その後の `target_extensions` / `pass_filenames=False` の分岐は
    通常通り適用される。`None` の場合は既定の `all_files` を使用する。

    モノレポモード（`base.subprojects` が2件以上）で当該コマンドが `subproject_aware=True`
    の場合、サブプロジェクト別ループで実行して `CommandResult.merge` で集約する。
    `subproject_aware=False` または単一プロジェクト時は従来通り起点 cwd で1回実行する。

    実行結果に対してuv経路でのツール未登録パターンを判定し、検出時には
    利用者向けの登録手順案内を `pyfltr.warnings_.emit_warning` 経由で発行する。
    """
    if pyfltr.command.subproject_loop.should_run_subproject_loop(command, ctx):
        result = pyfltr.command.subproject_loop.run_subproject_loop(
            command,
            args,
            ctx,
            dispatch_fn=_dispatch_command,
            disabled_skip_fn=_make_disabled_skip_result,
        )
    else:
        result = _dispatch_command(command, args, ctx)
    pyfltr.command.tool_resolution.maybe_emit_uv_missing_tool_warning(result)
    return result


def _make_disabled_skip_result(command: str, ctx: ExecutionContext) -> CommandResult:
    """設定で無効化してスキップしたか起点でも無効な場合に、起点cwd誤実行を避けて返す skipped 結果。"""
    info = ctx.config.commands.get(command)
    assert info is not None, "サブプロジェクトループは CommandInfo 非Noneの経路でのみ実行される"
    return CommandResult.from_run(
        command=command,
        command_info=info,
        commandline=[],
        returncode=None,
        output="サブプロジェクト設定で無効化されているためスキップしました。",
        files=0,
        elapsed=0.0,
    )


def _dispatch_command(
    command: str,
    args: argparse.Namespace,
    ctx: ExecutionContext,
) -> CommandResult:
    """コマンドを実行経路へ振り分ける本体実装。

    `execute_command` のwrapperから呼び出され、ターゲット解決・コマンドライン構築・
    各種2段階実行・plain経路など全ての分岐を担う。
    """
    # ctxから各フィールドを展開する。
    config = ctx.config
    all_files = ctx.all_files
    on_output = ctx.on_output
    is_interrupted = ctx.is_interrupted
    on_subprocess_start = ctx.on_subprocess_start
    on_subprocess_end = ctx.on_subprocess_end

    # 共通前処理: ターゲット解決・コマンドライン構築
    params_or_error = _prepare_execution_params(
        command,
        args,
        config,
        all_files,
        fix_stage=ctx.fix_stage,
        only_failed_targets=ctx.only_failed_targets,
        subproject_cwd=ctx.subproject_cwd,
        start_cwd=ctx.base.start_cwd,
    )
    if isinstance(params_or_error, CommandResult):
        # ツールパス解決失敗
        return params_or_error
    params = params_or_error
    command_info = params.command_info
    targets = params.targets
    commandline = params.commandline
    commandline_prefix = params.commandline_prefix
    additional_args = params.additional_args
    fix_mode = params.fix_mode
    fix_args = params.fix_args

    # 各CommandResultに当該ツールのターゲットファイル一覧とrunner解決情報を埋めるためのヘルパー。
    # retry_commandで差し替え可能なターゲットを復元するのに使う（特にpass-filenames=False
    # のツールではcommandlineからも復元できないため、ここで明示的に保持する）。
    # runner情報（effective_runner / runner_source）は `build_commandline` が成功した経路でのみ
    # 値が確定するため、targets空（0件）経路ではNoneのまま残す。
    # severityは `status` プロパティが従来failedとなる結果を `warning` に格下げするか
    # を決めるフラグで、結果生成時にconfigから解決して固定値で持たせる。
    severity = pyfltr.config.config.resolve_severity(config.values, command)

    def _with_targets(result: CommandResult) -> CommandResult:
        result.target_files = list(targets)
        result.effective_runner = params.effective_runner
        result.runner_source = params.runner_source
        result.runner_fallback = params.runner_fallback
        result.severity = severity
        return result

    if len(targets) <= 0:
        return _with_targets(
            CommandResult.from_run(
                command=command,
                command_info=command_info,
                commandline=commandline,
                returncode=None,
                output="対象ファイルが見つかりません。",
                files=0,
                elapsed=0,
            )
        )

    start_time = time.perf_counter()
    env = pyfltr.command.env.build_subprocess_env(config, command, via_mise=params.via_mise)

    # サブプロジェクト分割実行で各helperへ伝搬する cwd 引数。
    # 単一プロジェクト経路では None となり、subprocess.Popen は親プロセスの cwd で起動する。
    subproject_cwd = ctx.subproject_cwd
    start_cwd = ctx.base.start_cwd

    # pre-commitは .pre-commit-config.yamlを参照してSKIP環境変数を構築し、
    # pyfltr関連hookを除外したうえで2段階実行する。
    # stage 1でファイル修正のみ （fixer系） なら "formatted"、
    # checker系hookが残存エラーを報告すれば "failed" となる。
    if command == "pre-commit":
        return _with_targets(
            pyfltr.command.precommit.execute_pre_commit(
                command,
                command_info,
                commandline,
                targets,
                config,
                args,
                env,
                on_output,
                start_time,
                is_interrupted=is_interrupted,
                on_subprocess_start=on_subprocess_start,
                on_subprocess_end=on_subprocess_end,
                cwd=subproject_cwd,
            )
        )

    # glab-ci-lintはGitLab API経由のlintで、GitLab remote未登録の環境では
    # glab自身が非ゼロ終了しメッセージを返す。pyfltr利用者にとっては環境的事情のため、
    # failedではなくskipped相当へ書き換える。判定はglabの英語ロケール出力に
    # 依存するためLC_ALL/LANG=Cを強制する。
    if command == "glab-ci-lint":
        return _with_targets(
            pyfltr.command.glab.execute_glab_ci_lint(
                command,
                command_info,
                commandline,
                targets,
                config,
                env,
                on_output,
                start_time,
                args,
                is_interrupted=is_interrupted,
                on_subprocess_start=on_subprocess_start,
                on_subprocess_end=on_subprocess_end,
                cwd=subproject_cwd,
            )
        )

    # vitestはJSON reporter併用で失敗を構造化diagnosticへ変換する。
    # 利用者の`vitest-args`に`--reporter`または`--outputFile`指定がある場合は
    # 注入をスキップし、stdout経由の従来経路で動作する。
    if command == "vitest":
        return _with_targets(
            pyfltr.command.vitest.execute_vitest(
                command,
                command_info,
                commandline,
                commandline_prefix,
                targets,
                config,
                additional_args,
                env,
                on_output,
                start_time,
                args,
                is_interrupted=is_interrupted,
                on_subprocess_start=on_subprocess_start,
                on_subprocess_end=on_subprocess_end,
                cwd=subproject_cwd,
            )
        )

    # textlintのfixモードは2段階実行 （fix適用 + lintチェック）。
    # fixer-formatterがcompactをサポートしない問題と、残存違反をcompactで取得する
    # 要件を両立させるため、他のlinterとは別経路で実行する。
    if fix_args is not None and command == "textlint":
        return _with_targets(
            pyfltr.command.textlint_fix.execute_textlint_fix(
                command,
                command_info,
                commandline_prefix,
                config,
                targets,
                additional_args,
                env,
                on_output,
                start_time,
                args,
                is_interrupted=is_interrupted,
                on_subprocess_start=on_subprocess_start,
                on_subprocess_end=on_subprocess_end,
                cwd=subproject_cwd,
                start_cwd=start_cwd,
            )
        )

    # fixモードでlinterにfix-argsを適用する経路。
    # mtime変化でformatted判定を行い、rc != 0はそのままfailed扱いとする。
    if fix_args is not None and command_info.type != "formatter":
        return _with_targets(
            pyfltr.command.linter_fix.execute_linter_fix(
                command,
                command_info,
                commandline,
                targets,
                config,
                env,
                on_output,
                start_time,
                args,
                is_interrupted=is_interrupted,
                on_subprocess_start=on_subprocess_start,
                on_subprocess_end=on_subprocess_end,
                cwd=subproject_cwd,
                start_cwd=start_cwd,
            )
        )

    # ruff-formatでruff-format-by-checkが有効な場合は、
    # 先にruff check --fix --unsafe-fixesを実行してからruff formatを実行する。
    # ステップ1（check）のlint violation （exit 1） は無視する （lintはruff-checkで検出）。
    # ただしexit >= 2 （設定エラー等） は失敗扱いする。
    if command == "ruff-format" and config["ruff-format-by-check"]:
        return _with_targets(
            pyfltr.command.two_step.ruff.execute_ruff_format_two_step(
                command,
                command_info,
                commandline,
                commandline_prefix,
                targets,
                config,
                args,
                env,
                on_output,
                start_time,
                is_interrupted=is_interrupted,
                on_subprocess_start=on_subprocess_start,
                on_subprocess_end=on_subprocess_end,
                cwd=subproject_cwd,
                start_cwd=start_cwd,
            )
        )

    # taploはcheckとformatが排他のためshfmt同様の2段階実行。
    if command == "taplo":
        return _with_targets(
            pyfltr.command.two_step.taplo.execute_taplo_two_step(
                command,
                command_info,
                commandline_prefix,
                config,
                targets,
                additional_args,
                fix_mode=fix_mode,
                env=env,
                on_output=on_output,
                start_time=start_time,
                args=args,
                is_interrupted=is_interrupted,
                on_subprocess_start=on_subprocess_start,
                on_subprocess_end=on_subprocess_end,
                cwd=subproject_cwd,
                start_cwd=start_cwd,
            )
        )

    # shfmtは-l （確認） と-w （書き込み） が排他のためprettier同様の2段階実行。
    if command == "shfmt":
        return _with_targets(
            pyfltr.command.two_step.shfmt.execute_shfmt_two_step(
                command,
                command_info,
                commandline_prefix,
                config,
                targets,
                additional_args,
                fix_mode=fix_mode,
                env=env,
                on_output=on_output,
                start_time=start_time,
                args=args,
                is_interrupted=is_interrupted,
                on_subprocess_start=on_subprocess_start,
                on_subprocess_end=on_subprocess_end,
                cwd=subproject_cwd,
                start_cwd=start_cwd,
            )
        )

    # prettierは--check （read-only） と--write （書き込み） が排他のため2段階実行する。
    # ruff-formatと同じ位置・スタイルで分岐する。
    # prettierには {cmd}-fix-argsを定義していないためfix判定はfix_stage由来の
    # fix_mode変数を使う （filter_fix_commandsではformatterとして常にfix対象となる）。
    if command == "prettier":
        return _with_targets(
            pyfltr.command.two_step.prettier.execute_prettier_two_step(
                command,
                command_info,
                commandline_prefix,
                config,
                targets,
                additional_args,
                fix_mode=fix_mode,
                env=env,
                on_output=on_output,
                start_time=start_time,
                args=args,
                is_interrupted=is_interrupted,
                on_subprocess_start=on_subprocess_start,
                on_subprocess_end=on_subprocess_end,
                cwd=subproject_cwd,
                start_cwd=start_cwd,
            )
        )

    # plain経路（通常のlinter・formatter）
    return _with_targets(
        _run_plain_command(
            command,
            command_info,
            commandline,
            targets,
            additional_args,
            env,
            on_output,
            start_time,
            args,
            config,
            fix_args=fix_args,
            cache_store=ctx.cache_store,
            cache_run_id=ctx.cache_run_id,
            is_interrupted=is_interrupted,
            on_subprocess_start=on_subprocess_start,
            on_subprocess_end=on_subprocess_end,
            cwd=subproject_cwd,
        )
    )
