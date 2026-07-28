"""pyfltr.warnings_ のテストコード。"""

import logging

import pytest

import pyfltr.warnings_


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


def test_suppress_duplicates_is_scoped() -> None:
    """重複抑止はスコープ内の同一組だけに適用し、異なる組とスコープ外の重複を保持する。"""
    pyfltr.warnings_.emit_warning(source="config", message="same")
    pyfltr.warnings_.emit_warning(source="config", message="same")
    assert len(pyfltr.warnings_.collected_warnings()) == 2

    pyfltr.warnings_.clear()
    with pyfltr.warnings_.suppress_duplicates():
        pyfltr.warnings_.emit_warning(source="config", message="same")
        pyfltr.warnings_.emit_warning(source="config", message="same")
        pyfltr.warnings_.emit_warning(source="git", message="same")
        pyfltr.warnings_.add_filtered_direct_file("outside.py", reason="external")
        pyfltr.warnings_.add_filtered_direct_file("outside.py", reason="external")
        pyfltr.warnings_.add_filtered_direct_file("outside.py", reason="missing")

    assert pyfltr.warnings_.collected_warnings() == [
        {"source": "config", "message": "same"},
        {"source": "git", "message": "same"},
    ]
    assert pyfltr.warnings_.filtered_direct_files() == [
        "outside.py",
        "outside.py",
    ]

    pyfltr.warnings_.emit_warning(source="config", message="same")
    pyfltr.warnings_.add_filtered_direct_file("outside.py", reason="external")
    assert len(pyfltr.warnings_.collected_warnings()) == 3
    assert len(pyfltr.warnings_.filtered_direct_files()) == 3


def test_suppress_duplicates_nested_scope_shares_outer_state() -> None:
    """内側スコープは外側の既出組を共有し、外側の重複抑止契約を壊さない。"""
    pyfltr.warnings_.clear()
    with pyfltr.warnings_.suppress_duplicates():
        pyfltr.warnings_.emit_warning(source="config", message="same")
        with pyfltr.warnings_.suppress_duplicates():
            pyfltr.warnings_.emit_warning(source="config", message="same")
        pyfltr.warnings_.emit_warning(source="config", message="same")

    assert len(pyfltr.warnings_.collected_warnings()) == 1


def test_suppress_duplicates_restores_state_after_exception() -> None:
    """スコープ内の例外後は抑止状態を復元し、後続の同一警告を保持する。"""
    with pytest.raises(RuntimeError, match="stop"), pyfltr.warnings_.suppress_duplicates():
        pyfltr.warnings_.emit_warning(source="config", message="same")
        raise RuntimeError("stop")

    pyfltr.warnings_.emit_warning(source="config", message="same")
    assert len(pyfltr.warnings_.collected_warnings()) == 2


def test_filtered_direct_files_accumulates_with_reason() -> None:
    """add_filtered_direct_file はreason別に蓄積し、reasonフィルタ・全件取得の双方が正しく返る。"""
    pyfltr.warnings_.add_filtered_direct_file("docs/a.md", reason="excluded")
    pyfltr.warnings_.add_filtered_direct_file("missing/x.py", reason="missing")
    pyfltr.warnings_.add_filtered_direct_file("src/b.py", reason="excluded")
    assert pyfltr.warnings_.filtered_direct_files(reason="excluded") == ["docs/a.md", "src/b.py"]
    assert pyfltr.warnings_.filtered_direct_files(reason="missing") == ["missing/x.py"]
    assert pyfltr.warnings_.filtered_direct_files() == ["docs/a.md", "missing/x.py", "src/b.py"]


def test_clear_also_resets_filtered_direct_files() -> None:
    """clear は直接指定フィルタ対象ファイルの記録もreasonを問わずリセットする。"""
    pyfltr.warnings_.add_filtered_direct_file("a.md", reason="excluded")
    pyfltr.warnings_.add_filtered_direct_file("b.py", reason="missing")
    pyfltr.warnings_.clear()
    assert not pyfltr.warnings_.filtered_direct_files()
    assert not pyfltr.warnings_.filtered_direct_files(reason="excluded")
    assert not pyfltr.warnings_.filtered_direct_files(reason="missing")
