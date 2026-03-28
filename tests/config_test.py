"""テストコード。"""

import os
import pathlib

import pytest

import pyfltr.config


@pytest.mark.parametrize(
    "preset,expected_isort,expected_black,expected_ruff_format,expected_ruff_check",
    [
        ("", True, True, False, False),  # presetが空の場合はデフォルト
        ("20250710", False, False, True, True),  # 20250710プリセット
        ("latest", False, False, True, True),  # latestプリセット
    ],
)
def test_apply_preset(
    tmp_path: pathlib.Path,
    preset: str,
    expected_isort: bool,
    expected_black: bool,
    expected_ruff_format: bool,
    expected_ruff_check: bool,
) -> None:
    """presetのテスト。"""
    # pyproject.tomlを作成
    pyproject_content = f"""
[tool.pyfltr]
preset = "{preset}"
"""
    pyproject_path = tmp_path / "pyproject.toml"
    pyproject_path.write_text(pyproject_content)

    # カレントディレクトリを一時的に変更
    original_cwd = pathlib.Path.cwd()
    try:
        os.chdir(tmp_path)

        # 設定を読み込み
        config = pyfltr.config.load_config()

        # 期待される設定値になっているか確認
        assert config["isort"] == expected_isort
        assert config["black"] == expected_black
        assert config["ruff-format"] == expected_ruff_format
        assert config["ruff-check"] == expected_ruff_check

    finally:
        os.chdir(original_cwd)


def test_custom_command(tmp_path: pathlib.Path) -> None:
    """カスタムコマンド定義のテスト。"""
    pyproject_content = """
[tool.pyfltr]
preset = "latest"

[tool.pyfltr.custom-commands.bandit]
type = "linter"
path = "bandit"
args = ["-r"]
targets = "*.py"
error-pattern = '(?P<file>[^:]+):(?P<line>\\d+):(?P<col>\\d+):\\s*(?P<message>.+)'
"""
    (tmp_path / "pyproject.toml").write_text(pyproject_content)
    original_cwd = pathlib.Path.cwd()
    try:
        os.chdir(tmp_path)
        config = pyfltr.config.load_config()

        # カスタムコマンドがレジストリに登録されている
        assert "bandit" in config.commands
        assert config.commands["bandit"].type == "linter"
        assert config.commands["bandit"].builtin is False
        assert config.commands["bandit"].targets == "*.py"
        assert config.commands["bandit"].error_pattern is not None

        # values辞書にも登録されている
        assert config["bandit"] is True
        assert config["bandit-path"] == "bandit"
        assert config["bandit-args"] == ["-r"]

        # command_namesの末尾に追加されている
        assert config.command_names[-1] == "bandit"

        # ビルトインコマンドも正常
        assert config["ruff-format"] is True  # presetによる設定
    finally:
        os.chdir(original_cwd)


def test_custom_command_builtin_name_conflict(tmp_path: pathlib.Path) -> None:
    """ビルトインコマンドとの名前衝突テスト。"""
    pyproject_content = """
[tool.pyfltr.custom-commands.mypy]
type = "linter"
"""
    (tmp_path / "pyproject.toml").write_text(pyproject_content)
    original_cwd = pathlib.Path.cwd()
    try:
        os.chdir(tmp_path)
        with pytest.raises(ValueError, match="衝突"):
            pyfltr.config.load_config()
    finally:
        os.chdir(original_cwd)


def test_custom_command_invalid_type(tmp_path: pathlib.Path) -> None:
    """カスタムコマンドの不正なtypeテスト。"""
    pyproject_content = """
[tool.pyfltr.custom-commands.foo]
type = "invalid"
"""
    (tmp_path / "pyproject.toml").write_text(pyproject_content)
    original_cwd = pathlib.Path.cwd()
    try:
        os.chdir(tmp_path)
        with pytest.raises(ValueError, match="type"):
            pyfltr.config.load_config()
    finally:
        os.chdir(original_cwd)


def test_custom_command_invalid_error_pattern(tmp_path: pathlib.Path) -> None:
    """カスタムコマンドの不正なerror-patternテスト。"""
    # 必須グループが欠けている
    pyproject_content = """
[tool.pyfltr.custom-commands.foo]
type = "linter"
error-pattern = '(?P<file>[^:]+):(?P<line>\\d+)'
"""
    (tmp_path / "pyproject.toml").write_text(pyproject_content)
    original_cwd = pathlib.Path.cwd()
    try:
        os.chdir(tmp_path)
        with pytest.raises(ValueError, match="message"):
            pyfltr.config.load_config()
    finally:
        os.chdir(original_cwd)


def test_invalid_preset(tmp_path: pathlib.Path) -> None:
    """不正なpresetのテスト。"""
    # pyproject.tomlを作成
    pyproject_content = """
[tool.pyfltr]
preset = "invalid"
"""
    pyproject_path = tmp_path / "pyproject.toml"
    pyproject_path.write_text(pyproject_content)

    # カレントディレクトリを一時的に変更
    original_cwd = pathlib.Path.cwd()
    try:
        os.chdir(tmp_path)

        # 不正なプリセットでValueErrorが発生することを確認
        with pytest.raises(ValueError, match="invalid"):
            pyfltr.config.load_config()
    finally:
        os.chdir(original_cwd)
