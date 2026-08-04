"""出力形式が共有する診断位置の判定。

SARIF・GitHub Annotationsは同じ規則で列を出力するため、判定を本モジュールへ集約する。
Code Qualityは列を出力しないため本モジュールを使わない。

`pyfltr.command.error_parser`は型ヒントにしか使わないため、`TYPE_CHECKING`ガードで
循環importを回避する（`error_parser`が`pyfltr.output`配下を取り込むため）。
"""

from __future__ import annotations

import typing

if typing.TYPE_CHECKING:
    import pyfltr.command.error_parser


def is_publishable_column(error: pyfltr.command.error_parser.ErrorLocation, col: int | None) -> bool:
    """列を出力してよいかを判定する。

    値が無い場合と1未満の場合は行内位置として成立しないため出力しない。
    textlintは文を切り出すライブラリを用いるルールで列が行内位置にならないため、
    コマンド単位で一律に省略する。
    """
    return col is not None and col >= 1 and error.command != "textlint"
