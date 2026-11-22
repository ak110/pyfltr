import collections.abc
import contextlib
import contextvars
import dataclasses
import logging
import threading
import traceback
import typing

logger = logging.getLogger(__name__)

FilteredReason = typing.Literal["excluded", "missing", "external"]


@dataclasses.dataclass
class DuplicateSuppressionState:
    warning_keys: set[tuple[str, str]] = dataclasses.field(default_factory=set)
    filtered_file_keys: set[tuple[str, "FilteredReason"]] = dataclasses.field(default_factory=set)


_warnings: list[dict[str, typing.Any]] = []
_filtered_direct_files: list[tuple[str, FilteredReason]] = []
_delivery_lock = threading.RLock()


@dataclasses.dataclass
class _DeliveryState:
    depth: int = 0
    deferred: list[dict[str, typing.Any]] = dataclasses.field(default_factory=list)
    delivered_ids: set[int] = dataclasses.field(default_factory=set)


_delivery_state = _DeliveryState()
_duplicate_suppression_state: contextvars.ContextVar[DuplicateSuppressionState | None] = contextvars.ContextVar(
    "duplicate_suppression_state",
    default=None,
)


@contextlib.contextmanager
def suppress_duplicates() -> collections.abc.Generator[None, None, None]:
    if _duplicate_suppression_state.get() is not None:
        yield
        return
    token = _duplicate_suppression_state.set(DuplicateSuppressionState())
    try:
        yield
    finally:
        _duplicate_suppression_state.reset(token)


def current_suppression_state() -> DuplicateSuppressionState | None:
    return _duplicate_suppression_state.get()


def adopt_suppression_state(state: DuplicateSuppressionState | None) -> None:
    if state is not None:
        _duplicate_suppression_state.set(state)


@contextlib.contextmanager
def defer_stderr() -> collections.abc.Generator[None, None, None]:
    with _delivery_lock:
        _delivery_state.depth += 1
    try:
        yield
    finally:
        pending: list[dict[str, typing.Any]] = []
        with _delivery_lock:
            _delivery_state.depth -= 1
            if _delivery_state.depth == 0:
                pending = [entry for entry in _delivery_state.deferred if id(entry) not in _delivery_state.delivered_ids]
                _delivery_state.deferred.clear()
                _delivery_state.delivered_ids.clear()
        for entry in pending:
            logger.warning(format_warning_text(entry))


def mark_delivered(entries: collections.abc.Iterable[dict[str, typing.Any]]) -> None:
    with _delivery_lock:
        if _delivery_state.depth == 0:
            return
        _delivery_state.delivered_ids.update(id(entry) for entry in entries)


def emit_warning(source: str, message: str, *, hint: str, exc_info: bool = False) -> None:
    if not hint.strip():
        raise ValueError("hintには次の操作か「対応不要」を表す空白以外の文字列を指定してください")
    suppression_state = _duplicate_suppression_state.get()
    if suppression_state is not None:
        key = (source, message)
        with _delivery_lock:
            if key in suppression_state.warning_keys:
                return
            suppression_state.warning_keys.add(key)
    stored = message
    if exc_info:
        tb = traceback.format_exc().rstrip()
        if tb and tb != "NoneType: None":
            stored = f"{message}\n{tb}"
    entry: dict[str, typing.Any] = {"source": source, "message": stored, "hint": hint}
    with _delivery_lock:
        _warnings.append(entry)
        if _delivery_state.depth > 0:
            _delivery_state.deferred.append(entry)
            return
    logger.warning(format_warning_text({"message": message, "hint": hint}), exc_info=exc_info)


def format_warning_text(entry: dict[str, typing.Any]) -> str:
    message = str(entry["message"])
    return f"{message} 対処: {entry['hint']}"


def collected_warnings() -> list[dict[str, typing.Any]]:
    return list(_warnings)


def add_filtered_direct_file(path: str, *, reason: FilteredReason) -> None:
    suppression_state = _duplicate_suppression_state.get()
    key = (path, reason)
    with _delivery_lock:
        if suppression_state is not None:
            if key in suppression_state.filtered_file_keys:
                return
            suppression_state.filtered_file_keys.add(key)
        _filtered_direct_files.append((path, reason))


def filtered_direct_files(*, reason: FilteredReason | None = None) -> list[str]:
    if reason is None:
        return [path for path, _ in _filtered_direct_files]
    return [path for path, r in _filtered_direct_files if r == reason]


def clear() -> None:
    _warnings.clear()
    _filtered_direct_files.clear()
