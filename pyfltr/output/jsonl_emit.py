import threading
import time
import typing

import pyfltr.output.logging_

# 複数行を1つの排他境界で書き込み、heartbeatの最終出力時刻と同期させる。
_write_lock = threading.Lock()


class _JsonlOutputTimeState:
    monotonic_time: float | None = None


_output_time_state = _JsonlOutputTimeState()


def get_last_jsonl_output_time() -> float | None:
    with _write_lock:
        return _output_time_state.monotonic_time


def set_last_jsonl_output_time(value: float) -> None:
    with _write_lock:
        _output_time_state.monotonic_time = value


def _emit_structured(line: str) -> None:
    pyfltr.output.logging_.structured_logger.info(line)
    _output_time_state.monotonic_time = time.monotonic()


def emit_record(line: str) -> None:
    with _write_lock:
        _emit_structured(line)


def emit_records(lines: typing.Iterable[str]) -> None:
    with _write_lock:
        for line in lines:
            _emit_structured(line)
