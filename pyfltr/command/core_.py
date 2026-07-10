"""実行コンテキスト型・CommandResult。"""

import contextlib
import dataclasses
import pathlib
import tempfile
import typing

import pyfltr.config.config

if typing.TYPE_CHECKING:
    import pyfltr.command.error_parser
    import pyfltr.command.subprojects
    import pyfltr.state.cache
    import pyfltr.state.only_failed

    Subproject = pyfltr.command.subprojects.Subproject

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
    start_cwd: pathlib.Path = dataclasses.field(default_factory=pathlib.Path.cwd)
    """モノレポ探索とパス正規化の起点 cwd（絶対パス）。

    `expand_all_files` ・ `_filter_by_gitignore` ・ `discover_subprojects` 等の
    cwd 起点処理に明示引数として渡す。`run_pipeline` が起動時の cwd（`--work-dir` 適用後）
    を1度だけ設定し、サブプロジェクトループで `os.chdir()` を行わずに cwd 起点処理を維持する。
    """
    subprojects: "list[Subproject]" = dataclasses.field(default_factory=list)
    """検出済みサブプロジェクトの一覧。

    要素0件はモノレポモード無効を意味する（単一 `pyproject.toml` 検出または検出0件）。
    要素1件以上はモノレポモード有効。`subproject_aware=True` のツールはこの一覧を
    元にサブプロジェクト別ループ実行する。
    `Subproject` 型は循環import回避のためTYPE_CHECKING節で定義する。
    """
    subproject_files: "dict[pathlib.Path, list[pathlib.Path]]" = dataclasses.field(default_factory=dict)
    """サブプロジェクト cwd（絶対パス）からそのサブプロジェクトに属するファイル一覧（`start_cwd` 相対）への辞書。

    `pyfltr.command.subprojects.classify_files_by_subproject` で算出する。
    サブプロジェクト未検出時は空辞書。
    """
    external_files: "list[pathlib.Path]" = dataclasses.field(default_factory=list)
    """モノレポモードで`classify_files_by_subproject`がいずれのサブプロジェクトにも
    割り当てなかったファイル一覧（起点cwd配下にない絶対パスを含む）。

    `subproject_loop.run_subproject_loop` で注入対象ツールおよび素通し対象ツールへ
    起点cwdでの追加実行を行うために参照する。
    モノレポモード非適用時（`subprojects` が空）は空リスト。
    """
    subproject_configs: "dict[pathlib.Path, pyfltr.config.config.Config]" = dataclasses.field(default_factory=dict)
    """サブプロジェクト cwd（絶対パス）からそのサブプロジェクト配下の `Config` への辞書。

    各サブプロジェクトで `load_config(config_dir=cwd)` を解決し、起点と同一のCLIオーバーライドを
    再適用した結果を `run_pipeline` が事前構築して格納する。
    `subproject_aware=True` のツール起動時に当該サブプロジェクトの設定（ツールのON/OFF・除外・
    targets 等）を参照し、親子でON/OFFが異なる両方向を尊重する。
    """
    _temporary_directory_stack: contextlib.ExitStack | None = dataclasses.field(default=None, init=False, repr=False)
    _temporary_directory_path: pathlib.Path | None = dataclasses.field(default=None, init=False, repr=False)
    """一時的に生成する検査用ファイルのライフタイムを保持する。"""

    def ensure_temporary_directory(self) -> pathlib.Path:
        """パイプライン内で共有する一時ディレクトリを返す。"""
        if self._temporary_directory_path is None:
            stack = contextlib.ExitStack()
            self._temporary_directory_path = pathlib.Path(stack.enter_context(tempfile.TemporaryDirectory(prefix="pyfltr-")))
            self._temporary_directory_stack = stack
        return self._temporary_directory_path

    def cleanup(self) -> None:
        """パイプライン内で生成した一時ディレクトリを破棄する。"""
        if self._temporary_directory_stack is not None:
            self._temporary_directory_stack.close()
            self._temporary_directory_stack = None
            self._temporary_directory_path = None


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
    subproject_cwd: pathlib.Path | None = None
    """サブプロジェクト分割実行時の当該サブプロジェクト cwd（絶対パス）。

    `subproject_aware=True` のツールでサブプロジェクトループ内から実行されるとき、
    対応するサブプロジェクトの cwd を保持する。`None` の場合は起点 cwd で実行する
    （単一プロジェクトまたは `subproject_aware=False` のツール）。
    `subprocess.Popen(cwd=subproject_cwd)` や `load_config(config_dir=subproject_cwd)` の
    引数として渡す。
    """

    @property
    def config(self) -> pyfltr.config.config.Config:
        """`base.config` への委譲。"""
        return self.base.config

    @property
    def all_files(self) -> "list[pathlib.Path]":
        """対象ファイル一覧。サブプロジェクト分割実行時は当該サブプロジェクト分のみ返す。

        `subproject_cwd` が設定されている場合は `base.subproject_files` から
        当該サブプロジェクトのファイル集合を返す。設定されていない場合は
        `base.all_files` 全体を返す（既存挙動）。
        """
        if self.subproject_cwd is not None:
            return self.base.subproject_files.get(self.subproject_cwd, [])
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
    runner_fallback: str | None = None
    """`pyfltr.command.runner.ResolvedCommandline.runner_fallback` の値。

    期待していた非direct経路がdirectへ退行した場合のみ退行経路ラベル
    （例: `"uv->direct"` / `"uvx->direct"` / `"mise->direct"`）が入る。
    通常経路では `None`。JSONL commandレコードでは値ありの場合のみ
    `effective_runner` / `runner_source` / `runner_fallback` の3点をまとめて出力する判定キー。
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
    retry_count: int = 0
    """OOM起因で再試行した回数。0はリトライなしを意味する。"""

    @classmethod
    # from_run は各コマンド実行モジュール（linter_fix / textlint_fix 等）で同様の引数転送パターンを持つため重複検知される
    def from_run(
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
        retry_count: int = 0,
    ) -> "CommandResult":
        """実行結果からCommandResultを組み立てるファクトリメソッド。

        `command_type` を省略した場合は `command_info.type` を使う。
        `command_type` と `command_info` の両方を省略することはできない。
        `errors` を省略した場合は空リストを使う（parse_errorsの呼び出しは呼び出し側で行う）。
        `timeout_exceeded=True` を指定すると当該結果がtimeout由来の失敗であることを示す。
        `retry_count` はOOM起因の再試行回数（0はリトライなし、複数回subprocessを呼ぶ経路では合算値）。
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
            retry_count=retry_count,
        )

    @property
    def alerted(self) -> bool:
        """skipped/succeeded以外ならTrue"""
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
        - `warning`: per-tool `{command}-severity = "warning"` 設定下での失敗格下げ。
          パイプライン全体exit codeに影響しない。`commands_summary.needs_action.warning` へ集計し、
          `summary.guidance` のfailure系文言は出力しない
        - `resolution_failed`: ツール起動コマンドの解決に失敗した
        - `running`: heartbeat由来の実行中レコード（pipeline側で発行）

        `timeout_exceeded=True` の場合、`command_type` がformatterであっても `failed` を返す。
        timeoutで強制停止された結果を成功扱い（`formatted`）にしてしまうと、
        利用者・LLMがハング由来の停止と通常のformatter書き換えを区別できなくなるため。

        ツール起動自体に失敗したケース（`resolution_failed` / `timeout_exceeded`）は
        ツール起動自体の異常で警告扱いに馴染まないため、`severity` の影響を受けない。

        `status` は `resolution_failed`・`returncode`・`has_error`・`command_type`・
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
        elif self.command_type == "formatter" and not self.has_error:
            status = "formatted"
        elif self.severity == "warning":
            status = "warning"
        else:
            status = "failed"
        return status

    @classmethod
    def merge(cls, results: "list[CommandResult]") -> "CommandResult":
        """サブプロジェクト別の結果を1件にマージする。

        `subproject_aware=True` ツールがサブプロジェクトループで複数回実行された
        結果を、利用者向け出力スキーマを変えずに集約するためのヘルパー。

        マージ規則:
        - `output`: 各実行の出力を連結する（区切り行は `dispatcher` 側で挿入する）
        - `errors`: 全実行の和集合
        - `target_files`: 全実行の和集合（重複は順序維持で除去）
        - `files`: 全実行の和集合のファイル数
        - `elapsed`: 全実行の合計
        - `status`: `failed > resolution_failed > warning > formatted > succeeded > skipped`
          の順で最も重い結果を採用（`timeout_exceeded=True` は `status` プロパティ上 `failed` を返す）。
          `running` は heartbeat 由来の途中状態で `merge` の入力には来ない前提のため `status_weight` に含めない
        - `command_type`: 最も重い結果の値を採用（`status` と同じく `worst` 由来）。
          textlint fix のように同一コマンドが `linter` ・ `formatter` を切り替える結果を扱うため
        - `returncode`: 最初に発生した非ゼロ値を採用
        - `fixed_files`: 全実行の和集合
        - その他のフィールドは先頭結果から引き継ぐ

        `results` が1件のみの場合はそのまま返す（フィールドのコピー含む）。
        空リスト渡しはサブプロジェクトループの呼び出し側で防ぐ前提とする。
        """
        if not results:
            raise ValueError("merge には1件以上の CommandResult が必要")
        if len(results) == 1:
            return results[0]

        head = results[0]
        # status の重み付け（数値が大きいほど重い）。
        # `timeout_exceeded=True` の場合 `status` プロパティは `failed` を返すため、
        # `status` キーとしての `"timeout_exceeded"` は登場しない。
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

        merged_errors: list[pyfltr.command.error_parser.ErrorLocation] = []
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

        # returncode: 最初の非ゼロ値、無ければ最初の値
        first_nonzero: int | None = None
        for r in results:
            if r.returncode is not None and r.returncode != 0:
                first_nonzero = r.returncode
                break
        merged_returncode = first_nonzero if first_nonzero is not None else head.returncode

        total_elapsed = sum(r.elapsed for r in results)
        total_files = sum(r.files for r in results)
        total_retry_count = sum(r.retry_count for r in results)
        any_has_error = any(r.has_error for r in results)
        any_timeout = any(r.timeout_exceeded for r in results)
        any_resolution_failed = any(r.resolution_failed for r in results)
        any_archived = any(r.archived for r in results)

        return cls(
            command=head.command,
            command_type=worst.command_type,
            commandline=head.commandline,
            returncode=merged_returncode,
            has_error=any_has_error,
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
            effective_runner=head.effective_runner,
            runner_source=head.runner_source,
            runner_fallback=head.runner_fallback,
            timeout_exceeded=any_timeout,
            severity=head.severity,
            retry_count=total_retry_count,
        )

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
