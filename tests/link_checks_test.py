import json

import pytest

from pyfltr.link_checks import classify_failures


@pytest.mark.parametrize(("unknown", "expected"), [(0, (True, True)), (1, None)])
def test_classify_failures_with_unsupported(unknown: int, expected: tuple[bool, bool] | None) -> None:
    output = json.dumps(
        {
            "errors": 1,
            "timeouts": 0,
            "unknown": unknown,
            "unsupported": 1,
            "error_map": {"links.md": [{"url": "https://example.com", "status": {"code": 503}}]},
            "timeout_map": {},
        }
    )
    assert classify_failures(output) == expected
