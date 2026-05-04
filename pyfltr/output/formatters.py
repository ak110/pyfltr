"""出力フォーマット実装群。

`--output-format`で選択可能な各フォーマットの実装を`OutputFormatter` Protocolとして
一箇所に集約する。新フォーマット追加時は本モジュールのみ変更すればよい。

フォーマット分岐は`FORMATTERS`レジストリから動的に解決する。
レジストリは本モジュール初期化時に各formatter実装をトップレベルimportした上で
完結させる（遅延importは行わない）。
"""

import dataclasses
import json
import logging
import pathlib
import sys
import typing

import pyfltr.cli.output_format
import pyfltr.cli.render
import pyfltr.command.core_
import pyfltr.config.config
import pyfltr.output.code_quality
import pyfltr.output.jsonl
import pyfltr.output.sarif
import pyfltr.warnings_


@dataclasses.dataclass
class RunOutputContext:
    """formatterが必要とする実行文脈を一括保持するdataclass。

    `OutputFormatter` の各メソッドに渡すことで、formatter内部で必要な情報を
    引数なしに取得できる。

    `configure_loggers`呼び出し時点では`run_id` / `commands` / `all_files`など
    パイプライン後半で確定するフィールドが未知の場合があるため、デフォルト値を持つ。
    `on_start` / `on_result` / `on_finish`呼び出し時には全フィールドが確定した
    完全なctxを渡す。
    """

    config: pyfltr.config.config.Config
    output_file: pathlib.Path | None
    force_text_on_stderr: bool
    commands: list[str] = dataclasses.field(default_factory=list)
    all_files: int = 0
    run_id: str | None = None
    launcher_prefix: list[str] = dataclasses.field(default_factory=list)
    retry_args_template: list[str] = dataclasses.field(default_factory=list)
    stream: bool = False
    include_details: bool = True
    structured_stdout: bool = False
    # 出力形式の解決経路ラベル（`pyfltr.cli.output_format.FORMAT_SOURCE_*`）。
    # 実行系サブコマンドのみ`run_pipeline`が値を埋める。参照系・MCP経路では`None`のまま。
    # JSONL `header.format_source`に出力するためにJSONLFormatterで参照する。
    format_source: str | None = None


class OutputFormatter(typing.Protocol):
    """出力フォーマットを担うProtocol。

    各メソッドは`main.run_pipeline`から呼ばれる。実装クラスは`FORMATTERS`
    レジストリに登録し、`FORMATTERS[output_format]()`でインスタンス化して使う。
    """

    def configure_loggers(self, ctx: RunOutputContext) -> None:
        """`text_logger` / `structured_logger`の出力先・レベルを初期化する。

        `pyfltr.cli.output_format.configure_text_output` / `pyfltr.cli.output_format.configure_structured_output`
        を呼んで、フォーマット・output_file・force_text_on_stderrの組み合わせに応じた
        向き先を確定する。
        """

    def on_start(self, ctx: RunOutputContext) -> None:
        """パイプライン開始時に呼ぶ。

        - JSONL: header行を出力する
        - SARIF / Code Quality: 準備のみ（何もしない）
        - text / github-annotations: 何もしない（ヘッダー出力はmain.pyが担う）
        """

    def on_result(self, ctx: RunOutputContext, result: pyfltr.command.core_.CommandResult) -> None:
        """1ツール完了時に呼ぶ。`archive_hook`の後に呼ばれることが前提。

        - JSONL: diagnostic行 + tool行をstreamingで出力する
        - text / github-annotations: `ctx.stream`がTrueのとき詳細ログを即時出力
        - SARIF / Code Quality: バッファリング（何もしない）
        """

    def on_finish(
        self,
        ctx: RunOutputContext,
        results: list[pyfltr.command.core_.CommandResult],
        exit_code: int,
        warnings: list[dict[str, typing.Any]],
    ) -> None:
        """パイプライン終了時に呼ぶ。

        - JSONL: warning行 + summary行を出力する
        - SARIF: JSON全体を構造化出力loggerに出力する
        - Code Quality: JSON配列を構造化出力loggerに出力する
        - text / github-annotations: 詳細ログ（`ctx.include_details`がTrueの場合）+ summaryを出力する
        """


def command_index(config: pyfltr.config.config.Config, command: str) -> int:
    """`config.command_names`内での位置を返す（未登録コマンドは末尾扱い）。

    `llm_output.py` / `sarif_output.py`の重複実装を本モジュールへ集約したヘルパー。
    両モジュールは本関数をimportして使う。
    """
    if command in config.command_names:
        return config.command_names.index(command)
    return len(config.command_names)


class TextFormatter:
    """text形式の出力を担うformatter。"""

    def configure_loggers(self, ctx: RunOutputContext) -> None:
        """Text / github-annotations → stdout/INFO。構造化出力は無効。"""
        if ctx.force_text_on_stderr:
            pyfltr.cli.output_format.configure_text_output(sys.stderr, level=logging.INFO)
        else:
            pyfltr.cli.output_format.configure_text_output(sys.stdout, level=logging.INFO)
        pyfltr.cli.output_format.configure_structured_output(None)

    def on_start(self, ctx: RunOutputContext) -> None:
        """text形式の開始時処理。ヘッダー出力は`main.py`が担うため何もしない。"""

    def on_result(self, ctx: RunOutputContext, result: pyfltr.command.core_.CommandResult) -> None:
        """text形式のon_result。即時ログはcli._run_one_command（per_command_log経路）が担うため何もしない。"""

    def on_finish(
        self,
        ctx: RunOutputContext,
        results: list[pyfltr.command.core_.CommandResult],
        exit_code: int,
        warnings: list[dict[str, typing.Any]],
    ) -> None:
        """詳細ログ（include_detailsがTrueの場合）+ summaryを出力する。"""
        del exit_code
        pyfltr.cli.render.render_results(
            results,
            ctx.config,
            include_details=ctx.include_details,
            output_format="text",
            warnings=warnings,
        )


class GitHubAnnotationsFormatter:
    """GitHub Actions annotations形式の出力を担うformatter。

    textと同じレイアウトで、ErrorLocation行のみGAワークフローコマンド記法に切り替える。
    """

    def configure_loggers(self, ctx: RunOutputContext) -> None:
        """github-annotations → stdout/INFO。構造化出力は無効。"""
        if ctx.force_text_on_stderr:
            pyfltr.cli.output_format.configure_text_output(sys.stderr, level=logging.INFO)
        else:
            pyfltr.cli.output_format.configure_text_output(sys.stdout, level=logging.INFO)
        pyfltr.cli.output_format.configure_structured_output(None)

    def on_start(self, ctx: RunOutputContext) -> None:
        """github-annotations形式の開始時処理。何もしない。"""

    def on_result(self, ctx: RunOutputContext, result: pyfltr.command.core_.CommandResult) -> None:
        """github-annotations形式のon_result。即時ログはcli._run_one_command（per_command_log経路）が担うため何もしない。"""

    def on_finish(
        self,
        ctx: RunOutputContext,
        results: list[pyfltr.command.core_.CommandResult],
        exit_code: int,
        warnings: list[dict[str, typing.Any]],
    ) -> None:
        """詳細ログ（GA記法）+ summaryを出力する。"""
        del exit_code
        pyfltr.cli.render.render_results(
            results,
            ctx.config,
            include_details=ctx.include_details,
            output_format="github-annotations",
            warnings=warnings,
        )


class JSONLFormatter:
    """JSONL streaming形式の出力を担うformatter。"""

    def configure_loggers(self, ctx: RunOutputContext) -> None:
        """JSONL形式のlogger設定。

        jsonl + stdout → textはstderr/WARN、構造化はstdout。
        jsonl + output_file → textはstdout/INFO、構造化はFileHandler。
        """
        if ctx.force_text_on_stderr:
            pyfltr.cli.output_format.configure_text_output(sys.stderr, level=logging.INFO)
        elif ctx.output_file is None:
            pyfltr.cli.output_format.configure_text_output(sys.stderr, level=logging.WARNING)
        else:
            pyfltr.cli.output_format.configure_text_output(sys.stdout, level=logging.INFO)
        destination: typing.TextIO | pathlib.Path = ctx.output_file if ctx.output_file is not None else sys.stdout
        pyfltr.cli.output_format.configure_structured_output(destination)

    def on_start(self, ctx: RunOutputContext) -> None:
        """header行を出力する。"""
        pyfltr.output.jsonl.write_jsonl_header(
            commands=ctx.commands,
            files=ctx.all_files,
            run_id=ctx.run_id,
            config=ctx.config,
            format_source=ctx.format_source,
        )

    def on_result(self, ctx: RunOutputContext, result: pyfltr.command.core_.CommandResult) -> None:
        """Diagnostic行 + tool行をstreamingで出力する。"""
        pyfltr.output.jsonl.write_jsonl_streaming(result, ctx.config)

    def on_finish(
        self,
        ctx: RunOutputContext,
        results: list[pyfltr.command.core_.CommandResult],
        exit_code: int,
        warnings: list[dict[str, typing.Any]],
    ) -> None:
        """Warning行 + summary行を出力し、text整形も出力する。"""
        pyfltr.output.jsonl.write_jsonl_footer(
            results,
            exit_code=exit_code,
            warnings=warnings,
            run_id=ctx.run_id,
            launcher_prefix=ctx.launcher_prefix,
            fully_excluded_files=pyfltr.warnings_.filtered_direct_files(reason="excluded"),
            missing_targets=pyfltr.warnings_.filtered_direct_files(reason="missing"),
        )
        # 構造化出力の出力と並行して、常にtext整形を実行する。
        pyfltr.cli.render.render_results(
            results,
            ctx.config,
            include_details=ctx.include_details,
            output_format="jsonl",
            warnings=warnings,
        )


class SARIFFormatter:
    """SARIF 2.1.0形式の出力を担うformatter。バッファリングしon_finishで出力する。"""

    def configure_loggers(self, ctx: RunOutputContext) -> None:
        """SARIF形式のlogger設定。

        sarif + stdout → textはstderr/INFO、構造化はstdout。
        sarif + output_file → textはstdout/INFO、構造化はFileHandler。
        """
        if ctx.force_text_on_stderr or ctx.output_file is None:
            pyfltr.cli.output_format.configure_text_output(sys.stderr, level=logging.INFO)
        else:
            pyfltr.cli.output_format.configure_text_output(sys.stdout, level=logging.INFO)
        destination: typing.TextIO | pathlib.Path = ctx.output_file if ctx.output_file is not None else sys.stdout
        pyfltr.cli.output_format.configure_structured_output(destination)

    def on_start(self, ctx: RunOutputContext) -> None:
        """SARIFは事前準備なし。"""

    def on_result(self, ctx: RunOutputContext, result: pyfltr.command.core_.CommandResult) -> None:
        """SARIFはバッファリング。on_finishで一括出力するため何もしない。"""

    def on_finish(
        self,
        ctx: RunOutputContext,
        results: list[pyfltr.command.core_.CommandResult],
        exit_code: int,
        warnings: list[dict[str, typing.Any]],
    ) -> None:
        """SARIF JSONを構造化出力loggerに出力し、text整形も出力する。"""
        sarif = pyfltr.output.sarif.build_sarif(
            results,
            ctx.config,
            exit_code=exit_code,
            commands=ctx.commands,
            files=ctx.all_files,
            run_id=ctx.run_id,
        )
        pyfltr.cli.output_format.structured_logger.info(json.dumps(sarif, ensure_ascii=False, indent=2))
        pyfltr.cli.render.render_results(
            results,
            ctx.config,
            include_details=ctx.include_details,
            output_format="sarif",
            warnings=warnings,
        )


class CodeQualityFormatter:
    """GitLab Code Quality形式の出力を担うformatter。バッファリングしon_finishで出力する。"""

    def configure_loggers(self, ctx: RunOutputContext) -> None:
        """Code Quality形式のlogger設定。

        code-quality + stdout → textはstderr/INFO、構造化はstdout。
        code-quality + output_file → textはstdout/INFO、構造化はFileHandler。
        """
        if ctx.force_text_on_stderr or ctx.output_file is None:
            pyfltr.cli.output_format.configure_text_output(sys.stderr, level=logging.INFO)
        else:
            pyfltr.cli.output_format.configure_text_output(sys.stdout, level=logging.INFO)
        destination: typing.TextIO | pathlib.Path = ctx.output_file if ctx.output_file is not None else sys.stdout
        pyfltr.cli.output_format.configure_structured_output(destination)

    def on_start(self, ctx: RunOutputContext) -> None:
        """Code Qualityは事前準備なし。"""

    def on_result(self, ctx: RunOutputContext, result: pyfltr.command.core_.CommandResult) -> None:
        """Code Qualityはバッファリング。on_finishで一括出力するため何もしない。"""

    def on_finish(
        self,
        ctx: RunOutputContext,
        results: list[pyfltr.command.core_.CommandResult],
        exit_code: int,
        warnings: list[dict[str, typing.Any]],
    ) -> None:
        """Code Quality JSON配列を構造化出力loggerに出力し、text整形も出力する。"""
        del exit_code
        payload = pyfltr.output.code_quality.build_code_quality_payload(results)
        pyfltr.cli.output_format.structured_logger.info(json.dumps(payload, ensure_ascii=False, indent=2))
        pyfltr.cli.render.render_results(
            results,
            ctx.config,
            include_details=ctx.include_details,
            output_format="code-quality",
            warnings=warnings,
        )


FORMATTERS: dict[str, type[OutputFormatter]] = {
    "text": TextFormatter,
    "jsonl": JSONLFormatter,
    "sarif": SARIFFormatter,
    "github-annotations": GitHubAnnotationsFormatter,
    "code-quality": CodeQualityFormatter,
}
"""出力フォーマット名 → formatterクラスのレジストリ。

`FORMATTERS[output_format]()`でインスタンス化して使う。
新フォーマット追加時は本レジストリに追加するだけで、`main.py` / `cli.py`は変更不要。
"""
