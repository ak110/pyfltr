#!/usr/bin/env python3
"""ドキュメント内のコード例が、その言語として解析できることを検査する。

利用者がそのまま設定ファイルへ写す前提のコード例が構文不正だと、写した側の
ファイル全体が解析不能になる。taploとcheck-tomlは`*.toml`ファイルのみを
対象としMarkdownのフェンス内を見ないため、本スクリプトで補う。

対象はgit管理下のMarkdownに含まれる`toml`・`yaml`・`json`のフェンスとする。
shellやmermaidなど単一の正解パーサを持たない言語は対象外とする。
"""

import collections.abc
import json
import os
import pathlib
import re
import subprocess
import sys
import tomllib

import yaml
from markdown_it import MarkdownIt

ATTRIBUTE_LANGUAGE_PATTERN = re.compile(r"(?:^|[{\s])\.(?P<lang>[a-zA-Z0-9_+-]+)(?=[\s}])")
MARKDOWN = MarkdownIt("commonmark")


def _parse_yaml_documents(text: str) -> object:
    """YAML文字列に含まれる全ての文書を解析する。"""
    return list(yaml.safe_load_all(text))


def _format_diagnostic_path(path: pathlib.PurePath) -> str:
    """ファイルパスを一意な単一行表現へ変換する。"""
    characters: list[str] = []
    for character in path.as_posix():
        if character not in {"%", "\\"} and character.isprintable():
            characters.append(character)
        else:
            characters.extend(f"%{byte:02X}" for byte in character.encode(errors="surrogateescape"))
    return "".join(characters)


PARSERS: dict[str, collections.abc.Callable[[str], object]] = {
    "toml": tomllib.loads,
    "yaml": _parse_yaml_documents,
    "json": json.loads,
}


def _language_from_info(info: str) -> str:
    """フェンスの情報文字列から言語タグを取得する。"""
    stripped = info.strip()
    if not stripped:
        return ""
    if stripped.startswith("{"):
        match = ATTRIBUTE_LANGUAGE_PATTERN.search(stripped)
        return match.group("lang") if match is not None else ""
    return stripped.split(maxsplit=1)[0]


def iter_code_blocks(
    text: str,
) -> collections.abc.Iterator[tuple[str, int, str]]:
    """CommonMarkのトークンからフェンスコードブロックを取り出す。

    Yields:
        言語タグ、フェンス開始行の行番号（1始まり）、ブロック本文の組。
    """
    for token in MARKDOWN.parse(text):
        if token.type != "fence" or token.map is None:
            continue
        yield _language_from_info(token.info), token.map[0] + 1, token.content


def check_file(path: pathlib.Path) -> list[str]:
    """1ファイルを検査し、検出したエラー行の一覧を返す。"""
    errors: list[str] = []
    diagnostic_path = _format_diagnostic_path(path)
    for lang, lineno, body in iter_code_blocks(path.read_text(encoding="utf-8")):
        parser = PARSERS.get(lang)
        if parser is None:
            continue
        try:
            parser(body)
        except (tomllib.TOMLDecodeError, yaml.YAMLError, json.JSONDecodeError) as error:
            message = " ".join(str(error).split())
            errors.append(f"{diagnostic_path}:{lineno}:1: {lang}コードブロックを解析できない: {message}")
    return errors


def main() -> int:
    """git管理下の全Markdownを検査する。"""
    output = subprocess.run(
        ["git", "ls-files", "-z", "--", "*.md"],
        capture_output=True,
        check=True,
    ).stdout
    errors: list[str] = []
    for name in output.split(b"\0"):
        if not name:
            continue
        path = pathlib.Path(os.fsdecode(name))
        if path.is_file():
            errors.extend(check_file(path))
    for error in errors:
        print(error)
    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(main())
