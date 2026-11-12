"""品質チェックを実行するMCPツール。"""

from __future__ import annotations

import pathlib
import typing

import pydantic

import pyfltr.cli.command_selection
import pyfltr.cli.mcp_errors
import pyfltr.cli.mcp_models
import pyfltr.cli.pipeline
import pyfltr.config.config
import pyfltr.output.guidance
import pyfltr.run_options
import pyfltr.warnings_
from pyfltr.cli.mcp_models import (
    CommandSummaryModel,
    RunResult,
    RunWarningModel,
    SlowTestModel,
)


async def tool_run(
    paths: typing.Annotated[
        list[str],
        pydantic.Field(
            description="検査対象のファイルまたはディレクトリのパス一覧。相対パスは`work_dir`（省略時はサーバーのcwd）を基準に解決する。",
        ),
    ],
    mode: typing.Annotated[
        typing.Literal["run", "fast", "ci"],
        pydantic.Field(
            description=(
                "実行モード。`run`はfixステージを有効にし、formatterによる書き換えを成功扱いにする。"
                "`fast`は`fast`設定が有効なツールだけを`run`と同じ扱いで実行する。"
                "`ci`はfixステージを無効にし、formatterによる書き換えを失敗扱いにする。"
            ),
        ),
    ] = "run",
    commands: typing.Annotated[
        list[str] | None,
        pydantic.Field(
            description="実行するコマンド名またはエイリアス（`lint`など）の一覧。カンマ区切りも受け付ける。省略時は設定で有効な全コマンド。",
        ),
    ] = None,
    enable: typing.Annotated[
        list[str] | None,
        pydantic.Field(description="この実行に限り有効にするコマンド名の一覧。カンマ区切りも受け付ける。"),
    ] = None,
    disable: typing.Annotated[
        list[str] | None,
        pydantic.Field(description="この実行に限り無効にするコマンド名の一覧。カンマ区切りも受け付ける。"),
    ] = None,
    exclude_fence_under: typing.Annotated[
        list[str] | None,
        pydantic.Field(description="Markdownでフェンス内側を検査対象から除くH2見出しの一覧。"),
    ] = None,
    no_fix: typing.Annotated[
        bool,
        pydantic.Field(
            description=(
                "Trueで`run`・`fast`のfixステージを抑止する（`ci`は元から無効）。"
                "通常ステージのformatterは対象ファイルを書き換え、既定で有効な`ruff-format-by-check`により"
                "`ruff-format`も整形する。書き換えを避ける場合は`commands`をlinterへ限定する。"
            ),
        ),
    ] = False,
    fail_fast: typing.Annotated[bool, pydantic.Field(description="Trueで1つのツールが失敗した時点で残りを打ち切る。")] = False,
    only_failed: typing.Annotated[
        bool,
        pydantic.Field(
            description=(
                "Trueで同じ作業ディレクトリ（`work_dir`）の直前run（`from_run`指定時はそのrun）の失敗ツールを、"
                "失敗ファイルに限定して再実行する。"
                "診断にファイルを持たない失敗ツールは現在の対象全体で再実行する。"
            ),
        ),
    ] = False,
    from_run: typing.Annotated[
        str | None,
        pydantic.Field(
            description=(
                "`only_failed`の参照runの識別子（前方一致・`latest`可）。"
                "`latest`は未指定と同じく同じ作業ディレクトリの直前runを指す。"
                "別の作業ディレクトリのrunはrun_idで指定する。`only_failed=True`のときだけ指定できる。"
            ),
        ),
    ] = None,
    changed_since: typing.Annotated[
        str | None,
        pydantic.Field(description="指定したgit参照（ブランチ名・commitなど）から変更されたファイルだけを対象にする。"),
    ] = None,
    work_dir: typing.Annotated[
        str | None,
        pydantic.Field(
            description=(
                "実行の起点ディレクトリ。設定探索と相対パス解決の基準を兼ねる。"
                "省略時はMCPサーバープロセスのcwd。指定時は診断のファイルパスを絶対パスで返す場合がある。"
            ),
        ),
    ] = None,
    allow_external_paths: typing.Annotated[
        bool,
        pydantic.Field(
            description=(
                "Trueで実行起点の外側にあるパスを許可する。"
                "その場合は`work_dir`へ検査設定を持つプロジェクトのルートを指定する。"
                "検査設定を持たないディレクトリを起点にすると、適用される除外設定と検査対象の範囲が変わる。"
            ),
        ),
    ] = False,
    no_exclude: pyfltr.cli.mcp_models.NoExcludeArg = False,
    no_gitignore: pyfltr.cli.mcp_models.NoGitignoreArg = False,
    no_cache: typing.Annotated[
        bool, pydantic.Field(description="Trueでファイルハッシュによる結果キャッシュを使わない。")
    ] = False,
    human_readable: typing.Annotated[
        bool, pydantic.Field(description="Trueで対応ツールの機械可読出力を抑止し、人間向けの出力にする。")
    ] = False,
    shuffle: typing.Annotated[bool, pydantic.Field(description="Trueで対象ファイルの順序をシャッフルする。")] = False,
    exit_zero_even_if_formatted: typing.Annotated[
        bool, pydantic.Field(description="Trueでformatterによる書き換えだけなら終了コードを0にする。")
    ] = False,
    jobs: typing.Annotated[int | None, pydantic.Field(description="並列実行するツール数の上限。省略時は設定値を使う。")] = None,
) -> RunResult:
    """指定パスに対してlint/format/testを実行し、結果を返す。

    `run`・`fast`・`ci`の各実行モードをCLIと同じ既定値で扱う。
    実行アーカイブは常に有効化する。初期化に失敗した場合は`run_id=None`となり、
    `warnings`の`source="archive"`が理由と対処を示す。実行結果はアーカイブの成否によらず返す。
    `only_failed`による早期終了（直前runなし・失敗ツールなし・対象ファイル交差が空・
    `from_run`の解決失敗・アーカイブの読み取り失敗）の場合は`run_id=None`とし、
    実際の理由と対処を`skipped_reason`へ設定して返す。
    早期終了せず、一部の検査だけ直前runの失敗ファイルが対象と交差せず除外された場合と、
    `commands`へ指定した検査が設定で無効化されているために実行されなかった場合も、
    対象の検査名と理由を`skipped_reason`へ設定する。いずれも`run_pipeline`が発行する
    `source="only-failed"`・`source="commands"`の警告を入力とし、MCP側では再判定しない。
    実行中の警告は`warnings`へ、失敗時などの次の操作は`guidance`へ設定する。
    返却へ含めた警告はサーバーのstderrへ重ねて出力しない。

    結果は`completion`を最初に読む。`completed`以外なら`incomplete_commands`・
    `missing_targets`・`fully_excluded_files`・`skipped_reason`・`warnings`で未完了の理由を、
    `failed`が空でなければ`show_run_diagnostics`で診断を確認する。
    完了判定は`run_pipeline`が導出した値をそのまま返し、CLI JSONLの`summary`と同じ値になる。

    対応CLI: `pyfltr run` / `pyfltr fast` / `pyfltr ci`
    """
    # `run_pipeline`は警告を初期化しないため、対象の呼び出しが発行した警告だけを
    # `skipped_reason`の入力にできるよう、ここで蓄積を初期化する。
    pyfltr.warnings_.clear()

    # 返却へ含めた警告を配送済みとして扱い、サーバーのstderrへ重ねて出力しないよう、
    # 返却の組み立てまでを配送保留スコープに含める。例外で返却に至らなかった警告は
    # スコープの終了時にstderrへ通知される。
    with pyfltr.warnings_.defer_stderr():
        if mode not in ("run", "fast", "ci"):
            pyfltr.cli.mcp_errors.raise_mcp_error("mode は run / fast / ci のいずれかを指定してください。")
        if from_run is not None and not only_failed:
            pyfltr.cli.mcp_errors.raise_mcp_error("from_run は only_failed=True のときのみ指定できます。")

        work_dir_path = pathlib.Path(work_dir).expanduser().resolve() if work_dir is not None else None
        if work_dir_path is not None and not work_dir_path.is_dir():
            pyfltr.cli.mcp_errors.raise_mcp_error(
                f"work_dir が存在するディレクトリではありません: {work_dir}。存在するディレクトリの絶対パスを指定してください。"
            )

        base = work_dir_path if work_dir_path is not None else pathlib.Path.cwd()
        targets = [path if (path := pathlib.Path(raw)).is_absolute() else base / path for raw in paths]

        args = pyfltr.run_options.RunOptions(
            targets=targets,
            # CLI経路（`--commands`はaction="append"）と同じ`list[str] | None`で保持する。
            commands=list(commands) if commands else None,
            enable=list(enable) if enable else None,
            disable=list(disable) if disable else None,
            exclude_fence_under=list(exclude_fence_under) if exclude_fence_under else None,
            no_fix=no_fix,
            fail_fast=fail_fast,
            only_failed=only_failed,
            from_run=from_run,
            changed_since=changed_since,
            no_archive=False,  # アーカイブ必須化のため明示的にFalse
            no_cache=no_cache,
            verbose=False,
            output_format="jsonl",
            output_file=None,  # MCP応答を返すため構造化出力は無効化する
            ui=None,
            no_ui=True,
            no_clear=True,
            stream=False,
            shuffle=False,
            keep_ui=False,
            ci=mode == "ci",
            human_readable=human_readable,
            no_exclude=no_exclude,
            no_gitignore=no_gitignore,
            allow_external_paths=allow_external_paths,
            jobs=jobs,
            work_dir=work_dir_path,
            exit_zero_even_if_formatted=False,
            version=False,
            subcommand=mode,
            # MCPの戻り値は実行アーカイブから組み立てるためJSONL縮約の影響を受けない。
            # quiet=Trueはstderrへのprecommitガイダンス抑止のみに作用する。
            quiet=True,
        )
        pyfltr.cli.command_selection.apply_subcommand_defaults(args)
        args.shuffle = shuffle
        if exit_zero_even_if_formatted:
            args.exit_zero_even_if_formatted = True
        if no_fix:
            args.include_fix_stage = False

        try:
            config, commands_list = pyfltr.cli.pipeline.prepare_run(
                args, archive_required=True, list_commands=pyfltr.cli.command_selection.MCP_LIST_COMMANDS
            )
        except ValueError as exc:
            pyfltr.cli.mcp_errors.raise_mcp_error(pyfltr.config.config.format_config_error(exc, retry="`run`ツール"))

        outcome = pyfltr.cli.pipeline.run_pipeline(
            args,
            commands_list,
            config,
            start_cwd=base,
            original_sys_args=args.retry_arguments(for_mcp=True),
            force_text_on_stderr=True,
            jsonl_warnings_reach_consumer=False,
            archive_required=True,
        )

        exit_code = outcome.exit_code
        run_id = outcome.run_id
        completion = outcome.completion
        completion_fields: dict[str, typing.Any] = {
            "completion": completion.completion,
            "files_reached": completion.files_reached,
            "completed_commands": list(completion.completed_commands),
            "incomplete_commands": list(completion.incomplete_commands),
            "missing_targets": list(completion.missing_targets),
            "fully_excluded_files": list(completion.fully_excluded_files),
        }

        commands_model = [
            CommandSummaryModel(
                command=result.command,
                status=result.status,
                diagnostics=len(result.errors),
                elapsed=result.elapsed,
                started_at=result.started_at,
                finished_at=result.finished_at,
                slow_tests=[SlowTestModel.model_validate(test.to_dict()) for test in result.slow_tests],
            )
            for result in outcome.results
        ]
        failed_commands = [result.command for result in outcome.results if result.failed]
        retry_commands = {result.command: result.retry_command for result in outcome.results if result.retry_command}

        warning_entries = pyfltr.warnings_.collected_warnings()
        pyfltr.warnings_.mark_delivered(warning_entries)
        guidance = pyfltr.output.guidance.build_summary_guidance(
            failure_present=bool(failed_commands),
            resolution_failed_present=any(result.resolution_failed for result in outcome.results),
            applied_fixes_present=any(result.formatted for result in outcome.results),
            run_id=run_id,
            launcher_prefix=None,
            subcommand=mode,
            for_mcp=True,
        )
        return RunResult(
            run_id=run_id,
            exit_code=exit_code,
            **completion_fields,
            failed=failed_commands,
            commands=commands_model,
            skipped_reason=_join_warning_texts(warning_entries, sources=("only-failed", "commands")),
            retry_commands=retry_commands,
            warnings=_build_run_warnings(warning_entries),
            guidance=guidance,
        )


def _build_run_warnings(entries: list[dict[str, typing.Any]]) -> list[RunWarningModel]:
    """蓄積された警告を`RunResult.warnings`の要素へ変換する。"""
    return [
        RunWarningModel(source=str(entry["source"]), message=str(entry["message"]), hint=str(entry["hint"]))
        for entry in entries
    ]


def _join_warning_texts(entries: list[dict[str, typing.Any]], *, sources: tuple[str, ...]) -> str | None:
    """指定した発生源の警告を対処込みの文字列へ整形して連結する。該当が無ければNoneを返す。"""
    texts = [pyfltr.warnings_.format_warning_text(entry) for entry in entries if entry.get("source") in sources]
    return " / ".join(texts) or None
