"""コマンド実行順の制御。"""

import collections.abc
import contextlib
import pathlib
import threading

import pyfltr.command
import pyfltr.config

_serial_group_locks: dict[str, threading.Lock] = {}
"""serial_group 名をキーにした排他ロック辞書。"""

_serial_group_locks_registry_lock = threading.Lock()
"""``_serial_group_locks`` 自体の生成を直列化するためのメタロック。"""


@contextlib.contextmanager
def serial_group_lock(group: str | None) -> collections.abc.Iterator[None]:
    """指定された serial_group の排他ロックを取得するコンテキストマネージャー。

    ``group`` が ``None`` のときは no-op として振る舞い、呼び出し側は常に
    ``with`` 文で包めるようにする。これにより、cargo 系や dotnet 系の
    ``CommandInfo.serial_group`` が設定されたコマンドは並列実行されても
    同一グループ内では 1 件ずつ順に走り、``target`` ディレクトリなどの
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
    config: pyfltr.config.Config,
    all_files: list[pathlib.Path],
    *,
    fix_mode: bool = False,
) -> tuple[list[str], list[str]]:
    """有効なコマンドをフェーズごとに分割。

    linters/testers は推定実行時間の降順（LPT アルゴリズム）でソートし、
    並列実行時に重いツールが先に開始されるようにする。推定時間は
    ``CommandInfo.fixed_cost + CommandInfo.per_file_cost * 対象ファイル数`` で算出する。

    fix_mode=True のときは、対象コマンドを全て順次実行バケツ (formatters) に積み、
    linters/testers バケツは空にする (同一ファイルへの書き込み競合を避けるため
    並列実行を停止する)。
    """
    formatters: list[str] = []
    linters_and_testers: list[str] = []
    for command in commands:
        if not config[command]:
            continue
        if fix_mode:
            # fix モードでは全て順次実行バケツに積む
            formatters.append(command)
        elif config.commands[command].type == "formatter":
            formatters.append(command)
        else:
            linters_and_testers.append(command)

    # 推定実行時間の降順でソート（LPT: 重いツールを先に開始）
    def _estimate_time(c: str) -> float:
        info = config.commands[c]
        n = len(pyfltr.command.filter_by_globs(all_files, info.target_globs()))
        return info.fixed_cost + info.per_file_cost * n

    linters_and_testers.sort(key=_estimate_time, reverse=True)
    return formatters, linters_and_testers
