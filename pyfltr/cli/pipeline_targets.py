"""実行対象の準備とpytest専用の対象規則。"""

from __future__ import annotations

import dataclasses
import logging
import pathlib
import typing

import pyfltr.cli.command_selection
import pyfltr.cli.subproject_config
import pyfltr.command.core_
import pyfltr.command.only_failed
import pyfltr.command.subprojects
import pyfltr.command.targets
import pyfltr.config.config
import pyfltr.config.model
import pyfltr.config.selection
import pyfltr.config.validation
import pyfltr.run_options
import pyfltr.state.only_failed
import pyfltr.tools
import pyfltr.warnings_

logger = logging.getLogger(__name__)


@dataclasses.dataclass(frozen=True)
class TargetPreparationExit:
    """対象の準備を打ち切る理由。表示と完了判定はパイプラインが行う。"""

    reason: typing.Literal["missing-targets", "invalid-config", "only-failed"]


@dataclasses.dataclass
class PreparedTargets:
    """設定と対象展開を確定した実行準備の結果。"""

    commands: list[str]
    all_files: list[pathlib.Path]
    subprojects: list[pyfltr.command.subprojects.Subproject]
    subproject_files: dict[pathlib.Path, list[pathlib.Path]]
    subproject_configs: dict[pathlib.Path, pyfltr.config.model.Config]
    external_files: list[pathlib.Path]
    only_failed_targets: dict[str, pyfltr.command.only_failed.ToolTargets] | None
    fast_selected: bool
    unmet: list[str]


def prepare_execution_targets(
    args: pyfltr.run_options.RunOptions,
    commands: list[str],
    config: pyfltr.config.model.Config,
    start_cwd_path: pathlib.Path,
    *,
    archive_required: bool = False,
) -> PreparedTargets | TargetPreparationExit:
    """対象展開、差分・失敗対象の限定とコマンド選択を行う。"""
    subprojects = pyfltr.command.subprojects.discover_subprojects(start_cwd_path, config)
    if len(subprojects) < 2:
        # モノレポモード非適用: subprojects を空集合として扱う（dispatcher 側で単一経路）。
        subprojects = []

    # 対象ファイルを一括展開（ディレクトリ走査・exclude・gitignoreフィルタリングを1回だけ実行）
    # TUI起動前に実行することで、除外警告がログに表示される。
    # `start_cwd` を明示してプロセスグローバルな cwd 干渉を避ける。
    # サブプロジェクトの最終的な所属判定は `classify_files_by_subproject` の最深一致で行うため、
    # `exclude_subdirs` は渡さず全ファイルを一度収集する。
    all_files = pyfltr.command.targets.expand_all_files(
        args.targets,
        config,
        start_cwd=start_cwd_path,
    )

    # ユーザー指定パスが全て非存在の場合は、各ツールが個別に「ファイルが見つからない」エラーを
    # 多重に出力する前段で打ち切り、非ゼロ終了する。warning自体は`expand_all_files`内で発行済み。
    # 部分一致（一部のみ不在）は処理継続、対象未指定（カレント走査）も対象外として扱う。
    if args.targets and len(pyfltr.warnings_.filtered_direct_files(reason="missing")) == len(args.targets):
        return TargetPreparationExit("missing-targets")

    # --changed-since指定時はgit差分ファイルとの交差でフィルタリングする。
    # --only-failedよりも先に適用し、以後のフィルタはフィルタリング済みリストを受け取る。
    changed_since_ref: str | None = getattr(args, "changed_since", None)
    if changed_since_ref is not None:
        all_files = pyfltr.command.targets.filter_by_changed_since(all_files, changed_since_ref, cwd=start_cwd_path)

    # モノレポ用のサブプロジェクト別 config を準備する。
    # `subproject_aware=True` ツールが各サブプロジェクト cwd で実行する際に参照する。
    # config解決（`pyproject.toml`不在時の最近接祖先継承を含む）は
    # `pyfltr.cli.subproject_config.resolve_subproject_configs` に集約する。
    # `--only-failed`の候補（`pytest-always-targets`）と実行対象コマンドの確定（和集合判定）より前に構築する。
    subproject_configs: dict[pathlib.Path, pyfltr.config.model.Config] = {}
    if subprojects:
        try:
            subproject_configs = pyfltr.cli.subproject_config.resolve_subproject_configs(subprojects, config, args)
        except ValueError as error:
            if archive_required:
                # MCPの`run`はサーバーのstderrをクライアントへ届けないため、応答の`warnings`へ原因と操作を収める。
                pyfltr.warnings_.emit_warning(
                    source="config",
                    message=f"サブプロジェクトの設定を読み込めないため、検査を実行しませんでした: {error}",
                    hint=pyfltr.config.config.config_error_action(error, retry="`run`ツール"),
                )
            else:
                logger.error("設定エラー: %s", pyfltr.config.config.format_config_error(error, retry="元の実行操作"))
            return TargetPreparationExit("invalid-config")

    # --only-failed指定時は直前runからツール別の失敗ファイル集合を構築する。
    # archive / cache初期化より前に実行し、早期終了の場合はそれらの副作用を発生させない。
    commands, only_failed_targets, only_failed_exit_early = pyfltr.state.only_failed.apply_filter(
        args,
        commands,
        all_files,
        start_cwd=start_cwd_path,
        from_run=getattr(args, "from_run", None),
        extra_candidates=only_failed_extra_candidates(args, commands, config, subproject_configs, start_cwd_path),
        only_failed_option="only_failed=True" if archive_required else "--only-failed",
        from_run_option="from_run" if archive_required else "--from-run",
        list_runs="`list_runs`ツール" if archive_required else "`pyfltr list-runs`",
        run="`run`ツール" if archive_required else "`pyfltr run`",
    )
    if only_failed_exit_early:
        # スキップの理由は`apply_filter`が警告として発行済み。JSONLでもheader・warning・summaryを
        # 出力し、何も出力せずに終了コード0で終わる状態（理由が消費主体へ届かない）を避ける。
        return TargetPreparationExit("only-failed")

    # モノレポ用のサブプロジェクト分類を準備する。
    subproject_files: dict[pathlib.Path, list[pathlib.Path]] = {}
    external_files: list[pathlib.Path] = []
    if subprojects:
        subproject_files, external_files = pyfltr.command.subprojects.classify_files_by_subproject(
            all_files, subprojects, start_cwd_path
        )

    # 実行対象として有効化されていないコマンドはパイプラインから除外する。
    # 単一プロジェクトでは起点 config のON/OFF（`config.values.get(cmd) is True`）で判定する。
    # モノレポでは `subproject_aware=True` ツールに限り「起点またはいずれかのサブプロジェクトで有効」
    # の和集合で対象に含める（親OFF・子ONを子でのみ実行できるようにするため）。
    # `subproject_aware=False`（リポジトリ単位ツール）と `subproject_aware` 判定自体は起点 config で固定する。
    fast_selected = is_fast_selection(args, config)
    if fast_selected and not getattr(args, "only_failed", False):
        commands = add_subproject_fast_pytest(commands, args, config, subproject_configs)
    requested_commands = list(commands)
    commands = [c for c in commands if pyfltr.config.selection.is_command_enabled_anywhere(c, config, subproject_configs)]
    # 設定ファイル欠落の警告は確定した実行対象だけを判定する（未選択コマンドの警告を返さない）。
    pyfltr.config.validation.warn_config_files(config, start_cwd_path, commands)
    unmet: list[str] = []
    if getattr(args, "commands", None) is not None:
        unmet = pyfltr.cli.command_selection.compute_unmet_commands(
            pyfltr.cli.command_selection.flatten_commands_arg(args.commands, config),
            requested_commands,
            commands,
            config,
        )
        if unmet:
            pyfltr.warnings_.emit_warning(
                source="commands",
                message="--commandsで指定されたが有効化されていないため未実行のコマンドがあります: " + ", ".join(unmet),
                hint="--enable="
                + ",".join(unmet)
                + " または pyproject.toml [tool.pyfltr] で指定したコマンドを true に設定して有効化してください。",
            )

    return PreparedTargets(
        commands=commands,
        all_files=all_files,
        subprojects=subprojects,
        subproject_files=subproject_files,
        subproject_configs=subproject_configs,
        external_files=external_files,
        only_failed_targets=only_failed_targets,
        fast_selected=fast_selected,
        unmet=unmet,
    )


def is_fast_selection(args: pyfltr.run_options.RunOptions, config: pyfltr.config.model.Config) -> bool:
    """fastサブコマンド、または`--commands`にfastエイリアスを含む実行かを返す。"""
    if getattr(args, "subcommand", None) == "fast":
        return True
    raw_commands = getattr(args, "commands", None)
    if raw_commands is None:
        return False
    return "fast" in pyfltr.cli.command_selection.flatten_commands_arg(raw_commands, config)


def add_subproject_fast_pytest(
    commands: list[str],
    args: pyfltr.run_options.RunOptions,
    config: pyfltr.config.model.Config,
    subproject_configs: dict[pathlib.Path, pyfltr.config.model.Config],
) -> list[str]:
    """起点設定がpytestをfastへ含めない場合に、サブプロジェクトの`pytest-fast-targets`からpytestを加える。

    fastエイリアスは起点設定から展開されるため、起点に指定が無くサブプロジェクトだけが
    `pytest-fast-targets`を持つモノレポでは、展開結果にpytestが入らない。
    fastエイリアスを指定した実行に限り、pytestを有効にして非空の指定を持つサブプロジェクトがあれば加える。
    """
    if "pytest" in commands or "fast" not in pyfltr.cli.command_selection.flatten_commands_arg(args.commands, config):
        return commands
    if not any(
        sub_config.values.get("pytest") is True and pyfltr.config.selection.pytest_fast_target_globs(sub_config.values)
        for sub_config in subproject_configs.values()
    ):
        return commands
    return sorted([*commands, "pytest"], key=lambda name: pyfltr.tools.command_index(config.command_names, name))


def only_failed_extra_candidates(
    args: pyfltr.run_options.RunOptions,
    commands: list[str],
    config: pyfltr.config.model.Config,
    subproject_configs: dict[pathlib.Path, pyfltr.config.model.Config],
    start_cwd_path: pathlib.Path,
) -> dict[str, list[pathlib.Path]] | None:
    """`--only-failed`の交差の候補へ、`pytest-always-targets`に一致するプロジェクト全域のファイルを加える。

    位置引数・差分指定に依らずpytestの対象へ加わるテストは、前回失敗していれば再実行の対象に残す。
    """
    if not getattr(args, "only_failed", False) or "pytest" not in commands:
        return None
    globs = [
        glob
        for values in [config.values, *(sub.values for sub in subproject_configs.values())]
        for glob in pyfltr.config.selection.pytest_always_target_globs(values)
    ]
    if not globs:
        return None
    full_files = pyfltr.command.targets.expand_all_files([], config, start_cwd=start_cwd_path)
    return {"pytest": pyfltr.command.targets.filter_by_globs(full_files, globs)}


def build_pytest_base(
    base_ctx: pyfltr.command.core_.ExecutionBaseContext,
    *,
    fast_selected: bool,
) -> pyfltr.command.core_.ExecutionBaseContext | None:
    """pytestだけへ使う実行基盤を構築する。

    fast選択時に`pytest-fast-targets`を持つ設定（起点または各サブプロジェクト）では、位置引数・差分指定に
    依らずプロジェクト全域を走査したファイル集合を母集合とする。
    `pytest-always-targets`を持つ設定では、母集合へプロジェクト全域から同設定のglobに一致したファイルを加える。
    dispatcherはこの追加分を通常の対象globに依らずpytestの対象へ含めるため、通常の対象との和集合になる。
    指定の無い設定は従来の集合を保つ。いずれの設定も指定を持たない場合は`None`を返し、
    pytestも通常の実行基盤で動かす。
    """
    config = base_ctx.config

    def _fast_globs(values: dict[str, typing.Any]) -> list[str]:
        return pyfltr.config.selection.pytest_fast_target_globs(values) if fast_selected else []

    def _always_globs(values: dict[str, typing.Any]) -> list[str]:
        return pyfltr.config.selection.pytest_always_target_globs(values)

    sub_values = {sub.cwd: base_ctx.subproject_configs.get(sub.cwd, config).values for sub in base_ctx.subprojects}
    root_uses_full = bool(_fast_globs(config.values) or _always_globs(config.values))
    subs_using_full = [cwd for cwd, values in sub_values.items() if _fast_globs(values) or _always_globs(values)]
    if not root_uses_full and not subs_using_full:
        return None
    full_files = pyfltr.command.targets.expand_all_files([], config, start_cwd=base_ctx.start_cwd)

    def _merge(current: list[pathlib.Path], full: list[pathlib.Path], values: dict[str, typing.Any]) -> list[pathlib.Path]:
        base_files = full if _fast_globs(values) else current
        always_files = pyfltr.command.targets.filter_by_globs(full, _always_globs(values))
        # 位置引数の絶対パスと全域走査の相対パスが同じ実体を指す場合も1件にする。
        seen = {(base_ctx.start_cwd / f).resolve() for f in base_files}
        return [*base_files, *(f for f in always_files if (base_ctx.start_cwd / f).resolve() not in seen)]

    subproject_files = dict(base_ctx.subproject_files)
    # 起点cwdがサブプロジェクトでない場合、所属の無い起点cwd内のファイル（起点cwdの区分）は起点の設定で実行する。
    start_real = base_ctx.start_cwd.resolve()
    root_bucket_uses_full = root_uses_full and bool(base_ctx.subprojects) and start_real not in sub_values
    if subs_using_full or root_bucket_uses_full:
        full_subproject_files, _ = pyfltr.command.subprojects.classify_files_by_subproject(
            full_files, base_ctx.subprojects, base_ctx.start_cwd
        )
        for cwd in subs_using_full:
            subproject_files[cwd] = _merge(
                base_ctx.subproject_files.get(cwd, []), full_subproject_files.get(cwd, []), sub_values[cwd]
            )
        if root_bucket_uses_full:
            subproject_files[start_real] = _merge(
                base_ctx.subproject_files.get(start_real, []), full_subproject_files.get(start_real, []), config.values
            )
    return dataclasses.replace(
        base_ctx,
        all_files=_merge(base_ctx.all_files, full_files, config.values) if root_uses_full else base_ctx.all_files,
        subproject_files=subproject_files,
        pytest_base=None,
        pytest_fast_targets_active=fast_selected,
        pytest_always_targets_active=True,
    )
