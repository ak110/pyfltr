import contextlib
import dataclasses
import pathlib
import tempfile
import typing

import pyfltr.command.slow_tests
import pyfltr.config.config
import pyfltr.config.model
import pyfltr.diagnostics
import pyfltr.parsing.entry
import pyfltr.tool_types
import pyfltr.tools

FAILED_STATUSES: frozenset[str] = frozenset({"failed", "resolution_failed"})
RERUN_STATUSES: frozenset[str] = frozenset({"failed", "warning", "resolution_failed"})


def is_failed_status(status: str | None) -> bool:
    return status in FAILED_STATUSES


if typing.TYPE_CHECKING:
    import pyfltr.command.cache_policy
    import pyfltr.command.only_failed
    import pyfltr.command.process
    import pyfltr.command.subprojects

    Subproject = pyfltr.command.subprojects.Subproject

logger = __import__("logging").getLogger(__name__)


@dataclasses.dataclass
class ExecutionBaseContext:
    config: pyfltr.config.model.Config
    """実行設定（pyproject.tomlから読み込んだ設定値）。"""
    all_files: "list[pathlib.Path]"
    """対象ファイル一覧（ディレクトリ走査・excludeフィルタリング済み）。"""
    cache_store: "pyfltr.command.cache_policy.CacheStore | None"
    """ファイルhashキャッシュストア。`None` の場合はキャッシュ無効。"""
    cache_run_id: str | None
    """キャッシュ書き込み時の参照元run_id。`None` の場合はキャッシュ書き込みをスキップ。"""
    start_cwd: pathlib.Path = dataclasses.field(default_factory=pathlib.Path.cwd)
    """モノレポ探索とパス正規化の起点 cwd（絶対パス）。

    `expand_all_files` ・ `_filter_by_gitignore` ・ `discover_subprojects` 等の
    cwd 起点処理に明示引数として渡す。`run_pipeline` が起動時の cwd（`--work-dir` 適用後）
    を1度だけ設定し、サブプロジェクトループで `os.chdir()` を行わずに cwd 起点処理を維持する。
    """
    subprojects: "list[Subproject]" = dataclasses.field(default_factory=list)
    """検出済みサブプロジェクトの一覧。

    要素0件はモノレポモード無効を意味する（単一マーカー検出または検出0件）。
    マーカーは`pyproject.toml`・`Cargo.toml`・`*.csproj`・`*.sln`を指す。
    要素1件以上はモノレポモード有効。`subproject_aware=True` のツールはこの一覧を
    元にサブプロジェクト別ループ実行する。
    `Subproject` 型は循環import回避のためTYPE_CHECKING節で定義する。
    """
    subproject_files: "dict[pathlib.Path, list[pathlib.Path]]" = dataclasses.field(default_factory=dict)
    """サブプロジェクト cwd（絶対パス）からそのサブプロジェクトに属するファイル一覧（`start_cwd` 相対）への辞書。

    `pyfltr.command.subprojects.classify_files_by_subproject` で算出する。
    起点cwdの実体パスのキーは起点cwdの区分であり、起点cwdがサブプロジェクトでない場合も
    どのサブプロジェクトにも属さない起点cwd内のファイルを保持する。
    サブプロジェクト未検出時は空辞書。
    """
    external_files: "list[pathlib.Path]" = dataclasses.field(default_factory=list)
    """モノレポモードで`classify_files_by_subproject`が外部パス（起点cwd配下にない絶対パス）と
    分類したファイル一覧。所属の無い起点cwd内のファイルは含まず、`subproject_files`の起点cwdの区分へ入る。

    `subproject_loop.run_subproject_loop` で注入対象ツールおよび素通し対象ツールへ
    起点cwdでの追加実行を行うために参照する。
    モノレポモード非適用時（`subprojects` が空）は空リスト。
    """
    subproject_configs: "dict[pathlib.Path, pyfltr.config.model.Config]" = dataclasses.field(default_factory=dict)
    """サブプロジェクト cwd（絶対パス）からそのサブプロジェクト配下の `Config` への辞書。

    各サブプロジェクトで `load_config(config_dir=cwd, for_subproject=True)` を解決し、
    起点と同一のCLIオーバーライドを再適用した結果を `run_pipeline` が事前構築して格納する。
    `subproject_aware=True` のツール起動時に対象のサブプロジェクトの設定（ツールのON/OFF・除外・
    targets 等）を参照し、親子でON/OFFが異なる両方向を尊重する。
    """
    pytest_base: "ExecutionBaseContext | None" = None
    """pytestだけへ使う実行基盤。`None` の場合はpytestも本コンテキストで実行する。

    fast選択時に`pytest-fast-targets`を持つ設定と、`pytest-always-targets`を持つ設定（起点または
    各サブプロジェクト）について、位置引数・差分指定に依らずプロジェクト全域から選んだファイルを
    `all_files`・`subproject_files`へ反映したもの。
    `run_pipeline`が構築し、`dispatcher.execute_command`がpytestの実行時に差し替える。
    """
    pytest_impact_root_config: "pyfltr.config.model.Config | None" = None
    """サブプロジェクトの影響選択で全域走査に使う起点の除外設定。"""
    pytest_fast_targets_active: bool = False
    """本コンテキストがfast選択時のpytest用実行基盤であるか。

    真のとき、`pytest-fast-targets`が非空の設定ではpytestの対象globをその値で置き換える。
    """
    pytest_always_targets_active: bool = False
    """本コンテキストが`pytest-always-targets`を反映したpytest用実行基盤であるか。

    真のとき、`pytest-always-targets`が非空の設定ではpytestの対象へ一致ファイルを和集合として加える。
    """
    exclude_always_targets: bool = False
    """実行が`--no-always-targets`指定で、`pytest-always-targets`の常時追加を行わないか。"""
    fail_fast: bool = False
    """実行が`--fail-fast`指定で、失敗したツールの時点で後続の段を打ち切るか。

    偽のとき、fix段で実行したツールは同じ実行の通常の段でも必ず実行される。
    textlintのfix段はこれを前提に、通常の段と重複する検査を省く（`pyfltr.command.textlint_fix`）。
    """
    _temporary_directory_stack: contextlib.ExitStack | None = dataclasses.field(default=None, init=False, repr=False)
    _temporary_directory_path: pathlib.Path | None = dataclasses.field(default=None, init=False, repr=False)
    """一時的に生成する検査用ファイルのライフタイムを保持する。"""

    def ensure_temporary_directory(self) -> pathlib.Path:
        if self._temporary_directory_path is None:
            stack = contextlib.ExitStack()
            self._temporary_directory_path = pathlib.Path(stack.enter_context(tempfile.TemporaryDirectory(prefix="pyfltr-")))
            self._temporary_directory_stack = stack
        return self._temporary_directory_path

    def cleanup(self) -> None:
        if self._temporary_directory_stack is not None:
            self._temporary_directory_stack.close()
            self._temporary_directory_stack = None
            self._temporary_directory_path = None


@dataclasses.dataclass
class ExecutionContext:
    base: ExecutionBaseContext
    """パイプライン全体で不変のコンテキスト。"""
    fix_stage: bool = False
    """fixステージとして実行するか（fix-argsを適用して単発fix経路で動作する）。"""
    only_failed_targets: "pyfltr.command.only_failed.ToolTargets | None" = None
    """`--only-failed` 経路でのツール別実行対象。

    実対象は `ToolTargets.resolve_files()` が決める。`None` の場合は `all_files` を使用。
    """
    on_output: "typing.Callable[[str], None] | None" = None
    """サブプロセス出力の逐次コールバック。TUI経路でリアルタイム表示に使用。"""
    is_interrupted: "typing.Callable[[], bool] | None" = None
    """中断指示の確認コールバック。TUI協調停止経路で使用。"""
    on_subprocess_start: "typing.Callable[[], None] | None" = None
    """サブプロセス起動直後のフック。TUI経路で実行中コマンド集合を追跡するのに使用。"""
    on_subprocess_end: "typing.Callable[[], None] | None" = None
    """サブプロセス終了直前のフック。`on_subprocess_start` と対になる。"""
    subproject_cwd: pathlib.Path | None = None
    """サブプロジェクト分割実行時の対象のサブプロジェクト cwd（絶対パス）。

    `subproject_aware=True` のツールでサブプロジェクトループ内から実行されるとき、
    対応するサブプロジェクトの cwd を保持する。`None` の場合は起点 cwd で実行する
    （単一プロジェクトまたは `subproject_aware=False` のツール）。
    対象パスの相対化とキャッシュのサブプロジェクト識別に使う。
    外部コマンドの実行cwdは`effective_cwd`で一意に導出する。
    """

    @property
    def effective_cwd(self) -> pathlib.Path:
        return self.subproject_cwd if self.subproject_cwd is not None else self.base.start_cwd

    @property
    def uv_workspace_root(self) -> pathlib.Path | None:
        if self.subproject_cwd is None:
            return None
        for subproject in self.base.subprojects:
            if subproject.cwd == self.subproject_cwd:
                return subproject.uv_workspace_root
        return None

    @property
    def config(self) -> pyfltr.config.model.Config:
        return self.base.config

    @property
    def all_files(self) -> "list[pathlib.Path]":
        if self.subproject_cwd is not None:
            return self.base.subproject_files.get(self.subproject_cwd, [])
        return self.base.all_files

    @property
    def cache_store(self) -> "pyfltr.command.cache_policy.CacheStore | None":
        return self.base.cache_store

    @property
    def cache_run_id(self) -> "str | None":
        return self.base.cache_run_id


@dataclasses.dataclass
class CommandResult:
    command: str
    command_type: str
    commandline: list[str]
    returncode: int | None
    files: int
    output: str
    elapsed: float
    formatter_failed: bool = False
    """formatter型の非ゼロ終了が書き換えではなく失敗であることを示す。

    linter・testerでは`status`が終了コードから失敗を導出するため設定しない。
    """
    errors: "list[pyfltr.diagnostics.ErrorLocation]" = dataclasses.field(default_factory=list)
    target_files: list[pathlib.Path] = dataclasses.field(default_factory=list)
    """対象のツールに渡したターゲットファイル一覧 （retry_commandの位置引数復元に使用）。

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
    """対象のツール1件を再実行するためのshellコマンド文字列 （toolレコード用）。

    `run_pipeline` がツール完了時に埋める。未設定 （`None`） のときはtoolレコードから
    省略する （テスト等、パイプライン外でCommandResultを生成する場合）。
    """
    cached: bool = False
    """ファイルhashキャッシュから復元された結果か否か。

    `True` のとき、対象のツールは実際には実行されておらず、過去の実行結果を復元して
    返されている。`--no-cache` またはキャッシュ未ヒットの場合は `False`。
    """
    cached_from: str | None = None
    """キャッシュヒット時の復元元run_id （ULID）。

    `cached=True` のときに限り設定される。JSONL toolレコードで参照誘導用に出力する
    （`show-run` / MCPの詳細参照経路で対象のrunの全文を確認できる）。
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
    runner_fallback: str | None = None
    """`pyfltr.command.runner.ResolvedCommandline.runner_fallback` の値。

    期待していた非direct経路がdirectへ退行した場合のみ退行経路ラベル
    （例: `"uv->direct"` / `"uvx->direct"` / `"mise->direct"`）が入る。
    通常経路では `None`。JSONL commandレコードでは値ありの場合のみ
    `effective_runner` / `runner_source` / `runner_fallback` の3点をまとめて出力する判定キー。
    """
    not_applicable: bool = False
    """この実行に対象が無いため起動しなかったか。

    対象ファイルが0件だった場合と、サブプロジェクト設定で無効化されていた場合に`True`を設定する。
    `status`は`skipped`のまま変えない。`--fail-fast`・中断によるskipや、
    対象があるのにツールを起動できなかったskipでは`False`のまま残し、完了判定
    （`pyfltr.command.completion`）が両者を区別する。
    """
    timeout_exceeded: bool = False
    """`{command}-timeout` で指定された壁時計上限を超過してsubprocessが停止されたか。

    `True` のとき `status` は `failed` を取り、JSONL `command.hints` に `status.timeout`
    相当の英文注記を1件付与する。利用者・LLMがハング由来の失敗と通常のlint failureを
    区別できるようにするためのフラグ。
    """
    severity: str = "error"
    """対象のツールの失敗時の扱い。`{command}-severity` 設定値を結果生成時に転記する。

    - `"error"`（既定）: 通常失敗を `status="failed"` で扱う（従来挙動）
    - `"warning"`: 通常失敗を `status="warning"` に格下げし、パイプラインのexit codeに影響させない

    `status` プロパティはconfigを保持しないため、解決済み値を本フィールドへ保持する設計とする。
    `resolution_failed` / `timeout_exceeded` はツール自体の異常を示すため severity の影響を受けない。
    """
    retry_count: int = 0
    """OOM起因で再試行した回数。0はリトライなしを意味する。"""
    slow_tests: "list[pyfltr.command.slow_tests.SlowTest]" = dataclasses.field(default_factory=list)
    """テスターが報告した遅いテスト一覧（秒数降順、最大`SLOW_TEST_LIMIT`件）。

    抽出時点とサブプロジェクトのマージ時点で件数を制限するため、下流の各経路は追加の件数制限をしない。
    露出先は実行アーカイブの`tool.json`、JSONL commandレコード、MCPの`commands[]`、
    text成功時表示、TUI表示である。
    設定する経路はpytest（`dispatcher`）とvitest（`command/vitest.py`）で、
    他のツールでは常に空。
    """
    started_at: str | None = None
    """ツール本体の実行を始めた時刻（UTCのISO 8601）。

    `pyfltr.command.stage_runner.run_stages`が実行の直前に設定する。
    スキップ結果、キャッシュから復元した結果、`run_stages`の外で生成した結果では`None`のまま残す。
    実行アーカイブの`tool.json`へ保存し、実行枠の待機区間と同じ時間軸で本体区間を対応付けるために使う。
    """
    finished_at: str | None = None
    """ツール本体の実行を終えた時刻（UTCのISO 8601）。設定する経路は`started_at`と同じ。"""
    launch_boundary_at: str | None = None
    """テスターのプロセスを起動する前に記録した開始境界（UTCのISO 8601）。

    `pyfltr.command.stage_runner.run_stages`が、同じ呼び出しで起動したfix段・formatter段の最終終了時刻を含む
    1秒の区間が終わるまで待ってから記録する（整形でツールを起動していなければ待たない）。
    更新時刻が秒単位のファイルでも、更新時刻の上限（秒+0.999999999）がこの値より前なら、
    そのファイルはテスターの起動前に最後の更新を終えている。
    種別がtesterで、ツールのプロセスの起動を試みた結果（`stage_runner.ran_tool`が真）だけに設定し、
    成否は`status`で判定する。実行ファイルが見つからず起動できなかった場合も、既存の契約どおり
    終了コード127の`failed`として起動を試みた結果に含まれるため設定する（成功の証拠にはならない）。
    中断では、fail-fastの打ち切りのように起動後に終了させた結果が`failed`として残る経路では設定され、
    TUIのCtrl+Cのように結果を`make_skipped_result`のスキップへ差し替える経路では設定されない。
    どちらも成功の証拠にはならない。スキップ・起動コマンドの解決失敗・キャッシュ復元・テスター以外と、
    本項目を記録しない版が保存したrunでは`None`（`tool.json`では項目なし）となる。
    区間幅の1秒は更新時刻を秒単位で保持するファイルシステムを前提とし、2秒単位のFATなどは対象外とする。
    """

    @classmethod
    def from_run(
        cls,
        *,
        command: str,
        command_info: "pyfltr.tool_types.CommandInfo | None" = None,
        commandline: list[str],
        returncode: int | None,
        output: str,
        elapsed: float,
        files: int,
        formatter_failed: bool = False,
        errors: "list[pyfltr.diagnostics.ErrorLocation] | None" = None,
        command_type: str | None = None,
        resolution_failed: bool = False,
        timeout_exceeded: bool = False,
        retry_count: int = 0,
        slow_tests: "list[pyfltr.command.slow_tests.SlowTest] | None" = None,
    ) -> "CommandResult":
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
            formatter_failed=formatter_failed,
            files=files,
            output=output,
            elapsed=elapsed,
            errors=errors if errors is not None else [],
            resolution_failed=resolution_failed,
            timeout_exceeded=timeout_exceeded,
            retry_count=retry_count,
            slow_tests=slow_tests if slow_tests is not None else [],
        )

    @classmethod
    def from_process(
        cls,
        *,
        process: "pyfltr.command.process.CompletedProcessWithTimeoutInfo",
        command: str,
        command_info: "pyfltr.tool_types.CommandInfo",
        commandline: list[str],
        output: str,
        elapsed: float,
        files: int,
        formatter_failed: bool = False,
        errors: "list[pyfltr.diagnostics.ErrorLocation] | None" = None,
        slow_tests: "list[pyfltr.command.slow_tests.SlowTest] | None" = None,
    ) -> "CommandResult":
        return cls.from_run(
            command=command,
            command_info=command_info,
            commandline=commandline,
            returncode=process.returncode,
            output=output,
            elapsed=elapsed,
            files=files,
            formatter_failed=formatter_failed,
            errors=errors,
            timeout_exceeded=process.timeout_exceeded,
            retry_count=process.retry_count,
            slow_tests=slow_tests,
        )

    @property
    def alerted(self) -> bool:
        return self.returncode is not None and self.returncode != 0

    @property
    def status(self) -> str:
        """ステータスの文字列を返す。

        `status` 値の語彙はJSONL `command.status` のSSOTとして本プロパティ・本docstringに集約する。
        新規status値を追加する際は本判定分岐に組み込み、個別箇所でのstatus文字列
        直接組み立ては避ける。値ごとの意味は次の通り。

        - `succeeded`: 通常成功（`returncode == 0`）
        - `formatted`: formatterがファイルを書き換えた成功（再実行不要）
        - `skipped`: 対象ファイル0件等で起動しなかった
        - `failed`: 通常の失敗。`severity` が既定値 `"error"` のとき採用する
        - `warning`: per-tool `{command}-severity = "warning"` 設定またはツール固有の一時障害分類による失敗格下げ。
          パイプライン全体exit codeに影響しない。`commands_summary.needs_action.warning` へ集計し、
          `summary.guidance` のfailure系文言は出力しない
        - `resolution_failed`: ツール起動コマンドの解決に失敗した
        - `running`: heartbeat由来の実行中レコード（pipeline側で発行）

        `timeout_exceeded=True` の場合、`command_type` がformatterであっても `failed` を返す。
        timeoutで強制停止された結果を成功扱い（`formatted`）にしてしまうと、
        利用者・LLMがハング由来の停止と通常のformatter書き換えを区別できなくなるため。

        ツール起動自体に失敗したケース（`resolution_failed` / `timeout_exceeded`）は
        ツール起動自体の異常で警告扱いに馴染まないため、`severity` の影響を受けない。

        `status` は `resolution_failed`・`returncode`・`formatter_failed`・`command_type`・
        `timeout_exceeded`・`severity` から導出する計算プロパティとして実装する。
        """
        if self.resolution_failed:
            return "resolution_failed"
        if self.returncode is None:
            status = "skipped"
        elif self.returncode == 0:
            status = "succeeded"
        elif self.timeout_exceeded:
            status = "failed"
        elif self.command_type == "formatter" and not self.formatter_failed:
            status = "formatted"
        elif self.severity == "warning":
            status = "warning"
        else:
            status = "failed"
        return status

    @property
    def failed(self) -> bool:
        return is_failed_status(self.status)

    @property
    def formatted(self) -> bool:
        return self.status == "formatted"

    @property
    def skipped(self) -> bool:
        return self.status == "skipped"

    @property
    def needs_rerun(self) -> bool:
        return self.status in RERUN_STATUSES

    @classmethod
    def merge(cls, results: "list[CommandResult]") -> "CommandResult":
        if not results:
            raise ValueError("merge には1件以上の CommandResult が必要")
        if len(results) == 1:
            return results[0]

        head = results[0]
        status_weight: dict[str, int] = {
            "skipped": 0,
            "succeeded": 1,
            "formatted": 2,
            "warning": 3,
            "resolution_failed": 4,
            "failed": 5,
        }
        worst_idx = max(range(len(results)), key=lambda i: status_weight.get(results[i].status, 0))
        worst = results[worst_idx]

        outputs: list[str] = [r.output for r in results if r.output]
        merged_output = "\n".join(outputs)

        merged_errors: list[pyfltr.diagnostics.ErrorLocation] = []
        for r in results:
            merged_errors.extend(r.errors)

        merged_target_files: list[pathlib.Path] = []
        seen: set[pathlib.Path] = set()
        for r in results:
            for t in r.target_files:
                if t not in seen:
                    seen.add(t)
                    merged_target_files.append(t)

        merged_fixed_files: list[str] = []
        seen_fixed: set[str] = set()
        for r in results:
            for fp in r.fixed_files:
                if fp not in seen_fixed:
                    seen_fixed.add(fp)
                    merged_fixed_files.append(fp)

        representative = next((r for r in results if r.returncode is not None), head)
        first_nonzero: int | None = None
        for r in results:
            if r.returncode is not None and r.returncode != 0:
                first_nonzero = r.returncode
                break
        merged_returncode = first_nonzero if first_nonzero is not None else representative.returncode

        total_elapsed = sum(r.elapsed for r in results)
        total_files = sum(r.files for r in results)
        total_retry_count = sum(r.retry_count for r in results)
        merged_slow_tests: list[pyfltr.command.slow_tests.SlowTest] = []
        for result in results:
            merged_slow_tests.extend(result.slow_tests)
        any_formatter_failed = any(r.formatter_failed for r in results)
        any_timeout = any(r.timeout_exceeded for r in results)
        any_resolution_failed = any(r.resolution_failed for r in results)
        any_archived = any(r.archived for r in results)

        return cls(
            command=head.command,
            command_type=worst.command_type,
            commandline=representative.commandline,
            returncode=merged_returncode,
            formatter_failed=any_formatter_failed,
            files=total_files,
            output=merged_output,
            elapsed=total_elapsed,
            errors=merged_errors,
            target_files=merged_target_files,
            archived=any_archived,
            retry_command=head.retry_command,
            cached=all(r.cached for r in results),
            cached_from=head.cached_from,
            fixed_files=merged_fixed_files,
            resolution_failed=any_resolution_failed,
            effective_runner=representative.effective_runner,
            runner_source=representative.runner_source,
            runner_fallback=representative.runner_fallback,
            not_applicable=all(r.not_applicable for r in results),
            timeout_exceeded=any_timeout,
            severity=worst.severity,
            retry_count=total_retry_count,
            slow_tests=pyfltr.command.slow_tests.take_slowest(merged_slow_tests),
        )

    def get_status_text(self) -> str:
        base = f"{self.status} ({self.files}files in {self.elapsed:.1f}s)"
        if self.status == "formatted":
            base += "; no rerun needed"
        return base


@dataclasses.dataclass
class CacheContext:
    cache_store: "pyfltr.command.cache_policy.CacheStore"
    command: str
    key: str

    def lookup(self) -> CommandResult | None:
        return self.cache_store.get(self.command, self.key)

    def store(self, result: CommandResult, *, run_id: str | None) -> None:
        self.cache_store.put(self.command, self.key, result, run_id=run_id)


@dataclasses.dataclass(frozen=True)
class ExecutionParams:
    command_info: pyfltr.tool_types.CommandInfo
    targets: list[pathlib.Path]
    commandline_prefix: list[str]
    commandline: list[str]
    cache_commandline: list[str]
    """キャッシュキー計算に使うコマンドライン。

    一時ファイルを実行用commandlineへ渡す場合も、キャッシュキーは元パス基準で安定させる。
    """
    additional_args: list[str]
    fix_mode: bool
    fix_args: list[str] | None
    via_mise: bool = False
    """このコマンドが `mise exec` 経由で起動されるか。

    `ensure_mise_available` 通過後の `ResolvedCommandline` から判定する
    （mise不在時にdirectへフォールバックされたケースを除外するため、
    `build_commandline` 直後の値ではなく事後の値で判断する）。
    `build_subprocess_env` でのmise toolパス除外適用判断に使う。
    詳細は `pyfltr.command.env.build_subprocess_env` のdocstringを参照する。
    """
    effective_runner: str | None = None
    """`ResolvedCommandline.effective_runner` の事後値。

    `ensure_mise_available` 通過後の値を採用する（mise不在時のdirectフォールバックを反映するため）。
    `_with_targets` 経由で `CommandResult` へ転記し、JSONL commandレコードへ露出する。
    対象0件などでcommandline解決を行わない経路では `None`。
    """
    runner_source: str | None = None
    """`ResolvedCommandline.runner_source` の値。`explicit` / `default` / `path-override` のいずれか。"""
    runner_fallback: str | None = None
    """`ResolvedCommandline.runner_fallback` の事後値。

    `ensure_mise_available` 通過後の値を採用し、mise不在時のdirectフォールバック分岐も反映する。
    `_with_targets` 経由で `CommandResult.runner_fallback` へ転記する。
    """
    file_path_remap: dict[str, str] | None = None
    """一時ファイルパスから元ファイルパスへ戻すための辞書。"""
    injected_config_path: pathlib.Path | None = None
    """コマンドラインへ自動注入した設定ファイルの絶対パス。"""


class VerboseOptions(typing.Protocol):
    verbose: bool


@dataclasses.dataclass
class ExecutionRequest:
    command: str
    params: ExecutionParams
    ctx: ExecutionContext
    env: dict[str, str]
    start_time: float
    verbose: bool = False
    cwd: pathlib.Path | None = None
    nodeid_base_cwd: pathlib.Path | None = None

    @property
    def timeout(self) -> float | None:
        return pyfltr.config.model.resolve_command_timeout(self.ctx.config.values, self.command)

    @property
    def retry_options(self) -> dict[str, typing.Any]:
        return pyfltr.config.model.resolve_retry_kwargs(self.ctx.config.values)

    def parse_errors(self, output: str, error_pattern: str | None) -> "list[pyfltr.diagnostics.ErrorLocation]":
        return pyfltr.parsing.entry.parse_errors(
            self.command,
            output,
            error_pattern,
            file_path_remap=self.params.file_path_remap,
            path_base=self.cwd,
            start_cwd=self.ctx.base.start_cwd,
        )


def overall_status(results: "typing.Iterable[CommandResult]") -> typing.Literal["SUCCESS", "FORMATTED", "FAILED"]:
    formatted = False
    for result in results:
        if result.failed:
            return "FAILED"
        formatted = formatted or result.formatted
    return "FORMATTED" if formatted else "SUCCESS"
