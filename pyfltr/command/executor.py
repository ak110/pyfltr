"""コマンド実行順の制御。"""

import collections.abc
import contextlib
import pathlib
import threading

import pyfltr.command.core_
import pyfltr.command.targets
import pyfltr.config.config
import pyfltr.config.model
import pyfltr.config.selection

_serial_group_locks: dict[str, threading.Lock] = {}
"""`serial_group`名をキーにした排他ロック辞書。"""

_serial_group_locks_registry_lock = threading.Lock()
"""`_serial_group_locks` 自体の生成を直列化するためのメタロック。"""


@contextlib.contextmanager
def serial_group_lock(group: str | None) -> collections.abc.Generator[None, None, None]:
    """指定された serial_group の排他ロックを取得するコンテキストマネージャー。

    `group`がNoneのときはno-opとして振る舞い、呼び出し側は常に
    `with`文で包めるようにする。これにより、cargo系やdotnet系の
    `CommandInfo.serial_group`が設定されたコマンドは並列実行されても
    同一グループ内では1件ずつ順に実行され、`target`ディレクトリなどの
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
    config: pyfltr.config.model.Config,
    all_files: list[pathlib.Path],
    *,
    include_fix_stage: bool = False,
    subproject_configs: dict[pathlib.Path, pyfltr.config.model.Config] | None = None,
) -> tuple[list[str], list[str], list[str]]:
    """有効なコマンドをフェーズごとに分割。

    `(fixers, formatters, linters_and_testers)`の3段を返す。上流の実行器は
    この順でfixers → formatters → linters_and_testersの3ステージに分けて
    実行する。各ステージ内はfixers/formattersが順次、linters/testersが並列。

    linters/testersは推定実行時間の降順（LPTアルゴリズム）でソートし、
    並列実行時に重いツールが先に開始されるようにする。推定時間は
    `CommandInfo.fixed_cost + CommandInfo.per_file_cost * 対象ファイル数`で算出する。

    `include_fix_stage=True`のときは、fix-args定義済みかつ有効化済みのコマンドを
    `fixers`に追加する。`commands`側で既にfix対象フィルタが適用されている前提だが、
    ここでも`filter_fix_commands()`を適用して安全側に倒す。fixersに追加した
    コマンドは通常ステージ（formatters / linters_and_testers）にも従来どおり含める
    （ruff-checkのようにfixとlintを2段階で実行する構成を取るため）。

    `subproject_configs`を渡すと、有効判定を起点と各サブプロジェクトの和集合で行う
    （モノレポで親OFF・子ONのツールを子で実行するため）。未指定時は起点設定のみで判定する。
    """
    fixers: list[str] = []
    if include_fix_stage:
        fixers = pyfltr.config.selection.filter_fix_commands(commands, config, subproject_configs)

    formatters: list[str] = []
    linters_and_testers: list[str] = []
    for command in commands:
        if not pyfltr.config.selection.is_command_enabled_anywhere(command, config, subproject_configs):
            continue
        if config.commands[command].type == "formatter":
            formatters.append(command)
        else:
            linters_and_testers.append(command)

    # 推定実行時間の降順でソート（LPT: 重いツールを先に開始）
    def _estimate_time(c: str) -> float:
        info = config.commands[c]
        n = len(pyfltr.command.targets.filter_by_globs(all_files, info.target_globs()))
        return info.fixed_cost + info.per_file_cost * n

    linters_and_testers.sort(key=_estimate_time, reverse=True)
    return fixers, formatters, linters_and_testers


def has_no_candidate_targets(command: str, base_ctx: pyfltr.command.core_.ExecutionBaseContext) -> bool:
    """対象ファイルが1件も存在し得ず、外部ツールを起動しないことが確定するコマンドかを返す。

    `pyfltr.command.stage_runner.run_stages`は、真を返したコマンドを検証の実行枠の取得前に実行する。
    実行枠の外で外部ツールを起動しないよう、判定は「0件が確定する」側だけを扱う。
    実行対象の母集合（`all_files`・`external_files`・サブプロジェクト別のファイルと、pytest用実行基盤のファイル）に、
    起点と全サブプロジェクトの設定の対象glob（pytestはfast・always用のglobを含む）と一致するファイルが無い場合に限り真とする。
    dispatcherの対象決定（ツール別の除外・外部パスの除外・`--only-failed`）はこの母集合から除くだけで増やさないため、
    それらの分岐を複製せず、除いた結果で0件になるコマンドは従来どおり実行枠の内側で判定させる。
    """
    contexts = [base_ctx]
    if command == "pytest" and base_ctx.pytest_base is not None:
        contexts.append(base_ctx.pytest_base)
    files: list[pathlib.Path] = []
    for context in contexts:
        files.extend(context.all_files)
        files.extend(context.external_files)
        for sub_files in context.subproject_files.values():
            files.extend(sub_files)
    if not files:
        return True
    globs: list[str] = []
    for config in [base_ctx.config, *base_ctx.subproject_configs.values()]:
        info = config.commands.get(command)
        if info is not None:
            globs.extend(info.target_globs())
        if command == "pytest":
            globs.extend(pyfltr.config.selection.pytest_fast_target_globs(config.values))
            globs.extend(pyfltr.config.selection.pytest_always_target_globs(config.values))
    unique_files = list(dict.fromkeys(files))
    return not pyfltr.command.targets.filter_by_globs(unique_files, list(dict.fromkeys(globs)))
