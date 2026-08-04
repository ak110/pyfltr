"""GitLab CI Code Quality形式の出力生成。

`--output-format=code-quality`で呼ばれ、CommandResult群をCode Climate JSON issue形式の
サブセット（JSON配列）へ変換する。GitLab CIの`artifacts:reports:codequality`で取り込み、
Merge Request画面のCode Quality widgetおよびMR diffインライン表示に利用される。

severityはpyfltr内部の3値（error / warning / info）をCode Quality 5段階のうちの3値へ
マップする（`critical` / `blocker`は使わない）。pyfltr側に対応情報が無く、過大評価を
避けるためerror→major、warning→minor、info→info、未設定→minorとする。

`location`は`lines.begin`に加え、診断が終了行を保持する場合に`lines.end`を出力する。
Code Climate仕様の行範囲は両端を含むため、`begin`補正後の値より前の終了行は範囲として
成立せず出力しない。列は出力しない。GitLabの取り込み実装は`location.lines.begin`と
`location.positions.begin.line`だけを参照し、列を保持するフィールドを読まないためである。
列を伝えるには`lines`を`positions`へ置き換える必要があり、読まれない情報のために
開始行の表現をフォールバック側へ移すことになるため採らない。

`fingerprint`はtool・file・line・col・rule・msgをタブ区切りで連結した文字列の
SHA-256全桁を採用する。同一指摘の重複統合に足るユニーク性を確保しつつ、配置順の変化に
対して頑強にする。
"""

from __future__ import annotations

import hashlib
import typing

import pyfltr.command.core_
import pyfltr.command.error_parser

_SEVERITY_MAP: dict[str | None, str] = {
    "error": "major",
    "warning": "minor",
    "info": "info",
}
"""pyfltr severityからCode Quality severityへのマップ。未登録キーは`minor`にフォールバック。"""


def build_code_quality_payload(
    results: list[pyfltr.command.core_.CommandResult],
) -> list[dict[str, typing.Any]]:
    """Code Quality JSON issue形式のサブセット（JSON配列）を生成する。

    必須フィールド: `description` / `check_name` / `fingerprint` / `severity` /
    `location.path` / `location.lines.begin`。
    """
    payload: list[dict[str, typing.Any]] = []
    for result in results:
        for error in result.errors:
            payload.append(_build_issue(error))
    return payload


def _build_issue(error: pyfltr.command.error_parser.ErrorLocation) -> dict[str, typing.Any]:
    """ErrorLocation1件をCode Quality issue 1件に整形する。"""
    check_name = f"{error.command}:{error.rule}" if error.rule else error.command
    # GitLab Code Qualityはline=0を許容せず、message.lineがNoneまたは0のときは1に補正する。
    begin = error.line if error.line else 1
    lines: dict[str, int] = {"begin": begin}
    # Code Climate仕様の行範囲は両端を含むため、begin補正後の値より前の終了行は出力しない。
    if error.end_line is not None and error.end_line >= begin:
        lines["end"] = error.end_line
    return {
        "description": error.message,
        "check_name": check_name,
        "fingerprint": _build_fingerprint(error),
        "severity": _SEVERITY_MAP.get(error.severity, "minor"),
        "location": {
            "path": error.file,
            "lines": lines,
        },
    }


def _build_fingerprint(error: pyfltr.command.error_parser.ErrorLocation) -> str:
    """tool・file・line・col・rule・msgのタブ区切り連結からSHA-256を算出する。

    位置情報が欠落している（None）場合は空文字として連結する。tabをセパレーターにする
    ことで、各フィールド内にtabが含まれなければ衝突しない。
    """
    parts = [
        error.command,
        error.file,
        str(error.line) if error.line is not None else "",
        str(error.col) if error.col is not None else "",
        error.rule or "",
        error.message,
    ]
    key = "\t".join(parts)
    return hashlib.sha256(key.encode("utf-8")).hexdigest()
