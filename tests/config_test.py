"""config.py のテストコード。"""
# pylint: disable=too-many-lines  # 設定検証のSSOTテストはfixture密結合化を避けるため分割しない方針
# pylint: disable=protected-access  # _PRESETS等の内部定数を参照する単体テスト経路

import pathlib

import pytest

import pyfltr.config.config
import pyfltr.warnings_
from tests import conftest as _testconf


def _assert_language_gate(
    config: pyfltr.config.config.Config,
    category_key: str,
    *,
    passed: bool,
    preset: str = "latest",
) -> None:
    """言語カテゴリ gate の挙動をカテゴリ内ツール全件について一括検証する。

    `passed=True`: gate 開放側。当該カテゴリで preset が True にしたツールはそのまま
    True 通過し、preset が収録していないツールは False のままであることを確認する
    （個別 `{command} = true` が無い前提）。
    `passed=False`: gate 閉じ側。当該カテゴリの全ツールが False（preset 由来 True を
    gate が上書きする）になっていることを確認する。

    個別 `{command} = true` / `{command} = false` の上書きがあるテストでは、
    本ヘルパーではなく直接 assert を使う。
    """
    commands = dict(pyfltr.config.config.LANGUAGE_CATEGORIES)[category_key]
    if not passed:
        for cmd in commands:
            assert config[cmd] is False, f"{category_key} gate閉なのに{cmd}=True"
        return
    preset_tools = pyfltr.config.config._PRESETS[preset]
    for cmd in commands:
        expected = preset_tools.get(cmd, False)
        assert config[cmd] is expected, f"{category_key} gate開 preset={preset}: {cmd} expected {expected}, got {config[cmd]}"


_DOCS_ORTHOGONAL_KEYS = ("textlint", "markdownlint", "actionlint", "typos", "pre-commit")
"""言語カテゴリ gate の対象外となるドキュメント系ツールキー。

preset が直接 True/False を決め、言語カテゴリキーの影響を受けない。
"""


@pytest.mark.parametrize(
    "preset,extra_lines,docs_expected,gate_passed",
    [
        # presetが空: 全ツール既定（False）。
        (
            "",
            "",
            {"textlint": False, "markdownlint": False, "actionlint": False, "typos": False, "pre-commit": False},
            {"python": False, "javascript": False, "rust": False, "dotnet": False},
        ),
        # 20260330 + python=true: Python核 + pyright + docs（textlint/markdownlint）。
        (
            "20260330",
            "python = true\n",
            {"textlint": True, "markdownlint": True, "actionlint": False, "typos": False, "pre-commit": False},
            {"python": True, "javascript": False, "rust": False, "dotnet": False},
        ),
        # 20260411はactionlint / typos / uv-sortが追加される。
        (
            "20260411",
            "python = true\n",
            {"textlint": True, "markdownlint": True, "actionlint": True, "typos": True, "pre-commit": False},
            {"python": True, "javascript": False, "rust": False, "dotnet": False},
        ),
        # 20260413はpre-commitが追加される。
        (
            "20260413",
            "python = true\n",
            {"textlint": True, "markdownlint": True, "actionlint": True, "typos": True, "pre-commit": True},
            {"python": True, "javascript": False, "rust": False, "dotnet": False},
        ),
        # latest = 20260413と同じ構成。
        (
            "latest",
            "python = true\n",
            {"textlint": True, "markdownlint": True, "actionlint": True, "typos": True, "pre-commit": True},
            {"python": True, "javascript": False, "rust": False, "dotnet": False},
        ),
        # latest + javascript=true: JS/TS系gate通過、docsはpreset由来でTrueのまま。
        (
            "latest",
            "javascript = true\n",
            {"textlint": True, "markdownlint": True},
            {"python": False, "javascript": True, "rust": False, "dotnet": False},
        ),
        # latest + rust=true: Rust系gate通過。docsは検証不要（orthogonal）。
        (
            "latest",
            "rust = true\n",
            {},
            {"python": False, "javascript": False, "rust": True, "dotnet": False},
        ),
        # latest + dotnet=true: .NET系gate通過。
        (
            "latest",
            "dotnet = true\n",
            {},
            {"python": False, "javascript": False, "rust": False, "dotnet": True},
        ),
        # 20260330 + rust=true: 歴史的presetでも_PRESET_BASE経由でRust系が一式有効化される。
        (
            "20260330",
            "rust = true\n",
            {},
            {"python": False, "javascript": False, "rust": True, "dotnet": False},
        ),
    ],
)
def test_apply_preset(
    tmp_path: pathlib.Path,
    preset: str,
    extra_lines: str,
    docs_expected: dict[str, bool],
    gate_passed: dict[str, bool],
) -> None:
    """preset × 言語カテゴリgateの有効化パターンを一括検証する。

    `docs_expected`は言語カテゴリと独立に決まるドキュメント系ツールの期待値。
    `gate_passed`は各言語カテゴリキーのgate開閉状態。言語カテゴリ所属ツールの期待値は
    `_assert_language_gate`でpreset内容から機械的に算出する。
    """
    pyproject_path = tmp_path / "pyproject.toml"
    pyproject_path.write_text(f'[tool.pyfltr]\npreset = "{preset}"\n{extra_lines}')

    config = pyfltr.config.config.load_config(config_dir=tmp_path)
    for key, value in docs_expected.items():
        assert config[key] == value, f"{key}: expected {value}, got {config[key]}"
    # preset=""のとき_assert_language_gateのpreset参照が無効化されるようgate閉のみ検証
    # （gate閉の枝はpreset引数を見ないため"latest"等どの値を渡しても問題ない）
    preset_for_gate = preset or "latest"
    for category_key, passed in gate_passed.items():
        _assert_language_gate(config, category_key, passed=passed, preset=preset_for_gate)


def test_custom_command(tmp_path: pathlib.Path) -> None:
    """カスタムコマンド定義のテスト。"""
    # presetとpython opt-inを併用することでPython系ツールの有効化も検証する。
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
    config = pyfltr.config.config.load_config(config_dir=tmp_path)

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

    # preset + python = trueのgate通過によりpreset内のPython系ツールが有効化されている
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
        pyfltr.config.config.load_config(config_dir=tmp_path)


def test_custom_command_invalid_type(tmp_path: pathlib.Path) -> None:
    """カスタムコマンドの不正なtypeテスト。"""
    pyproject_content = """
[tool.pyfltr.custom-commands.foo]
type = "invalid"
"""
    (tmp_path / "pyproject.toml").write_text(pyproject_content)
    with pytest.raises(ValueError, match="type"):
        pyfltr.config.config.load_config(config_dir=tmp_path)


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
        pyfltr.config.config.load_config(config_dir=tmp_path)


def test_fast_alias_dynamic(tmp_path: pathlib.Path) -> None:
    """fastエイリアスがper-command fastフラグから動的計算されることのテスト。"""
    # デフォルト設定でfastエイリアスが正しく構築される
    # fastエイリアスはツールの有効/無効に関わらず{tool}-fastフラグがTrueのものを列挙する
    config = pyfltr.config.config.create_default_config()
    fast = config["aliases"]["fast"]
    # ruff-format-fast=Trueなのでfastに含まれる
    assert "ruff-format" in fast
    # mypy-fast=Falseなのでfastに含まれない
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
    config = pyfltr.config.config.load_config(config_dir=tmp_path)
    fast = config["aliases"]["fast"]
    assert "mypy" in fast
    assert "ruff-format" not in fast


def test_ruff_format_by_check_default() -> None:
    """ruff-format-by-checkのデフォルト値テスト。"""
    config = pyfltr.config.config.create_default_config()
    # デフォルトで有効
    assert config["ruff-format-by-check"] is True
    # デフォルトのcheck用引数はruff check --fix --unsafe-fixes
    assert config["ruff-format-check-args"] == ["check", "--fix", "--unsafe-fixes"]


def test_ruff_format_by_check_overridable(tmp_path: pathlib.Path) -> None:
    """pyproject.tomlでruff-format-by-checkを上書きできることのテスト。"""
    pyproject_content = """
[tool.pyfltr]
ruff-format-by-check = false
ruff-format-check-args = ["check", "--fix"]
"""
    (tmp_path / "pyproject.toml").write_text(pyproject_content)
    config = pyfltr.config.config.load_config(config_dir=tmp_path)
    assert config["ruff-format-by-check"] is False
    assert config["ruff-format-check-args"] == ["check", "--fix"]


def test_fix_args_defaults() -> None:
    """fix-argsの既定値テスト。"""
    config = pyfltr.config.config.create_default_config()
    # fix対応ビルトインはfix-argsが定義されている
    assert config["textlint-fix-args"] == ["--fix"]
    assert config["markdownlint-fix-args"] == ["--fix"]
    assert config["ruff-check-fix-args"] == ["--fix", "--unsafe-fixes"]
    # fix非対応ビルトインはfix-argsキーが存在しない
    assert "mypy-fix-args" not in config.values
    assert "pytest-fix-args" not in config.values


def test_filter_fix_commands_defaults() -> None:
    """`filter_fix_commands`の基本動作テスト。"""
    config = pyfltr.config.config.create_default_config()
    # 既定では全ツール無効またはfix-args未定義のため全て除外
    commands = ["mypy", "textlint", "markdownlint", "ruff-check"]
    result = pyfltr.config.config.filter_fix_commands(commands, config)
    # mypyはfix-args未定義、textlint/markdownlint/ruff-checkはdisabledのため全て除外
    assert not result


def test_filter_fix_commands_enabled_linter(tmp_path: pathlib.Path) -> None:
    """enabledにしたfix対応linterがfilter_fix_commandsに含まれることのテスト。"""
    pyproject_content = """
[tool.pyfltr]
textlint = true
markdownlint = true
ruff-check = true
"""
    (tmp_path / "pyproject.toml").write_text(pyproject_content)
    config = pyfltr.config.config.load_config(config_dir=tmp_path)
    commands = ["textlint", "markdownlint", "ruff-check", "mypy"]
    result = pyfltr.config.config.filter_fix_commands(commands, config)
    assert "textlint" in result
    assert "markdownlint" in result
    assert "ruff-check" in result
    assert "mypy" not in result  # fix-args未定義


def test_custom_command_fix_args(tmp_path: pathlib.Path) -> None:
    """カスタムコマンドのfix-args登録テスト。"""
    pyproject_content = """
[tool.pyfltr.custom-commands.my-linter]
type = "linter"
path = "my-linter"
args = ["--check"]
fix-args = ["--fix", "--verbose"]
"""
    (tmp_path / "pyproject.toml").write_text(pyproject_content)
    config = pyfltr.config.config.load_config(config_dir=tmp_path)
    assert config["my-linter-fix-args"] == ["--fix", "--verbose"]
    # filter_fix_commandsにも含まれる
    result = pyfltr.config.config.filter_fix_commands(["my-linter"], config)
    assert result == ["my-linter"]


def test_custom_command_without_fix_args(tmp_path: pathlib.Path) -> None:
    """fix-argsを省略したカスタムlinterはfix対象外になる。"""
    pyproject_content = """
[tool.pyfltr.custom-commands.plain-linter]
type = "linter"
path = "plain-linter"
"""
    (tmp_path / "pyproject.toml").write_text(pyproject_content)
    config = pyfltr.config.config.load_config(config_dir=tmp_path)
    assert "plain-linter-fix-args" not in config.values
    result = pyfltr.config.config.filter_fix_commands(["plain-linter"], config)
    assert not result


def test_custom_command_fix_args_invalid(tmp_path: pathlib.Path) -> None:
    """fix-argsが文字列などの場合はエラーになる。"""
    pyproject_content = """
[tool.pyfltr.custom-commands.bad-linter]
type = "linter"
fix-args = "--fix"
"""
    (tmp_path / "pyproject.toml").write_text(pyproject_content)
    with pytest.raises(ValueError, match="fix-args"):
        pyfltr.config.config.load_config(config_dir=tmp_path)


def test_severity_default_is_error() -> None:
    """severityの既定値は "error" で、ビルトイン全コマンドに登録されている。"""
    config = pyfltr.config.config.create_default_config()
    for name in pyfltr.config.config.BUILTIN_COMMAND_NAMES:
        assert config.values[f"{name}-severity"] == "error", f"{name}-severity既定値"
    assert pyfltr.config.config.resolve_severity(config.values, "mypy") == "error"


def test_severity_warning_resolved(tmp_path: pathlib.Path) -> None:
    """ビルトインコマンドのseverityをwarningに上書きできる。"""
    (tmp_path / "pyproject.toml").write_text('[tool.pyfltr]\nmypy-severity = "warning"\n')
    config = pyfltr.config.config.load_config(config_dir=tmp_path)
    assert config.values["mypy-severity"] == "warning"
    assert pyfltr.config.config.resolve_severity(config.values, "mypy") == "warning"


def test_severity_invalid_value_rejected(tmp_path: pathlib.Path) -> None:
    """severityに許容外の値を指定するとValueErrorになる。"""
    (tmp_path / "pyproject.toml").write_text('[tool.pyfltr]\nmypy-severity = "info"\n')
    with pytest.raises(ValueError, match="severity"):
        pyfltr.config.config.load_config(config_dir=tmp_path)


def test_custom_command_severity_warning(tmp_path: pathlib.Path) -> None:
    """カスタムコマンドのseverityにwarningを指定できる。"""
    pyproject_content = """
[tool.pyfltr.custom-commands.colloquial]
type = "linter"
path = "uv"
args = ["run"]
severity = "warning"
"""
    (tmp_path / "pyproject.toml").write_text(pyproject_content)
    config = pyfltr.config.config.load_config(config_dir=tmp_path)
    assert config.values["colloquial-severity"] == "warning"


def test_custom_command_severity_invalid(tmp_path: pathlib.Path) -> None:
    """カスタムコマンドのseverityに不正な値を指定するとValueErrorになる。"""
    pyproject_content = """
[tool.pyfltr.custom-commands.bad]
type = "linter"
severity = "info"
"""
    (tmp_path / "pyproject.toml").write_text(pyproject_content)
    with pytest.raises(ValueError, match="severity"):
        pyfltr.config.config.load_config(config_dir=tmp_path)


def test_hints_default_is_empty() -> None:
    """hintsの既定値は空配列で、ビルトイン全コマンドに登録されている。"""
    config = pyfltr.config.config.create_default_config()
    for name in pyfltr.config.config.BUILTIN_COMMAND_NAMES:
        assert config.values[f"{name}-hints"] == [], f"{name}-hints既定値"


def test_hints_override(tmp_path: pathlib.Path) -> None:
    """hintsを文字列リストで上書きできる。"""
    (tmp_path / "pyproject.toml").write_text('[tool.pyfltr]\nmypy-hints = ["Read mypy strict-mode docs.", "Avoid Any."]\n')
    config = pyfltr.config.config.load_config(config_dir=tmp_path)
    assert config.values["mypy-hints"] == ["Read mypy strict-mode docs.", "Avoid Any."]


def test_hints_invalid_element_rejected(tmp_path: pathlib.Path) -> None:
    """hints要素にstr以外を含めるとValueErrorになる。"""
    (tmp_path / "pyproject.toml").write_text("[tool.pyfltr]\nmypy-hints = [1, 2]\n")
    with pytest.raises(ValueError, match="hints"):
        pyfltr.config.config.load_config(config_dir=tmp_path)


def test_custom_command_hints(tmp_path: pathlib.Path) -> None:
    """カスタムコマンドのhintsを文字列リストで指定できる。"""
    pyproject_content = """
[tool.pyfltr.custom-commands.colloquial]
type = "linter"
path = "uv"
hints = ["Replace colloquial expressions.", "See SKILL.md for guidance."]
"""
    (tmp_path / "pyproject.toml").write_text(pyproject_content)
    config = pyfltr.config.config.load_config(config_dir=tmp_path)
    assert config.values["colloquial-hints"] == [
        "Replace colloquial expressions.",
        "See SKILL.md for guidance.",
    ]


def test_custom_command_hints_invalid(tmp_path: pathlib.Path) -> None:
    """カスタムコマンドのhintsが文字列以外を含むとValueErrorになる。"""
    pyproject_content = """
[tool.pyfltr.custom-commands.bad]
type = "linter"
hints = [1]
"""
    (tmp_path / "pyproject.toml").write_text(pyproject_content)
    with pytest.raises(ValueError, match="hints"):
        pyfltr.config.config.load_config(config_dir=tmp_path)


def test_custom_command_args_preserve_tilde(tmp_path: pathlib.Path) -> None:
    """カスタムコマンドの`~`を含むargs / pathはconfig読込時点では原文を保持する。

    展開はsubprocess引数組み立て直前で行うため、config.valuesには `~` のまま入る。
    `command-info` の `configured_args` / `configured_path` 等で原文が露出する。
    """
    pyproject_content = """
[tool.pyfltr.custom-commands.colloquial]
type = "linter"
path = "~/dotfiles/scripts/check.py"
args = ["--config=~/dotfiles/config.toml"]
fix-args = ["--fix=~/tmp/log"]
"""
    (tmp_path / "pyproject.toml").write_text(pyproject_content)
    config = pyfltr.config.config.load_config(config_dir=tmp_path)
    assert config.values["colloquial-path"] == "~/dotfiles/scripts/check.py"
    assert config.values["colloquial-args"] == ["--config=~/dotfiles/config.toml"]
    assert config.values["colloquial-fix-args"] == ["--fix=~/tmp/log"]


def test_vitest_args_default_contains_pass_with_no_tests() -> None:
    """vitest-argsの既定に--passWithNoTestsが含まれることのテスト。

    pyfltrがtargets設定でフィルタリングしたファイル群とプロジェクト側のvitest include
    設定が交差せず対象ゼロになるケースでrc=1→failed扱いになるのを避けるため、
    既定引数として含める方針を固定化する。
    """
    config = pyfltr.config.config.create_default_config()
    assert config["vitest-args"] == ["run", "--passWithNoTests"]


def test_js_runner_default() -> None:
    """js-runnerの既定値はpnpx（従来互換）。"""
    config = pyfltr.config.config.create_default_config()
    assert config["js-runner"] == "pnpx"


def test_js_runner_override(tmp_path: pathlib.Path) -> None:
    """pyproject.tomlでjs-runnerを上書きできる。"""
    pyproject_content = """
[tool.pyfltr]
js-runner = "pnpm"
"""
    (tmp_path / "pyproject.toml").write_text(pyproject_content)
    config = pyfltr.config.config.load_config(config_dir=tmp_path)
    assert config["js-runner"] == "pnpm"


def test_js_runner_invalid_rejected(tmp_path: pathlib.Path) -> None:
    """js-runnerに未知の値を指定するとエラーになる。"""
    pyproject_content = """
[tool.pyfltr]
js-runner = "bogus"
"""
    (tmp_path / "pyproject.toml").write_text(pyproject_content)
    with pytest.raises(ValueError, match="js-runner"):
        pyfltr.config.config.load_config(config_dir=tmp_path)


def test_textlint_packages_default() -> None:
    """textlint-packagesのデフォルトに3パッケージが含まれる。"""
    config = pyfltr.config.config.create_default_config()
    assert config["textlint-packages"] == [
        "textlint-rule-preset-ja-technical-writing",
        "textlint-rule-preset-jtf-style",
        "textlint-rule-ja-no-abusage",
    ]


def test_textlint_protected_identifiers_default() -> None:
    """textlint-protected-identifiersのデフォルトに主要な識別子が含まれる。"""
    config = pyfltr.config.config.create_default_config()
    identifiers = config["textlint-protected-identifiers"]
    assert ".NET" in identifiers
    assert "Node.js" in identifiers
    assert "Vue.js" in identifiers
    assert "Next.js" in identifiers
    assert "Nuxt.js" in identifiers


def test_textlint_protected_identifiers_override(tmp_path: pathlib.Path) -> None:
    """pyproject.tomlでtextlint-protected-identifiersを上書きできる（空リストも可）。"""
    (tmp_path / "pyproject.toml").write_text("[tool.pyfltr]\ntextlint-protected-identifiers = []\n")
    config = pyfltr.config.config.load_config(config_dir=tmp_path)
    assert config["textlint-protected-identifiers"] == []


def test_textlint_markdownlint_path_default_empty() -> None:
    """textlint-path / markdownlint-pathの既定値は空文字（runner自動解決）。"""
    config = pyfltr.config.config.create_default_config()
    assert config["textlint-path"] == ""
    assert config["markdownlint-path"] == ""
    assert config["markdownlint-args"] == []


def test_command_info_target_globs_str() -> None:
    """`CommandInfo.target_globs()`はstr targetsを単一要素リストに正規化する。"""
    info = pyfltr.config.config.CommandInfo(type="linter", targets="*.py")
    assert info.target_globs() == ["*.py"]


def test_command_info_target_globs_list() -> None:
    """`CommandInfo.target_globs()`はlist targetsをそのままコピーして返す。"""
    info = pyfltr.config.config.CommandInfo(type="linter", targets=["*.ts", "*.tsx"])
    result = info.target_globs()
    assert result == ["*.ts", "*.tsx"]
    # 元リストと切り離されている
    result.append("*.js")
    assert info.targets == ["*.ts", "*.tsx"]


def test_custom_command_targets_list(tmp_path: pathlib.Path) -> None:
    """カスタムコマンドのtargetsにlistを指定できる。"""
    pyproject_content = """
[tool.pyfltr.custom-commands.multi]
type = "linter"
path = "multi"
targets = ["*.ts", "*.tsx"]
"""
    (tmp_path / "pyproject.toml").write_text(pyproject_content)
    config = pyfltr.config.config.load_config(config_dir=tmp_path)
    assert config.commands["multi"].targets == ["*.ts", "*.tsx"]
    assert config.commands["multi"].target_globs() == ["*.ts", "*.tsx"]


def test_custom_command_targets_invalid_type(tmp_path: pathlib.Path) -> None:
    """カスタムコマンドのtargetsに不正な型を指定するとエラーになる。"""
    pyproject_content = """
[tool.pyfltr.custom-commands.bad]
type = "linter"
targets = 42
"""
    (tmp_path / "pyproject.toml").write_text(pyproject_content)
    with pytest.raises(ValueError, match="targets"):
        pyfltr.config.config.load_config(config_dir=tmp_path)


class TestConfigFilesWarning:
    """config_files未配置時の警告機構のテスト。"""

    def test_pre_commit_enabled_without_config_emits_warning(self, tmp_path: pathlib.Path) -> None:
        """pre-commitが有効で.pre-commit-config.yaml不在の場合に警告を発行する。"""
        (tmp_path / "pyproject.toml").write_text('[tool.pyfltr]\npreset = "latest"\n')
        pyfltr.config.config.load_config(config_dir=tmp_path)
        # preset=latestではtextlintなどにもconfig_filesが定義されるため複数のwarningが出る。
        # 本テストはpre-commit分が出ることだけを確認する。
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
        pyfltr.config.config.load_config(config_dir=tmp_path)
        # pre-commitのconfigは配置済みなのでpre-commit固有の警告は出ない。
        # textlint等の他ツールのconfig_files警告は関心外として除外する。
        entries = [
            w
            for w in pyfltr.warnings_.collected_warnings()
            if w["source"] == "config" and ".pre-commit-config.yaml" in w["message"]
        ]
        assert not entries

    def test_pre_commit_disabled_no_warning(self, tmp_path: pathlib.Path) -> None:
        """pre-commit無効なら設定ファイル不在でも警告は出ない。"""
        (tmp_path / "pyproject.toml").write_text("[tool.pyfltr]\npre-commit = false\n")
        pyfltr.config.config.load_config(config_dir=tmp_path)
        entries = [w for w in pyfltr.warnings_.collected_warnings() if w["source"] == "config"]
        assert not entries

    def test_custom_command_missing_config_file_emits_warning(self, tmp_path: pathlib.Path) -> None:
        """カスタムコマンドにconfig-filesを指定し不在なら警告。"""
        pyproject_content = """
[tool.pyfltr.custom-commands.mytool]
type = "linter"
path = "mytool"
config-files = [".mytoolrc"]
"""
        (tmp_path / "pyproject.toml").write_text(pyproject_content)
        pyfltr.config.config.load_config(config_dir=tmp_path)
        entries = [w for w in pyfltr.warnings_.collected_warnings() if w["source"] == "config"]
        assert len(entries) == 1
        assert "mytool" in entries[0]["message"]

    def test_custom_command_config_file_present_no_warning(self, tmp_path: pathlib.Path) -> None:
        """カスタムコマンドのconfig-filesが配置済みなら警告は出ない。"""
        pyproject_content = """
[tool.pyfltr.custom-commands.mytool]
type = "linter"
path = "mytool"
config-files = [".mytoolrc"]
"""
        (tmp_path / "pyproject.toml").write_text(pyproject_content)
        (tmp_path / ".mytoolrc").write_text("")
        pyfltr.config.config.load_config(config_dir=tmp_path)
        entries = [w for w in pyfltr.warnings_.collected_warnings() if w["source"] == "config"]
        assert not entries

    def test_config_files_glob_pattern(self, tmp_path: pathlib.Path) -> None:
        """config-filesにglobを指定し、いずれかがマッチすれば警告は出ない。"""
        pyproject_content = """
[tool.pyfltr.custom-commands.mytool]
type = "linter"
path = "mytool"
config-files = [".mytoolrc*"]
"""
        (tmp_path / "pyproject.toml").write_text(pyproject_content)
        (tmp_path / ".mytoolrc.json").write_text("{}")
        pyfltr.config.config.load_config(config_dir=tmp_path)
        entries = [w for w in pyfltr.warnings_.collected_warnings() if w["source"] == "config"]
        assert not entries

    def test_config_files_invalid_type_rejected(self, tmp_path: pathlib.Path) -> None:
        """カスタムコマンドのconfig-filesがlist[str]でなければエラー。"""
        pyproject_content = """
[tool.pyfltr.custom-commands.mytool]
type = "linter"
path = "mytool"
config-files = "foo"
"""
        (tmp_path / "pyproject.toml").write_text(pyproject_content)
        with pytest.raises(ValueError, match="config-files"):
            pyfltr.config.config.load_config(config_dir=tmp_path)


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
        pyfltr.config.config.load_config(config_dir=tmp_path)


def test_removed_preset_20250710(tmp_path: pathlib.Path) -> None:
    """preset = "20250710"はValueErrorになり、メッセージに「削除」と「latest」が含まれる。"""
    (tmp_path / "pyproject.toml").write_text('[tool.pyfltr]\npreset = "20250710"\n')
    with pytest.raises(ValueError, match="削除") as exc_info:
        pyfltr.config.config.load_config(config_dir=tmp_path)
    assert "latest" in str(exc_info.value)


@pytest.mark.parametrize("removed_tool", ["pyupgrade", "autoflake", "isort", "black", "pflake8"])
def test_removed_tool_config_key(tmp_path: pathlib.Path, removed_tool: str) -> None:
    """削除ツールの設定キーを書くとValueErrorが出て、メッセージにツール名が含まれる。"""
    (tmp_path / "pyproject.toml").write_text(f"[tool.pyfltr]\n{removed_tool} = true\n")
    with pytest.raises(ValueError, match=removed_tool):
        pyfltr.config.config.load_config(config_dir=tmp_path)


def test_archive_config_defaults() -> None:
    """アーカイブ設定の既定値テスト。"""
    config = pyfltr.config.config.create_default_config()
    assert config["archive"] is True
    assert config["archive-max-runs"] == 100
    assert config["archive-max-size-mb"] == 1024
    assert config["archive-max-age-days"] == 30


def test_jsonl_smart_truncation_defaults() -> None:
    """JSONL smart truncation設定の既定値テスト。"""
    config = pyfltr.config.config.create_default_config()
    # 既定はdiagnostic無制限（0）、メッセージは30行 / 2000文字（従来ハードコード値）。
    assert config["jsonl-diagnostic-limit"] == 0
    assert config["jsonl-message-max-lines"] == 30
    assert config["jsonl-message-max-chars"] == 2000


def test_jsonl_smart_truncation_override(tmp_path: pathlib.Path) -> None:
    """pyproject.tomlでJSONL smart truncation設定を上書きできる。"""
    pyproject_content = """
[tool.pyfltr]
jsonl-diagnostic-limit = 50
jsonl-message-max-lines = 100
jsonl-message-max-chars = 5000
"""
    (tmp_path / "pyproject.toml").write_text(pyproject_content)
    config = pyfltr.config.config.load_config(config_dir=tmp_path)
    assert config["jsonl-diagnostic-limit"] == 50
    assert config["jsonl-message-max-lines"] == 100
    assert config["jsonl-message-max-chars"] == 5000


def test_jsonl_smart_truncation_invalid_type(tmp_path: pathlib.Path) -> None:
    """JSONL smart truncationキーに整数以外を指定するとエラーになる。"""
    (tmp_path / "pyproject.toml").write_text('[tool.pyfltr]\njsonl-diagnostic-limit = "many"\n')
    with pytest.raises(ValueError, match="jsonl-diagnostic-limit"):
        pyfltr.config.config.load_config(config_dir=tmp_path)


def test_archive_config_override(tmp_path: pathlib.Path) -> None:
    """pyproject.tomlでアーカイブ設定を上書きできることのテスト。"""
    pyproject_content = """
[tool.pyfltr]
archive = false
archive-max-runs = 50
archive-max-size-mb = 512
archive-max-age-days = 7
"""
    (tmp_path / "pyproject.toml").write_text(pyproject_content)
    config = pyfltr.config.config.load_config(config_dir=tmp_path)
    assert config["archive"] is False
    assert config["archive-max-runs"] == 50
    assert config["archive-max-size-mb"] == 512
    assert config["archive-max-age-days"] == 7


def test_respect_gitignore_default() -> None:
    """respect-gitignoreの既定値がTrueであることを確認する。"""
    config = pyfltr.config.config.create_default_config()
    assert config["respect-gitignore"] is True


def test_cache_config_defaults() -> None:
    """ファイルhashキャッシュ設定の既定値テスト。"""
    config = pyfltr.config.config.create_default_config()
    assert config["cache"] is True
    assert config["cache-max-age-hours"] == 12


def test_cache_config_override(tmp_path: pathlib.Path) -> None:
    """pyproject.tomlでキャッシュ設定を上書きできる。"""
    pyproject_content = """
[tool.pyfltr]
cache = false
cache-max-age-hours = 24
"""
    (tmp_path / "pyproject.toml").write_text(pyproject_content)
    config = pyfltr.config.config.load_config(config_dir=tmp_path)
    assert config["cache"] is False
    assert config["cache-max-age-hours"] == 24


def test_cache_config_invalid_type(tmp_path: pathlib.Path) -> None:
    """cache-max-age-hoursに整数以外を指定するとエラーになる。"""
    (tmp_path / "pyproject.toml").write_text('[tool.pyfltr]\ncache-max-age-hours = "many"\n')
    with pytest.raises(ValueError, match="cache-max-age-hours"):
        pyfltr.config.config.load_config(config_dir=tmp_path)


def test_textlint_command_info_is_cacheable() -> None:
    """textlintのCommandInfoがcacheable=Trueでconfig_filesを完全列挙している。"""
    config = pyfltr.config.config.create_default_config()
    info = config.commands["textlint"]
    assert info.cacheable is True
    # textlintの公式設定ファイル候補が全て列挙されている
    assert ".textlintrc" in info.config_files
    assert ".textlintrc.json" in info.config_files
    assert ".textlintrc.yml" in info.config_files
    assert ".textlintrc.yaml" in info.config_files
    assert ".textlintrc.js" in info.config_files
    assert ".textlintrc.cjs" in info.config_files
    assert "package.json" in info.config_files
    assert ".textlintignore" in info.config_files


def test_non_cacheable_commands_remain_default() -> None:
    """textlint以外のビルトインコマンドはcacheable=False（既定）のまま。"""
    config = pyfltr.config.config.create_default_config()
    for name, info in config.commands.items():
        if name == "textlint":
            continue
        assert info.cacheable is False, f"{name}が意図せずcacheable=Trueになっている"


def test_auto_option_defaults() -> None:
    """自動オプションの既定値テスト。"""
    config = pyfltr.config.config.create_default_config()
    assert config["pylint-pydantic"] is True
    assert config["mypy-unused-awaitable"] is True


def test_auto_option_disable(tmp_path: pathlib.Path) -> None:
    """自動オプションをFalseに設定できる。"""
    pyproject_content = """
[tool.pyfltr]
pylint-pydantic = false
mypy-unused-awaitable = false
"""
    (tmp_path / "pyproject.toml").write_text(pyproject_content)
    config = pyfltr.config.config.load_config(config_dir=tmp_path)
    assert config["pylint-pydantic"] is False
    assert config["mypy-unused-awaitable"] is False


def test_python_default() -> None:
    """pythonの既定値はFalse（opt-in）。"""
    config = pyfltr.config.config.create_default_config()
    assert config["python"] is False


def test_python_default_disables_python_tools() -> None:
    """既定でPython系ツールが全て無効化されている。"""
    config = pyfltr.config.config.create_default_config()
    _assert_language_gate(config, "python", passed=False)
    # JS/共通系も影響を受けない
    assert config["markdownlint"] is False
    assert config["textlint"] is False


def test_python_true_without_preset_enables_nothing(tmp_path: pathlib.Path) -> None:
    """python = true単独では何も有効化されない（presetがgateを通過する対象を決める）。"""
    pyproject_content = """
[tool.pyfltr]
python = true
"""
    (tmp_path / "pyproject.toml").write_text(pyproject_content)
    config = pyfltr.config.config.load_config(config_dir=tmp_path)
    for cmd in pyfltr.config.config.PYTHON_COMMANDS:
        assert config[cmd] is False, f"{cmd}はpreset未指定ではFalseのまま"
    # docs系もpreset未指定なのでFalse
    assert config["markdownlint"] is False
    assert config["textlint"] is False


def test_python_true_with_preset_latest(tmp_path: pathlib.Path) -> None:
    """preset = latest + python = trueでpreset内のPython系推奨構成がgateを通過する。"""
    pyproject_content = """
[tool.pyfltr]
preset = "latest"
python = true
"""
    (tmp_path / "pyproject.toml").write_text(pyproject_content)
    config = pyfltr.config.config.load_config(config_dir=tmp_path)
    # preset = latest（= 20260413）に含まれるPython系推奨ツールが一式True
    assert config["ruff-format"] is True
    assert config["ruff-check"] is True
    assert config["mypy"] is True
    assert config["pylint"] is True
    assert config["pyright"] is True
    assert config["pytest"] is True
    assert config["uv-sort"] is True
    # tyはpreset非収録のため個別指定が必要
    assert config["ty"] is False
    # presetが有効化したドキュメント系ツールも有効
    assert config["textlint"] is True
    assert config["markdownlint"] is True


def test_python_true_with_individual_extra(tmp_path: pathlib.Path) -> None:
    """preset = latest + python = true + ty = trueでpreset非収録のtyも有効化される。"""
    pyproject_content = """
[tool.pyfltr]
preset = "latest"
python = true
ty = true
"""
    (tmp_path / "pyproject.toml").write_text(pyproject_content)
    config = pyfltr.config.config.load_config(config_dir=tmp_path)
    # 個別指定でtyがTrue
    assert config["ty"] is True
    # preset内のPython系も一式有効
    assert config["mypy"] is True
    assert config["pyright"] is True
    assert config["ruff-format"] is True


def test_python_true_with_individual_override(tmp_path: pathlib.Path) -> None:
    """preset + python = trueでも個別`{command} = false`で上書きできる。"""
    pyproject_content = """
[tool.pyfltr]
preset = "latest"
python = true
ruff-check = false
mypy = false
"""
    (tmp_path / "pyproject.toml").write_text(pyproject_content)
    config = pyfltr.config.config.load_config(config_dir=tmp_path)
    # 個別にFalse指定したものはFalse
    assert config["ruff-check"] is False
    assert config["mypy"] is False
    # 他のpreset内Python系は有効
    assert config["ruff-format"] is True
    assert config["pyright"] is True
    assert config["pylint"] is True


def test_individual_tool_crosses_gate(tmp_path: pathlib.Path) -> None:
    """言語カテゴリがFalseでも個別`{command} = true`はgateを越えて有効化される。"""
    pyproject_content = """
[tool.pyfltr]
preset = "latest"
mypy = true
"""
    (tmp_path / "pyproject.toml").write_text(pyproject_content)
    config = pyfltr.config.config.load_config(config_dir=tmp_path)
    # python = falseでもmypyだけ個別指定でTrue
    assert config["mypy"] is True
    # preset由来のPython系はgateによりFalseに上書きされる
    assert config["ruff-format"] is False
    assert config["ruff-check"] is False
    assert config["pylint"] is False
    assert config["pyright"] is False
    assert config["pytest"] is False


def test_bin_runner_default() -> None:
    """bin-runnerの既定値はmise。"""
    config = pyfltr.config.config.create_default_config()
    assert config["bin-runner"] == "mise"


def test_bin_runner_override(tmp_path: pathlib.Path) -> None:
    """pyproject.tomlでbin-runnerを上書きできる。"""
    pyproject_content = """
[tool.pyfltr]
bin-runner = "direct"
"""
    (tmp_path / "pyproject.toml").write_text(pyproject_content)
    config = pyfltr.config.config.load_config(config_dir=tmp_path)
    assert config["bin-runner"] == "direct"


def test_bin_runner_invalid_rejected(tmp_path: pathlib.Path) -> None:
    """bin-runnerに未知の値を指定するとエラーになる。"""
    pyproject_content = """
[tool.pyfltr]
bin-runner = "bogus"
"""
    (tmp_path / "pyproject.toml").write_text(pyproject_content)
    with pytest.raises(ValueError, match="bin-runner"):
        pyfltr.config.config.load_config(config_dir=tmp_path)


def test_command_runner_validation_accepts_uv_value(tmp_path: pathlib.Path) -> None:
    """`mypy-runner = "uv"` はエラーにならず読み込める（直接指定値として後方互換維持）。"""
    pyproject_content = """
[tool.pyfltr]
mypy-runner = "uv"
"""
    (tmp_path / "pyproject.toml").write_text(pyproject_content)
    config = pyfltr.config.config.load_config(config_dir=tmp_path)
    assert config["mypy-runner"] == "uv"


def test_python_runner_default() -> None:
    """python-runnerの既定値はuv（従来互換）。"""
    config = pyfltr.config.config.create_default_config()
    assert config["python-runner"] == "uv"


def test_python_runner_override(tmp_path: pathlib.Path) -> None:
    """pyproject.tomlでpython-runnerを上書きできる。"""
    pyproject_content = """
[tool.pyfltr]
python-runner = "uvx"
"""
    (tmp_path / "pyproject.toml").write_text(pyproject_content)
    config = pyfltr.config.config.load_config(config_dir=tmp_path)
    assert config["python-runner"] == "uvx"


def test_python_runner_invalid_rejected(tmp_path: pathlib.Path) -> None:
    """python-runnerに未知の値を指定するとエラーになる。"""
    pyproject_content = """
[tool.pyfltr]
python-runner = "bogus"
"""
    (tmp_path / "pyproject.toml").write_text(pyproject_content)
    with pytest.raises(ValueError, match="python-runner"):
        pyfltr.config.config.load_config(config_dir=tmp_path)


@pytest.mark.parametrize(
    "value",
    ["python-runner", "js-runner", "bin-runner", "direct", "mise", "uv", "uvx", "pnpx", "pnpm", "npm", "npx", "yarn"],
)
def test_command_runner_validation_accepts_symmetric_12_values(tmp_path: pathlib.Path, value: str) -> None:
    """{command}-runnerは対称12値（カテゴリ委譲3値＋直接指定9値）すべてを受理する。

    カテゴリ横断の組み合わせ（例: Python系ツールに`pnpm`を指定）はバリデーションでは拒否しない方針で、
    実装簡潔さを優先する（無意味な組み合わせは実行時の解決ロジックがエラー終了する）。
    """
    (tmp_path / "pyproject.toml").write_text(f'[tool.pyfltr]\nmypy-runner = "{value}"\n')
    config = pyfltr.config.config.load_config(config_dir=tmp_path)
    assert config["mypy-runner"] == value


def test_mise_auto_trust_default() -> None:
    """mise-auto-trustの既定値はTrue。"""
    config = pyfltr.config.config.create_default_config()
    assert config["mise-auto-trust"] is True


def test_mise_auto_trust_disable(tmp_path: pathlib.Path) -> None:
    """pyproject.tomlでmise-auto-trustをFalseに設定できる。"""
    (tmp_path / "pyproject.toml").write_text("[tool.pyfltr]\nmise-auto-trust = false\n")
    config = pyfltr.config.config.load_config(config_dir=tmp_path)
    assert config["mise-auto-trust"] is False


def test_mise_auto_trust_invalid_type_rejected(tmp_path: pathlib.Path) -> None:
    """mise-auto-trustにbool以外の値を指定するとエラーになる。"""
    (tmp_path / "pyproject.toml").write_text('[tool.pyfltr]\nmise-auto-trust = "yes"\n')
    with pytest.raises(ValueError, match="mise-auto-trust"):
        pyfltr.config.config.load_config(config_dir=tmp_path)


def test_preset_latest_suppresses_language_categories(tmp_path: pathlib.Path) -> None:
    """preset = "latest"単独（カテゴリキー全False）では全言語のツールがFalseに上書きされる。"""
    (tmp_path / "pyproject.toml").write_text('[tool.pyfltr]\npreset = "latest"\n')
    config = pyfltr.config.config.load_config(config_dir=tmp_path)
    # presetに含まれるドキュメント系はTrue
    for cmd in _DOCS_ORTHOGONAL_KEYS:
        assert config[cmd] is True, f"{cmd}はpreset=latestで有効化されるべき"
    # 言語カテゴリに属するツールはgateにより全てFalseに上書きされる
    # （_PRESET_BASEでTrueだったPython核 / JS / Rust / .NETも含む）
    for category_key, _ in pyfltr.config.config.LANGUAGE_CATEGORIES:
        _assert_language_gate(config, category_key, passed=False)


def test_javascript_true_enables_preset_tools(tmp_path: pathlib.Path) -> None:
    """javascript = trueでpreset内のJS/TS系推奨ツール一式がgate通過で有効化される。"""
    (tmp_path / "pyproject.toml").write_text('[tool.pyfltr]\npreset = "latest"\njavascript = true\n')
    config = pyfltr.config.config.load_config(config_dir=tmp_path)
    _assert_language_gate(config, "javascript", passed=True)
    # 他言語カテゴリはgate閉のままFalse
    _assert_language_gate(config, "python", passed=False)
    _assert_language_gate(config, "rust", passed=False)
    _assert_language_gate(config, "dotnet", passed=False)


def test_javascript_true_with_individual_override(tmp_path: pathlib.Path) -> None:
    """javascript = trueでも個別`{command} = false`でpreset由来Trueを無効化できる。"""
    pyproject_content = """
[tool.pyfltr]
preset = "latest"
javascript = true
prettier = false
"""
    (tmp_path / "pyproject.toml").write_text(pyproject_content)
    config = pyfltr.config.config.load_config(config_dir=tmp_path)
    # 個別にFalse指定したprettierだけFalse
    assert config["prettier"] is False
    # 他のJS/TS系はgate通過でTrueのまま
    assert config["eslint"] is True
    assert config["biome"] is True
    assert config["tsc"] is True


def test_rust_true_enables_preset_tools(tmp_path: pathlib.Path) -> None:
    """rust = trueでpreset内のRust系推奨ツール一式がgate通過で有効化される。"""
    (tmp_path / "pyproject.toml").write_text('[tool.pyfltr]\npreset = "latest"\nrust = true\n')
    config = pyfltr.config.config.load_config(config_dir=tmp_path)
    _assert_language_gate(config, "rust", passed=True)


def test_dotnet_true_enables_preset_tools(tmp_path: pathlib.Path) -> None:
    """dotnet = trueでpreset内の.NET系推奨ツール一式がgate通過で有効化される。"""
    (tmp_path / "pyproject.toml").write_text('[tool.pyfltr]\npreset = "latest"\ndotnet = true\n')
    config = pyfltr.config.config.load_config(config_dir=tmp_path)
    _assert_language_gate(config, "dotnet", passed=True)


def test_individual_tool_enables_despite_category_false(tmp_path: pathlib.Path) -> None:
    """言語カテゴリがFalseでも個別`{tool} = true`でそのツールだけ有効化される。"""
    pyproject_content = """
[tool.pyfltr]
preset = "latest"
eslint = true
cargo-fmt = true
"""
    (tmp_path / "pyproject.toml").write_text(pyproject_content)
    config = pyfltr.config.config.load_config(config_dir=tmp_path)
    # 個別にTrueにしたツールは有効
    assert config["eslint"] is True
    assert config["cargo-fmt"] is True
    # 同カテゴリの他ツールはgateでFalseに上書きされる
    assert config["prettier"] is False
    assert config["biome"] is False
    assert config["cargo-clippy"] is False


def test_language_categories_defaults_false() -> None:
    """言語カテゴリキーの既定値は全てFalse。"""
    config = pyfltr.config.config.create_default_config()
    for category_key, _ in pyfltr.config.config.LANGUAGE_CATEGORIES:
        assert config[category_key] is False, f"{category_key}の既定値はFalseであるべき"


def test_custom_command_pass_filenames(tmp_path: pathlib.Path) -> None:
    """カスタムコマンドのpass-filenames設定が登録される。"""
    pyproject_content = """
[tool.pyfltr.custom-commands.my-checker]
type = "linter"
path = "my-checker"
pass-filenames = false
"""
    (tmp_path / "pyproject.toml").write_text(pyproject_content)
    config = pyfltr.config.config.load_config(config_dir=tmp_path)
    assert config["my-checker-pass-filenames"] is False


def test_custom_command_pass_filenames_default(tmp_path: pathlib.Path) -> None:
    """カスタムコマンドのpass-filenamesの既定値はTrue。"""
    pyproject_content = """
[tool.pyfltr.custom-commands.my-checker]
type = "linter"
path = "my-checker"
"""
    (tmp_path / "pyproject.toml").write_text(pyproject_content)
    config = pyfltr.config.config.load_config(config_dir=tmp_path)
    assert config["my-checker-pass-filenames"] is True


def test_bin_tool_default_config_values() -> None:
    """bin-runner対応ツールのデフォルト設定値が正しく定義されている。"""
    config = pyfltr.config.config.create_default_config()
    # bin-runner経由ツールの有効/無効とバージョン設定を確認（fast=True系列）
    bin_tools = ["ec", "shellcheck", "shfmt", "actionlint", "taplo", "hadolint"]
    for tool in bin_tools:
        assert config[tool] is False, f"{tool}は既定で無効"
        assert config[f"{tool}-path"] == "", f"{tool}-pathは空文字"
        assert config[f"{tool}-version"] == "latest", f"{tool}-versionはlatest"
        assert config[f"{tool}-fast"] is True, f"{tool}-fastはTrue"

    # uv-sortの既定値
    assert config["uv-sort"] is False
    assert config["uv-sort-path"] == ""
    assert config["uv-sort-runner"] == "python-runner"
    assert config["uv-sort-fast"] is True

    # tscのpass-filenames
    assert config["tsc-pass-filenames"] is False


def test_gitleaks_default_config_values() -> None:
    """gitleaksは既定で無効、pass-filenames=falseでリポジトリ全体を対象とする。"""
    config = pyfltr.config.config.create_default_config()
    assert config["gitleaks"] is False, "gitleaksは既定で無効"
    assert config["gitleaks-path"] == "", "gitleaks-pathは空文字"
    assert config["gitleaks-args"] == ["detect", "--no-banner"], "gitleaks-argsはdetect --no-banner"
    assert config["gitleaks-pass-filenames"] is False, "gitleaks-pass-filenamesはFalse"
    assert config["gitleaks-version"] == "latest", "gitleaks-versionはlatest"
    assert config["gitleaks-fast"] is False, "gitleaks-fastはFalse"
    info = pyfltr.config.config.BUILTIN_COMMANDS["gitleaks"]
    assert info.type == "linter"


def test_yamllint_default_config_values() -> None:
    """yamllintは既定で無効（opt-in）、直接実行経路で`{command}-path`は空文字列契約に揃える。"""
    config = pyfltr.config.config.create_default_config()
    assert config["yamllint"] is False, "yamllintは既定で無効"
    assert config["yamllint-path"] == "", "yamllint-pathは空文字列契約に揃える"
    assert config["yamllint-args"] == [], "yamllint-argsは空リスト"
    assert config["yamllint-fast"] is True, "yamllint-fastはTrue"
    info = pyfltr.config.config.BUILTIN_COMMANDS["yamllint"]
    assert info.type == "linter"
    assert info.target_globs() == ["*.yaml", "*.yml"]


def test_typos_default_config_values() -> None:
    """typosはPyPI依存として直接実行するため、pathは空文字列契約に揃え未登録ツール経路で解決する。"""
    config = pyfltr.config.config.create_default_config()
    assert config["typos"] is False, "typosは既定で無効"
    assert config["typos-path"] == "", "typos-pathは空文字列契約に揃える"
    # typos-versionは既存ユーザーの設定との互換維持のため定義を残す
    assert config["typos-version"] == "latest", "typos-versionはlatest（互換維持）"
    assert config["typos-fast"] is True, "typos-fastはTrue"


def test_uv_sort_in_python_commands() -> None:
    """uv-sortがPYTHON_COMMANDSに含まれる。"""
    assert "uv-sort" in pyfltr.config.config.PYTHON_COMMANDS


def test_bin_runners_tuple() -> None:
    """`BIN_RUNNERS`にdirectとmiseが含まれる。"""
    assert "direct" in pyfltr.config.config.BIN_RUNNERS
    assert "mise" in pyfltr.config.config.BIN_RUNNERS


def test_glab_ci_lint_default_config_values() -> None:
    """glab-ci-lintは既定で無効（opt-in）、args既定値に`ci lint`サブコマンドが入る。"""
    config = pyfltr.config.config.create_default_config()
    assert config["glab-ci-lint"] is False, "GitLab API 認証必須のため opt-in"
    assert config["glab-ci-lint-path"] == ""
    assert config["glab-ci-lint-args"] == ["ci", "lint"]
    assert config["glab-ci-lint-version"] == "latest"
    assert config["glab-ci-lint-fast"] is False
    info = pyfltr.config.config.BUILTIN_COMMANDS["glab-ci-lint"]
    assert info.type == "linter"
    assert info.target_globs() == [".gitlab-ci.yml"]


def test_builtin_targets_override_str(tmp_path: pathlib.Path) -> None:
    """ビルトインコマンドのtargetsを文字列で完全上書きできる。"""
    (tmp_path / "pyproject.toml").write_text('[tool.pyfltr]\nshfmt-targets = "*.bash"\n')
    config = pyfltr.config.config.load_config(config_dir=tmp_path)
    assert config.commands["shfmt"].targets == "*.bash"
    assert config.commands["shfmt"].target_globs() == ["*.bash"]


def test_builtin_targets_override_list(tmp_path: pathlib.Path) -> None:
    """ビルトインコマンドのtargetsをリストで完全上書きできる。"""
    (tmp_path / "pyproject.toml").write_text('[tool.pyfltr]\nshfmt-targets = ["*.sh", "*.bash"]\n')
    config = pyfltr.config.config.load_config(config_dir=tmp_path)
    assert config.commands["shfmt"].targets == ["*.sh", "*.bash"]
    assert config.commands["shfmt"].target_globs() == ["*.sh", "*.bash"]


def test_builtin_extend_targets_str(tmp_path: pathlib.Path) -> None:
    """ビルトインコマンドのtargetsに文字列で追加できる。"""
    (tmp_path / "pyproject.toml").write_text('[tool.pyfltr]\nshfmt-extend-targets = "*.bash"\n')
    config = pyfltr.config.config.load_config(config_dir=tmp_path)
    # デフォルトの"*.sh"に"*.bash"が追加される
    assert config.commands["shfmt"].target_globs() == ["*.sh", "*.bash"]


def test_builtin_extend_targets_list(tmp_path: pathlib.Path) -> None:
    """ビルトインコマンドのtargetsにリストで追加できる。"""
    (tmp_path / "pyproject.toml").write_text('[tool.pyfltr]\nshfmt-extend-targets = ["*.bash", "dot_bashrc"]\n')
    config = pyfltr.config.config.load_config(config_dir=tmp_path)
    assert config.commands["shfmt"].target_globs() == [
        "*.sh",
        "*.bash",
        "dot_bashrc",
    ]


def test_builtin_targets_and_extend_targets(tmp_path: pathlib.Path) -> None:
    """targetsで上書き後にextend-targetsで追加される。"""
    (tmp_path / "pyproject.toml").write_text(
        '[tool.pyfltr]\nshfmt-targets = ["*.bash"]\nshfmt-extend-targets = ["dot_bashrc"]\n'
    )
    config = pyfltr.config.config.load_config(config_dir=tmp_path)
    assert config.commands["shfmt"].target_globs() == ["*.bash", "dot_bashrc"]


def test_builtin_targets_invalid_type(tmp_path: pathlib.Path) -> None:
    """ビルトインコマンドのtargetsに不正な型を指定するとエラーになる。"""
    (tmp_path / "pyproject.toml").write_text("[tool.pyfltr]\nshfmt-targets = 42\n")
    with pytest.raises(ValueError, match="targets"):
        pyfltr.config.config.load_config(config_dir=tmp_path)


def test_builtin_targets_unknown_command(tmp_path: pathlib.Path) -> None:
    """未知のコマンド名のtargets指定はエラーになる。"""
    (tmp_path / "pyproject.toml").write_text('[tool.pyfltr]\nunknown-targets = "*.py"\n')
    with pytest.raises(ValueError, match="設定キーが不正です"):
        pyfltr.config.config.load_config(config_dir=tmp_path)


def test_builtin_targets_no_mutation_of_builtins(tmp_path: pathlib.Path) -> None:
    """targets上書きでBUILTIN_COMMANDSが汚染されない。"""
    original_targets = pyfltr.config.config.BUILTIN_COMMANDS["shfmt"].targets
    (tmp_path / "pyproject.toml").write_text('[tool.pyfltr]\nshfmt-targets = "*.bash"\n')
    config = pyfltr.config.config.load_config(config_dir=tmp_path)
    assert config.commands["shfmt"].targets == "*.bash"
    # BUILTIN_COMMANDS側は元のまま
    assert pyfltr.config.config.BUILTIN_COMMANDS["shfmt"].targets == original_targets


# Rust / .NET言語ツール向けのテスト群。
# 全ツール既定False、pass-filenames=False、formatterは常時書き込みモード、
# cargo-clippyのみlint-args / fix-argsを持つ。
# pylint duplicate-code（R0801）を避けるため、config側の定義をそのまま再利用する。
_NATIVE_LANG_TOOLS: tuple[str, ...] = pyfltr.config.config.RUST_COMMANDS + pyfltr.config.config.DOTNET_COMMANDS


def test_native_lang_tools_registered() -> None:
    """Rust / .NET言語ツールがBUILTIN_COMMANDSとDEFAULT_CONFIGに登録されている。"""
    config = pyfltr.config.config.create_default_config()
    for tool in _NATIVE_LANG_TOOLS:
        assert tool in pyfltr.config.config.BUILTIN_COMMANDS, f"{tool}がBUILTIN_COMMANDSに未登録"
        assert config[tool] is False, f"{tool}の既定値はFalseであるべき"
        # cargo系・dotnet系はbin-runner経由で起動する設計のため、path既定値は空文字。
        # 起動方式は{command}-runner（既定"bin-runner"）→グローバルbin-runner（既定"mise"）で解決する。
        assert config[f"{tool}-path"] == "", f"{tool}-pathは既定で空文字であるべき"
        assert config[f"{tool}-runner"] == "bin-runner", f"{tool}-runnerの既定値は'bin-runner'であるべき"
        assert config[f"{tool}-version"] == "latest", f"{tool}-versionの既定値は'latest'であるべき"


def test_native_lang_tools_pass_filenames_false() -> None:
    """Rust / .NET言語ツールは全てpass-filenames=False（crate / solution全体を対象）。"""
    config = pyfltr.config.config.create_default_config()
    for tool in _NATIVE_LANG_TOOLS:
        assert config[f"{tool}-pass-filenames"] is False, f"{tool}-pass-filenamesはFalseであるべき"


def test_native_lang_tools_command_types() -> None:
    """Rust / .NET言語ツールのtype分類。"""
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
        assert pyfltr.config.config.BUILTIN_COMMANDS[tool].type == expected_type


def test_native_formatters_write_by_default() -> None:
    """cargo-fmt / dotnet-formatは既定で書き込みモード（--check等を含まない）。"""
    config = pyfltr.config.config.create_default_config()
    assert config["cargo-fmt-args"] == ["fmt"]
    assert config["dotnet-format-args"] == ["format"]
    # pyfltr規約: formatterにはfix-argsを定義しない
    assert "cargo-fmt-fix-args" not in config.values
    assert "dotnet-format-fix-args" not in config.values


def test_cargo_clippy_args_separation() -> None:
    """cargo-clippyはargs / lint-args / fix-argsを分離し、trailing `-- -D warnings`を双方に持つ。"""
    config = pyfltr.config.config.create_default_config()
    assert config["cargo-clippy-args"] == _testconf.CARGO_CLIPPY_ARGS
    assert config["cargo-clippy-lint-args"] == _testconf.CARGO_CLIPPY_LINT_ARGS
    assert config["cargo-clippy-fix-args"] == _testconf.CARGO_CLIPPY_FIX_ARGS


def test_native_lang_tools_fast_defaults() -> None:
    """fast既定値はcargo-fmt / cargo-clippy / dotnet-formatのみTrue。"""
    config = pyfltr.config.config.create_default_config()
    assert config["cargo-fmt-fast"] is True
    assert config["cargo-clippy-fast"] is True
    assert config["dotnet-format-fast"] is True
    for tool in ("cargo-check", "cargo-test", "cargo-deny", "dotnet-build", "dotnet-test"):
        assert config[f"{tool}-fast"] is False, f"{tool}-fastは既定Falseであるべき"


def test_native_lang_tools_not_affected_by_python(tmp_path: pathlib.Path) -> None:
    """python設定はRust / .NET言語ツールの設定を変更しない。"""
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
    config = pyfltr.config.config.load_config(config_dir=tmp_path)
    assert config["cargo-fmt"] is True
    assert config["cargo-clippy"] is True
    assert config["dotnet-format"] is True
    # python系ツールは個別指定で有効化されている
    assert config["mypy"] is True
    assert config["pytest"] is True


def test_native_lang_tools_serial_group() -> None:
    """cargo系はserial_group=cargo、dotnet系はserial_group=dotnetに設定される。"""
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
        assert pyfltr.config.config.BUILTIN_COMMANDS[tool].serial_group == group, f"{tool}.serial_groupは{group!r}であるべき"


def test_existing_tools_have_no_serial_group() -> None:
    """既存ツールはserial_group未設定（後方互換）。"""
    for name, info in pyfltr.config.config.BUILTIN_COMMANDS.items():
        if name.startswith(("cargo-", "dotnet-")):
            continue
        assert info.serial_group is None, f"{name}.serial_groupはNoneであるべき"


def test_native_lang_tools_in_aliases() -> None:
    """Rust / .NET言語ツールがformat / lint / testの各エイリアスに含まれる。"""
    config = pyfltr.config.config.create_default_config()
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
    """`{tool}-exclude`がpyproject.tomlから読み込まれてconfig.valuesに格納される。"""
    (tmp_path / "pyproject.toml").write_text('[tool.pyfltr]\nmypy-exclude = ["vendor", "gen_*.py"]\n')
    config = pyfltr.config.config.load_config(config_dir=tmp_path)
    assert config.values["mypy-exclude"] == ["vendor", "gen_*.py"]


def test_tool_exclude_unknown_command(tmp_path: pathlib.Path) -> None:
    """未知のコマンド名の`{tool}-exclude`指定はエラーになる。"""
    (tmp_path / "pyproject.toml").write_text('[tool.pyfltr]\nunknown-exclude = ["foo"]\n')
    with pytest.raises(ValueError, match="設定キーが不正です"):
        pyfltr.config.config.load_config(config_dir=tmp_path)


def test_tool_exclude_invalid_type(tmp_path: pathlib.Path) -> None:
    """`{tool}-exclude`に文字列リスト以外を指定するとエラーになる。"""
    (tmp_path / "pyproject.toml").write_text("[tool.pyfltr]\nmypy-exclude = 42\n")
    with pytest.raises(ValueError, match="str型のリスト"):
        pyfltr.config.config.load_config(config_dir=tmp_path)


# --- グローバル設定（XDG準拠 + archive/cache global優先） ---


class TestGlobalConfig:
    """ユーザーレベルglobal設定ファイルのテスト群。

    `~/.config/pyfltr/config.toml`を模した一時パスを`global_config_path=`で指定し、
    project側`pyproject.toml`との読み込み・マージ挙動を検証する。
    `_isolate_global_config`fixture（autouse）によりPYFLTR_GLOBAL_CONFIGは
    既にtmp配下のダミーパスへ固定されているため、本テスト群は独立したglobal_pathを
    `load_config(... , global_config_path=...)`で明示する。
    """

    @staticmethod
    def _setup(
        tmp_path: pathlib.Path,
        *,
        global_text: str | None = None,
        project_text: str | None = None,
    ) -> tuple[pathlib.Path, pathlib.Path]:
        """global設定とproject設定の一時ファイルを配置するヘルパー。"""
        global_path = tmp_path / "global_config.toml"
        if global_text is not None:
            global_path.write_text(global_text, encoding="utf-8")
        project_dir = tmp_path / "project"
        project_dir.mkdir()
        if project_text is not None:
            (project_dir / "pyproject.toml").write_text(project_text, encoding="utf-8")
        return global_path, project_dir

    def test_global_only_archive_key_applies(self, tmp_path: pathlib.Path) -> None:
        """globalのみarchive-max-age-daysが書かれているとき、値が反映される。"""
        global_path, project_dir = self._setup(
            tmp_path,
            global_text="[tool.pyfltr]\narchive-max-age-days = 7\n",
            project_text="[tool.pyfltr]\n",
        )
        config = pyfltr.config.config.load_config(config_dir=project_dir, global_config_path=global_path)
        assert config["archive-max-age-days"] == 7

    def test_global_wins_archive_with_warning(self, tmp_path: pathlib.Path) -> None:
        """globalとproject両方にarchive-max-age-daysがあるとき、global値が勝ち警告が出る。"""
        global_path, project_dir = self._setup(
            tmp_path,
            global_text="[tool.pyfltr]\narchive-max-age-days = 7\n",
            project_text="[tool.pyfltr]\narchive-max-age-days = 14\n",
        )
        config = pyfltr.config.config.load_config(config_dir=project_dir, global_config_path=global_path)
        assert config["archive-max-age-days"] == 7
        assert _count_config_warnings("archive-max-age-days") == 1

    def test_project_wins_normal_key_no_warning(self, tmp_path: pathlib.Path) -> None:
        """globalとproject両方にarchive/cache以外の同じキーがあるとき、project値が勝ち警告は出ない。"""
        global_path, project_dir = self._setup(
            tmp_path,
            global_text='[tool.pyfltr]\njs-runner = "pnpm"\n',
            project_text='[tool.pyfltr]\njs-runner = "npm"\n',
        )
        config = pyfltr.config.config.load_config(config_dir=project_dir, global_config_path=global_path)
        assert config["js-runner"] == "npm"
        assert _count_config_warnings("") == 0

    def test_global_config_missing_path_acts_as_empty(self, tmp_path: pathlib.Path) -> None:
        """global設定ファイルが存在しないパスを指したとき、project側のみが反映される。"""
        global_path, project_dir = self._setup(
            tmp_path,
            project_text='[tool.pyfltr]\njs-runner = "npm"\n',
        )
        # global_pathは存在しないファイル
        assert not global_path.exists()
        config = pyfltr.config.config.load_config(config_dir=project_dir, global_config_path=global_path)
        assert config["js-runner"] == "npm"

    def test_global_config_invalid_toml_raises(self, tmp_path: pathlib.Path) -> None:
        """global設定ファイルのTOMLが破損しているときValueErrorで停止する。"""
        global_path, project_dir = self._setup(
            tmp_path,
            global_text="[tool.pyfltr\n",  # 閉じ括弧なし
            project_text="[tool.pyfltr]\n",
        )
        with pytest.raises(ValueError, match="TOML"):
            pyfltr.config.config.load_config(config_dir=project_dir, global_config_path=global_path)

    def test_global_preset_applied(self, tmp_path: pathlib.Path) -> None:
        """global側にpreset = "latest"を書いたとき、preset由来のコマンド有効化が反映される。"""
        global_path, project_dir = self._setup(
            tmp_path,
            global_text='[tool.pyfltr]\npreset = "latest"\n',
            project_text="[tool.pyfltr]\n",
        )
        config = pyfltr.config.config.load_config(config_dir=project_dir, global_config_path=global_path)
        # preset=latestのドキュメント系ツールが有効化されている
        assert config["textlint"] is True
        assert config["markdownlint"] is True

    def test_global_language_gate_applied(self, tmp_path: pathlib.Path) -> None:
        """global側にpreset = latest + python = trueを書いたとき、Python系ツールがgate通過で有効化される。"""
        global_path, project_dir = self._setup(
            tmp_path,
            global_text='[tool.pyfltr]\npreset = "latest"\npython = true\n',
            project_text="[tool.pyfltr]\n",
        )
        config = pyfltr.config.config.load_config(config_dir=project_dir, global_config_path=global_path)
        assert config["ruff-format"] is True
        assert config["ruff-check"] is True
        assert config["mypy"] is True

    def test_global_custom_commands_applied(self, tmp_path: pathlib.Path) -> None:
        """global側にcustom-commandsを書いたとき、カスタムコマンドが正しく登録される。"""
        global_text = """
[tool.pyfltr.custom-commands.my-tool]
type = "linter"
path = "my-tool"
targets = ["*.py"]
"""
        global_path, project_dir = self._setup(
            tmp_path,
            global_text=global_text,
            project_text="[tool.pyfltr]\n",
        )
        config = pyfltr.config.config.load_config(config_dir=project_dir, global_config_path=global_path)
        assert "my-tool" in config.commands
        assert config.commands["my-tool"].type == "linter"

    def test_global_custom_commands_with_severity_and_hints(self, tmp_path: pathlib.Path) -> None:
        """global側にseverity / hints / `~`混じりのargsを含むカスタムコマンドを記述できる。

        「カスタムコマンドにseverity・hints・~展開を追加し
        check_colloquialをchezmoiでホスト限定配布する」計画の主用途を再現する統合経路。
        """
        global_text = """
[tool.pyfltr.custom-commands.colloquial]
type = "linter"
path = "uv"
args = ["run", "--script", "~/dotfiles/agent-toolkit/skills/writing-standards/scripts/check_colloquial.py"]
targets = ["*"]
severity = "warning"
hints = [
    "Colloquial Japanese expressions detected.",
    "See SKILL.md for guidance.",
]
"""
        global_path, project_dir = self._setup(
            tmp_path,
            global_text=global_text,
            project_text="[tool.pyfltr]\n",
        )
        config = pyfltr.config.config.load_config(config_dir=project_dir, global_config_path=global_path)
        assert "colloquial" in config.commands
        assert config["colloquial"] is True
        assert config["colloquial-severity"] == "warning"
        assert config["colloquial-hints"] == [
            "Colloquial Japanese expressions detected.",
            "See SKILL.md for guidance.",
        ]
        # ~混じりargsはconfig読込時点では原文を保持する（subprocess引数組み立て直前で展開）。
        assert config["colloquial-args"] == [
            "run",
            "--script",
            "~/dotfiles/agent-toolkit/skills/writing-standards/scripts/check_colloquial.py",
        ]

    def test_global_unknown_key_warns_no_error(self, tmp_path: pathlib.Path) -> None:
        """global側に未知キーが書かれているとき、警告は出るがValueErrorにはならない（前方互換）。"""
        global_path, project_dir = self._setup(
            tmp_path,
            global_text="[tool.pyfltr]\nfuture-only-key = 1\n",
            project_text="[tool.pyfltr]\n",
        )
        config = pyfltr.config.config.load_config(config_dir=project_dir, global_config_path=global_path)
        assert "future-only-key" not in config.values
        assert _count_config_warnings("future-only-key") == 1

    def test_unknown_key_in_both_raises(self, tmp_path: pathlib.Path) -> None:
        """同じ未知キーがglobalとproject両方にあるとき、project由来扱いでValueErrorになる。"""
        global_path, project_dir = self._setup(
            tmp_path,
            global_text="[tool.pyfltr]\nfuture-only-key = 1\n",
            project_text="[tool.pyfltr]\nfuture-only-key = 2\n",
        )
        with pytest.raises(ValueError, match="future-only-key"):
            pyfltr.config.config.load_config(config_dir=project_dir, global_config_path=global_path)

    def test_no_pyproject_with_global_only(self, tmp_path: pathlib.Path) -> None:
        """pyproject.toml不在のconfig_dirでglobal設定のみが書かれているとき、global値が反映される。

        早期returnで素通りせずglobal設定が処理されることを確認する回帰テスト。
        """
        global_path = tmp_path / "global_config.toml"
        global_path.write_text("[tool.pyfltr]\narchive-max-age-days = 5\n", encoding="utf-8")
        project_dir = tmp_path / "project_no_pyproject"
        project_dir.mkdir()
        # pyproject.tomlは敢えて生成しない
        config = pyfltr.config.config.load_config(config_dir=project_dir, global_config_path=global_path)
        assert config["archive-max-age-days"] == 5


# conftest.count_config_warningsを再エクスポート（同モジュール内の参照を統一するため）
_count_config_warnings = _testconf.count_config_warnings
