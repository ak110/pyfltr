"""出力フォーマット実装群。

``--output-format`` で選択可能な各フォーマットの実装を ``OutputFormatter`` Protocol として
一箇所に集約する。新フォーマット追加時は本モジュールのみ変更すればよい。

フォーマット分岐は ``FORMATTERS`` レジストリから動的に解決する。
"""

import dataclasses
import json
import logging
import pathlib
import sys
import typing

import pyfltr.cli
import pyfltr.command
import pyfltr.config


@dataclasses.dataclass
class RunOutputContext:
    """formatter が必要とする実行文脈を一括保持する dataclass。

    ``OutputFormatter`` の各メソッドに渡すことで、formatter 内部で必要な情報を
    引数なしに取得できる。

    ``configure_loggers`` 呼び出し時点では ``run_id`` / ``commands`` / ``all_files`` など
    パイプライン後半で確定するフィールドが未知の場合があるため、デフォルト値を持つ。
    ``on_start`` / ``on_result`` / ``on_finish`` 呼び出し時には全フィールドが確定した
    完全な ctx を渡す。
    """

    config: pyfltr.config.Config
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


class OutputFormatter(typing.Protocol):
    """出力フォーマットを担う Protocol。

    各メソッドは ``main.run_pipeline`` から呼ばれる。実装クラスは ``FORMATTERS``
    レジストリに登録し、``FORMATTERS[output_format]()`` でインスタンス化して使う。
    """

    def configure_loggers(self, ctx: RunOutputContext) -> None:
        """text_logger / structured_logger の出力先・レベルを初期化する。

        ``pyfltr.cli.configure_text_output`` / ``pyfltr.cli.configure_structured_output``
        を呼んで、フォーマット・output_file・force_text_on_stderr の組み合わせに応じた
        向き先を確定する。
        """

    def on_start(self, ctx: RunOutputContext) -> None:
        """パイプライン開始時に呼ぶ。

        - JSONL: header 行を書き出す
        - SARIF / Code Quality: 準備のみ（何もしない）
        - text / github-annotations: 何もしない（ヘッダー出力は main.py が担う）
        """

    def on_result(self, ctx: RunOutputContext, result: pyfltr.command.CommandResult) -> None:
        """1 ツール完了時に呼ぶ。archive_hook の後に呼ばれることが前提。

        - JSONL: diagnostic 行 + tool 行を streaming 書き出し
        - text / github-annotations: ``ctx.stream`` が True のとき詳細ログを即時出力
        - SARIF / Code Quality: バッファリング（何もしない）
        """

    def on_finish(
        self,
        ctx: RunOutputContext,
        results: list[pyfltr.command.CommandResult],
        exit_code: int,
        warnings: list[dict[str, typing.Any]],
    ) -> None:
        """パイプライン終了時に呼ぶ。

        - JSONL: warning 行 + summary 行を書き出す
        - SARIF: JSON 全体を構造化出力 logger に書き出す
        - Code Quality: JSON 配列を構造化出力 logger に書き出す
        - text / github-annotations: 詳細ログ（``ctx.include_details`` が True の場合）+ summary を書き出す
        """


def command_index(config: pyfltr.config.Config, command: str) -> int:
    """config.command_names 内での位置を返す（未登録コマンドは末尾扱い）。

    llm_output.py / sarif_output.py の重複実装を本モジュールへ集約したヘルパ。
    両モジュールは本関数を import して使う。
    """
    if command in config.command_names:
        return config.command_names.index(command)
    return len(config.command_names)


class TextFormatter:
    """text 形式の出力を担う formatter。"""

    def configure_loggers(self, ctx: RunOutputContext) -> None:
        """Text / github-annotations → stdout/INFO。構造化出力は無効。"""
        if ctx.force_text_on_stderr:
            pyfltr.cli.configure_text_output(sys.stderr, level=logging.INFO)
        else:
            pyfltr.cli.configure_text_output(sys.stdout, level=logging.INFO)
        pyfltr.cli.configure_structured_output(None)

    def on_start(self, ctx: RunOutputContext) -> None:
        """Text 形式の開始時処理。ヘッダー出力は main.py が担うため何もしない。"""

    def on_result(self, ctx: RunOutputContext, result: pyfltr.command.CommandResult) -> None:
        """Text 形式の on_result。即時ログは cli._run_one_command（per_command_log 経路）が担うため何もしない。"""

    def on_finish(
        self,
        ctx: RunOutputContext,
        results: list[pyfltr.command.CommandResult],
        exit_code: int,
        warnings: list[dict[str, typing.Any]],
    ) -> None:
        """詳細ログ（include_details が True の場合）+ summary を書き出す。"""
        del exit_code
        pyfltr.cli.render_results(
            results,
            ctx.config,
            include_details=ctx.include_details,
            output_format="text",
            warnings=warnings,
        )


class GitHubAnnotationsFormatter:
    """GitHub Actions annotations 形式の出力を担う formatter。

    text と同じレイアウトで、ErrorLocation 行のみ GA ワークフローコマンド記法に切り替える。
    """

    def configure_loggers(self, ctx: RunOutputContext) -> None:
        """github-annotations → stdout/INFO。構造化出力は無効。"""
        if ctx.force_text_on_stderr:
            pyfltr.cli.configure_text_output(sys.stderr, level=logging.INFO)
        else:
            pyfltr.cli.configure_text_output(sys.stdout, level=logging.INFO)
        pyfltr.cli.configure_structured_output(None)

    def on_start(self, ctx: RunOutputContext) -> None:
        """github-annotations 形式の開始時処理。何もしない。"""

    def on_result(self, ctx: RunOutputContext, result: pyfltr.command.CommandResult) -> None:
        """github-annotations 形式の on_result。即時ログは cli._run_one_command（per_command_log 経路）が担うため何もしない。"""

    def on_finish(
        self,
        ctx: RunOutputContext,
        results: list[pyfltr.command.CommandResult],
        exit_code: int,
        warnings: list[dict[str, typing.Any]],
    ) -> None:
        """詳細ログ（GA 記法）+ summary を書き出す。"""
        del exit_code
        pyfltr.cli.render_results(
            results,
            ctx.config,
            include_details=ctx.include_details,
            output_format="github-annotations",
            warnings=warnings,
        )


class JSONLFormatter:
    """JSONL streaming 形式の出力を担う formatter。"""

    def configure_loggers(self, ctx: RunOutputContext) -> None:
        """JSONL 形式の logger 設定。

        jsonl + stdout → text は stderr/WARN、構造化は stdout。
        jsonl + output_file → text は stdout/INFO、構造化は FileHandler。
        """
        if ctx.force_text_on_stderr:
            pyfltr.cli.configure_text_output(sys.stderr, level=logging.INFO)
        elif ctx.output_file is None:
            pyfltr.cli.configure_text_output(sys.stderr, level=logging.WARNING)
        else:
            pyfltr.cli.configure_text_output(sys.stdout, level=logging.INFO)
        destination: typing.TextIO | pathlib.Path = ctx.output_file if ctx.output_file is not None else sys.stdout
        pyfltr.cli.configure_structured_output(destination)

    def on_start(self, ctx: RunOutputContext) -> None:
        """Header 行を書き出す。"""
        from pyfltr import llm_output  # pylint: disable=import-outside-toplevel

        llm_output.write_jsonl_header(
            commands=ctx.commands,
            files=ctx.all_files,
            run_id=ctx.run_id,
        )

    def on_result(self, ctx: RunOutputContext, result: pyfltr.command.CommandResult) -> None:
        """Diagnostic 行 + tool 行を streaming 書き出しする。"""
        from pyfltr import llm_output  # pylint: disable=import-outside-toplevel

        llm_output.write_jsonl_streaming(result, ctx.config)

    def on_finish(
        self,
        ctx: RunOutputContext,
        results: list[pyfltr.command.CommandResult],
        exit_code: int,
        warnings: list[dict[str, typing.Any]],
    ) -> None:
        """Warning 行 + summary 行を書き出し、text 整形も出力する。"""
        from pyfltr import llm_output  # pylint: disable=import-outside-toplevel

        llm_output.write_jsonl_footer(
            results,
            exit_code=exit_code,
            warnings=warnings,
            run_id=ctx.run_id,
            launcher_prefix=ctx.launcher_prefix,
        )
        # 構造化出力の書き出しと並行して、常に text 整形を実行する。
        pyfltr.cli.render_results(
            results,
            ctx.config,
            include_details=ctx.include_details,
            output_format="jsonl",
            warnings=warnings,
        )


class SARIFFormatter:
    """SARIF 2.1.0 形式の出力を担う formatter。バッファリングし on_finish で書き出す。"""

    def configure_loggers(self, ctx: RunOutputContext) -> None:
        """SARIF 形式の logger 設定。

        sarif + stdout → text は stderr/INFO、構造化は stdout。
        sarif + output_file → text は stdout/INFO、構造化は FileHandler。
        """
        if ctx.force_text_on_stderr or ctx.output_file is None:
            pyfltr.cli.configure_text_output(sys.stderr, level=logging.INFO)
        else:
            pyfltr.cli.configure_text_output(sys.stdout, level=logging.INFO)
        destination: typing.TextIO | pathlib.Path = ctx.output_file if ctx.output_file is not None else sys.stdout
        pyfltr.cli.configure_structured_output(destination)

    def on_start(self, ctx: RunOutputContext) -> None:
        """SARIF は事前準備なし。"""

    def on_result(self, ctx: RunOutputContext, result: pyfltr.command.CommandResult) -> None:
        """SARIF はバッファリング。on_finish で一括書き出しするため何もしない。"""

    def on_finish(
        self,
        ctx: RunOutputContext,
        results: list[pyfltr.command.CommandResult],
        exit_code: int,
        warnings: list[dict[str, typing.Any]],
    ) -> None:
        """SARIF JSON を構造化出力 logger に書き出し、text 整形も出力する。"""
        from pyfltr import sarif_output  # pylint: disable=import-outside-toplevel

        sarif = sarif_output.build_sarif(
            results,
            ctx.config,
            exit_code=exit_code,
            commands=ctx.commands,
            files=ctx.all_files,
            run_id=ctx.run_id,
        )
        pyfltr.cli.structured_logger.info(json.dumps(sarif, ensure_ascii=False, indent=2))
        pyfltr.cli.render_results(
            results,
            ctx.config,
            include_details=ctx.include_details,
            output_format="sarif",
            warnings=warnings,
        )


class CodeQualityFormatter:
    """GitLab Code Quality 形式の出力を担う formatter。バッファリングし on_finish で書き出す。"""

    def configure_loggers(self, ctx: RunOutputContext) -> None:
        """Code Quality 形式の logger 設定。

        code-quality + stdout → text は stderr/INFO、構造化は stdout。
        code-quality + output_file → text は stdout/INFO、構造化は FileHandler。
        """
        if ctx.force_text_on_stderr or ctx.output_file is None:
            pyfltr.cli.configure_text_output(sys.stderr, level=logging.INFO)
        else:
            pyfltr.cli.configure_text_output(sys.stdout, level=logging.INFO)
        destination: typing.TextIO | pathlib.Path = ctx.output_file if ctx.output_file is not None else sys.stdout
        pyfltr.cli.configure_structured_output(destination)

    def on_start(self, ctx: RunOutputContext) -> None:
        """Code Quality は事前準備なし。"""

    def on_result(self, ctx: RunOutputContext, result: pyfltr.command.CommandResult) -> None:
        """Code Quality はバッファリング。on_finish で一括書き出しするため何もしない。"""

    def on_finish(
        self,
        ctx: RunOutputContext,
        results: list[pyfltr.command.CommandResult],
        exit_code: int,
        warnings: list[dict[str, typing.Any]],
    ) -> None:
        """Code Quality JSON 配列を構造化出力 logger に書き出し、text 整形も出力する。"""
        del exit_code
        from pyfltr import code_quality  # pylint: disable=import-outside-toplevel

        payload = code_quality.build_code_quality_payload(results)
        pyfltr.cli.structured_logger.info(json.dumps(payload, ensure_ascii=False, indent=2))
        pyfltr.cli.render_results(
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
"""出力フォーマット名 → formatter クラスのレジストリ。

``FORMATTERS[output_format]()`` でインスタンス化して使う。
新フォーマット追加時は本レジストリに追加するだけで、main.py / cli.py は変更不要。
"""
