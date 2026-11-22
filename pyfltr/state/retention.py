import collections.abc
import dataclasses
import datetime as dt
import json
import pathlib
import shutil


@dataclasses.dataclass(frozen=True)
class RetentionPolicy:
    max_entries: int
    """保存する最大世代数。"""
    max_size_bytes: int
    """ディレクトリ全体の合計バイト数の上限。"""
    max_age_days: int
    """保存期間の上限（日数）。"""


def timestamp_of(entry_dir: pathlib.Path, *, meta_filename: str, timestamp_key: str) -> dt.datetime | None:
    meta_path = entry_dir / meta_filename
    if not meta_path.exists():
        return None
    try:
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    raw = meta.get(timestamp_key)
    if not raw:
        return None
    try:
        return dt.datetime.fromisoformat(raw)
    except ValueError:
        return None


def dir_size(path: pathlib.Path) -> int:
    total = 0
    for entry in path.rglob("*"):
        if entry.is_file():
            try:
                total += entry.stat().st_size
            except OSError:
                continue
    return total


def rmtree_silent(path: pathlib.Path) -> None:
    shutil.rmtree(path, ignore_errors=True)


@dataclasses.dataclass(frozen=True)
class RetentionEntry:
    name: str
    timestamp: dt.datetime | None
    is_active: bool


def select_expired(
    entries: collections.abc.Iterable[RetentionEntry],
    policy: RetentionPolicy,
    *,
    now: dt.datetime,
    sort_by_time: bool,
) -> tuple[list[str], list[RetentionEntry]]:
    remaining = sorted(entries, key=lambda entry: entry.name)
    times = {
        entry.name: entry.timestamp.replace(tzinfo=dt.UTC)
        if entry.timestamp is not None and entry.timestamp.tzinfo is None
        else entry.timestamp
        for entry in remaining
    }
    if sort_by_time:
        oldest = dt.datetime.min.replace(tzinfo=dt.UTC)
        remaining.sort(key=lambda entry: (times[entry.name] or oldest, entry.name))
    removed: list[str] = []

    def remove(entry: RetentionEntry) -> None:
        removed.append(entry.name)
        remaining.remove(entry)

    if policy.max_age_days > 0:
        age_limit = dt.timedelta(days=policy.max_age_days)
        for entry in list(remaining):
            timestamp = times[entry.name]
            if not entry.is_active and timestamp is not None and now - timestamp > age_limit:
                remove(entry)

    if policy.max_entries > 0:
        for entry in [entry for entry in remaining if not entry.is_active]:
            if len(remaining) <= policy.max_entries:
                break
            remove(entry)

    return removed, remaining


def select_oversized(
    entries: collections.abc.Sequence[RetentionEntry], sizes: collections.abc.Mapping[str, int], max_size_bytes: int
) -> list[str]:
    total = sum(sizes[entry.name] for entry in entries)
    removed: list[str] = []
    for entry in entries:
        if total <= max_size_bytes:
            break
        if not entry.is_active:
            removed.append(entry.name)
            total -= sizes[entry.name]
    return removed


def cleanup_generational_directory(
    root: pathlib.Path,
    policy: RetentionPolicy,
    *,
    meta_filename: str,
    timestamp_key: str,
    fallback_timestamp_key: str | None = None,
    is_active: collections.abc.Callable[[pathlib.Path], bool] | None = None,
) -> list[str]:
    if not root.exists():
        return []
    directories = {entry.name: entry for entry in root.iterdir() if entry.is_dir()}

    def reference_time(entry: pathlib.Path) -> dt.datetime | None:
        timestamp = timestamp_of(entry, meta_filename=meta_filename, timestamp_key=timestamp_key)
        if timestamp is None and fallback_timestamp_key is not None:
            timestamp = timestamp_of(entry, meta_filename=meta_filename, timestamp_key=fallback_timestamp_key)
        return timestamp

    entries = [
        RetentionEntry(name, reference_time(path), is_active is not None and is_active(path))
        for name, path in directories.items()
    ]
    removed, remaining = select_expired(
        entries, policy, now=dt.datetime.now(dt.UTC), sort_by_time=fallback_timestamp_key is not None
    )
    if policy.max_size_bytes > 0:
        sizes = {entry.name: dir_size(directories[entry.name]) for entry in remaining}
        removed += select_oversized(remaining, sizes, policy.max_size_bytes)
    for name in removed:
        rmtree_silent(directories[name])
    return removed
