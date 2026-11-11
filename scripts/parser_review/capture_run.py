"""採取時にpyfltrを起動し、実行時に解析へ渡った入力を記録する起動スクリプト。

`collect`がサンプルごとに`uv run python <本ファイル> run ...`として起動する。
引数はそのまま`pyfltr`のCLIへ渡し、`pyfltr.command.core_.ExecutionRequest.parse_errors`へ渡った入力を
環境変数`PARSER_REVIEW_PARSE_INPUTS`が指すファイルへJSON配列で書く。
vitestのように一時ファイルの内容を解析し、実行アーカイブには人間向けの出力だけを残すツールでも、
`compare`が実行時と同じ入力で比べられるようにするためである。pyfltr本体の動作は変えない。
"""

import json
import os
import pathlib
import typing

import pyfltr.cli.main
import pyfltr.command.core_

CAPTURE_ENV = "PARSER_REVIEW_PARSE_INPUTS"
"""解析入力の記録先を渡す環境変数。"""


def main() -> None:
    """解析入力を記録しながらpyfltrを実行する。終了コードはpyfltrのものを返す。"""
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
