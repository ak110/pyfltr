# pylint: disable=duplicate-code  # process.run_subprocess呼び出しの引数並び等が他経路と類似
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
import pyfltr.command.targets
import pyfltr.command.textlint_fix
import pyfltr.command.two_step.prettier
import pyfltr.command.two_step.ruff
import pyfltr.command.two_step.shfmt
import pyfltr.command.two_step.taplo
import pyfltr.config.config
import pyfltr.state.cache
import pyfltr.state.only_failed
import pyfltr.warnings_
from pyfltr.command.core_ import CacheContext, CommandResult, ExecutionContext, ExecutionParams

logger = __import__("logging").getLogger(__name__)


def _failed_resolution_result(
    command: str,
    command_info: pyfltr.config.config.CommandInfo,
    message: str,
    *,
    files: int,
) -> "CommandResult":
    """ツール解決失敗時の `CommandResult` を組み立てる。

    `files` には実際の処理対象件数を渡す。`status` は `resolution_failed` を返し、
    通常の実行失敗（`failed`）と区別できるようにする。
    """
    pyfltr.warnings_.emit_warning(source="tool-resolve", message=f"{command}: {message}")
    return CommandResult.from_run(
        command=command,
        command_info=command_info,
        commandline=[],
        returncode=1,
        has_error=True,
        files=files,
        output=message,
        elapsed=0.0,
        resolution_failed=True,
    )


def _prepare_execution_params(
    command: str,
    args: argparse.Namespace,
    config: pyfltr.config.config.Config,
    all_files: list[pathlib.Path],
    *,
    fix_stage: bool,
    only_failed_targets: "pyfltr.state.only_failed.ToolTargets | None",
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

    # ファイルの順番をシャッフルまたはソート（fixステージは再現性重視でシャッフルを無効化）
    if args.shuffle and not fix_stage:
        random.shuffle(targets)
    else:
        # natsort.natsortedの型ヒントが不十分でtyがunion型へ縮めるためcastで明示。
        targets = typing.cast("list[pathlib.Path]", natsort.natsorted(targets, key=str))

    # fixステージでは当該コマンドのfix-argsを引用してfix経路に分岐する。
    # fix-args未定義のformatterは通常経路を通る（通常実行でもファイルを書き換えるため挙動は同じ）。
    fix_mode = fix_stage
    fix_args: list[str] | None = None
    if fix_mode:
        fix_args = config.values.get(f"{command}-fix-args")

    # 対象ファイル0件ならこの後の実行自体が走らないため、ツールパス解決を省略する。
    # mise等のbin-runner解決はネットワークやプラットフォーム制約で失敗し得るため、
    # 解決不要な状況で副作用的な失敗を出さないよう早期返却する。
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
        )

    # `{command}-runner` および `{command}-path` 設定からツール起動コマンドラインを解決する。
    # bin-runner経路（mise / direct / グローバル `bin-runner` 委譲）とjs-runner経路、
    # 直接実行を統一的に扱う。mise経路では事前可用性チェック（mise exec --version）も実行する。
    try:
        # 実コマンド実行経路はmise副作用を許可し、mise設定判定の `mise ls --current --json` でも
        # `mise-auto-trust` に従ったtrust→再実行を可能にする。
        resolved = pyfltr.command.runner.build_commandline(command, config, allow_side_effects=True)
        resolved = pyfltr.command.runner.ensure_mise_available(resolved, config, command=command)
    except ValueError as e:
        return _failed_resolution_result(command, command_info, str(e), files=len(targets))
    except FileNotFoundError as e:
        if command in pyfltr.command.runner.JS_TOOL_BIN and config["js-runner"] == "direct":
            message = (
                f"js-runner=direct 指定ですが実行ファイルが見つかりません: {e}. "
                "package.jsonで対象パッケージをインストールしてください。"
            )
        else:
            message = f"ツールが見つかりません: {e}"
        return _failed_resolution_result(command, command_info, message, files=len(targets))
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
    # pass-filenames = falseのツールはファイル引数を渡さない（tsc等）
    if config.values.get(f"{command}-pass-filenames", True):
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
) -> CacheContext | None:
    """キャッシュ参照用のキー算出。対象外の場合はNoneを返す。"""
    if cache_store is None or not command_info.cacheable or fix_args is not None:
        return None
    if not pyfltr.state.cache.is_cacheable(command, config, additional_args):
        return None
    structured_spec = pyfltr.command.runner.get_structured_output_spec(command, config)
    key = cache_store.compute_key(
        command=command,
        commandline=commandline,
        fix_stage=False,
        structured_output=structured_spec is not None,
        target_files=targets,
        config_files=pyfltr.state.cache.resolve_config_files(command, config),
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
    proc = pyfltr.command.process.run_subprocess(
        commandline,
        env,
        on_output,
        is_interrupted=is_interrupted,
        on_subprocess_start=on_subprocess_start,
        on_subprocess_end=on_subprocess_end,
    )
    returncode = proc.returncode

    output = proc.stdout.strip()
    elapsed = time.perf_counter() - start_time

    # エラー箇所のパース
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
    通常経路と挙動が変わらないため、呼び出し側はfixステージで走らせる対象を
    `split_commands_for_execution()` で絞り込んだうえで指定する前提。

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

    # 各CommandResultに当該ツールのターゲットファイル一覧を埋めるためのヘルパー。
    # retry_commandで差し替え可能なターゲットを復元するのに使う（特にpass-filenames=False
    # のツールではcommandlineからも復元できないため、ここで明示的に保持する）。
    def _with_targets(result: CommandResult) -> CommandResult:
        result.target_files = list(targets)
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
                env,
                on_output,
                start_time,
                args,
                is_interrupted=is_interrupted,
                on_subprocess_start=on_subprocess_start,
                on_subprocess_end=on_subprocess_end,
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
                env,
                on_output,
                start_time,
                args,
                is_interrupted=is_interrupted,
                on_subprocess_start=on_subprocess_start,
                on_subprocess_end=on_subprocess_end,
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
                targets,
                config,
                args,
                env,
                on_output,
                start_time,
                is_interrupted=is_interrupted,
                on_subprocess_start=on_subprocess_start,
                on_subprocess_end=on_subprocess_end,
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
        )
    )
