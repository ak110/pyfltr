"""実行コンテキスト型・CommandResult。"""

import dataclasses
import pathlib
import typing

import pyfltr.config.config

if typing.TYPE_CHECKING:
    import pyfltr.command.error_parser
    import pyfltr.state.cache
    import pyfltr.state.only_failed

logger = __import__("logging").getLogger(__name__)


@dataclasses.dataclass
class ExecutionBaseContext:
    """実行パイプライン全体で不変のコンテキスト。

    `run_pipeline` が1回だけ組み立て、CLI/TUI各経路へ渡す。
    """

    config: pyfltr.config.config.Config
    """実行設定（pyproject.tomlから読み込んだ設定値）。"""
    all_files: "list[pathlib.Path]"
    """対象ファイル一覧（ディレクトリ走査・excludeフィルタリング済み）。"""
    cache_store: "pyfltr.state.cache.CacheStore | None"
    """ファイルhashキャッシュストア。`None` の場合はキャッシュ無効。"""
    cache_run_id: str | None
    """キャッシュ書き込み時の参照元run_id。`None` の場合はキャッシュ書き込みをスキップ。"""


@dataclasses.dataclass
class ExecutionContext:
    """コマンド実行ごとに変動するコンテキスト。

    `ExecutionBaseContext` を包みつつ、各コマンド実行直前に組み立てる。
    CLI経路では `_run_one_command` が、TUI経路では `UIApp._execute_command` が組み立てる。
    """

    base: ExecutionBaseContext
    """パイプライン全体で不変のコンテキスト。"""
    fix_stage: bool = False
    """fixステージとして実行するか（fix-argsを適用して単発fix経路で動作する）。"""
    only_failed_targets: "pyfltr.state.only_failed.ToolTargets | None" = None
    """`--only-failed` 経路でのツール別失敗ファイル集合。`None` の場合は `all_files` を使用。"""
    on_output: "typing.Callable[[str], None] | None" = None
    """サブプロセス出力の逐次コールバック。TUI経路でリアルタイム表示に使用。"""
    is_interrupted: "typing.Callable[[], bool] | None" = None
    """中断指示の確認コールバック。TUI協調停止経路で使用。"""
    on_subprocess_start: "typing.Callable[[], None] | None" = None
    """サブプロセス起動直後のフック。TUI経路で実行中コマンド集合を追跡するのに使用。"""
    on_subprocess_end: "typing.Callable[[], None] | None" = None
    """サブプロセス終了直前のフック。`on_subprocess_start` と対になる。"""

    @property
    def config(self) -> pyfltr.config.config.Config:
        """`base.config` への委譲。"""
        return self.base.config

    @property
    def all_files(self) -> "list[pathlib.Path]":
        """`base.all_files` への委譲。"""
        return self.base.all_files

    @property
    def cache_store(self) -> "pyfltr.state.cache.CacheStore | None":
        """`base.cache_store` への委譲。"""
        return self.base.cache_store

    @property
    def cache_run_id(self) -> "str | None":
        """`base.cache_run_id` への委譲。"""
        return self.base.cache_run_id


@dataclasses.dataclass
class CommandResult:
    """コマンドの実行結果。"""

    command: str
    command_type: str
    commandline: list[str]
    returncode: int | None
    has_error: bool
    files: int
    output: str
    elapsed: float
    errors: "list[pyfltr.command.error_parser.ErrorLocation]" = dataclasses.field(default_factory=list)
    target_files: list[pathlib.Path] = dataclasses.field(default_factory=list)
    """当該ツールに渡したターゲットファイル一覧 （retry_commandの位置引数復元に使用）。

    `pass-filenames=False` のツールでは `commandline` にファイルが含まれないため、
    retry_commandでターゲットを差し替えるには実行時点のリストを別途保持する必要がある。
    """
    archived: bool = False
    """実行アーカイブへの書き込みに成功したか。

    `True` のときに限り、JSONL側でsmart truncationによるメッセージ/diagnostic省略を
    適用できる （切り詰め分はアーカイブから復元可能）。`--no-archive` やアーカイブ初期化
    失敗時は `False` のままとなり、切り詰めをスキップして全文をJSONLに出力する。
    """
    retry_command: str | None = None
    """当該ツール1件を再実行するためのshellコマンド文字列 （toolレコード用）。

    `run_pipeline` がツール完了時に埋める。未設定 （`None`） のときはtoolレコードから
    省略する （テスト等、パイプライン外でCommandResultを生成する場合）。
    """
    cached: bool = False
    """ファイルhashキャッシュから復元された結果か否か。

    `True` のとき、当該ツールは実際には実行されておらず、過去の実行結果を復元して
    返されている。`--no-cache` またはキャッシュ未ヒットの場合は `False`。
    """
    cached_from: str | None = None
    """キャッシュヒット時の復元元run_id （ULID）。

    `cached=True` のときに限り設定される。JSONL toolレコードで参照誘導用に出力する
    （`show-run` / MCPの詳細参照経路で当該runの全文を確認できる）。
    """
    fixed_files: list[str] = dataclasses.field(default_factory=list)
    """fixステージ・formatterステージで実際にファイル内容が変化した対象のパス一覧。

    内容ハッシュ比較により変化を検知して設定する。
    内容変化が検知されなかった場合は空。
    `summary.applied_fixes` の集計に使用する。
    """
    resolution_failed: bool = False
    """ツール起動コマンドの解決に失敗したか。

    bin-runner / js-runnerからの解決失敗時に `True` を設定する。
    `status` プロパティは通常の実行失敗（`failed`）より優先して
    `resolution_failed` を返し、CIログ等で「対象0件で失敗したのか／
    対象はあったが解決に失敗したのか」を区別可能にする。
    """
    effective_runner: str | None = None
    """`pyfltr.command.runner.ResolvedCommandline.effective_runner` の値。

    `build_commandline` が成功してツール起動経路が確定した場合のみセットされる。
    `direct` / `mise` / `uv` / `uvx` / `pnpx` / `pnpm` / `npm` / `npx` / `yarn` のいずれかの値を取り、
    JSONL commandレコードの`effective_runner`フィールドへ露出する。
    `resolution_failed` 時や対象0件でcommandline解決を行わない経路では `None` のまま。
    """
    runner_source: str | None = None
    """`pyfltr.command.runner.ResolvedCommandline.runner_source` の値。

    `explicit` / `default` / `path-override` のいずれかで、`{command}-runner` の決定経緯を示す。
    `effective_runner` と同じくJSONL commandレコードの `runner_source` フィールドへ露出する。
    """
    timeout_exceeded: bool = False
    """`{command}-timeout` で指定された壁時計上限を超過してsubprocessが停止されたか。

    `True` のとき `status` は `failed` を取り、JSONL `command.hints` に `status.timeout`
    相当の英文注記を1件付与する。利用者・LLMがハング由来の失敗と通常のlint failureを
    区別できるようにするためのフラグ。
    """
    severity: str = "error"
    """当該ツールの失敗時の扱い。`{command}-severity` 設定値を結果生成時に転記する。

    - `"error"`（既定）: 通常失敗を `status="failed"` で扱う（従来挙動）
    - `"warning"`: 通常失敗を `status="warning"` に格下げし、パイプラインのexit codeに影響させない

    `status` プロパティはconfigを保持しないため、解決済み値を本フィールドへ保持する設計とする。
    `resolution_failed` / `timeout_exceeded` はツール自体の異常を示すため severity の影響を受けない。
    """

    @classmethod
    # from_run は各コマンド実行モジュール（linter_fix / textlint_fix 等）で同様の引数転送パターンを持つため重複検知される
    def from_run(  # pylint: disable=duplicate-code
        cls,
        *,
        command: str,
        command_info: "pyfltr.config.config.CommandInfo | None" = None,
        commandline: list[str],
        returncode: int | None,
        output: str,
        elapsed: float,
        files: int,
        has_error: bool = False,
        errors: "list[pyfltr.command.error_parser.ErrorLocation] | None" = None,
        command_type: str | None = None,
        resolution_failed: bool = False,
        timeout_exceeded: bool = False,
    ) -> "CommandResult":
        """実行結果からCommandResultを組み立てるファクトリメソッド。

        `command_type` を省略した場合は `command_info.type` を使う。
        `command_type` と `command_info` の両方を省略することはできない。
        `errors` を省略した場合は空リストを使う（parse_errorsの呼び出しは呼び出し側で行う）。
        `timeout_exceeded=True` を指定すると当該結果がtimeout由来の失敗であることを示す。
        """
        resolved_type: str
        if command_type is None:
            assert command_info is not None, "command_type と command_info のどちらかを指定する必要がある"
            resolved_type = command_info.type
        else:
            resolved_type = command_type
        return cls(
            command=command,
            command_type=resolved_type,
            commandline=commandline,
            returncode=returncode,
            has_error=has_error,
            files=files,
            output=output,
            elapsed=elapsed,
            errors=errors if errors is not None else [],
            resolution_failed=resolution_failed,
            timeout_exceeded=timeout_exceeded,
        )

    @property
    def alerted(self) -> bool:
        """skipped/succeeded以外ならTrue"""
        return self.returncode is not None and self.returncode != 0

    @property
    def status(self) -> str:
        """ステータスの文字列を返す。

        `timeout_exceeded=True`の場合、`command_type`がformatterであっても`failed`を返す。
        timeoutで強制停止された結果を成功扱い（`formatted`）にしてしまうと、
        利用者・LLMがハング由来の停止と通常のformatter書き換えを区別できなくなるため。

        `severity="warning"` 設定下では通常失敗を `"warning"` に格下げする。
        ツール起動自体に失敗したケース（`resolution_failed` / `timeout_exceeded`）は
        ツール起動自体の異常で警告扱いに馴染まないため、`severity` の影響を受けない。

        `status`は`resolution_failed`・`returncode`・`has_error`・`command_type`・
        `timeout_exceeded`・`severity`から導出する計算プロパティとして実装する。
        新規status値を追加する際は本判定分岐に組み込み、個別箇所でのstatus文字列
        直接組み立ては避ける。
        """
        if self.resolution_failed:
            return "resolution_failed"
        if self.returncode is None:
            status = "skipped"
        elif self.returncode == 0:
            status = "succeeded"
        elif self.timeout_exceeded:
            status = "failed"
        elif self.command_type == "formatter" and not self.has_error:
            status = "formatted"
        elif self.severity == "warning":
            status = "warning"
        else:
            status = "failed"
        return status

    def get_status_text(self) -> str:
        """成型した文字列を返す。

        `formatted`の場合は末尾に「再実行不要」の補足を付与する。
        formatterによる書き換えはそれ自体が成功扱いであり、再実行を要しないことを
        利用者に明示するため。`summary.guidance`（パイプライン全体）と
        `command.hints`（個別ツール）と並ぶtext出力側の経路。
        """
        base = f"{self.status} ({self.files}files in {self.elapsed:.1f}s)"
        if self.status == "formatted":
            base += "; no rerun needed"
        return base


@dataclasses.dataclass
class CacheContext:
    """キャッシュ参照用のコンテキスト。

    `execute_command` のplain経路でのみ使う内部ヘルパー。
    """

    cache_store: "pyfltr.state.cache.CacheStore"
    command: str
    key: str

    def lookup(self) -> CommandResult | None:
        """キャッシュを参照する。ヒットならCommandResult、ミスならNone。"""
        return self.cache_store.get(self.command, self.key)

    def store(self, result: CommandResult, *, run_id: str | None) -> None:
        """キャッシュへ書き込む （ソースrun_id付き）。"""
        self.cache_store.put(self.command, self.key, result, run_id=run_id)


@dataclasses.dataclass(frozen=True)
class ExecutionParams:
    """`execute_command` の共通前処理結果。

    ターゲット解決・コマンドライン構築を済ませた中間状態を保持し、
    dispatcherと各runner関数で参照する。
    """

    command_info: pyfltr.config.config.CommandInfo
    targets: list[pathlib.Path]
    commandline_prefix: list[str]
    commandline: list[str]
    additional_args: list[str]
    fix_mode: bool
    fix_args: list[str] | None
    via_mise: bool = False
    """このコマンドが `mise exec` 経由で起動されるか。

    `ensure_mise_available` 通過後の `ResolvedCommandline` から判定する
    （mise不在時にdirectへフォールバックされたケースを除外するため、
    `build_commandline` 直後の値ではなく事後の値で判断する）。
    `build_subprocess_env` でのmise toolパス除外適用判断に使う。
    詳細はCLAUDE.md「subprocess起動時のPATH整理方針」節を参照。
    """
    effective_runner: str | None = None
    """`ResolvedCommandline.effective_runner` の事後値。

    `ensure_mise_available` 通過後の値を採用する（mise不在時のdirectフォールバックを反映するため）。
    `_with_targets` 経由で `CommandResult` へ転記し、JSONL commandレコードへ露出する。
    対象0件などでcommandline解決を行わない経路では `None`。
    """
    runner_source: str | None = None
    """`ResolvedCommandline.runner_source` の値。`explicit` / `default` / `path-override` のいずれか。"""
