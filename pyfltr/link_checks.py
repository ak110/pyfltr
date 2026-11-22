import json
import typing
import urllib.parse


def _http_status(entry: dict[str, typing.Any]) -> dict[str, typing.Any] | None:
    url = entry.get("url")
    status = entry.get("status")
    if not isinstance(url, str) or not isinstance(status, dict):
        return None
    try:
        parsed = urllib.parse.urlsplit(url)
    except ValueError:
        return None
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        return None
    return status


def is_transient_failure(entry: dict[str, typing.Any], *, timeout: bool = False) -> bool:
    status = _http_status(entry)
    if status is None:
        return False
    if timeout:
        return status.get("text") == "Timeout"
    code = status.get("code")
    return isinstance(code, int) and not isinstance(code, bool) and 500 <= code < 600


def is_network_failure(entry: dict[str, typing.Any]) -> bool:
    status = _http_status(entry)
    if status is None or "code" in status:
        return False
    text = status.get("text")
    return isinstance(text, str) and text.startswith("Network error")


def classify_failures(output: str) -> tuple[bool, bool] | None:
    try:
        data = json.loads("\n".join(line for line in output.splitlines() if not line.startswith("Hint: ")))
    except json.JSONDecodeError:
        return None
    if not isinstance(data, dict):
        return None
    counts = [data.get(key) for key in ("errors", "timeouts", "unknown", "unsupported")]
    if any(not isinstance(count, int) or isinstance(count, bool) or count < 0 for count in counts):
        return None
    if data["unknown"]:
        return None
    transient: list[bool] = []
    has_retryable = False
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
                has_retryable |= (temporary and not is_timeout) or (not is_timeout and is_network_failure(entry))
        if count != data[count_name]:
            return None
    if not transient:
        return None
    return all(transient), has_retryable
