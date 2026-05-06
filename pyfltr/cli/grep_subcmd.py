"""`grep`サブコマンドの登録と実行本体。

`pyfltr/grep_/`配下のコアロジック（パターン構築・ファイル走査）を呼び出す薄いCLI層。
`pyfltr/.claude/rules/grep-replace.md`の引数体系（共通オプション名）に従い、
ripgrep流儀のオプション群を受理する。
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
import pyfltr.grep_.jsonl_records
import pyfltr.grep_.matcher
import pyfltr.grep_.scanner
import pyfltr.grep_.text_render
import pyfltr.warnings_
from pyfltr.grep_.types import MatchRecord

# grep_subcmdは`logging.WARNING`レベルを`configure_text_output`へ渡すため、
# `logging`モジュールのimport自体は維持する

_OUTPUT_FORMATS: tuple[str, ...] = ("text", "json", "jsonl")
_VALID_OUTPUT_FORMATS: frozenset[str] = frozenset(_OUTPUT_FORMATS)


def register_subparsers(subparsers: typing.Any) -> None:
    """`grep`サブパーサーを登録する。

    `subparsers`の`parser_class`は`build_parser`側で`_HelpOnErrorArgumentParser`に
    設定済みのため、`add_parser`直書きで自動継承される。
    """
    parser = subparsers.add_parser(
        "grep",
        help="ファイル群から正規表現に一致する行を検索する。",
    )
    # 位置引数: pattern + paths
    parser.add_argument(
        "pattern",
        nargs="?",
        default=None,
        help="検索パターン（正規表現）。`-e`/`-f`で複数指定する場合は省略可。",
    )
    parser.add_argument(
        "paths",
        nargs="*",
        type=pathlib.Path,
        help="検索対象のファイルまたはディレクトリ（既定: カレントディレクトリ）。",
    )

    # パターン関連オプション
    parser.add_argument(
        "-e",
        "--regexp",
        action="append",
        default=[],
        metavar="PATTERN",
        help="追加の検索パターン（複数指定可、`|`連結）。",
    )
    parser.add_argument(
        "-f",
        "--file",
        type=pathlib.Path,
        default=None,
        metavar="PATH",
        help="検索パターンを1行1パターンで記述したファイルを読み込む。",
    )
    parser.add_argument("-F", "--fixed-strings", action="store_true", help="パターンを固定文字列として扱う。")
    parser.add_argument("-i", "--ignore-case", action="store_true", help="大文字小文字を区別しない。")
    parser.add_argument(
        "-S",
        "--smart-case",
        action="store_true",
        help="パターンに大文字を含まない場合のみ大文字小文字を区別しない（ripgrep流儀）。",
    )
    parser.add_argument("-w", "--word-regexp", action="store_true", help="単語境界で囲まれたマッチのみ採用する。")
    parser.add_argument("-x", "--line-regexp", action="store_true", help="行全体に一致したマッチのみ採用する。")
    parser.add_argument(
        "-U",
        "--multiline",
        action="store_true",
        help="マルチラインマッチを有効化する（`.`が改行に一致、`^`/`$`が行単位で評価）。",
    )

    # コンテキスト・件数制限
    parser.add_argument("-A", "--after-context", type=int, default=0, metavar="N", help="マッチ行の後ろN行を出力する。")
    parser.add_argument("-B", "--before-context", type=int, default=0, metavar="N", help="マッチ行の前N行を出力する。")
    parser.add_argument(
        "-C",
        "--context",
        type=int,
        default=None,
        metavar="N",
        help="マッチ行の前後N行を出力する（`-A`/`-B`を一括指定）。",
    )
    parser.add_argument("-m", "--max-count", type=int, default=0, metavar="N", help="ファイル単位の最大マッチ件数。")
    parser.add_argument("--max-total", type=int, default=0, metavar="N", help="全体での最大マッチ件数（pyfltr独自）。")

    # ファイル選定オプション
    parser.add_argument(
        "--type",
        action="append",
        default=[],
        metavar="TYPE",
        help="特定言語タイプのファイルのみ対象化する（python/rust/ts/js/md/json/toml/yaml/shell）。",
    )
    parser.add_argument("-g", "--glob", action="append", default=[], metavar="PAT", help="globパターンで対象を限定する。")
    parser.add_argument("--encoding", default="utf-8", help="ファイル読み込み時のエンコーディング（既定: utf-8）。")
    parser.add_argument(
        "--max-filesize",
        type=int,
        default=None,
        metavar="BYTES",
        help="走査対象ファイルサイズの上限（バイト単位）。",
    )
    parser.add_argument("--hidden", action="store_true", help="ドットファイルも対象に含める。")

    # ファイル単位サマリ系（grepでのみ受理する。replace側では拒否）
    parser.add_argument(
        "-l",
        "--files-with-matches",
        action="store_true",
        help="マッチ件数1以上のファイルパスのみ列挙する。",
    )
    parser.add_argument("-c", "--count", action="store_true", help="ファイルごとのマッチ件数のみ出力する。")
    parser.add_argument(
        "--count-matches",
        action="store_true",
        help="`--count`と同等（互換のため受理）。",
    )
    parser.add_argument(
        "--files-without-match",
        action="store_true",
        help="マッチが1件も無いファイルパスを列挙する。",
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
        help=(
            "出力形式を指定する（text / json / jsonl、既定: text）。"
            f"未指定時は環境変数 {pyfltr.cli.output_format.OUTPUT_FORMAT_ENV} を、"
            f"{pyfltr.cli.output_format.AI_AGENT_ENV} が設定されていれば jsonl を採用する。"
        ),
    )
    parser.add_argument(
        "--output-file",
        type=pathlib.Path,
        default=None,
        help="JSONL / json出力先ファイル。未指定時は stdout に出力する。",
    )


def execute_grep(parser: argparse.ArgumentParser, args: argparse.Namespace) -> int:
    """`grep`サブコマンドの処理本体。"""
    # 出力形式の解決
    resolution = pyfltr.cli.output_format.resolve_output_format(
        parser,
        args.output_format,
        valid_values=_VALID_OUTPUT_FORMATS,
        ai_agent_default="jsonl",
    )
    output_format = resolution.format

    # text出力先のセットアップ。jsonl / jsonの場合はstdout専有のため text_logger を抑止する。
    if output_format == "text":
        pyfltr.cli.output_format.configure_text_output(sys.stdout)
    else:
        pyfltr.cli.output_format.configure_text_output(sys.stderr, level=logging.WARNING)

    # 構造化出力先の設定（jsonl時のみハンドラー設定。json時はバッファして最後に1回dumpする）。
    if output_format == "jsonl":
        if args.output_file is not None:
            pyfltr.cli.output_format.configure_structured_output(args.output_file)
        else:
            pyfltr.cli.output_format.configure_structured_output(sys.stdout)
    else:
        pyfltr.cli.output_format.configure_structured_output(None)

    # パターン群の収集
    patterns = _collect_patterns(parser, args)
    if not patterns:
        parser.error("パターンが指定されていません。位置引数または `-e` / `-f` を使ってください。")

    # 正規表現コンパイル
    try:
        compiled = pyfltr.grep_.matcher.compile_pattern(
            patterns,
            fixed_strings=args.fixed_strings,
            ignore_case=args.ignore_case,
            smart_case=args.smart_case,
            word_regexp=args.word_regexp,
            line_regexp=args.line_regexp,
            multiline=args.multiline,
        )
    except ValueError as exc:
        parser.error(str(exc))

    # `-C`は `-A` / `-B` 未指定時の一括指定として作用する
    after_ctx = args.after_context
    before_ctx = args.before_context
    if args.context is not None:
        if after_ctx == 0:
            after_ctx = args.context
        if before_ctx == 0:
            before_ctx = args.context

    # 設定ロードとファイル展開
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

    # 隠しファイル除外（`--hidden`未指定時は`.`始まりエントリを除外する）
    if not args.hidden:
        expanded = [p for p in expanded if not _has_hidden_segment(p)]

    files_scanned = len(expanded)

    # JSONL header
    pattern_repr = "|".join(patterns) if len(patterns) > 1 else patterns[0]
    if output_format == "jsonl":
        pyfltr.grep_.jsonl_records.emit_grep_header(
            pattern=pattern_repr,
            files=files_scanned,
            format_source=resolution.source,
        )

    # サマリ系オプションの処理（`--files-with-matches`等）
    summary_only_mode = args.files_with_matches or args.count or args.count_matches or args.files_without_match

    # スキャン実行
    matches: list[MatchRecord] = []
    per_file_counts: dict[pathlib.Path, int] = {}
    for record in pyfltr.grep_.scanner.scan_files(
        expanded,
        compiled,
        before_context=before_ctx,
        after_context=after_ctx,
        max_per_file=args.max_count,
        max_total=args.max_total,
        encoding=args.encoding,
        max_filesize=args.max_filesize,
        multiline=args.multiline,
    ):
        if not isinstance(record, MatchRecord):
            continue  # FileMatchSummaryは現状未使用
        matches.append(record)
        per_file_counts[record.file] = per_file_counts.get(record.file, 0) + 1
        if not summary_only_mode and output_format != "json":
            if output_format == "jsonl":
                pyfltr.grep_.jsonl_records.emit_match(record)
            else:
                pyfltr.grep_.text_render.render_match(record)

    total_matches = len(matches)
    files_with_matches = len(per_file_counts)

    # サマリ系オプション出力（text / jsonl）
    if summary_only_mode:
        _emit_summary_only(
            output_format,
            args=args,
            per_file_counts=per_file_counts,
            scanned=expanded,
        )

    # ガイダンス文（replace起動コマンド案内）
    guidance = _build_grep_guidance(total_matches)

    if output_format == "jsonl":
        # スキャン中に蓄積された警告（エンコーディングエラー・読み込み失敗等）を
        # summaryの直前に出力し、pipelineのwarning出力位置と挙動を揃える
        for warning_entry in pyfltr.warnings_.collected_warnings():
            pyfltr.grep_.jsonl_records.emit_warning(warning_entry)
        pyfltr.grep_.jsonl_records.emit_grep_summary(
            total_matches=total_matches,
            files_scanned=files_scanned,
            exit_code=0 if total_matches > 0 else 1,
            guidance=guidance if total_matches > 0 else None,
        )
    elif output_format == "json":
        summary: dict[str, typing.Any] = {
            "total_matches": total_matches,
            "files_scanned": files_scanned,
            "files_with_matches": files_with_matches,
        }
        if total_matches > 0 and guidance:
            summary["guidance"] = guidance
        if summary_only_mode:
            payload = _build_summary_only_json(args=args, per_file_counts=per_file_counts, scanned=expanded)
            payload["summary"] = summary
        else:
            payload = {
                "matches": [_match_to_dict(m) for m in matches],
                "summary": summary,
            }
        _print_json(payload, args.output_file)
    else:
        if not summary_only_mode:
            pyfltr.grep_.text_render.render_grep_summary(
                total_matches=total_matches,
                files_with_matches=files_with_matches,
                files_scanned=files_scanned,
            )
        if total_matches > 0:
            pyfltr.grep_.text_render.render_grep_guidance(guidance)

    # ripgrep互換でマッチ0件はexit 1とする
    return 0 if total_matches > 0 else 1


def _collect_patterns(parser: argparse.ArgumentParser, args: argparse.Namespace) -> list[str]:
    """位置引数 / `-e` / `-f` からパターン群を収集する。"""
    patterns: list[str] = []
    if args.pattern is not None:
        patterns.append(args.pattern)
    patterns.extend(args.regexp)
    if args.file is not None:
        try:
            patterns.extend(pyfltr.grep_.matcher.read_pattern_file(args.file))
        except OSError as exc:
            parser.error(f"パターンファイルを読み込めません: {args.file}: {exc}")
    return patterns


def _has_hidden_segment(path: pathlib.Path) -> bool:
    """パス内に`.`始まりのセグメント（`.`/`..`を除く）が含まれるか判定する。"""
    for part in path.parts:
        if part in (".", ".."):
            continue
        if part.startswith("."):
            return True
    return False


def _build_grep_guidance(total_matches: int) -> list[str]:
    """grep完了時のガイダンス文（英語）を組み立てる。

    `total_matches > 0`時のみreplace起動コマンドを案内する。マッチ0件時は呼び出し側で省略する。
    """
    if total_matches <= 0:
        return []
    return [
        "Use 'pyfltr replace <pattern> <replacement> [paths...]' with the same arguments to apply replacements.",
        "Use --dry-run to preview, or --from-grep=<jsonl-file> to limit replacement to files emitted by this grep.",
    ]


def _match_to_dict(record: MatchRecord) -> dict[str, typing.Any]:
    """MatchRecordをjson形式の辞書へ変換する。"""
    payload: dict[str, typing.Any] = {
        "file": str(record.file),
        "line": record.line,
        "col": record.col,
        "match_text": record.match_text,
        "line_text": record.line_text,
    }
    if record.end_col is not None:
        payload["end_col"] = record.end_col
    if record.before_lines:
        payload["before"] = list(record.before_lines)
    if record.after_lines:
        payload["after"] = list(record.after_lines)
    return payload


def _build_summary_only_json(
    *,
    args: argparse.Namespace,
    per_file_counts: dict[pathlib.Path, int],
    scanned: list[pathlib.Path],
) -> dict[str, typing.Any]:
    """`--files-with-matches` / `--count` / `--files-without-match` 時のjson payload。

    モード判定はargparse側のフラグ参照順（`files_without_match`→`files_with_matches`→`count`）に
    揃え、`_emit_summary_only`の出力先と同じレコード構造で返す。
    """
    if args.files_without_match:
        return {
            "summary_only_mode": "files-without-match",
            "files": [str(p) for p in scanned if p not in per_file_counts],
        }
    if args.files_with_matches:
        return {
            "summary_only_mode": "files-with-matches",
            "files": [str(p) for p in per_file_counts],
        }
    # `--count` / `--count-matches`
    return {
        "summary_only_mode": "count",
        "counts": [{"file": str(p), "count": c} for p, c in per_file_counts.items()],
    }


def _emit_summary_only(
    output_format: str,
    *,
    args: argparse.Namespace,
    per_file_counts: dict[pathlib.Path, int],
    scanned: list[pathlib.Path],
) -> None:
    """`--files-with-matches` / `--count` / `--files-without-match` 時の結果出力。"""
    if args.files_without_match:
        files = [p for p in scanned if p not in per_file_counts]
        if output_format == "text":
            with pyfltr.cli.output_format.text_output_lock:
                for path in files:
                    pyfltr.cli.output_format.text_logger.info(str(path))
        elif output_format == "jsonl":
            for path in files:
                pyfltr.grep_.jsonl_records.emit_file_without_match(path)
        return

    if args.files_with_matches:
        if output_format == "text":
            with pyfltr.cli.output_format.text_output_lock:
                for path in per_file_counts:
                    pyfltr.cli.output_format.text_logger.info(str(path))
        elif output_format == "jsonl":
            for path, count in per_file_counts.items():
                pyfltr.grep_.jsonl_records.emit_file_with_matches(path, count)
        return

    # `--count` / `--count-matches`
    if args.count or args.count_matches:
        if output_format == "text":
            with pyfltr.cli.output_format.text_output_lock:
                for path, count in per_file_counts.items():
                    pyfltr.cli.output_format.text_logger.info(f"{path}:{count}")
        elif output_format == "jsonl":
            for path, count in per_file_counts.items():
                pyfltr.grep_.jsonl_records.emit_file_count(path, count)


def _print_json(payload: dict[str, typing.Any], output_file: pathlib.Path | None) -> None:
    """単発JSONをstdoutまたは`--output-file`に書く。"""
    text = json.dumps(payload, ensure_ascii=False, indent=2)
    if output_file is not None:
        output_file.parent.mkdir(parents=True, exist_ok=True)
        output_file.write_text(text + "\n", encoding="utf-8")
    else:
        sys.stdout.write(text + "\n")
        sys.stdout.flush()
