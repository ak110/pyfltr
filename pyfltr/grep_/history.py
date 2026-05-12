"""replace履歴の世代管理。

`pyfltr/state/archive.py`と同じ世代管理パターン（ULID採番・XDG準拠キャッシュ・
3軸自動クリーンアップ）を踏襲する。保存内容は計画通り「変更前全文・変更後ハッシュ・
各置換箇所の前後行」の3点で、変更後全文を別途保存しない。

`FileNotFoundError`の契約: 本モジュールが送出する`FileNotFoundError`の引数は
`replace_id`のみとし、利用者向けメッセージ文面はcatch側
（`pyfltr/cli/replace_subcmd.py`・`pyfltr/cli/mcp_server.py`など）で組み立てる。

ディレクトリ構造（`<cache_root> = pyfltr.state.archive.default_cache_root()`）::

    <cache_root>/replaces/<replace_id>/meta.json
    <cache_root>/replaces/<replace_id>/files/<sanitized_path>/before.txt
    <cache_root>/replaces/<replace_id>/files/<sanitized_path>/changes.json

`<sanitized_path>`はファイルパス由来の安定識別子で、衝突回避のため
「サニタイズ済みベース名 + パス全体のSHA-256短縮ハッシュ」の組み合わせで構築する。
利用者が`changes.json`等を参照する場面で大半はベース名で識別でき、衝突時もハッシュで一意化される。
"""

import dataclasses
import datetime
import hashlib
import json
import logging
import pathlib
import shutil
import typing

import ulid

import pyfltr.config.config
import pyfltr.paths
import pyfltr.state.archive
from pyfltr.grep_.types import ReplaceCommandMeta, ReplaceRecord

logger = logging.getLogger(__name__)

_REPLACES_DIRNAME = "replaces"
_META_FILENAME = "meta.json"
_FILES_DIRNAME = "files"
_BEFORE_FILENAME = "before.txt"
_CHANGES_FILENAME = "changes.json"


def default_history_root() -> pathlib.Path:
    """履歴ディレクトリのルートパスを返す。

    `default_cache_root()`配下の`replaces/`サブディレクトリへ集約することで、
    実行アーカイブ（`runs/`）と並ぶ世代管理として配置する。
    """
    return pyfltr.state.archive.default_cache_root() / _REPLACES_DIRNAME


def generate_replace_id() -> str:
    """ULID形式の`replace_id`を生成する。"""
    return str(ulid.ULID())


@dataclasses.dataclass(frozen=True)
class ReplaceHistoryPolicy:
    """replace履歴の自動クリーンアップ閾値。"""

    max_entries: int
    """保存する最大世代数。"""
    max_size_bytes: int
    """履歴全体の合計バイト数の上限。"""
    max_age_days: int
    """保存期間の上限（日数）。"""


def policy_from_config(config: pyfltr.config.config.Config) -> ReplaceHistoryPolicy:
    """`Config`から`ReplaceHistoryPolicy`を組み立てる。

    既定値は`max_entries=100` / `max_size_bytes=200MB` / `max_age_days=30`。
    実行アーカイブと別管理にする目的で、設定キーは`replace-history-*`系を使う。
    """
    return ReplaceHistoryPolicy(
        max_entries=int(config.values.get("replace-history-max-entries", 100)),
        max_size_bytes=int(config.values.get("replace-history-max-size-bytes", 200 * 1024 * 1024)),
        max_age_days=int(config.values.get("replace-history-max-age-days", 30)),
    )


class ReplaceHistoryStore:
    """replace履歴の読み書き。

    各メソッドはディレクトリ作成・JSON永続化を内部で完結させる。
    呼び出し側はファイルパスを意識せずに高水準APIだけで履歴を扱える。
    """

    def __init__(self, history_root: pathlib.Path | None = None) -> None:
        self._history_root = history_root if history_root is not None else default_history_root()

    @property
    def history_root(self) -> pathlib.Path:
        """履歴ルートディレクトリの絶対パス。"""
        return self._history_root

    def save_replace(
        self,
        replace_id: str,
        *,
        command_meta: ReplaceCommandMeta,
        file_changes: list[dict[str, typing.Any]],
    ) -> None:
        """1回のreplace実行結果を保存する。

        `file_changes`は各ファイル変更の辞書列。期待キーは次の通り。

        - `file` (`pathlib.Path` または `str`): 対象ファイル
        - `before_content` (`str`): 変更前全文
        - `after_hash` (`str`): 変更後全文のSHA-256ハッシュ
        - `records` (`list[ReplaceRecord]`): 各置換箇所のレコード

        `meta.json`には実行コマンドメタとファイル一覧（相対パス・after_hash）を保存する。
        各ファイル本体は`files/<sanitized_path>/before.txt`へ保存し、
        `changes.json`へ`ReplaceRecord`相当のJSON配列を保存する。
        """
        run_dir = self._history_root / replace_id
        run_dir.mkdir(parents=True, exist_ok=True)
        files_dir = run_dir / _FILES_DIRNAME
        files_dir.mkdir(parents=True, exist_ok=True)
        files_meta: list[dict[str, typing.Any]] = []
        for change in file_changes:
            file_path = pathlib.Path(change["file"])
            before_content: str = change["before_content"]
            after_hash: str = change["after_hash"]
            records: list[ReplaceRecord] = change.get("records", [])
            sanitized = _sanitize_file_key(file_path)
            file_dir = files_dir / sanitized
            file_dir.mkdir(parents=True, exist_ok=True)
            (file_dir / _BEFORE_FILENAME).write_text(before_content, encoding="utf-8")
            (file_dir / _CHANGES_FILENAME).write_text(
                json.dumps([_record_to_dict(r) for r in records], ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            files_meta.append(
                {
                    "file": pyfltr.paths.normalize_separators(str(file_path)),
                    "sanitized": sanitized,
                    "after_hash": after_hash,
                    "records_count": len(records),
                }
            )
        meta = {
            "replace_id": replace_id,
            "saved_at": _now_iso(),
            "command": _command_meta_to_dict(command_meta),
            "files": files_meta,
        }
        (run_dir / _META_FILENAME).write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")

    def load_replace(self, replace_id: str) -> dict[str, typing.Any]:
        """指定`replace_id`のメタ情報とファイル一覧を取得する。

        戻り値の`files`各要素には`before_content`と`records`が含まれる。
        """
        meta_path = self._history_root / replace_id / _META_FILENAME
        if not meta_path.exists():
            raise FileNotFoundError(replace_id)
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
        files_dir = self._history_root / replace_id / _FILES_DIRNAME
        for entry in meta.get("files", []):
            sanitized = entry["sanitized"]
            file_dir = files_dir / sanitized
            entry["before_content"] = (file_dir / _BEFORE_FILENAME).read_text(encoding="utf-8")
            changes_raw = (file_dir / _CHANGES_FILENAME).read_text(encoding="utf-8")
            entry["records"] = json.loads(changes_raw)
        return meta

    def undo_replace(
        self,
        replace_id: str,
        *,
        force: bool = False,
    ) -> tuple[list[pathlib.Path], list[pathlib.Path]]:
        """履歴を読み込んでファイルを変更前内容へ復元する。

        まず全ファイルについて保存済み`after_hash`と現在ファイルのハッシュを照合する。
        force未指定で不一致が1件でも検出された場合は警告として全件をスキップ扱いとし、
        書き戻しを行わずに`(restored=[], skipped=[...])`を返す。
        force指定時、または全件一致時のみ実際の書き戻しを実施する。

        Returns:
            `(restored_files, skipped_files)`のタプル。force未指定で不一致が
            含まれる場合は`restored`は空、`skipped`は対象全件となる
            （手動編集を巻き戻す事故を避ける一括中断の方針）
        """
        meta = self.load_replace(replace_id)
        entries = list(meta.get("files", []))

        # 不一致検出パス: force未指定時は1件でも不一致があれば全件スキップへ倒す
        if not force:
            mismatched: list[pathlib.Path] = []
            for entry in entries:
                file_path = pathlib.Path(entry["file"])
                saved_after_hash: str = entry["after_hash"]
                current_hash: str | None = None
                if file_path.exists():
                    current_hash = hashlib.sha256(file_path.read_text(encoding="utf-8").encode("utf-8")).hexdigest()
                if current_hash != saved_after_hash:
                    mismatched.append(file_path)
            if mismatched:
                # 計画方針（grep-replace.md）に従い、不一致時は中断して全件スキップ扱いとする
                return [], [pathlib.Path(entry["file"]) for entry in entries]

        # 書き戻しパス: force指定時または全件一致時のみ実際に書き戻す
        restored: list[pathlib.Path] = []
        for entry in entries:
            file_path = pathlib.Path(entry["file"])
            before_content: str = entry["before_content"]
            file_path.parent.mkdir(parents=True, exist_ok=True)
            file_path.write_text(before_content, encoding="utf-8")
            restored.append(file_path)
        return restored, []

    def list_replaces(self, *, limit: int | None = None) -> list[dict[str, typing.Any]]:
        """保存済み履歴を新しい順（`replace_id`降順）で返す。

        各要素は`meta.json`の生辞書を返す（ファイル本文は含めない）。
        """
        if not self._history_root.exists():
            return []
        entries = sorted(
            (entry for entry in self._history_root.iterdir() if entry.is_dir()),
            key=lambda p: p.name,
            reverse=True,
        )
        if limit is not None:
            entries = entries[:limit]
        result: list[dict[str, typing.Any]] = []
        for entry in entries:
            meta_path = entry / _META_FILENAME
            if not meta_path.exists():
                continue
            try:
                result.append(json.loads(meta_path.read_text(encoding="utf-8")))
            except (OSError, ValueError):
                logger.debug("replace history: 破損した meta.json をスキップ: %s", meta_path)
                continue
        return result

    def cleanup(self, policy: ReplaceHistoryPolicy) -> list[str]:
        """自動クリーンアップを実施する。削除された`replace_id`のリストを返す。

        世代数 / 合計サイズ / 保存期間のいずれかを超過した時点で、古い順
        （`replace_id`昇順 = ULIDタイムスタンプ昇順）に削除する。
        実装は`pyfltr/state/archive.py`の`ArchiveStore.cleanup`を踏襲する。
        """
        if not self._history_root.exists():
            return []
        entries = sorted(
            (entry for entry in self._history_root.iterdir() if entry.is_dir()),
            key=lambda p: p.name,
        )
        if not entries:
            return []
        removed: list[str] = []
        now = datetime.datetime.now(datetime.UTC)
        age_limit = datetime.timedelta(days=policy.max_age_days) if policy.max_age_days > 0 else None

        # 期間超過の削除（最古から）
        if age_limit is not None:
            for entry in list(entries):
                saved = _saved_at_of(entry)
                if saved is None:
                    continue
                if now - saved > age_limit:
                    _rmtree_silent(entry)
                    removed.append(entry.name)
                    entries.remove(entry)

        # 世代数超過の削除
        if policy.max_entries > 0 and len(entries) > policy.max_entries:
            overflow = len(entries) - policy.max_entries
            for entry in entries[:overflow]:
                _rmtree_silent(entry)
                removed.append(entry.name)
            entries = entries[overflow:]

        # サイズ超過の削除（古い方から削っていく）
        if policy.max_size_bytes > 0:
            total = sum(_dir_size(entry) for entry in entries)
            for entry in list(entries):
                if total <= policy.max_size_bytes:
                    break
                size = _dir_size(entry)
                _rmtree_silent(entry)
                removed.append(entry.name)
                total -= size
                entries.remove(entry)

        return removed


def _command_meta_to_dict(meta: ReplaceCommandMeta) -> dict[str, typing.Any]:
    """`ReplaceCommandMeta`をJSON対応の辞書へ変換する。"""
    return {
        "replace_id": meta.replace_id,
        "dry_run": meta.dry_run,
        "fixed_strings": meta.fixed_strings,
        "pattern": meta.pattern,
        "replacement": meta.replacement,
        "encoding": meta.encoding,
    }


def _record_to_dict(record: ReplaceRecord) -> dict[str, typing.Any]:
    """`ReplaceRecord`をJSON対応の辞書へ変換する。"""
    return {
        "file": pyfltr.paths.normalize_separators(str(record.file)),
        "line": record.line,
        "col": record.col,
        "before_line": record.before_line,
        "after_line": record.after_line,
        "before_text": record.before_text,
        "after_text": record.after_text,
    }


def _sanitize_file_key(file: pathlib.Path) -> str:
    """履歴保存用のサブディレクトリ名を生成する。

    パスのベース名をサニタイズし、衝突回避のためパス全体のSHA-256短縮ハッシュを連結する。
    例: `pyfltr/grep_/scanner.py` -> `scanner.py_<hash8>`
    """
    base = pyfltr.paths.sanitize_command_name(file.name)
    full = pyfltr.paths.normalize_separators(str(file))
    digest = hashlib.sha256(full.encode("utf-8")).hexdigest()[:8]
    return f"{base}_{digest}"


def _now_iso() -> str:
    """現在時刻をISO 8601（UTC、マイクロ秒付き）で返す。"""
    return datetime.datetime.now(datetime.UTC).isoformat()


def _saved_at_of(run_dir: pathlib.Path) -> datetime.datetime | None:
    """履歴ディレクトリの`meta.json`から`saved_at`をパースして返す。"""
    meta_path = run_dir / _META_FILENAME
    if not meta_path.exists():
        return None
    try:
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    raw = meta.get("saved_at")
    if not raw:
        return None
    try:
        return datetime.datetime.fromisoformat(raw)
    except ValueError:
        return None


def _dir_size(path: pathlib.Path) -> int:
    """ディレクトリ配下の合計バイト数を返す。"""
    total = 0
    for entry in path.rglob("*"):
        if entry.is_file():
            try:
                total += entry.stat().st_size
            except OSError:
                continue
    return total


def _rmtree_silent(path: pathlib.Path) -> None:
    """ディレクトリを無言で再帰削除する。"""
    shutil.rmtree(path, ignore_errors=True)
