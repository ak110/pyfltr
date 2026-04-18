"""pyfltr.warnings_ のテストコード。"""

import logging

import pytest

import pyfltr.warnings_


@pytest.fixture(autouse=True)
def _clear_warnings() -> None:
    """各テストの前後で警告リストをクリアする。"""
    pyfltr.warnings_.clear()


def test_emit_warning_accumulates() -> None:
    """emit_warning の呼び出し順で内部リストに蓄積される。"""
    pyfltr.warnings_.emit_warning(source="config", message="foo")
    pyfltr.warnings_.emit_warning(source="git", message="bar")
    entries = pyfltr.warnings_.collected_warnings()
    assert entries == [
        {"source": "config", "message": "foo"},
        {"source": "git", "message": "bar"},
    ]


def test_collected_warnings_returns_copy() -> None:
    """collected_warnings は呼び出し側の操作で内部状態が汚れないコピーを返す。"""
    pyfltr.warnings_.emit_warning(source="config", message="foo")
    entries = pyfltr.warnings_.collected_warnings()
    entries.clear()
    assert len(pyfltr.warnings_.collected_warnings()) == 1


def test_clear_resets_state() -> None:
    """clear を呼ぶと蓄積が空になる。"""
    pyfltr.warnings_.emit_warning(source="config", message="foo")
    pyfltr.warnings_.clear()
    assert not pyfltr.warnings_.collected_warnings()


def test_emit_warning_logs_via_logger(caplog: pytest.LogCaptureFixture) -> None:
    """emit_warning は logger.warning 経由で stderr にも出力する。"""
    with caplog.at_level(logging.WARNING, logger="pyfltr.warnings_"):
        pyfltr.warnings_.emit_warning(source="config", message="please fix")
    assert any("please fix" in record.message for record in caplog.records)


def test_emit_warning_with_exc_info_captures_traceback() -> None:
    """exc_info=True でスタックトレースが message 末尾に連結される。"""
    try:
        raise ValueError("boom")
    except ValueError:
        pyfltr.warnings_.emit_warning(source="file-resolver", message="I/O Error", exc_info=True)
    entries = pyfltr.warnings_.collected_warnings()
    assert len(entries) == 1
    assert entries[0]["source"] == "file-resolver"
    assert "I/O Error" in entries[0]["message"]
    assert "ValueError: boom" in entries[0]["message"]


def test_emit_warning_with_hint_included() -> None:
    """hint指定時は蓄積 dict に hint キーが含まれる。"""
    pyfltr.warnings_.emit_warning(source="config", message="foo", hint="fooを bar に直す")
    entries = pyfltr.warnings_.collected_warnings()
    assert entries == [{"source": "config", "message": "foo", "hint": "fooを bar に直す"}]


def test_emit_warning_without_hint_omitted() -> None:
    """hint未指定時は蓄積 dict に hint キーが含まれない（下位互換）。"""
    pyfltr.warnings_.emit_warning(source="config", message="foo")
    entries = pyfltr.warnings_.collected_warnings()
    assert entries == [{"source": "config", "message": "foo"}]
    assert "hint" not in entries[0]
