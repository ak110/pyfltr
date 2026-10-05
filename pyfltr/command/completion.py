"""実行結果の完了判定。

指定したチェックが対象へ到達して完了したかを、1回の実行につき1度だけ導出する。
MCP `run`の応答とCLI JSONLの最終`summary`は本モジュールの戻り値をそのまま返し、
出力側で判定条件を書かない。両経路の値を構造的に一致させるためである。

数値終了コードは診断・書き換え・ツール失敗を表し、完了区分は対象到達と実行完了を表す。
診断を検出して終了コードが1になった実行でも、チェック自体が対象を評価し終えていれば`completed`となる。

判定の入力は実行アーカイブではなくパイプラインが保持する最終`CommandResult`列とする。
キャッシュから復元した結果は実行アーカイブへ記録されないため、アーカイブから組み立てると
キャッシュヒットしたコマンドが完了一覧から欠ける。
"""

import dataclasses
import typing

import pyfltr.command.core_
import pyfltr.config.config

CompletionStatus = typing.Literal["completed", "incomplete", "not_reached"]


@dataclasses.dataclass(frozen=True)
class RunCompletion:
    """1回の実行の完了判定。

    MCP応答とJSONL `summary`が同じ値を返すよう、導出後に内容を変更できない型で保持する。
    出力側は配列へ変換して公開する。
    """

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
    config: pyfltr.config.config.Config,
    *,
    files_reached: int,
    requested_commands: list[str],
    unmet_commands: list[str],
    missing_targets: list[str],
    fully_excluded_files: list[str],
) -> RunCompletion:
    """実行結果から完了判定を導出する。

    `requested_commands`は`--commands`（MCPでは`commands`）でコマンド名を明示したもので、
    エイリアス経由と未指定時の全コマンドは含めない。
    対象0件またはサブプロジェクト設定の無効化でskipした結果（`CommandResult.not_applicable`）は、
    明示したコマンドなら未完了、それ以外なら完了・未完了のいずれにも数えない。
    未指定時の全コマンド実行では、対象ファイルを持たないツールのskipは期待どおりの動作であり、
    未完了として数えると対象の言語を含まない実行が常に`incomplete`となって区分が判断に使えないためである。

    `completion`は次の順に、最初に該当した条件で確定する。

    - `not_reached`: `completed_commands`が空で、開始後に時間上限へ達したコマンドも無い
    - `completed`: `files_reached`が1以上、`completed_commands`が1件以上で、
      `incomplete_commands`・`missing_targets`・`fully_excluded_files`が全て空
    - `incomplete`: 上記のいずれにも該当しない

    コマンド名の一覧は設定のコマンド定義順に並べ、重複を除く。
    """
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
    """結果が対象を評価し終えたことを表すなら真を返す。"""
    if result.timeout_exceeded or result.status in ("skipped", "resolution_failed"):
        return False
    return result.files >= 1


def _ordered(names: set[str], config: pyfltr.config.config.Config) -> tuple[str, ...]:
    """コマンド名を設定の定義順へ並べる。未登録の名前は末尾へ名前順で置く。"""
    order = {name: index for index, name in enumerate(config.command_names)}
    return tuple(sorted(names, key=lambda name: (order.get(name, len(order)), name)))
