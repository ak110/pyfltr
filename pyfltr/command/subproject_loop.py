"""サブプロジェクト走査ループ。

モノレポモード時のサブプロジェクト単位ループ走査を担う。
各サブプロジェクトの設定再判定と外部パス追加実行を含む。
dispatcher側のディスパッチ関数と無効スキップ結果生成関数をコールバックで受け取り、循環importを避ける。
"""

import argparse
import concurrent.futures
import dataclasses
import typing

import pyfltr.command.targets
import pyfltr.command.tool_parallelism
import pyfltr.config.config
import pyfltr.paths
import pyfltr.warnings_
from pyfltr.command.core_ import CommandResult, ExecutionContext


def should_run_subproject_loop(command: str, ctx: ExecutionContext) -> bool:
    """サブプロジェクトループ経路を採用するか判定する。

    判定条件:
    - モノレポモード有効（`base.subprojects` が2件以上）
    - 当該コマンドの `subproject_aware` が True（既定は `CommandInfo.subproject_aware`、
      利用者が `{command}-subproject-aware` で上書き可能）
    - `ctx.subproject_cwd` が未設定（既にサブループ内側ならネストさせない）

    上記をすべて満たすときに True を返す。`subproject_aware` はツール特性を表すメタ設定のため
    起点 config で固定する。コマンドのON/OFF自体は本判定で見ず、サブプロジェクト単位の再判定は
    `run_subproject_loop` 内で行うため、親OFF・子ONのコマンドも本判定を通過してループへ入る。
    """
    if ctx.subproject_cwd is not None:
        return False
    subprojects = ctx.base.subprojects
    if len(subprojects) < 2:
        return False
    info = ctx.config.commands.get(command)
    if info is None:
        return False
    default_aware = info.subproject_aware
    return pyfltr.config.config.resolve_subproject_aware(ctx.config.values, command, default_aware)


def _run_one(
    command: str,
    args: argparse.Namespace,
    ctx: ExecutionContext,
    sub: typing.Any,
    sub_config: typing.Any,
    *,
    dispatch_fn: typing.Callable[[str, argparse.Namespace, ExecutionContext], CommandResult],
) -> CommandResult:
    """1つのサブプロジェクトで当該コマンドを実行し、区切り行を付けた結果を返す。"""
    sub_base = dataclasses.replace(ctx.base, config=sub_config)
    sub_ctx = dataclasses.replace(ctx, base=sub_base, subproject_cwd=sub.cwd)
    sub_result = dispatch_fn(command, args, sub_ctx)
    # output 冒頭にサブプロジェクト区切り行を挿入する（人間向け識別のため）
    if sub_result.output:
        sub_result.output = f"# subproject: {sub.relative}\n{sub_result.output}"
    return sub_result


def resolve_subproject_workers(command: str, ctx: ExecutionContext, cwds: typing.Sequence[typing.Any]) -> int:
    """サブプロジェクトを同時に実行する上限を`jobs`から決める。

    `jobs`を当該ツール自身のワーカー数の推定値で割った値とする。
    推定値はサブプロジェクトごとに異なりうるため最大値を基準とし、
    ツール側の並列度との積が`jobs`を超えないようにする。

    `jobs`が推定値以下の場合は1を返し、サブプロジェクトを1件ずつ実行する。
    """
    values = ctx.config.values
    budget = values.get("jobs", 1)
    if not isinstance(budget, int) or budget < 1:
        budget = 1
    tool_workers = 1
    for cwd in cwds:
        tool_workers = max(
            tool_workers,
            pyfltr.command.tool_parallelism.estimate_tool_workers(command, values, cwd),
        )
    return max(1, budget // tool_workers)


def run_subproject_loop(
    command: str,
    args: argparse.Namespace,
    ctx: ExecutionContext,
    *,
    dispatch_fn: typing.Callable[[str, argparse.Namespace, ExecutionContext], CommandResult],
    disabled_skip_fn: typing.Callable[[str, ExecutionContext], CommandResult],
) -> CommandResult:
    """サブプロジェクト別ループでツールを実行し `CommandResult` をマージする。

    各サブプロジェクトの設定（`base.subproject_configs`）で当該コマンドのON/OFFを再判定し、
    無効のサブプロジェクト（親ON・子OFF）とファイル0件のサブプロジェクトは実行から除外する。
    外部パス（`base.external_files`）への適用は起点設定のON/OFFで固定し、起点で無効なら何も行わない。

    有効なサブプロジェクトは `jobs` から導いた上限まで同時に実行する。
    実行順が非決定になっても報告順を変えないよう、`relative` の昇順で並べてから集約する。

    結果は `CommandResult.merge` で集約する（1件のみならそのまま返す）。
    いずれのサブプロジェクトでも実行されず、設定による無効スキップが発生したか起点でも無効な場合は、
    起点cwdでの全ファイル誤実行を避けて skipped 結果を返す。
    全件ファイル0件かつ起点で有効なときのみ通常経路の0件結果を返す（`dispatch_fn` 経由）。
    """
    with pyfltr.warnings_.suppress_duplicates():
        base = ctx.base
        # 起点設定での当該コマンドのON/OFF。外部パス追加実行とフォールバックの採否に用いる。
        start_enabled = base.config.values.get(command) is True
        subproject_results: list[CommandResult] = []
        # 設定で無効化してスキップしたサブプロジェクトの有無。0件スキップと区別し誤実行を抑止する。
        skipped_by_config = False
        targets: list[tuple[typing.Any, typing.Any]] = []
        for sub in base.subprojects:
            sub_files = base.subproject_files.get(sub.cwd, [])
            if not sub_files:
                continue
            sub_config = base.subproject_configs.get(sub.cwd, base.config)
            if sub_config.values.get(command) is not True:
                # 親ON・子OFF: 当該サブプロジェクトの設定で無効化されているため実行しない。
                skipped_by_config = True
                continue
            targets.append((sub, sub_config))

        if targets:
            workers = min(len(targets), resolve_subproject_workers(command, ctx, [sub.cwd for sub, _ in targets]))
            # 実行順が非決定になっても報告順を変えないため、`relative`の昇順で集約する。
            ordered = sorted(targets, key=lambda item: item[0].relative)
            if workers <= 1:
                subproject_results.extend(
                    [_run_one(command, args, ctx, sub, sub_config, dispatch_fn=dispatch_fn) for sub, sub_config in ordered]
                )
            else:
                # ワーカースレッドは既定で空のコンテキストを持つため、警告の重複抑止スコープを引き継ぐ。
                with concurrent.futures.ThreadPoolExecutor(
                    max_workers=workers,
                    initializer=pyfltr.warnings_.adopt_suppression_state,
                    initargs=(pyfltr.warnings_.current_suppression_state(),),
                ) as executor:
                    futures = [
                        executor.submit(_run_one, command, args, ctx, sub, sub_config, dispatch_fn=dispatch_fn)
                        for sub, sub_config in ordered
                    ]
                    subproject_results.extend([future.result() for future in futures])

        # 外部パスへの追加実行・警告は起点設定のON/OFFで固定する（起点で無効なら何も行わない）。
        # `allows_external_paths=True`のツールは起点cwdで外部パス専用に追加実行し、注入対象では
        # `config_arg_template`の自動注入が適用される。それ以外は除外して警告のみ発行する。
        # `--allow-external-paths`指定時は分類にかかわらず追加実行の対象とする。
        info = ctx.config.commands.get(command)
        external_files = base.external_files
        if external_files and info is not None and start_enabled:
            # 当該ツールのglob条件にマッチする外部パスのみを対象とする
            # （サブプロジェクト経路と同じ`filter_by_globs`基準）。
            relevant_external = pyfltr.command.targets.filter_by_globs(external_files, info.target_globs())
            if relevant_external:
                if info.allows_external_paths or args.allow_external_paths:
                    ext_base = dataclasses.replace(
                        base, all_files=relevant_external, subprojects=[], subproject_files={}, external_files=[]
                    )
                    ext_ctx = dataclasses.replace(ctx, base=ext_base, subproject_cwd=None)
                    ext_result = dispatch_fn(command, args, ext_ctx)
                    if ext_result.output:
                        ext_result.output = f"# external paths\n{ext_result.output}"
                    subproject_results.append(ext_result)
                else:
                    for t in relevant_external:
                        normalized_target = pyfltr.paths.normalize_separators(t)
                        pyfltr.warnings_.emit_warning(
                            source="external-path",
                            message=(f"{command}: 起点cwd外のパスは対象から除外しました: {normalized_target}"),
                        )
                        pyfltr.warnings_.add_filtered_direct_file(normalized_target, reason="external")

        if subproject_results:
            if len(subproject_results) == 1:
                return subproject_results[0]
            return CommandResult.merge(subproject_results)

        if skipped_by_config or not start_enabled:
            # 設定で無効化してスキップした、または起点でも無効。
            # 起点cwdで全ファイルを誤実行しないよう、対象0件相当のskipped結果を返す。
            return disabled_skip_fn(command, ctx)
        # 全サブプロジェクトでファイル0件 → 通常経路の0件結果を返す（`dispatch_fn` 経由）。
        return dispatch_fn(command, args, ctx)
