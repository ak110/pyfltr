"""診断解析の選択・パス補正・並び順。"""

import dataclasses
import pathlib

import pyfltr.paths
import pyfltr.rule_urls
import pyfltr.tools
from pyfltr.diagnostics import ErrorLocation
from pyfltr.parsing.common import extract_last_line, parse_with_pattern


def parse_errors(
    command: str,
    output: str,
    error_pattern: str | None = None,
    *,
    file_path_remap: dict[str, str] | None = None,
    path_base: pathlib.Path | None = None,
) -> list[ErrorLocation]:
    """コマンド出力からエラー箇所をパースする。

    優先順位:
        1. error_pattern（カスタム正規表現）が指定されていればそれを使用
        2. `path_base`を要するコマンド専用の関数ベースパーサー（`_PATH_BASE_PARSERS`）
        3. コマンド専用の関数ベースパーサー（JSON出力などregexで扱いにくいもの）
        4. ビルトイン正規表現パーサー
        5. いずれもなければ空リスト

    `path_base`は、ツールが出力する相対パスの基準ディレクトリである。
    サブプロジェクト分割実行では対象のサブプロジェクトのcwdを渡し、
    パーサーが起点cwd相対へ変換するために使う。
    """
    if error_pattern is not None:
        return apply_file_path_remap(parse_with_pattern(command, output, error_pattern), file_path_remap)
    path_base_parser = info.path_base_parser if (info := pyfltr.tools.BUILTIN_COMMANDS.get(command)) else None
    if path_base_parser is not None:
        return apply_file_path_remap(path_base_parser(output, path_base=path_base), file_path_remap)
    custom_parser = info.parser if (info := pyfltr.tools.BUILTIN_COMMANDS.get(command)) else None
    if custom_parser is not None:
        return apply_file_path_remap(custom_parser(output), file_path_remap)
    builtin = info.diagnostic_pattern if (info := pyfltr.tools.BUILTIN_COMMANDS.get(command)) else None
    if builtin is not None:
        return apply_file_path_remap(parse_with_pattern(command, output, builtin), file_path_remap)
    return []


def apply_file_path_remap(errors: list[ErrorLocation], remap: dict[str, str] | None) -> list[ErrorLocation]:
    """診断の一時ファイルパスを元ファイルパスへ戻す。"""
    if remap is None:
        return errors
    normalized_remap = dict(remap)
    for temporary_path, original_path in remap.items():
        normalized_remap[pyfltr.paths.to_cwd_relative(temporary_path)] = original_path
        normalized_remap[pyfltr.paths.normalize_separators(pathlib.Path(temporary_path).resolve())] = original_path
    return [
        dataclasses.replace(error, file=pyfltr.paths.to_cwd_relative(original_path))
        if (original_path := normalized_remap.get(normalize_remap_lookup_path(error.file))) is not None
        else error
        for error in errors
    ]


def normalize_remap_lookup_path(path: str) -> str:
    """remap照合用に診断ファイルパスを正規化する。"""
    as_path = pathlib.Path(path)
    if as_path.is_absolute() or ".." in as_path.parts:
        return pyfltr.paths.normalize_separators(as_path.resolve())
    return pyfltr.paths.normalize_separators(path)


def sort_errors(errors: list[ErrorLocation], command_names: list[str]) -> list[ErrorLocation]:
    """エラー箇所をファイル:行番号でソートし、同一箇所はcommand_names順に並べる。"""

    def sort_key(e: ErrorLocation) -> tuple[str, int, int, int]:
        cmd_index = pyfltr.tools.command_index(command_names, e.command)
        return (e.file, e.line, e.col or 0, cmd_index)

    return sorted(errors, key=sort_key)


def get_custom_parser_commands() -> set[str]:
    """カスタムパーサーが登録されているコマンド名の集合を返す。

    `path_base`を要するパーサーも含める。呼び出し元（`pyfltr/output/ui.py`）は
    構造化出力を逐次表示しない対象の判定に使うため、パーサーの引数の別を問わない。
    """
    return {command for command, info in pyfltr.tools.BUILTIN_COMMANDS.items() if info.parser or info.path_base_parser}


def parse_summary(command: str, output: str) -> str | None:
    """コマンド出力からサマリー文字列を抽出する。

    カスタムサマリーパーサーがあればそれを使い、なければテキスト出力の
    末尾行をフォールバックで抽出する。JSON出力はフォールバック対象外。
    """
    parser = info.summary_parser if (info := pyfltr.tools.BUILTIN_COMMANDS.get(command)) else None
    if parser is not None:
        return parser(output)
    return extract_last_line(output)
