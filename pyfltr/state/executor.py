"""コマンド実行順の制御。"""

import collections.abc
import contextlib
import pathlib
import threading

import pyfltr.command
import pyfltr.config.config

_serial_group_locks: dict[str, threading.Lock] = {}
"""`serial_group`名をキーにした排他ロック辞書。"""

_serial_group_locks_registry_lock = threading.Lock()
"""`_serial_group_locks` 自体の生成を直列化するためのメタロック。"""


@contextlib.contextmanager
def serial_group_lock(group: str | None) -> collections.abc.Iterator[None]:
    """指定された serial_group の排他ロックを取得するコンテキストマネージャー。

    `group`がNoneのときはno-opとして振る舞い、呼び出し側は常に
    `with`文で包めるようにする。これにより、cargo系やdotnet系の
    `CommandInfo.serial_group`が設定されたコマンドは並列実行されても
    同一グループ内では1件ずつ順に走り、`target`ディレクトリなどの
    内部ロック競合を回避できる。
    """
    if group is None:
        yield
        return
    with _serial_group_locks_registry_lock:
        lock = _serial_group_locks.get(group)
        if lock is None:
            lock = threading.Lock()
            _serial_group_locks[group] = lock
    with lock:
        yield


def split_commands_for_execution(
    commands: list[str],
    config: pyfltr.config.config.Config,
    all_files: list[pathlib.Path],
    *,
    include_fix_stage: bool = False,
) -> tuple[list[str], list[str], list[str]]:
    """有効なコマンドをフェーズごとに分割。

    `(fixers, formatters, linters_and_testers)`の3段を返す。上流の実行器は
    この順でfixers → formatters → linters_and_testersの3ステージに分けて
    実行する。各ステージ内はfixers/formattersが順次、linters/testersが並列。

    linters/testersは推定実行時間の降順（LPTアルゴリズム）でソートし、
    並列実行時に重いツールが先に開始されるようにする。推定時間は
    `CommandInfo.fixed_cost + CommandInfo.per_file_cost * 対象ファイル数`で算出する。

    `include_fix_stage=True`のときは、fix-args定義済みかつ有効化済みのコマンドを
    `fixers`に積む。`commands`側で既にfix対象フィルタが効いている前提だが、
    ここでも`filter_fix_commands()`を適用して安全側に倒す。fixersに積んだ
    コマンドは通常ステージ（formatters / linters_and_testers）にも従来どおり含める
    （ruff-checkのようにfixとlintを2段階で走らせる構成を取るため）。
    """
    fixers: list[str] = []
    if include_fix_stage:
        fixers = pyfltr.config.config.filter_fix_commands(commands, config)

    formatters: list[str] = []
    linters_and_testers: list[str] = []
    for command in commands:
        if not config[command]:
            continue
        if config.commands[command].type == "formatter":
            formatters.append(command)
        else:
            linters_and_testers.append(command)

    # 推定実行時間の降順でソート（LPT: 重いツールを先に開始）
    def _estimate_time(c: str) -> float:
        info = config.commands[c]
        n = len(pyfltr.command.filter_by_globs(all_files, info.target_globs()))
        return info.fixed_cost + info.per_file_cost * n

    linters_and_testers.sort(key=_estimate_time, reverse=True)
    return fixers, formatters, linters_and_testers
