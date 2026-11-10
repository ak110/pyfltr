"""実行アーカイブとキャッシュの準備・保存フック。"""

import logging
import pathlib
import typing

import pyfltr.command.core_
import pyfltr.command.execution_slot
import pyfltr.config.model
import pyfltr.run_options
import pyfltr.state.archive
import pyfltr.state.cache
import pyfltr.warnings_

logger = logging.getLogger(__name__)


def archive_failure_hint(*, can_disable: bool) -> str:
    """アーカイブを無効化できる呼び出し手段かに応じた対処を返す。"""
    action = "キャッシュディレクトリの書き込み権限と空き容量を確認してください。"
    if can_disable:
        return action + "実行アーカイブが不要なら `--no-archive` を指定すると警告を抑止できます。"
    return action + "MCPのrunでは実行アーカイブを無効化できません。"


def make_archive_hook(
    archive_store: pyfltr.state.archive.ArchiveStore,
    run_id: str,
    *,
    archive_required: bool = False,
) -> typing.Callable[[pyfltr.command.core_.CommandResult], None]:
    """アーカイブ書き込みフックを生成する。

    書き込みに成功した場合のみ`result.archived = True`を設定し、
    smart truncationの可否判定に使う。失敗時は警告を発行して処理を続行する。
    """

    def _hook(result: pyfltr.command.core_.CommandResult) -> None:
        try:
            archive_store.write_tool_result(run_id, result)
        except OSError as e:
            # ハンドラ内でwarningを通知してもsummary末尾にまとまる。
            pyfltr.warnings_.emit_warning(
                source="archive",
                message=(
                    f"{result.command} の結果を実行アーカイブへ書き込めませんでした: {e}。"
                    "このコマンドの結果は `show-run` と `--only-failed` から参照できません"
                ),
                hint=archive_failure_hint(can_disable=not archive_required),
            )
            return
        # 書き込み成功時のみarchived=Trueに更新。smart truncationの可否判定に使う。
        result.archived = True

    return _hook


def make_slot_wait_hook(
    archive_store: pyfltr.state.archive.ArchiveStore,
    run_id: str,
    *,
    archive_required: bool = False,
) -> pyfltr.command.execution_slot.WaitIntervalCallback:
    """実行枠の待機区間を実行アーカイブへ保存するフックを生成する。

    書き込みに失敗した場合は最初の1回だけ警告を発行し、検証自体は続行する。
    """
    warned = False

    def _hook(wait: pyfltr.command.execution_slot.SlotWait) -> None:
        nonlocal warned
        try:
            archive_store.record_slot_wait(run_id, wait.to_dict())
        except OSError as e:
            if warned:
                return
            warned = True
            pyfltr.warnings_.emit_warning(
                source="archive",
                message=(
                    f"実行枠の待機区間を実行アーカイブへ書き込めませんでした: {e}。"
                    "このrunの待機時間は `show-run` から参照できません"
                ),
                hint=archive_failure_hint(can_disable=not archive_required),
            )

    return _hook


def initialize_archive(
    args: pyfltr.run_options.RunOptions,
    config: pyfltr.config.model.Config,
    commands: list[str],
    all_files: list[pathlib.Path],
    start_cwd_path: pathlib.Path,
    *,
    archive_required: bool = False,
) -> tuple[pyfltr.state.archive.ArchiveStore | None, str | None]:
    """実行アーカイブを初期化し、失敗時は警告とともに無効化する。"""
    # 実行アーカイブの初期化 （既定で有効）。
    # `--no-archive` または `archive = false` で無効化できる。クリーンアップ失敗や
    # 書き込み失敗はパイプライン本体を止めないようwarningsへ転送する。
    archive_enabled = bool(config.values.get("archive", True)) and not getattr(args, "no_archive", False)
    archive_store: pyfltr.state.archive.ArchiveStore | None = None
    run_id: str | None = None
    if archive_enabled:
        try:
            archive_store = pyfltr.state.archive.ArchiveStore()
            run_id = archive_store.start_run(commands=commands, files=len(all_files), cwd=str(start_cwd_path))
            removed = archive_store.cleanup(pyfltr.state.archive.policy_from_config(config))
            if removed:
                logger.debug("archive: 自動削除で %d 件の古い run を削除", len(removed))
        except OSError as e:
            if archive_store is not None and run_id is not None:
                archive_store.release_run(run_id)
            pyfltr.warnings_.emit_warning(
                source="archive",
                message=(
                    f"実行アーカイブを初期化できないため、アーカイブ無しで続行しました: {e}。"
                    "この実行にはrun_idが付かず、`show-run` と次回の `--only-failed` から参照できません"
                ),
                hint=archive_failure_hint(can_disable=not archive_required),
            )
            archive_store = None
            run_id = None

    return archive_store, run_id


def initialize_cache(
    args: pyfltr.run_options.RunOptions,
    config: pyfltr.config.model.Config,
) -> pyfltr.state.cache.CacheStore | None:
    """ファイルキャッシュを初期化し、失敗時は警告とともに無効化する。"""
    # ファイルhashキャッシュの初期化 （既定で有効）。
    # `--no-cache` または `cache = false` で無効化できる。期間超過エントリの削除失敗や
    # 書き込み失敗はパイプライン本体を止めないためwarningsへ転送する。
    cache_enabled = bool(config.values.get("cache", True)) and not getattr(args, "no_cache", False)
    cache_store: pyfltr.state.cache.CacheStore | None = None
    if cache_enabled:
        try:
            cache_store = pyfltr.state.cache.CacheStore()
            cache_removed = cache_store.cleanup(pyfltr.state.cache.cache_policy_from_config(config))
            if cache_removed:
                logger.debug("cache: 期間超過で %d 件のエントリを削除", len(cache_removed))
        except OSError as e:
            pyfltr.warnings_.emit_warning(
                source="cache",
                message=(
                    f"ファイル hash キャッシュを初期化できないため、キャッシュ無しで続行しました: {e}。"
                    "検査結果は変わりませんが、変更の無いファイルも再検査します"
                ),
                hint=(
                    "キャッシュディレクトリの書き込み権限と空き容量を確認してください。"
                    "キャッシュが不要なら `--no-cache` を指定すると警告を抑止できます。"
                ),
            )
            cache_store = None

    return cache_store


def finalize_archive(
    archive_store: pyfltr.state.archive.ArchiveStore | None,
    run_id: str | None,
    returncode: int,
    commands: list[str],
    files: int,
    *,
    archive_required: bool = False,
) -> None:
    """終了コードと完了時刻を保存する。保存失敗は検査結果を変えない。"""
    # アーカイブ終端: meta.jsonにexit_code / finished_atを書き込む。
    if archive_store is not None and run_id is not None:
        try:
            archive_store.finalize_run(run_id, exit_code=returncode, commands=commands, files=files)
        except OSError as e:
            pyfltr.warnings_.emit_warning(
                source="archive",
                message=(
                    f"実行アーカイブの meta.json を更新できませんでした: {e}。"
                    "`list-runs` にこの実行の終了コードと完了時刻が表示されません"
                ),
                hint=archive_failure_hint(can_disable=not archive_required),
            )
