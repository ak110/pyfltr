"""スモークテスト用のpytest最小ターゲット。"""


def test_smoke() -> None:
    """常に成功するダミーテスト。"""
    assert 1 + 1 == 2
