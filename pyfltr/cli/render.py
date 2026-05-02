"""実行結果の text 整形描画。

`render_results` / `write_log` および warnings / summary 出力ヘルパーを担う。
`output/formatters.py` から呼び出され、各 formatter が text 整形を委譲する。

`pipeline.py` からは独立しており、依存方向は
`pipeline → formatters → render` の順方向に統一する。
"""

import logging
import shlex
import typing

import pyfltr.cli.output_format
import pyfltr.command.core_
import pyfltr.command.error_parser
import pyfltr.config.config
import pyfltr.warnings_

NCOLS = 128

logger = logging.getLogger(__name__)

text_logger = pyfltr.cli.output_format.text_logger
lock = pyfltr.cli.output_format.text_output_lock


def write_log(result: pyfltr.command.core_.CommandResult, *, use_github_annotations: bool = False) -> None:
    """コマンド実行結果の詳細ログ出力。

    パース済みエラーがある場合は`format_error()`で整形した一覧を表示する。
    エラーがなく失敗した場合は生出力をフォールバック表示する。

    `use_github_annotations`がTrueのとき、ErrorLocation行をGAワークフローコマンド記法で出す。
    False（既定）のときは従来のテキスト形式（`file:line:col: [tool:rule] msg`）で出す。
    枠線・区切り線・進捗ラベルは常にtext記法を維持する
    （GAはエラー箇所の解釈だけを切り替え、レイアウトはtextと同じにする設計）。
    """
    mark = "@" if result.alerted else "*"
    with lock:
        text_logger.info(f"{mark * 32} {result.command} {mark * (NCOLS - 34 - len(result.command))}")
        logger.debug(f"{mark} commandline: {shlex.join(result.commandline)}")
        text_logger.info(mark)
        if result.errors:
            for error in result.errors:
                if use_github_annotations:
                    text_logger.info(pyfltr.command.error_parser.format_error_github(error))
                else:
                    text_logger.info(pyfltr.command.error_parser.format_error(error))
        elif result.alerted:
            text_logger.info(result.output)
        else:
            summary = pyfltr.command.error_parser.parse_summary(result.command, result.output)
            if summary:
                text_logger.info(f"{mark} {summary}")
        text_logger.info(mark)
        text_logger.info(f"{mark} returncode: {result.returncode}")
        text_logger.info(mark * NCOLS)


def render_results(
    results: list[pyfltr.command.core_.CommandResult],
    config: pyfltr.config.config.Config,
    *,
    include_details: bool,
    output_format: str = "text",
    exit_code: int = 0,
    commands: list[str] | None = None,
    files: int | None = None,
    warnings: list[dict[str, typing.Any]] | None = None,
    run_id: str | None = None,
    launcher_prefix: list[str] | None = None,
) -> None:
    """実行結果を `成功コマンド → 失敗コマンド → summary` の順でまとめて出力する。

    summaryを末尾に出力することで、`tail -N`で末尾だけ読み取るツール
    （Claude Codeなど）でもsummaryが確実に見えるようにする。失敗コマンド詳細も
    summaryの直前に置くため、`tail -N`でエラー情報も捕捉しやすい。

    `include_details=False`のときは、詳細ログは既に出力済みとみなしsummaryのみ表示する
    （`--stream`モード向け）。

    構造化出力（JSONL / SARIF）はここでは扱わず、呼び出し元（`pyfltr.cli.main`）が
    `structured_logger`経由で書き出す。本関数は常にtext整形ログを
    `text_logger`に流す。`output_format`はErrorLocation行の整形方式の
    切替（`github-annotations`時のみGA記法）に使う。
    """
    del exit_code, commands, files, run_id, launcher_prefix  # 構造化出力への委譲が無くなり未使用
    ordered = sorted(results, key=lambda r: config.command_names.index(r.command))
    warnings = warnings or []

    use_ga = output_format == "github-annotations"
    if include_details:
        # 1. 成功コマンドの詳細ログ
        for result in ordered:
            if not result.alerted:
                write_log(result, use_github_annotations=use_ga)

        # 2. 失敗コマンドの詳細ログ（summaryの直前に配置しtail -Nでも拾えるようにする）
        for result in ordered:
            if result.alerted:
                write_log(result, use_github_annotations=use_ga)

    # 3. warnings（summaryの直前。先頭だと見落とされやすいため）
    _write_warnings_section(warnings)

    # 4. fully excluded files（summary直前。警告と混ざらないよう独立ブロックで出す）
    _write_fully_excluded_files_section(pyfltr.warnings_.filtered_direct_files(reason="excluded"))

    # 5. summary（末尾に出力することでtail -Nで必ず見えるようにする）
    _write_summary(ordered)


def _write_warnings_section(warnings: list[dict[str, typing.Any]]) -> None:
    """Warningsセクションをsummary直前に出力する。"""
    if not warnings:
        return
    with lock:
        text_logger.info(f"{'-' * 10} warnings {'-' * (72 - 10 - 10)}")
        for entry in warnings:
            text_logger.info(f"    [{entry['source']}] {entry['message']}")


def _write_fully_excluded_files_section(files: list[str]) -> None:
    """直接指定されたが除外設定で全除外されたファイルをまとめて表示する。

    警告としては個別のwarning行で既に通知しているが、総覧で見落とされやすいため
    summary直前に専用ブロックを置く。exit コードには影響しない。
    """
    if not files:
        return
    with lock:
        text_logger.info(f"{'-' * 10} fully-excluded-files {'-' * (72 - 10 - 22)}")
        for path in files:
            text_logger.info(f"    {path}")


def _write_summary(ordered_results: list[pyfltr.command.core_.CommandResult]) -> None:
    """Summary セクションを出力する。"""
    with lock:
        text_logger.info(f"{'-' * 10} summary {'-' * (72 - 10 - 9)}")
        for result in ordered_results:
            text_logger.info(f"    {result.command:<16s} {result.get_status_text()}")
        text_logger.info("-" * 72)
