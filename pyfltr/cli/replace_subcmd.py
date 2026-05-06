"""`replace`サブコマンドの登録と実行本体。

`pyfltr/grep_/`配下のコアロジック（パターン構築・置換適用・履歴管理）を呼び出す薄いCLI層。
履歴照会（`--list-history` / `--show-history=<id>`）と取り消し（`--undo`）を含む。
"""

from __future__ import annotations

import argparse
import json
import logging
import pathlib
import sys
import typing

import pyfltr.cli.output_format
import pyfltr.command.targets
import pyfltr.config.config
import pyfltr.grep_.history
import pyfltr.grep_.jsonl_records
import pyfltr.grep_.matcher
import pyfltr.grep_.replacer
import pyfltr.grep_.scanner
import pyfltr.grep_.text_render
import pyfltr.warnings_
from pyfltr.grep_.types import ReplaceCommandMeta

# replace_subcmdは`logging.WARNING`レベルを`configure_text_output`へ渡すため、
# `logging`モジュールのimport自体は維持する

_OUTPUT_FORMATS: tuple[str, ...] = ("text", "json", "jsonl")
_VALID_OUTPUT_FORMATS: frozenset[str] = frozenset(_OUTPUT_FORMATS)


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
    parser.add_argument("--hidden", action="store_true", help="ドットファイルも対象に含める。")

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

    # 共通オプション
    parser.add_argument(
        "--no-exclude",
        action="store_true",
        help="exclude / extend-exclude による除外を無効化する。",
    )
    parser.add_argument(
        "--no-gitignore",
        action="store_true",
        help=".gitignore による除外を無効化する。",
    )
    parser.add_argument(
        "--output-format",
        choices=_OUTPUT_FORMATS,
        default=None,
        help="出力形式を指定する（text / json / jsonl、既定: text）。",
    )
    parser.add_argument(
        "--output-file",
        type=pathlib.Path,
        default=None,
        help="JSONL / json出力先ファイル。未指定時は stdout に出力する。",
    )


def execute_replace(parser: argparse.ArgumentParser, args: argparse.Namespace) -> int:
    """`replace`サブコマンドの処理本体。"""
    resolution = pyfltr.cli.output_format.resolve_output_format(
        parser,
        args.output_format,
        valid_values=_VALID_OUTPUT_FORMATS,
        ai_agent_default="jsonl",
    )
    output_format = resolution.format

    if output_format == "text":
        pyfltr.cli.output_format.configure_text_output(sys.stdout)
    else:
        pyfltr.cli.output_format.configure_text_output(sys.stderr, level=logging.WARNING)

    if output_format == "jsonl":
        if args.output_file is not None:
            pyfltr.cli.output_format.configure_structured_output(args.output_file)
        else:
            pyfltr.cli.output_format.configure_structured_output(sys.stdout)
    else:
        pyfltr.cli.output_format.configure_structured_output(None)

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

    try:
        compiled = pyfltr.grep_.matcher.compile_pattern(
            [args.pattern],
            fixed_strings=args.fixed_strings,
            ignore_case=args.ignore_case,
            smart_case=args.smart_case,
            word_regexp=args.word_regexp,
            line_regexp=args.line_regexp,
            multiline=args.multiline,
        )
    except ValueError as exc:
        parser.error(str(exc))

    try:
        config = pyfltr.config.config.load_config()
    except (ValueError, OSError) as exc:
        sys.stderr.write(f"設定エラー: {exc}\n")
        return 1
    if args.no_exclude:
        config.values["exclude"] = []
        config.values["extend-exclude"] = []
    if args.no_gitignore:
        config.values["respect-gitignore"] = False

    targets = list(args.paths) if args.paths else []
    expanded = pyfltr.command.targets.expand_all_files(targets, config)
    expanded = pyfltr.grep_.scanner.filter_files_by_type(expanded, args.type)
    expanded = pyfltr.grep_.scanner.filter_by_globs(expanded, args.glob)

    if not args.hidden:
        expanded = [p for p in expanded if not _has_hidden_segment(p)]

    # `--exclude-file` / `--from-grep` での対象限定
    excluded = {pathlib.Path(p).resolve() for p in args.exclude_file}
    if excluded:
        expanded = [p for p in expanded if p.resolve() not in excluded]
    if args.from_grep is not None:
        allowed = _read_from_grep(parser, args.from_grep)
        expanded = [p for p in expanded if p.resolve() in allowed]

    files_count = len(expanded)
    dry_run = args.dry_run
    replace_id = pyfltr.grep_.history.generate_replace_id() if not dry_run else None

    if output_format == "jsonl":
        pyfltr.grep_.jsonl_records.emit_replace_header(
            pattern=args.pattern,
            replacement=args.replacement,
            files=files_count,
            replace_id=replace_id,
            dry_run=dry_run,
            format_source=resolution.source,
        )

    file_changes: list[dict[str, typing.Any]] = []
    total_replacements = 0
    files_changed = 0
    read_failures = 0
    json_records: list[dict[str, typing.Any]] = []
    for file in expanded:
        # MCP側の_tool_replace（mcp_server.py）と挙動を揃える目的で、
        # `--max-filesize`超過ファイルは読み込み前にスキップする。
        if args.max_filesize is not None and args.max_filesize > 0:
            try:
                if file.stat().st_size > args.max_filesize:
                    continue
            except OSError:
                continue
        try:
            before, after, count, records = pyfltr.grep_.replacer.apply_replace_to_file(
                file,
                compiled,
                args.replacement,
                encoding=args.encoding,
            )
        except (UnicodeDecodeError, OSError) as exc:
            sys.stderr.write(f"warning: 読み込みに失敗したためスキップしました: {file}: {exc}\n")
            read_failures += 1
            continue
        if count == 0:
            continue
        files_changed += 1
        total_replacements += count
        before_hash = pyfltr.grep_.replacer.compute_hash(before)
        after_hash = pyfltr.grep_.replacer.compute_hash(after)
        if not dry_run:
            file.write_text(after, encoding=args.encoding)
            file_changes.append(
                {
                    "file": file,
                    "before_content": before,
                    "after_hash": after_hash,
                    "records": list(records),
                }
            )

        if output_format == "jsonl":
            pyfltr.grep_.jsonl_records.emit_file_change(
                file=file,
                count=count,
                before_hash=before_hash,
                after_hash=after_hash,
                dry_run=dry_run,
                records=list(records),
                show_changes=args.show_changes,
            )
        elif output_format == "text":
            pyfltr.grep_.text_render.render_file_change(file=file, count=count, dry_run=dry_run)
            if args.show_changes:
                for record in records:
                    pyfltr.grep_.text_render.render_change_diff(record)
        else:  # json
            entry: dict[str, typing.Any] = {
                "file": str(file),
                "count": count,
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
                    for r in records
                ]
            json_records.append(entry)

    # 履歴保存
    if not dry_run and file_changes and replace_id is not None:
        meta = ReplaceCommandMeta(
            replace_id=replace_id,
            dry_run=False,
            fixed_strings=args.fixed_strings,
            pattern=args.pattern,
            replacement=args.replacement,
            encoding=args.encoding,
        )
        store = pyfltr.grep_.history.ReplaceHistoryStore()
        store.save_replace(replace_id, command_meta=meta, file_changes=file_changes)
        store.cleanup(pyfltr.grep_.history.policy_from_config(config))

    guidance = _build_replace_guidance(replace_id=replace_id, files_changed=files_changed, dry_run=dry_run)
    # 失敗判定は「ファイル読み込み失敗が1件以上発生したか」で行う。
    # 書き込みエラーは現状捕捉対象外で、呼び出し側のOSError例外として上位へ伝播する。
    exit_code = 1 if read_failures > 0 else 0

    if output_format == "jsonl":
        # 走査・読み込み中に蓄積された警告をsummary直前に出力し、
        # pipelineのwarning出力位置と挙動を揃える
        for warning_entry in pyfltr.warnings_.collected_warnings():
            pyfltr.grep_.jsonl_records.emit_warning(warning_entry)
        pyfltr.grep_.jsonl_records.emit_replace_summary(
            files_changed=files_changed,
            total_replacements=total_replacements,
            exit_code=exit_code,
            replace_id=replace_id,
            dry_run=dry_run,
            guidance=guidance if guidance else None,
        )
    elif output_format == "json":
        payload: dict[str, typing.Any] = {
            "changes": json_records,
            "summary": {
                "files_changed": files_changed,
                "total_replacements": total_replacements,
                "dry_run": dry_run,
            },
        }
        if replace_id is not None:
            payload["summary"]["replace_id"] = replace_id
        if guidance:
            payload["summary"]["guidance"] = guidance
        _print_json(payload, args.output_file)
    else:
        pyfltr.grep_.text_render.render_replace_summary(
            files_changed=files_changed,
            total_replacements=total_replacements,
            dry_run=dry_run,
            replace_id=replace_id,
        )
        if guidance:
            pyfltr.grep_.text_render.render_replace_guidance(guidance)

    return exit_code


def _has_hidden_segment(path: pathlib.Path) -> bool:
    """パス内に`.`始まりのセグメントが含まれるか判定する。"""
    for part in path.parts:
        if part in (".", ".."):
            continue
        if part.startswith("."):
            return True
    return False


def _read_from_grep(parser: argparse.ArgumentParser, jsonl_path: pathlib.Path) -> set[pathlib.Path]:
    """grep出力JSONLから`kind=match`のファイル集合を抽出する。"""
    try:
        text = jsonl_path.read_text(encoding="utf-8")
    except OSError as exc:
        parser.error(f"--from-grep の読み込みに失敗しました: {jsonl_path}: {exc}")
    files: set[pathlib.Path] = set()
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            continue
        if record.get("kind") != "match":
            continue
        file = record.get("file")
        if isinstance(file, str):
            files.add(pathlib.Path(file).resolve())
    return files


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
        _print_json({"history": entries}, output_file)
        return 0
    # text
    if not entries:
        with pyfltr.cli.output_format.text_output_lock:
            pyfltr.cli.output_format.text_logger.info("(no replace history)")
        return 0
    with pyfltr.cli.output_format.text_output_lock:
        for entry in entries:
            pyfltr.cli.output_format.text_logger.info(
                f"{entry.get('replace_id')}\t{entry.get('saved_at')}\tfiles={len(entry.get('files') or [])}"
            )
    return 0


def _execute_show_history(replace_id: str, output_format: str, output_file: pathlib.Path | None) -> int:
    """`--show-history=ID` 時の表示。"""
    store = pyfltr.grep_.history.ReplaceHistoryStore()
    try:
        meta = store.load_replace(replace_id)
    except FileNotFoundError:
        sys.stderr.write(f"エラー: replace_id が見つかりません: {replace_id}\n")
        return 1
    if output_format == "jsonl":
        pyfltr.grep_.jsonl_records.emit_replace_history(meta)
        return 0
    if output_format == "json":
        _print_json(meta, output_file)
        return 0
    with pyfltr.cli.output_format.text_output_lock:
        pyfltr.cli.output_format.text_logger.info(f"replace_id: {meta.get('replace_id')}")
        pyfltr.cli.output_format.text_logger.info(f"saved_at: {meta.get('saved_at')}")
        cmd = meta.get("command") or {}
        pyfltr.cli.output_format.text_logger.info(
            f"command: pattern={cmd.get('pattern')!r} replacement={cmd.get('replacement')!r}"
        )
        for file_entry in meta.get("files", []):
            pyfltr.cli.output_format.text_logger.info(f"  {file_entry.get('file')} (records={file_entry.get('records_count')})")
    return 0


def _execute_undo(parser: argparse.ArgumentParser, args: argparse.Namespace, output_format: str) -> int:
    """`--undo` 時の処理。"""
    if args.pattern is None:
        parser.error("`--undo` 指定時は復元対象の replace_id を位置引数として渡してください。")
    replace_id = args.pattern
    store = pyfltr.grep_.history.ReplaceHistoryStore()
    try:
        restored, skipped = store.undo_replace(replace_id, force=args.force)
    except FileNotFoundError:
        sys.stderr.write(f"エラー: replace_id が見つかりません: {replace_id}\n")
        return 1
    except UnicodeDecodeError as exc:
        # 履歴メタJSONや保存済み変更前ファイルのデコード失敗（保存後にディレクトリ構造を
        # 直接破壊された場合等）を捕捉する
        sys.stderr.write(f"エラー: 履歴のデコードに失敗しました: {replace_id}: {exc}\n")
        return 1
    except OSError as exc:
        # 履歴ディレクトリへのアクセス失敗（権限エラー・ストレージ障害等）を捕捉する
        sys.stderr.write(f"エラー: 履歴の読み込みに失敗しました: {replace_id}: {exc}\n")
        return 1

    exit_code = 1 if skipped else 0
    if skipped:
        sys.stderr.write(
            f"warning: undo で {len(skipped)} 件のファイルが手動編集後の状態のためスキップされました。"
            " --force で強制復元できます。\n"
        )

    if output_format == "jsonl":
        pyfltr.grep_.jsonl_records.emit_replace_undo_summary(
            replace_id=replace_id,
            restored=restored,
            skipped=skipped,
            exit_code=exit_code,
        )
    elif output_format == "json":
        _print_json(
            {
                "replace_id": replace_id,
                "restored": [str(p) for p in restored],
                "skipped": [str(p) for p in skipped],
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


def _print_json(payload: dict[str, typing.Any], output_file: pathlib.Path | None) -> None:
    """単発JSONをstdoutまたは`--output-file`に書く。"""
    text = json.dumps(payload, ensure_ascii=False, indent=2)
    if output_file is not None:
        output_file.parent.mkdir(parents=True, exist_ok=True)
        output_file.write_text(text + "\n", encoding="utf-8")
    else:
        sys.stdout.write(text + "\n")
        sys.stdout.flush()
