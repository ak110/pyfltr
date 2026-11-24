import pathlib

import pytest

import pyfltr.grep_.transaction

_original_write_bytes = pathlib.Path.write_bytes


def test_rollback_keeps_edit_made_after_write(tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch) -> None:
    first = tmp_path / "a.txt"
    second = tmp_path / "b.txt"
    first.write_bytes(b"a0")
    second.write_bytes(b"b0")

    def _write(path: pathlib.Path, data: bytes) -> int:
        if path == second and data == b"b1":
            _original_write_bytes(first, b"edited")
            raise OSError("書き込み失敗を注入")
        return _original_write_bytes(path, data)

    monkeypatch.setattr(pathlib.Path, "write_bytes", _write)
    updates = [
        pyfltr.grep_.transaction.FileUpdate(first, b"a0", b"a1"),
        pyfltr.grep_.transaction.FileUpdate(second, b"b0", b"b1"),
    ]
    with pytest.raises(pyfltr.grep_.transaction.WriteFailure) as exc_info:
        pyfltr.grep_.transaction.write_updates(updates)

    assert first.read_bytes() == b"edited"
    assert second.read_bytes() == b"b0"
    assert exc_info.value.conflicted == [first]
    assert not exc_info.value.unrestored
    assert first.as_posix() in exc_info.value.describe()


def test_rollback_reports_partial_write_of_failed_file(tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch) -> None:
    first = tmp_path / "a.txt"
    second = tmp_path / "b.txt"
    first.write_bytes(b"a0")
    second.write_bytes(b"b0")

    def _write(path: pathlib.Path, data: bytes) -> int:
        if path == second and data == b"b1-long":
            _original_write_bytes(path, b"b1-")
            raise OSError("途中までの書き込み失敗を注入")
        return _original_write_bytes(path, data)

    monkeypatch.setattr(pathlib.Path, "write_bytes", _write)
    updates = [
        pyfltr.grep_.transaction.FileUpdate(first, b"a0", b"a1"),
        pyfltr.grep_.transaction.FileUpdate(second, b"b0", b"b1-long"),
    ]
    with pytest.raises(pyfltr.grep_.transaction.WriteFailure) as exc_info:
        pyfltr.grep_.transaction.write_updates(updates)

    assert [first.read_bytes(), second.read_bytes()] == [b"a0", b"b1-"]
    assert not exc_info.value.conflicted
    assert exc_info.value.unrestored == [second]


def test_rollback_keeps_prefix_edit_after_failed_write(tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch) -> None:
    target = tmp_path / "a.txt"
    target.write_bytes(b"before")

    def _write(path: pathlib.Path, data: bytes) -> int:
        if path == target and data == b"replacement":
            _original_write_bytes(path, b"re")
            raise OSError("書き込み失敗を注入")
        return _original_write_bytes(path, data)

    monkeypatch.setattr(pathlib.Path, "write_bytes", _write)
    with pytest.raises(pyfltr.grep_.transaction.WriteFailure) as exc_info:
        pyfltr.grep_.transaction.write_updates([pyfltr.grep_.transaction.FileUpdate(target, b"before", b"replacement")])

    assert target.read_bytes() == b"re"
    assert target.as_posix() in exc_info.value.describe()


def test_rollback_reports_restore_failure(tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch) -> None:
    first = tmp_path / "a.txt"
    second = tmp_path / "b.txt"
    first.write_bytes(b"a0")
    second.write_bytes(b"b0")

    def _write(path: pathlib.Path, data: bytes) -> int:
        if (path, data) in {(second, b"b1"), (first, b"a0")}:
            raise OSError("書き込み失敗を注入")
        return _original_write_bytes(path, data)

    monkeypatch.setattr(pathlib.Path, "write_bytes", _write)
    updates = [
        pyfltr.grep_.transaction.FileUpdate(first, b"a0", b"a1"),
        pyfltr.grep_.transaction.FileUpdate(second, b"b0", b"b1"),
    ]
    with pytest.raises(pyfltr.grep_.transaction.WriteFailure) as exc_info:
        pyfltr.grep_.transaction.write_updates(updates)

    assert [first.read_bytes(), second.read_bytes()] == [b"a1", b"b0"]
    assert exc_info.value.unrestored == [first]
    assert first.as_posix() in exc_info.value.describe()
