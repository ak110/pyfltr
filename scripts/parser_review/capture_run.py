import json
import os
import pathlib
import typing

import pyfltr.cli.main
import pyfltr.command.core_

CAPTURE_ENV = "PARSER_REVIEW_PARSE_INPUTS"
"""解析入力の記録先を渡す環境変数。"""


def main() -> None:
    destination = pathlib.Path(os.environ[CAPTURE_ENV])
    captured: list[str] = []
    original = pyfltr.command.core_.ExecutionRequest.parse_errors

    def recording(self: pyfltr.command.core_.ExecutionRequest, output: str, error_pattern: str | None) -> typing.Any:
        captured.append(output)
        return original(self, output, error_pattern)

    pyfltr.command.core_.ExecutionRequest.parse_errors = recording  # type: ignore[method-assign]  # 採取のための差し替え
    try:
        pyfltr.cli.main.main()
    finally:
        destination.write_text(json.dumps(captured, ensure_ascii=False), encoding="utf-8")


if __name__ == "__main__":
    main()
