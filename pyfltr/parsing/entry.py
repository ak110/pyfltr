"""診断解析の選択・パス補正・並び順。"""

import dataclasses
import pathlib
import typing

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
    start_cwd: pathlib.Path | None = None,
) -> list[ErrorLocation]:
    """コマンド出力からエラー箇所をパースし、診断の`file`を起点相対へ揃える。

    優先順位:
        1. error_pattern（カスタム正規表現）が指定されていればそれを使用
        2. パス変換関数を受け取るコマンド専用の関数ベースパーサー（`CommandInfo.path_resolving_parser`）
        3. コマンド専用の関数ベースパーサー（JSON出力などregexで扱いにくいもの）
        4. ビルトイン正規表現パーサー
        5. いずれもなければ空リスト

    `path_base`はツールが出力する相対パスの基準ディレクトリ（ツールの実行cwd）、
    `start_cwd`はpyfltr実行の起点である。各パーサーはツール出力のパスを区切りの統一だけで返し、
    本関数が`pyfltr.paths.to_start_relative`で起点相対へ一括変換する。
    サブプロジェクト分割実行では`path_base`がサブプロジェクトのcwdとなり、
    相対パスの診断も起点相対の対象集合（`--only-failed`・`retry_command`）と突合できる値になる。
    `path_resolving_parser`は診断メッセージにもパスを埋め込むため同じ変換関数を受け取って自ら変換し、
    その結果には本関数の変換を重ねない。
    一時ファイルから元ファイルへの付け替え（`file_path_remap`）は変換の後に適用し、付け替え後の値は再変換しない。
    """

    def resolve(path: str) -> str:
        return pyfltr.paths.to_start_relative(path, path_base=path_base, start_cwd=start_cwd)

    info = pyfltr.tools.BUILTIN_COMMANDS.get(command)
    errors: list[ErrorLocation]
    if error_pattern is not None:
        errors = rebase_errors(parse_with_pattern(command, output, error_pattern), resolve)
    elif info is not None and info.path_resolving_parser is not None:
        errors = info.path_resolving_parser(output, resolve_path=resolve)
    elif info is not None and info.parser is not None:
        errors = rebase_errors(info.parser(output), resolve)
    elif info is not None and info.diagnostic_pattern is not None:
        errors = rebase_errors(parse_with_pattern(command, output, info.diagnostic_pattern), resolve)
    else:
        return []
    return apply_file_path_remap(errors, file_path_remap, start_cwd=start_cwd)


def rebase_errors(errors: list[ErrorLocation], resolve: typing.Callable[[str], str]) -> list[ErrorLocation]:
    """診断の`file`へパス変換関数を適用する。"""
    return [dataclasses.replace(error, file=resolve(error.file)) for error in errors]


def apply_file_path_remap(
    errors: list[ErrorLocation],
    remap: dict[str, str] | None,
    *,
    start_cwd: pathlib.Path | None = None,
) -> list[ErrorLocation]:
    """診断の一時ファイルパスを元ファイルパスへ戻す。

    診断の`file`は`to_start_relative`で変換済みの値を受け取る。照合キーも同じ変換と
    シンボリックリンクを解決した絶対パスの双方で登録する。
    """
    if remap is None:
        return errors
    normalized_remap = dict(remap)
    for temporary_path, original_path in remap.items():
        normalized_remap[pyfltr.paths.to_start_relative(temporary_path, start_cwd=start_cwd)] = original_path
        normalized_remap[pyfltr.paths.normalize_separators(pathlib.Path(temporary_path).resolve())] = original_path
    return [
        dataclasses.replace(error, file=pyfltr.paths.to_start_relative(original_path, start_cwd=start_cwd))
        if (original_path := normalized_remap.get(normalize_remap_lookup_path(error.file))) is not None
        else error
        for error in errors
    ]


def normalize_remap_lookup_path(path: str) -> str:
    """remap照合用に診断ファイルパスを正規化する。

    変換済みの診断は起点外を絶対パスで持つため、絶対パスだけシンボリックリンクを解決して照合する。
    """
    as_path = pathlib.Path(path)
    if as_path.is_absolute():
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

    パス変換関数を受け取るパーサーも含める。呼び出し元（`pyfltr/output/ui.py`）は
    構造化出力を逐次表示しない対象の判定に使うため、パーサーの引数の別を問わない。
    """
    return {command for command, info in pyfltr.tools.BUILTIN_COMMANDS.items() if info.parser or info.path_resolving_parser}


def parse_summary(command: str, output: str) -> str | None:
    """コマンド出力からサマリー文字列を抽出する。

    カスタムサマリーパーサーがあればそれを使い、なければテキスト出力の
    末尾行をフォールバックで抽出する。JSON出力はフォールバック対象外。
    """
    parser = info.summary_parser if (info := pyfltr.tools.BUILTIN_COMMANDS.get(command)) else None
    if parser is not None:
        return parser(output)
    return extract_last_line(output)
