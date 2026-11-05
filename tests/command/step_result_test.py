"""段結果の値域と合成優先度。"""

import pytest

from pyfltr.command import step_result


@pytest.mark.parametrize("code", [0, 1, 2, -9])
@pytest.mark.parametrize("timeout", [False, True])
@pytest.mark.parametrize("changed", [False, True])
def test_classify(code: int, timeout: bool, changed: bool) -> None:
    """許容コードと変更の有無にかかわらずシグナルとtimeoutを失敗にする。"""
    result = step_result.classify(code, timeout=timeout, success_codes=(0, 1, -9), changed=changed)
    if timeout:
        assert result.state is step_result.StepState.TIMEOUT
        assert result.returncode != 0
    elif code in (-9, 2):
        assert result.state is step_result.StepState.FAILED
        assert result.returncode == code
    else:
        assert result.state is (step_result.StepState.CHANGED if changed else step_result.StepState.SUCCEEDED)


@pytest.mark.parametrize("codes", [(0, 1, -9), (1, 2, 0), (2, 0, 1)])
def test_combine_preserves_fatal_failure(codes: tuple[int, ...]) -> None:
    """3段へ増えても通常違反や成功によって致命的失敗が消えない。"""
    combined = step_result.combine(*(step_result.classify(code) for code in codes))
    assert combined.failed
    assert combined.returncode == (-9 if -9 in codes else 2)


def test_combine_timeout_dominates_failure_and_change() -> None:
    """timeoutは変更と他段の失敗に優先する。"""
    combined = step_result.combine(
        step_result.classify(0, changed=True), step_result.classify(-9), step_result.classify(0, timeout=True)
    )
    assert combined.state is step_result.StepState.TIMEOUT
    assert combined.returncode != 0
