"""コマンドのキャッシュ可否とストアの操作契約。"""

from __future__ import annotations

import pathlib
import typing

import pyfltr.command.core_
import pyfltr.config.model

# ツール固有設定ファイルの外部参照を伴うフラグ。--{command}-argsにこれらが含まれる場合は
# 対象の実行でキャッシュを無効化する（動的パスを解釈する複雑さを避けるため安全側に倒す）。
_EXTERNAL_REF_ARGS: frozenset[str] = frozenset({"--config", "--ignore-path"})


class CacheStore(typing.Protocol):
    """実行制御が必要とするキャッシュ操作。"""

    def compute_key(
        self,
        *,
        command: str,
        commandline: list[str],
        fix_stage: bool,
        structured_output: bool,
        target_files: list[pathlib.Path],
        config_files: list[pathlib.Path],
        target_base_cwd: pathlib.Path | None = None,
        subproject_cwd: pathlib.Path | None = None,
    ) -> str:
        """compute_keyのキャッシュ操作。"""
        ...

    def get(self, command: str, key: str) -> pyfltr.command.core_.CommandResult | None:
        """getのキャッシュ操作。"""
        ...

    def put(self, command: str, key: str, result: pyfltr.command.core_.CommandResult, *, run_id: str | None) -> None:
        """putのキャッシュ操作。"""
        ...


def is_cacheable(
    command: str,
    config: pyfltr.config.model.Config,
    additional_args: list[str],
) -> bool:
    """対象の実行がキャッシュ対象になるかを判定する。

    条件:
        - `CommandInfo.cacheable=True`である
        - `--{command}-args`に`--config` / `--ignore-path`など
          外部ファイル参照を伴うフラグを含まない

    `additional_args`は`--{command}-args`の値をshlex分割したリスト。

    `--config` / `--ignore-path`検知時はキャッシュの読み書きを
    まとめて無効化する。指定されたパスを動的に解釈してキャッシュキーへ
    含める実装は複雑度が高く、誤ったhash算出による誤ヒットの方が
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
    config: pyfltr.config.model.Config,
    base: pathlib.Path | None = None,
    *,
    injected_config_path: pathlib.Path | None = None,
) -> list[pathlib.Path]:
    """コマンドの設定ファイル候補のうち、プロジェクトルートに実在するものを列挙する。

    `CommandInfo.config_files`は自動読込対象を完全列挙した静的リストで、
    存在しないファイルはhash計算時に空文字扱いとなる（`_file_sha256`の挙動に準拠）。

    本関数はキャッシュキー算出専用で、`CommandInfo.config_arg_template`による
    `--config`引数注入とは別経路。注入経路は`dispatcher._resolve_config_inject_path`が
    `config_inject_candidates`を順に走査して候補1件を選び、本関数の`config_files`
    （自動読込候補の完全列挙）とは責務を分離している。
    """
    info = config.commands.get(command)
    if info is None or not info.config_files:
        return []
    root = base if base is not None else pathlib.Path.cwd()
    if injected_config_path is None:
        return [root / name for name in info.config_files]
    injected_names = set(info.config_inject_candidates)
    implicit_files = [root / name for name in info.config_files if name not in injected_names]
    return [injected_config_path, *implicit_files]
