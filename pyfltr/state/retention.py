"""世代管理ディレクトリの共通クリーンアップロジック。

`pyfltr/state/archive.py`（実行アーカイブ）と`pyfltr/grep_/history.py`（replace履歴）は、
ULID採番ディレクトリを世代数・合計サイズ・保存期間の3軸で自動クリーンアップする
同一パターンを採用する。ポリシーの型（`ArchivePolicy` / `ReplaceHistoryPolicy`）は
呼び出し側それぞれの公開インターフェースとして維持し、本モジュールは
ポリシー非依存の共通処理（`RetentionPolicy`への変換後のクリーンアップ本体・
`meta.json`のタイムスタンプ解析・ディレクトリサイズ計測・削除）のみを集約する。
"""

import collections.abc
import dataclasses
import datetime as dt
import json
import pathlib
import shutil


@dataclasses.dataclass(frozen=True)
class RetentionPolicy:
    """世代管理ディレクトリの自動クリーンアップ閾値（呼び出し側ポリシーの共通形）。"""

    max_entries: int
    """保存する最大世代数。"""
    max_size_bytes: int
    """ディレクトリ全体の合計バイト数の上限。"""
    max_age_days: int
    """保存期間の上限（日数）。"""


def now_iso() -> str:
    """現在時刻をISO 8601（UTC、マイクロ秒付き）で返す。"""
    return dt.datetime.now(dt.UTC).isoformat()


def timestamp_of(entry_dir: pathlib.Path, *, meta_filename: str, timestamp_key: str) -> dt.datetime | None:
    """世代ディレクトリの`meta_filename`から`timestamp_key`をパースして返す。

    `meta.json`が存在しない・破損している・キー未設定・パース不能のいずれの場合も`None`を返す
    （呼び出し側は`None`を「期間超過の削除対象から除外」の意味で扱う）。
    """
    meta_path = entry_dir / meta_filename
    if not meta_path.exists():
        return None
    try:
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    raw = meta.get(timestamp_key)
    if not raw:
        return None
    try:
        return dt.datetime.fromisoformat(raw)
    except ValueError:
        return None


def dir_size(path: pathlib.Path) -> int:
    """ディレクトリ配下の合計バイト数を返す。"""
    total = 0
    for entry in path.rglob("*"):
        if entry.is_file():
            try:
                total += entry.stat().st_size
            except OSError:
                continue
    return total


def rmtree_silent(path: pathlib.Path) -> None:
    """ディレクトリを無言で再帰削除する。"""
    shutil.rmtree(path, ignore_errors=True)


def cleanup_generational_directory(
    root: pathlib.Path,
    policy: RetentionPolicy,
    *,
    meta_filename: str,
    timestamp_key: str,
    fallback_timestamp_key: str | None = None,
    is_active: collections.abc.Callable[[pathlib.Path], bool] | None = None,
) -> list[str]:
    """世代管理ディレクトリへ世代数・合計サイズ・保存期間の3軸クリーンアップを適用する。

    世代数 / 合計サイズ / 保存期間のいずれかを超過した時点で古い順に削除し、
    削除したディレクトリ名のリストを返す。
    `ArchiveStore.cleanup`と`ReplaceHistoryStore.cleanup`が同一アルゴリズムを共有するため、
    本関数へ集約する。

    `fallback_timestamp_key`を省略した場合は、ディレクトリ名昇順（= ULIDタイムスタンプ昇順）を古い順とし、
    期間は`timestamp_key`の時刻で判定する。
    指定した場合は`timestamp_key`、無ければ`fallback_timestamp_key`の時刻を基準時刻とし、
    基準時刻の昇順（時刻を読めないディレクトリは最古として扱う）を古い順とする。

    `is_active`が真を返すディレクトリはどの軸でも削除せず、世代数と合計サイズの計数には含める。
    """
    if not root.exists():
        return []
    entries = sorted((entry for entry in root.iterdir() if entry.is_dir()), key=lambda p: p.name)
    if not entries:
        return []

    def reference_time(entry: pathlib.Path) -> dt.datetime | None:
        timestamp = timestamp_of(entry, meta_filename=meta_filename, timestamp_key=timestamp_key)
        if timestamp is None and fallback_timestamp_key is not None:
            timestamp = timestamp_of(entry, meta_filename=meta_filename, timestamp_key=fallback_timestamp_key)
        if timestamp is not None and timestamp.tzinfo is None:
            timestamp = timestamp.replace(tzinfo=dt.UTC)
        return timestamp

    times = {entry: reference_time(entry) for entry in entries}
    if fallback_timestamp_key is not None:
        oldest = dt.datetime.min.replace(tzinfo=dt.UTC)
        entries.sort(key=lambda entry: (times[entry] or oldest, entry.name))
    active = {entry for entry in entries if is_active is not None and is_active(entry)}
    removed: list[str] = []

    def remove(entry: pathlib.Path) -> None:
        rmtree_silent(entry)
        removed.append(entry.name)
        entries.remove(entry)

    # 期間超過の削除（最古から）
    if policy.max_age_days > 0:
        now = dt.datetime.now(dt.UTC)
        age_limit = dt.timedelta(days=policy.max_age_days)
        for entry in list(entries):
            timestamp = times[entry]
            if entry in active or timestamp is None:
                continue
            if now - timestamp > age_limit:
                remove(entry)

    # 世代数超過の削除
    if policy.max_entries > 0:
        for entry in [entry for entry in entries if entry not in active]:
            if len(entries) <= policy.max_entries:
                break
            remove(entry)

    # サイズ超過の削除（古い方から）
    if policy.max_size_bytes > 0:
        sizes = {entry: dir_size(entry) for entry in entries}
        total = sum(sizes.values())
        for entry in [entry for entry in entries if entry not in active]:
            if total <= policy.max_size_bytes:
                break
            remove(entry)
            total -= sizes[entry]

    return removed
