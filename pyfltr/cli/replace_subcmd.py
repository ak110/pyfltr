"""`replace`サブコマンドの登録と実行本体。

`pyfltr/grep_/`配下のコアロジック（パターン構築・置換適用・履歴管理）を呼び出す薄いCLI層。
履歴照会（`--list-history` / `--show-history=<id>`）と取り消し（`--undo`）を含む。
"""

from __future__ import annotations

import argparse
import contextlib
import pathlib
import sys
import typing

import pyfltr.cli.grep_replace_common
import pyfltr.cli.output_format
import pyfltr.grep_.history
import pyfltr.grep_.jsonl_records
import pyfltr.grep_.matcher
import pyfltr.grep_.operations
import pyfltr.grep_.replacer
import pyfltr.grep_.scanner
import pyfltr.grep_.text_render
import pyfltr.output.logging_
import pyfltr.paths
import pyfltr.warnings_


def register_subparsers(subparsers: typing.Any) -> None:
    """`replace`サブパーサーを登録する。"""
    parser = subparsers.add_parser(
        "replace",
        help="grepと同じ引数体系で正規表現置換を実行する（履歴保存・undo対応）。",
    )
    # 位置引数: pattern + replacement + paths
    parser.add_argument(
        "pattern",
        nargs="?",
        default=None,
        help="検索パターン（正規表現）。`--undo` / `--list-history` / `--show-history`時は省略可。",
    )
    parser.add_argument(
        "replacement",
        nargs="?",
        default=None,
        help="置換式（`re.sub`互換、`\\1`/`\\g<name>`参照可）。",
    )
    parser.add_argument(
        "paths",
        nargs="*",
        type=pathlib.Path,
        help="対象のファイルまたはディレクトリ（既定: カレントディレクトリ）。",
    )

    # grepと共通のパターン関連オプション
    parser.add_argument("-F", "--fixed-strings", action="store_true", help="パターンを固定文字列として扱う。")
    parser.add_argument("-i", "--ignore-case", action="store_true", help="大文字小文字を区別しない。")
    parser.add_argument(
        "-S",
        "--smart-case",
        action="store_true",
        help="パターンに大文字を含まない場合のみ大文字小文字を区別しない。",
    )
    parser.add_argument("-w", "--word-regexp", action="store_true", help="単語境界で囲まれたマッチのみ採用する。")
    parser.add_argument("-x", "--line-regexp", action="store_true", help="行全体に一致したマッチのみ採用する。")
    parser.add_argument("-U", "--multiline", action="store_true", help="マルチラインマッチを有効化する。")
    # ブロック内限定置換（sedの範囲アドレス相当）。`--within`でアンカーを指定し、
    # アンカー行＋前後コンテキスト（`-A`/`-B`/`-C`）で定まる行範囲内のみ置換する。
    parser.add_argument(
        "--within",
        default=None,
        metavar="ANCHOR",
        help="アンカー正規表現にマッチした行とその前後（`-A`/`-B`/`-C`）で定まる領域内のみ置換する。",
    )
    parser.add_argument(
        "-A", "--after-context", type=int, default=0, metavar="N", help="`--within`領域のアンカー行の後ろN行を含める。"
    )
    parser.add_argument(
        "-B", "--before-context", type=int, default=0, metavar="N", help="`--within`領域のアンカー行の前N行を含める。"
    )
    parser.add_argument(
        "-C",
        "--context",
        type=int,
        default=None,
        metavar="N",
        help="`--within`領域のアンカー行の前後N行を含める（`-A`/`-B`を一括指定）。",
    )
    parser.add_argument(
        "--type",
        action="append",
        default=[],
        metavar="TYPE",
        help="特定言語タイプのファイルのみ対象化する。",
    )
    parser.add_argument("-g", "--glob", action="append", default=[], metavar="PAT", help="globパターンで対象を限定する。")
    parser.add_argument("--encoding", default="utf-8", help="ファイル読み込み・書き込み時のエンコーディング。")
    parser.add_argument(
        "--max-filesize",
        type=int,
        default=None,
        metavar="BYTES",
        help="走査対象ファイルサイズの上限（バイト単位）。",
    )
    # replace固有
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="ファイル書き込みをスキップして変更内容のみを出力する。",
    )
    parser.add_argument(
        "--show-changes",
        action="store_true",
        help="各置換箇所の変更前後の行を併せて表示する。",
    )
    parser.add_argument(
        "--exclude-file",
        action="append",
        default=[],
        metavar="PATH",
        help="置換対象から除外するファイルパス（複数指定可）。",
    )
    parser.add_argument(
        "--from-grep",
        type=pathlib.Path,
        default=None,
        metavar="PATH",
        help="grep出力JSONLを読み込み、`kind=match`のファイル集合に対象を限定する。",
    )
    parser.add_argument(
        "--undo",
        action="store_true",
        help="保存済み履歴IDを指定してreplaceを取り消す。`pattern`位置に履歴IDを渡す。",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="`--undo`時、対象ファイルが手動編集されていても強制復元する。",
    )
    parser.add_argument(
        "--list-history",
        action="store_true",
        help="保存済みreplace履歴の一覧を表示する。",
    )
    parser.add_argument(
        "--show-history",
        default=None,
        metavar="ID",
        help="指定replace_idの詳細（meta + ファイル一覧）を表示する。",
    )

    # 共通オプション（grep/replaceで共有。詳細は`grep_replace_common`のdocstringを参照）
    pyfltr.cli.grep_replace_common.add_common_output_args(parser)


def execute_replace(parser: argparse.ArgumentParser, args: argparse.Namespace) -> int:
    """`replace`サブコマンドの処理本体。"""
    # warnings_はモジュールグローバルに蓄積するため、実行開始時に初期化する
    pyfltr.warnings_.clear()
    # 出力形式の解決とtext logger / structured loggerの出力先設定
    resolution = pyfltr.cli.grep_replace_common.setup_output(parser, args)
    output_format = resolution.format
    warning_scope = pyfltr.warnings_.defer_stderr() if output_format == "jsonl" else contextlib.nullcontext()
    with warning_scope:
        return _execute_replace(parser, args, resolution=resolution)


def _execute_replace(
    parser: argparse.ArgumentParser,
    args: argparse.Namespace,
    *,
    resolution: pyfltr.cli.output_format.OutputFormatResolution,
) -> int:
    """警告配送スコープ内でreplaceを実行する。"""
    output_format = resolution.format

    # 履歴照会・undo モードを先に捌く（位置引数の意味が変わるため）
    if args.list_history:
        return _execute_list_history(output_format, args.output_file)
    if args.show_history is not None:
        return _execute_show_history(args.show_history, output_format, args.output_file)
    if args.undo:
        return _execute_undo(parser, args, output_format)

    # 通常モード: pattern と replacement が必須
    if args.pattern is None or args.replacement is None:
        parser.error("`pattern`と`replacement`の両方を指定してください。")

    # `--within`なしの`-A`/`-B`/`-C`はgrepからの引数転用時に意味差で誤動作させるため拒否する。
    # grepでは表示コンテキスト幅、replaceでは`--within`領域幅と意味が異なる。
    if args.within is None and (args.after_context or args.before_context or args.context is not None):
        parser.error("`-A`/`-B`/`-C` は `--within` と併用してください。")
    # `--within`は行範囲で領域を定めるため、行境界を跨ぐマルチライン検索とは併用不可。
    if args.within is not None and args.multiline:
        parser.error("`--within` と `-U/--multiline` は併用できません。")

    def emit_header(replace_id: str | None, files_count: int) -> None:
        if output_format == "jsonl":
            pyfltr.grep_.jsonl_records.emit_replace_header(
                pattern=args.pattern,
                replacement=args.replacement,
                files=files_count,
                replace_id=replace_id,
                dry_run=args.dry_run,
                format_source=resolution.source,
            )

    request = pyfltr.grep_.operations.ReplaceRequest(
        pattern=args.pattern,
        replacement=args.replacement,
        dry_run=args.dry_run,
        within=args.within,
        exclude_files=[pathlib.Path(path) for path in args.exclude_file],
        from_grep=args.from_grep,
        **pyfltr.cli.grep_replace_common.pattern_arguments(args),
    )
    try:
        operation = pyfltr.grep_.operations.execute_replace(request, on_start=emit_header)
    except pyfltr.grep_.operations.TargetConfigError as exc:
        sys.stderr.write(f"{exc}\n")
        return 1
    except pyfltr.grep_.history.ReplaceFailure as exc:
        message = exc.describe(undo=f"`pyfltr replace --undo {exc.replace_id} --force`")
        sys.stderr.write(f"エラー: {message}\n")
        return 1
    except ValueError as exc:
        parser.error(str(exc))
    dry_run = args.dry_run
    replace_id = operation.replace_id
    prepared = operation.prepared
    read_failures = operation.read_failures
    files_changed = 0
    total_replacements = 0
    json_records: list[dict[str, typing.Any]] = []

    for file, result in prepared:
        files_changed += 1
        total_replacements += result.count
        before_hash = pyfltr.grep_.replacer.compute_hash(result.before_content)
        after_hash = pyfltr.grep_.replacer.compute_hash(result.after_content)
        if output_format == "jsonl":
            pyfltr.grep_.jsonl_records.emit_file_change(
                file=file,
                count=result.count,
                before_hash=before_hash,
                after_hash=after_hash,
                dry_run=dry_run,
                records=list(result.records),
                show_changes=args.show_changes,
            )
        elif output_format == "text":
            pyfltr.grep_.text_render.render_file_change(file=file, count=result.count, dry_run=dry_run)
            if args.show_changes:
                for record in result.records:
                    pyfltr.grep_.text_render.render_change_diff(record)
        else:  # json
            entry: dict[str, typing.Any] = {
                "file": pyfltr.paths.normalize_separators(file),
                "count": result.count,
                "before_hash": before_hash,
                "after_hash": after_hash,
                "dry_run": dry_run,
            }
            if args.show_changes:
                entry["changes"] = [
                    {
                        "line": r.line,
                        "col": r.col,
                        "before_line": r.before_line,
                        "after_line": r.after_line,
                    }
                    for r in result.records
                ]
            json_records.append(entry)

    guidance = _build_replace_guidance(replace_id=replace_id, files_changed=files_changed, dry_run=dry_run)
    # 失敗判定は「ファイル読み込み失敗が1件以上発生したか」で行う。
    exit_code = 1 if read_failures > 0 else 0

    # 直接指定が除外・不在で対象外になった一覧をsummaryへ載せる。
    fully_excluded = pyfltr.warnings_.filtered_direct_files(reason="excluded")
    missing_targets = pyfltr.warnings_.filtered_direct_files(reason="missing")

    if output_format == "jsonl":
        # 走査・読み込み中に蓄積された警告をsummary直前に出力し、
        # pipelineのwarning出力位置と挙動を揃える
        warning_entries = pyfltr.warnings_.collected_warnings()
        for warning_entry in warning_entries:
            pyfltr.grep_.jsonl_records.emit_warning(warning_entry)
        pyfltr.grep_.jsonl_records.emit_replace_summary(
            files_changed=files_changed,
            total_replacements=total_replacements,
            exit_code=exit_code,
            replace_id=replace_id,
            dry_run=dry_run,
            guidance=guidance if guidance else None,
            fully_excluded_files=fully_excluded,
            missing_targets=missing_targets,
            warning_count=len(warning_entries),
        )
        pyfltr.warnings_.mark_delivered(warning_entries)
    elif output_format == "json":
        replace_summary: dict[str, typing.Any] = {
            "files_changed": files_changed,
            "total_replacements": total_replacements,
            "dry_run": dry_run,
        }
        if replace_id is not None:
            replace_summary["replace_id"] = replace_id
        if guidance:
            replace_summary["guidance"] = guidance
        if fully_excluded:
            replace_summary["fully_excluded_files"] = fully_excluded
        if missing_targets:
            replace_summary["missing_targets"] = missing_targets
        payload: dict[str, typing.Any] = {
            "changes": json_records,
            "summary": replace_summary,
        }
        pyfltr.cli.grep_replace_common.print_json(payload, args.output_file)
    else:
        pyfltr.grep_.text_render.render_filtered_sections(
            warnings=pyfltr.warnings_.collected_warnings(),
            missing_targets=missing_targets,
            fully_excluded_files=fully_excluded,
        )
        pyfltr.grep_.text_render.render_replace_summary(
            files_changed=files_changed,
            total_replacements=total_replacements,
            dry_run=dry_run,
            replace_id=replace_id,
        )
        if guidance:
            pyfltr.grep_.text_render.render_replace_guidance(guidance)

    return exit_code


def _build_replace_guidance(
    *,
    replace_id: str | None,
    files_changed: int,
    dry_run: bool,
) -> list[str]:
    """replace完了時のガイダンス文（英語）を組み立てる。

    実書き込み成功時のみundo案内を表示する。dry-run時は実書き込みコマンドへの誘導を案内する。
    """
    if files_changed == 0:
        return []
    if dry_run:
        return ["Dry-run only; rerun without --dry-run to write changes."]
    if replace_id is None:
        return []
    return [
        f"Use 'pyfltr replace --undo {replace_id}' to revert this change.",
        "Use 'pyfltr replace --list-history' to inspect saved replace history.",
    ]


def _execute_list_history(output_format: str, output_file: pathlib.Path | None) -> int:
    """`--list-history` 時の表示。"""
    store = pyfltr.grep_.history.ReplaceHistoryStore()
    entries = store.list_replaces()
    if output_format == "jsonl":
        for entry in entries:
            pyfltr.grep_.jsonl_records.emit_replace_history(entry)
        return 0
    if output_format == "json":
        pyfltr.cli.grep_replace_common.print_json({"history": entries}, output_file)
        return 0
    # text
    if not entries:
        with pyfltr.output.logging_.text_output_lock:
            pyfltr.output.logging_.text_logger.info("(no replace history)")
        return 0
    with pyfltr.output.logging_.text_output_lock:
        for entry in entries:
            pyfltr.output.logging_.text_logger.info(
                f"{entry.get('replace_id')}\t{entry.get('saved_at')}\tfiles={len(entry.get('files') or [])}"
            )
    return 0


def _execute_show_history(replace_id: str, output_format: str, output_file: pathlib.Path | None) -> int:
    """`--show-history=ID` 時の表示。"""
    store = pyfltr.grep_.history.ReplaceHistoryStore()
    try:
        meta = store.load_replace(replace_id)
    except FileNotFoundError:
        sys.stderr.write(
            "エラー: "
            + pyfltr.grep_.history.format_replace_id_not_found(replace_id, list_history="`pyfltr replace --list-history`")
            + "\n"
        )
        return 1
    if output_format == "jsonl":
        pyfltr.grep_.jsonl_records.emit_replace_history(meta)
        return 0
    if output_format == "json":
        pyfltr.cli.grep_replace_common.print_json(meta, output_file)
        return 0
    with pyfltr.output.logging_.text_output_lock:
        pyfltr.output.logging_.text_logger.info(f"replace_id: {meta.get('replace_id')}")
        pyfltr.output.logging_.text_logger.info(f"saved_at: {meta.get('saved_at')}")
        cmd = meta.get("command") or {}
        pyfltr.output.logging_.text_logger.info(
            f"command: pattern={cmd.get('pattern')!r} replacement={cmd.get('replacement')!r}"
        )
        for file_entry in meta.get("files", []):
            pyfltr.output.logging_.text_logger.info(f"  {file_entry.get('file')} (records={file_entry.get('records_count')})")
    return 0


def _execute_undo(parser: argparse.ArgumentParser, args: argparse.Namespace, output_format: str) -> int:
    """`--undo` 時の処理。"""
    if args.pattern is None:
        parser.error("`--undo` 指定時は復元対象の replace_id を位置引数として渡してください。")
    replace_id = args.pattern
    store = pyfltr.grep_.history.ReplaceHistoryStore()
    try:
        restored, skipped, history_warnings = store.undo_replace(replace_id, force=args.force)
    except pyfltr.grep_.history.ReplaceFailure as exc:
        sys.stderr.write(f"エラー: {exc.describe(undo=f'`pyfltr replace --undo {replace_id} --force`')}\n")
        return 1
    except FileNotFoundError:
        sys.stderr.write(
            "エラー: "
            + pyfltr.grep_.history.format_replace_id_not_found(replace_id, list_history="`pyfltr replace --list-history`")
            + "\n"
        )
        return 1
    except (UnicodeDecodeError, OSError) as exc:
        # 履歴メタJSONや保存済み変更前ファイルのデコード失敗（保存後にディレクトリ構造を
        # 直接破壊された場合等）と、履歴ディレクトリへのアクセス失敗（権限エラー・ストレージ障害等）を捕捉する
        message = pyfltr.grep_.history.format_history_unreadable(replace_id, store.history_root / replace_id, exc)
        sys.stderr.write(f"エラー: {message}\n")
        return 1

    exit_code = 1 if skipped else 0
    for warning in history_warnings:
        pyfltr.warnings_.emit_warning(source="replace-undo", message=warning)
    if skipped:
        pyfltr.warnings_.emit_warning(
            source="replace-undo",
            message=pyfltr.grep_.history.format_undo_skipped(len(skipped), force="--force"),
        )

    if output_format == "jsonl":
        warning_entries = pyfltr.warnings_.collected_warnings()
        for warning_entry in warning_entries:
            pyfltr.grep_.jsonl_records.emit_warning(warning_entry)
        pyfltr.grep_.jsonl_records.emit_replace_undo_summary(
            replace_id=replace_id,
            restored=restored,
            skipped=skipped,
            exit_code=exit_code,
        )
        pyfltr.warnings_.mark_delivered(warning_entries)
    elif output_format == "json":
        pyfltr.cli.grep_replace_common.print_json(
            {
                "replace_id": replace_id,
                "restored": [pyfltr.paths.normalize_separators(p) for p in restored],
                "skipped": [pyfltr.paths.normalize_separators(p) for p in skipped],
                "exit": exit_code,
            },
            args.output_file,
        )
    else:
        pyfltr.grep_.text_render.render_undo_summary(
            replace_id=replace_id,
            restored=restored,
            skipped=skipped,
        )
    return exit_code
