"""CLIとMCPで共有する検索・置換の要求、対象展開と実行。"""

from __future__ import annotations

import collections.abc
import dataclasses
import json
import pathlib
import re
import typing

import pyfltr.command.targets
import pyfltr.config.config
import pyfltr.config.model
import pyfltr.grep_.adaptive
import pyfltr.grep_.history
import pyfltr.grep_.jsonl_records
import pyfltr.grep_.matcher
import pyfltr.grep_.preview
import pyfltr.grep_.replacer
import pyfltr.grep_.scanner
import pyfltr.warnings_
from pyfltr.grep_.types import MatchRecord, ReplaceCommandMeta


class TargetConfigError(ValueError):
    """検索・置換の設定を読み込めなかったことを表す。"""


@dataclasses.dataclass
class TargetRequest:
    """両操作の対象指定。"""

    paths: list[pathlib.Path]
    types: list[str] = dataclasses.field(default_factory=list)
    globs: list[str] = dataclasses.field(default_factory=list)
    no_exclude: bool = False
    no_gitignore: bool = False

    @classmethod
    def from_paths(
        cls,
        paths: typing.Sequence[str | pathlib.Path],
        types: list[str],
        globs: list[str],
        no_exclude: bool,
        no_gitignore: bool,
    ) -> typing.Self:
        """公開入力のパスを共通型へ正規化して対象要求を構築する。"""
        return cls([pathlib.Path(path) for path in paths], types, globs, no_exclude, no_gitignore)


@dataclasses.dataclass(kw_only=True)
class PatternOptions:
    """検索と置換で共通のパターン・読み込み設定。"""

    targets: TargetRequest
    fixed_strings: bool = False
    ignore_case: bool = False
    smart_case: bool = False
    word_regexp: bool = False
    line_regexp: bool = False
    multiline: bool = False
    before_context: int = 0
    after_context: int = 0
    context: int | None = None
    encoding: str = "utf-8"
    max_filesize: int | None = None

    @staticmethod
    def arguments(
        flags: tuple[bool, bool, bool, bool, bool, bool],
        context_widths: tuple[int, int, int | None],
        encoding: str,
        max_filesize: int | None,
    ) -> dict[str, typing.Any]:
        """照合フラグ、前後の行数、読み込み設定を共通の要求引数へ変換する。"""
        fixed_strings, ignore_case, smart_case, word_regexp, line_regexp, multiline = flags
        before_context, after_context, context = context_widths
        return {
            "fixed_strings": fixed_strings,
            "ignore_case": ignore_case,
            "smart_case": smart_case,
            "word_regexp": word_regexp,
            "line_regexp": line_regexp,
            "multiline": multiline,
            "before_context": before_context,
            "after_context": after_context,
            "context": context,
            "encoding": encoding,
            "max_filesize": max_filesize,
        }

    def compile(self, patterns: list[str], *, multiline: bool | None = None) -> re.Pattern[str]:
        """同じ設定で検索パターンと置換アンカーをコンパイルする。"""
        return pyfltr.grep_.matcher.compile_pattern(
            patterns,
            fixed_strings=self.fixed_strings,
            ignore_case=self.ignore_case,
            smart_case=self.smart_case,
            word_regexp=self.word_regexp,
            line_regexp=self.line_regexp,
            multiline=self.multiline if multiline is None else multiline,
        )

    def context_widths(self) -> tuple[int, int]:
        """個別指定が0の方向だけへ一括指定を適用する。"""
        before = self.before_context
        after = self.after_context
        if self.context is not None:
            before = self.context if before == 0 else before
            after = self.context if after == 0 else after
        return before, after


@dataclasses.dataclass(kw_only=True)
class GrepRequest(PatternOptions):
    """出力入口から独立した検索要求。"""

    patterns: list[str]
    summary_only: bool = False
    max_count: int | None = None
    max_total: int | None = None
    max_preview_chars: int | None = None
    auto_summary: bool = False
    output_format: pyfltr.grep_.adaptive.OutputFormat = "text"
    full_text_hint: str = "`--max-preview-chars=0`"


@dataclasses.dataclass
class GrepResult:
    """各入口が公開形式へ変換する検索結果。"""

    expanded: list[pathlib.Path]
    per_file_counts: dict[pathlib.Path, int]
    total_matches: int
    selection: pyfltr.grep_.adaptive.Selection


def expand_targets(request: TargetRequest) -> tuple[pyfltr.config.model.Config, list[pathlib.Path]]:
    """共通設定を適用して検索・置換の対象を展開する。"""
    try:
        config = pyfltr.config.config.load_config()
    except (ValueError, OSError) as exc:
        raise TargetConfigError(f"設定エラー: {exc}") from exc
    if request.no_exclude:
        config.values["exclude"] = []
        config.values["extend-exclude"] = []
    if request.no_gitignore:
        config.values["respect-gitignore"] = False
    expanded = pyfltr.command.targets.expand_all_files(request.paths, config)
    expanded = pyfltr.grep_.scanner.filter_files_by_type(expanded, request.types)
    return config, pyfltr.grep_.scanner.filter_by_globs(expanded, request.globs)


def execute_grep(request: GrepRequest) -> GrepResult:
    """検索・本文の縮約・警告収集を一つの実行境界で行う。"""
    compiled = request.compile(request.patterns)
    before_ctx, after_ctx = request.context_widths()
    _config, expanded = expand_targets(request.targets)
    # スキャン実行
    matches: list[MatchRecord] = []
    per_file_counts: dict[pathlib.Path, int] = {}
    for record in pyfltr.grep_.scanner.scan_files(
        expanded,
        compiled,
        before_context=before_ctx,
        after_context=after_ctx,
        max_per_file=request.max_count or 0,
        max_total=request.max_total or 0,
        encoding=request.encoding,
        max_filesize=request.max_filesize,
        multiline=request.multiline,
    ):
        if not isinstance(record, MatchRecord):
            continue  # FileMatchSummaryは現状未使用
        matches.append(record)
        per_file_counts[record.file] = per_file_counts.get(record.file, 0) + 1
    total_matches = len(matches)

    preview_limit = (
        pyfltr.grep_.preview.DEFAULT_MAX_PREVIEW_CHARS if request.max_preview_chars is None else request.max_preview_chars
    )
    previews = (
        []
        if request.summary_only
        else [pyfltr.grep_.preview.build_match_preview(record, max_chars=preview_limit) for record in matches]
    )
    match_payloads = (
        []
        if request.summary_only
        else [
            pyfltr.grep_.jsonl_records.match_payload(record, preview) for record, preview in zip(matches, previews, strict=True)
        ]
    )
    explicit_output_control = request.summary_only or any(
        value is not None for value in (request.max_count, request.max_total, request.max_preview_chars)
    )
    if request.auto_summary and not explicit_output_control:
        selection = pyfltr.grep_.adaptive.select_output(match_payloads, output_format=request.output_format)
    else:
        selection = pyfltr.grep_.adaptive.full_output(match_payloads)

    returned_payloads = list(selection.matches)
    for file_result in selection.file_results:
        returned_payloads.extend(typing.cast(list[dict[str, typing.Any]], file_result.get("matches", [])))
    truncated_matches = sum(bool(payload.get("truncated")) for payload in returned_payloads)

    if truncated_matches > 0:
        pyfltr.warnings_.emit_warning(
            source="grep",
            message=pyfltr.grep_.preview.build_truncation_warning(
                truncated_matches=truncated_matches,
                max_chars=preview_limit,
                full_text_hint=request.full_text_hint,
            ),
        )

    return GrepResult(expanded, per_file_counts, total_matches, selection)


@dataclasses.dataclass(kw_only=True)
class ReplaceRequest(PatternOptions):
    """置換と履歴保存に必要な共通要求。"""

    pattern: str
    replacement: str
    dry_run: bool = False
    within: str | None = None
    exclude_files: list[pathlib.Path] = dataclasses.field(default_factory=list)
    from_grep: pathlib.Path | None = None


@dataclasses.dataclass
class ReplaceResult:
    """保存結果と各入口で表示する変更内容。"""

    replace_id: str | None
    files_count: int
    prepared: list[tuple[pathlib.Path, pyfltr.grep_.replacer.ReplacementResult]]
    read_failures: int


def execute_replace(
    request: ReplaceRequest,
    *,
    on_start: collections.abc.Callable[[str | None, int], None] | None = None,
) -> ReplaceResult:
    """対象を一括で準備して履歴保存後に置換する。

    on_startは、準備したIDと対象件数をCLIの先頭レコードへ通知する。
    """
    compiled = request.compile([request.pattern])
    anchor = request.compile([request.within], multiline=False) if request.within is not None else None
    before_ctx, after_ctx = request.context_widths()
    config, expanded = expand_targets(request.targets)
    excluded = {path.resolve() for path in request.exclude_files}
    if excluded:
        expanded = [path for path in expanded if path.resolve() not in excluded]
    if request.from_grep is not None:
        allowed = read_from_grep(request.from_grep)
        expanded = [path for path in expanded if path.resolve() in allowed]
    dry_run = request.dry_run
    replace_id = pyfltr.grep_.history.generate_replace_id() if not dry_run else None
    if on_start is not None:
        on_start(replace_id, len(expanded))
    file_changes: list[dict[str, typing.Any]] = []
    read_failures = 0
    prepared: list[tuple[pathlib.Path, pyfltr.grep_.replacer.ReplacementResult]] = []
    for file in expanded:
        if request.max_filesize is not None and request.max_filesize > 0:
            try:
                if file.stat().st_size > request.max_filesize:
                    continue
            except OSError:
                continue
        try:
            if anchor is not None:
                result = pyfltr.grep_.replacer.apply_block_replace_to_file(
                    file,
                    compiled,
                    request.replacement,
                    anchor,
                    before_context=before_ctx,
                    after_context=after_ctx,
                    encoding=request.encoding,
                )
            else:
                result = pyfltr.grep_.replacer.apply_replace_to_file(
                    file,
                    compiled,
                    request.replacement,
                    encoding=request.encoding,
                )
        except (UnicodeDecodeError, OSError) as exc:
            pyfltr.grep_.scanner.emit_read_failure_warning("replace", file, exc, encoding=request.encoding)
            read_failures += 1
            continue
        if result.count == 0:
            continue
        prepared.append((file, result))
        if not dry_run:
            file_changes.append(
                {
                    "file": file,
                    "before_bytes": result.before_bytes,
                    "after_bytes": result.after_bytes,
                    "records": list(result.records),
                }
            )

    if file_changes and replace_id is not None:
        meta = ReplaceCommandMeta(
            replace_id=replace_id,
            dry_run=False,
            fixed_strings=request.fixed_strings,
            pattern=request.pattern,
            replacement=request.replacement,
            encoding=request.encoding,
        )
        store = pyfltr.grep_.history.ReplaceHistoryStore()
        store.apply_replace(
            replace_id,
            command_meta=meta,
            file_changes=file_changes,
            policy=pyfltr.grep_.history.policy_from_config(config),
        )
    return ReplaceResult(replace_id, len(expanded), prepared, read_failures)


def read_from_grep(jsonl_path: pathlib.Path) -> set[pathlib.Path]:
    """grep出力JSONLから`kind=match`のファイル集合を抽出する。

    CLIとMCPの双方から利用できるよう、エラー表現を呼び出し側へ委ねる。
    """
    try:
        text = jsonl_path.read_text(encoding="utf-8")
    except OSError as exc:
        raise ValueError(f"--from-grep の読み込みに失敗しました: {jsonl_path}: {exc}") from exc
    files: set[pathlib.Path] = set()
    output_modes: set[str] = set()
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            continue
        output_mode = record.get("output_mode")
        if isinstance(output_mode, str):
            output_modes.add(output_mode)
        if record.get("kind") != "match":
            continue
        file = record.get("file")
        if isinstance(file, str):
            files.add(pathlib.Path(file).resolve())
    unsupported_modes = output_modes - {"full"}
    if unsupported_modes:
        modes = ", ".join(sorted(unsupported_modes))
        raise ValueError(f"--from-grep には省略を含まないfull出力を指定してください（検出したoutput_mode: {modes}）。")
    return files
