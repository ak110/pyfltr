"""MCPツールスキーマ用Pydanticモデル群。

`mcp_server.py`が公開する8ツールの引数・戻り値スキーマをまとめて定義する。
FastMCPはこれらのモデルからJSONスキーマを自動生成してMCPクライアントへ公開する。
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


class CommandSummaryModel(pydantic.BaseModel):
    """コマンドごとのサマリ。`show_run`ツールの戻り値内要素。"""

    command: str | None = pydantic.Field(default=None, description="コマンド名。")
    status: str | None = pydantic.Field(
        default=None,
        description="実行ステータス（succeeded / formatted / failed / skipped）。",
    )
    has_error: bool | None = pydantic.Field(default=None, description="エラーが発生したか否か。")
    diagnostics: int | None = pydantic.Field(default=None, description="diagnosticの件数。")


class DiagnosticMessageModel(pydantic.BaseModel):
    """集約diagnostic内の1指摘分。`DiagnosticModel.messages`の要素。"""

    line: int | None = pydantic.Field(default=None, description="行番号。")
    col: int | None = pydantic.Field(default=None, description="列番号。")
    end_line: int | None = pydantic.Field(
        default=None,
        description="違反範囲の終端行。範囲を返すツール（現状textlintのみ）で設定される。",
    )
    end_col: int | None = pydantic.Field(
        default=None,
        description=(
            "違反範囲の終端列。範囲を返すツール（現状textlintのみ）で設定される。"
            "textlintはノード先頭からの累積位置を返す仕様で、行内オフセットではない。"
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


class CommandDiagnosticsModel(pydantic.BaseModel):
    """コマンドの詳細情報（tool.json + diagnostics.jsonl全件）。`show_run_diagnostics`ツールの戻り値。

    JSONL本体・`tool.json`の双方で`hint_urls`キー（アンダースコア区切り）を採用するため、
    Pydantic側でも属性名・出力キー名ともに`hint_urls`で揃える。
    同様に`hints`キーも`tool.json`と同名で揃える。
    """

    command_meta: dict[str, typing.Any] = pydantic.Field(description="コマンドのmeta情報（`tool.json`の内容）。")
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
        description="コマンド別サマリ一覧（status・has_error・diagnostics件数）。",
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

    file: str = pydantic.Field(description="マッチを検出したファイルパス。")
    line: int = pydantic.Field(description="マッチ開始行番号（1-origin）。")
    col: int = pydantic.Field(description="マッチ開始列番号（1-origin、文字単位）。")
    end_col: int | None = pydantic.Field(default=None, description="マッチ終了列番号（1-origin）。")
    match_text: str = pydantic.Field(description="マッチした文字列。")
    line_text: str = pydantic.Field(description="マッチを含む行の本文（改行除く）。")
    before: list[str] = pydantic.Field(default_factory=list, description="`-B`コンテキストの前行群。")
    after: list[str] = pydantic.Field(default_factory=list, description="`-A`コンテキストの後行群。")


class GrepResultModel(pydantic.BaseModel):
    """`grep`ツールの戻り値。"""

    matches: list[GrepMatchModel] = pydantic.Field(description="マッチ一覧。")
    total_matches: int = pydantic.Field(description="全マッチ件数。")
    files_scanned: int = pydantic.Field(description="走査したファイル数。")
    exit_code: int = pydantic.Field(description="終了コード。マッチあり=0、マッチなし=1。")


class ReplaceFileChangeModel(pydantic.BaseModel):
    """`replace`ツールの1ファイル変更分。"""

    file: str = pydantic.Field(description="変更対象ファイルパス。")
    count: int = pydantic.Field(description="置換箇所数。")
    before_hash: str | None = pydantic.Field(default=None, description="変更前内容のSHA-256ハッシュ。")
    after_hash: str | None = pydantic.Field(default=None, description="変更後内容のSHA-256ハッシュ。")


class ReplaceChangeRecordModel(pydantic.BaseModel):
    """`replace`ツールの1置換箇所。`show_changes=True`時に`ReplaceResultModel.changes`へ含まれる。"""

    file: str = pydantic.Field(description="対象ファイルパス。")
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


class ReplaceUndoModel(pydantic.BaseModel):
    """`replace_undo`ツールの戻り値。"""

    replace_id: str = pydantic.Field(description="undo対象のreplace識別子（ULID）。")
    restored: list[str] = pydantic.Field(description="復元に成功したファイルパスの一覧。")
    skipped: list[str] = pydantic.Field(
        description="ハッシュ不一致でスキップされたファイルパスの一覧（force=False時）。",
    )
    exit_code: int = pydantic.Field(description="終了コード。skippedあり=1、全件復元=0。")
