import collections.abc
import dataclasses
import logging
import os
import typing

logger = logging.getLogger(__name__)

OUTPUT_FORMAT_ENV = "PYFLTR_OUTPUT_FORMAT"
"""出力形式を環境変数で既定指定するためのキー名。"""

AGENT_INDICATOR_ENVS: tuple[str, ...] = ("AI_AGENT", "CODEX_CI", "CLAUDECODE", "CURSOR_AGENT")
"""エージェント実行を示す環境変数名のタプル。
`AI_AGENT`は慣習的な総称、`CODEX_CI`はCodex CI、`CLAUDECODE`はClaude Code、`CURSOR_AGENT`はCursor。
走査順・採用判定は`detect_agent_indicator`参照。
"""

# 解決経路を示す`format_source`の固定語彙。各値はそれ単独で読んで意味が通るよう命名する。
# JSONL `header.format_source`の値・利用者向けドキュメントの説明・テストのassertで参照する。
# エージェント検出経由の値は別系統で、検出した変数ごとに`env.<NAME>`の形で動的に構築する。
FORMAT_SOURCE_CLI = "cli"
FORMAT_SOURCE_ENV_PYFLTR = f"env.{OUTPUT_FORMAT_ENV}"
FORMAT_SOURCE_SUBCOMMAND_DEFAULT = "subcommand_default"
FORMAT_SOURCE_FALLBACK = "fallback"


@dataclasses.dataclass(frozen=True)
class OutputFormatResolution:
    format: str
    source: str


def detect_agent_indicator() -> str | None:
    for env_name in AGENT_INDICATOR_ENVS:
        indicator_value = os.environ.get(env_name)
        if indicator_value is not None and indicator_value != "":
            return env_name
    return None


def resolve_output_format(
    parser: typing.Any,
    cli_value: str | None,
    *,
    valid_values: collections.abc.Set[str],
    subcommand_default: str | None = None,
    ai_agent_default: str | None = None,
    final_default: str = "text",
) -> OutputFormatResolution:
    if cli_value is not None:
        return OutputFormatResolution(format=cli_value, source=FORMAT_SOURCE_CLI)
    env_value = os.environ.get(OUTPUT_FORMAT_ENV)
    if env_value is not None and env_value != "":
        if env_value not in valid_values:
            parser.error(
                f"環境変数 {OUTPUT_FORMAT_ENV} に不正な値が指定されています: {env_value!r} "
                f"(有効値: {', '.join(sorted(valid_values))})"
            )
        return OutputFormatResolution(format=env_value, source=FORMAT_SOURCE_ENV_PYFLTR)
    if subcommand_default is not None and subcommand_default in valid_values:
        return OutputFormatResolution(format=subcommand_default, source=FORMAT_SOURCE_SUBCOMMAND_DEFAULT)
    if ai_agent_default is not None and ai_agent_default in valid_values:
        env_name = detect_agent_indicator()
        if env_name is not None:
            return OutputFormatResolution(format=ai_agent_default, source=f"env.{env_name}")
    return OutputFormatResolution(format=final_default, source=FORMAT_SOURCE_FALLBACK)
