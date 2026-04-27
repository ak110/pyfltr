"""ファイル hash ベースのスキップキャッシュ。

``CommandInfo.cacheable=True`` のツール (textlint) の実行結果をユーザーキャッシュへ保存し、
同じ入力に対する再実行を省略するための仕組み。v3.0.0 パート D で追加。

ディレクトリ構造 (``<cache_root> = platformdirs.user_cache_dir("pyfltr")``)::

    <cache_root>/cache/<tool>/<hash>.json

キャッシュキーには「ツール名・実効コマンドライン全体・ステージ区別・構造化出力設定・
対象ファイル群 sha256・ツール固有設定ファイル群 sha256・pyfltr MAJOR バージョン」を含める。
CLI 引数や出力切替による誤ヒットを防ぐ一方、ツール本体のバージョンは含めない
(短期破棄前提で許容)。

対象は「ファイル間依存を持たず、設定ファイルも CWD でのみ解決する linter」に限る。
以下はいずれもキャッシュ対象外とし ``CommandInfo.cacheable=False`` のままにする。

- 書き込み型 formatter — ヒット時にファイル書き換えがスキップされ、
  ファイル状態と結果が不整合になるため
- tester — 対象ファイル以外のソース・設定・環境への依存があり、
  依存解析またはプロジェクト全体 hash が必要で実装コストに見合わない
- 依存型 linter (``mypy`` / ``ruff-check`` / ``pylint`` 等) — import 先や
  型情報のキャッシュが必要で、対象ファイル単独の hash では整合性を保てない
- 外部参照 linter (``shellcheck`` / ``actionlint``) — ``source`` 文や
  reusable workflow など外部参照を含みうるため安全側で除外
- 階層型設定を参照する linter (``ec`` / ``markdownlint`` / ``typos``) —
  階層型の設定解決は静的列挙では網羅できない

自動クリーンアップは期間軸 (``cache-max-age-hours``) のみのシンプルな方針。
サイズ・世代数の軸は採用しない (短期破棄前提でストレージ暴発リスクが小さいため)。

serialize/deserialize の辞書構造は archive.py の ``_error_to_dict`` と偶発的に
重複するが、用途 (キャッシュ復元 vs 生成物のアーカイブ) が異なるため共通化は避ける。
"""
# pylint: disable=duplicate-code

import dataclasses
import datetime
import hashlib
import importlib.metadata
import json
import logging
import pathlib
import shutil
import typing

import pyfltr.archive
import pyfltr.command
import pyfltr.config
import pyfltr.error_parser

logger = logging.getLogger(__name__)

_CACHE_DIRNAME = "cache"
# ツール固有設定ファイルの外部参照を伴うフラグ。--{command}-args にこれらが含まれる場合は
# 当該実行でキャッシュを無効化する (動的パスを解釈する複雑さを避けるため安全側に倒す)。
_EXTERNAL_REF_ARGS: frozenset[str] = frozenset({"--config", "--ignore-path"})


@dataclasses.dataclass(frozen=True)
class CachePolicy:
    """自動クリーンアップの閾値。

    ファイル hash キャッシュは短期破棄前提のため、期間軸 (時間単位) のみを持つ。
    """

    max_age_hours: int
    """保存期間の上限 (時間)。0 以下で期間軸の自動削除を無効化する。"""


class CacheStore:
    """ファイル hash キャッシュの読み書き。

    1 回の pyfltr 実行で 1 インスタンスを生成する。``get()`` でキャッシュ参照、
    ``put()`` で書き込みを行う。クリーンアップは ``cleanup()`` が呼ばれた時点で
    実行する (呼び出し側は実行冒頭で発火させることを想定)。
    """

    def __init__(self, cache_root: pathlib.Path | None = None) -> None:
        self._cache_root = cache_root if cache_root is not None else pyfltr.archive.default_cache_root()
        self._cache_dir = self._cache_root / _CACHE_DIRNAME

    @property
    def cache_dir(self) -> pathlib.Path:
        """cache/ ディレクトリの絶対パス。"""
        return self._cache_dir

    def compute_key(
        self,
        *,
        command: str,
        commandline: list[str],
        fix_stage: bool,
        structured_output: bool,
        target_files: list[pathlib.Path],
        config_files: list[pathlib.Path],
    ) -> str:
        """キャッシュキー (sha256 hex) を計算する。

        ツール名・実効コマンドライン・ステージ・構造化出力設定・対象ファイル群の内容と
        相対パス・設定ファイル群の内容・pyfltr MAJOR バージョンを連結して hash 化する。
        """
        hasher = hashlib.sha256()
        hasher.update(f"pyfltr-major={_pyfltr_major_version()}\n".encode())
        # JSONL / archive スキーマを変えた際の一括無効化用識別子。
        # archive / diagnostics の表現 (aggregated messages + hint-urls) 切替時に
        # v2 に更新した。textlint 向けの ErrorLocation.hint 追加に合わせて v3 に更新した。
        # `pyproject.toml` の版数は `hatch-vcs` 管理でMAJORを手動更新できないため、
        # 互換性を落とす際はこの定数を更新してキャッシュを一括無効化する。
        hasher.update(b"schema=v3\n")
        hasher.update(f"command={command}\n".encode())
        hasher.update(f"fix_stage={int(fix_stage)}\n".encode())
        hasher.update(f"structured={int(structured_output)}\n".encode())
        hasher.update(b"commandline:\n")
        for token in commandline:
            hasher.update(token.encode("utf-8"))
            hasher.update(b"\0")
        hasher.update(b"targets:\n")
        for target in target_files:
            hasher.update(str(target).encode("utf-8"))
            hasher.update(b"\0")
            hasher.update(_file_sha256(target).encode("ascii"))
            hasher.update(b"\n")
        hasher.update(b"configs:\n")
        for config_path in config_files:
            hasher.update(str(config_path).encode("utf-8"))
            hasher.update(b"\0")
            hasher.update(_file_sha256(config_path).encode("ascii"))
            hasher.update(b"\n")
        return hasher.hexdigest()

    def get(self, command: str, key: str) -> pyfltr.command.CommandResult | None:
        """キャッシュヒット時は CommandResult を復元して返す。ミス時は None。"""
        path = self._entry_path(command, key)
        if not path.exists():
            return None
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            logger.debug("cache: 破損したエントリをスキップ: %s", path)
            return None
        return _deserialize_result(data)

    def put(self, command: str, key: str, result: pyfltr.command.CommandResult, *, run_id: str | None) -> None:
        """CommandResult をキャッシュに書き込む。

        ``run_id`` は ``cached_from`` 復元用のソース run_id。``None`` の場合は書き込まない
        (アーカイブ無効時などにソースを特定できないケースを避けるため)。
        """
        if run_id is None:
            return
        path = self._entry_path(command, key)
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = _serialize_result(result, source_run_id=run_id)
        path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")

    def cleanup(self, policy: CachePolicy) -> list[pathlib.Path]:
        """期間超過したキャッシュエントリを削除する。削除したファイルパスのリストを返す。"""
        if not self._cache_dir.exists():
            return []
        if policy.max_age_hours <= 0:
            return []
        now = datetime.datetime.now(datetime.UTC).timestamp()
        age_limit = policy.max_age_hours * 3600
        removed: list[pathlib.Path] = []
        for entry in self._cache_dir.rglob("*.json"):
            try:
                mtime = entry.stat().st_mtime
            except OSError:
                continue
            if now - mtime > age_limit:
                try:
                    entry.unlink()
                    removed.append(entry)
                except OSError:
                    continue
        return removed

    def clear(self) -> None:
        """キャッシュを全削除する (主にテスト用)。"""
        if self._cache_dir.exists():
            shutil.rmtree(self._cache_dir, ignore_errors=True)

    def _entry_path(self, command: str, key: str) -> pathlib.Path:
        return self._cache_dir / _sanitize_tool_name(command) / f"{key}.json"


def cache_policy_from_config(config: pyfltr.config.Config) -> CachePolicy:
    """pyproject.toml の設定から CachePolicy を組み立てる。"""
    return CachePolicy(max_age_hours=int(config.values.get("cache-max-age-hours", 12)))


def is_cacheable(
    command: str,
    config: pyfltr.config.Config,
    additional_args: list[str],
) -> bool:
    """当該実行がキャッシュ対象になるかを判定する。

    条件:
        - ``CommandInfo.cacheable=True`` である
        - ``--{command}-args`` に ``--config`` ／ ``--ignore-path`` など
          外部ファイル参照を伴うフラグを含まない

    ``additional_args`` は ``--{command}-args`` の値を shlex 分割したリスト。

    ``--config`` / ``--ignore-path`` 検知時はキャッシュの読み書きを
    まとめて無効化する。指定されたパスを動的に解釈してキャッシュキーへ
    含める実装は複雑度が高く、誤った hash 算出による誤ヒットの方が
    リスクが高いため、安全側に倒して無効化のみに留める。
    """
    info = config.commands.get(command)
    if info is None or not info.cacheable:
        return False
    for arg in additional_args:
        if arg in _EXTERNAL_REF_ARGS:
            return False
        for ref in _EXTERNAL_REF_ARGS:
            if arg.startswith(f"{ref}="):
                return False
    return True


def resolve_config_files(
    command: str,
    config: pyfltr.config.Config,
    base: pathlib.Path | None = None,
) -> list[pathlib.Path]:
    """コマンドの設定ファイル候補のうち、プロジェクトルートに実在するものを列挙する。

    ``CommandInfo.config_files`` は自動読込対象を完全列挙した静的リストで、
    存在しないファイルは hash 計算時に空文字扱いとなる (``_file_sha256`` の挙動に準拠)。
    """
    info = config.commands.get(command)
    if info is None or not info.config_files:
        return []
    root = base if base is not None else pathlib.Path.cwd()
    return [root / name for name in info.config_files]


def _pyfltr_major_version() -> str:
    """Pyfltr の MAJOR バージョンを返す。開発中 (dev) の場合は ``"dev"`` を返す。"""
    try:
        version = importlib.metadata.version("pyfltr")
    except importlib.metadata.PackageNotFoundError:
        return "dev"
    return version.split(".", 1)[0]


def _file_sha256(path: pathlib.Path) -> str:
    """ファイル内容の sha256 を hex で返す。存在しないファイルは空文字を返す。"""
    try:
        with path.open("rb") as f:
            return hashlib.file_digest(f, "sha256").hexdigest()
    except OSError:
        return ""


def _sanitize_tool_name(name: str) -> str:
    """ツール名をファイルシステム安全な形式へ変換する。"""
    safe = "".join(ch if ch.isalnum() or ch in ("-", "_") else "_" for ch in name)
    return safe or "_"


def _serialize_result(result: pyfltr.command.CommandResult, *, source_run_id: str) -> dict[str, typing.Any]:
    """CommandResult をキャッシュエントリ用 dict に変換する。"""
    return {
        "source_run_id": source_run_id,
        "command": result.command,
        "command_type": result.command_type,
        "commandline": list(result.commandline),
        "returncode": result.returncode,
        "has_error": result.has_error,
        "files": result.files,
        "output": result.output,
        "elapsed": result.elapsed,
        "errors": [_error_to_dict(e) for e in result.errors],
        "target_files": [str(p) for p in result.target_files],
    }


def _deserialize_result(data: dict[str, typing.Any]) -> pyfltr.command.CommandResult:
    """キャッシュエントリ用 dict から CommandResult を復元する (cached=True を付与)。"""
    errors = [_dict_to_error(e) for e in data.get("errors", [])]
    target_files = [pathlib.Path(p) for p in data.get("target_files", [])]
    return pyfltr.command.CommandResult(
        command=data["command"],
        command_type=data["command_type"],
        commandline=list(data["commandline"]),
        returncode=data["returncode"],
        has_error=data["has_error"],
        files=data["files"],
        output=data["output"],
        elapsed=data["elapsed"],
        errors=errors,
        target_files=target_files,
        cached=True,
        cached_from=data.get("source_run_id"),
    )


def _error_to_dict(error: pyfltr.error_parser.ErrorLocation) -> dict[str, typing.Any]:
    """ErrorLocation をキャッシュ用 dict に変換する。"""
    return {
        "command": error.command,
        "file": error.file,
        "line": error.line,
        "col": error.col,
        "rule": error.rule,
        "rule_url": error.rule_url,
        "severity": error.severity,
        "fix": error.fix,
        "hint": error.hint,
        "message": error.message,
    }


def _dict_to_error(data: dict[str, typing.Any]) -> pyfltr.error_parser.ErrorLocation:
    """Dict から ErrorLocation を復元する。"""
    return pyfltr.error_parser.ErrorLocation(
        command=data["command"],
        file=data["file"],
        line=data["line"],
        col=data.get("col"),
        rule=data.get("rule"),
        rule_url=data.get("rule_url"),
        severity=data.get("severity"),
        fix=data.get("fix"),
        hint=data.get("hint"),
        message=data["message"],
    )
