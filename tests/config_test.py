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
                "ty": False,
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
                "ty": False,
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
                "ty": False,
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
                "ty": False,
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


def test_ruff_format_by_check_default() -> None:
    """ruff-format-by-check のデフォルト値テスト。"""
    config = pyfltr.config.create_default_config()
    # デフォルトで有効
    assert config["ruff-format-by-check"] is True
    # デフォルトの check 用引数は ruff check --fix --unsafe-fixes
    assert config["ruff-format-check-args"] == ["check", "--fix", "--unsafe-fixes"]


def test_ruff_format_by_check_overridable(tmp_path: pathlib.Path) -> None:
    """pyproject.toml で ruff-format-by-check を上書きできることのテスト。"""
    pyproject_content = """
[tool.pyfltr]
ruff-format-by-check = false
ruff-format-check-args = ["check", "--fix"]
"""
    (tmp_path / "pyproject.toml").write_text(pyproject_content)
    original_cwd = pathlib.Path.cwd()
    try:
        os.chdir(tmp_path)
        config = pyfltr.config.load_config()
        assert config["ruff-format-by-check"] is False
        assert config["ruff-format-check-args"] == ["check", "--fix"]
    finally:
        os.chdir(original_cwd)


def test_fix_args_defaults() -> None:
    """fix-args の既定値テスト。"""
    config = pyfltr.config.create_default_config()
    # fix 対応ビルトインは fix-args が定義されている
    assert config["textlint-fix-args"] == ["--fix"]
    assert config["markdownlint-fix-args"] == ["--fix"]
    assert config["ruff-check-fix-args"] == ["--fix", "--unsafe-fixes"]
    # fix 非対応ビルトインは fix-args キーが存在しない
    assert "mypy-fix-args" not in config.values
    assert "pytest-fix-args" not in config.values
    assert "black-fix-args" not in config.values


def test_filter_fix_commands_defaults() -> None:
    """filter_fix_commands の基本動作テスト。"""
    config = pyfltr.config.create_default_config()
    # 既定では textlint/markdownlint/ruff-check は disabled、formatter は enabled
    commands = ["pyupgrade", "black", "mypy", "textlint", "markdownlint", "ruff-check"]
    result = pyfltr.config.filter_fix_commands(commands, config)
    # enabled な formatter (pyupgrade, black) だけが残る
    # mypy は fix-args 未定義、textlint/markdownlint/ruff-check は disabled
    assert "pyupgrade" in result
    assert "black" in result
    assert "mypy" not in result
    assert "textlint" not in result
    assert "markdownlint" not in result
    assert "ruff-check" not in result


def test_filter_fix_commands_enabled_linter(tmp_path: pathlib.Path) -> None:
    """enabled にした fix 対応 linter が filter_fix_commands に含まれることのテスト。"""
    pyproject_content = """
[tool.pyfltr]
textlint = true
markdownlint = true
ruff-check = true
"""
    (tmp_path / "pyproject.toml").write_text(pyproject_content)
    original_cwd = pathlib.Path.cwd()
    try:
        os.chdir(tmp_path)
        config = pyfltr.config.load_config()
        commands = ["textlint", "markdownlint", "ruff-check", "mypy"]
        result = pyfltr.config.filter_fix_commands(commands, config)
        assert "textlint" in result
        assert "markdownlint" in result
        assert "ruff-check" in result
        assert "mypy" not in result  # fix-args 未定義
    finally:
        os.chdir(original_cwd)


def test_custom_command_fix_args(tmp_path: pathlib.Path) -> None:
    """カスタムコマンドの fix-args 登録テスト。"""
    pyproject_content = """
[tool.pyfltr.custom-commands.my-linter]
type = "linter"
path = "my-linter"
args = ["--check"]
fix-args = ["--fix", "--verbose"]
"""
    (tmp_path / "pyproject.toml").write_text(pyproject_content)
    original_cwd = pathlib.Path.cwd()
    try:
        os.chdir(tmp_path)
        config = pyfltr.config.load_config()
        assert config["my-linter-fix-args"] == ["--fix", "--verbose"]
        # filter_fix_commands にも含まれる
        result = pyfltr.config.filter_fix_commands(["my-linter"], config)
        assert result == ["my-linter"]
    finally:
        os.chdir(original_cwd)


def test_custom_command_without_fix_args(tmp_path: pathlib.Path) -> None:
    """fix-args を省略したカスタム linter は fix 対象外になる。"""
    pyproject_content = """
[tool.pyfltr.custom-commands.plain-linter]
type = "linter"
path = "plain-linter"
"""
    (tmp_path / "pyproject.toml").write_text(pyproject_content)
    original_cwd = pathlib.Path.cwd()
    try:
        os.chdir(tmp_path)
        config = pyfltr.config.load_config()
        assert "plain-linter-fix-args" not in config.values
        result = pyfltr.config.filter_fix_commands(["plain-linter"], config)
        assert not result
    finally:
        os.chdir(original_cwd)


def test_custom_command_fix_args_invalid(tmp_path: pathlib.Path) -> None:
    """fix-args が文字列などの場合はエラーになる。"""
    pyproject_content = """
[tool.pyfltr.custom-commands.bad-linter]
type = "linter"
fix-args = "--fix"
"""
    (tmp_path / "pyproject.toml").write_text(pyproject_content)
    original_cwd = pathlib.Path.cwd()
    try:
        os.chdir(tmp_path)
        with pytest.raises(ValueError, match="fix-args"):
            pyfltr.config.load_config()
    finally:
        os.chdir(original_cwd)


def test_js_runner_default() -> None:
    """js-runner の既定値は pnpx (従来互換)。"""
    config = pyfltr.config.create_default_config()
    assert config["js-runner"] == "pnpx"


def test_js_runner_override(tmp_path: pathlib.Path) -> None:
    """pyproject.toml で js-runner を上書きできる。"""
    pyproject_content = """
[tool.pyfltr]
js-runner = "pnpm"
"""
    (tmp_path / "pyproject.toml").write_text(pyproject_content)
    original_cwd = pathlib.Path.cwd()
    try:
        os.chdir(tmp_path)
        config = pyfltr.config.load_config()
        assert config["js-runner"] == "pnpm"
    finally:
        os.chdir(original_cwd)


def test_js_runner_invalid_rejected(tmp_path: pathlib.Path) -> None:
    """js-runner に未知の値を指定するとエラーになる。"""
    pyproject_content = """
[tool.pyfltr]
js-runner = "bogus"
"""
    (tmp_path / "pyproject.toml").write_text(pyproject_content)
    original_cwd = pathlib.Path.cwd()
    try:
        os.chdir(tmp_path)
        with pytest.raises(ValueError, match="js-runner"):
            pyfltr.config.load_config()
    finally:
        os.chdir(original_cwd)


def test_textlint_packages_default() -> None:
    """textlint-packages のデフォルトにプリセットが含まれる。"""
    config = pyfltr.config.create_default_config()
    assert config["textlint-packages"] == ["textlint-rule-preset-ja-technical-writing"]


def test_textlint_markdownlint_path_default_empty() -> None:
    """textlint-path / markdownlint-path の既定値は空文字 (runner 自動解決)。"""
    config = pyfltr.config.create_default_config()
    assert config["textlint-path"] == ""
    assert config["markdownlint-path"] == ""
    assert config["markdownlint-args"] == []


def test_command_info_target_globs_str() -> None:
    """CommandInfo.target_globs() は str targets を単一要素リストに正規化する。"""
    info = pyfltr.config.CommandInfo(type="linter", targets="*.py")
    assert info.target_globs() == ["*.py"]


def test_command_info_target_globs_list() -> None:
    """CommandInfo.target_globs() は list targets をそのままコピーして返す。"""
    info = pyfltr.config.CommandInfo(type="linter", targets=["*.ts", "*.tsx"])
    result = info.target_globs()
    assert result == ["*.ts", "*.tsx"]
    # 元リストと切り離されている
    result.append("*.js")
    assert info.targets == ["*.ts", "*.tsx"]


def test_custom_command_targets_list(tmp_path: pathlib.Path) -> None:
    """カスタムコマンドの targets に list を指定できる。"""
    pyproject_content = """
[tool.pyfltr.custom-commands.multi]
type = "linter"
path = "multi"
targets = ["*.ts", "*.tsx"]
"""
    (tmp_path / "pyproject.toml").write_text(pyproject_content)
    original_cwd = pathlib.Path.cwd()
    try:
        os.chdir(tmp_path)
        config = pyfltr.config.load_config()
        assert config.commands["multi"].targets == ["*.ts", "*.tsx"]
        assert config.commands["multi"].target_globs() == ["*.ts", "*.tsx"]
    finally:
        os.chdir(original_cwd)


def test_custom_command_targets_invalid_type(tmp_path: pathlib.Path) -> None:
    """カスタムコマンドの targets に不正な型を指定するとエラーになる。"""
    pyproject_content = """
[tool.pyfltr.custom-commands.bad]
type = "linter"
targets = 42
"""
    (tmp_path / "pyproject.toml").write_text(pyproject_content)
    original_cwd = pathlib.Path.cwd()
    try:
        os.chdir(tmp_path)
        with pytest.raises(ValueError, match="targets"):
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
