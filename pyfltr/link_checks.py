"""リンク診断の一時的失敗の分類。"""

import json
import typing
import urllib.parse


def is_transient_failure(entry: dict[str, typing.Any], *, timeout: bool = False) -> bool:
    """HTTP(S)の5xxまたは応答タイムアウトと確認できる場合に真を返す。"""
    url = entry.get("url")
    status = entry.get("status")
    if not isinstance(url, str) or not isinstance(status, dict):
        return False
    try:
        parsed = urllib.parse.urlsplit(url)
    except ValueError:
        return False
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        return False
    if timeout:
        return status.get("text") == "Timeout"
    code = status.get("code")
    return isinstance(code, int) and not isinstance(code, bool) and 500 <= code < 600


def classify_failures(output: str) -> tuple[bool, bool] | None:
    """完全な失敗JSONから（一時障害のみか、5xxを含むか）を返す。

    非0終了の格下げに使うため、進捗や別のエラーが混入したJSONは救済しない。
    lycheeがstderrへ出力する通常のHint行だけを除き、残り全体をJSONとして読む。
    失敗件数とmapの全項目を比較し、欠落・分類不能を通常失敗として保持する。
    """
    try:
        data = json.loads("\n".join(line for line in output.splitlines() if not line.startswith("Hint: ")))
    except json.JSONDecodeError:
        return None
    if not isinstance(data, dict):
        return None
    counts = [data.get(key) for key in ("errors", "timeouts", "unknown", "unsupported")]
    if any(not isinstance(count, int) or isinstance(count, bool) or count < 0 for count in counts):
        return None
    if data["unknown"] or data["unsupported"]:
        return None
    transient: list[bool] = []
    has_server_error = False
    for map_name, count_name in (("error_map", "errors"), ("timeout_map", "timeouts")):
        entries_by_file = data.get(map_name)
        if not isinstance(entries_by_file, dict):
            return None
        count = 0
        for file_path, entries in entries_by_file.items():
            if not isinstance(file_path, str) or not file_path or not isinstance(entries, list):
                return None
            for entry in entries:
                if not isinstance(entry, dict):
                    return None
                count += 1
                is_timeout = map_name == "timeout_map"
                temporary = is_transient_failure(entry, timeout=is_timeout)
                transient.append(temporary)
                has_server_error |= temporary and not is_timeout
        if count != data[count_name]:
            return None
    if not transient:
        return None
    return all(transient), has_server_error
