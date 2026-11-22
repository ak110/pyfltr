import logging
import pathlib
import threading
import typing

text_logger = logging.getLogger("pyfltr.textout")
text_logger.propagate = False

text_output_lock = threading.Lock()

structured_logger = logging.getLogger("pyfltr.structured")
structured_logger.propagate = False


def configure_text_output(stream: typing.TextIO, *, level: int = logging.INFO) -> None:
    for existing in list(text_logger.handlers):
        text_logger.removeHandler(existing)
    handler = logging.StreamHandler(stream)
    handler.setFormatter(logging.Formatter("%(message)s"))
    text_logger.addHandler(handler)
    text_logger.setLevel(level)


def configure_structured_output(destination: typing.TextIO | pathlib.Path | None) -> None:
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
