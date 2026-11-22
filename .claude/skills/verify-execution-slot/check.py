"""実行枠の調停を有界状態空間で検査する。"""

import contextlib
import itertools
import os
import pathlib
import typing
from unittest import mock

from pyfltr import file_lock
from pyfltr.command import execution_slot

# 各主体の状態: 未登録、待機中、枠番号、終了。
NEW, WAITING, DONE = -3, -2, -1
ACTORS = 4
State = tuple[tuple[int, ...], tuple[int, ...]]
Trace = tuple[tuple[str, int, int], ...]


class Retry(Exception):
    """実装が今回の取得を見送り、再試行を待つ境界。"""


class SlotLock:
    """OSロックの境界を占有集合で表す。"""

    def __init__(self, path: pathlib.Path, occupied: set[pathlib.Path]) -> None:
        self.path = path
        self.occupied = occupied

    def try_acquire(self) -> bool:
        """空き枠だけを取得する。"""
        if self.path in self.occupied:
            return False
        self.occupied.add(self.path)
        return True

    def release(self) -> None:
        """保持中の枠を解放する。"""
        self.occupied.remove(self.path)


# 非公開の調停処理を呼び、候補の計算だけでなくロック操作と再試行への到達も照合する。
# pylint: disable=protected-access
def poll(count: int, waiters: list[pathlib.Path], request: pathlib.Path, occupied: list[int]) -> int | None:
    """_acquireを一度の調停区間だけ進め、取得した枠または待機を返す。"""
    directory = pathlib.Path("locks")
    slots = [execution_slot._slot_paths(directory, index) for index in range(count)]
    held = {slots[index][0] for index in occupied}
    before = held.copy()
    with (
        mock.patch.multiple(
            execution_slot,
            _request=lambda _: contextlib.nullcontext(request),
            _queue_guard=lambda _: contextlib.nullcontext(),
            _waiters=lambda _: waiters,
            read_owners=lambda _: [],
            _write_owner=lambda *_: None,
        ),
        mock.patch.object(file_lock, "FileLock", lambda path: SlotLock(path, held)),
        mock.patch.object(execution_slot.time, "sleep", side_effect=Retry),
    ):
        try:
            result = execution_slot._acquire(directory, slots, label=None, should_abort=None, on_wait=None)
        except Retry:
            if held != before:
                raise AssertionError(("poll leaked locks", held, before)) from None
            return None
        if result is None:
            raise AssertionError("独立した主体が枠を継承した")
        lock, owner = result
        index = next(index for index, pair in enumerate(slots) if pair[1] == owner)
        if not isinstance(lock, SlotLock) or lock.path != slots[index][0] or held != before | {slots[index][0]}:
            raise AssertionError(("acquire changed wrong locks", held, before, index))
        return index


def release(count: int, occupied: list[int], slot: int) -> None:
    """_releaseを呼び、保持していた枠だけが解放されたことを確かめる。"""
    paths = [execution_slot._slot_paths(pathlib.Path("locks"), index)[0] for index in range(count)]
    held = {paths[index] for index in occupied}
    expected = held - {paths[slot]}
    owner = mock.Mock(spec=pathlib.Path)
    lock = SlotLock(paths[slot], held)
    execution_slot._release(typing.cast("file_lock.FileLock", lock), owner)
    if held != expected:
        raise AssertionError(("release changed wrong locks", held, expected))


# pylint: enable=protected-access


def check(count: int, legacy: tuple[bool, ...]) -> int:
    """空の状態から登録・調停・解放の全順序を探索する。"""
    initial = ((NEW,) * ACTORS, ())
    pending: list[tuple[State, Trace]] = [(initial, ())]
    seen: set[State] = {initial}
    while pending:
        (states, queue), trace = pending.pop()
        occupied = [value for value in states if value >= 0]
        if len(occupied) != len(set(occupied)) or any(value >= count for value in occupied):
            raise AssertionError((count, legacy, trace, occupied))

        paths = {
            actor: pathlib.Path(f"execution.wait-{index:020d}-{'id' if legacy[actor] else 'm-id'}")
            for index, actor in enumerate(queue)
        }
        waiters = [paths[actor] for actor in queue]
        steps: list[tuple[int, int, tuple[int, ...], str]] = []
        for actor, value in enumerate(states):
            if value == NEW:
                steps.append((actor, WAITING, (*queue, actor), "register"))
            elif value == WAITING:
                ahead = queue[: queue.index(actor)]
                eligible = [
                    index
                    for index in range(1 if legacy[actor] else count)
                    if not ahead or (index > 0 and all(legacy[other] for other in ahead))
                ]
                expected = next((index for index in eligible if index not in occupied), None)
                free = expected if legacy[actor] else poll(count, waiters, paths[actor], occupied)
                if free != expected:
                    raise AssertionError((count, legacy, trace, "poll", actor, free, expected))
                if free is not None:
                    steps.append((actor, free, queue, "acquire"))
            elif actor in queue:
                steps.append((actor, value, tuple(other for other in queue if other != actor), "dequeue"))
            elif value >= 0:
                if not legacy[actor]:
                    release(count, occupied, value)
                steps.append((actor, DONE, queue, "release"))
        if not steps and states != (DONE,) * ACTORS:
            raise AssertionError((count, legacy, trace, "deadlock"))
        for actor, value, next_queue, action in steps:
            next_states = (*states[:actor], value, *states[actor + 1 :])
            state = (next_states, next_queue)
            if state not in seen:
                seen.add(state)
                pending.append((state, (*trace, (action, actor, value))))
    return len(seen)


def main() -> None:
    """設定ごとの探索状態数を表示する。"""
    for flag, limit in [(None, 2), ("1", 1)]:
        with mock.patch.dict(os.environ):
            os.environ.pop("PYFLTR_EXECUTION_SLOTS", None)
            if flag is not None:
                os.environ["PYFLTR_EXECUTION_SLOTS"] = flag
            count = execution_slot.slot_count()
        if count != limit:
            raise AssertionError((flag, count, limit))
        directory = pathlib.Path("locks")
        # 旧版と共有する枠0のロック名は内部関数が決める。
        lock_paths = [execution_slot._slot_paths(directory, index)[0] for index in range(count)]  # pylint: disable=protected-access
        if lock_paths[0] != directory / "execution.lock" or len(set(lock_paths)) != count:
            raise AssertionError(lock_paths)
        sizes = [check(count, legacy) for legacy in itertools.product((False, True), repeat=ACTORS)]
        print(f"slots={count}: {len(sizes)} actor assignments, {sum(sizes)} states")


if __name__ == "__main__":
    main()
