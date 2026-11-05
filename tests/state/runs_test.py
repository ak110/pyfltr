"""実行アーカイブの読み取り（`pyfltr.state.runs`）の試験。"""

import pytest

import pyfltr.state.runs


def _wait(kind: str, sequence: int, elapsed: float | None) -> dict:
    return {
        "kind": kind,
        "sequence": sequence,
        "started_at": "2026-11-05T00:00:00+00:00",
        "finished_at": None if elapsed is None else "2026-11-05T00:00:01+00:00",
        "elapsed": elapsed,
        "outcome": None if elapsed is None else "acquired",
    }


@pytest.mark.parametrize(
    ("meta", "expected"),
    [
        pytest.param(
            {"started_at": "2026-11-05T00:00:00+00:00", "finished_at": "2026-11-05T00:00:10.5+00:00"},
            {
                "measured": False,
                "run_elapsed": 10.5,
                "slot_waits": None,
                "slot_wait_total": None,
                "unfinished_slot_waits": None,
            },
            id="not-measured",
        ),
        pytest.param(
            {
                "started_at": "2026-11-05T00:00:00+00:00",
                "finished_at": "2026-11-05T00:00:10+00:00",
                "slot_waits": [_wait("initial", 0, 0.0), _wait("reacquire", 1, 3.25)],
            },
            {"measured": True, "run_elapsed": 10.0, "slot_wait_total": 3.25, "unfinished_slot_waits": 0},
            id="finished",
        ),
        pytest.param(
            {"started_at": "2026-11-05T00:00:00+00:00", "slot_waits": [_wait("initial", 0, 1.0), _wait("reacquire", 1, None)]},
            {"measured": True, "run_elapsed": None, "slot_wait_total": None, "unfinished_slot_waits": 1},
            id="unfinished",
        ),
        pytest.param(
            {"started_at": "2026-11-05T00:00:00+00:00", "finished_at": "2026-11-05T00:00:01+00:00", "slot_waits": []},
            {"measured": True, "run_elapsed": 1.0, "slot_waits": [], "slot_wait_total": 0, "unfinished_slot_waits": 0},
            id="measured-without-interval",
        ),
    ],
)
def test_summarize_timing(meta: dict, expected: dict) -> None:
    """未計測・未完了を`None`で表し、完了した区間だけで待機の合計を出す。"""
    timing = pyfltr.state.runs.summarize_timing(meta)
    for key, value in expected.items():
        assert timing[key] == value, key
    assert timing["slot_wait_total"] is None or isinstance(timing["slot_wait_total"], float | int)
