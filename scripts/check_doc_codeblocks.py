#!/usr/bin/env python3

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
    return list(yaml.safe_load_all(text))


def _format_diagnostic_path(path: pathlib.PurePath) -> str:
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
    for token in MARKDOWN.parse(text):
        if token.type != "fence" or token.map is None:
            continue
        yield _language_from_info(token.info), token.map[0] + 1, token.content


def check_file(path: pathlib.Path) -> list[str]:
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
            errors.append(
                f"{diagnostic_path}:{lineno}:1: {lang}コードブロックを解析できない: {message}"
                f" 対処: コードブロックを{lang}として正しい構文に直す。{lang}として解析させない内容なら、"
                "フェンスの言語指定を内容に合う言語へ変える"
            )
    return errors


def main() -> int:
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
