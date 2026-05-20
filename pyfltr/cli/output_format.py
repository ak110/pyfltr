"""出力形式の解決とログ設定。

`resolve_output_format`（優先順位付き出力形式決定）と、
`text_logger` / `structured_logger`の設定切り替えを担う。

pyfltrは3系統のloggerを役割分担する。

- root（system logger）: 常にstderr。抑止しない（設定エラー・アーカイブ初期化失敗等を送出する）
- `pyfltr.textout`: 人間向けテキスト出力。`configure_text_output`で出力先を切り替える
- `pyfltr.structured`: JSONL / SARIF / Code Quality等の構造化出力。
  `configure_structured_output`で出力先を切り替える

stdout占有は出力形式が`jsonl` / `sarif` / `code-quality`かつ`--output-file`未指定時のみ発生する。
その場合は`text_logger`の出力先をstderrへ切り替える運用とする。

モノレポモードのサブプロジェクト分割実行では、各サブプロジェクトに対する個別ツール起動が
パイプラインの内側で繰り返される。loggerはプロセスごとに1度だけ設定する設計で、サブ
プロジェクト切替の都度`configure_loggers`を呼び直さない（出力先の二重設定を避ける）。
"""

import collections.abc
import dataclasses
import logging
import os
import pathlib
import threading
import typing

logger = logging.getLogger(__name__)

# 人間向けテキスト出力用の専用logger（進捗・詳細ログ・summary・warnings・`--only-failed`案内）。
# system logger（root）と分離することで、format別に出力先（stdout / stderr）と
# ログレベルを独立に切り替えられる。propagate=Falseでrootへのpropagateを止め、
# rootのstderrハンドラーと重複発火しないようにする。
text_logger = logging.getLogger("pyfltr.textout")
text_logger.propagate = False

# text_logger への並行書き込みを保護するロック。
# pipeline.py と render.py が同一インスタンスを共有し、行間への混入を防ぐ。
text_output_lock = threading.Lock()

# 構造化出力（JSONL / SARIF）用の専用logger。出力先は`configure_structured_output`で
# StreamHandler（stdout）またはFileHandler（`--output-file`）に切り替える。
# propagate=Falseでroot経由の二重出力とlevel継承の副作用を防ぐ。
structured_logger = logging.getLogger("pyfltr.structured")
structured_logger.propagate = False


OUTPUT_FORMAT_ENV = "PYFLTR_OUTPUT_FORMAT"
"""出力形式を環境変数で既定指定するためのキー名。"""

AGENT_INDICATOR_ENVS: tuple[str, ...] = ("AI_AGENT", "CODEX_CI", "CLAUDECODE", "CURSOR_AGENT")
"""エージェント実行を示す環境変数名のタプル。
`AI_AGENT`は慣習的な総称、`CODEX_CI`はCodex CI、`CLAUDECODE`はClaude Code、`CURSOR_AGENT`はCursor。
走査順・採用判定は`resolve_output_format`参照。
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
    """`resolve_output_format`の戻り値。決定値と由来ラベルの2要素で構成する。

    `source`はJSONL `header.format_source`へ解決経路を露出する目的で保持する。
    """

    format: str
    source: str


def resolve_output_format(
    parser: typing.Any,
    cli_value: str | None,
    *,
    valid_values: collections.abc.Set[str],
    subcommand_default: str | None = None,
    ai_agent_default: str | None = None,
    final_default: str = "text",
) -> OutputFormatResolution:
    """出力形式を共通の優先順位で決定する。

    優先順位は「CLI > `PYFLTR_OUTPUT_FORMAT` > サブコマンド既定値 > エージェント検出変数 > 最終既定値」。
    CLI明示値（`cli_value`）と`PYFLTR_OUTPUT_FORMAT`は利用者が意識的に指定した値とみなし、
    サブコマンド既定値・エージェント検出より優先する。これによりエージェント環境下や
    `run-for-agent`配下でも`PYFLTR_OUTPUT_FORMAT=text`で元の形式に戻すことができる。

    エージェント検出は`AGENT_INDICATOR_ENVS`の先頭から走査する。
    最初に非空値（空文字列でない値）が設定された変数を採用し、
    `format_source`は`env.<検出変数名>`の形で返す。値の中身は問わない。

    Args:
        parser: 環境変数バリデーションエラー時の`parser.error`呼び出しに使う。
        cli_value: CLIで明示された`--output-format`の値。未指定時は`None`。
        valid_values: サブコマンドが受理する出力形式集合。`PYFLTR_OUTPUT_FORMAT`の値検証と、
            サブコマンド既定値・エージェント検出時既定値の採否判定に使う。
        subcommand_default: サブコマンド固有の既定値（例: `run-for-agent`では`"jsonl"`）。
            `valid_values`に含まれない場合は無視する。`None`の場合は次段階へ進む。
        ai_agent_default: エージェント検出時に採用する既定値。実行系・参照系では`"jsonl"`、
            `command-info`では`"json"`を渡す。`None`または`valid_values`に含まれない場合は
            エージェント検出を無視する。
        final_default: いずれの解決経路にも該当しない場合の最終既定値。

    Returns:
        解決済みの出力形式と由来ラベルを保持する`OutputFormatResolution`。
    """
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
        for env_name in AGENT_INDICATOR_ENVS:
            indicator_value = os.environ.get(env_name)
            if indicator_value is not None and indicator_value != "":
                return OutputFormatResolution(format=ai_agent_default, source=f"env.{env_name}")
    return OutputFormatResolution(format=final_default, source=FORMAT_SOURCE_FALLBACK)


def configure_text_output(stream: typing.TextIO, *, level: int = logging.INFO) -> None:
    """text_logger の出力先とログレベルを差し替える。

    既存ハンドラーを全て外してから `StreamHandler(stream)` を新規追加する。
    同一プロセス内で `run()` が複数回呼ばれるケースに備えて、呼び出し毎に完全に
    再構築する（古いハンドラーが残って二重出力・古いstream参照が残るのを避ける）。
    logger役割分担の全体像は本モジュールのdocstring参照。
    """
    for existing in list(text_logger.handlers):
        text_logger.removeHandler(existing)
    handler = logging.StreamHandler(stream)
    handler.setFormatter(logging.Formatter("%(message)s"))
    text_logger.addHandler(handler)
    text_logger.setLevel(level)


def configure_structured_output(destination: typing.TextIO | pathlib.Path | None) -> None:
    """structured_logger の出力先を切り替える。

    - `None`: ハンドラーを全て外す（jsonl/sarifを出力しないformat向け）
    - `TextIO`: `StreamHandler(destination)` を設定する
    - `pathlib.Path`: `FileHandler(destination, mode="w", encoding="utf-8")` を設定する。
      親ディレクトリは自動作成する

    levelは常に `logging.INFO` で固定する。root loggerがWARNING初期化でも
    structured_logger側はINFO記録を破棄しないようにするため。
    `--output-file` 指定時は `pathlib.Path` を渡してファイル出力へ切り替えることで
    stdout占有を解除し、人間向けtext出力をstdoutへ戻すことができる。
    logger役割分担の全体像は本モジュールのdocstring参照。
    """
    for existing in list(structured_logger.handlers):
        structured_logger.removeHandler(existing)
        if isinstance(existing, logging.FileHandler):
            existing.close()
    if destination is None:
        structured_logger.setLevel(logging.INFO)
        return
    handler: logging.Handler
    if isinstance(destination, pathlib.Path):
        destination.parent.mkdir(parents=True, exist_ok=True)
        handler = logging.FileHandler(destination, mode="w", encoding="utf-8")
    else:
        handler = logging.StreamHandler(destination)
    handler.setFormatter(logging.Formatter("%(message)s"))
    structured_logger.addHandler(handler)
    structured_logger.setLevel(logging.INFO)
