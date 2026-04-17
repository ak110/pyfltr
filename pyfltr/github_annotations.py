"""GitHub Actions 注釈形式の出力生成。

``--output-format=github-annotations`` で呼ばれ、CommandResult 群を
``::error file=...`` 形式の行群に変換する。GitHub Actions が該当行を拾って
PR のファイル行にインラインで警告/エラーを表示するために使う。
"""

import pyfltr.command
import pyfltr.config

_SEVERITY_TO_KIND: dict[str | None, str] = {
    "error": "error",
    "warning": "warning",
    "info": "notice",
}


def build_github_annotation_lines(
    results: list[pyfltr.command.CommandResult],
    config: pyfltr.config.Config,
) -> list[str]:
    """CommandResult 群から ``::<kind> file=...`` 形式の行群を生成する。

    severity が ``error`` → ``::error``、``warning`` → ``::warning``、``info`` →
    ``::notice``。未設定の diagnostic は ``::warning`` にフォールバックする。
    GitHub の仕様上、メッセージ本体に改行・カンマを含めると壊れるためエスケープする。
    """
    ordered = sorted(results, key=lambda r: _command_index(config, r.command))
    lines: list[str] = []
    for result in ordered:
        for error in result.errors:
            kind = _SEVERITY_TO_KIND.get(error.severity, "warning")
            props = [f"file={_escape_property(error.file)}", f"line={error.line}"]
            if error.col is not None:
                props.append(f"col={error.col}")
            if error.rule is not None:
                props.append(f"title={_escape_property(f'{result.command}: {error.rule}')}")
            else:
                props.append(f"title={_escape_property(result.command)}")
            message_text = _escape_message(error.message)
            lines.append(f"::{kind} {','.join(props)}::{message_text}")
    return lines


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
