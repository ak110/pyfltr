"""世代管理ディレクトリの共通クリーンアップロジック。

`pyfltr/state/archive.py`（実行アーカイブ）と`pyfltr/grep_/history.py`（replace履歴）は、
ULID採番ディレクトリを世代数・合計サイズ・保存期間の3軸で自動クリーンアップする
同一パターンを採用する。ポリシーの型（`ArchivePolicy` / `ReplaceHistoryPolicy`）は
呼び出し側それぞれの公開インターフェースとして維持し、本モジュールは
ポリシー非依存の共通処理（`RetentionPolicy`への変換後のクリーンアップ本体・
`meta.json`のタイムスタンプ解析・ディレクトリサイズ計測・削除）のみを集約する。
"""

import dataclasses
import datetime
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
    return datetime.datetime.now(datetime.UTC).isoformat()


def timestamp_of(entry_dir: pathlib.Path, *, meta_filename: str, timestamp_key: str) -> datetime.datetime | None:
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
        return datetime.datetime.fromisoformat(raw)
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
) -> list[str]:
    """世代管理ディレクトリへ世代数・合計サイズ・保存期間の3軸クリーンアップを適用する。

    世代数 / 合計サイズ / 保存期間のいずれかを超過した時点で、古い順
    （ディレクトリ名昇順 = ULIDタイムスタンプ昇順）に削除し、削除したディレクトリ名のリストを返す。
    `ArchiveStore.cleanup`と`ReplaceHistoryStore.cleanup`が同一アルゴリズムを共有するため、
    本関数へ集約する。
    """
    if not root.exists():
        return []
    entries = sorted((entry for entry in root.iterdir() if entry.is_dir()), key=lambda p: p.name)
    if not entries:
        return []
    removed: list[str] = []
    now = datetime.datetime.now(datetime.UTC)
    age_limit = datetime.timedelta(days=policy.max_age_days) if policy.max_age_days > 0 else None

    # 期間超過の削除（最古から）
    if age_limit is not None:
        for entry in list(entries):
            timestamp = timestamp_of(entry, meta_filename=meta_filename, timestamp_key=timestamp_key)
            if timestamp is None:
                continue
            if now - timestamp > age_limit:
                rmtree_silent(entry)
                removed.append(entry.name)
                entries.remove(entry)

    # 世代数超過の削除
    if policy.max_entries > 0 and len(entries) > policy.max_entries:
        overflow = len(entries) - policy.max_entries
        for entry in entries[:overflow]:
            rmtree_silent(entry)
            removed.append(entry.name)
        entries = entries[overflow:]

    # サイズ超過の削除（古い方から）
    if policy.max_size_bytes > 0:
        total = sum(dir_size(entry) for entry in entries)
        for entry in list(entries):
            if total <= policy.max_size_bytes:
                break
            size = dir_size(entry)
            rmtree_silent(entry)
            removed.append(entry.name)
            total -= size
            entries.remove(entry)

    return removed
