import datetime as dt

from pyfltr.state.retention import RetentionEntry, RetentionPolicy, select_expired, select_oversized

_NOW = dt.datetime(2026, 1, 31, tzinfo=dt.UTC)


def _policy(*, max_entries: int = 0, max_size_bytes: int = 0, max_age_days: int = 0) -> RetentionPolicy:
    return RetentionPolicy(max_entries=max_entries, max_size_bytes=max_size_bytes, max_age_days=max_age_days)


def test_time_order_treats_missing_as_oldest_and_naive_as_utc() -> None:
    entries = [
        RetentionEntry("a", dt.datetime(2026, 1, 30, tzinfo=dt.UTC), False),
        RetentionEntry("b", dt.datetime(2026, 1, 29, 12), False),
        RetentionEntry("d", None, False),
        RetentionEntry("c", None, False),
    ]
    removed, remaining = select_expired(entries, _policy(max_age_days=1), now=_NOW, sort_by_time=True)
    assert removed == ["b"]
    assert [entry.name for entry in remaining] == ["c", "d", "a"]


def test_name_order_without_time_sort() -> None:
    entries = [RetentionEntry(name, None, False) for name in ("c", "a", "b")]
    removed, remaining = select_expired(entries, _policy(max_entries=1), now=_NOW, sort_by_time=False)
    assert removed == ["a", "b"]
    assert [entry.name for entry in remaining] == ["c"]


def test_active_entry_is_kept_and_counted() -> None:
    old = dt.datetime(2025, 1, 1, tzinfo=dt.UTC)
    entries = [RetentionEntry("a", old, True), RetentionEntry("b", old, False), RetentionEntry("c", _NOW, False)]
    removed, remaining = select_expired(entries, _policy(max_entries=2), now=_NOW, sort_by_time=True)
    assert removed == ["b"]
    assert [entry.name for entry in remaining] == ["a", "c"]
    assert select_oversized(remaining, {"a": 10, "c": 5}, 12) == ["c"]


def test_size_selection_covers_only_remaining_entries() -> None:
    old = dt.datetime(2025, 1, 1, tzinfo=dt.UTC)
    entries = [RetentionEntry("a", old, False), RetentionEntry("b", _NOW, False), RetentionEntry("c", _NOW, False)]
    removed, remaining = select_expired(entries, _policy(max_age_days=30), now=_NOW, sort_by_time=True)
    assert removed == ["a"]
    assert select_oversized(remaining, {"b": 4, "c": 4}, 4) == ["b"]
