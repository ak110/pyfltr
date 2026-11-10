"""検索と置換のMCPツール。"""

from __future__ import annotations

import pathlib
import typing

import pydantic

import pyfltr.cli.mcp_errors
import pyfltr.cli.mcp_models
import pyfltr.config.config
import pyfltr.grep_.history
import pyfltr.grep_.matcher
import pyfltr.grep_.operations
import pyfltr.paths
import pyfltr.warnings_
from pyfltr.cli.mcp_models import (
    GrepFileCountModel,
    GrepFileResultModel,
    GrepMatchModel,
    GrepResultModel,
    ReplaceChangeRecordModel,
    ReplaceFileChangeModel,
    ReplaceHistoryEntryModel,
    ReplaceHistoryFileModel,
    ReplaceHistoryModel,
    ReplaceResultModel,
    ReplaceUndoModel,
)

REPLACE_HISTORY_COMMAND_KEYS: tuple[str, ...] = ("pattern", "replacement", "fixed_strings", "encoding")
"""`replace_history`の`command`へ返す保存項目。

`replace_id`は応答の同じ要素の最上位と重複し、`dry_run`は保存される履歴では常に偽のため返さない。
"""


GREP_FILE_MATCH_KEYS: frozenset[str] = frozenset(set(GrepMatchModel.model_fields) - {"file"})
"""`grep`の`file_results[].matches`へ返すキー。外側の`file`と重複する`file`を除いた`GrepMatchModel`の項目に限る。

縮約出力の一致は辞書のまま渡るため、生成側（`pyfltr.grep_.jsonl_records.match_payload`）へ加えたキーを
用途の対照（`tests/cli/mcp_models_test.py`の`GrepMatchModel`の項目）を経ずに配送しないよう限定する。
"""


IgnoreCaseArg = typing.Annotated[bool, pydantic.Field(description="Trueで大文字と小文字を区別しない。")]


SmartCaseArg = typing.Annotated[
    bool,
    pydantic.Field(description="Trueでパターンに大文字を含まない場合だけ大文字と小文字を区別しない。"),
]


FixedStringsArg = typing.Annotated[bool, pydantic.Field(description="Trueでパターンを正規表現ではなく固定文字列として扱う。")]


WordRegexpArg = typing.Annotated[bool, pydantic.Field(description="Trueで単語境界に囲まれた一致だけを採用する。")]


LineRegexpArg = typing.Annotated[bool, pydantic.Field(description="Trueで行全体に一致した場合だけ採用する。")]


MultilineArg = typing.Annotated[bool, pydantic.Field(description="Trueで改行をまたぐ一致を許可する。")]


TypesArg = typing.Annotated[
    list[str] | None,
    pydantic.Field(description='対象を言語タイプで限定する一覧（例: ["python", "ts"]）。省略時は限定しない。'),
]


GlobsArg = typing.Annotated[
    list[str] | None, pydantic.Field(description="対象をglobパターンで限定する一覧。省略時は限定しない。")
]


MaxFilesizeArg = typing.Annotated[
    int | None,
    pydantic.Field(description="走査するファイルサイズの上限（バイト）。超えるファイルは対象外。省略または0で上限なし。"),
]


async def tool_grep(
    paths: typing.Annotated[list[str], pydantic.Field(description="検索対象のファイルまたはディレクトリのパス一覧。")],
    pattern: typing.Annotated[
        str | None,
        pydantic.Field(description="検索パターン（正規表現）。`patterns`または`pattern_file`だけで指定する場合は省略できる。"),
    ] = None,
    patterns: typing.Annotated[
        list[str] | None,
        pydantic.Field(
            description="追加の検索パターンの一覧。`pattern`・`pattern_file`と合わせ、いずれかに一致すれば採用する。"
        ),
    ] = None,
    pattern_file: typing.Annotated[str | None, pydantic.Field(description="1行に1パターンを書いたファイルのパス。")] = None,
    ignore_case: IgnoreCaseArg = False,
    smart_case: SmartCaseArg = False,
    fixed_strings: FixedStringsArg = False,
    word_regexp: WordRegexpArg = False,
    line_regexp: LineRegexpArg = False,
    multiline: MultilineArg = False,
    before_context: typing.Annotated[int, pydantic.Field(description="一致行の前に含める文脈の行数。")] = 0,
    after_context: typing.Annotated[int, pydantic.Field(description="一致行の後に含める文脈の行数。")] = 0,
    context: typing.Annotated[
        int | None,
        pydantic.Field(description="前後の文脈行数の一括指定。`before_context`・`after_context`のうち0の方向だけへ適用する。"),
    ] = None,
    max_count: typing.Annotated[
        int | None, pydantic.Field(description="ファイルごとの最大一致件数。省略または0で無制限。")
    ] = None,
    max_total: typing.Annotated[
        int | None,
        pydantic.Field(
            description='全体の最大一致件数。省略または0で無制限。`summary_mode="files_without_match"`とは正の値を併用できない。'
        ),
    ] = None,
    summary_mode: typing.Annotated[
        typing.Literal["files_with_matches", "count", "files_without_match"] | None,
        pydantic.Field(
            description=(
                "集計モード。指定時は`matches`を空で返し、`files_with_matches`は一致したファイル一覧、"
                "`count`はファイルごとの一致件数、`files_without_match`は一致しなかったファイル一覧を返す。"
            ),
        ),
    ] = None,
    types: TypesArg = None,
    globs: GlobsArg = None,
    encoding: typing.Annotated[str, pydantic.Field(description="ファイルを読む文字コード。")] = "utf-8",
    max_filesize: MaxFilesizeArg = None,
    max_preview_chars: typing.Annotated[
        int | None,
        pydantic.Field(description="返す本文1件あたりの最大文字数。省略時は200文字、0で無制限。"),
    ] = None,
    auto_summary: typing.Annotated[
        bool,
        pydantic.Field(
            description=(
                "Trueで、件数上限・プレビュー上限・集計を指定していない大きな結果を約10,000文字へ縮約し、"
                "一致の多いファイルを件数表示へ切り替える。Falseで指定範囲の全一致を返す。"
            ),
        ),
    ] = True,
    no_exclude: pyfltr.cli.mcp_models.NoExcludeArg = False,
    no_gitignore: pyfltr.cli.mcp_models.NoGitignoreArg = False,
) -> GrepResultModel:
    """指定ファイル群から正規表現パターンを検索し、マッチ一覧を返す。

    pyfltrの`exclude`/`extend-exclude`/`respect-gitignore`設定を尊重する。
    通常検索でも未指定の`max_total`は走査を制限せず、全結果から返却形式を選択する。
    """
    # warnings_はモジュールグローバルに蓄積するため、リクエスト開始時に初期化する
    pyfltr.warnings_.clear()
    collected = ([pattern] if pattern is not None else []) + list(patterns or [])
    if pattern_file is not None:
        try:
            collected.extend(pyfltr.grep_.matcher.read_pattern_file(pathlib.Path(pattern_file)))
        except OSError as exc:
            pyfltr.cli.mcp_errors.raise_mcp_error(
                f"パターンファイルの読み込みに失敗しました: {exc}。`pattern_file`のパスと読み取り権限を確認してください"
            )
    if not collected:
        pyfltr.cli.mcp_errors.raise_mcp_error(
            "パターンが指定されていません。`pattern`・`patterns`・`pattern_file`のいずれかで検索パターンを指定してください"
        )
    valid_summary_modes = ("files_with_matches", "count", "files_without_match")
    if summary_mode is not None and summary_mode not in valid_summary_modes:
        pyfltr.cli.mcp_errors.raise_mcp_error(
            "summary_mode は files_with_matches / count / files_without_match のいずれかを指定してください。"
        )
    if summary_mode == "files_without_match" and max_total is not None and max_total > 0:
        pyfltr.cli.mcp_errors.raise_mcp_error(
            "summary_mode=files_without_match では max_total に正の値を指定できません。"
            "max_totalを省略するか0を指定してください。"
        )
    request = pyfltr.grep_.operations.GrepRequest(
        patterns=collected,
        summary_only=summary_mode is not None,
        max_count=max_count,
        max_total=max_total,
        max_preview_chars=max_preview_chars,
        auto_summary=auto_summary,
        output_format="mcp",
        full_text_hint="`max_preview_chars=0`",
        targets=pyfltr.grep_.operations.TargetRequest.from_paths(paths, types or [], globs or [], no_exclude, no_gitignore),
        **pyfltr.grep_.operations.PatternOptions.arguments(
            (fixed_strings, ignore_case, smart_case, word_regexp, line_regexp, multiline),
            (before_context, after_context, context),
            encoding,
            max_filesize,
        ),
    )
    try:
        operation = pyfltr.grep_.operations.execute_grep(request)
    except pyfltr.grep_.operations.TargetConfigError as exc:
        pyfltr.cli.mcp_errors.raise_mcp_error(pyfltr.config.config.format_config_error(exc, retry="`grep`ツール"))
    except ValueError as exc:
        pyfltr.cli.mcp_errors.raise_mcp_error(
            f"{exc}。パターンの該当位置を修正するか、固定文字列には fixed_strings=True を指定してください。"
        )
    expanded = operation.expanded
    per_file_counts = operation.per_file_counts
    total_matches = operation.total_matches
    selection = operation.selection
    files_scanned = len(expanded)
    matches = [GrepMatchModel.model_validate(payload) for payload in selection.matches]
    adaptive_file_results = [
        GrepFileResultModel.model_validate(
            {
                **result,
                "matches": [
                    {key: value for key, value in match.items() if key in GREP_FILE_MATCH_KEYS}
                    for match in result.get("matches", [])
                ],
            }
        )
        for result in selection.file_results
    ]

    files_with_matches = (
        [pyfltr.paths.normalize_separators(file) for file in per_file_counts] if summary_mode == "files_with_matches" else []
    )
    file_counts = (
        [
            GrepFileCountModel(file=pyfltr.paths.normalize_separators(file), count=count)
            for file, count in per_file_counts.items()
        ]
        if summary_mode == "count"
        else []
    )
    files_without_match = (
        [pyfltr.paths.normalize_separators(file) for file in expanded if file not in per_file_counts]
        if summary_mode == "files_without_match"
        else []
    )
    return GrepResultModel(
        matches=matches,
        file_results=adaptive_file_results,
        output_mode=selection.output_mode,
        total_matches=total_matches,
        files_scanned=files_scanned,
        exit_code=0 if total_matches > 0 else 1,
        returned_matches=selection.returned_matches,
        omitted_matches=selection.omitted_matches,
        omitted_files=selection.omitted_files,
        guidance=(
            [
                "Narrow the search by file path, globs, or types to inspect omitted matches.",
                "Set auto_summary=false to return every match in the selected search range.",
            ]
            if selection.output_mode != "full"
            else []
        ),
        warnings=[pyfltr.warnings_.format_warning_text(entry) for entry in pyfltr.warnings_.collected_warnings()],
        fully_excluded_files=pyfltr.warnings_.filtered_direct_files(reason="excluded"),
        missing_targets=pyfltr.warnings_.filtered_direct_files(reason="missing"),
        summary_mode=summary_mode,
        files_with_matches=files_with_matches,
        file_counts=file_counts,
        files_without_match=files_without_match,
    )


async def tool_replace(
    pattern: typing.Annotated[str, pydantic.Field(description="置換対象を探すパターン（正規表現）。")],
    replacement: typing.Annotated[
        str, pydantic.Field(description="置換後の文字列。`re.sub`互換で、`\\1`や`\\g<name>`でグループを参照できる。")
    ],
    paths: typing.Annotated[
        list[str], pydantic.Field(description="置換対象のファイルまたはディレクトリのパス一覧（1件以上）。")
    ],
    dry_run: typing.Annotated[
        bool,
        pydantic.Field(
            description=(
                "既定のTrueではファイルを変更せず変更内容だけを返す。"
                "Falseを明示した場合だけファイルへ書き込み、取り消し用の`replace_id`を返す。"
            ),
        ),
    ] = True,
    ignore_case: IgnoreCaseArg = False,
    smart_case: SmartCaseArg = False,
    fixed_strings: FixedStringsArg = False,
    word_regexp: WordRegexpArg = False,
    line_regexp: LineRegexpArg = False,
    multiline: typing.Annotated[
        bool, pydantic.Field(description="Trueで改行をまたぐ一致を許可する。`within`とは併用できない。")
    ] = False,
    within: typing.Annotated[
        str | None,
        pydantic.Field(
            description=(
                "置換する領域を決めるアンカーの正規表現。指定時はアンカーに一致した行と、"
                "その前後`before_context`・`after_context`行の範囲内に完全に収まる一致だけを置換する。"
            ),
        ),
    ] = None,
    before_context: typing.Annotated[
        int,
        pydantic.Field(description="`within`の領域でアンカー行の前に含める行数。`within`を指定した場合だけ使える。"),
    ] = 0,
    after_context: typing.Annotated[
        int,
        pydantic.Field(description="`within`の領域でアンカー行の後に含める行数。`within`を指定した場合だけ使える。"),
    ] = 0,
    context: typing.Annotated[
        int | None,
        pydantic.Field(description="`within`の領域の前後行数の一括指定。`within`を指定した場合だけ使える。"),
    ] = None,
    types: TypesArg = None,
    globs: GlobsArg = None,
    encoding: typing.Annotated[str, pydantic.Field(description="ファイルを読み書きする文字コード。")] = "utf-8",
    max_filesize: MaxFilesizeArg = None,
    exclude_files: typing.Annotated[
        list[str] | None, pydantic.Field(description="置換対象から除くファイルパスの一覧。")
    ] = None,
    from_grep: typing.Annotated[
        str | None,
        pydantic.Field(
            description=(
                "`pyfltr grep --output-format=jsonl`の出力を保存したファイルのパス。"
                "そこに現れるファイルへ対象を限定する。縮約された出力は拒否する。"
            ),
        ),
    ] = None,
    no_exclude: pyfltr.cli.mcp_models.NoExcludeArg = False,
    no_gitignore: pyfltr.cli.mcp_models.NoGitignoreArg = False,
    show_changes: typing.Annotated[
        bool, pydantic.Field(description="Trueで`changes`へ置換箇所ごとの変更前後の行を含める。")
    ] = False,
) -> ReplaceResultModel:
    r"""指定ファイル群へ正規表現置換を適用し、変更内容を返す。

    `dry_run=True`（既定）はファイルを変更せず変更内容のみを返す。
    `dry_run=False`を明示した場合のみ実書き込みし、`replace_id`を返す。
    `dry_run`の既定値がCLI（`False`）と異なるのはLLM暴発防止のため
    （`.claude/skills/grep-replace/SKILL.md`参照）。
    """
    if not paths:
        pyfltr.cli.mcp_errors.raise_mcp_error("paths を 1 件以上指定してください。")

    # CLIの`-A`/`-B`/`-C`拒否方針と対称に、`within`なしのコンテキスト指定を拒否する。
    if within is None and (before_context or after_context or context is not None):
        pyfltr.cli.mcp_errors.raise_mcp_error("before_context / after_context / context は within と併用してください。")
    # `within`は行範囲で領域を定めるためマルチラインとは併用不可。
    if within is not None and multiline:
        pyfltr.cli.mcp_errors.raise_mcp_error(
            "within と multiline は併用できません。withinを省略するかmultiline=Falseを指定してください。"
        )

    pyfltr.warnings_.clear()
    request = pyfltr.grep_.operations.ReplaceRequest(
        pattern=pattern,
        replacement=replacement,
        dry_run=dry_run,
        within=within,
        exclude_files=[pathlib.Path(path) for path in exclude_files or []],
        from_grep=pathlib.Path(from_grep) if from_grep is not None else None,
        targets=pyfltr.grep_.operations.TargetRequest.from_paths(paths, types or [], globs or [], no_exclude, no_gitignore),
        **pyfltr.grep_.operations.PatternOptions.arguments(
            (fixed_strings, ignore_case, smart_case, word_regexp, line_regexp, multiline),
            (before_context, after_context, context),
            encoding,
            max_filesize,
        ),
    )
    try:
        operation = pyfltr.grep_.operations.execute_replace(request)
    except pyfltr.grep_.history.ReplaceFailure as exc:
        pyfltr.cli.mcp_errors.raise_mcp_error(exc.describe(undo=f"`replace_undo(replace_id={exc.replace_id!r}, force=True)`"))
    except pyfltr.grep_.operations.TargetConfigError as exc:
        pyfltr.cli.mcp_errors.raise_mcp_error(pyfltr.config.config.format_config_error(exc, retry="`replace`ツール"))
    except ValueError as exc:
        pyfltr.cli.mcp_errors.raise_mcp_error(
            f"{exc}。正規表現の該当位置を修正するか、固定文字列には fixed_strings=True を指定してください。"
            "from_grepの読込失敗時は指定パスと読み取り権限を確認してください。"
        )
    replace_id = operation.replace_id
    file_changes = [
        ReplaceFileChangeModel(
            file=pyfltr.paths.normalize_separators(file),
            count=result.count,
        )
        for file, result in operation.prepared
    ]
    change_records = (
        [
            ReplaceChangeRecordModel(
                file=pyfltr.paths.normalize_separators(record.file),
                line=record.line,
                col=record.col,
                before_line=record.before_line,
                after_line=record.after_line,
            )
            for _file, result in operation.prepared
            for record in result.records
        ]
        if show_changes
        else []
    )
    files_changed = len(operation.prepared)
    total_replacements = sum(result.count for _file, result in operation.prepared)
    return ReplaceResultModel(
        replace_id=replace_id,
        dry_run=dry_run,
        files_changed=files_changed,
        total_replacements=total_replacements,
        file_changes=file_changes,
        changes=change_records,
        exit_code=0,
        fully_excluded_files=pyfltr.warnings_.filtered_direct_files(reason="excluded"),
        missing_targets=pyfltr.warnings_.filtered_direct_files(reason="missing"),
        warnings=[pyfltr.warnings_.format_warning_text(entry) for entry in pyfltr.warnings_.collected_warnings()],
    )


async def tool_replace_undo(
    replace_id: typing.Annotated[
        str,
        pydantic.Field(description="取り消すreplaceの識別子（ULID）。`replace`の応答か`replace_history`で確認できる。"),
    ],
    force: typing.Annotated[
        bool,
        pydantic.Field(description="Trueで、replace後に編集された（ハッシュが一致しない）ファイルも変更前の内容へ戻す。"),
    ] = False,
) -> ReplaceUndoModel:
    """保存済みreplace履歴からファイルを変更前の内容へ復元する。

    `force=True`を指定しない限り、手動編集済み（ハッシュ不一致）のファイルはスキップする。
    スキップが発生した場合は`exit_code=1`を返す。クライアント側で`force=True`再呼び出しの
    判断材料にする。
    """
    store = pyfltr.grep_.history.ReplaceHistoryStore()
    try:
        restored, skipped, warnings = store.undo_replace(replace_id, force=force)
        warnings = [f"{warning}。復元後のファイルを読み直し、内容と改行の差分を確認してください。" for warning in warnings]
    except pyfltr.grep_.history.ReplaceFailure as exc:
        pyfltr.cli.mcp_errors.raise_mcp_error(exc.describe(undo=f"`replace_undo(replace_id={replace_id!r}, force=True)`"))
    except FileNotFoundError:
        pyfltr.cli.mcp_errors.raise_mcp_error(
            pyfltr.grep_.history.format_replace_id_not_found(replace_id, list_history="`replace_history`ツール")
        )
    except (UnicodeDecodeError, OSError) as exc:
        pyfltr.cli.mcp_errors.raise_mcp_error(
            pyfltr.grep_.history.format_history_unreadable(replace_id, store.history_root / replace_id, exc)
        )

    exit_code = 1 if skipped else 0
    if skipped:
        warnings = [*warnings, pyfltr.grep_.history.format_undo_skipped(len(skipped), force="`force=True`")]
    return ReplaceUndoModel(
        replace_id=replace_id,
        restored=[pyfltr.paths.normalize_separators(p) for p in restored],
        skipped=[pyfltr.paths.normalize_separators(p) for p in skipped],
        exit_code=exit_code,
        warnings=warnings,
    )


async def tool_replace_history(
    action: typing.Annotated[
        typing.Literal["list", "show"],
        pydantic.Field(description="`list`は履歴を新しい順に一覧し、`show`は`replace_id`の1件を返す。"),
    ] = "list",
    replace_id: typing.Annotated[str | None, pydantic.Field(description='`action="show"`で必須のreplace識別子。')] = None,
    limit: typing.Annotated[int | None, pydantic.Field(description='`action="list"`で返す最大件数。省略時は全件。')] = None,
) -> ReplaceHistoryModel:
    """replace履歴を一覧または単体で参照する。

    対応CLI: `pyfltr replace --list-history` / `pyfltr replace --show-history <ID>`
    """
    pyfltr.warnings_.clear()
    if action not in ("list", "show"):
        pyfltr.cli.mcp_errors.raise_mcp_error("action は list / show のいずれかを指定してください。")
    if action == "show" and replace_id is None:
        pyfltr.cli.mcp_errors.raise_mcp_error('action="show"では replace_id を指定してください。')

    store = pyfltr.grep_.history.ReplaceHistoryStore()
    if action == "list":
        raw_entries = store.list_replaces(limit=limit)
    else:
        try:
            raw_entries = [store.load_replace(typing.cast(str, replace_id))]
        except FileNotFoundError:
            pyfltr.cli.mcp_errors.raise_mcp_error(
                pyfltr.grep_.history.format_replace_id_not_found(
                    typing.cast(str, replace_id), list_history='`replace_history`ツールの`action="list"`'
                )
            )

    entries = [
        ReplaceHistoryEntryModel(
            replace_id=str(entry["replace_id"]),
            saved_at=entry.get("saved_at"),
            command={
                key: value for key, value in dict(entry.get("command", {})).items() if key in REPLACE_HISTORY_COMMAND_KEYS
            },
            files=[
                ReplaceHistoryFileModel(
                    file=str(file_entry["file"]),
                    records_count=int(file_entry.get("records_count", 0)),
                )
                for file_entry in entry.get("files", [])
            ],
        )
        for entry in raw_entries
    ]
    return ReplaceHistoryModel(action=action, entries=entries)
