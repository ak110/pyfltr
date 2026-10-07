"""テキストと構造化出力のlogger設定。

root loggerはstderrへシステム診断を送る。pyfltr.textoutとpyfltr.structuredは
出力形式と出力先に応じて独立に設定し、サブプロジェクトごとの再設定を避ける。
"""

import logging
import pathlib
import threading
import typing

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
