"""MCPツールスキーマ用Pydanticモデル群。

`mcp_server.py`が公開する11ツールの戻り値スキーマをまとめて定義する。
入力側は各ツール関数の型注釈とdocstringからJSONスキーマを自動生成する。
"""

from __future__ import annotations

import typing

import pydantic


class RunSummaryModel(pydantic.BaseModel):
    """run一覧の1件分サマリ。`list_runs`ツールの戻り値要素。"""

    run_id: str = pydantic.Field(description="runの識別子（ULID）。")
    started_at: str | None = pydantic.Field(default=None, description="実行開始日時（ISO 8601形式）。")
    finished_at: str | None = pydantic.Field(default=None, description="実行完了日時（ISO 8601形式）。")
    exit_code: int | None = pydantic.Field(default=None, description="終了コード。0 = 成功、1 = 失敗。")
    commands: list[str] = pydantic.Field(default_factory=list, description="実行したコマンド名の一覧。")
    files: int | None = pydantic.Field(default=None, description="対象ファイル数。")


class SlowTestModel(pydantic.BaseModel):
    """遅いテスト1件分。`CommandSummaryModel.slow_tests`と`CommandMetaModel.slow_tests`の要素。"""

    nodeid: str = pydantic.Field(
        description="テスター共通の識別子。pytestはnodeid、vitestはファイルパスとテスト名を連結した形式。"
    )
    phase: str = pydantic.Field(description="計測区間。pytestはsetup / call / teardown、区間を区別しないvitestはtest。")
    seconds: float = pydantic.Field(description="当該フェーズの所要秒数。")


class CommandSummaryModel(pydantic.BaseModel):
    """コマンドごとのサマリ。`show_run`ツールの戻り値内要素。"""

    command: str | None = pydantic.Field(default=None, description="コマンド名。")
    status: str | None = pydantic.Field(
        default=None,
        description="実行ステータス（succeeded / formatted / failed / skipped）。",
    )
    diagnostics: int | None = pydantic.Field(default=None, description="diagnosticの件数。")
    elapsed: float | None = pydantic.Field(
        default=None,
        description=(
            "当該コマンドの実行に要した秒数。キャッシュヒットしたコマンドは実行アーカイブへ記録されないため本一覧に現れない。"
        ),
    )
    slow_tests: list[SlowTestModel] = pydantic.Field(
        default_factory=list,
        description="テスターが報告した遅いテストの上位一覧（秒数降順）。",
    )


class DiagnosticMessageModel(pydantic.BaseModel):
    """集約diagnostic内の1指摘分。`DiagnosticModel.messages`の要素。"""

    line: int | None = pydantic.Field(default=None, description="行番号。")
    col: int | None = pydantic.Field(default=None, description="列番号。")
    end_line: int | None = pydantic.Field(
        default=None,
        description="診断範囲の終了行。終了位置を出力するツールで設定される。",
    )
    end_col: int | None = pydantic.Field(
        default=None,
        description=(
            "診断範囲の終了列。終了位置を出力するツールで設定される。"
            "textlintはノード先頭からの累積位置を返す仕様で、行内オフセットではない。"
            "biomeは行内オフセットを返す。"
        ),
    )
    rule: str | None = pydantic.Field(default=None, description="ルール識別子。")
    severity: str | None = pydantic.Field(
        default=None,
        description="severity（error / warning / info）。未対応ツールはNone。",
    )
    fix: str | None = pydantic.Field(default=None, description="自動修正可能な場合の修正内容。")
    msg: str | None = pydantic.Field(default=None, description="エラーメッセージ。")


class DiagnosticModel(pydantic.BaseModel):
    """`(command, file)`単位で集約されたdiagnosticエントリ。

    `show_run_diagnostics`ツールの戻り値内要素。`messages`に個別指摘を保持する。
    """

    command: str | None = pydantic.Field(default=None, description="コマンド名。")
    file: str | None = pydantic.Field(default=None, description="対象ファイルパス。")
    messages: list[DiagnosticMessageModel] = pydantic.Field(
        default_factory=list,
        description="`(line, col, rule)`昇順で並ぶ個別指摘のリスト。",
    )


class RunOverviewModel(pydantic.BaseModel):
    """runの概要（meta + コマンド別サマリ）。`show_run`ツールの戻り値。"""

    run_id: str = pydantic.Field(description="runの識別子（ULID）。")
    meta: dict[str, typing.Any] = pydantic.Field(description="runのmeta情報（`read_meta`の戻り値）。")
    commands: list[CommandSummaryModel] = pydantic.Field(description="コマンド別サマリ一覧。")


class CommandMetaModel(pydantic.BaseModel):
    """コマンドのmeta情報。`CommandDiagnosticsModel.command_meta`の値。

    実行アーカイブの`tool.json`から`commandline`を除いた項目を保持する。
    `commandline`は対象ファイルを引数へ展開するツールで長さが対象ファイル数に比例し、
    大規模な対象では応答の大半を占めて診断本体の読み取りを妨げるため含めない。
    完全な引数列は`pyfltr show-run <run_id> --commands=<name>`と実行アーカイブの
    `tool.json`から取得する。`show_run_output`が返す`output.log`は引数列を含まない。

    `hint_urls`・`hints`は`CommandDiagnosticsModel`の同名フィールドが返すため本モデルへ含めない。
    `slow_tests`・`retry_command`は`tool.json`に保存されている場合だけ直列化する。
    """

    command: str = pydantic.Field(description="コマンド名。")
    type: str = pydantic.Field(description="コマンドの種別（formatter / linter / tester）。")
    status: str = pydantic.Field(
        description=("実行ステータス（succeeded / formatted / skipped / failed / warning / resolution_failed）。"),
    )
    returncode: int | None = pydantic.Field(
        default=None,
        description="ツールの終了コード。対象ファイル0件等で起動しなかった場合はNone。",
    )
    files: int = pydantic.Field(description="対象ファイル数。")
    elapsed: float = pydantic.Field(description="当該コマンドの実行に要した秒数。")
    diagnostics: int = pydantic.Field(description="diagnosticの件数。")
    slow_tests: list[SlowTestModel] = pydantic.Field(
        default_factory=list,
        description="テスターが報告した遅いテストの上位一覧（秒数降順）。",
    )
    retry_command: str | None = pydantic.Field(
        default=None,
        description="当該コマンドを失敗ファイルのみに限定して再実行するシェルコマンド。",
    )

    @pydantic.model_serializer(mode="wrap")
    def _omit_unsaved_optional_fields(
        self,
        handler: pydantic.SerializerFunctionWrapHandler,
    ) -> dict[str, typing.Any]:
        """保存元に無い任意項目を直列化結果から省く。"""
        serialized = typing.cast(dict[str, typing.Any], handler(self))
        for field_name in ("slow_tests", "retry_command"):
            if field_name not in self.model_fields_set:
                serialized.pop(field_name, None)
        return serialized


class CommandDiagnosticsModel(pydantic.BaseModel):
    """コマンドの詳細情報（`tool.json`のmeta情報 + diagnostics.jsonl全件）。`show_run_diagnostics`ツールの戻り値。

    `command_meta`は`tool.json`から`commandline`を除いた項目を`CommandMetaModel`として返す。

    JSONL本体・`tool.json`の双方で`hint_urls`キー（アンダースコア区切り）を採用するため、
    Pydantic側でも属性名・出力キー名ともに`hint_urls`で揃える。
    同様に`hints`キーも`tool.json`と同名で揃える。
    """

    command_meta: CommandMetaModel = pydantic.Field(description="コマンドのmeta情報。")
    diagnostics: list[DiagnosticModel] = pydantic.Field(description="diagnosticの全件一覧。")
    hint_urls: dict[str, str] | None = pydantic.Field(
        default=None,
        description="rule ID → ドキュメントURLの辞書。URLを生成できたruleのみ含める。",
    )
    hints: dict[str, str] | None = pydantic.Field(
        default=None,
        description="rule ID → 短い修正ヒント文字列の辞書。ヒントを持つruleのみ含める。",
    )


class RunForAgentResult(pydantic.BaseModel):
    """`run_for_agent`ツールの戻り値。"""

    run_id: str | None = pydantic.Field(
        default=None,
        description="実行アーカイブの参照キー（ULID）。early exit時はNone。",
    )
    exit_code: int = pydantic.Field(description="終了コード。0 = 成功、1 = 失敗。")
    failed: list[str] = pydantic.Field(description="失敗したコマンド名の一覧。")
    commands: list[CommandSummaryModel] = pydantic.Field(
        default_factory=list,
        description="コマンド別サマリ一覧（status・diagnostics件数）。",
    )
    skipped_reason: str | None = pydantic.Field(
        default=None,
        description="early exitが発生した理由。runが実行されなかった場合に設定される。",
    )
    retry_commands: dict[str, str] = pydantic.Field(
        default_factory=dict,
        description="失敗コマンドの再実行シェルコマンド辞書（コマンド名 → shell文字列）。成功・cachedは省略。",
    )


class GrepMatchModel(pydantic.BaseModel):
    """`grep`ツールの1マッチ分。"""

    file: str = pydantic.Field(
        description="マッチを検出したファイルパス。区切りは`/`へ統一するため、Windowsでも`C:/...`形式となる。"
    )
    line: int = pydantic.Field(description="マッチ開始行番号（1-origin）。")
    col: int = pydantic.Field(description="マッチ開始列番号（1-origin、文字単位）。")
    end_col: int | None = pydantic.Field(default=None, description="マッチ終了列番号（1-origin）。")
    match_text: str = pydantic.Field(description="マッチした文字列。")
    line_text: str = pydantic.Field(description="マッチを含む行の本文（改行除く）。")
    before: list[str] = pydantic.Field(default_factory=list, description="`-B`コンテキストの前行群。")
    after: list[str] = pydantic.Field(default_factory=list, description="`-A`コンテキストの後行群。")
    line_text_offset: int = pydantic.Field(
        default=0, description="`line_text`を切り出した開始位置（0-origin文字数）。行頭から切り出した場合は0。"
    )
    truncated: list[str] = pydantic.Field(
        default_factory=list,
        description="切り詰めが発生したフィールド名の一覧（line_text / match_text / before / after）。",
    )


class GrepFileCountModel(pydantic.BaseModel):
    """`grep`の集計モード`count`の1ファイル分。"""

    file: str = pydantic.Field(description="対象ファイルパス。区切りは`/`へ統一するため、Windowsでも`C:/...`形式となる。")
    count: int = pydantic.Field(description="当該ファイルのマッチ件数。")


class GrepResultModel(pydantic.BaseModel):
    """`grep`ツールの戻り値。"""

    matches: list[GrepMatchModel] = pydantic.Field(description="マッチ一覧。")
    total_matches: int = pydantic.Field(description="全マッチ件数。")
    files_scanned: int = pydantic.Field(description="走査したファイル数。")
    exit_code: int = pydantic.Field(description="終了コード。マッチあり=0、マッチなし=1。")
    warnings: list[str] = pydantic.Field(default_factory=list, description="実行中に発行された警告メッセージ。")
    fully_excluded_files: list[str] = pydantic.Field(
        default_factory=list,
        description="直接指定がexclude / .gitignoreで対象外になったファイル一覧。",
    )
    missing_targets: list[str] = pydantic.Field(
        default_factory=list,
        description="直接指定したが存在しなかったファイル一覧。",
    )
    summary_mode: str | None = pydantic.Field(
        default=None,
        description="集計モード（files_with_matches / count / files_without_match）。未指定時はNone。",
    )
    files_with_matches: list[str] = pydantic.Field(
        default_factory=list,
        description=('`summary_mode="files_with_matches"`時のマッチを含むファイル一覧。区切りは`/`へ統一する。'),
    )
    file_counts: list[GrepFileCountModel] = pydantic.Field(
        default_factory=list,
        description='`summary_mode="count"`時のファイル別マッチ件数。',
    )
    files_without_match: list[str] = pydantic.Field(
        default_factory=list,
        description=('`summary_mode="files_without_match"`時のマッチを含まないファイル一覧。区切りは`/`へ統一する。'),
    )


class ReplaceFileChangeModel(pydantic.BaseModel):
    """`replace`ツールの1ファイル変更分。"""

    file: str = pydantic.Field(description="変更対象ファイルパス。区切りは`/`へ統一するため、Windowsでも`C:/...`形式となる。")
    count: int = pydantic.Field(description="置換箇所数。")
    before_hash: str | None = pydantic.Field(default=None, description="変更前内容のSHA-256ハッシュ。")
    after_hash: str | None = pydantic.Field(default=None, description="変更後内容のSHA-256ハッシュ。")


class ReplaceChangeRecordModel(pydantic.BaseModel):
    """`replace`ツールの1置換箇所。`show_changes=True`時に`ReplaceResultModel.changes`へ含まれる。"""

    file: str = pydantic.Field(description="対象ファイルパス。区切りは`/`へ統一するため、Windowsでも`C:/...`形式となる。")
    line: int = pydantic.Field(description="置換対象行番号（1-origin）。")
    col: int = pydantic.Field(description="置換開始列番号（1-origin）。")
    before_line: str = pydantic.Field(description="置換前の行本文。")
    after_line: str = pydantic.Field(description="置換後の行本文。")


class ReplaceResultModel(pydantic.BaseModel):
    """`replace`ツールの戻り値。"""

    replace_id: str | None = pydantic.Field(
        default=None,
        description="replace履歴の識別子（ULID）。dry_run=True時はNone。",
    )
    dry_run: bool = pydantic.Field(description="dry-runモードか否か。")
    files_changed: int = pydantic.Field(description="変更が発生したファイル数。")
    total_replacements: int = pydantic.Field(description="置換箇所の総数。")
    file_changes: list[ReplaceFileChangeModel] = pydantic.Field(description="ファイルごとの変更サマリ。")
    changes: list[ReplaceChangeRecordModel] = pydantic.Field(
        default_factory=list,
        description="`show_changes=True`時の各置換箇所の変更前後（空リストで省略）。",
    )
    exit_code: int = pydantic.Field(description="終了コード。0 = 成功。")
    fully_excluded_files: list[str] = pydantic.Field(
        default_factory=list,
        description="直接指定がexclude / .gitignoreで対象外になったファイル一覧。",
    )
    missing_targets: list[str] = pydantic.Field(
        default_factory=list,
        description="直接指定したが存在しなかったファイル一覧。",
    )


class ReplaceUndoModel(pydantic.BaseModel):
    """`replace_undo`ツールの戻り値。"""

    replace_id: str = pydantic.Field(description="undo対象のreplace識別子（ULID）。")
    restored: list[str] = pydantic.Field(description="復元に成功したファイルパスの一覧。区切りは`/`へ統一する。")
    skipped: list[str] = pydantic.Field(
        description="ハッシュ不一致でスキップされたファイルパスの一覧（force=False時）。区切りは`/`へ統一する。",
    )
    exit_code: int = pydantic.Field(description="終了コード。skippedあり=1、全件復元=0。")


class ReplaceHistoryFileModel(pydantic.BaseModel):
    """replace履歴の対象ファイル1件分。"""

    file: str = pydantic.Field(
        description="対象ファイルパス。区切りは保存時に`/`へ統一するため、Windowsでも`C:/...`形式となる。"
    )
    records_count: int = pydantic.Field(description="当該ファイルの置換件数。")


class ReplaceHistoryEntryModel(pydantic.BaseModel):
    """replace履歴の1件分。`before_content`と`records`は応答に含めない。"""

    replace_id: str = pydantic.Field(description="replace識別子（ULID）。")
    saved_at: str | None = pydantic.Field(default=None, description="保存日時（ISO 8601形式）。")
    command: dict[str, typing.Any] = pydantic.Field(
        default_factory=dict,
        description="実行時のパターン・置換式・エンコーディング等。",
    )
    files: list[ReplaceHistoryFileModel] = pydantic.Field(
        default_factory=list,
        description="対象ファイルと置換件数の一覧。",
    )


class ReplaceHistoryModel(pydantic.BaseModel):
    """`replace_history`ツールの戻り値。"""

    action: str = pydantic.Field(description="実行したaction（list / show）。")
    entries: list[ReplaceHistoryEntryModel] = pydantic.Field(
        default_factory=list,
        description='履歴一覧。`action="show"`時は1件のみ。',
    )


class CommandInfoModel(pydantic.BaseModel):
    """`command_info`ツールの戻り値。"""

    command: str = pydantic.Field(description="対象のツール名。")
    resolved: bool = pydantic.Field(description="起動方式の解決に成功したか否か。")
    info: dict[str, typing.Any] = pydantic.Field(
        description="解決結果の詳細。解決失敗時は`error`キーを含む。",
    )


class ConfigResultModel(pydantic.BaseModel):
    """`config`ツールの戻り値。actionごとに使うフィールドが異なる。"""

    action: str = pydantic.Field(description="実行したaction（get / set / delete / list）。")
    path: str = pydantic.Field(description="操作対象の設定ファイルパス。")
    key: str | None = pydantic.Field(default=None, description="対象の設定キー名。listでは未使用。")
    value: typing.Any = pydantic.Field(default=None, description="getまたはsetの設定値。")
    is_default: bool | None = pydantic.Field(default=None, description="getの値が既定値由来か否か。")
    existed: bool | None = pydantic.Field(default=None, description="deleteで対象キーが書かれていたか否か。")
    values: dict[str, typing.Any] = pydantic.Field(
        default_factory=dict,
        description="listの設定値一覧。include_defaults=True時は値と既定値由来か否かを含む。",
    )
    warnings: list[str] = pydantic.Field(default_factory=list, description="操作中に発行された警告メッセージ。")
