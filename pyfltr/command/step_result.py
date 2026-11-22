import collections.abc
import dataclasses
import enum


class StepState(enum.Enum):
    SUCCEEDED = enum.auto()
    CHANGED = enum.auto()
    FAILED = enum.auto()
    TIMEOUT = enum.auto()


@dataclasses.dataclass(frozen=True)
class StepResult:
    state: StepState
    returncode: int

    @property
    def failed(self) -> bool:
        return self.state in (StepState.FAILED, StepState.TIMEOUT)


def classify(
    returncode: int,
    *,
    timeout: bool = False,
    success_codes: collections.abc.Collection[int] = (0,),
    change_codes: collections.abc.Collection[int] = (),
    changed: bool = False,
) -> StepResult:
    if timeout:
        return StepResult(StepState.TIMEOUT, returncode or 1)
    if returncode < 0 or (returncode not in success_codes and returncode not in change_codes):
        return StepResult(StepState.FAILED, returncode)
    if changed or returncode in change_codes:
        return StepResult(StepState.CHANGED, 1)
    return StepResult(StepState.SUCCEEDED, 0)


def combine(*steps: StepResult) -> StepResult:
    for state in (StepState.TIMEOUT, StepState.FAILED):
        failures = [step for step in steps if step.state is state]
        if failures:
            return next((step for step in failures if step.returncode != 1), failures[0])
    if any(step.state is StepState.CHANGED for step in steps):
        return StepResult(StepState.CHANGED, 1)
    return StepResult(StepState.SUCCEEDED, 0)
