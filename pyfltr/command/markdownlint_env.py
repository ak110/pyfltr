"""pnpxで取得するmarkdownlintの依存と配布パッチを所有する。"""

import dataclasses
import datetime as dt
import hashlib
import json
import os
import pathlib
import shutil
import stat
import subprocess
import tempfile
import time
import typing

import platformdirs
import yaml

import pyfltr.command.process

PATCH_PATH = pathlib.Path(__file__).with_name("braces-3.0.3.patch")
PATCHED_DEPENDENCY = "braces@3.0.3"
MAIN_PACKAGE = "markdownlint-cli2"
INSTALL_DEFINITION = {"patchedDependencies": {PATCHED_DEPENDENCY: PATCH_PATH.name}, "packages": []}
CACHE_RETENTION_SECONDS = 7 * 24 * 60 * 60
CACHE_MAX_BYTES = 1024 * 1024 * 1024


@dataclasses.dataclass(frozen=True)
class MarkdownlintEnvironment:
    """設定・取得定義・パッチに対応する導入先。完成した環境だけを公開する。"""

    root: pathlib.Path
    settings: dict[str, typing.Any]
    cwd: pathlib.Path

    @property
    def executable(self) -> str:
        """呼び出し元のcwdで起動する絶対パスのshimを返す。"""
        suffix = ".cmd" if os.name == "nt" else ""
        return str(self.root / "node_modules" / ".bin" / f"markdownlint-cli2{suffix}")


def _read_settings(cwd: pathlib.Path) -> dict[str, typing.Any]:
    # 通常の探索元から解決し、認証情報を含みうる値はメモリー内だけで引き渡す。
    result = subprocess.run(
        ["pnpm", "config", "list", "--json"],
        cwd=cwd,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="backslashreplace",
        timeout=30,
        check=False,
    )
    if result.returncode != 0:
        raise ValueError(f"pnpmの設定取得に失敗しました: {result.stderr}。pnpmの設定を確認して再実行してください。")
    settings = json.loads(result.stdout)
    if not isinstance(settings, dict):
        raise ValueError("pnpmの設定出力がオブジェクトではありません。pnpmの導入状態を確認して再実行してください。")
    return settings


def resolve_environment(cwd: pathlib.Path) -> MarkdownlintEnvironment:
    """pnpmの実効設定を読み、公開済み環境と導入先を同じ定義で選ぶ。

    日ごとに依存を再解決し、主パッケージの更新を取り込む。
    長寿命プロセスでも解決のたびに設定と日付を読み直す。
    設定値はディスクへ複製せず、内容のdigestだけを導入先の区別に用いる。
    """
    try:
        settings = _read_settings(cwd)
        definition = json.dumps(
            {
                "package": MAIN_PACKAGE,
                "settings": settings,
                "workspace": INSTALL_DEFINITION,
                "date": dt.datetime.now(dt.UTC).date().isoformat(),
            },
            sort_keys=True,
        ).encode()
        digest = hashlib.sha256(definition + PATCH_PATH.read_bytes()).hexdigest()
        cache = pathlib.Path(platformdirs.user_cache_dir("pyfltr", appauthor=False)) / "markdownlint"
        return MarkdownlintEnvironment(cache / digest, settings, cwd)
    except (OSError, subprocess.TimeoutExpired, ValueError) as exc:
        raise ValueError(
            f"markdownlintの取得環境を解決できません: {exc}。pnpmの導入と設定を確認して再実行してください。"
        ) from exc


def _setting_args(settings: dict[str, typing.Any], prefix: str = "") -> list[str]:
    result: list[str] = []
    for key, value in settings.items():
        name = f"{prefix}.{key}" if prefix else key
        if isinstance(value, dict):
            result.extend(_setting_args(value, name))
        elif isinstance(value, list):
            result.extend(f"--config.{name}[]={item}" for item in value)
        elif value is not None:
            shown = str(value).lower() if isinstance(value, bool) else str(value)
            result.append(f"--config.{name}={shown}")
    return result


def _patch_is_recorded(root: pathlib.Path) -> bool:
    lock = root / "pnpm-lock.yaml"
    if not lock.is_file():
        return False
    metadata = yaml.safe_load(lock.read_text(encoding="utf-8"))
    return isinstance(metadata, dict) and PATCHED_DEPENDENCY in metadata.get("patchedDependencies", {})


def _cache_directories(root: pathlib.Path) -> typing.Iterator[pathlib.Path]:
    """専用取得処理が所有する環境と導入中の領域を列挙する。"""
    for candidate in root.parent.iterdir():
        digest_name = len(candidate.name) == 64 and all(char in "0123456789abcdef" for char in candidate.name)
        if (
            (candidate == root or digest_name or candidate.name.startswith("install-"))
            and not candidate.is_symlink()
            and candidate.is_dir()
        ):
            yield candidate


def _prune_environments(root: pathlib.Path) -> None:
    cutoff = time.time() - CACHE_RETENTION_SECONDS
    for candidate in _cache_directories(root):
        if candidate != root:
            try:
                if candidate.stat().st_mtime < cutoff:
                    shutil.rmtree(candidate)
            except FileNotFoundError:
                # 別の取得処理が先に回収した場合は完了している。
                continue


def _check_cache_capacity(root: pathlib.Path) -> None:
    def raise_scan_error(error: OSError) -> typing.NoReturn:
        raise error

    total = 0
    for candidate in _cache_directories(root):
        for directory, _subdirs, files in os.walk(candidate, onerror=raise_scan_error):
            for name in files:
                try:
                    metadata = (pathlib.Path(directory) / name).lstat()
                except FileNotFoundError:
                    continue
                if stat.S_ISREG(metadata.st_mode):
                    total += metadata.st_size
                if total > CACHE_MAX_BYTES:
                    raise ValueError(
                        "markdownlintの取得環境が容量上限1 GiBを超えます。不要な取得環境を削除して再実行してください。"
                    )


def ensure_environment(environment: MarkdownlintEnvironment, *, timeout: float | None) -> None:
    """導入を終えた環境を原子的に公開し、失敗した導入は次回作り直す。

    完成後の環境は変更しない。同じ導入先を並行して構築した場合は先に完成した環境を使う。
    pnpmの通常導入がpatchedDependenciesを適用し、適用不能なら公開しない。
    """
    root = environment.root
    try:
        root.parent.mkdir(parents=True, exist_ok=True)
        if pathlib.Path(environment.executable).is_file():
            if not _patch_is_recorded(root):
                raise ValueError(f"{root}のpnpm lockfileにパッチ適用記録がありません。この環境を削除して再実行してください。")
            root.touch()
            _prune_environments(root)
            return
        _prune_environments(root)
        with tempfile.TemporaryDirectory(prefix="install-", dir=root.parent) as temporary:
            stage = pathlib.Path(temporary)
            shutil.copyfile(PATCH_PATH, stage / PATCH_PATH.name)
            manifest = {"private": True, "pnpm": {"patchedDependencies": {PATCHED_DEPENDENCY: PATCH_PATH.name}}}
            (stage / "package.json").write_text(json.dumps(manifest), encoding="utf-8")
            # pnpm 11も同じ所有元からpatch定義を読む。
            (stage / "pnpm-workspace.yaml").write_text(json.dumps(INSTALL_DEFINITION), encoding="utf-8")
            commandline = [
                "pnpm",
                *_setting_args(environment.settings),
                "--dir",
                str(stage),
                "add",
                "--config.frozen-lockfile=false",
                "--config.enableGlobalVirtualStore=false",
                "--",
                MAIN_PACKAGE,
            ]
            proc = pyfltr.command.process.run_process_loop(commandline, dict(os.environ), cwd=environment.cwd, timeout=timeout)
            if proc.returncode != 0:
                raise ValueError(f"pnpmによるmarkdownlintの導入に失敗しました: {proc.stdout}")
            relative = pathlib.Path(environment.executable).relative_to(root)
            if not (stage / relative).is_file():
                raise ValueError("pnpmの導入後にmarkdownlint-cli2の実行ファイルが見つかりません。")
            if not _patch_is_recorded(stage):
                raise ValueError("pnpmがパッチを適用しませんでした。pnpmのpatchedDependencies設定を確認してください。")
            _check_cache_capacity(root)
            try:
                stage.rename(root)
            except OSError:
                if not pathlib.Path(environment.executable).is_file() or not _patch_is_recorded(root):
                    raise
        root.touch()
        _prune_environments(root)
    except (OSError, ValueError, yaml.YAMLError) as exc:
        raise ValueError(
            f"markdownlintの修正済み環境を準備できません: {exc}。pnpmの設定と導入出力を確認して再実行してください。"
        ) from exc
