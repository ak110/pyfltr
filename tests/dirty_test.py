# pylint: disable=missing-module-docstring
# pylint: disable=missing-function-docstring

import json

import pyfltr.dirty


def test_dirty_init(tmp_path):
    """initで.format-dirtyが削除されることを確認。"""
    dirty_dir = tmp_path / ".claude"
    dirty_dir.mkdir()
    dirty_file = dirty_dir / ".format-dirty"
    dirty_file.write_text("src/foo.py\n")

    result = pyfltr.dirty.run_dirty(["init"], base_dir=tmp_path)
    assert result == 0
    assert not dirty_file.exists()


def test_dirty_init_missing_file(tmp_path):
    """initで.format-dirtyが存在しなくてもエラーにならない。"""
    result = pyfltr.dirty.run_dirty(["init"], base_dir=tmp_path)
    assert result == 0


def test_dirty_add(tmp_path, monkeypatch):
    """stdinからのJSON読み取りとファイル追記を確認。"""
    (tmp_path / ".claude").mkdir()

    hook_json = json.dumps({"tool_input": {"file_path": "src/foo.py"}})
    monkeypatch.setattr("sys.stdin", __import__("io").StringIO(hook_json))

    result = pyfltr.dirty.run_dirty(["add"], base_dir=tmp_path)
    assert result == 0

    dirty_file = tmp_path / ".claude" / ".format-dirty"
    assert dirty_file.exists()
    assert "src/foo.py" in dirty_file.read_text().splitlines()


def test_dirty_add_non_py(tmp_path, monkeypatch):
    """.py以外のファイルも追記されることを確認。"""
    (tmp_path / ".claude").mkdir()

    hook_json = json.dumps({"tool_input": {"file_path": "src/foo.ts"}})
    monkeypatch.setattr("sys.stdin", __import__("io").StringIO(hook_json))

    result = pyfltr.dirty.run_dirty(["add"], base_dir=tmp_path)
    assert result == 0

    dirty_file = tmp_path / ".claude" / ".format-dirty"
    assert dirty_file.exists()
    assert "src/foo.ts" in dirty_file.read_text().splitlines()


def test_dirty_add_duplicate(tmp_path, monkeypatch):
    """重複パスが追記されないことを確認。"""
    dirty_dir = tmp_path / ".claude"
    dirty_dir.mkdir()
    dirty_file = dirty_dir / ".format-dirty"
    dirty_file.write_text("src/foo.py\n")

    hook_json = json.dumps({"tool_input": {"file_path": "src/foo.py"}})
    monkeypatch.setattr("sys.stdin", __import__("io").StringIO(hook_json))

    result = pyfltr.dirty.run_dirty(["add"], base_dir=tmp_path)
    assert result == 0

    lines = [line for line in dirty_file.read_text().splitlines() if line]
    assert lines.count("src/foo.py") == 1


def test_dirty_run(tmp_path, mocker):
    """蓄積ファイルの整形と.format-dirty削除を確認。"""
    dirty_dir = tmp_path / ".claude"
    dirty_dir.mkdir()
    dirty_file = dirty_dir / ".format-dirty"

    # 存在するファイルを作成
    target = tmp_path / "src" / "foo.py"
    target.parent.mkdir(parents=True)
    target.write_text("x = 1\n")
    dirty_file.write_text(f"{target}\n")

    mock_run = mocker.patch("pyfltr.main.run", return_value=0)

    result = pyfltr.dirty.run_dirty(["run"], base_dir=tmp_path)
    assert result == 0
    assert not dirty_file.exists()

    # pyfltr.main.runがfastサブコマンドで呼ばれたことを確認
    mock_run.assert_called_once()
    call_args = mock_run.call_args[0][0]
    assert call_args[0] == "fast"
    assert "--no-clear" in call_args
    assert "--no-ui" in call_args


def test_dirty_run_nonexistent_files(tmp_path, mocker):
    """存在しないファイルはフィルタされることを確認。"""
    dirty_dir = tmp_path / ".claude"
    dirty_dir.mkdir()
    dirty_file = dirty_dir / ".format-dirty"
    dirty_file.write_text("/nonexistent/file.py\n")

    mock_run = mocker.patch("pyfltr.main.run", return_value=0)

    result = pyfltr.dirty.run_dirty(["run"], base_dir=tmp_path)
    assert result == 0
    assert not dirty_file.exists()
    # 存在するファイルがないため、pyfltr.main.runは呼ばれない
    mock_run.assert_not_called()


def test_dirty_run_no_dirty_file(tmp_path):
    """.format-dirtyが存在しない場合はスキップ。"""
    result = pyfltr.dirty.run_dirty(["run"], base_dir=tmp_path)
    assert result == 0


def test_dirty_empty_args():
    """引数なしでもexit 0を返す。"""
    result = pyfltr.dirty.run_dirty([])
    assert result == 0


def test_dirty_unknown_sub():
    """不明なサブコマンドでもexit 0を返す。"""
    result = pyfltr.dirty.run_dirty(["unknown"])
    assert result == 0
