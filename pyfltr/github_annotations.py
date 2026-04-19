"""GitHub Actions 注釈形式の出力生成。

``--output-format=github-annotations`` のレイアウトは ``text`` と同じで、
``pyfltr.cli.write_log()`` から呼ばれる ``pyfltr.error_parser.format_error()`` が
1 診断 1 行の整形を本モジュールに委譲する。

GitHub のログビューアーは ``::xxx file=...,line=...,title=...::msg`` から
プロパティ部を剥がし ``##[xxx]msg`` としてしか描画しない。そのため生ログ上でも
file / line / rule を視認できるよう、メッセージ本体に
``{file}:{line}[:{col}]: [{tool}[:{rule}]] {message}`` を前置する。
この 1 行が GitHub Actions の annotations とログビューアー両方の要件を満たす。

``pyfltr.error_parser`` は型ヒントにしか使わないため、``TYPE_CHECKING`` ガードで
循環 import を回避する（``error_parser.format_error`` から本モジュールが呼ばれる）。
"""

from __future__ import annotations

import typing

if typing.TYPE_CHECKING:
    import pyfltr.error_parser

_SEVERITY_TO_KIND: dict[str | None, str] = {
    "error": "error",
    "warning": "warning",
    "info": "notice",
}


def build_workflow_command(error: pyfltr.error_parser.ErrorLocation) -> str:
    """ErrorLocation 1 件を GitHub Actions のワークフローコマンド 1 行に整形する。

    severity が ``error`` → ``::error``、``warning`` → ``::warning``、``info`` →
    ``::notice``。未設定は ``::warning`` にフォールバックする。
    メッセージ本体には生ログ視認用のプレフィックス
    ``{file}:{line}[:{col}]: [{tool}[:{rule}]] {message}`` を埋め込む。
    GitHub 仕様に従い ``%`` / 改行 はパーセントエンコードする。
    """
    kind = _SEVERITY_TO_KIND.get(error.severity, "warning")
    props = [f"file={_escape_property(error.file)}", f"line={error.line}"]
    if error.col is not None:
        props.append(f"col={error.col}")
    title = f"{error.command}: {error.rule}" if error.rule else error.command
    props.append(f"title={_escape_property(title)}")
    prefix = _build_plain_prefix(error)
    message_text = _escape_message(f"{prefix} {error.message}")
    return f"::{kind} {','.join(props)}::{message_text}"


def _build_plain_prefix(error: pyfltr.error_parser.ErrorLocation) -> str:
    """``file:line[:col]: [tool[:rule]]`` の形で生ログ視認用プレフィックスを組み立てる。"""
    location = error.file or "?"
    if error.line:
        location += f":{error.line}"
    if error.col is not None:
        location += f":{error.col}"
    tag = f"[{error.command}:{error.rule}]" if error.rule else f"[{error.command}]"
    return f"{location}: {tag}"


def _escape_property(value: str) -> str:
    """プロパティ値用のエスケープ。``,`` / ``:`` / 改行 を GitHub 仕様でエンコードする。"""
    return value.replace("%", "%25").replace("\r", "%0D").replace("\n", "%0A").replace(":", "%3A").replace(",", "%2C")


def _escape_message(value: str) -> str:
    r"""メッセージ値用のエスケープ。``%`` / ``\\r`` / ``\\n`` のみ必要。"""
    return value.replace("%", "%25").replace("\r", "%0D").replace("\n", "%0A")
