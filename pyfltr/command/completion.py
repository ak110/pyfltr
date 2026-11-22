import dataclasses
import typing

import pyfltr.command.core_
import pyfltr.config.config
import pyfltr.config.model
import pyfltr.tools

CompletionStatus = typing.Literal["completed", "incomplete", "not_reached"]


@dataclasses.dataclass(frozen=True)
class RunCompletion:
    completion: CompletionStatus
    """完了区分。導出条件は`evaluate_completion`のdocstringを参照する。"""
    files_reached: int
    """対象解決後に実行文脈へ残ったファイル数。実行文脈を組み立てる前に打ち切った場合は0。"""
    completed_commands: tuple[str, ...]
    """1件以上の対象を評価し、skip・ツール解決失敗・時間上限超過ではない最終結果を返したコマンド名。"""
    incomplete_commands: tuple[str, ...]
    """対象のチェックを完了しなかったコマンド名。

    skip・ツール解決失敗・時間上限超過の結果に加え、
    `--commands`で指定したが設定で無効化されて実行しなかったコマンドを含む。
    対象0件のskipは、`--commands`でコマンド名を明示した場合だけ含める。
    """
    missing_targets: tuple[str, ...]
    """直接指定したが存在しなかった対象。"""
    fully_excluded_files: tuple[str, ...]
    """直接指定したが除外設定・`.gitignore`で全て対象外になった対象。"""


def evaluate_completion(
    results: list[pyfltr.command.core_.CommandResult],
    config: pyfltr.config.model.Config,
    *,
    files_reached: int,
    requested_commands: list[str],
    unmet_commands: list[str],
    missing_targets: list[str],
    fully_excluded_files: list[str],
) -> RunCompletion:
    completed: set[str] = set()
    incomplete: set[str] = set(unmet_commands)
    for result in results:
        if _is_completed(result):
            completed.add(result.command)
        elif not result.not_applicable or result.command in requested_commands:
            incomplete.add(result.command)
    completed_commands = _ordered(completed, config)
    incomplete_commands = _ordered(incomplete, config)

    completion: CompletionStatus
    if not completed_commands and not any(result.timeout_exceeded for result in results):
        completion = "not_reached"
    elif (
        files_reached >= 1
        and completed_commands
        and not incomplete_commands
        and not missing_targets
        and not fully_excluded_files
    ):
        completion = "completed"
    else:
        completion = "incomplete"
    return RunCompletion(
        completion=completion,
        files_reached=files_reached,
        completed_commands=completed_commands,
        incomplete_commands=incomplete_commands,
        missing_targets=tuple(missing_targets),
        fully_excluded_files=tuple(fully_excluded_files),
    )


def _is_completed(result: pyfltr.command.core_.CommandResult) -> bool:
    if result.timeout_exceeded or result.skipped or result.resolution_failed:
        return False
    return result.files >= 1


def _ordered(names: set[str], config: pyfltr.config.model.Config) -> tuple[str, ...]:
    return tuple(sorted(names, key=lambda name: (pyfltr.tools.command_index(config.command_names, name), name)))
