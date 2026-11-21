"""実行枠の調停を有界状態空間で検査する。"""

import itertools
import os
import pathlib
from unittest import mock

from pyfltr.command import execution_slot

# 各主体の状態: 未登録、待機中、枠番号、終了。
NEW, WAITING, DONE = -3, -2, -1
ACTORS = 4
State = tuple[tuple[int, ...], tuple[int, ...]]
Trace = tuple[tuple[str, int, int], ...]


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
                if legacy[actor]:
                    eligible = [0] if not ahead else []
                else:
                    # 調停関数そのものを呼び、実装と独立した期待値に照らす。
                    eligible = list(execution_slot._eligible_slots(waiters, paths[actor], count))  # pylint: disable=protected-access
                    expected = [
                        index for index in range(count) if not ahead or (index > 0 and all(legacy[other] for other in ahead))
                    ]
                    if eligible != expected:
                        raise AssertionError((count, legacy, trace, "poll", actor, eligible, expected))
                free = next((index for index in eligible if index not in occupied), None)
                if free is not None:
                    steps.append((actor, free, queue, "acquire"))
            elif actor in queue:
                steps.append((actor, value, tuple(other for other in queue if other != actor), "dequeue"))
            elif value >= 0:
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
