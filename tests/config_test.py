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

        # 設定をリセット
        pyfltr.config.CONFIG["preset"] = ""
        pyfltr.config.CONFIG["isort"] = True
        pyfltr.config.CONFIG["black"] = True
        pyfltr.config.CONFIG["ruff-format"] = False
        pyfltr.config.CONFIG["ruff-check"] = False

        # 設定を読み込み
        pyfltr.config.load_config()

        # 期待される設定値になっているか確認
        assert pyfltr.config.CONFIG["isort"] == expected_isort
        assert pyfltr.config.CONFIG["black"] == expected_black
        assert pyfltr.config.CONFIG["ruff-format"] == expected_ruff_format
        assert pyfltr.config.CONFIG["ruff-check"] == expected_ruff_check

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

        # 設定をリセット
        pyfltr.config.CONFIG["preset"] = ""

        # 不正なプリセットでValueErrorが発生することを確認
        with pytest.raises(ValueError, match="invalid"):
            pyfltr.config.load_config()
    finally:
        os.chdir(original_cwd)
