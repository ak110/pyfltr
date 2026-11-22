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
    entries = sorted((entry for entry in root.iterdir() if entry.is_dir()), key=lambda p: p.name)
    if not entries:
        return []

    def reference_time(entry: pathlib.Path) -> dt.datetime | None:
        timestamp = timestamp_of(entry, meta_filename=meta_filename, timestamp_key=timestamp_key)
        if timestamp is None and fallback_timestamp_key is not None:
            timestamp = timestamp_of(entry, meta_filename=meta_filename, timestamp_key=fallback_timestamp_key)
        if timestamp is not None and timestamp.tzinfo is None:
            timestamp = timestamp.replace(tzinfo=dt.UTC)
        return timestamp

    times = {entry: reference_time(entry) for entry in entries}
    if fallback_timestamp_key is not None:
        oldest = dt.datetime.min.replace(tzinfo=dt.UTC)
        entries.sort(key=lambda entry: (times[entry] or oldest, entry.name))
    active = {entry for entry in entries if is_active is not None and is_active(entry)}
    removed: list[str] = []

    def remove(entry: pathlib.Path) -> None:
        rmtree_silent(entry)
        removed.append(entry.name)
        entries.remove(entry)

    if policy.max_age_days > 0:
        now = dt.datetime.now(dt.UTC)
        age_limit = dt.timedelta(days=policy.max_age_days)
        for entry in list(entries):
            timestamp = times[entry]
            if entry in active or timestamp is None:
                continue
            if now - timestamp > age_limit:
                remove(entry)

    if policy.max_entries > 0:
        for entry in [entry for entry in entries if entry not in active]:
            if len(entries) <= policy.max_entries:
                break
            remove(entry)

    if policy.max_size_bytes > 0:
        sizes = {entry: dir_size(entry) for entry in entries}
        total = sum(sizes.values())
        for entry in [entry for entry in entries if entry not in active]:
            if total <= policy.max_size_bytes:
                break
            remove(entry)
            total -= sizes[entry]

    return removed
