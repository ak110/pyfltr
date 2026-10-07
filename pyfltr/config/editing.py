"""コメントを保持した設定ファイルの読み書き。"""

import pathlib
import typing

import tomlkit
import tomlkit.exceptions

from pyfltr.config.config import read_config_text, unwrap_tomlkit
from pyfltr.config.model import DEFAULT_CONFIG
from pyfltr.config.validation import format_unknown_key_message


def read_config_values(path: pathlib.Path) -> dict[str, typing.Any]:
    """pyproject.tomlまたはglobal config.tomlから`[tool.pyfltr]`配下を返す。

    ファイル不在時は空辞書を返す。TOML構文エラー時は`ValueError`で停止する。
    `pyfltr config get` / `pyfltr config list`の読み取り経路で使用する。
    """
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
    """設定ファイルの`[tool.pyfltr]`配下を更新する。

    既存ファイルはtomlkit経由で読み書きするためコメント・セクション順は保持される。
    `create_if_missing=True`なら、ファイル不在時にディレクトリ含めて新規作成する。
    `create_if_missing=False`でファイル不在なら`FileNotFoundError`を送出する。
    """
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
    """設定ファイルから`[tool.pyfltr]`配下のキーを削除する。

    存在したかをboolで返す。セクションが空になっても削除しない
    （手書きコメントを保持するため）。
    ファイル不在時は`False`を返す。
    """
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
    """文字列値を`DEFAULT_CONFIG[key]`の型に変換する。

    bool / int / str / `list[str]`のみ対応。dict系（`aliases`等）は非対応で
    `ValueError`を送出する（CLI経由でdict編集はサポートしない方針）。

    - bool: `true`/`false`/`1`/`0`を受理（大文字小文字は無視）
    - int: `int(raw)`、失敗で`ValueError`
    - str: そのまま
    - list[str]: カンマ区切りでsplit、要素のtrimは行わない
      （`*-args`系で空白を含むケースに対応するため）
    """
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
