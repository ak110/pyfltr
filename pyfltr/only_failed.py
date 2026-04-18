"""--only-failed フィルター処理。

直前 run の失敗情報から再実行対象コマンドとファイル集合を構築する。
"""

from __future__ import annotations

import argparse
import dataclasses
import logging
import pathlib
import typing

import pyfltr.archive
import pyfltr.paths
import pyfltr.runs
import pyfltr.warnings_

logger = logging.getLogger(__name__)


@dataclasses.dataclass(frozen=True)
class ToolTargets:
    """ツール別の実行対象ファイル指定。

    mode="fallback": 診断ファイルなしのツール（pytest 等）で all_files をそのまま使う。
    mode="files": 失敗ファイルのみを対象とする。
    """

    mode: typing.Literal["fallback", "files"]
    files: tuple[pathlib.Path, ...]

    @classmethod
    def fallback_default(cls) -> ToolTargets:
        """診断ファイルなしのツール向け（all_files フォールバック）インスタンスを返す。"""
        return cls(mode="fallback", files=())

    @classmethod
    def with_files(cls, files: typing.Iterable[pathlib.Path]) -> ToolTargets:
        """絞り込みファイル集合付きインスタンスを返す。"""
        return cls(mode="files", files=tuple(files))

    def resolve_files(self, all_files: list[pathlib.Path]) -> list[pathlib.Path]:
        """実行対象ファイルのリストを返す。

        mode="fallback" のとき all_files をそのまま返す。
        mode="files" のとき self.files のリストを返す。
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
    """``--only-failed`` 指定時、直前 run からツール別の失敗ファイル集合を構築する。

    Args:
        args: コマンドライン引数。``args.only_failed`` が真のとき適用する。
        commands: 実行対象コマンド名のリスト。
        all_files: ``expand_all_files`` の結果。``args.targets`` 指定があれば既に絞り込み済み。
        from_run: 参照対象 run を明示指定する（前方一致 / ``latest`` 対応）。
            ``None`` の場合は直前 run を自動選択する。

    Returns:
        ``(絞り込み後 commands, per_tool_targets, exit_early)``
        - ``args.only_failed`` が偽の場合は ``(commands, None, False)`` を返す（未適用）
        - 直前 run が存在しない / アーカイブ読み取り失敗 / 失敗ツールが無い / 全ツールで
          targets 交差が空の場合は ``(commands, None, True)`` を返し、呼び出し側で
          rc=0 の早期終了を促す
        - それ以外は絞り込み後 commands と ToolTargets dict を返す（exit_early=False）

    ``all_files`` は ``expand_all_files`` の結果（``args.targets`` 指定があれば既に
    その範囲に絞り込まれている）。本関数ではそれとの交差を取ることで
    ``--only-failed`` と位置引数 ``targets`` の併用要件を同時に満たす。
    """
    if not getattr(args, "only_failed", False):
        return commands, None, False

    try:
        store = pyfltr.archive.ArchiveStore()
    except OSError as e:
        pyfltr.warnings_.emit_warning(
            source="only-failed",
            message=f"実行アーカイブを読み取れません: {e}",
        )
        return commands, None, True

    # from_run 指定時に run_id が解決できない場合は warning を出して早期終了する。
    # 解決できた場合は _load_last_run にサマリ取得を委ねる。
    if from_run is not None:
        try:
            pyfltr.runs.resolve_run_id(store, from_run)
        except pyfltr.runs.RunIdError as e:
            logger.warning(f"--from-run {from_run!r}: {e}")
            return commands, None, True

    last_run = _load_last_run(store, from_run=from_run)
    if last_run is None:
        _log_skip_reason("参照可能な直前 run が見つかりません。対象なしでスキップします。")
        return commands, None, True

    failed_tools = _collect_failed_tools(store, last_run.run_id, commands)
    if not failed_tools:
        _log_skip_reason(f"直前 run ({last_run.run_id}) に失敗ツールがありません。対象なしでスキップします。")
        return commands, None, True

    # ツール別のターゲットを構築する。交差空のツールは targets dict から除外し、
    # filtered_commands でも除外することで skip 状態は dict に含めない設計とする。
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

    logger.info(f"--only-failed: 直前 run ({last_run.run_id}) から {len(filtered_commands)} ツールを対象として再実行します。")
    return filtered_commands, targets, False


def _load_last_run(
    store: pyfltr.archive.ArchiveStore,
    *,
    from_run: str | None = None,
) -> pyfltr.archive.RunSummary | None:
    """直前 run のサマリを返す。取得失敗または存在しない場合は None。

    ``from_run`` が指定された場合は ``resolve_run_id`` で解決した run_id を用いる。
    呼び出し元 (``apply_filter``) で ``RunIdError`` が発生しないことを確認済みの前提で
    呼ばれるため、ここでは ``OSError`` のみを捕捉する。
    未指定の場合は ``list_runs(limit=1)`` で最新 run を取得する。
    """
    if from_run is not None:
        try:
            run_id = pyfltr.runs.resolve_run_id(store, from_run)
            # ArchiveStore には run_id 直接引き当て API が無いため list_runs() から探す。
            all_runs = store.list_runs()
        except OSError as e:
            pyfltr.warnings_.emit_warning(
                source="only-failed",
                message=f"実行アーカイブを読み取れません: {e}",
            )
            return None
        return next((r for r in all_runs if r.run_id == run_id), None)

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
    store: pyfltr.archive.ArchiveStore,
    run_id: str,
    commands: list[str],
) -> list[str]:
    """直前 run から失敗ツールの名前一覧を返す。読み取り失敗時は空リスト。"""
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
        if meta.get("status") == "failed":
            failed.append(tool)
    return failed


def _extract_failed_files_for_tool(
    store: pyfltr.archive.ArchiveStore,
    run_id: str,
    tool: str,
    all_files: list[pathlib.Path],
) -> ToolTargets | None:
    """ツール別の失敗ファイル集合を抽出する。

    - 診断ファイルなし（pytest 等）: ToolTargets.fallback_default()
    - 診断あり・all_files との交差あり: ToolTargets.with_files(交差ファイル)
    - 診断あり・交差空: None（呼び出し側で除外扱い）
    """
    try:
        diagnostics = store.read_tool_diagnostics(run_id, tool)
    except OSError:
        diagnostics = []

    failed_files = {d["file"] for d in diagnostics if isinstance(d.get("file"), str)}
    if not failed_files:
        # 診断ファイル無し（pass-filenames=False のツール等）→ 既定対象でフォールバック実行
        return ToolTargets.fallback_default()

    # all_files を正規化済み相対パス文字列でキーに持つ辞書にしておき、
    # 診断側の ``file``（``_normalize_path`` 済み）とそのまま比較する。
    # all_files_map を基準に走査することで target 側の並び順を保つ
    # （failed_files を直接反復するとセット由来の順序不定が混入する）。
    all_files_map = {pyfltr.paths.normalize_separators(p): p for p in all_files}
    intersected = [path for key, path in all_files_map.items() if key in failed_files]
    if not intersected:
        # 交差空 → 除外扱い（None を返すことで呼び出し側が targets dict から除く）
        return None
    return ToolTargets.with_files(intersected)


def _filter_commands_with_targets(
    commands: list[str],
    targets: dict[str, ToolTargets],
) -> list[str]:
    """Targets dict に含まれるツールのみに commands を絞り込む（順序は保持）。"""
    return [cmd for cmd in commands if cmd in targets]


def _log_skip_reason(reason: str) -> None:
    """--only-failed の早期終了理由をログ出力する。"""
    logger.info(f"--only-failed: {reason}")
