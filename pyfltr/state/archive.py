"""実行アーカイブ。

全実行のツール出力・diagnostic・メタ情報をXDG Base Directory準拠の
ユーザーキャッシュへ保存する仕組み。v3.0.0で追加。

ディレクトリ構造（`<cache_root> = platformdirs.user_cache_dir("pyfltr")`）::

    <cache_root>/runs/<run_id>/meta.json
    <cache_root>/runs/<run_id>/tools/<sanitize(command)>/output.log
    <cache_root>/runs/<run_id>/tools/<sanitize(command)>/diagnostics.jsonl
    <cache_root>/runs/<run_id>/tools/<sanitize(command)>/tool.json

`run_id`はULID（26文字、Crockford Base32、タイムスタンプ由来で辞書順ソート可能）。
自動クリーンアップは世代数・合計サイズ・保存期間の3軸のうち超過した時点で
古い順（= run_id昇順）に削除する。

アーカイブは既定で有効。`--no-archive` CLIオプションまたは
`archive = false`設定で無効化できる。
"""

import dataclasses
import datetime
import importlib.metadata
import json
import logging
import os
import pathlib
import shutil
import sys
import typing

import platformdirs
import ulid

import pyfltr.command.core_
import pyfltr.config.config
import pyfltr.output.jsonl
import pyfltr.paths

logger = logging.getLogger(__name__)

_RUNS_DIRNAME = "runs"
_META_FILENAME = "meta.json"
_TOOL_OUTPUT_FILENAME = "output.log"
_TOOL_DIAGNOSTICS_FILENAME = "diagnostics.jsonl"
_TOOL_META_FILENAME = "tool.json"


def default_cache_root() -> pathlib.Path:
    r"""XDG 準拠のキャッシュルートを返す。

    Linuxでは`~/.cache/pyfltr/`、macOSでは`~/Library/Caches/pyfltr/`、
    Windowsでは`%LOCALAPPDATA%\pyfltr\Cache`になる。環境変数
    `PYFLTR_CACHE_DIR`が設定されていればそれを優先する（テストや運用上の
    強制上書き用）。

    プロジェクトローカル（`.pyfltr_cache/`のようなリポジトリ内ディレクトリ）
    には保存しない方針を採用する。`.gitignore`運用の負担を増やさず、
    複数プロジェクト横断での参照を可能にするため。
    """
    override = os.environ.get("PYFLTR_CACHE_DIR")
    if override:
        return pathlib.Path(override)
    return pathlib.Path(platformdirs.user_cache_dir("pyfltr"))


def generate_run_id() -> str:
    """ULID 形式の run_id を生成する。"""
    return str(ulid.ULID())


@dataclasses.dataclass(frozen=True)
class ArchivePolicy:
    """自動クリーンアップの閾値。"""

    max_runs: int
    """保存する最大世代数。"""
    max_size_bytes: int
    """アーカイブ全体の合計バイト数の上限。"""
    max_age_days: int
    """保存期間の上限 (日数)。"""


@dataclasses.dataclass(frozen=True)
class RunSummary:
    """list_runs() が返す 1 世代分の要約。"""

    run_id: str
    started_at: str | None
    finished_at: str | None
    exit_code: int | None
    commands: list[str]
    files: int | None


class ArchiveStore:
    """実行アーカイブの読み書き。

    1回のpyfltr実行で1インスタンスを生成する。`start_run()`でrun_idを
    採番して以後のツール書き込みを受け付け、`finalize_run()`でメタ情報を
    確定させる。クリーンアップは`cleanup()`が呼ばれた時点で行う
    （呼び出し側は実行冒頭で発火させることを想定）。
    """

    def __init__(self, cache_root: pathlib.Path | None = None) -> None:
        self._cache_root = cache_root if cache_root is not None else default_cache_root()
        self._runs_dir = self._cache_root / _RUNS_DIRNAME

    @property
    def runs_dir(self) -> pathlib.Path:
        """runs/ ディレクトリの絶対パス。"""
        return self._runs_dir

    def start_run(
        self,
        *,
        run_id: str | None = None,
        commands: list[str] | None = None,
        files: int | None = None,
        cwd: str | None = None,
        argv: list[str] | None = None,
    ) -> str:
        """新しい run ディレクトリを作成し、初期 meta.json を書き込んで run_id を返す。"""
        run_id = run_id or generate_run_id()
        run_dir = self._runs_dir / run_id
        (run_dir / "tools").mkdir(parents=True, exist_ok=True)
        meta = {
            "run_id": run_id,
            "version": importlib.metadata.version("pyfltr"),
            "python": sys.version,
            "executable": sys.executable,
            "platform": sys.platform,
            "cwd": cwd if cwd is not None else os.getcwd(),
            "argv": argv if argv is not None else sys.argv[1:],
            "commands": commands or [],
            "files": files,
            "started_at": _now_iso(),
        }
        (run_dir / _META_FILENAME).write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
        return run_id

    def write_tool_result(
        self,
        run_id: str,
        result: pyfltr.command.core_.CommandResult,
    ) -> None:
        """1 ツール完了時に呼び出されるフック。生出力・diagnostic・メタを保存する。

        `diagnostics.jsonl`は`(command, file)`単位の集約形式で保存する。各行は
        `{"kind": "diagnostic", "command": ..., "file": ..., "messages": [...]}`構造で
        `llm_output.aggregate_diagnostics()`の出力と同形。
        `tool.json`には`hint_urls`・`hints`をそれぞれ空でないときに限り含める。
        """
        tool_dir = self._runs_dir / run_id / "tools" / pyfltr.paths.sanitize_command_name(result.command)
        tool_dir.mkdir(parents=True, exist_ok=True)
        (tool_dir / _TOOL_OUTPUT_FILENAME).write_text(result.output, encoding="utf-8")

        aggregated, hint_urls, hints = pyfltr.output.jsonl.aggregate_diagnostics(result.errors)
        with (tool_dir / _TOOL_DIAGNOSTICS_FILENAME).open("w", encoding="utf-8") as f:
            for record in aggregated:
                f.write(json.dumps(record, ensure_ascii=False))
                f.write("\n")
        meta: dict[str, typing.Any] = {
            "command": result.command,
            "type": result.command_type,
            "status": result.status,
            "returncode": result.returncode,
            "files": result.files,
            "elapsed": round(result.elapsed, 3),
            "diagnostics": len(result.errors),
            "has_error": result.has_error,
            "commandline": result.commandline,
        }
        if hint_urls:
            meta["hint_urls"] = dict(hint_urls)
        if hints:
            meta["hints"] = dict(hints)
        if result.retry_command is not None:
            meta["retry_command"] = result.retry_command
        (tool_dir / _TOOL_META_FILENAME).write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")

    def finalize_run(
        self,
        run_id: str,
        *,
        exit_code: int,
        commands: list[str] | None = None,
        files: int | None = None,
    ) -> None:
        """実行終了時に meta.json を更新する。"""
        meta_path = self._runs_dir / run_id / _META_FILENAME
        if not meta_path.exists():
            return
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
        meta["finished_at"] = _now_iso()
        meta["exit_code"] = exit_code
        if commands is not None:
            meta["commands"] = commands
        if files is not None:
            meta["files"] = files
        meta_path.write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")

    def list_runs(self, *, limit: int | None = None) -> list[RunSummary]:
        """保存済みのrunを新しい順（run_id降順）で返す。"""
        if not self._runs_dir.exists():
            return []
        entries = sorted(
            (entry for entry in self._runs_dir.iterdir() if entry.is_dir()),
            key=lambda p: p.name,
            reverse=True,
        )
        if limit is not None:
            entries = entries[:limit]
        summaries: list[RunSummary] = []
        for entry in entries:
            meta_path = entry / _META_FILENAME
            if not meta_path.exists():
                continue
            try:
                meta = json.loads(meta_path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                logger.debug("archive: 破損した meta.json をスキップ: %s", meta_path)
                continue
            summaries.append(
                RunSummary(
                    run_id=entry.name,
                    started_at=meta.get("started_at"),
                    finished_at=meta.get("finished_at"),
                    exit_code=meta.get("exit_code"),
                    commands=list(meta.get("commands", [])),
                    files=meta.get("files"),
                )
            )
        return summaries

    def read_meta(self, run_id: str) -> dict[str, typing.Any]:
        """指定runのmeta.jsonを読み出す。存在しなければFileNotFoundError。"""
        meta_path = self._runs_dir / run_id / _META_FILENAME
        if not meta_path.exists():
            raise FileNotFoundError(run_id)
        return json.loads(meta_path.read_text(encoding="utf-8"))

    def list_tools(self, run_id: str) -> list[str]:
        """指定 run で実際にアーカイブされているツール名一覧を返す。

        `tools/`直下のディレクトリ名（`pyfltr.paths.sanitize_command_name()`済み）を自然順で返す。
        `meta["commands"]`は実行予定リストで、fail-fast中断やskippedで実体を
        伴わないツールを含みうるため、実保存ツールのSSOTとして本メソッドを使う。
        不在run_id指定時は他の`read_*`と同じく`FileNotFoundError`を送出する。
        """
        tools_dir = self._runs_dir / run_id / "tools"
        if not tools_dir.exists():
            raise FileNotFoundError(run_id)
        return sorted(entry.name for entry in tools_dir.iterdir() if entry.is_dir())

    def read_tool_meta(self, run_id: str, tool: str) -> dict[str, typing.Any]:
        """指定 run / tool のメタ情報を読み出す。"""
        path = self._runs_dir / run_id / "tools" / pyfltr.paths.sanitize_command_name(tool) / _TOOL_META_FILENAME
        if not path.exists():
            raise FileNotFoundError(f"{run_id}/{tool}")
        return json.loads(path.read_text(encoding="utf-8"))

    def read_tool_output(self, run_id: str, tool: str) -> str:
        """指定 run / tool の生出力を読み出す。"""
        path = self._runs_dir / run_id / "tools" / pyfltr.paths.sanitize_command_name(tool) / _TOOL_OUTPUT_FILENAME
        if not path.exists():
            raise FileNotFoundError(f"{run_id}/{tool}")
        return path.read_text(encoding="utf-8")

    def read_tool_diagnostics(self, run_id: str, tool: str) -> list[dict[str, typing.Any]]:
        """指定 run / tool の diagnostic 一覧を返す。"""
        path = self._runs_dir / run_id / "tools" / pyfltr.paths.sanitize_command_name(tool) / _TOOL_DIAGNOSTICS_FILENAME
        if not path.exists():
            raise FileNotFoundError(f"{run_id}/{tool}")
        entries: list[dict[str, typing.Any]] = []
        for line in path.read_text(encoding="utf-8").splitlines():
            if not line:
                continue
            entries.append(json.loads(line))
        return entries

    def cleanup(self, policy: ArchivePolicy) -> list[str]:
        """自動クリーンアップを実施する。削除された run_id のリストを返す。

        世代数 / 合計サイズ / 保存期間のいずれかを超過した時点で、古い順
        （run_id昇順 = ULIDタイムスタンプ昇順）に削除する。

        各実行冒頭で同期的に呼び出すことを想定する。アーカイブ規模は通常
        小さく削除対象も限定的のため、非同期化は実装コストに見合わない。
        将来的な非同期化の余地は残すが現状は同期実行とする。
        """
        if not self._runs_dir.exists():
            return []
        entries = sorted(
            (entry for entry in self._runs_dir.iterdir() if entry.is_dir()),
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
                started = _started_at_of(entry)
                if started is None:
                    continue
                if now - started > age_limit:
                    _rmtree_silent(entry)
                    removed.append(entry.name)
                    entries.remove(entry)

        # 世代数超過の削除
        if policy.max_runs > 0 and len(entries) > policy.max_runs:
            overflow = len(entries) - policy.max_runs
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


def policy_from_config(config: pyfltr.config.config.Config) -> ArchivePolicy:
    """pyproject.toml の設定から ArchivePolicy を組み立てる。"""
    return ArchivePolicy(
        max_runs=int(config.values.get("archive-max-runs", 100)),
        max_size_bytes=int(config.values.get("archive-max-size-mb", 1024)) * 1024 * 1024,
        max_age_days=int(config.values.get("archive-max-age-days", 30)),
    )


def _now_iso() -> str:
    """現在時刻を ISO 8601 (UTC, マイクロ秒付き) で返す。"""
    return datetime.datetime.now(datetime.UTC).isoformat()


def _started_at_of(run_dir: pathlib.Path) -> datetime.datetime | None:
    """Run ディレクトリの meta.json から started_at をパースして返す。"""
    meta_path = run_dir / _META_FILENAME
    if not meta_path.exists():
        return None
    try:
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    raw = meta.get("started_at")
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
