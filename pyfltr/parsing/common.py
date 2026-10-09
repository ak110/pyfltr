"""診断解析の共通処理。"""

import json
import pathlib
import re
import typing

import pyfltr.paths
import pyfltr.rule_urls
from pyfltr.diagnostics import ErrorLocation


def try_json_loads(output: str) -> typing.Any:
    """JSONパースを試みる。失敗時はNoneを返す。

    一部ツール（例: pylint）は`PYTHONDEVMODE=1`環境で読み込んだプラグインの
    `DeprecationWarning`などをJSON本体の前にテキストとして出力する。そのままでは
    パースが必ず失敗するため、先頭の`{`または`[`を見つけて、それ以前の
    不要文字列を除去してから再試行する。JSON本体の後ろに進捗表示が続く場合も、
    デコーダーが読み取ったJSON部分だけを採用する。
    """
    stripped = output.strip()
    if not stripped:
        return None
    try:
        return json.loads(stripped)
    except json.JSONDecodeError:
        pass
    # JSON前後が進捗表示で汚染されたケースを救済する。入れ子の開始位置からも
    # decodeできるため、最も後ろまで読み取れた候補をJSON本体として採用する。
    decoder = json.JSONDecoder()
    candidates: list[tuple[int, typing.Any]] = []
    for match in re.finditer(r"[\{\[]", stripped):
        try:
            value, end_index = decoder.raw_decode(stripped[match.start() :])
        except json.JSONDecodeError:
            continue
        candidates.append((match.start() + end_index, value))
    if not candidates:
        return None
    return max(candidates, key=lambda candidate: candidate[0])[1]


def json_list_field(output: str, field: str) -> list[typing.Any] | None:
    """トップレベルJSONオブジェクトのリストフィールドを返す。"""
    data = try_json_loads(output)
    if not isinstance(data, dict):
        return None
    value = data.get(field, [])
    return value if isinstance(value, list) else None


def normalize_severity(value: typing.Any) -> str | None:
    """生のseverity値を`"error" / "warning" / "info"`の3値に正規化する。

    `high` / `medium` / `low`はbanditのseverity表現に対応し、それぞれ
    `error` / `warning` / `info`へマップする。
    未知の値やNoneは`None`を返し、JSONL出力側で省略される。
    """
    if value is None:
        return None
    if isinstance(value, int):
        return eslint_severity(value)
    if not isinstance(value, str):
        return None
    lowered = value.strip().lower()
    if not lowered:
        return None
    if lowered in ("error", "fatal", "high"):
        return "error"
    if lowered in ("warning", "warn", "medium"):
        return "warning"
    if lowered in ("info", "information", "informational", "note", "notice", "hint", "style", "convention", "refactor", "low"):
        return "info"
    return None


def to_inclusive_end_position(line: int, end_line: int | None, end_col: int | None) -> tuple[int | None, int | None]:
    """終了位置を診断範囲の最終行と、その行における終了列の組へ揃える。

    ツールが返す終了位置は範囲末尾の次の位置を指す場合がある。範囲が行末で終わると
    終了位置は次行の先頭となり、終了列が行頭（1起点で1）を指す。この場合の終了行は
    範囲に含まれないため直前の行へ補正する。あわせて終了列を省略する。
    元の終了列は次行上の値であり、直前行の終端排他列はツール出力から算出できないためである。

    終了行が開始行と同じか前の場合は補正しない。開始と終了が同じ位置を指すゼロ幅の範囲で、
    終了行を開始行より前へ動かさないためである。
    終了行が最終行そのものを指すツール（pylint・bandit）では終了列が行頭を指す範囲が
    成立しないため、本関数を一律に適用しても値は変わらない。

    textlintは文を切り出すライブラリを用いるルールで列が行内位置にならない場合があり、
    判定材料の確からしさが他ツールより低い。実出力では終了列が行頭へずれる事象を
    観測していないため補正対象から外していないが、行と列を1箇所で決める本関数の形を保ち、
    判定が変わる場合に両者が同時に追随するようにする。

    行と列を別々の関数で決めると、呼び出し側が異なる開始行を渡した場合に
    両者が別の行を指しうる。組で返すことで、この不整合が成立しないようにする。
    """
    if end_col == 1 and end_line is not None and end_line > line:
        return end_line - 1, None
    return end_line, end_col


def eslint_severity(value: typing.Any) -> str | None:
    """ESLint/textlint の severity 数値を文字列に変換する。"""
    if value == 2:
        return "error"
    if value == 1:
        return "warning"
    return None


def json_int(value: typing.Any) -> int | None:
    """JSONの値が整数の場合だけ返す。"""
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def one_based_offset(value: typing.Any) -> int | None:
    """非負の0起点オフセットを1起点へ変換する。"""
    offset = json_int(value)
    return offset + 1 if offset is not None and offset >= 0 else None


def parse_file_messages_format(
    data: list[dict],
    message_to_location: typing.Callable[[dict, str], ErrorLocation | None],
) -> list[ErrorLocation]:
    """topレベルが`[{filePath, messages:[...]}]`形式のJSON配列を処理する共通ヘルパー。

    eslint / textlint の出力形式に共通する「ファイルごとのmessages配列」構造を処理する。
    各メッセージから`ErrorLocation`へのマッピングは`message_to_location`に委ねる。
    `message_to_location`が`None`を返したメッセージはスキップする。
    """
    results: list[ErrorLocation] = []
    for entry in data:
        if not isinstance(entry, dict):
            continue
        file_path = str(entry.get("filePath", ""))
        messages = entry.get("messages", [])
        if not isinstance(messages, list):
            continue
        for msg in messages:
            if not isinstance(msg, dict):
                continue
            location = message_to_location(msg, file_path)
            if location is not None:
                results.append(location)
    return results


def parse_optional_int(value: str | None) -> int | None:
    """任意の整数グループの値を変換する。

    グループが存在しない場合と整数として解釈できない場合はNoneを返す。
    位置情報を持たない診断でも他フィールドの抽出結果を保持するため、変換失敗を無視する。
    """
    if value is None:
        return None
    try:
        return int(value)
    except ValueError:
        return None


def parse_with_pattern(command: str, output: str, pattern: str) -> list[ErrorLocation]:
    """正規表現パターンでエラー箇所をパースする。

    パターンに名前付きグループ`rule`が含まれる場合、マッチ内容を
    `ErrorLocation.rule`に格納し、`rule_urls.build_rule_url()`でURLも補完する。
    名前付きグループ`severity`を含む場合は`normalize_severity`で正規化した値を
    `ErrorLocation.severity`へ格納する（biome `::notice`→`"info"`等）。
    名前付きグループ`end_line`・`end_col`を含む場合は同名フィールドへ格納する
    （biomeの`endLine`・`endColumn`等）。格納前に`to_inclusive_end_position`を通し、
    終了位置が次行の先頭を指す場合は終了行を直前の行へ補正する。
    本経路は利用者定義の`error_pattern`にも適用されるため、終了行が最終行そのものを指すツールを
    取り込む場合も同じ補正の対象となる。対象の補正は終了列が行頭を指す場合だけ発火し、
    そのようなツールでは終了列が次行の先頭を指す組が成立しないため、値は変わらない。
    """
    compiled = re.compile(pattern)
    results: list[ErrorLocation] = []
    for line in output.splitlines():
        match = compiled.search(line)
        if match is None:
            continue
        groups = match.groupdict()
        file_path = groups.get("file", "")
        line_str = groups.get("line", "0")
        message = groups.get("message") or ""
        try:
            line_num = int(line_str)
        except ValueError:
            continue
        col_num = parse_optional_int(groups.get("col"))
        end_line_num = parse_optional_int(groups.get("end_line"))
        end_col_num = parse_optional_int(groups.get("end_col"))
        rule_raw = groups.get("rule")
        rule = rule_raw.strip() if isinstance(rule_raw, str) and rule_raw.strip() else None
        rule_url = pyfltr.rule_urls.build_rule_url(command, rule) if rule is not None else None
        severity = normalize_severity(groups.get("severity"))
        end_line_num, end_col_num = to_inclusive_end_position(line_num, end_line_num, end_col_num)
        results.append(
            ErrorLocation(
                file=pyfltr.paths.normalize_separators(file_path),
                line=line_num,
                col=col_num,
                command=command,
                message=message.strip(),
                rule=rule,
                severity=severity,
                rule_url=rule_url,
                end_line=end_line_num,
                end_col=end_col_num,
            )
        )
    return results


def extract_last_line(output: str) -> str | None:
    """テキスト出力の末尾から意味のある行を抽出する。

    JSON出力（先頭が`[`または`{`）は対象外。区切り線のみの行はスキップする。
    """
    stripped = output.strip()
    if not stripped or stripped[0] in ("[", "{"):
        return None
    for line in reversed(stripped.splitlines()):
        line = line.strip()
        if line and not re.fullmatch(r"[=\-*#]+", line):
            return line
    return None


def is_project_path(normalized_path: str) -> bool:
    """正規化済みパスがプロジェクト内のファイルかを判定する。

    以下を全て満たす場合にプロジェクト内と見なす:
    - 相対パスである（絶対パスはcwd外 = 標準ライブラリ等）
    - `..`で始まらない（uv管理Pythonの標準ライブラリ等）
    - `.venv/`で始まらない（仮想環境内サードパーティー）
    - `site-packages/`・`dist-packages/`を含まない（名前の異なる仮想環境内サードパーティー）
    """
    if pathlib.PurePosixPath(normalized_path).is_absolute():
        return False
    if normalized_path.startswith(".."):
        return False
    if normalized_path.startswith(".venv/"):
        return False
    return not ("site-packages/" in normalized_path or "dist-packages/" in normalized_path)
