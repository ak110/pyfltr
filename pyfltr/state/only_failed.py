"""`--only-failed` フィルター処理。

直前runの失敗情報から再実行対象コマンドとファイル集合を構築する。
"""

from __future__ import annotations

import argparse
import dataclasses
import logging
import pathlib
import typing

import pyfltr.cli.output_format
import pyfltr.paths
import pyfltr.state.archive
import pyfltr.state.runs
import pyfltr.warnings_

logger = logging.getLogger(__name__)


@dataclasses.dataclass(frozen=True)
class ToolTargets:
    """ツール別の実行対象ファイル指定。

    mode="fallback": 診断ファイルなしのツール（pytest等）でall_filesをそのまま使う。
    mode="files": 失敗ファイルのみを対象とする。

    旧形式（`dict[str, list[pathlib.Path] | None]`）では`None`と空リストの
    違いがコードを読むだけでは不明瞭で、フォールバック実行と除外扱いを取り違える
    実装ミスを誘発しやすかった。`mode`属性で二状態を型として区別し、対象ファイル
    リスト取得は`resolve_files()`の単一経路に集約する。
    """

    mode: typing.Literal["fallback", "files"]
    files: tuple[pathlib.Path, ...]

    @classmethod
    def fallback_default(cls) -> ToolTargets:
        """診断ファイルなしのツール向け（all_filesフォールバック）インスタンスを返す。"""
        return cls(mode="fallback", files=())

    @classmethod
    def with_files(cls, files: typing.Iterable[pathlib.Path]) -> ToolTargets:
        """フィルタリング済みファイル集合付きインスタンスを返す。"""
        return cls(mode="files", files=tuple(files))

    def resolve_files(self, all_files: list[pathlib.Path]) -> list[pathlib.Path]:
        """実行対象ファイルのリストを返す。

        mode="fallback"のときall_filesをそのまま返す。
        mode="files"のときself.filesのリストを返す。
        """
        if self.mode == "fallback":
            return all_files
        if self.mode == "files":
            return list(self.files)
        typing.assert_never(self.mode)


def apply_filter(
    args: argparse.Namespace,
    commands: list[str],
    all_files: list[pathlib.Path],
    *,
    from_run: str | None = None,
) -> tuple[list[str], dict[str, ToolTargets] | None, bool]:
    """`--only-failed`指定時、直前runからツール別の失敗ファイル集合を構築する。

    Args:
        args: コマンドライン引数。`args.only_failed`が真のとき適用する。
        commands: 実行対象コマンド名のリスト。
        all_files: `expand_all_files`の結果。`args.targets`指定があれば既にフィルタリング済み。
        from_run: 参照対象runを明示指定する（前方一致 / `latest`対応）。
            `None`の場合は直前runを自動選択する。

    Returns:
        `(フィルタリング後commands, per_tool_targets, exit_early)`
        - `args.only_failed`が偽の場合は`(commands, None, False)`を返す（未適用）
        - 直前runが存在しない / アーカイブ読み取り失敗 / 失敗ツールが無い / 全ツールで
          targets交差が空の場合は`(commands, None, True)`を返し、呼び出し側で
          rc=0の早期終了を促す
        - それ以外はフィルタリング後commandsとToolTargets dictを返す（exit_early=False）

    `all_files`は`expand_all_files`の結果（`args.targets`指定があれば既に
    その範囲でフィルタリング済み）。本関数ではそれとの交差を取ることで
    `--only-failed`と位置引数`targets`の併用要件を同時に満たす。
    """
    if not getattr(args, "only_failed", False):
        return commands, None, False

    try:
        store = pyfltr.state.archive.ArchiveStore()
    except OSError as e:
        pyfltr.warnings_.emit_warning(
            source="only-failed",
            message=f"実行アーカイブを読み取れません: {e}",
        )
        return commands, None, True

    # from_run指定時にrun_idを解決し、失敗ならwarningを発行して早期終了する。
    # 解決済みrun_idを_load_run_summaryに渡すことで二重解決を避ける。
    resolved_run_id: str | None = None
    if from_run is not None:
        try:
            resolved_run_id = pyfltr.state.runs.resolve_run_id(store, from_run)
        except pyfltr.state.runs.RunIdError as e:
            logger.warning(f"--from-run {from_run!r}: {e}")
            return commands, None, True

    last_run = _load_run_summary(store, resolved_run_id=resolved_run_id)
    if last_run is None:
        _log_skip_reason("参照可能な直前 run が見つかりません。対象なしでスキップします。")
        return commands, None, True

    failed_tools = _collect_failed_tools(store, last_run.run_id, commands)
    if not failed_tools:
        _log_skip_reason(f"直前 run ({last_run.run_id}) に失敗ツールがありません。対象なしでスキップします。")
        return commands, None, True

    # ツール別のターゲットを構築する。交差空のツールはtargets dictから除外し、
    # filtered_commandsでも除外することでskip状態はdictに含めない設計とする。
    targets: dict[str, ToolTargets] = {}
    for tool in failed_tools:
        tool_targets = _extract_failed_files_for_tool(store, last_run.run_id, tool, all_files)
        if tool_targets is not None:
            targets[tool] = tool_targets

    filtered_commands = _filter_commands_with_targets(commands, targets)
    if not filtered_commands:
        _log_skip_reason(
            f"直前 run ({last_run.run_id}) の失敗ツールはすべて指定 targets と交差しません。対象なしでスキップします。"
        )
        return commands, None, True

    pyfltr.cli.output_format.text_logger.info(
        f"--only-failed: 直前 run ({last_run.run_id}) から {len(filtered_commands)} ツールを対象として再実行します。"
    )
    return filtered_commands, targets, False


def _load_run_summary(
    store: pyfltr.state.archive.ArchiveStore,
    *,
    resolved_run_id: str | None = None,
) -> pyfltr.state.archive.RunSummary | None:
    """runのサマリを返す。取得失敗または存在しない場合はNone。

    `resolved_run_id`が指定された場合は、そのrun_idに対応するサマリを返す。
    未指定の場合は`list_runs(limit=1)`で最新runを取得する。
    """
    if resolved_run_id is not None:
        try:
            # ArchiveStoreにはrun_id直接検索APIが無いためlist_runs()から探す。
            all_runs = store.list_runs()
        except OSError as e:
            pyfltr.warnings_.emit_warning(
                source="only-failed",
                message=f"実行アーカイブを読み取れません: {e}",
            )
            return None
        return next((r for r in all_runs if r.run_id == resolved_run_id), None)

    try:
        runs = store.list_runs(limit=1)
    except OSError as e:
        pyfltr.warnings_.emit_warning(
            source="only-failed",
            message=f"実行アーカイブを読み取れません: {e}",
        )
        return None
    return runs[0] if runs else None


def _collect_failed_tools(
    store: pyfltr.state.archive.ArchiveStore,
    run_id: str,
    commands: list[str],
) -> list[str]:
    """直前runから失敗ツールの名前一覧を返す。読み取り失敗時は空リスト。"""
    try:
        tool_names = store.list_tools(run_id)
    except OSError as e:
        pyfltr.warnings_.emit_warning(
            source="only-failed",
            message=f"直前 run のツール一覧を読み取れません: {e}",
        )
        return []

    commands_set = set(commands)
    failed: list[str] = []
    for tool in tool_names:
        if tool not in commands_set:
            continue
        try:
            meta = store.read_tool_meta(run_id, tool)
        except OSError:
            continue
        if meta.get("status") in {"failed", "resolution_failed"}:
            failed.append(tool)
    return failed


def _extract_failed_files_for_tool(
    store: pyfltr.state.archive.ArchiveStore,
    run_id: str,
    tool: str,
    all_files: list[pathlib.Path],
) -> ToolTargets | None:
    """ツール別の失敗ファイル集合を抽出する。

    - 診断ファイルなし（pytest等）: ToolTargets.fallback_default()
    - 診断あり・all_filesとの交差あり: ToolTargets.with_files(交差ファイル)
    - 診断あり・交差空: None（呼び出し側で除外扱い）
    """
    try:
        diagnostics = store.read_tool_diagnostics(run_id, tool)
    except OSError:
        diagnostics = []

    failed_files = {d["file"] for d in diagnostics if isinstance(d.get("file"), str)}
    if not failed_files:
        # 診断ファイル無し（pass-filenames=Falseのツール等）→ 既定対象でフォールバック実行
        return ToolTargets.fallback_default()

    # all_filesを正規化済み相対パス文字列でキーに持つ辞書にしておき、
    # 診断側の`file`（`_normalize_path`済み）とそのまま比較する。
    # all_files_mapを基準に走査することでtarget側の並び順を保つ
    # （failed_filesを直接反復するとセット由来の順序不定が混入する）。
    all_files_map = {pyfltr.paths.normalize_separators(p): p for p in all_files}
    intersected = [path for key, path in all_files_map.items() if key in failed_files]
    if not intersected:
        # 交差空 → 除外扱い（Noneを返すことで呼び出し側がtargets dictから除く）
        return None
    return ToolTargets.with_files(intersected)


def _filter_commands_with_targets(
    commands: list[str],
    targets: dict[str, ToolTargets],
) -> list[str]:
    """Targets dictに含まれるツールのみにcommandsを限定する（順序は保持）。"""
    return [cmd for cmd in commands if cmd in targets]


def _log_skip_reason(reason: str) -> None:
    """`--only-failed`の早期終了理由をログ出力する。"""
    pyfltr.cli.output_format.text_logger.info(f"--only-failed: {reason}")
