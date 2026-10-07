"""vitest実行。"""

import dataclasses
import pathlib
import shlex
import tempfile
import time
import typing

import pyfltr.command.core_
import pyfltr.command.process
import pyfltr.command.slow_tests
import pyfltr.config.config
import pyfltr.config.model
import pyfltr.parsing.entry
import pyfltr.tools
from pyfltr.command.core_ import CommandResult
from pyfltr.command.runner import build_invocation_argv

logger = __import__("logging").getLogger(__name__)


def _has_user_reporter_override(args_list: typing.Iterable[str]) -> bool:
    """利用者引数に `--reporter` または `--outputFile` 指定が含まれるかを判定する。

    vitestは `--outputFile.json=...` のようなドット記法やスペース区切り（`--reporter json`）も受け付ける。
    いずれの形式でも検出できるよう、`startswith` でフラグ名のプレフィクスを判定する。
    """
    for arg in args_list:
        if arg == "--reporter" or arg.startswith("--reporter="):
            return True
        if arg == "--outputFile" or arg.startswith("--outputFile="):
            return True
        if arg.startswith("--outputFile."):
            return True
    return False


def execute_vitest(request: pyfltr.command.core_.ExecutionRequest) -> pyfltr.command.core_.CommandResult:
    """vitestをJSON reporter併用で実行し、失敗を構造化diagnosticへ変換する。

    Vitestはデフォルトで `command.message` フォールバック経路（stdout末尾のtruncate）に倒れ、
    複数のテスト失敗が1つの文字列に結合されてエージェント側で個別解釈できない。
    `--reporter=default --reporter=json --outputFile.json=<tmpfile>` を末尾注入することで、
    利用者向けのデフォルトreporter出力（人間可読のテスト進捗・サマリ）を維持しつつ、
    Jest互換JSONをtmpfile経由で取得して `pyfltr.parsing.entry.parse_errors`
    に渡せるようにする。

    利用者の `vitest-args` または `additional_args` に `--reporter` または `--outputFile`
    指定が含まれる場合は、利用者の制御を尊重して注入をスキップする。
    その場合は `commandline` をそのまま実行し、stdout経由の従来経路で動作する。

    JSON出力からのdiagnostic生成は `_parse_vitest_json` が担い、失敗の `assertionResult` 単位で
    1つの `ErrorLocation` を生成する。
    `cwd`はsubprocessの起動先、`nodeid_base_cwd`はJSON中の絶対パスを相対化する基準として
    別々に受け取る。
    """
    user_args: list[str] = list(pyfltr.config.model.command_setting(request.ctx.config.values, request.command, "args", []))
    if _has_user_reporter_override(user_args) or _has_user_reporter_override(request.params.additional_args):
        return _run_vitest_subprocess(request, commandline=request.params.commandline, json_output_path=None)

    # tmpfileは `delete=False` で確保し、後始末をfinallyで明示する。
    # vitest側がtmpfileを書き込めるよう、Pythonからは開きっぱなしにしない。
    with tempfile.NamedTemporaryFile(prefix="pyfltr-vitest-", suffix=".json", delete=False) as tmp:
        json_path = pathlib.Path(tmp.name)
    try:
        injection_args = [
            "--reporter=default",
            "--reporter=json",
            f"--outputFile.json={json_path}",
        ]
        # `build_invocation_argv` で組み立てたargv末尾に注入引数とtargetsを追加する。
        # `_prepare_execution_params` で構築済みの `commandline` はtarget混入後のため、
        # 注入引数をtargetsより前に置く目的で再構築する。
        argv = build_invocation_argv(
            request.command,
            request.ctx.config,
            request.params.commandline_prefix,
            request.params.additional_args,
            fix_stage=False,
        )
        argv.extend(injection_args)
        if pyfltr.config.model.command_setting(request.ctx.config.values, request.command, "pass-filenames", True):
            argv.extend(str(t) for t in request.params.targets)
        return _run_vitest_subprocess(request, commandline=argv, json_output_path=json_path)
    finally:
        json_path.unlink(missing_ok=True)


def _run_vitest_subprocess(
    request: pyfltr.command.core_.ExecutionRequest, *, commandline: list[str], json_output_path: pathlib.Path | None
) -> CommandResult:
    """vitestをsubprocess起動し、JSON reporter出力をparse_errorsへ渡す。

    `json_output_path` が指定された場合は実行後に対象のファイルを読み込み、
    その内容を `parse_errors` のoutputとして渡す。指定が無い場合は
    stdoutを従来通り `parse_errors` に渡す。
    JSON reporter出力を取得できた場合は、各テストの所要時間から遅いテスト一覧を抽出して
    `CommandResult.slow_tests`へ設定する。利用者が`--reporter`等を自前指定して注入が
    スキップされた経路ではstdoutが解析対象となり、抽出結果は空になる。
    subprocessの起動先には`cwd`、nodeidの相対化には`nodeid_base_cwd`を用いる。
    """
    if request.verbose and request.ctx.on_output is not None:
        request.ctx.on_output(f"commandline: {shlex.join(commandline)}\n")
    proc = pyfltr.command.process.run_process(dataclasses.replace(request, verbose=False), commandline)
    output = proc.stdout.strip()
    elapsed = time.perf_counter() - request.start_time

    parse_source = output
    if json_output_path is not None:
        try:
            parse_source = json_output_path.read_text(encoding="utf-8")
        except OSError:
            # JSON reporter出力がtmpfileへ生成されていない場合（例: vitestがrcエラーで
            # 早期終了）はstdoutベースのフォールバックを使う。
            parse_source = output

    errors = pyfltr.parsing.entry.parse_errors(request.command, parse_source, request.params.command_info.error_pattern)
    slow_tests = pyfltr.command.slow_tests.parse_vitest_durations(
        parse_source, base_cwd=(request.nodeid_base_cwd or request.ctx.effective_cwd)
    )
    result = CommandResult.from_process(
        process=proc,
        command=request.command,
        command_info=request.params.command_info,
        commandline=commandline,
        output=output,
        elapsed=elapsed,
        files=len(request.params.targets),
        errors=errors,
        slow_tests=slow_tests,
    )
    return result
