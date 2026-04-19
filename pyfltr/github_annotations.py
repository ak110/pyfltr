"""GitHub Actions 注釈形式の出力生成。

``--output-format=github-annotations`` で呼ばれ、CommandResult 群を
``::error file=...`` 形式の行群に変換する。GitHub Actions が該当行を拾って
PR のファイル行にインラインで警告/エラーを表示するために使う。

GitHub のログビューアーは ``::xxx file=...,line=...,title=...::msg`` から
プロパティ部を剥がして ``##[xxx]msg`` としてしか描画しないため、
CI の生ログでは file / line / rule が見えない。そこでワークフローコマンド行とは別に、
``file:line:col: severity: [tool: rule] message`` 形式のプレーンテキスト行も
併せて出力し、生ログ閲覧時にも原因を特定できるようにする。
テキスト行は GitHub にワークフローコマンドとして解釈されないため副作用はない。
"""

import pyfltr.command
import pyfltr.config
import pyfltr.error_parser

_SEVERITY_TO_KIND: dict[str | None, str] = {
    "error": "error",
    "warning": "warning",
    "info": "notice",
}


def build_github_annotation_lines(
    results: list[pyfltr.command.CommandResult],
    config: pyfltr.config.Config,
) -> list[str]:
    """CommandResult 群から ``::<kind> file=...`` 形式とプレーンテキストの行群を生成する。

    severity が ``error`` → ``::error``、``warning`` → ``::warning``、``info`` →
    ``::notice``。未設定の diagnostic は ``::warning`` にフォールバックする。
    各診断につき、プレーンテキスト行とワークフローコマンド行の 2 行を順に出力する。
    GitHub の仕様上、メッセージ本体に改行・カンマを含めると壊れるためエスケープする。
    """
    ordered = sorted(results, key=lambda r: _command_index(config, r.command))
    lines: list[str] = []
    for result in ordered:
        for error in result.errors:
            kind = _SEVERITY_TO_KIND.get(error.severity, "warning")
            lines.append(_build_plain_line(result.command, error, kind))
            lines.append(_build_workflow_command(result.command, error, kind))
    return lines


def _build_plain_line(command: str, error: pyfltr.error_parser.ErrorLocation, kind: str) -> str:
    """生ログ閲覧用のプレーンテキスト 1 行を組み立てる。

    ``::`` を含まない通常文字列のため GitHub はワークフローコマンドとして解釈しない。
    改行・タブはログを壊さないようスペースへ畳み、1 診断 1 行に保つ。
    """
    location = error.file or "?"
    if error.line:
        location += f":{error.line}"
    if error.col is not None:
        location += f":{error.col}"
    rule_part = f"[{command}: {error.rule}]" if error.rule else f"[{command}]"
    message_text = error.message.replace("\r\n", " ").replace("\n", " ").replace("\t", " ")
    return f"{location}: {kind}: {rule_part} {message_text}"


def _build_workflow_command(command: str, error: pyfltr.error_parser.ErrorLocation, kind: str) -> str:
    """GitHub Actions 向けのワークフローコマンド 1 行を組み立てる。"""
    props = [f"file={_escape_property(error.file)}", f"line={error.line}"]
    if error.col is not None:
        props.append(f"col={error.col}")
    if error.rule is not None:
        props.append(f"title={_escape_property(f'{command}: {error.rule}')}")
    else:
        props.append(f"title={_escape_property(command)}")
    message_text = _escape_message(error.message)
    return f"::{kind} {','.join(props)}::{message_text}"


def _escape_property(value: str) -> str:
    """プロパティ値用のエスケープ。``,`` / ``:`` / 改行 を GitHub 仕様でエンコードする。"""
    return value.replace("%", "%25").replace("\r", "%0D").replace("\n", "%0A").replace(":", "%3A").replace(",", "%2C")


def _escape_message(value: str) -> str:
    r"""メッセージ値用のエスケープ。``%`` / ``\\r`` / ``\\n`` のみ必要。"""
    return value.replace("%", "%25").replace("\r", "%0D").replace("\n", "%0A")


def _command_index(config: pyfltr.config.Config, command: str) -> int:
    if command in config.command_names:
        return config.command_names.index(command)
    return len(config.command_names)
