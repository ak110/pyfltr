import logging
import typing

import pytest

import pyfltr.warnings_


def test_emit_warning_accumulates() -> None:
    pyfltr.warnings_.emit_warning(source="config", message="foo", hint="対応不要です。")
    pyfltr.warnings_.emit_warning(source="git", message="bar", hint="設定を見直してください。")
    entries = pyfltr.warnings_.collected_warnings()
    assert entries == [
        {"source": "config", "message": "foo", "hint": "対応不要です。"},
        {"source": "git", "message": "bar", "hint": "設定を見直してください。"},
    ]


def test_collected_warnings_returns_copy() -> None:
    pyfltr.warnings_.emit_warning(source="config", message="foo", hint="対応不要です。")
    entries = pyfltr.warnings_.collected_warnings()
    entries.clear()
    assert len(pyfltr.warnings_.collected_warnings()) == 1


def test_clear_resets_state() -> None:
    pyfltr.warnings_.emit_warning(source="config", message="foo", hint="対応不要です。")
    pyfltr.warnings_.clear()
    assert not pyfltr.warnings_.collected_warnings()


def test_emit_warning_logs_via_logger(caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level(logging.WARNING, logger="pyfltr.warnings_"):
        pyfltr.warnings_.emit_warning(source="config", message="please fix", hint="対応不要です。")
    assert any("please fix" in record.message for record in caplog.records)


def test_defer_stderr_suppresses_delivered_warning(caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level(logging.WARNING, logger="pyfltr.warnings_"), pyfltr.warnings_.defer_stderr():
        pyfltr.warnings_.emit_warning(source="config", message="delivered", hint="対応不要です。")
        pyfltr.warnings_.mark_delivered(pyfltr.warnings_.collected_warnings())

    assert not [record for record in caplog.records if "delivered" in record.message]


def test_defer_stderr_flushes_undelivered_warning(caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level(logging.WARNING, logger="pyfltr.warnings_"), pyfltr.warnings_.defer_stderr():
        pyfltr.warnings_.emit_warning(source="config", message="not delivered", hint="対応不要です。")

    records = [record for record in caplog.records if "not delivered" in record.message]
    assert len(records) == 1


def test_defer_stderr_nested_scope_flushes_once(caplog: pytest.LogCaptureFixture) -> None:
    with (
        caplog.at_level(logging.WARNING, logger="pyfltr.warnings_"),
        pyfltr.warnings_.defer_stderr(),
        pyfltr.warnings_.defer_stderr(),
    ):
        pyfltr.warnings_.emit_warning(source="config", message="nested", hint="対応不要です。")

    records = [record for record in caplog.records if "nested" in record.message]
    assert len(records) == 1


def test_defer_stderr_restores_after_exception(caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level(logging.WARNING, logger="pyfltr.warnings_"):
        with pytest.raises(RuntimeError, match="stop"), pyfltr.warnings_.defer_stderr():
            pyfltr.warnings_.emit_warning(source="config", message="before stop", hint="対応不要です。")
            raise RuntimeError("stop")
        pyfltr.warnings_.emit_warning(source="config", message="after stop", hint="対応不要です。")

    assert len([record for record in caplog.records if "before stop" in record.message]) == 1
    assert len([record for record in caplog.records if "after stop" in record.message]) == 1


def test_emit_warning_with_exc_info_captures_traceback() -> None:
    try:
        raise ValueError("boom")
    except ValueError:
        pyfltr.warnings_.emit_warning(source="file-resolver", message="I/O Error", exc_info=True, hint="対応不要です。")
    entries = pyfltr.warnings_.collected_warnings()
    assert len(entries) == 1
    assert entries[0]["source"] == "file-resolver"
    assert "I/O Error" in entries[0]["message"]
    assert "ValueError: boom" in entries[0]["message"]


def test_suppress_duplicates_is_scoped() -> None:
    pyfltr.warnings_.emit_warning(source="config", message="same", hint="対応不要です。")
    pyfltr.warnings_.emit_warning(source="config", message="same", hint="対応不要です。")
    assert len(pyfltr.warnings_.collected_warnings()) == 2

    pyfltr.warnings_.clear()
    with pyfltr.warnings_.suppress_duplicates():
        pyfltr.warnings_.emit_warning(source="config", message="same", hint="対応不要です。")
        pyfltr.warnings_.emit_warning(source="config", message="same", hint="対応不要です。")
        pyfltr.warnings_.emit_warning(source="git", message="same", hint="対応不要です。")
        pyfltr.warnings_.add_filtered_direct_file("outside.py", reason="external")
        pyfltr.warnings_.add_filtered_direct_file("outside.py", reason="external")
        pyfltr.warnings_.add_filtered_direct_file("outside.py", reason="missing")

    assert pyfltr.warnings_.collected_warnings() == [
        {"source": "config", "message": "same", "hint": "対応不要です。"},
        {"source": "git", "message": "same", "hint": "対応不要です。"},
    ]
    assert pyfltr.warnings_.filtered_direct_files() == [
        "outside.py",
        "outside.py",
    ]

    pyfltr.warnings_.emit_warning(source="config", message="same", hint="対応不要です。")
    pyfltr.warnings_.add_filtered_direct_file("outside.py", reason="external")
    assert len(pyfltr.warnings_.collected_warnings()) == 3
    assert len(pyfltr.warnings_.filtered_direct_files()) == 3


def test_suppress_duplicates_nested_scope_shares_outer_state() -> None:
    pyfltr.warnings_.clear()
    with pyfltr.warnings_.suppress_duplicates():
        pyfltr.warnings_.emit_warning(source="config", message="same", hint="対応不要です。")
        with pyfltr.warnings_.suppress_duplicates():
            pyfltr.warnings_.emit_warning(source="config", message="same", hint="対応不要です。")
        pyfltr.warnings_.emit_warning(source="config", message="same", hint="対応不要です。")

    assert len(pyfltr.warnings_.collected_warnings()) == 1


def test_suppress_duplicates_restores_state_after_exception() -> None:
    with pytest.raises(RuntimeError, match="stop"), pyfltr.warnings_.suppress_duplicates():
        pyfltr.warnings_.emit_warning(source="config", message="same", hint="対応不要です。")
        raise RuntimeError("stop")

    pyfltr.warnings_.emit_warning(source="config", message="same", hint="対応不要です。")
    assert len(pyfltr.warnings_.collected_warnings()) == 2


def test_filtered_direct_files_accumulates_with_reason() -> None:
    pyfltr.warnings_.add_filtered_direct_file("docs/a.md", reason="excluded")
    pyfltr.warnings_.add_filtered_direct_file("missing/x.py", reason="missing")
    pyfltr.warnings_.add_filtered_direct_file("src/b.py", reason="excluded")
    assert pyfltr.warnings_.filtered_direct_files(reason="excluded") == ["docs/a.md", "src/b.py"]
    assert pyfltr.warnings_.filtered_direct_files(reason="missing") == ["missing/x.py"]
    assert pyfltr.warnings_.filtered_direct_files() == ["docs/a.md", "missing/x.py", "src/b.py"]


def test_clear_also_resets_filtered_direct_files() -> None:
    pyfltr.warnings_.add_filtered_direct_file("a.md", reason="excluded")
    pyfltr.warnings_.add_filtered_direct_file("b.py", reason="missing")
    pyfltr.warnings_.clear()
    assert not pyfltr.warnings_.filtered_direct_files()
    assert not pyfltr.warnings_.filtered_direct_files(reason="excluded")
    assert not pyfltr.warnings_.filtered_direct_files(reason="missing")


def test_emit_warning_requires_hint() -> None:
    emit = typing.cast(typing.Callable[..., None], pyfltr.warnings_.emit_warning)
    with pytest.raises(TypeError, match="hint"):
        emit(source="config", message="対処が必要")  # pylint: disable=missing-kwoa  # 必須引数省略の実行時拒否を検証する。
    assert not pyfltr.warnings_.collected_warnings()


@pytest.mark.parametrize("hint", ["", " ", "\t\n"])
def test_emit_warning_rejects_blank_hint(hint: str) -> None:
    with pytest.raises(ValueError, match="hint"):
        pyfltr.warnings_.emit_warning(source="config", message="対処が必要", hint=hint)
    assert not pyfltr.warnings_.collected_warnings()
