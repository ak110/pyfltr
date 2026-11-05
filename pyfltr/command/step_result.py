"""ツール固有の終了コードを段の意味へ変換し、多段実行の結果を合成する。"""

import collections.abc
import dataclasses
import enum


class StepState(enum.Enum):
    """段の結果の意味。公開statusへの変換はCommandResultが担う。"""

    SUCCEEDED = enum.auto()
    CHANGED = enum.auto()
    FAILED = enum.auto()
    TIMEOUT = enum.auto()


@dataclasses.dataclass(frozen=True)
class StepResult:
    """終了コードの意味を確定した不変の段結果。"""

    state: StepState
    returncode: int

    @property
    def failed(self) -> bool:
        """実行失敗かtimeoutかを返す。"""
        return self.state in (StepState.FAILED, StepState.TIMEOUT)


def classify(
    returncode: int,
    *,
    timeout: bool = False,
    success_codes: collections.abc.Collection[int] = (0,),
    change_codes: collections.abc.Collection[int] = (),
    changed: bool = False,
) -> StepResult:
    """許容コードと実変更を入力として段を分類する。

    timeoutとシグナル終了は許容コードより優先する。timeoutで終了コードが0の場合も、
    CommandResultの正常終了分岐へ入らないよう非0にする。
    """
    if timeout:
        return StepResult(StepState.TIMEOUT, returncode or 1)
    if returncode < 0 or (returncode not in success_codes and returncode not in change_codes):
        return StepResult(StepState.FAILED, returncode)
    if changed or returncode in change_codes:
        return StepResult(StepState.CHANGED, 1)
    return StepResult(StepState.SUCCEEDED, 0)


def combine(*steps: StepResult) -> StepResult:
    """timeout、致命的失敗、通常違反、変更、成功の優先順で合成する。

    通常違反の終了1より、シグナル終了と終了2以上を優先する。
    同じ優先度では実行順を保つ。
    """
    for state in (StepState.TIMEOUT, StepState.FAILED):
        failures = [step for step in steps if step.state is state]
        if failures:
            return next((step for step in failures if step.returncode != 1), failures[0])
    if any(step.state is StepState.CHANGED for step in steps):
        return StepResult(StepState.CHANGED, 1)
    return StepResult(StepState.SUCCEEDED, 0)
