"""コマンド実行関連の処理。"""

import argparse
import dataclasses
import logging
import os
import pathlib
import random
import shlex
import subprocess
import time

import natsort

import pyfltr.config

logger = logging.getLogger(__name__)


@dataclasses.dataclass
class CommandResult:
    """コマンドの実行結果。"""

    command: str
    commandline: list[str]
    returncode: int | None
    has_error: bool
    files: int
    output: str
    elapsed: float

    @property
    def command_type(self) -> str:
        """コマンドの種類を返す。"""
        return pyfltr.config.ALL_COMMANDS[self.command].type

    @property
    def alerted(self) -> bool:
        """skipped/succeeded以外ならTrue"""
        return self.returncode is not None and self.returncode != 0

    @property
    def status(self) -> str:
        """ステータスの文字列を返す。"""
        if self.returncode is None:
            status = "skipped"
        elif self.returncode == 0:
            status = "succeeded"
        elif self.command_type == "formatter" and not self.has_error:
            status = "formatted"
        else:
            status = "failed"
        return status

    def get_status_text(self) -> str:
        """成型した文字列を返す。"""
        return f"{self.status} ({self.files}files in {self.elapsed:.1f}s)"


def execute_command(command: str, args: argparse.Namespace) -> CommandResult:
    """コマンドの実行。

    Args:
        command: 実行するコマンド名
        args: コマンドライン引数

    Returns:
        (CommandResult, output): 実行結果と出力文字列
    """
    globs = ["*_test.py"] if command == "pytest" else ["*.py"]
    targets = expand_globs(args.targets, globs)

    # ファイルの順番をシャッフルまたはソート
    if args.shuffle:
        random.shuffle(targets)
    else:
        targets = natsort.natsorted(targets, key=str)

    commandline: list[str] = [pyfltr.config.CONFIG[f"{command}-path"]]
    commandline.extend(pyfltr.config.CONFIG[f"{command}-args"])

    # 起動オプションからの追加引数を適用
    additional_args_str = getattr(args, f"{command.replace('-', '_')}_args", "")
    if additional_args_str:
        additional_args = shlex.split(additional_args_str)
        commandline.extend(additional_args)

    commandline.extend(map(str, targets))

    if len(targets) <= 0:
        return CommandResult(
            command=command,
            commandline=commandline,
            returncode=None,
            has_error=False,
            output="No target files found.",
            files=0,
            elapsed=0,
        )

    # --checkオプションを使わないとファイル変更があったかわからないコマンドは、
    # 一度--checkオプションをつけて実行してから、
    # 変更があった場合は再度--checkオプションなしで実行する。
    check_args = ["--check"] if command in ("autoflake", "isort", "black") else []

    has_error = False
    start_time = time.perf_counter()
    env = os.environ.copy()
    env["PYTHONIOENCODING"] = "utf-8"
    if pyfltr.config.CONFIG.get(f"{command}-devmode", False):
        env["PYTHONDEVMODE"] = "1"

    proc = subprocess.run(
        commandline + check_args,
        check=False,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        env=env,
        text=True,
        encoding="utf-8",
        errors="backslashreplace",
    )
    returncode = proc.returncode

    # autoflake/isort/black/ruff-formatの再実行
    if returncode != 0 and command in ("autoflake", "isort", "black"):
        proc = subprocess.run(
            commandline,
            check=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            env=env,
            text=True,
            encoding="utf-8",
            errors="backslashreplace",
        )
        if proc.returncode != 0:
            returncode = proc.returncode
            has_error = True

    output = proc.stdout.strip()
    elapsed = time.perf_counter() - start_time

    return CommandResult(
        command=command,
        commandline=commandline,
        returncode=returncode,
        has_error=has_error,
        files=len(targets),
        output=output,
        elapsed=elapsed,
    )


def expand_globs(targets: list[pathlib.Path], globs: list[str]) -> list[pathlib.Path]:
    """対象ファイルのリストアップ。"""
    # 空ならカレントディレクトリを対象とする
    if len(targets) == 0:
        targets = [pathlib.Path(".")]

    expanded: list[pathlib.Path] = []

    def _expand_target(target):
        try:
            if excluded(target):
                pass
            elif target.is_dir():
                # ディレクトリの場合、再帰
                for child in target.iterdir():
                    _expand_target(child)
            else:
                # ファイルの場合、globsのいずれかに一致するなら追加
                if any(target.match(glob) for glob in globs):
                    expanded.append(target)
        except OSError:
            logger.warning(f"I/O Error: {target}", exc_info=True)

    for target in targets:
        _expand_target(target.absolute())

    return expanded


def excluded(path: pathlib.Path) -> bool:
    """無視パターンチェック。"""
    excludes = pyfltr.config.CONFIG["exclude"] + pyfltr.config.CONFIG["extend-exclude"]
    # 対象パスに一致したらTrue
    if any(path.match(glob) for glob in excludes):
        return True
    # 親に一致してもTrue
    part = path.parent
    for _ in range(len(path.parts) - 1):
        if any(part.match(glob) for glob in excludes):
            return True
        part = part.parent
    # どれにも一致しなかったらFalse
    return False
