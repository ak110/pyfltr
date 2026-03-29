"""テストコード。"""

import os
import pathlib

import pytest

import pyfltr.config


@pytest.mark.parametrize(
    "preset,expected",
    [
        # presetが空の場合はデフォルト
        (
            "",
            {
                "isort": True,
                "black": True,
                "ruff-format": False,
                "ruff-check": False,
                "pyright": False,
                "textlint": False,
                "markdownlint": False,
            },
        ),
        # 20250710プリセット（互換性維持）
        (
            "20250710",
            {
                "isort": False,
                "black": False,
                "ruff-format": True,
                "ruff-check": True,
                "pyright": False,
                "textlint": False,
                "markdownlint": False,
            },
        ),
        # 20260330プリセット
        (
            "20260330",
            {
                "isort": False,
                "black": False,
                "ruff-format": True,
                "ruff-check": True,
                "pyright": True,
                "textlint": True,
                "markdownlint": True,
            },
        ),
        # latestプリセット（= 20260330）
        (
            "latest",
            {
                "isort": False,
                "black": False,
                "ruff-format": True,
                "ruff-check": True,
                "pyright": True,
                "textlint": True,
                "markdownlint": True,
            },
        ),
    ],
)
def test_apply_preset(
    tmp_path: pathlib.Path,
    preset: str,
    expected: dict[str, bool],
) -> None:
    """presetのテスト。"""
    pyproject_path = tmp_path / "pyproject.toml"
    pyproject_path.write_text(f'[tool.pyfltr]\npreset = "{preset}"\n')

    original_cwd = pathlib.Path.cwd()
    try:
        os.chdir(tmp_path)
        config = pyfltr.config.load_config()
        for key, value in expected.items():
            assert config[key] == value, f"{key}: expected {value}, got {config[key]}"
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
fast = true
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
        assert config["bandit-fast"] is True

        # command_namesの末尾に追加されている
        assert config.command_names[-1] == "bandit"

        # ビルトインコマンドも正常
        assert config["ruff-format"] is True  # presetによる設定

        # fastエイリアスにカスタムコマンドが含まれている
        assert "bandit" in config["aliases"]["fast"]
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


def test_fast_alias_dynamic(tmp_path: pathlib.Path) -> None:
    """fastエイリアスがper-command fastフラグから動的計算されることのテスト。"""
    # デフォルト設定でfastエイリアスが正しく構築される
    config = pyfltr.config.create_default_config()
    fast = config["aliases"]["fast"]
    # デフォルトでfastに含まれるコマンド
    assert "pyupgrade" in fast
    assert "ruff-format" in fast
    assert "markdownlint" in fast
    assert "textlint" in fast
    # デフォルトでfastに含まれないコマンド
    assert "mypy" not in fast
    assert "pylint" not in fast
    assert "pytest" not in fast

    # pyproject.tomlでfastフラグを変更
    pyproject_content = """
[tool.pyfltr]
mypy-fast = true
pyupgrade-fast = false
"""
    (tmp_path / "pyproject.toml").write_text(pyproject_content)
    original_cwd = pathlib.Path.cwd()
    try:
        os.chdir(tmp_path)
        config = pyfltr.config.load_config()
        fast = config["aliases"]["fast"]
        assert "mypy" in fast
        assert "pyupgrade" not in fast
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
