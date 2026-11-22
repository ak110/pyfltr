import pathlib
import typing

import tomlkit
import tomlkit.exceptions

from pyfltr.config.config import read_config_text, unwrap_tomlkit
from pyfltr.config.model import DEFAULT_CONFIG
from pyfltr.config.validation import format_unknown_key_message


def read_config_values(path: pathlib.Path) -> dict[str, typing.Any]:
    if not path.exists():
        return {}
    try:
        text = read_config_text(path)
        data = tomlkit.parse(text)
    except tomlkit.exceptions.TOMLKitError as e:
        raise ValueError(f"設定ファイルのTOML構文が不正です: {path}: {e}") from e
    raw = data.get("tool", {})
    raw = raw.get("pyfltr", {}) if isinstance(raw, dict) else {}
    return unwrap_tomlkit(raw) if isinstance(raw, dict) else {}


def set_config_value(
    path: pathlib.Path,
    key: str,
    value: typing.Any,
    *,
    create_if_missing: bool = False,
) -> None:
    if path.exists():
        text = read_config_text(path)
        try:
            doc = tomlkit.parse(text)
        except tomlkit.exceptions.TOMLKitError as e:
            raise ValueError(f"設定ファイルのTOML構文が不正です: {path}: {e}") from e
    else:
        if not create_if_missing:
            raise FileNotFoundError(f"設定ファイルが存在しません: {path}")
        path.parent.mkdir(parents=True, exist_ok=True)
        doc = tomlkit.document()

    tool_table = doc.get("tool")
    if tool_table is None:
        tool_table = tomlkit.table()
        doc["tool"] = tool_table
    pyfltr_table = tool_table.get("pyfltr")
    if pyfltr_table is None:
        pyfltr_table = tomlkit.table()
        tool_table["pyfltr"] = pyfltr_table

    pyfltr_table[key] = value
    path.write_text(tomlkit.dumps(doc), encoding="utf-8")


def delete_config_value(path: pathlib.Path, key: str) -> bool:
    if not path.exists():
        return False
    text = read_config_text(path)
    try:
        doc = tomlkit.parse(text)
    except tomlkit.exceptions.TOMLKitError as e:
        raise ValueError(f"設定ファイルのTOML構文が不正です: {path}: {e}") from e
    tool_table = doc.get("tool")
    if tool_table is None:
        return False
    pyfltr_table = tool_table.get("pyfltr")
    if pyfltr_table is None:
        return False
    if key not in pyfltr_table:
        return False
    del pyfltr_table[key]
    path.write_text(tomlkit.dumps(doc), encoding="utf-8")
    return True


def parse_config_value(key: str, raw: str) -> typing.Any:
    if key not in DEFAULT_CONFIG:
        raise ValueError(format_unknown_key_message(key, DEFAULT_CONFIG.keys()))
    default = DEFAULT_CONFIG[key]
    if isinstance(default, bool):
        lowered = raw.strip().lower()
        if lowered in ("true", "1"):
            return True
        if lowered in ("false", "0"):
            return False
        raise ValueError(f"`{key}` には true / false / 1 / 0 のいずれかを指定してください: 実値 {raw!r}")
    if isinstance(default, int):
        try:
            return int(raw)
        except ValueError as e:
            raise ValueError(f"`{key}` には整数を指定してください: 実値 {raw!r}") from e
    if isinstance(default, str):
        return raw
    if isinstance(default, list):
        return raw.split(",") if raw else []
    if isinstance(default, dict):
        raise ValueError(f"`{key}` は辞書型のためCLIから直接設定できません。pyproject.tomlを直接編集してください")
    raise ValueError(
        f"`{key}` の値型はCLI経由では設定できません: {type(default).__name__}。pyproject.tomlを直接編集してください"
    )
