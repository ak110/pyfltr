"""replace履歴の世代管理。

`pyfltr/state/archive.py`と同じ世代管理パターン（ULID採番・XDG準拠キャッシュ・
3軸自動クリーンアップ）を踏襲する。保存内容は「変更前バイト列・変更後ハッシュ・
各置換箇所の前後行」の3点で、変更後全文を別途保存しない。

`FileNotFoundError`の契約: 本モジュールが送出する`FileNotFoundError`の引数は
`replace_id`のみとし、利用者向けメッセージ文面はcatch側
（`pyfltr/cli/replace_subcmd.py`・`pyfltr/cli/mcp_server.py`など）で組み立てる。

ディレクトリ構造（`<cache_root> = pyfltr.state.archive.default_cache_root()`）::

    <cache_root>/replaces/<replace_id>/meta.json
    <cache_root>/replaces/<replace_id>/files/<sanitized_path>/before.bin
    <cache_root>/replaces/<replace_id>/files/<sanitized_path>/changes.json

`<sanitized_path>`はファイルパス由来の安定識別子で、衝突回避のため
「サニタイズ済みベース名 + パス全体のSHA-256短縮ハッシュ」の組み合わせで構築する。
利用者が`changes.json`等を参照する場面で大半はベース名で識別でき、衝突時もハッシュで一意化される。
"""

import dataclasses
import hashlib
import json
import logging
import pathlib
import typing

import ulid

import pyfltr.config.config
import pyfltr.config.model
import pyfltr.grep_.transaction
import pyfltr.paths
import pyfltr.state.archive
import pyfltr.state.retention
import pyfltr.warnings_
from pyfltr.grep_.types import ReplaceCommandMeta, ReplaceRecord

logger = logging.getLogger(__name__)

_REPLACES_DIRNAME = "replaces"
_META_FILENAME = "meta.json"
_FILES_DIRNAME = "files"
_BEFORE_FILENAME = "before.txt"
_BEFORE_BYTES_FILENAME = "before.bin"
_CHANGES_FILENAME = "changes.json"
_LEGACY_WARNING = "旧形式の履歴には元の改行情報がないため、取り消し後のバイト列は置換前と完全に一致しない場合があります。"


class ReplaceFailure(OSError):
    """履歴保存または対象更新の失敗を、両公開入口へ同じ文面で届ける。"""

    def __init__(self, message: str, *, replace_id: str | None = None) -> None:
        super().__init__(message)
        self.replace_id = replace_id

    def describe(self, *, undo: str) -> str:
        """履歴を使える場合に復旧操作を添える。"""
        if self.replace_id is None:
            return str(self)
        return f"{self} 変更前内容の履歴は保持しています。対象の編集内容を確認し、必要なら {undo} で復元してください。"


def format_replace_id_not_found(replace_id: str, *, list_history: str) -> str:
    """replace_idが見つからない場合の案内文を返す。

    CLIとMCPで同じ文面を使い、履歴一覧を確認する操作名（`list_history`）だけを呼び出し側が渡す。
    """
    return f"replace_id が見つかりません: {replace_id}。{list_history} で有効な replace_id を確認できます。"


def format_undo_skipped(count: int, *, force: str) -> str:
    """手動編集によりundoをスキップした場合の案内文を返す。

    CLIとMCPで同じ文面を使い、強制復元の指定方法（`force`）だけを呼び出し側が渡す。
    """
    return (
        f"undo で {count} 件のファイルが手動編集後の状態のためスキップされました。"
        f"手動編集を破棄してよい場合は {force} で強制復元できます。"
    )


def format_history_unreadable(replace_id: str, history_dir: pathlib.Path, exc: Exception) -> str:
    """履歴の読み込み・デコードに失敗した場合の案内文を返す。"""
    return (
        f"履歴を読み込めません: {replace_id}: {exc}。"
        f"履歴ディレクトリ {history_dir} の権限と内容を確認してください。"
        "読み込めない状態が続く場合、この履歴は取り消しに使えないため、対象ファイルは手動で戻してください。"
    )


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


def policy_from_config(config: pyfltr.config.model.Config) -> ReplaceHistoryPolicy:
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
        - `before_bytes` (`bytes`): 新形式の変更前バイト列
        - `after_bytes` (`bytes`): 新形式の実書き込みバイト列
        - `before_content` / `after_hash`: 旧形式の履歴を構築する場合の値
        - `records` (`list[ReplaceRecord]`): 各置換箇所のレコード

        `meta.json`には実行コマンドメタとファイル一覧（相対パス・after_hash）を保存する。
        新形式の元バイト列は`files/<sanitized_path>/before.bin`へ保存し、
        `changes.json`へ`ReplaceRecord`相当のJSON配列を保存する。
        """
        run_dir = self._history_root / replace_id
        run_dir.mkdir(parents=True, exist_ok=True)
        files_dir = run_dir / _FILES_DIRNAME
        files_dir.mkdir(parents=True, exist_ok=True)
        files_meta: list[dict[str, typing.Any]] = []
        for change in file_changes:
            file_path = pathlib.Path(change["file"])
            new_format = "before_bytes" in change
            if new_format:
                after_bytes: bytes = change["after_bytes"]
                after_hash = hashlib.sha256(after_bytes).hexdigest()
            else:
                after_hash = change["after_hash"]
            records: list[ReplaceRecord] = change.get("records", [])
            sanitized = _sanitize_file_key(file_path)
            file_dir = files_dir / sanitized
            file_dir.mkdir(parents=True, exist_ok=True)
            if new_format:
                (file_dir / _BEFORE_BYTES_FILENAME).write_bytes(change["before_bytes"])
            else:
                (file_dir / _BEFORE_FILENAME).write_text(change["before_content"], encoding="utf-8")
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
            "saved_at": pyfltr.state.retention.now_iso(),
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
            before_bytes_path = file_dir / _BEFORE_BYTES_FILENAME
            if before_bytes_path.exists():
                entry["before_content"] = before_bytes_path.read_bytes().decode(meta["command"]["encoding"])
            else:
                entry["before_content"] = (file_dir / _BEFORE_FILENAME).read_text(encoding="utf-8")
            changes_raw = (file_dir / _CHANGES_FILENAME).read_text(encoding="utf-8")
            entry["records"] = json.loads(changes_raw)
        return meta

    def apply_replace(
        self,
        replace_id: str,
        *,
        command_meta: ReplaceCommandMeta,
        file_changes: list[dict[str, typing.Any]],
        policy: ReplaceHistoryPolicy,
    ) -> None:
        """変更前内容を全件保存してから書き込み、途中失敗時には開始前へ戻す。"""
        try:
            self.save_replace(replace_id, command_meta=command_meta, file_changes=file_changes)
        except OSError as exc:
            raise ReplaceFailure(
                f"置換履歴を保存できないため、対象ファイルは変更していません。原因: {exc}。"
                f"保存先 {self.history_root} の権限と空き容量を確認して再実行してください。"
            ) from exc
        updates = [
            pyfltr.grep_.transaction.FileUpdate(pathlib.Path(change["file"]), change["before_bytes"], change["after_bytes"])
            for change in file_changes
        ]
        try:
            pyfltr.grep_.transaction.write_updates(updates)
        except pyfltr.grep_.transaction.WriteFailure as exc:
            raise ReplaceFailure(exc.describe(), replace_id=replace_id) from exc
        try:
            self.cleanup(policy)
        except OSError as exc:
            pyfltr.warnings_.emit_warning(
                source="replace-history",
                message=f"置換と履歴保存は完了しましたが、古い履歴を削除できませんでした: {exc}。",
                hint=f"保存先 {self.history_root} の権限を確認してください。置換の再実行は不要です。",
            )

    def undo_replace(
        self,
        replace_id: str,
        *,
        force: bool = False,
    ) -> tuple[list[pathlib.Path], list[pathlib.Path], list[str]]:
        """履歴を読み込んでファイルを変更前内容へ復元する。

        まず全ファイルについて保存済み`after_hash`と現在ファイルのハッシュを照合する。
        force未指定で不一致が1件でも検出された場合は警告として全件をスキップ扱いとし、
        書き戻しを行わずに復元0件・スキップ全件を返す。
        force指定時、または全件一致時のみ実際の書き戻しを実施する。

        Returns:
            `(restored_files, skipped_files, warnings)`のタプル。force未指定で不一致が
            含まれる場合は`restored`は空、`skipped`は対象全件となる
            （手動編集を巻き戻す事故を避ける一括中断の方針）
        """
        meta = self.load_replace(replace_id)
        entries = list(meta.get("files", []))
        encoding = str(meta["command"]["encoding"])
        files_dir = self._history_root / replace_id / _FILES_DIRNAME
        legacy = any(not (files_dir / entry["sanitized"] / _BEFORE_BYTES_FILENAME).exists() for entry in entries)
        warnings = [_LEGACY_WARNING] if legacy else []

        # 不一致検出パス: force未指定時は1件でも不一致があれば全件スキップへ倒す
        if not force:
            mismatched: list[pathlib.Path] = []
            for entry in entries:
                file_path = pathlib.Path(entry["file"])
                saved_after_hash: str = entry["after_hash"]
                current_hash: str | None = None
                if file_path.exists():
                    before_bytes_path = files_dir / entry["sanitized"] / _BEFORE_BYTES_FILENAME
                    if before_bytes_path.exists():
                        current_hash = hashlib.sha256(file_path.read_bytes()).hexdigest()
                    else:
                        current_hash = hashlib.sha256(file_path.read_text(encoding=encoding).encode("utf-8")).hexdigest()
                if current_hash != saved_after_hash:
                    mismatched.append(file_path)
            if mismatched:
                # 計画方針（grep-replace.md）に従い、不一致時は中断して全件スキップ扱いとする
                return [], [pathlib.Path(entry["file"]) for entry in entries], warnings

        # 全復元元と開始前状態の読み込み後に書き込む。途中失敗後も通常のundoを再試行できる。
        updates: list[pyfltr.grep_.transaction.FileUpdate] = []
        for entry in entries:
            file_path = pathlib.Path(entry["file"])
            before_bytes_path = files_dir / entry["sanitized"] / _BEFORE_BYTES_FILENAME
            restored_bytes = (
                before_bytes_path.read_bytes() if before_bytes_path.exists() else entry["before_content"].encode(encoding)
            )
            current = file_path.read_bytes() if file_path.exists() else None
            updates.append(pyfltr.grep_.transaction.FileUpdate(file_path, current, restored_bytes))
        try:
            pyfltr.grep_.transaction.write_updates(updates)
        except pyfltr.grep_.transaction.WriteFailure as exc:
            raise ReplaceFailure(exc.describe(), replace_id=replace_id) from exc
        return [update.file for update in updates], [], warnings

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
        （`replace_id`昇順 = ULIDタイムスタンプ昇順）に削除する。実装本体は
        `pyfltr.state.retention.cleanup_generational_directory`へ集約する
        （`ArchiveStore.cleanup`と同一アルゴリズムを共有するため）。
        """
        retention_policy = pyfltr.state.retention.RetentionPolicy(
            max_entries=policy.max_entries,
            max_size_bytes=policy.max_size_bytes,
            max_age_days=policy.max_age_days,
        )
        return pyfltr.state.retention.cleanup_generational_directory(
            self._history_root,
            retention_policy,
            meta_filename=_META_FILENAME,
            timestamp_key="saved_at",
        )


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
