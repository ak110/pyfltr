"""glab関連コマンド実行。"""

import argparse
import pathlib
import re
import shlex
import time
import typing

import pyfltr.command.error_parser
import pyfltr.command.process
import pyfltr.config.config
import pyfltr.warnings_
from pyfltr.command.core_ import CommandResult

logger = __import__("logging").getLogger(__name__)


# GitLab remote未登録/未認証の状況でglab自身が出力するエラー文言。
# 検出後にglab-ci-lintをskipped扱いへ書き換える根拠とする。
# 各パターンの登録形式と照合方式は `_looks_like_glab_host_missing` のdocstringへ集約する。
_GLAB_HOST_NOT_FOUND_PATTERNS: tuple[str, ...] = (
    "none of the git remotes configured for this repository point to a known gitlab host",
    "not authenticated",
)


def _looks_like_glab_host_missing(output: str) -> bool:
    r"""GlabがGitLabホストを検出できなかった旨のエラーかを判定する。

    外部CLIの英文エラー出力を部分文字列マッチで判定するヘルパーは、
    判定前に `re.sub(r"\s+", " ", output.lower())` で空白を正規化するものとする。
    Windows runner等で端末幅により改行・追加スペースが挿入されパターンが分断されても
    検出できるようにするためである。
    パターン側は事前に正規化済み（小文字・連続空白なし）の形で登録する前提とする。
    同種ヘルパーを新設する場合も同方針を踏襲する。
    """
    normalized = re.sub(r"\s+", " ", output.lower())
    return any(pattern in normalized for pattern in _GLAB_HOST_NOT_FOUND_PATTERNS)


def execute_glab_ci_lint(
    command: str,
    command_info: pyfltr.config.config.CommandInfo,
    commandline: list[str],
    targets: list[pathlib.Path],
    config: pyfltr.config.config.Config,
    env: dict[str, str],
    on_output: typing.Callable[[str], None] | None,
    start_time: float,
    args: argparse.Namespace,
    *,
    is_interrupted: typing.Callable[[], bool] | None = None,
    on_subprocess_start: typing.Callable[[], None] | None = None,
    on_subprocess_end: typing.Callable[[], None] | None = None,
    cwd: pathlib.Path | None = None,
) -> CommandResult:
    """Glab ci lintをホスト未検出時にスキップ扱いへ変換しつつ実行する。"""
    glab_env = dict(env)
    # 文言判定がロケール依存にならないよう英語ロケールを強制する。
    glab_env["LC_ALL"] = "C"
    glab_env["LANG"] = "C"

    if args.verbose and on_output is not None:
        on_output(f"commandline: {shlex.join(commandline)}\n")

    proc = pyfltr.command.process.run_subprocess_with_timeout(
        commandline,
        glab_env,
        on_output,
        is_interrupted=is_interrupted,
        on_subprocess_start=on_subprocess_start,
        on_subprocess_end=on_subprocess_end,
        timeout=pyfltr.config.config.resolve_command_timeout(config.values, command),
        cwd=cwd,
    )
    returncode = proc.returncode
    output = proc.stdout.strip()
    elapsed = time.perf_counter() - start_time

    if returncode != 0 and _looks_like_glab_host_missing(output):
        message = "glab がGitLabホストを検出できなかったためスキップしました。"
        pyfltr.warnings_.emit_warning(source=command, message=message)
        skip_output = f"{message}\n\n{output}" if output else message
        return CommandResult.from_run(
            command=command,
            command_info=command_info,
            commandline=commandline,
            returncode=None,
            output=skip_output,
            files=len(targets),
            elapsed=elapsed,
        )

    errors = pyfltr.command.error_parser.parse_errors(command, output, command_info.error_pattern)
    return CommandResult.from_run(
        command=command,
        command_info=command_info,
        commandline=commandline,
        returncode=returncode,
        output=output,
        elapsed=elapsed,
        files=len(targets),
        errors=errors,
        timeout_exceeded=proc.timeout_exceeded,
    )
