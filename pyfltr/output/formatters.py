import dataclasses
import json
import logging
import pathlib
import sys
import typing

import pyfltr.command.completion
import pyfltr.command.core_
import pyfltr.config.config
import pyfltr.config.model
import pyfltr.output.code_quality
import pyfltr.output.jsonl
import pyfltr.output.logging_
import pyfltr.output.render
import pyfltr.output.sarif
import pyfltr.tools
import pyfltr.warnings_


@dataclasses.dataclass
class RunOutputContext:
    config: pyfltr.config.model.Config
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
    format_source: str | None = None
    quiet: bool = False
    subcommand: str | None = None
    jsonl_warnings_reach_consumer: bool = True
    completion: pyfltr.command.completion.RunCompletion | None = None


class OutputFormatter(typing.Protocol):
    def configure_loggers(self, ctx: RunOutputContext) -> None:
        pass

    def on_start(self, ctx: RunOutputContext) -> None:
        pass

    def on_result(self, ctx: RunOutputContext, result: pyfltr.command.core_.CommandResult) -> None:
        pass

    def on_finish(
        self,
        ctx: RunOutputContext,
        results: list[pyfltr.command.core_.CommandResult],
        exit_code: int,
        warnings: list[dict[str, typing.Any]],
    ) -> None:
        pass


def _configure_text_loggers(ctx: RunOutputContext) -> None:
    stream = sys.stderr if ctx.force_text_on_stderr else sys.stdout
    pyfltr.output.logging_.configure_text_output(stream, level=logging.INFO)
    pyfltr.output.logging_.configure_structured_output(None)


def _finish_text_output(
    ctx: RunOutputContext,
    results: list[pyfltr.command.core_.CommandResult],
    warnings: list[dict[str, typing.Any]],
    output_format: typing.Literal["text", "github-annotations"],
) -> None:
    pyfltr.output.render.render_results(
        results,
        ctx.config,
        include_details=ctx.include_details,
        output_format=output_format,
        warnings=warnings,
    )


class TextFormatter:
    def configure_loggers(self, ctx: RunOutputContext) -> None:
        _configure_text_loggers(ctx)

    def on_start(self, ctx: RunOutputContext) -> None:
        pass

    def on_result(self, ctx: RunOutputContext, result: pyfltr.command.core_.CommandResult) -> None:
        pass

    def on_finish(
        self,
        ctx: RunOutputContext,
        results: list[pyfltr.command.core_.CommandResult],
        exit_code: int,
        warnings: list[dict[str, typing.Any]],
    ) -> None:
        del exit_code
        _finish_text_output(ctx, results, warnings, "text")


class GitHubAnnotationsFormatter:
    def configure_loggers(self, ctx: RunOutputContext) -> None:
        _configure_text_loggers(ctx)

    def on_start(self, ctx: RunOutputContext) -> None:
        pass

    def on_result(self, ctx: RunOutputContext, result: pyfltr.command.core_.CommandResult) -> None:
        pass

    def on_finish(
        self,
        ctx: RunOutputContext,
        results: list[pyfltr.command.core_.CommandResult],
        exit_code: int,
        warnings: list[dict[str, typing.Any]],
    ) -> None:
        del exit_code
        _finish_text_output(ctx, results, warnings, "github-annotations")


class JSONLFormatter:
    def configure_loggers(self, ctx: RunOutputContext) -> None:
        if ctx.force_text_on_stderr:
            pyfltr.output.logging_.configure_text_output(sys.stderr, level=logging.INFO)
        elif ctx.output_file is None:
            pyfltr.output.logging_.configure_text_output(sys.stderr, level=logging.WARNING)
        else:
            pyfltr.output.logging_.configure_text_output(sys.stdout, level=logging.INFO)
        destination: typing.TextIO | pathlib.Path = ctx.output_file if ctx.output_file is not None else sys.stdout
        pyfltr.output.logging_.configure_structured_output(destination)

    def on_start(self, ctx: RunOutputContext) -> None:
        pyfltr.output.jsonl.write_jsonl_header(
            commands=ctx.commands,
            files=ctx.all_files,
            run_id=ctx.run_id,
            config=ctx.config,
            format_source=ctx.format_source,
            quiet=ctx.quiet,
        )

    def on_result(self, ctx: RunOutputContext, result: pyfltr.command.core_.CommandResult) -> None:
        pyfltr.output.jsonl.write_jsonl_streaming(result, ctx.config, quiet=ctx.quiet)

    def on_finish(
        self,
        ctx: RunOutputContext,
        results: list[pyfltr.command.core_.CommandResult],
        exit_code: int,
        warnings: list[dict[str, typing.Any]],
    ) -> None:
        pyfltr.output.jsonl.write_jsonl_footer(
            results,
            exit_code=exit_code,
            warnings=warnings,
            run_id=ctx.run_id,
            launcher_prefix=ctx.launcher_prefix,
            subcommand=ctx.subcommand,
            fully_excluded_files=pyfltr.warnings_.filtered_direct_files(reason="excluded"),
            missing_targets=pyfltr.warnings_.filtered_direct_files(reason="missing"),
            completion=ctx.completion,
        )
        if ctx.jsonl_warnings_reach_consumer:
            pyfltr.warnings_.mark_delivered(warnings)
        pyfltr.output.render.render_results(
            results,
            ctx.config,
            include_details=ctx.include_details,
            output_format="jsonl",
            warnings=warnings if ctx.output_file is not None else [],
        )


class SARIFFormatter:
    def configure_loggers(self, ctx: RunOutputContext) -> None:
        if ctx.force_text_on_stderr or ctx.output_file is None:
            pyfltr.output.logging_.configure_text_output(sys.stderr, level=logging.INFO)
        else:
            pyfltr.output.logging_.configure_text_output(sys.stdout, level=logging.INFO)
        destination: typing.TextIO | pathlib.Path = ctx.output_file if ctx.output_file is not None else sys.stdout
        pyfltr.output.logging_.configure_structured_output(destination)

    def on_start(self, ctx: RunOutputContext) -> None:
        pass

    def on_result(self, ctx: RunOutputContext, result: pyfltr.command.core_.CommandResult) -> None:
        pass

    def on_finish(
        self,
        ctx: RunOutputContext,
        results: list[pyfltr.command.core_.CommandResult],
        exit_code: int,
        warnings: list[dict[str, typing.Any]],
    ) -> None:
        sarif = pyfltr.output.sarif.build_sarif(
            results,
            ctx.config,
            exit_code=exit_code,
            commands=ctx.commands,
            files=ctx.all_files,
            run_id=ctx.run_id,
        )
        pyfltr.output.logging_.structured_logger.info(json.dumps(sarif, ensure_ascii=False, indent=2))
        pyfltr.output.render.render_results(
            results,
            ctx.config,
            include_details=ctx.include_details,
            output_format="sarif",
            warnings=warnings,
        )


class CodeQualityFormatter:
    def configure_loggers(self, ctx: RunOutputContext) -> None:
        if ctx.force_text_on_stderr or ctx.output_file is None:
            pyfltr.output.logging_.configure_text_output(sys.stderr, level=logging.INFO)
        else:
            pyfltr.output.logging_.configure_text_output(sys.stdout, level=logging.INFO)
        destination: typing.TextIO | pathlib.Path = ctx.output_file if ctx.output_file is not None else sys.stdout
        pyfltr.output.logging_.configure_structured_output(destination)

    def on_start(self, ctx: RunOutputContext) -> None:
        pass

    def on_result(self, ctx: RunOutputContext, result: pyfltr.command.core_.CommandResult) -> None:
        pass

    def on_finish(
        self,
        ctx: RunOutputContext,
        results: list[pyfltr.command.core_.CommandResult],
        exit_code: int,
        warnings: list[dict[str, typing.Any]],
    ) -> None:
        del exit_code
        payload = pyfltr.output.code_quality.build_code_quality_payload(results)
        pyfltr.output.logging_.structured_logger.info(json.dumps(payload, ensure_ascii=False, indent=2))
        pyfltr.output.render.render_results(
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
新フォーマット追加時は本レジストリに追加するだけで、`cli/pipeline.py`は変更不要。
"""
