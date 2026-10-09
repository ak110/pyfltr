"""実行アーカイブ。

全実行のツール出力・diagnostic・メタ情報をXDG Base Directory準拠の
ユーザーキャッシュへ保存する仕組み。v3.0.0で追加。

`FileNotFoundError`の契約: 本モジュールが送出する`FileNotFoundError`の引数は
識別子（`run_id` または `f"{run_id}/{tool}"`）のみとし、利用者向けメッセージ文面は
catch側（`pyfltr/state/runs.py`・`pyfltr/cli/mcp_server.py`など）で組み立てる。

ディレクトリ構造（`<cache_root> = platformdirs.user_cache_dir("pyfltr", appauthor=False)`）::

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
import importlib.metadata
import json
import logging
import os
import pathlib
import posixpath
import re
import sys
import typing

import platformdirs
import ulid

import pyfltr.command.core_
import pyfltr.config.config
import pyfltr.config.model
import pyfltr.output.jsonl
import pyfltr.parsing.entry
import pyfltr.paths
import pyfltr.state.retention

logger = logging.getLogger(__name__)

_RUNS_DIRNAME = "runs"
_META_FILENAME = "meta.json"
_TOOL_OUTPUT_FILENAME = "output.log"
_TOOL_DIAGNOSTICS_FILENAME = "diagnostics.jsonl"
_TOOL_META_FILENAME = "tool.json"
TOOL_META_REMOVED_KEYS: frozenset[str] = frozenset({"has_error"})
"""保存済みのツールメタ情報から読み取り時に除去するフィールド。"""
SUBPROJECT_RESTORE_COMMANDS: frozenset[str] = frozenset({"pytest"})
"""保存済み診断のサブプロジェクト所属を生出力から復元して読むコマンド。"""
DIAGNOSTIC_PATHS_META_KEY = "diagnostic_paths"
"""診断の`file`を起点相対で保存した版が`meta.json`へ書くキー。値は`DIAGNOSTIC_PATHS_START_RELATIVE`。

このキーを持たないrunは、サブプロジェクト相対の診断を保存していた版の記録として補正の対象にする。
"""
DIAGNOSTIC_PATHS_START_RELATIVE = "start-relative"
_SECTION_HEADER_RE = re.compile(r"^# (?:subproject: (?P<relative>.+)|external paths)$")


def default_cache_root() -> pathlib.Path:
    r"""XDG 準拠のキャッシュルートを返す。

    Linuxでは`~/.cache/pyfltr/`、macOSでは`~/Library/Caches/pyfltr/`、
    Windowsでは`%LOCALAPPDATA%\pyfltr\Cache`になる。環境変数
    `PYFLTR_CACHE_DIR`が設定されていればそれを優先する（テストや運用上の
    強制上書き用）。

    `appauthor=False`を渡すのは、未指定時にWindowsで`appname`が
    appauthorとしても付与され`%LOCALAPPDATA%\pyfltr\pyfltr\Cache`に
    なる挙動を回避するため。

    プロジェクトローカル（`.pyfltr_cache/`のようなリポジトリ内ディレクトリ）
    には保存しない方針を採用する。`.gitignore`運用の負担を増やさず、
    複数プロジェクト横断での参照を可能にするため。
    """
    override = os.environ.get("PYFLTR_CACHE_DIR")
    if override:
        return pathlib.Path(override)
    return pathlib.Path(platformdirs.user_cache_dir("pyfltr", appauthor=False))


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
            "started_at": pyfltr.state.retention.now_iso(),
            DIAGNOSTIC_PATHS_META_KEY: DIAGNOSTIC_PATHS_START_RELATIVE,
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
        `output.jsonl.aggregate_diagnostics()`の出力と同形。
        `tool.json`には`hint_urls`・`hints`・`slow_tests`をそれぞれ空でないときに限り含める。
        `slow_tests`は`CommandResult`への設定時点で正規化済みのため、ここでは件数を変えない。
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
            "commandline": result.commandline,
        }
        if hint_urls:
            meta["hint_urls"] = dict(hint_urls)
        if hints:
            meta["hints"] = dict(hints)
        if result.slow_tests:
            meta["slow_tests"] = [test.to_dict() for test in result.slow_tests]
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
        meta["finished_at"] = pyfltr.state.retention.now_iso()
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
        """指定runのmeta.jsonを読み取る。存在しなければFileNotFoundError。"""
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
        """指定run / toolのメタ情報から撤去済みフィールドだけを除いて返す。"""
        path = self._runs_dir / run_id / "tools" / pyfltr.paths.sanitize_command_name(tool) / _TOOL_META_FILENAME
        if not path.exists():
            raise FileNotFoundError(f"{run_id}/{tool}")
        meta = json.loads(path.read_text(encoding="utf-8"))
        return {key: value for key, value in meta.items() if key not in TOOL_META_REMOVED_KEYS}

    def read_tool_output(self, run_id: str, tool: str) -> str:
        """指定 run / tool の生出力を読み取る。"""
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
        if tool in SUBPROJECT_RESTORE_COMMANDS and self._saved_subproject_relative_diagnostics(run_id):
            try:
                output = self.read_tool_output(run_id, tool)
            except FileNotFoundError:
                return entries
            entries = restore_subproject_paths(tool, output, entries)
        return entries

    def _saved_subproject_relative_diagnostics(self, run_id: str) -> bool:
        """診断を起点相対へ揃える前の版が保存したrunかを判定する。

        新しい版のrunへ補正を適用すると、起点直下の診断を同じ位置・メッセージを持つ子の診断と取り違えるため、
        `meta.json`の`DIAGNOSTIC_PATHS_META_KEY`を持たないrunだけを対象にする。
        """
        try:
            meta = self.read_meta(run_id)
        except (FileNotFoundError, ValueError):
            return False
        return meta.get(DIAGNOSTIC_PATHS_META_KEY) != DIAGNOSTIC_PATHS_START_RELATIVE

    def cleanup(self, policy: ArchivePolicy) -> list[str]:
        """自動クリーンアップを実施する。削除された run_id のリストを返す。

        世代数 / 合計サイズ / 保存期間のいずれかを超過した時点で、古い順
        （run_id昇順 = ULIDタイムスタンプ昇順）に削除する。実装本体は
        `pyfltr.state.retention.cleanup_generational_directory`へ集約する
        （`ReplaceHistoryStore.cleanup`と同一アルゴリズムを共有するため）。

        各実行冒頭で同期的に呼び出すことを想定する。アーカイブ規模は通常
        小さく削除対象も限定的のため、非同期化は実装コストに見合わない。
        将来的な非同期化の余地は残すが現状は同期実行とする。
        """
        retention_policy = pyfltr.state.retention.RetentionPolicy(
            max_entries=policy.max_runs,
            max_size_bytes=policy.max_size_bytes,
            max_age_days=policy.max_age_days,
        )
        return pyfltr.state.retention.cleanup_generational_directory(
            self._runs_dir,
            retention_policy,
            meta_filename=_META_FILENAME,
            timestamp_key="started_at",
        )


def restore_subproject_paths(
    command: str,
    output: str,
    entries: list[dict[str, typing.Any]],
) -> list[dict[str, typing.Any]]:
    """サブプロジェクト相対のまま保存された診断を起点相対へ補正する。

    診断パスを解析時点で起点相対へ揃える前の版は、サブプロジェクトのcwdで実行したツールの
    相対パス診断をそのまま保存していた。保存済み生出力の`# subproject: <相対>`区間ごとに
    同じ解析を再実行し、`file`・行・メッセージが一致する区間が1件だけの診断を`<相対>/<file>`へ補正する。
    一致区間が0件または複数件の診断は変えない。
    呼び出し側（`ArchiveStore.read_tool_diagnostics`）は、起点相対で保存した新しい版のrunへ本関数を適用しない。
    元の作業ツリーの存在を要求せず、保存ファイルは書き換えない。
    """
    owners: dict[tuple[str, typing.Any, typing.Any], set[str]] = {}
    for relative, body in _split_subproject_sections(output):
        for error in pyfltr.parsing.entry.parse_errors(command, body):
            owners.setdefault((error.file, error.line, error.message), set()).add(relative)
    if not owners:
        return entries
    restored: list[dict[str, typing.Any]] = []
    for entry in entries:
        file = entry.get("file")
        messages = entry.get("messages")
        if not isinstance(file, str) or not isinstance(messages, list) or not _is_plain_relative(file):
            restored.append(entry)
            continue
        groups: dict[str | None, list[typing.Any]] = {}
        for message in messages:
            relatives = (
                owners.get((file, message.get("line"), message.get("msg")), set()) if isinstance(message, dict) else set()
            )
            owner = next(iter(relatives)) if len(relatives) == 1 else None
            groups.setdefault(owner, []).append(message)
        if list(groups) == [None]:
            restored.append(entry)
            continue
        for owner, owned_messages in groups.items():
            target = file if owner is None else posixpath.normpath(posixpath.join(owner, file))
            restored.append({**entry, "file": target, "messages": owned_messages})
    return restored


def _split_subproject_sections(output: str) -> list[tuple[str, str]]:
    """生出力をサブプロジェクト区間へ分け、`(相対パス, 区間本文)`の一覧を返す。

    区切り行は`pyfltr.command.subproject_loop`が挿入する。外部パス区間と最初の区切り行より前は対象外とする。
    """
    sections: list[tuple[str, str]] = []
    relative: str | None = None
    lines: list[str] = []
    for line in [*output.splitlines(), "# external paths"]:
        match = _SECTION_HEADER_RE.match(line)
        if match is None:
            lines.append(line)
            continue
        if relative is not None:
            sections.append((relative, "\n".join(lines)))
        relative = match.group("relative")
        lines = []
    return sections


def _is_plain_relative(path: str) -> bool:
    """起点外を指さない相対パスかを判定する。"""
    pure = pathlib.PurePosixPath(path)
    return bool(path) and not pure.is_absolute() and ".." not in pure.parts and not path.startswith("<")


def policy_from_config(config: pyfltr.config.model.Config) -> ArchivePolicy:
    """pyproject.toml の設定から ArchivePolicy を組み立てる。"""
    return ArchivePolicy(
        max_runs=int(config.values.get("archive-max-runs", 100)),
        max_size_bytes=int(config.values.get("archive-max-size-mb", 1024)) * 1024 * 1024,
        max_age_days=int(config.values.get("archive-max-age-days", 30)),
    )
