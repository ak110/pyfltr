"""pnpxで取得するmarkdownlintの依存と配布パッチを所有する。"""

import dataclasses
import datetime as dt
import functools
import hashlib
import json
import os
import pathlib
import shutil
import subprocess
import tempfile
import typing

import platformdirs
import yaml

import pyfltr.command.process

PATCH_PATH = pathlib.Path(__file__).with_name("braces-3.0.3.patch")
PATCHED_DEPENDENCY = "braces@3.0.3"
MAIN_PACKAGE = "markdownlint-cli2"
INSTALL_DEFINITION = {"patchedDependencies": {PATCHED_DEPENDENCY: PATCH_PATH.name}, "packages": []}


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


@functools.lru_cache(maxsize=1)
def resolve_environment(cwd: pathlib.Path) -> MarkdownlintEnvironment:
    """pnpmの実効設定を読み、公開済み環境と導入先を同じ定義で選ぶ。

    日ごとに依存を再解決し、主パッケージの更新を取り込む。
    設定値はディスクへ複製せず、内容のdigestだけを導入先の区別に用いる。
    """
    try:
        settings = _read_settings(cwd)
        definition = json.dumps(
            {
                "package": MAIN_PACKAGE,
                "settings": settings,
                "workspace": INSTALL_DEFINITION,
                "cwd": str(cwd),
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


def ensure_environment(environment: MarkdownlintEnvironment, *, timeout: float) -> None:
    """導入を終えた環境を原子的に公開し、失敗した導入は次回作り直す。

    完成後の環境は変更しない。同じ導入先を並行して構築した場合は先に完成した環境を使う。
    pnpmの通常導入がpatchedDependenciesを適用し、適用不能なら公開しない。
    """
    root = environment.root
    try:
        if pathlib.Path(environment.executable).is_file():
            if not _patch_is_recorded(root):
                raise ValueError(f"{root}のpnpm lockfileにパッチ適用記録がありません。この環境を削除して再実行してください。")
            return
        root.parent.mkdir(parents=True, exist_ok=True)
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
            try:
                stage.rename(root)
            except OSError:
                if not pathlib.Path(environment.executable).is_file() or not _patch_is_recorded(root):
                    raise
    except (OSError, ValueError, yaml.YAMLError) as exc:
        raise ValueError(
            f"markdownlintの修正済み環境を準備できません: {exc}。pnpmの設定と導入出力を確認して再実行してください。"
        ) from exc
