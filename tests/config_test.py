"""テストコード。"""
# pylint: disable=too-many-lines

import pathlib

import pytest

import pyfltr.config
import pyfltr.warnings_
from tests import conftest as _testconf


@pytest.mark.parametrize(
    "preset,extra_lines,expected",
    [
        # presetが空の場合はデフォルト（全ツールFalse）
        (
            "",
            "",
            {
                "mypy": False,
                "pylint": False,
                "pytest": False,
                "ruff-format": False,
                "ruff-check": False,
                "pyright": False,
                "ty": False,
                "textlint": False,
                "markdownlint": False,
                "actionlint": False,
                "typos": False,
                "pre-commit": False,
                "eslint": False,
                "cargo-fmt": False,
                "dotnet-format": False,
            },
        ),
        # 20260330 + python = true で preset 内の Python 系とドキュメント系が有効化される
        (
            "20260330",
            "python = true\n",
            {
                "ruff-format": True,
                "ruff-check": True,
                "mypy": True,
                "pylint": True,
                "pytest": True,
                "pyright": True,
                "textlint": True,
                "markdownlint": True,
                # 20260330 には含まれない
                "uv-sort": False,
                "actionlint": False,
                "typos": False,
                "pre-commit": False,
                # 他言語カテゴリは gate されたまま
                "eslint": False,
                "cargo-fmt": False,
                "dotnet-format": False,
                # preset 非収録の ty は gate 通過でも False
                "ty": False,
            },
        ),
        # 20260411 は uv-sort / actionlint / typos が増える
        (
            "20260411",
            "python = true\n",
            {
                "ruff-format": True,
                "ruff-check": True,
                "mypy": True,
                "pylint": True,
                "pytest": True,
                "pyright": True,
                "uv-sort": True,
                "textlint": True,
                "markdownlint": True,
                "actionlint": True,
                "typos": True,
                # pre-commit は 20260413 以降
                "pre-commit": False,
            },
        ),
        # 20260413 は pre-commit が増える
        (
            "20260413",
            "python = true\n",
            {
                "ruff-format": True,
                "ruff-check": True,
                "mypy": True,
                "pylint": True,
                "pytest": True,
                "pyright": True,
                "uv-sort": True,
                "textlint": True,
                "markdownlint": True,
                "actionlint": True,
                "typos": True,
                "pre-commit": True,
            },
        ),
        # latest = 20260413
        (
            "latest",
            "python = true\n",
            {
                "ruff-format": True,
                "ruff-check": True,
                "mypy": True,
                "pylint": True,
                "pytest": True,
                "pyright": True,
                "uv-sort": True,
                "textlint": True,
                "markdownlint": True,
                "actionlint": True,
                "typos": True,
                "pre-commit": True,
            },
        ),
        # latest + javascript = true で JS/TS 系推奨ツール一式が gate 通過する
        (
            "latest",
            "javascript = true\n",
            {
                # ドキュメント系は言語 gate と独立
                "textlint": True,
                "markdownlint": True,
                # JS/TS 系は全量 True
                "eslint": True,
                "biome": True,
                "oxlint": True,
                "prettier": True,
                "tsc": True,
                "vitest": True,
                # 他言語は gate 閉のまま False
                "ruff-format": False,
                "mypy": False,
                "pytest": False,
                "cargo-fmt": False,
                "dotnet-format": False,
            },
        ),
        # latest + rust = true で Rust 系推奨ツール一式が gate 通過する
        (
            "latest",
            "rust = true\n",
            {
                "cargo-fmt": True,
                "cargo-clippy": True,
                "cargo-check": True,
                "cargo-test": True,
                "cargo-deny": True,
                # 他言語は gate 閉のまま False
                "ruff-format": False,
                "eslint": False,
                "dotnet-format": False,
            },
        ),
        # latest + dotnet = true で .NET 系推奨ツール一式が gate 通過する
        (
            "latest",
            "dotnet = true\n",
            {
                "dotnet-format": True,
                "dotnet-build": True,
                "dotnet-test": True,
                # 他言語は gate 閉のまま False
                "ruff-format": False,
                "eslint": False,
                "cargo-fmt": False,
            },
        ),
        # 20260330 + rust = true でも _PRESET_BASE 経由で Rust 系が有効化される
        (
            "20260330",
            "rust = true\n",
            {
                "cargo-fmt": True,
                "cargo-clippy": True,
                "cargo-check": True,
                "cargo-test": True,
                "cargo-deny": True,
            },
        ),
    ],
)
def test_apply_preset(
    tmp_path: pathlib.Path,
    preset: str,
    extra_lines: str,
    expected: dict[str, bool],
) -> None:
    """presetのテスト。preset で推奨構成が True になり、カテゴリキー gate を通して有効化する。"""
    pyproject_path = tmp_path / "pyproject.toml"
    pyproject_path.write_text(f'[tool.pyfltr]\npreset = "{preset}"\n{extra_lines}')

    config = pyfltr.config.load_config(config_dir=tmp_path)
    for key, value in expected.items():
        assert config[key] == value, f"{key}: expected {value}, got {config[key]}"


def test_custom_command(tmp_path: pathlib.Path) -> None:
    """カスタムコマンド定義のテスト。"""
    # preset と python opt-in を併用することで Python 系ツールの有効化も検証する。
    pyproject_content = """
[tool.pyfltr]
preset = "latest"
python = true

[tool.pyfltr.custom-commands.bandit]
type = "linter"
path = "bandit"
args = ["-r"]
targets = "*.py"
error-pattern = '(?P<file>[^:]+):(?P<line>\\d+):(?P<col>\\d+):\\s*(?P<message>.+)'
fast = true
"""
    (tmp_path / "pyproject.toml").write_text(pyproject_content)
    config = pyfltr.config.load_config(config_dir=tmp_path)

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

    # preset + python = true の gate 通過により preset 内の Python 系ツールが有効化されている
    assert config["ruff-format"] is True

    # fastエイリアスにカスタムコマンドが含まれている
    assert "bandit" in config["aliases"]["fast"]


def test_custom_command_builtin_name_conflict(tmp_path: pathlib.Path) -> None:
    """ビルトインコマンドとの名前衝突テスト。"""
    pyproject_content = """
[tool.pyfltr.custom-commands.mypy]
type = "linter"
"""
    (tmp_path / "pyproject.toml").write_text(pyproject_content)
    with pytest.raises(ValueError, match="衝突"):
        pyfltr.config.load_config(config_dir=tmp_path)


def test_custom_command_invalid_type(tmp_path: pathlib.Path) -> None:
    """カスタムコマンドの不正なtypeテスト。"""
    pyproject_content = """
[tool.pyfltr.custom-commands.foo]
type = "invalid"
"""
    (tmp_path / "pyproject.toml").write_text(pyproject_content)
    with pytest.raises(ValueError, match="type"):
        pyfltr.config.load_config(config_dir=tmp_path)


def test_custom_command_invalid_error_pattern(tmp_path: pathlib.Path) -> None:
    """カスタムコマンドの不正なerror-patternテスト。"""
    # 必須グループが欠けている
    pyproject_content = """
[tool.pyfltr.custom-commands.foo]
type = "linter"
error-pattern = '(?P<file>[^:]+):(?P<line>\\d+)'
"""
    (tmp_path / "pyproject.toml").write_text(pyproject_content)
    with pytest.raises(ValueError, match="message"):
        pyfltr.config.load_config(config_dir=tmp_path)


def test_fast_alias_dynamic(tmp_path: pathlib.Path) -> None:
    """fastエイリアスがper-command fastフラグから動的計算されることのテスト。"""
    # デフォルト設定でfastエイリアスが正しく構築される
    # fast エイリアスはツールの有効/無効に関わらず {tool}-fast フラグが True のものを列挙する
    config = pyfltr.config.create_default_config()
    fast = config["aliases"]["fast"]
    # ruff-format-fast=True なので fast に含まれる
    assert "ruff-format" in fast
    # mypy-fast=False なので fast に含まれない
    assert "mypy" not in fast
    assert "pylint" not in fast
    assert "pytest" not in fast

    # pyproject.tomlでfastフラグを変更
    pyproject_content = """
[tool.pyfltr]
python = true
mypy-fast = true
ruff-format-fast = false
"""
    (tmp_path / "pyproject.toml").write_text(pyproject_content)
    config = pyfltr.config.load_config(config_dir=tmp_path)
    fast = config["aliases"]["fast"]
    assert "mypy" in fast
    assert "ruff-format" not in fast


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
    config = pyfltr.config.load_config(config_dir=tmp_path)
    assert config["ruff-format-by-check"] is False
    assert config["ruff-format-check-args"] == ["check", "--fix"]


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


def test_filter_fix_commands_defaults() -> None:
    """filter_fix_commands の基本動作テスト。"""
    config = pyfltr.config.create_default_config()
    # 既定では全ツール無効または fix-args 未定義のため全て除外
    commands = ["mypy", "textlint", "markdownlint", "ruff-check"]
    result = pyfltr.config.filter_fix_commands(commands, config)
    # mypy は fix-args 未定義、textlint/markdownlint/ruff-check は disabled のため全て除外
    assert not result


def test_filter_fix_commands_enabled_linter(tmp_path: pathlib.Path) -> None:
    """enabled にした fix 対応 linter が filter_fix_commands に含まれることのテスト。"""
    pyproject_content = """
[tool.pyfltr]
textlint = true
markdownlint = true
ruff-check = true
"""
    (tmp_path / "pyproject.toml").write_text(pyproject_content)
    config = pyfltr.config.load_config(config_dir=tmp_path)
    commands = ["textlint", "markdownlint", "ruff-check", "mypy"]
    result = pyfltr.config.filter_fix_commands(commands, config)
    assert "textlint" in result
    assert "markdownlint" in result
    assert "ruff-check" in result
    assert "mypy" not in result  # fix-args 未定義


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
    config = pyfltr.config.load_config(config_dir=tmp_path)
    assert config["my-linter-fix-args"] == ["--fix", "--verbose"]
    # filter_fix_commands にも含まれる
    result = pyfltr.config.filter_fix_commands(["my-linter"], config)
    assert result == ["my-linter"]


def test_custom_command_without_fix_args(tmp_path: pathlib.Path) -> None:
    """fix-args を省略したカスタム linter は fix 対象外になる。"""
    pyproject_content = """
[tool.pyfltr.custom-commands.plain-linter]
type = "linter"
path = "plain-linter"
"""
    (tmp_path / "pyproject.toml").write_text(pyproject_content)
    config = pyfltr.config.load_config(config_dir=tmp_path)
    assert "plain-linter-fix-args" not in config.values
    result = pyfltr.config.filter_fix_commands(["plain-linter"], config)
    assert not result


def test_custom_command_fix_args_invalid(tmp_path: pathlib.Path) -> None:
    """fix-args が文字列などの場合はエラーになる。"""
    pyproject_content = """
[tool.pyfltr.custom-commands.bad-linter]
type = "linter"
fix-args = "--fix"
"""
    (tmp_path / "pyproject.toml").write_text(pyproject_content)
    with pytest.raises(ValueError, match="fix-args"):
        pyfltr.config.load_config(config_dir=tmp_path)


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
    config = pyfltr.config.load_config(config_dir=tmp_path)
    assert config["js-runner"] == "pnpm"


def test_js_runner_invalid_rejected(tmp_path: pathlib.Path) -> None:
    """js-runner に未知の値を指定するとエラーになる。"""
    pyproject_content = """
[tool.pyfltr]
js-runner = "bogus"
"""
    (tmp_path / "pyproject.toml").write_text(pyproject_content)
    with pytest.raises(ValueError, match="js-runner"):
        pyfltr.config.load_config(config_dir=tmp_path)


def test_textlint_packages_default() -> None:
    """textlint-packages のデフォルトに3パッケージが含まれる。"""
    config = pyfltr.config.create_default_config()
    assert config["textlint-packages"] == [
        "textlint-rule-preset-ja-technical-writing",
        "textlint-rule-preset-jtf-style",
        "textlint-rule-ja-no-abusage",
    ]


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
    config = pyfltr.config.load_config(config_dir=tmp_path)
    assert config.commands["multi"].targets == ["*.ts", "*.tsx"]
    assert config.commands["multi"].target_globs() == ["*.ts", "*.tsx"]


def test_custom_command_targets_invalid_type(tmp_path: pathlib.Path) -> None:
    """カスタムコマンドの targets に不正な型を指定するとエラーになる。"""
    pyproject_content = """
[tool.pyfltr.custom-commands.bad]
type = "linter"
targets = 42
"""
    (tmp_path / "pyproject.toml").write_text(pyproject_content)
    with pytest.raises(ValueError, match="targets"):
        pyfltr.config.load_config(config_dir=tmp_path)


class TestConfigFilesWarning:
    """config_files 未配置時の警告機構のテスト。"""

    @pytest.fixture(autouse=True)
    def _reset_warnings(self) -> None:
        pyfltr.warnings_.clear()

    def test_pre_commit_enabled_without_config_emits_warning(self, tmp_path: pathlib.Path) -> None:
        """pre-commit 有効かつ .pre-commit-config.yaml 不在で警告が出る。"""
        (tmp_path / "pyproject.toml").write_text('[tool.pyfltr]\npreset = "latest"\n')
        pyfltr.config.load_config(config_dir=tmp_path)
        # preset=latest では textlint などにも config_files が定義されるため複数の warning が出る。
        # 本テストは pre-commit 分が出ることだけを確認する。
        entries = [
            w
            for w in pyfltr.warnings_.collected_warnings()
            if w["source"] == "config" and ".pre-commit-config.yaml" in w["message"]
        ]
        assert len(entries) == 1
        assert "pre-commit" in entries[0]["message"]

    def test_pre_commit_enabled_with_config_no_warning(self, tmp_path: pathlib.Path) -> None:
        """設定ファイルが存在すれば警告は出ない。"""
        (tmp_path / "pyproject.toml").write_text('[tool.pyfltr]\npreset = "latest"\n')
        (tmp_path / ".pre-commit-config.yaml").write_text("repos: []\n")
        pyfltr.config.load_config(config_dir=tmp_path)
        # pre-commit の config は配置済みなので pre-commit 固有の警告は出ない。
        # textlint 等の他ツールの config_files 警告は関心外として除外する。
        entries = [
            w
            for w in pyfltr.warnings_.collected_warnings()
            if w["source"] == "config" and ".pre-commit-config.yaml" in w["message"]
        ]
        assert not entries

    def test_pre_commit_disabled_no_warning(self, tmp_path: pathlib.Path) -> None:
        """pre-commit 無効なら設定ファイル不在でも警告は出ない。"""
        (tmp_path / "pyproject.toml").write_text("[tool.pyfltr]\npre-commit = false\n")
        pyfltr.config.load_config(config_dir=tmp_path)
        entries = [w for w in pyfltr.warnings_.collected_warnings() if w["source"] == "config"]
        assert not entries

    def test_custom_command_missing_config_file_emits_warning(self, tmp_path: pathlib.Path) -> None:
        """カスタムコマンドに config-files を指定し不在なら警告。"""
        pyproject_content = """
[tool.pyfltr.custom-commands.mytool]
type = "linter"
path = "mytool"
config-files = [".mytoolrc"]
"""
        (tmp_path / "pyproject.toml").write_text(pyproject_content)
        pyfltr.config.load_config(config_dir=tmp_path)
        entries = [w for w in pyfltr.warnings_.collected_warnings() if w["source"] == "config"]
        assert len(entries) == 1
        assert "mytool" in entries[0]["message"]

    def test_custom_command_config_file_present_no_warning(self, tmp_path: pathlib.Path) -> None:
        """カスタムコマンドの config-files が配置済みなら警告は出ない。"""
        pyproject_content = """
[tool.pyfltr.custom-commands.mytool]
type = "linter"
path = "mytool"
config-files = [".mytoolrc"]
"""
        (tmp_path / "pyproject.toml").write_text(pyproject_content)
        (tmp_path / ".mytoolrc").write_text("")
        pyfltr.config.load_config(config_dir=tmp_path)
        entries = [w for w in pyfltr.warnings_.collected_warnings() if w["source"] == "config"]
        assert not entries

    def test_config_files_glob_pattern(self, tmp_path: pathlib.Path) -> None:
        """config-files に glob を指定し、いずれかがマッチすれば警告は出ない。"""
        pyproject_content = """
[tool.pyfltr.custom-commands.mytool]
type = "linter"
path = "mytool"
config-files = [".mytoolrc*"]
"""
        (tmp_path / "pyproject.toml").write_text(pyproject_content)
        (tmp_path / ".mytoolrc.json").write_text("{}")
        pyfltr.config.load_config(config_dir=tmp_path)
        entries = [w for w in pyfltr.warnings_.collected_warnings() if w["source"] == "config"]
        assert not entries

    def test_config_files_invalid_type_rejected(self, tmp_path: pathlib.Path) -> None:
        """カスタムコマンドの config-files が list[str] でなければエラー。"""
        pyproject_content = """
[tool.pyfltr.custom-commands.mytool]
type = "linter"
path = "mytool"
config-files = "foo"
"""
        (tmp_path / "pyproject.toml").write_text(pyproject_content)
        with pytest.raises(ValueError, match="config-files"):
            pyfltr.config.load_config(config_dir=tmp_path)


def test_invalid_preset(tmp_path: pathlib.Path) -> None:
    """不正なpresetのテスト。"""
    pyproject_content = """
[tool.pyfltr]
preset = "invalid"
"""
    pyproject_path = tmp_path / "pyproject.toml"
    pyproject_path.write_text(pyproject_content)

    # 不正なプリセットでValueErrorが発生することを確認
    with pytest.raises(ValueError, match="invalid"):
        pyfltr.config.load_config(config_dir=tmp_path)


def test_removed_preset_20250710(tmp_path: pathlib.Path) -> None:
    """preset = "20250710" は ValueError になり、メッセージに「削除」と「latest」が含まれる。"""
    (tmp_path / "pyproject.toml").write_text('[tool.pyfltr]\npreset = "20250710"\n')
    with pytest.raises(ValueError, match="削除") as exc_info:
        pyfltr.config.load_config(config_dir=tmp_path)
    assert "latest" in str(exc_info.value)


@pytest.mark.parametrize("removed_tool", ["pyupgrade", "autoflake", "isort", "black", "pflake8"])
def test_removed_tool_config_key(tmp_path: pathlib.Path, removed_tool: str) -> None:
    """削除ツールの設定キーを書くと ValueError が出て、メッセージにツール名が含まれる。"""
    (tmp_path / "pyproject.toml").write_text(f"[tool.pyfltr]\n{removed_tool} = true\n")
    with pytest.raises(ValueError, match=removed_tool):
        pyfltr.config.load_config(config_dir=tmp_path)


def test_archive_config_defaults() -> None:
    """アーカイブ設定の既定値テスト。"""
    config = pyfltr.config.create_default_config()
    assert config["archive"] is True
    assert config["archive-max-runs"] == 100
    assert config["archive-max-size-mb"] == 1024
    assert config["archive-max-age-days"] == 30


def test_jsonl_smart_truncation_defaults() -> None:
    """JSONL smart truncation 設定の既定値テスト。"""
    config = pyfltr.config.create_default_config()
    # 既定は diagnostic 無制限 (0)、メッセージは 30 行 / 2000 文字 (従来ハードコード値)。
    assert config["jsonl-diagnostic-limit"] == 0
    assert config["jsonl-message-max-lines"] == 30
    assert config["jsonl-message-max-chars"] == 2000


def test_jsonl_smart_truncation_override(tmp_path: pathlib.Path) -> None:
    """pyproject.toml で JSONL smart truncation 設定を上書きできる。"""
    pyproject_content = """
[tool.pyfltr]
jsonl-diagnostic-limit = 50
jsonl-message-max-lines = 100
jsonl-message-max-chars = 5000
"""
    (tmp_path / "pyproject.toml").write_text(pyproject_content)
    config = pyfltr.config.load_config(config_dir=tmp_path)
    assert config["jsonl-diagnostic-limit"] == 50
    assert config["jsonl-message-max-lines"] == 100
    assert config["jsonl-message-max-chars"] == 5000


def test_jsonl_smart_truncation_invalid_type(tmp_path: pathlib.Path) -> None:
    """JSONL smart truncation キーに整数以外を指定するとエラーになる。"""
    (tmp_path / "pyproject.toml").write_text('[tool.pyfltr]\njsonl-diagnostic-limit = "many"\n')
    with pytest.raises(ValueError, match="jsonl-diagnostic-limit"):
        pyfltr.config.load_config(config_dir=tmp_path)


def test_archive_config_override(tmp_path: pathlib.Path) -> None:
    """pyproject.toml でアーカイブ設定を上書きできることのテスト。"""
    pyproject_content = """
[tool.pyfltr]
archive = false
archive-max-runs = 50
archive-max-size-mb = 512
archive-max-age-days = 7
"""
    (tmp_path / "pyproject.toml").write_text(pyproject_content)
    config = pyfltr.config.load_config(config_dir=tmp_path)
    assert config["archive"] is False
    assert config["archive-max-runs"] == 50
    assert config["archive-max-size-mb"] == 512
    assert config["archive-max-age-days"] == 7


def test_respect_gitignore_default() -> None:
    """respect-gitignore の既定値が True であることを確認する。"""
    config = pyfltr.config.create_default_config()
    assert config["respect-gitignore"] is True


def test_cache_config_defaults() -> None:
    """ファイル hash キャッシュ設定の既定値テスト。"""
    config = pyfltr.config.create_default_config()
    assert config["cache"] is True
    assert config["cache-max-age-hours"] == 12


def test_cache_config_override(tmp_path: pathlib.Path) -> None:
    """pyproject.toml でキャッシュ設定を上書きできる。"""
    pyproject_content = """
[tool.pyfltr]
cache = false
cache-max-age-hours = 24
"""
    (tmp_path / "pyproject.toml").write_text(pyproject_content)
    config = pyfltr.config.load_config(config_dir=tmp_path)
    assert config["cache"] is False
    assert config["cache-max-age-hours"] == 24


def test_cache_config_invalid_type(tmp_path: pathlib.Path) -> None:
    """cache-max-age-hours に整数以外を指定するとエラーになる。"""
    (tmp_path / "pyproject.toml").write_text('[tool.pyfltr]\ncache-max-age-hours = "many"\n')
    with pytest.raises(ValueError, match="cache-max-age-hours"):
        pyfltr.config.load_config(config_dir=tmp_path)


def test_textlint_command_info_is_cacheable() -> None:
    """textlint の CommandInfo が cacheable=True で config_files を完全列挙している。"""
    config = pyfltr.config.create_default_config()
    info = config.commands["textlint"]
    assert info.cacheable is True
    # textlint の公式設定ファイル候補が全て列挙されている
    assert ".textlintrc" in info.config_files
    assert ".textlintrc.json" in info.config_files
    assert ".textlintrc.yml" in info.config_files
    assert ".textlintrc.yaml" in info.config_files
    assert ".textlintrc.js" in info.config_files
    assert ".textlintrc.cjs" in info.config_files
    assert "package.json" in info.config_files
    assert ".textlintignore" in info.config_files


def test_non_cacheable_commands_remain_default() -> None:
    """textlint 以外のビルトインコマンドは cacheable=False (既定) のまま。"""
    config = pyfltr.config.create_default_config()
    for name, info in config.commands.items():
        if name == "textlint":
            continue
        assert info.cacheable is False, f"{name} が意図せず cacheable=True になっている"


def test_auto_option_defaults() -> None:
    """自動オプションの既定値テスト。"""
    config = pyfltr.config.create_default_config()
    assert config["pylint-pydantic"] is True
    assert config["mypy-unused-awaitable"] is True


def test_auto_option_disable(tmp_path: pathlib.Path) -> None:
    """自動オプションを False に設定できる。"""
    pyproject_content = """
[tool.pyfltr]
pylint-pydantic = false
mypy-unused-awaitable = false
"""
    (tmp_path / "pyproject.toml").write_text(pyproject_content)
    config = pyfltr.config.load_config(config_dir=tmp_path)
    assert config["pylint-pydantic"] is False
    assert config["mypy-unused-awaitable"] is False


def test_python_default() -> None:
    """python の既定値は False（opt-in）。"""
    config = pyfltr.config.create_default_config()
    assert config["python"] is False


def test_python_default_disables_python_tools() -> None:
    """既定で Python 系ツールが全て無効化されている。"""
    config = pyfltr.config.create_default_config()
    for cmd in pyfltr.config.PYTHON_COMMANDS:
        assert config[cmd] is False, f"{cmd} は既定で無効化されるべき"
    # JS/共通系も影響を受けない
    assert config["markdownlint"] is False
    assert config["textlint"] is False


def test_python_true_without_preset_enables_nothing(tmp_path: pathlib.Path) -> None:
    """python = true 単独では何も有効化されない (preset が gate を通過する対象を決める)。"""
    pyproject_content = """
[tool.pyfltr]
python = true
"""
    (tmp_path / "pyproject.toml").write_text(pyproject_content)
    config = pyfltr.config.load_config(config_dir=tmp_path)
    for cmd in pyfltr.config.PYTHON_COMMANDS:
        assert config[cmd] is False, f"{cmd} は preset 未指定では False のまま"
    # docs 系も preset 未指定なので False
    assert config["markdownlint"] is False
    assert config["textlint"] is False


def test_python_true_with_preset_latest(tmp_path: pathlib.Path) -> None:
    """preset = latest + python = true で preset 内の Python 系推奨構成が gate を通過する。"""
    pyproject_content = """
[tool.pyfltr]
preset = "latest"
python = true
"""
    (tmp_path / "pyproject.toml").write_text(pyproject_content)
    config = pyfltr.config.load_config(config_dir=tmp_path)
    # preset = latest (= 20260413) に含まれる Python 系推奨ツールが一式 True
    assert config["ruff-format"] is True
    assert config["ruff-check"] is True
    assert config["mypy"] is True
    assert config["pylint"] is True
    assert config["pyright"] is True
    assert config["pytest"] is True
    assert config["uv-sort"] is True
    # ty は preset 非収録のため個別指定が必要
    assert config["ty"] is False
    # preset が有効化したドキュメント系ツールも有効
    assert config["textlint"] is True
    assert config["markdownlint"] is True


def test_python_true_with_individual_extra(tmp_path: pathlib.Path) -> None:
    """preset = latest + python = true + ty = true で preset 非収録の ty も有効化される。"""
    pyproject_content = """
[tool.pyfltr]
preset = "latest"
python = true
ty = true
"""
    (tmp_path / "pyproject.toml").write_text(pyproject_content)
    config = pyfltr.config.load_config(config_dir=tmp_path)
    # 個別指定で ty が True
    assert config["ty"] is True
    # preset 内の Python 系も一式有効
    assert config["mypy"] is True
    assert config["pyright"] is True
    assert config["ruff-format"] is True


def test_python_true_with_individual_override(tmp_path: pathlib.Path) -> None:
    """preset + python = true でも個別 ``{command} = false`` で上書きできる。"""
    pyproject_content = """
[tool.pyfltr]
preset = "latest"
python = true
ruff-check = false
mypy = false
"""
    (tmp_path / "pyproject.toml").write_text(pyproject_content)
    config = pyfltr.config.load_config(config_dir=tmp_path)
    # 個別に False 指定したものは False
    assert config["ruff-check"] is False
    assert config["mypy"] is False
    # 他の preset 内 Python 系は有効
    assert config["ruff-format"] is True
    assert config["pyright"] is True
    assert config["pylint"] is True


def test_individual_tool_crosses_gate(tmp_path: pathlib.Path) -> None:
    """言語カテゴリが False でも個別 ``{command} = true`` は gate を越えて有効化される。"""
    pyproject_content = """
[tool.pyfltr]
preset = "latest"
mypy = true
"""
    (tmp_path / "pyproject.toml").write_text(pyproject_content)
    config = pyfltr.config.load_config(config_dir=tmp_path)
    # python = false でも mypy だけ個別指定で True
    assert config["mypy"] is True
    # preset 由来の Python 系は gate により False へ押し戻される
    assert config["ruff-format"] is False
    assert config["ruff-check"] is False
    assert config["pylint"] is False
    assert config["pyright"] is False
    assert config["pytest"] is False


def test_bin_runner_default() -> None:
    """bin-runner の既定値は mise。"""
    config = pyfltr.config.create_default_config()
    assert config["bin-runner"] == "mise"


def test_bin_runner_override(tmp_path: pathlib.Path) -> None:
    """pyproject.toml で bin-runner を上書きできる。"""
    pyproject_content = """
[tool.pyfltr]
bin-runner = "direct"
"""
    (tmp_path / "pyproject.toml").write_text(pyproject_content)
    config = pyfltr.config.load_config(config_dir=tmp_path)
    assert config["bin-runner"] == "direct"


def test_bin_runner_invalid_rejected(tmp_path: pathlib.Path) -> None:
    """bin-runner に未知の値を指定するとエラーになる。"""
    pyproject_content = """
[tool.pyfltr]
bin-runner = "bogus"
"""
    (tmp_path / "pyproject.toml").write_text(pyproject_content)
    with pytest.raises(ValueError, match="bin-runner"):
        pyfltr.config.load_config(config_dir=tmp_path)


def test_preset_latest_suppresses_language_categories(tmp_path: pathlib.Path) -> None:
    """preset = "latest" 単独 (カテゴリキー全 False) では全言語のツールが False に押し戻される。"""
    (tmp_path / "pyproject.toml").write_text('[tool.pyfltr]\npreset = "latest"\n')
    config = pyfltr.config.load_config(config_dir=tmp_path)
    # preset に含まれるドキュメント系は True
    for cmd in ("markdownlint", "textlint", "actionlint", "typos", "pre-commit"):
        assert config[cmd] is True, f"{cmd} は preset=latest で有効化されるべき"
    # 言語カテゴリに属するツールは gate により全て False に押し戻される
    # (_PRESET_BASE で True だった Python 核 / JS / Rust / .NET も含む)
    for _, commands in pyfltr.config.LANGUAGE_CATEGORIES:
        for cmd in commands:
            assert config[cmd] is False, f"{cmd} は preset=latest 単独では gate で False"


def test_javascript_true_enables_preset_tools(tmp_path: pathlib.Path) -> None:
    """javascript = true で preset 内の JS/TS 系推奨ツール一式が gate 通過で有効化される。"""
    (tmp_path / "pyproject.toml").write_text('[tool.pyfltr]\npreset = "latest"\njavascript = true\n')
    config = pyfltr.config.load_config(config_dir=tmp_path)
    # preset = latest に含まれる JS/TS 系推奨ツールが一式 True
    for cmd in pyfltr.config.JAVASCRIPT_COMMANDS:
        assert config[cmd] is True, f"{cmd} は preset=latest + javascript=true で有効化されるべき"
    # 他言語カテゴリは gate 閉のまま False
    for cmd in pyfltr.config.PYTHON_COMMANDS:
        assert config[cmd] is False, f"{cmd} は python gate 閉のため False"
    for cmd in pyfltr.config.RUST_COMMANDS:
        assert config[cmd] is False, f"{cmd} は rust gate 閉のため False"


def test_javascript_true_with_individual_override(tmp_path: pathlib.Path) -> None:
    """javascript = true でも個別 ``{command} = false`` で preset 由来 True を無効化できる。"""
    pyproject_content = """
[tool.pyfltr]
preset = "latest"
javascript = true
prettier = false
"""
    (tmp_path / "pyproject.toml").write_text(pyproject_content)
    config = pyfltr.config.load_config(config_dir=tmp_path)
    # 個別に False 指定した prettier だけ False
    assert config["prettier"] is False
    # 他の JS/TS 系は gate 通過で True のまま
    assert config["eslint"] is True
    assert config["biome"] is True
    assert config["tsc"] is True


def test_rust_true_enables_preset_tools(tmp_path: pathlib.Path) -> None:
    """rust = true で preset 内の Rust 系推奨ツール一式が gate 通過で有効化される。"""
    (tmp_path / "pyproject.toml").write_text('[tool.pyfltr]\npreset = "latest"\nrust = true\n')
    config = pyfltr.config.load_config(config_dir=tmp_path)
    for cmd in pyfltr.config.RUST_COMMANDS:
        assert config[cmd] is True, f"{cmd} は preset=latest + rust=true で有効化されるべき"


def test_dotnet_true_enables_preset_tools(tmp_path: pathlib.Path) -> None:
    """dotnet = true で preset 内の .NET 系推奨ツール一式が gate 通過で有効化される。"""
    (tmp_path / "pyproject.toml").write_text('[tool.pyfltr]\npreset = "latest"\ndotnet = true\n')
    config = pyfltr.config.load_config(config_dir=tmp_path)
    for cmd in pyfltr.config.DOTNET_COMMANDS:
        assert config[cmd] is True, f"{cmd} は preset=latest + dotnet=true で有効化されるべき"


def test_individual_tool_enables_despite_category_false(tmp_path: pathlib.Path) -> None:
    """言語カテゴリが False でも個別 ``{tool} = true`` でそのツールだけ有効化される。"""
    pyproject_content = """
[tool.pyfltr]
preset = "latest"
eslint = true
cargo-fmt = true
"""
    (tmp_path / "pyproject.toml").write_text(pyproject_content)
    config = pyfltr.config.load_config(config_dir=tmp_path)
    # 個別に True にしたツールは有効
    assert config["eslint"] is True
    assert config["cargo-fmt"] is True
    # 同カテゴリの他ツールは gate で False へ押し戻される
    assert config["prettier"] is False
    assert config["biome"] is False
    assert config["cargo-clippy"] is False


def test_language_categories_defaults_false() -> None:
    """言語カテゴリキーの既定値は全て False。"""
    config = pyfltr.config.create_default_config()
    for category_key, _ in pyfltr.config.LANGUAGE_CATEGORIES:
        assert config[category_key] is False, f"{category_key} の既定値は False であるべき"


def test_custom_command_pass_filenames(tmp_path: pathlib.Path) -> None:
    """カスタムコマンドのpass-filenames設定が登録される。"""
    pyproject_content = """
[tool.pyfltr.custom-commands.my-checker]
type = "linter"
path = "my-checker"
pass-filenames = false
"""
    (tmp_path / "pyproject.toml").write_text(pyproject_content)
    config = pyfltr.config.load_config(config_dir=tmp_path)
    assert config["my-checker-pass-filenames"] is False


def test_custom_command_pass_filenames_default(tmp_path: pathlib.Path) -> None:
    """カスタムコマンドのpass-filenamesの既定値はTrue。"""
    pyproject_content = """
[tool.pyfltr.custom-commands.my-checker]
type = "linter"
path = "my-checker"
"""
    (tmp_path / "pyproject.toml").write_text(pyproject_content)
    config = pyfltr.config.load_config(config_dir=tmp_path)
    assert config["my-checker-pass-filenames"] is True


def test_bin_tool_default_config_values() -> None:
    """bin-runner対応ツールのデフォルト設定値が正しく定義されている。"""
    config = pyfltr.config.create_default_config()
    # 全bin系ツールの有効/無効とバージョン設定を確認
    bin_tools = ["ec", "shellcheck", "shfmt", "typos", "actionlint"]
    for tool in bin_tools:
        assert config[tool] is False, f"{tool}は既定で無効"
        assert config[f"{tool}-path"] == "", f"{tool}-pathは空文字"
        assert config[f"{tool}-version"] == "latest", f"{tool}-versionはlatest"
        assert config[f"{tool}-fast"] is True, f"{tool}-fastはTrue"

    # uv-sortの既定値
    assert config["uv-sort"] is False
    assert config["uv-sort-path"] == "uv-sort"
    assert config["uv-sort-fast"] is True

    # tscのpass-filenames
    assert config["tsc-pass-filenames"] is False


def test_uv_sort_in_python_commands() -> None:
    """uv-sortがPYTHON_COMMANDSに含まれる。"""
    assert "uv-sort" in pyfltr.config.PYTHON_COMMANDS


def test_bin_runners_tuple() -> None:
    """BIN_RUNNERS に direct と mise が含まれる。"""
    assert "direct" in pyfltr.config.BIN_RUNNERS
    assert "mise" in pyfltr.config.BIN_RUNNERS


def test_builtin_targets_override_str(tmp_path: pathlib.Path) -> None:
    """ビルトインコマンドの targets を文字列で完全上書きできる。"""
    (tmp_path / "pyproject.toml").write_text('[tool.pyfltr]\nshfmt-targets = "*.bash"\n')
    config = pyfltr.config.load_config(config_dir=tmp_path)
    assert config.commands["shfmt"].targets == "*.bash"
    assert config.commands["shfmt"].target_globs() == ["*.bash"]


def test_builtin_targets_override_list(tmp_path: pathlib.Path) -> None:
    """ビルトインコマンドの targets をリストで完全上書きできる。"""
    (tmp_path / "pyproject.toml").write_text('[tool.pyfltr]\nshfmt-targets = ["*.sh", "*.bash"]\n')
    config = pyfltr.config.load_config(config_dir=tmp_path)
    assert config.commands["shfmt"].targets == ["*.sh", "*.bash"]
    assert config.commands["shfmt"].target_globs() == ["*.sh", "*.bash"]


def test_builtin_extend_targets_str(tmp_path: pathlib.Path) -> None:
    """ビルトインコマンドの targets に文字列で追加できる。"""
    (tmp_path / "pyproject.toml").write_text('[tool.pyfltr]\nshfmt-extend-targets = "*.bash"\n')
    config = pyfltr.config.load_config(config_dir=tmp_path)
    # デフォルトの "*.sh" に "*.bash" が追加される
    assert config.commands["shfmt"].target_globs() == ["*.sh", "*.bash"]


def test_builtin_extend_targets_list(tmp_path: pathlib.Path) -> None:
    """ビルトインコマンドの targets にリストで追加できる。"""
    (tmp_path / "pyproject.toml").write_text('[tool.pyfltr]\nshfmt-extend-targets = ["*.bash", "dot_bashrc"]\n')
    config = pyfltr.config.load_config(config_dir=tmp_path)
    assert config.commands["shfmt"].target_globs() == [
        "*.sh",
        "*.bash",
        "dot_bashrc",
    ]


def test_builtin_targets_and_extend_targets(tmp_path: pathlib.Path) -> None:
    """targets で上書き後に extend-targets で追加される。"""
    (tmp_path / "pyproject.toml").write_text(
        '[tool.pyfltr]\nshfmt-targets = ["*.bash"]\nshfmt-extend-targets = ["dot_bashrc"]\n'
    )
    config = pyfltr.config.load_config(config_dir=tmp_path)
    assert config.commands["shfmt"].target_globs() == ["*.bash", "dot_bashrc"]


def test_builtin_targets_invalid_type(tmp_path: pathlib.Path) -> None:
    """ビルトインコマンドの targets に不正な型を指定するとエラーになる。"""
    (tmp_path / "pyproject.toml").write_text("[tool.pyfltr]\nshfmt-targets = 42\n")
    with pytest.raises(ValueError, match="targets"):
        pyfltr.config.load_config(config_dir=tmp_path)


def test_builtin_targets_unknown_command(tmp_path: pathlib.Path) -> None:
    """未知のコマンド名の targets 指定はエラーになる。"""
    (tmp_path / "pyproject.toml").write_text('[tool.pyfltr]\nunknown-targets = "*.py"\n')
    with pytest.raises(ValueError, match="設定キーが不正です"):
        pyfltr.config.load_config(config_dir=tmp_path)


def test_builtin_targets_no_mutation_of_builtins(tmp_path: pathlib.Path) -> None:
    """targets 上書きで BUILTIN_COMMANDS が汚染されない。"""
    original_targets = pyfltr.config.BUILTIN_COMMANDS["shfmt"].targets
    (tmp_path / "pyproject.toml").write_text('[tool.pyfltr]\nshfmt-targets = "*.bash"\n')
    config = pyfltr.config.load_config(config_dir=tmp_path)
    assert config.commands["shfmt"].targets == "*.bash"
    # BUILTIN_COMMANDS 側は元のまま
    assert pyfltr.config.BUILTIN_COMMANDS["shfmt"].targets == original_targets


# Rust / .NET 言語ツール向けのテスト群。
# 全ツール既定 False、pass-filenames=False、formatter は常時書き込みモード、
# cargo-clippy のみ lint-args / fix-args を持つ。
# pylint duplicate-code (R0801) を避けるため、config 側の定義をそのまま再利用する。
_NATIVE_LANG_TOOLS: tuple[str, ...] = pyfltr.config.RUST_COMMANDS + pyfltr.config.DOTNET_COMMANDS


def test_native_lang_tools_registered() -> None:
    """Rust / .NET 言語ツールが BUILTIN_COMMANDS と DEFAULT_CONFIG に登録されている。"""
    config = pyfltr.config.create_default_config()
    for tool in _NATIVE_LANG_TOOLS:
        assert tool in pyfltr.config.BUILTIN_COMMANDS, f"{tool} が BUILTIN_COMMANDS に未登録"
        assert config[tool] is False, f"{tool} の既定値は False であるべき"
        assert config[f"{tool}-path"], f"{tool}-path が設定されていない"


def test_native_lang_tools_pass_filenames_false() -> None:
    """Rust / .NET 言語ツールは全て pass-filenames=False (crate / solution 全体を対象)。"""
    config = pyfltr.config.create_default_config()
    for tool in _NATIVE_LANG_TOOLS:
        assert config[f"{tool}-pass-filenames"] is False, f"{tool}-pass-filenames は False であるべき"


def test_native_lang_tools_command_types() -> None:
    """Rust / .NET 言語ツールの type 分類。"""
    expected = {
        "cargo-fmt": "formatter",
        "cargo-clippy": "linter",
        "cargo-check": "linter",
        "cargo-test": "tester",
        "cargo-deny": "linter",
        "dotnet-format": "formatter",
        "dotnet-build": "linter",
        "dotnet-test": "tester",
    }
    for tool, expected_type in expected.items():
        assert pyfltr.config.BUILTIN_COMMANDS[tool].type == expected_type


def test_native_formatters_write_by_default() -> None:
    """cargo-fmt / dotnet-format は既定で書き込みモード (--check 等を含まない)。"""
    config = pyfltr.config.create_default_config()
    assert config["cargo-fmt-args"] == ["fmt"]
    assert config["dotnet-format-args"] == ["format"]
    # pyfltr 規約: formatter には fix-args を定義しない
    assert "cargo-fmt-fix-args" not in config.values
    assert "dotnet-format-fix-args" not in config.values


def test_cargo_clippy_args_separation() -> None:
    """cargo-clippy は args / lint-args / fix-args を分離し、trailing `-- -D warnings` を双方に持つ。"""
    config = pyfltr.config.create_default_config()
    assert config["cargo-clippy-args"] == _testconf.CARGO_CLIPPY_ARGS
    assert config["cargo-clippy-lint-args"] == _testconf.CARGO_CLIPPY_LINT_ARGS
    assert config["cargo-clippy-fix-args"] == _testconf.CARGO_CLIPPY_FIX_ARGS


def test_native_lang_tools_fast_defaults() -> None:
    """fast 既定値は cargo-fmt / cargo-clippy / dotnet-format のみ True。"""
    config = pyfltr.config.create_default_config()
    assert config["cargo-fmt-fast"] is True
    assert config["cargo-clippy-fast"] is True
    assert config["dotnet-format-fast"] is True
    for tool in ("cargo-check", "cargo-test", "cargo-deny", "dotnet-build", "dotnet-test"):
        assert config[f"{tool}-fast"] is False, f"{tool}-fast は既定 False であるべき"


def test_native_lang_tools_not_affected_by_python(tmp_path: pathlib.Path) -> None:
    """python 設定は Rust / .NET 言語ツールの設定を変更しない。"""
    pyproject_content = """
[tool.pyfltr]
preset = "latest"
python = true
mypy = true
pytest = true
cargo-fmt = true
cargo-clippy = true
dotnet-format = true
"""
    (tmp_path / "pyproject.toml").write_text(pyproject_content)
    config = pyfltr.config.load_config(config_dir=tmp_path)
    assert config["cargo-fmt"] is True
    assert config["cargo-clippy"] is True
    assert config["dotnet-format"] is True
    # python 系ツールは個別指定で有効化されている
    assert config["mypy"] is True
    assert config["pytest"] is True


def test_native_lang_tools_serial_group() -> None:
    """cargo 系は serial_group=cargo、dotnet 系は serial_group=dotnet に設定される。"""
    expected = {
        "cargo-fmt": "cargo",
        "cargo-clippy": "cargo",
        "cargo-check": "cargo",
        "cargo-test": "cargo",
        "cargo-deny": "cargo",
        "dotnet-format": "dotnet",
        "dotnet-build": "dotnet",
        "dotnet-test": "dotnet",
    }
    for tool, group in expected.items():
        assert pyfltr.config.BUILTIN_COMMANDS[tool].serial_group == group, f"{tool}.serial_group は {group!r} であるべき"


def test_existing_tools_have_no_serial_group() -> None:
    """既存ツールは serial_group 未設定 (後方互換)。"""
    for name, info in pyfltr.config.BUILTIN_COMMANDS.items():
        if name.startswith(("cargo-", "dotnet-")):
            continue
        assert info.serial_group is None, f"{name}.serial_group は None であるべき"


def test_native_lang_tools_in_aliases() -> None:
    """Rust / .NET 言語ツールが format / lint / test の各エイリアスに含まれる。"""
    config = pyfltr.config.create_default_config()
    aliases = config["aliases"]
    assert "cargo-fmt" in aliases["format"]
    assert "dotnet-format" in aliases["format"]
    assert "cargo-clippy" in aliases["lint"]
    assert "cargo-check" in aliases["lint"]
    assert "cargo-deny" in aliases["lint"]
    assert "dotnet-build" in aliases["lint"]
    assert "cargo-test" in aliases["test"]
    assert "dotnet-test" in aliases["test"]


def test_tool_exclude_loaded(tmp_path: pathlib.Path) -> None:
    """{tool}-exclude が pyproject.toml から読み込まれて config.values に格納される。"""
    (tmp_path / "pyproject.toml").write_text('[tool.pyfltr]\nmypy-exclude = ["vendor", "gen_*.py"]\n')
    config = pyfltr.config.load_config(config_dir=tmp_path)
    assert config.values["mypy-exclude"] == ["vendor", "gen_*.py"]


def test_tool_exclude_unknown_command(tmp_path: pathlib.Path) -> None:
    """未知のコマンド名の {tool}-exclude 指定はエラーになる。"""
    (tmp_path / "pyproject.toml").write_text('[tool.pyfltr]\nunknown-exclude = ["foo"]\n')
    with pytest.raises(ValueError, match="設定キーが不正です"):
        pyfltr.config.load_config(config_dir=tmp_path)


def test_tool_exclude_invalid_type(tmp_path: pathlib.Path) -> None:
    """{tool}-exclude に文字列リスト以外を指定するとエラーになる。"""
    (tmp_path / "pyproject.toml").write_text("[tool.pyfltr]\nmypy-exclude = 42\n")
    with pytest.raises(ValueError, match="str型のリスト"):
        pyfltr.config.load_config(config_dir=tmp_path)
