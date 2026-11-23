import pathlib
import typing

import pytest

import pyfltr.command.core_
import pyfltr.command.dispatcher
import pyfltr.command.env
import pyfltr.command.glab
import pyfltr.command.invocation
import pyfltr.command.mise
import pyfltr.command.only_failed
import pyfltr.command.precommit
import pyfltr.command.precommit_guidance
import pyfltr.command.process
import pyfltr.command.runner
import pyfltr.command.slow_tests
import pyfltr.command.subprojects
import pyfltr.command.targets
import pyfltr.command.tool_resolution
import pyfltr.command.two_step.base
import pyfltr.command.two_step.prettier
import pyfltr.command.two_step.ruff
import pyfltr.config.config
import pyfltr.config.editing
import pyfltr.config.model
import pyfltr.config.selection
import pyfltr.config.validation
import pyfltr.paths
import pyfltr.run_options
import pyfltr.state.cache
import pyfltr.state.only_failed
import pyfltr.tool_types
import pyfltr.tools
import pyfltr.warnings_
from tests import conftest as _testconf


def test_expanduser_does_not_apply_to_targets(tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HOME", "/tmp/fake-home")
    pyproject_content = """
[tool.pyfltr.custom-commands.tilde-glob]
type = "linter"
path = "echo"
targets = ["~/never-expanded.py"]
config-files = ["~/never-expanded.toml"]
"""
    (tmp_path / "pyproject.toml").write_text(pyproject_content)
    config = pyfltr.config.config.load_config(config_dir=tmp_path)
    assert config.commands["tilde-glob"].target_globs() == ["~/never-expanded.py"]
    assert config.commands["tilde-glob"].config_files == ["~/never-expanded.toml"]


def _find_config_warning(needle: str) -> str | None:
    for w in pyfltr.warnings_.collected_warnings():
        if w["source"] == "config" and needle in w["message"]:
            return str(w["message"])
    return None


def _assert_language_gate(
    config: pyfltr.config.model.Config,
    category_key: str,
    *,
    passed: bool,
    enabled: frozenset[str] | None = None,
) -> None:
    if passed and enabled is None:
        raise ValueError("passed=True の場合は enabled を指定する必要がある")
    commands = dict(pyfltr.tools.LANGUAGE_CATEGORIES)[category_key]
    if not passed:
        for cmd in commands:
            assert config[cmd] is False, f"{category_key} gate閉なのに{cmd}=True"
        return
    assert enabled is not None
    for cmd in commands:
        expected = cmd in enabled
        assert config[cmd] is expected, f"{category_key} gate開: {cmd} expected {expected}, got {config[cmd]}"


_DOCS_ORTHOGONAL_KEYS = ("textlint", "markdownlint", "actionlint", "pinact", "typos", "prek", "pre-commit")


_PYTHON_ENABLED_20260330 = frozenset({"ruff-format", "ruff-check", "mypy", "pylint", "arid", "pytest", "pyright"})


_PYTHON_ENABLED_LATEST = frozenset({"ruff-format", "ruff-check", "mypy", "pylint", "arid", "pytest", "pyright", "uv-sort"})


_JAVASCRIPT_ENABLED = frozenset({"eslint", "biome", "oxlint", "prettier", "tsc", "vitest"})


_RUST_ENABLED = frozenset({"cargo-fmt", "cargo-clippy", "cargo-check", "cargo-test", "cargo-deny"})


_DOTNET_ENABLED = frozenset({"dotnet-format", "dotnet-build", "dotnet-test"})


@pytest.mark.parametrize(
    "preset,extra_lines,docs_expected,gate_config",
    [
        (
            "",
            "",
            {
                "textlint": False,
                "markdownlint": False,
                "actionlint": False,
                "pinact": False,
                "typos": False,
                "prek": False,
                "pre-commit": False,
            },
            {"python": False, "javascript": False, "rust": False, "dotnet": False},
        ),
        (
            "20260330",
            "python = true\n",
            {
                "textlint": True,
                "markdownlint": True,
                "actionlint": False,
                "pinact": False,
                "typos": False,
                "prek": False,
                "pre-commit": False,
            },
            {"python": _PYTHON_ENABLED_20260330, "javascript": False, "rust": False, "dotnet": False},
        ),
        (
            "20260411",
            "python = true\n",
            {
                "textlint": True,
                "markdownlint": True,
                "actionlint": True,
                "pinact": False,
                "typos": True,
                "prek": False,
                "pre-commit": False,
            },
            {"python": _PYTHON_ENABLED_LATEST, "javascript": False, "rust": False, "dotnet": False},
        ),
        (
            "20260413",
            "python = true\n",
            {
                "textlint": True,
                "markdownlint": True,
                "actionlint": True,
                "pinact": False,
                "typos": True,
                "prek": False,
                "pre-commit": True,
            },
            {"python": _PYTHON_ENABLED_LATEST, "javascript": False, "rust": False, "dotnet": False},
        ),
        (
            "20260726",
            "python = true\n",
            {
                "textlint": True,
                "markdownlint": True,
                "actionlint": True,
                "pinact": False,
                "typos": True,
                "prek": True,
                "pre-commit": False,
            },
            {"python": _PYTHON_ENABLED_LATEST, "javascript": False, "rust": False, "dotnet": False},
        ),
        (
            "20260926",
            "python = true\n",
            {
                "textlint": True,
                "markdownlint": True,
                "actionlint": True,
                "pinact": True,
                "typos": True,
                "prek": True,
                "pre-commit": False,
            },
            {"python": _PYTHON_ENABLED_LATEST, "javascript": False, "rust": False, "dotnet": False},
        ),
        (
            "latest",
            "python = true\n",
            {
                "textlint": True,
                "markdownlint": True,
                "actionlint": True,
                "pinact": True,
                "typos": True,
                "prek": True,
                "pre-commit": False,
            },
            {"python": _PYTHON_ENABLED_LATEST, "javascript": False, "rust": False, "dotnet": False},
        ),
        (
            "latest",
            "javascript = true\n",
            {"textlint": True, "markdownlint": True},
            {"python": False, "javascript": _JAVASCRIPT_ENABLED, "rust": False, "dotnet": False},
        ),
        (
            "latest",
            "rust = true\n",
            {},
            {"python": False, "javascript": False, "rust": _RUST_ENABLED, "dotnet": False},
        ),
        (
            "latest",
            "dotnet = true\n",
            {},
            {"python": False, "javascript": False, "rust": False, "dotnet": _DOTNET_ENABLED},
        ),
        (
            "20260330",
            "rust = true\n",
            {},
            {"python": False, "javascript": False, "rust": _RUST_ENABLED, "dotnet": False},
        ),
    ],
)
def test_apply_preset(
    tmp_path: pathlib.Path,
    preset: str,
    extra_lines: str,
    docs_expected: dict[str, bool],
    gate_config: dict[str, bool | frozenset[str]],
) -> None:
    pyproject_path = tmp_path / "pyproject.toml"
    pyproject_path.write_text(f'[tool.pyfltr]\npreset = "{preset}"\n{extra_lines}')

    config = pyfltr.config.config.load_config(config_dir=tmp_path)
    for key, value in docs_expected.items():
        assert config[key] == value, f"{key}: expected {value}, got {config[key]}"
    for category_key, value in gate_config.items():
        if isinstance(value, frozenset):
            _assert_language_gate(config, category_key, passed=True, enabled=value)
        else:
            _assert_language_gate(config, category_key, passed=value)


def test_custom_command(tmp_path: pathlib.Path) -> None:
    pyproject_content = """
[tool.pyfltr]
preset = "latest"
python = true

[tool.pyfltr.custom-commands.mytool]
type = "linter"
path = "mytool"
args = ["-r"]
targets = "*.py"
error-pattern = '(?P<file>[^:]+):(?P<line>\\d+):(?P<col>\\d+):\\s*(?P<message>.+)'
fast = true
"""
    (tmp_path / "pyproject.toml").write_text(pyproject_content)
    config = pyfltr.config.config.load_config(config_dir=tmp_path)

    assert "mytool" in config.commands
    assert config.commands["mytool"].type == "linter"
    assert config.commands["mytool"].builtin is False
    assert config.commands["mytool"].targets == "*.py"
    assert config.commands["mytool"].error_pattern is not None

    assert config["mytool"] is True
    assert config["mytool-path"] == "mytool"
    assert config["mytool-args"] == ["-r"]
    assert config["mytool-fast"] is True

    assert config.command_names[-1] == "mytool"

    assert config["ruff-format"] is True

    assert "mytool" in config["aliases"]["fast"]


@pytest.mark.parametrize(
    "name,definition,needle",
    [
        ("mypy", 'type = "linter"\n', "衝突"),
        ("foo", 'type = "invalid"\n', "type"),
        ("foo", 'type = "linter"\npath = 42\n', "path"),
        ("foo", 'type = "linter"\nargs = "not-a-list"\n', "args"),
        ("foo", 'type = "linter"\nerror-pattern = 42\n', "error-pattern"),
        (
            "foo",
            "type = \"linter\"\nerror-pattern = '(?P<file>[^:]+):(?P<line>\\d+)'\n",
            "message",
        ),
        ("foo", "type = \"linter\"\nerror-pattern = '[unclosed'\n", "error-pattern"),
        ("foo", 'type = "linter"\nfast = "yes"\n', "fast"),
        ("foo", 'type = "linter"\npass-filenames = "yes"\n', "pass-filenames"),
        ("bad-linter", 'type = "linter"\nfix-args = "--fix"\n', "fix-args"),
        ("bad", 'type = "linter"\nseverity = "info"\n', "severity"),
        ("bad", 'type = "linter"\nhints = [1]\n', "hints"),
        ("bad", 'type = "linter"\ntargets = 42\n', "targets"),
        ("mytool", 'type = "linter"\npath = "mytool"\nconfig-files = "foo"\n', "config-files"),
    ],
)
def test_custom_command_invalid_definition_warns(tmp_path: pathlib.Path, name: str, definition: str, needle: str) -> None:
    pyproject_content = f"[tool.pyfltr.custom-commands.{name}]\n{definition}"
    (tmp_path / "pyproject.toml").write_text(pyproject_content)
    config = pyfltr.config.config.load_config(config_dir=tmp_path)
    assert _testconf.count_config_warnings(needle) >= 1
    if name == "mypy":
        assert config.commands["mypy"].builtin is True
    else:
        assert name not in config.commands
        assert name not in config.values
        assert name not in config.command_names


def test_fast_alias_dynamic(tmp_path: pathlib.Path) -> None:
    config = pyfltr.config.config.create_default_config()
    fast = config["aliases"]["fast"]
    assert "ruff-format" in fast
    assert "arid" in fast
    assert "prek" in fast
    assert "pre-commit" in fast
    assert "mypy" not in fast
    assert "pylint" not in fast
    assert "pytest" not in fast

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


@pytest.mark.parametrize(
    ("value", "expected_globs", "in_fast"),
    [
        ("[]", [], False),
        ('"*_invariant_test.py"', ["*_invariant_test.py"], True),
        ('["a_test.py", "b/*_test.py"]', ["a_test.py", "b/*_test.py"], True),
    ],
)
def test_pytest_fast_targets_values_and_fast_alias(
    tmp_path: pathlib.Path, value: str, expected_globs: list[str], in_fast: bool
) -> None:
    (tmp_path / "pyproject.toml").write_text(
        f"[tool.pyfltr]\npytest = true\npytest-fast = false\npytest-fast-targets = {value}\n", encoding="utf-8"
    )
    config = pyfltr.config.config.load_config(config_dir=tmp_path)
    assert pyfltr.config.selection.pytest_fast_target_globs(config.values) == expected_globs
    assert ("pytest" in config["aliases"]["fast"]) is in_fast
    assert config.commands["pytest"].targets == pyfltr.tools.BUILTIN_COMMANDS["pytest"].targets
    assert _find_config_warning("pytest-fast-targets") is None


def test_pytest_fast_targets_invalid_type_warns_and_keeps_default(tmp_path: pathlib.Path) -> None:
    (tmp_path / "pyproject.toml").write_text("[tool.pyfltr]\npytest = true\npytest-fast-targets = 42\n", encoding="utf-8")
    config = pyfltr.config.config.load_config(config_dir=tmp_path)
    assert pyfltr.config.selection.pytest_fast_target_globs(config.values) == []
    assert "pytest" not in config["aliases"]["fast"]
    message = _find_config_warning("pytest-fast-targets")
    assert message is not None
    assert "文字列または文字列のリスト" in message


@pytest.mark.parametrize(
    ("value", "expected_globs", "warned"),
    [
        ('"*_invariant_test.py"', ["*_invariant_test.py"], False),
        ('["a_test.py", "b/*_test.py"]', ["a_test.py", "b/*_test.py"], False),
        ("42", [], True),
    ],
)
def test_pytest_always_targets_values(tmp_path: pathlib.Path, value: str, expected_globs: list[str], warned: bool) -> None:
    (tmp_path / "pyproject.toml").write_text(
        f"[tool.pyfltr]\npytest = true\npytest-always-targets = {value}\n", encoding="utf-8"
    )
    config = pyfltr.config.config.load_config(config_dir=tmp_path)
    assert pyfltr.config.selection.pytest_always_target_globs(config.values) == expected_globs
    assert "pytest" not in config["aliases"]["fast"]
    assert (_find_config_warning("pytest-always-targets") is not None) is warned


def test_ruff_format_by_check_default() -> None:
    config = pyfltr.config.config.create_default_config()
    assert config["ruff-format-by-check"] is True
    assert config["ruff-format-check-args"] == ["check", "--fix", "--unsafe-fixes"]


def test_ruff_format_by_check_overridable(tmp_path: pathlib.Path) -> None:
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
    config = pyfltr.config.config.create_default_config()
    assert config["textlint-fix-args"] == ["--fix"]
    assert config["markdownlint-fix-args"] == ["--fix"]
    assert config["ruff-check-fix-args"] == ["--fix", "--unsafe-fixes"]
    assert "mypy-fix-args" not in config.values
    assert "pytest-fix-args" not in config.values


def test_filter_fix_commands_defaults() -> None:
    config = pyfltr.config.config.create_default_config()
    commands = ["mypy", "textlint", "markdownlint", "ruff-check"]
    result = pyfltr.config.selection.filter_fix_commands(commands, config)
    assert not result


def test_filter_fix_commands_enabled_linter(tmp_path: pathlib.Path) -> None:
    pyproject_content = """
[tool.pyfltr]
textlint = true
markdownlint = true
ruff-check = true
"""
    (tmp_path / "pyproject.toml").write_text(pyproject_content)
    config = pyfltr.config.config.load_config(config_dir=tmp_path)
    commands = ["textlint", "markdownlint", "ruff-check", "mypy"]
    result = pyfltr.config.selection.filter_fix_commands(commands, config)
    assert "textlint" in result
    assert "markdownlint" in result
    assert "ruff-check" in result
    assert "mypy" not in result


def test_custom_command_fix_args(tmp_path: pathlib.Path) -> None:
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
    result = pyfltr.config.selection.filter_fix_commands(["my-linter"], config)
    assert result == ["my-linter"]


def test_custom_command_without_fix_args(tmp_path: pathlib.Path) -> None:
    pyproject_content = """
[tool.pyfltr.custom-commands.plain-linter]
type = "linter"
path = "plain-linter"
"""
    (tmp_path / "pyproject.toml").write_text(pyproject_content)
    config = pyfltr.config.config.load_config(config_dir=tmp_path)
    assert "plain-linter-fix-args" not in config.values
    result = pyfltr.config.selection.filter_fix_commands(["plain-linter"], config)
    assert not result


def test_custom_command_subproject_aware_default_registered(tmp_path: pathlib.Path) -> None:
    pyproject_content = """
[tool.pyfltr.custom-commands.mylinter]
type = "linter"
path = "mylinter"
"""
    (tmp_path / "pyproject.toml").write_text(pyproject_content)
    config = pyfltr.config.config.load_config(config_dir=tmp_path)
    assert config["mylinter-subproject-aware"] is True


def test_custom_command_subproject_aware_override(tmp_path: pathlib.Path) -> None:
    pyproject_content = """
[tool.pyfltr]
mylinter-subproject-aware = false

[tool.pyfltr.custom-commands.mylinter]
type = "linter"
path = "mylinter"
"""
    (tmp_path / "pyproject.toml").write_text(pyproject_content)
    config = pyfltr.config.config.load_config(config_dir=tmp_path)
    assert config["mylinter-subproject-aware"] is False
    assert _testconf.count_config_warnings("mylinter-subproject-aware") == 0


def test_removed_slot_ticket_key_warns_and_is_ignored(tmp_path: pathlib.Path) -> None:
    pyproject_content = """
[tool.pyfltr]
pytest-slot-ticket = true
mylinter-slot-ticket = true

[tool.pyfltr.custom-commands.mylinter]
type = "linter"
path = "mylinter"
"""
    (tmp_path / "pyproject.toml").write_text(pyproject_content)
    config = pyfltr.config.config.load_config(config_dir=tmp_path)
    assert "pytest-slot-ticket" not in config.values
    assert "mylinter-slot-ticket" not in config.values
    assert _testconf.count_config_warnings("pytest-slot-ticket") == 1
    assert _testconf.count_config_warnings("mylinter-slot-ticket") == 1


def test_severity_default_is_error() -> None:
    config = pyfltr.config.config.create_default_config()
    for name, info in pyfltr.tools.BUILTIN_COMMANDS.items():
        expected = info.defaults.get(f"{name}-severity", "error")
        assert config.values[f"{name}-severity"] == expected, f"{name}-severity既定値"
    assert pyfltr.config.model.resolve_severity(config.values, "mypy") == "error"


def test_colloquial_check_severity_default_is_warning() -> None:
    assert pyfltr.tools.BUILTIN_COMMANDS["colloquial-check"].defaults["colloquial-check-severity"] == "warning"
    config = pyfltr.config.config.create_default_config()
    assert config.values["colloquial-check-severity"] == "warning"
    assert pyfltr.config.model.resolve_severity(config.values, "colloquial-check") == "warning"


def test_severity_defaults_keep_builtin_specific_value() -> None:
    defaults: dict[str, typing.Any] = {"alpha-severity": "warning"}
    pyfltr.config.model.register_command_severity_defaults(defaults, ["alpha", "beta"])
    assert defaults == {"alpha-severity": "warning", "beta-severity": "error"}


def test_colloquial_check_severity_explicit_error(tmp_path: pathlib.Path) -> None:
    (tmp_path / "pyproject.toml").write_text('[tool.pyfltr]\ncolloquial-check-severity = "error"\n')
    config = pyfltr.config.config.load_config(config_dir=tmp_path)
    assert config.values["colloquial-check-severity"] == "error"


def test_severity_warning_resolved(tmp_path: pathlib.Path) -> None:
    (tmp_path / "pyproject.toml").write_text('[tool.pyfltr]\nmypy-severity = "warning"\n')
    config = pyfltr.config.config.load_config(config_dir=tmp_path)
    assert config.values["mypy-severity"] == "warning"
    assert pyfltr.config.model.resolve_severity(config.values, "mypy") == "warning"


def test_severity_invalid_value_warns(tmp_path: pathlib.Path) -> None:
    (tmp_path / "pyproject.toml").write_text('[tool.pyfltr]\nmypy-severity = "info"\n')
    config = pyfltr.config.config.load_config(config_dir=tmp_path)
    assert _testconf.count_config_warnings("mypy-severity") >= 1
    assert config.values["mypy-severity"] == "error"
    assert pyfltr.config.model.resolve_severity(config.values, "mypy") == "error"


def test_custom_command_severity_warning(tmp_path: pathlib.Path) -> None:
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


def test_hints_default_is_empty() -> None:
    config = pyfltr.config.config.create_default_config()
    for name in pyfltr.tools.BUILTIN_COMMAND_NAMES:
        assert config.values[f"{name}-hints"] == [], f"{name}-hints既定値"


def test_hints_override(tmp_path: pathlib.Path) -> None:
    (tmp_path / "pyproject.toml").write_text('[tool.pyfltr]\nmypy-hints = ["Read mypy strict-mode docs.", "Avoid Any."]\n')
    config = pyfltr.config.config.load_config(config_dir=tmp_path)
    assert config.values["mypy-hints"] == ["Read mypy strict-mode docs.", "Avoid Any."]


def test_hints_invalid_element_warns(tmp_path: pathlib.Path) -> None:
    (tmp_path / "pyproject.toml").write_text("[tool.pyfltr]\nmypy-hints = [1, 2]\n")
    config = pyfltr.config.config.load_config(config_dir=tmp_path)
    assert _testconf.count_config_warnings("mypy-hints") >= 1
    assert config.values["mypy-hints"] == []


def test_custom_command_hints(tmp_path: pathlib.Path) -> None:
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


def test_custom_commands_table_invalid_type_warns(tmp_path: pathlib.Path) -> None:
    (tmp_path / "pyproject.toml").write_text('[tool.pyfltr]\ncustom-commands = "bogus"\n')
    config = pyfltr.config.config.load_config(config_dir=tmp_path)
    assert _testconf.count_config_warnings("custom-commands") >= 1
    for name, info in config.commands.items():
        assert info.builtin is True, f"非ビルトインコマンド `{name}` が登録されている"


@pytest.mark.parametrize("category_key", ["python", "javascript", "rust", "dotnet"])
def test_language_category_non_bool_warns(tmp_path: pathlib.Path, category_key: str) -> None:
    (tmp_path / "pyproject.toml").write_text(f'[tool.pyfltr]\npreset = "latest"\n{category_key} = "yes"\n')
    config = pyfltr.config.config.load_config(config_dir=tmp_path)
    assert _testconf.count_config_warnings(category_key) >= 1
    _assert_language_gate(config, category_key, passed=False)


def test_custom_command_args_preserve_tilde(tmp_path: pathlib.Path) -> None:
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
    config = pyfltr.config.config.create_default_config()
    assert config["vitest-args"] == ["run", "--passWithNoTests"]


def test_js_runner_override(tmp_path: pathlib.Path) -> None:
    pyproject_content = """
[tool.pyfltr]
js-runner = "pnpm"
"""
    (tmp_path / "pyproject.toml").write_text(pyproject_content)
    config = pyfltr.config.config.load_config(config_dir=tmp_path)
    assert config["js-runner"] == "pnpm"


def test_js_runner_invalid_warns(tmp_path: pathlib.Path) -> None:
    pyproject_content = """
[tool.pyfltr]
js-runner = "bogus"
"""
    (tmp_path / "pyproject.toml").write_text(pyproject_content)
    config = pyfltr.config.config.load_config(config_dir=tmp_path)
    assert _testconf.count_config_warnings("js-runner") >= 1
    assert config["js-runner"] == "pnpx"


def test_textlint_packages_default() -> None:
    config = pyfltr.config.config.create_default_config()
    assert config["textlint-packages"] == [
        "textlint-rule-preset-ja-technical-writing",
        "textlint-rule-preset-jtf-style",
        "textlint-rule-ja-no-abusage",
        "textlint-rule-preset-ai-words-ja",
    ]


def test_textlint_protected_identifiers_default() -> None:
    config = pyfltr.config.config.create_default_config()
    identifiers = config["textlint-protected-identifiers"]
    assert ".NET" in identifiers
    assert "Node.js" in identifiers
    assert "Vue.js" in identifiers
    assert "Next.js" in identifiers
    assert "Nuxt.js" in identifiers


def test_textlint_protected_identifiers_override(tmp_path: pathlib.Path) -> None:
    (tmp_path / "pyproject.toml").write_text("[tool.pyfltr]\ntextlint-protected-identifiers = []\n")
    config = pyfltr.config.config.load_config(config_dir=tmp_path)
    assert config["textlint-protected-identifiers"] == []


def test_textlint_markdownlint_path_default_empty() -> None:
    config = pyfltr.config.config.create_default_config()
    assert config["textlint-path"] == ""
    assert config["markdownlint-path"] == ""
    assert config["markdownlint-args"] == []


@pytest.mark.parametrize(
    "command,expected_enabled,expected_runner",
    [
        ("designmd", True, "js-runner"),
        ("lychee", True, "bin-runner"),
        ("semgrep", False, "uvx"),
        ("sqlfluff", False, "uvx"),
        ("bandit", False, "python-runner"),
        ("colloquial-check", False, "direct"),
        ("comment-check", False, "direct"),
        ("markdown-structure", False, "direct"),
        ("test-copies", False, "direct"),
    ],
)
def test_representative_tools_defaults_by_runner_classification(
    command: str, expected_enabled: bool, expected_runner: str
) -> None:
    config = pyfltr.config.config.create_default_config()
    assert config[command] is expected_enabled, f"{command} の既定値が想定と異なる"
    assert config[f"{command}-runner"] == expected_runner
    assert f"{command}-path" in config.values
    assert f"{command}-args" in config.values
    assert f"{command}-fast" in config.values


def test_lychee_version_default_exists() -> None:
    config = pyfltr.config.config.create_default_config()
    assert config["lychee-version"] == "latest"


@pytest.mark.parametrize("command", ["designmd", "lychee", "semgrep", "sqlfluff", "bandit", "colloquial-check", "arid"])
def test_new_tools_registered_in_lint_alias(command: str) -> None:
    config = pyfltr.config.config.create_default_config()
    assert command in config["aliases"]["lint"]


@pytest.mark.parametrize("command", ["uv-audit", "pnpm-audit", "npm-audit", "yarn-audit"])
def test_audit_tools_defaults(command: str) -> None:
    config = pyfltr.config.config.create_default_config()
    assert config[command] is False, f"{command} はopt-inのため既定無効"
    assert config[f"{command}-runner"] == "direct"
    assert config[f"{command}-pass-filenames"] is False
    assert config[f"{command}-fast"] is False
    assert f"{command}-version" not in config.values


@pytest.mark.parametrize("command", ["uv-audit", "pnpm-audit", "npm-audit", "yarn-audit"])
def test_audit_tools_registered_in_lint_alias(command: str) -> None:
    config = pyfltr.config.config.create_default_config()
    assert command in config["aliases"]["lint"]


def test_audit_alias_resolves_to_audit_tools() -> None:
    config = pyfltr.config.config.create_default_config()
    resolved = pyfltr.config.selection.resolve_aliases(["audit"], config)
    assert resolved == ["uv-audit", "pnpm-audit", "npm-audit", "yarn-audit"]


def test_uv_audit_args_contain_frozen_and_preview_features() -> None:
    config = pyfltr.config.config.create_default_config()
    assert config["uv-audit-args"] == ["audit", "--preview-features", "audit", "--frozen", "--no-progress"]
    assert "--frozen" in config["uv-audit-args"]
    assert "--preview-features" in config["uv-audit-args"]


@pytest.mark.parametrize("command", ["pnpm-audit", "npm-audit", "yarn-audit"])
def test_js_audit_tools_args_use_json(command: str) -> None:
    config = pyfltr.config.config.create_default_config()
    assert config[f"{command}-args"] == ["audit", "--json"]


def test_command_info_target_globs_str() -> None:
    info = pyfltr.tool_types.CommandInfo(type="linter", targets="*.py")
    assert info.target_globs() == ["*.py"]


def test_command_info_target_globs_list() -> None:
    info = pyfltr.tool_types.CommandInfo(type="linter", targets=["*.ts", "*.tsx"])
    result = info.target_globs()
    assert result == ["*.ts", "*.tsx"]
    result.append("*.js")
    assert info.targets == ["*.ts", "*.tsx"]


# test-copy-contract: 全組み込みコマンドが動的設定キー4種を持つ構造的制約
def test_builtin_dynamic_config_defaults() -> None:
    expected_keys = {
        f"{command}-{suffix}"
        for command in pyfltr.tools.BUILTIN_COMMAND_NAMES
        for suffix in ("targets", "extend-targets", "exclude", "extend-args")
    }
    assert len(expected_keys) == 4 * len(pyfltr.tools.BUILTIN_COMMANDS)
    assert expected_keys <= pyfltr.config.model.DEFAULT_CONFIG.keys()

    for command, info in pyfltr.tools.BUILTIN_COMMANDS.items():
        targets = pyfltr.config.model.DEFAULT_CONFIG[f"{command}-targets"]
        assert targets == info.targets
        if isinstance(info.targets, list):
            assert targets is not info.targets
        assert pyfltr.config.model.DEFAULT_CONFIG[f"{command}-extend-targets"] == []
        assert pyfltr.config.model.DEFAULT_CONFIG[f"{command}-exclude"] == []
        assert pyfltr.config.model.DEFAULT_CONFIG[f"{command}-extend-args"] == []


def test_arid_config_defaults() -> None:
    config = pyfltr.config.config.create_default_config()

    assert config["arid"] is False
    assert config["arid-path"] == ""
    assert config["arid-args"] == ["--project-root", "."]
    assert config["arid-runner"] == "python-runner"
    assert config["arid-fast"] is True
    assert config["arid-json"] is True


def test_custom_command_targets_list(tmp_path: pathlib.Path) -> None:
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


def test_builtin_targets_invalid_type_warns(tmp_path: pathlib.Path) -> None:
    original = pyfltr.tools.BUILTIN_COMMANDS["shfmt"].targets
    (tmp_path / "pyproject.toml").write_text("[tool.pyfltr]\nshfmt-targets = 42\n")
    config = pyfltr.config.config.load_config(config_dir=tmp_path)
    assert _testconf.count_config_warnings("shfmt-targets") >= 1
    assert config.commands["shfmt"].targets == original
    assert config.values["shfmt-targets"] == original


class TestConfigFilesWarning:
    def test_for_subproject_suppresses_precommit_prek_conflict_warning(self, tmp_path: pathlib.Path) -> None:
        (tmp_path / "pyproject.toml").write_text("[tool.pyfltr]\npre-commit = true\nprek = true\n", encoding="utf-8")
        (tmp_path / ".pre-commit-config.yaml").write_text("repos: []\n", encoding="utf-8")

        pyfltr.config.config.load_config(config_dir=tmp_path, for_subproject=True)

        entries = [
            warning
            for warning in pyfltr.warnings_.collected_warnings()
            if warning["source"] == "config" and "二重実行" in warning["message"]
        ]
        assert not entries


def test_invalid_preset_warns(tmp_path: pathlib.Path) -> None:
    pyproject_content = """
[tool.pyfltr]
preset = "invalid"
"""
    pyproject_path = tmp_path / "pyproject.toml"
    pyproject_path.write_text(pyproject_content)

    config = pyfltr.config.config.load_config(config_dir=tmp_path)
    assert _testconf.count_config_warnings("invalid") >= 1
    assert config["textlint"] is False
    assert config["markdownlint"] is False
    _assert_language_gate(config, "python", passed=False)
    assert config["preset"] == ""


def test_valid_preset_is_saved_to_config_values(tmp_path: pathlib.Path) -> None:
    (tmp_path / "pyproject.toml").write_text('[tool.pyfltr]\npreset = "latest"\n')
    config = pyfltr.config.config.load_config(config_dir=tmp_path)
    assert config["preset"] == "latest"


def test_removed_preset_20250710_warns(tmp_path: pathlib.Path) -> None:
    (tmp_path / "pyproject.toml").write_text('[tool.pyfltr]\npreset = "20250710"\n')
    config = pyfltr.config.config.load_config(config_dir=tmp_path)
    message = _find_config_warning("削除")
    assert message is not None
    assert "latest" in message
    _assert_language_gate(config, "python", passed=False)


@pytest.mark.parametrize("removed_tool", ["pyupgrade", "autoflake", "isort", "black", "pflake8"])
def test_removed_tool_config_key_warns(tmp_path: pathlib.Path, removed_tool: str) -> None:
    (tmp_path / "pyproject.toml").write_text(f"[tool.pyfltr]\n{removed_tool} = true\n")
    config = pyfltr.config.config.load_config(config_dir=tmp_path)
    assert _testconf.count_config_warnings(removed_tool) >= 1
    assert removed_tool not in config.values
    assert removed_tool not in config.commands


def test_archive_config_defaults() -> None:
    config = pyfltr.config.config.create_default_config()
    assert config["archive"] is True
    assert config["archive-max-runs"] == 100
    assert config["archive-max-size-mb"] == 1024
    assert config["archive-max-age-days"] == 30


def test_jsonl_smart_truncation_defaults() -> None:
    config = pyfltr.config.config.create_default_config()
    assert config["jsonl-diagnostic-limit"] == 0
    assert config["jsonl-message-max-lines"] == 30
    assert config["jsonl-message-max-chars"] == 2000


def test_jsonl_smart_truncation_override(tmp_path: pathlib.Path) -> None:
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


def test_jsonl_smart_truncation_invalid_type_warns(tmp_path: pathlib.Path) -> None:
    (tmp_path / "pyproject.toml").write_text('[tool.pyfltr]\njsonl-diagnostic-limit = "many"\n')
    config = pyfltr.config.config.load_config(config_dir=tmp_path)
    assert _testconf.count_config_warnings("jsonl-diagnostic-limit") >= 1
    assert config["jsonl-diagnostic-limit"] == 0


def test_archive_config_override(tmp_path: pathlib.Path) -> None:
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
    config = pyfltr.config.config.create_default_config()
    assert config["respect-gitignore"] is True


def test_cache_config_defaults() -> None:
    config = pyfltr.config.config.create_default_config()
    assert config["cache"] is True
    assert config["cache-max-age-hours"] == 12


def test_cache_config_override(tmp_path: pathlib.Path) -> None:
    pyproject_content = """
[tool.pyfltr]
cache = false
cache-max-age-hours = 24
"""
    (tmp_path / "pyproject.toml").write_text(pyproject_content)
    config = pyfltr.config.config.load_config(config_dir=tmp_path)
    assert config["cache"] is False
    assert config["cache-max-age-hours"] == 24


def test_cache_config_invalid_type_warns(tmp_path: pathlib.Path) -> None:
    (tmp_path / "pyproject.toml").write_text('[tool.pyfltr]\ncache-max-age-hours = "many"\n')
    config = pyfltr.config.config.load_config(config_dir=tmp_path)
    assert _testconf.count_config_warnings("cache-max-age-hours") >= 1
    assert config["cache-max-age-hours"] == 12


def test_retry_on_oom_default() -> None:
    config = pyfltr.config.config.create_default_config()
    assert config["retry-on-oom"] is True


def test_retry_max_attempts_default() -> None:
    config = pyfltr.config.config.create_default_config()
    assert config["retry-max-attempts"] == 1


def test_retry_config_overridable(tmp_path: pathlib.Path) -> None:
    pyproject_content = """
[tool.pyfltr]
retry-on-oom = false
retry-max-attempts = 3
"""
    (tmp_path / "pyproject.toml").write_text(pyproject_content)
    config = pyfltr.config.config.load_config(config_dir=tmp_path)
    assert config["retry-on-oom"] is False
    assert config["retry-max-attempts"] == 3


def test_textlint_command_info_is_cacheable() -> None:
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
    config = pyfltr.config.config.create_default_config()
    for name, info in config.commands.items():
        if name == "textlint":
            continue
        assert info.cacheable is False, f"{name}が意図せずcacheable=Trueになっている"


def test_auto_option_defaults() -> None:
    config = pyfltr.config.config.create_default_config()
    assert config["pylint-pydantic"] is True
    assert config["mypy-unused-awaitable"] is True
    assert config["lychee-max-concurrency"] == 4


def test_lychee_max_concurrency_override(tmp_path: pathlib.Path) -> None:
    (tmp_path / "pyproject.toml").write_text("[tool.pyfltr]\nlychee-max-concurrency = 0\n")
    config = pyfltr.config.config.load_config(config_dir=tmp_path)
    assert config["lychee-max-concurrency"] == 0


def test_auto_option_disable(tmp_path: pathlib.Path) -> None:
    pyproject_content = """
[tool.pyfltr]
pylint-pydantic = false
mypy-unused-awaitable = false
"""
    (tmp_path / "pyproject.toml").write_text(pyproject_content)
    config = pyfltr.config.config.load_config(config_dir=tmp_path)
    assert config["pylint-pydantic"] is False
    assert config["mypy-unused-awaitable"] is False


def test_python_default_disables_python_tools() -> None:
    config = pyfltr.config.config.create_default_config()
    _assert_language_gate(config, "python", passed=False)
    assert config["markdownlint"] is False
    assert config["textlint"] is False


def test_python_true_without_preset_enables_nothing(tmp_path: pathlib.Path) -> None:
    pyproject_content = """
[tool.pyfltr]
python = true
"""
    (tmp_path / "pyproject.toml").write_text(pyproject_content)
    config = pyfltr.config.config.load_config(config_dir=tmp_path)
    for cmd in pyfltr.tools.PYTHON_COMMANDS:
        assert config[cmd] is False, f"{cmd}はpreset未指定ではFalseのまま"
    assert config["markdownlint"] is False
    assert config["textlint"] is False


def test_python_true_with_individual_extra(tmp_path: pathlib.Path) -> None:
    pyproject_content = """
[tool.pyfltr]
preset = "latest"
python = true
ty = true
"""
    (tmp_path / "pyproject.toml").write_text(pyproject_content)
    config = pyfltr.config.config.load_config(config_dir=tmp_path)
    assert config["ty"] is True
    assert config["mypy"] is True
    assert config["pyright"] is True
    assert config["ruff-format"] is True


def test_python_true_with_individual_override(tmp_path: pathlib.Path) -> None:
    pyproject_content = """
[tool.pyfltr]
preset = "latest"
python = true
ruff-check = false
mypy = false
"""
    (tmp_path / "pyproject.toml").write_text(pyproject_content)
    config = pyfltr.config.config.load_config(config_dir=tmp_path)
    assert config["ruff-check"] is False
    assert config["mypy"] is False
    assert config["ruff-format"] is True
    assert config["pyright"] is True
    assert config["pylint"] is True


def test_individual_tool_crosses_gate(tmp_path: pathlib.Path) -> None:
    pyproject_content = """
[tool.pyfltr]
preset = "latest"
mypy = true
"""
    (tmp_path / "pyproject.toml").write_text(pyproject_content)
    config = pyfltr.config.config.load_config(config_dir=tmp_path)
    assert config["mypy"] is True
    assert config["ruff-format"] is False
    assert config["ruff-check"] is False
    assert config["pylint"] is False
    assert config["pyright"] is False
    assert config["pytest"] is False


def test_bin_runner_default() -> None:
    config = pyfltr.config.config.create_default_config()
    assert config["bin-runner"] == "mise"


def test_bin_runner_override(tmp_path: pathlib.Path) -> None:
    pyproject_content = """
[tool.pyfltr]
bin-runner = "direct"
"""
    (tmp_path / "pyproject.toml").write_text(pyproject_content)
    config = pyfltr.config.config.load_config(config_dir=tmp_path)
    assert config["bin-runner"] == "direct"


def test_bin_runner_invalid_warns(tmp_path: pathlib.Path) -> None:
    pyproject_content = """
[tool.pyfltr]
bin-runner = "bogus"
"""
    (tmp_path / "pyproject.toml").write_text(pyproject_content)
    config = pyfltr.config.config.load_config(config_dir=tmp_path)
    assert _testconf.count_config_warnings("bin-runner") >= 1
    assert config["bin-runner"] == "mise"


def test_python_runner_default() -> None:
    config = pyfltr.config.config.create_default_config()
    assert config["python-runner"] == "uv"


def test_python_runner_override(tmp_path: pathlib.Path) -> None:
    pyproject_content = """
[tool.pyfltr]
python-runner = "uvx"
"""
    (tmp_path / "pyproject.toml").write_text(pyproject_content)
    config = pyfltr.config.config.load_config(config_dir=tmp_path)
    assert config["python-runner"] == "uvx"


def test_python_runner_invalid_warns(tmp_path: pathlib.Path) -> None:
    pyproject_content = """
[tool.pyfltr]
python-runner = "bogus"
"""
    (tmp_path / "pyproject.toml").write_text(pyproject_content)
    config = pyfltr.config.config.load_config(config_dir=tmp_path)
    assert _testconf.count_config_warnings("python-runner") >= 1
    assert config["python-runner"] == "uv"


@pytest.mark.parametrize(
    "value",
    ["python-runner", "js-runner", "bin-runner", "direct", "mise", "uv", "uvx", "pnpx", "pnpm", "npm", "npx", "yarn"],
)
def test_command_runner_validation_accepts_symmetric_12_values(tmp_path: pathlib.Path, value: str) -> None:
    (tmp_path / "pyproject.toml").write_text(f'[tool.pyfltr]\nmypy-runner = "{value}"\n')
    config = pyfltr.config.config.load_config(config_dir=tmp_path)
    assert config["mypy-runner"] == value


def test_mise_auto_trust_default() -> None:
    config = pyfltr.config.config.create_default_config()
    assert config["mise-auto-trust"] is True


def test_mise_auto_trust_disable(tmp_path: pathlib.Path) -> None:
    (tmp_path / "pyproject.toml").write_text("[tool.pyfltr]\nmise-auto-trust = false\n")
    config = pyfltr.config.config.load_config(config_dir=tmp_path)
    assert config["mise-auto-trust"] is False


def test_mise_auto_trust_invalid_type_warns(tmp_path: pathlib.Path) -> None:
    (tmp_path / "pyproject.toml").write_text('[tool.pyfltr]\nmise-auto-trust = "yes"\n')
    config = pyfltr.config.config.load_config(config_dir=tmp_path)
    assert _testconf.count_config_warnings("mise-auto-trust") >= 1
    assert config["mise-auto-trust"] is True


def test_preset_latest_suppresses_language_categories(tmp_path: pathlib.Path) -> None:
    (tmp_path / "pyproject.toml").write_text('[tool.pyfltr]\npreset = "latest"\n')
    config = pyfltr.config.config.load_config(config_dir=tmp_path)
    for cmd in _DOCS_ORTHOGONAL_KEYS:
        expected = cmd != "pre-commit"
        assert config[cmd] is expected, f"{cmd}のpreset=latest有効状態が不正"
    for category_key, _ in pyfltr.tools.LANGUAGE_CATEGORIES:
        _assert_language_gate(config, category_key, passed=False)


def test_javascript_true_with_individual_override(tmp_path: pathlib.Path) -> None:
    pyproject_content = """
[tool.pyfltr]
preset = "latest"
javascript = true
prettier = false
"""
    (tmp_path / "pyproject.toml").write_text(pyproject_content)
    config = pyfltr.config.config.load_config(config_dir=tmp_path)
    assert config["prettier"] is False
    assert config["eslint"] is True
    assert config["biome"] is True
    assert config["tsc"] is True


def test_individual_tool_enables_despite_category_false(tmp_path: pathlib.Path) -> None:
    pyproject_content = """
[tool.pyfltr]
preset = "latest"
eslint = true
cargo-fmt = true
"""
    (tmp_path / "pyproject.toml").write_text(pyproject_content)
    config = pyfltr.config.config.load_config(config_dir=tmp_path)
    assert config["eslint"] is True
    assert config["cargo-fmt"] is True
    assert config["prettier"] is False
    assert config["biome"] is False
    assert config["cargo-clippy"] is False


def test_language_categories_defaults_false() -> None:
    config = pyfltr.config.config.create_default_config()
    for category_key, _ in pyfltr.tools.LANGUAGE_CATEGORIES:
        assert config[category_key] is False, f"{category_key}の既定値はFalseであるべき"


def test_custom_command_pass_filenames(tmp_path: pathlib.Path) -> None:
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
    pyproject_content = """
[tool.pyfltr.custom-commands.my-checker]
type = "linter"
path = "my-checker"
"""
    (tmp_path / "pyproject.toml").write_text(pyproject_content)
    config = pyfltr.config.config.load_config(config_dir=tmp_path)
    assert config["my-checker-pass-filenames"] is True


def test_bin_tool_default_config_values() -> None:
    config = pyfltr.config.config.create_default_config()
    bin_tools = ["ec", "shellcheck", "shfmt", "actionlint", "pinact", "taplo", "hadolint"]
    for tool in bin_tools:
        assert config[tool] is False, f"{tool}は既定で無効"
        assert config[f"{tool}-path"] == "", f"{tool}-pathは空文字"
        assert config[f"{tool}-version"] == "latest", f"{tool}-versionはlatest"
        assert config[f"{tool}-fast"] is True, f"{tool}-fastはTrue"

    assert config["uv-sort"] is False
    assert config["uv-sort-path"] == ""
    assert config["uv-sort-runner"] == "python-runner"
    assert config["uv-sort-fast"] is True

    assert config["tsc-pass-filenames"] is False


def test_gitleaks_default_config_values() -> None:
    config = pyfltr.config.config.create_default_config()
    assert config["gitleaks"] is False, "gitleaksは既定で無効"
    assert config["gitleaks-path"] == "", "gitleaks-pathは空文字"
    assert config["gitleaks-args"] == ["detect", "--no-banner"], "gitleaks-argsはdetect --no-banner"
    assert config["gitleaks-pass-filenames"] is False, "gitleaks-pass-filenamesはFalse"
    assert config["gitleaks-version"] == "latest", "gitleaks-versionはlatest"
    assert config["gitleaks-fast"] is False, "gitleaks-fastはFalse"
    info = pyfltr.tools.BUILTIN_COMMANDS["gitleaks"]
    assert info.type == "linter"


def test_yamllint_default_config_values() -> None:
    config = pyfltr.config.config.create_default_config()
    assert config["yamllint"] is False, "yamllintは既定で無効"
    assert config["yamllint-path"] == "", "yamllint-pathは空文字列契約に揃える"
    assert config["yamllint-args"] == [], "yamllint-argsは空リスト"
    assert config["yamllint-fast"] is True, "yamllint-fastはTrue"
    info = pyfltr.tools.BUILTIN_COMMANDS["yamllint"]
    assert info.type == "linter"
    assert info.target_globs() == ["*.yaml", "*.yml"]


def test_typos_default_config_values() -> None:
    config = pyfltr.config.config.create_default_config()
    assert config["typos"] is False, "typosは既定で無効"
    assert config["typos-path"] == "", "typos-pathは空文字列契約に揃える"
    # typos-versionは既存ユーザーの設定との互換維持のため定義を残す
    assert config["typos-version"] == "latest", "typos-versionはlatest（互換維持）"
    assert config["typos-fast"] is True, "typos-fastはTrue"


def test_glab_ci_lint_default_config_values() -> None:
    config = pyfltr.config.config.create_default_config()
    assert config["glab-ci-lint"] is False, "GitLab API 認証必須のため opt-in"
    assert config["glab-ci-lint-path"] == ""
    assert config["glab-ci-lint-args"] == ["ci", "lint"]
    assert config["glab-ci-lint-version"] == "latest"
    assert config["glab-ci-lint-fast"] is False
    info = pyfltr.tools.BUILTIN_COMMANDS["glab-ci-lint"]
    assert info.type == "linter"
    assert info.target_globs() == [".gitlab-ci.yml"]


def test_builtin_targets_override_str(tmp_path: pathlib.Path) -> None:
    (tmp_path / "pyproject.toml").write_text('[tool.pyfltr]\nshfmt-targets = "*.bash"\n')
    config = pyfltr.config.config.load_config(config_dir=tmp_path)
    assert config.values["shfmt-targets"] == "*.bash"
    assert config.commands["shfmt"].targets == "*.bash"
    assert config.commands["shfmt"].target_globs() == ["*.bash"]


def test_builtin_targets_override_list(tmp_path: pathlib.Path) -> None:
    (tmp_path / "pyproject.toml").write_text('[tool.pyfltr]\nshfmt-targets = ["*.sh", "*.bash"]\n')
    config = pyfltr.config.config.load_config(config_dir=tmp_path)
    assert config.values["shfmt-targets"] == ["*.sh", "*.bash"]
    assert config.commands["shfmt"].targets == ["*.sh", "*.bash"]
    assert config.commands["shfmt"].target_globs() == ["*.sh", "*.bash"]


def test_builtin_extend_targets_str(tmp_path: pathlib.Path) -> None:
    (tmp_path / "pyproject.toml").write_text('[tool.pyfltr]\nshfmt-extend-targets = "*.bash"\n')
    config = pyfltr.config.config.load_config(config_dir=tmp_path)
    assert config.values["shfmt-extend-targets"] == "*.bash"
    assert config.commands["shfmt"].target_globs() == ["*.sh", "*.bash"]


def test_builtin_extend_targets_list(tmp_path: pathlib.Path) -> None:
    (tmp_path / "pyproject.toml").write_text('[tool.pyfltr]\nshfmt-extend-targets = ["*.bash", "dot_bashrc"]\n')
    config = pyfltr.config.config.load_config(config_dir=tmp_path)
    assert config.values["shfmt-extend-targets"] == ["*.bash", "dot_bashrc"]
    assert config.commands["shfmt"].target_globs() == [
        "*.sh",
        "*.bash",
        "dot_bashrc",
    ]


def test_builtin_targets_and_extend_targets(tmp_path: pathlib.Path) -> None:
    (tmp_path / "pyproject.toml").write_text(
        '[tool.pyfltr]\nshfmt-targets = ["*.bash"]\nshfmt-extend-targets = ["dot_bashrc"]\n'
    )
    config = pyfltr.config.config.load_config(config_dir=tmp_path)
    assert config.commands["shfmt"].target_globs() == ["*.bash", "dot_bashrc"]


def test_builtin_targets_unknown_command_warns(tmp_path: pathlib.Path) -> None:
    (tmp_path / "pyproject.toml").write_text('[tool.pyfltr]\nunknown-targets = "*.py"\n')
    config = pyfltr.config.config.load_config(config_dir=tmp_path)
    assert _testconf.count_config_warnings("unknown-targets") >= 1
    assert "unknown-targets" not in config.values
    assert "unknown" not in config.commands


def test_builtin_targets_no_mutation_of_builtins(tmp_path: pathlib.Path) -> None:
    original_targets = pyfltr.tools.BUILTIN_COMMANDS["shfmt"].targets
    (tmp_path / "pyproject.toml").write_text('[tool.pyfltr]\nshfmt-targets = "*.bash"\n')
    config = pyfltr.config.config.load_config(config_dir=tmp_path)
    assert config.commands["shfmt"].targets == "*.bash"
    assert pyfltr.tools.BUILTIN_COMMANDS["shfmt"].targets == original_targets


def test_builtin_extend_args_list(tmp_path: pathlib.Path) -> None:
    (tmp_path / "pyproject.toml").write_text(
        '[tool.pyfltr]\nlychee-extend-args = ["--exclude=github\\\\.com/owner/repo/actions"]\n'
    )
    config = pyfltr.config.config.load_config(config_dir=tmp_path)
    assert config["lychee-args"] == ["--format", "json", "--no-progress"]
    assert config["lychee-extend-args"] == ["--exclude=github\\.com/owner/repo/actions"]


def test_builtin_args_and_extend_args(tmp_path: pathlib.Path) -> None:
    (tmp_path / "pyproject.toml").write_text(
        "[tool.pyfltr]\n"
        'lychee-args = ["--format", "json", "--no-progress", "--max-concurrency=5"]\n'
        'lychee-extend-args = ["--exclude=example\\\\.com"]\n'
    )
    config = pyfltr.config.config.load_config(config_dir=tmp_path)
    assert config["lychee-args"] == ["--format", "json", "--no-progress", "--max-concurrency=5"]
    assert config["lychee-extend-args"] == ["--exclude=example\\.com"]


def test_builtin_extend_args_unknown_command_warns(tmp_path: pathlib.Path) -> None:
    (tmp_path / "pyproject.toml").write_text('[tool.pyfltr]\nunknown-extend-args = ["--flag"]\n')
    config = pyfltr.config.config.load_config(config_dir=tmp_path)
    assert _testconf.count_config_warnings("unknown-extend-args") >= 1
    assert "unknown-extend-args" not in config.values
    assert "unknown" not in config.commands


@pytest.mark.parametrize(
    "value_repr",
    [
        '"--offline"',
        "42",
        '[1, "--offline"]',
    ],
)
def test_builtin_extend_args_invalid_type_warns(tmp_path: pathlib.Path, value_repr: str) -> None:
    (tmp_path / "pyproject.toml").write_text(f"[tool.pyfltr]\nlychee-extend-args = {value_repr}\n")
    config = pyfltr.config.config.load_config(config_dir=tmp_path)
    assert _testconf.count_config_warnings("lychee-extend-args") >= 1
    assert config.values["lychee-extend-args"] == []


def test_builtin_extend_args_preserves_tilde(tmp_path: pathlib.Path) -> None:
    (tmp_path / "pyproject.toml").write_text('[tool.pyfltr]\nlychee-extend-args = ["--config=~/lychee.toml"]\n')
    config = pyfltr.config.config.load_config(config_dir=tmp_path)
    assert config["lychee-extend-args"] == ["--config=~/lychee.toml"]


def test_custom_command_extend_args(tmp_path: pathlib.Path) -> None:
    pyproject_content = """
[tool.pyfltr.custom-commands.mytool]
type = "linter"
path = "mytool"
args = ["--default"]
extend-args = ["--extra=foo"]
"""
    (tmp_path / "pyproject.toml").write_text(pyproject_content)
    config = pyfltr.config.config.load_config(config_dir=tmp_path)
    assert "mytool" in config.commands
    assert config["mytool-args"] == ["--default"]
    assert config["mytool-extend-args"] == ["--extra=foo"]


def test_custom_command_extend_args_default_empty(tmp_path: pathlib.Path) -> None:
    pyproject_content = """
[tool.pyfltr.custom-commands.mytool]
type = "linter"
path = "mytool"
"""
    (tmp_path / "pyproject.toml").write_text(pyproject_content)
    config = pyfltr.config.config.load_config(config_dir=tmp_path)
    assert config["mytool-extend-args"] == []


def test_custom_command_extend_args_invalid_type_warns(tmp_path: pathlib.Path) -> None:
    pyproject_content = """
[tool.pyfltr.custom-commands.mytool]
type = "linter"
path = "mytool"
extend-args = "not-a-list"
"""
    (tmp_path / "pyproject.toml").write_text(pyproject_content)
    config = pyfltr.config.config.load_config(config_dir=tmp_path)
    assert _testconf.count_config_warnings("extend-args") >= 1
    assert "mytool" not in config.commands


_NATIVE_LANG_TOOLS: tuple[str, ...] = pyfltr.tools.RUST_COMMANDS + pyfltr.tools.DOTNET_COMMANDS


def test_native_lang_tools_registered() -> None:
    config = pyfltr.config.config.create_default_config()
    for tool in _NATIVE_LANG_TOOLS:
        assert tool in pyfltr.tools.BUILTIN_COMMANDS, f"{tool}がBUILTIN_COMMANDSに未登録"
        assert config[tool] is False, f"{tool}の既定値はFalseであるべき"
        assert config[f"{tool}-path"] == "", f"{tool}-pathは既定で空文字であるべき"
        assert config[f"{tool}-runner"] == "bin-runner", f"{tool}-runnerの既定値は'bin-runner'であるべき"
        assert config[f"{tool}-version"] == "latest", f"{tool}-versionの既定値は'latest'であるべき"


def test_native_lang_tools_pass_filenames_false() -> None:
    config = pyfltr.config.config.create_default_config()
    for tool in _NATIVE_LANG_TOOLS:
        assert config[f"{tool}-pass-filenames"] is False, f"{tool}-pass-filenamesはFalseであるべき"


def test_native_formatters_write_by_default() -> None:
    config = pyfltr.config.config.create_default_config()
    assert config["cargo-fmt-args"] == ["fmt"]
    assert config["dotnet-format-args"] == ["format"]
    assert "cargo-fmt-fix-args" not in config.values
    assert "dotnet-format-fix-args" not in config.values


def test_cargo_clippy_args_separation() -> None:
    config = pyfltr.config.config.create_default_config()
    assert config["cargo-clippy-args"] == _testconf.CARGO_CLIPPY_ARGS
    assert config["cargo-clippy-lint-args"] == _testconf.CARGO_CLIPPY_LINT_ARGS
    assert config["cargo-clippy-fix-args"] == _testconf.CARGO_CLIPPY_FIX_ARGS


def test_native_lang_tools_fast_defaults() -> None:
    config = pyfltr.config.config.create_default_config()
    assert config["cargo-fmt-fast"] is True
    assert config["cargo-clippy-fast"] is True
    assert config["dotnet-format-fast"] is True
    for tool in ("cargo-check", "cargo-test", "cargo-deny", "dotnet-build", "dotnet-test"):
        assert config[f"{tool}-fast"] is False, f"{tool}-fastは既定Falseであるべき"


def test_native_lang_tools_not_affected_by_python(tmp_path: pathlib.Path) -> None:
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
    assert config["mypy"] is True
    assert config["pytest"] is True


def test_native_lang_tools_in_aliases() -> None:
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
    (tmp_path / "pyproject.toml").write_text('[tool.pyfltr]\nmypy-exclude = ["vendor", "gen_*.py"]\n')
    config = pyfltr.config.config.load_config(config_dir=tmp_path)
    assert config.values["mypy-exclude"] == ["vendor", "gen_*.py"]


def test_tool_exclude_unknown_command_warns(tmp_path: pathlib.Path) -> None:
    (tmp_path / "pyproject.toml").write_text('[tool.pyfltr]\nunknown-exclude = ["foo"]\n')
    config = pyfltr.config.config.load_config(config_dir=tmp_path)
    assert _testconf.count_config_warnings("unknown-exclude") >= 1
    assert "unknown-exclude" not in config.values


def test_tool_exclude_invalid_type_warns(tmp_path: pathlib.Path) -> None:
    (tmp_path / "pyproject.toml").write_text("[tool.pyfltr]\nmypy-exclude = 42\n")
    config = pyfltr.config.config.load_config(config_dir=tmp_path)
    assert _testconf.count_config_warnings("str型のリスト") >= 1
    assert config.values["mypy-exclude"] == []


class TestGlobalConfig:
    @staticmethod
    def _setup(
        tmp_path: pathlib.Path,
        *,
        global_text: str | None = None,
        project_text: str | None = None,
    ) -> tuple[pathlib.Path, pathlib.Path]:
        global_path = tmp_path / "global_config.toml"
        if global_text is not None:
            global_path.write_text(global_text, encoding="utf-8")
        project_dir = tmp_path / "project"
        project_dir.mkdir()
        if project_text is not None:
            (project_dir / "pyproject.toml").write_text(project_text, encoding="utf-8")
        return global_path, project_dir

    def test_global_only_archive_key_applies(self, tmp_path: pathlib.Path) -> None:
        global_path, project_dir = self._setup(
            tmp_path,
            global_text="[tool.pyfltr]\narchive-max-age-days = 7\n",
            project_text="[tool.pyfltr]\n",
        )
        config = pyfltr.config.config.load_config(config_dir=project_dir, global_config_path=global_path)
        assert config["archive-max-age-days"] == 7

    def test_global_wins_archive_with_warning(self, tmp_path: pathlib.Path) -> None:
        global_path, project_dir = self._setup(
            tmp_path,
            global_text="[tool.pyfltr]\narchive-max-age-days = 7\n",
            project_text="[tool.pyfltr]\narchive-max-age-days = 14\n",
        )
        config = pyfltr.config.config.load_config(config_dir=project_dir, global_config_path=global_path)
        assert config["archive-max-age-days"] == 7
        assert _count_config_warnings("archive-max-age-days") == 1
        priority_warning = next(w for w in pyfltr.warnings_.collected_warnings() if "archive-max-age-days" in w["message"])
        assert "pyfltr config set --global" in priority_warning["hint"]

    def test_project_wins_normal_key_no_warning(self, tmp_path: pathlib.Path) -> None:
        global_path, project_dir = self._setup(
            tmp_path,
            global_text='[tool.pyfltr]\njs-runner = "pnpm"\n',
            project_text='[tool.pyfltr]\njs-runner = "npm"\n',
        )
        config = pyfltr.config.config.load_config(config_dir=project_dir, global_config_path=global_path)
        assert config["js-runner"] == "npm"
        assert _count_config_warnings("") == 0

    def test_global_config_missing_path_acts_as_empty(self, tmp_path: pathlib.Path) -> None:
        global_path, project_dir = self._setup(
            tmp_path,
            project_text='[tool.pyfltr]\njs-runner = "npm"\n',
        )
        assert not global_path.exists()
        config = pyfltr.config.config.load_config(config_dir=project_dir, global_config_path=global_path)
        assert config["js-runner"] == "npm"

    def test_global_config_invalid_toml_raises(self, tmp_path: pathlib.Path) -> None:
        global_path, project_dir = self._setup(
            tmp_path,
            global_text="[tool.pyfltr\n",
            project_text="[tool.pyfltr]\n",
        )
        with pytest.raises(ValueError, match="TOML"):
            pyfltr.config.config.load_config(config_dir=project_dir, global_config_path=global_path)

    def test_global_preset_applied(self, tmp_path: pathlib.Path) -> None:
        global_path, project_dir = self._setup(
            tmp_path,
            global_text='[tool.pyfltr]\npreset = "latest"\n',
            project_text="[tool.pyfltr]\n",
        )
        config = pyfltr.config.config.load_config(config_dir=project_dir, global_config_path=global_path)
        assert config["textlint"] is True
        assert config["markdownlint"] is True

    def test_global_language_gate_applied(self, tmp_path: pathlib.Path) -> None:
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
        global_text = """
[tool.pyfltr.custom-commands.colloquial]
type = "linter"
path = "uv"
args = ["run", "--script", "~/dotfiles/agent-basics/skills/writing-standards/scripts/check_colloquial.py"]
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
        assert config["colloquial-args"] == [
            "run",
            "--script",
            "~/dotfiles/agent-basics/skills/writing-standards/scripts/check_colloquial.py",
        ]

    def test_global_unknown_key_warns_no_error(self, tmp_path: pathlib.Path) -> None:
        global_path, project_dir = self._setup(
            tmp_path,
            global_text="[tool.pyfltr]\nfuture-only-key = 1\n",
            project_text="[tool.pyfltr]\n",
        )
        config = pyfltr.config.config.load_config(config_dir=project_dir, global_config_path=global_path)
        assert "future-only-key" not in config.values
        assert _count_config_warnings("future-only-key") == 1

    def test_unknown_key_in_both_warns(self, tmp_path: pathlib.Path) -> None:
        global_path, project_dir = self._setup(
            tmp_path,
            global_text="[tool.pyfltr]\nfuture-only-key = 1\n",
            project_text="[tool.pyfltr]\nfuture-only-key = 2\n",
        )
        config = pyfltr.config.config.load_config(config_dir=project_dir, global_config_path=global_path)
        assert "future-only-key" not in config.values
        assert _count_config_warnings("future-only-key") == 1

    def test_no_pyproject_with_global_only(self, tmp_path: pathlib.Path) -> None:
        global_path = tmp_path / "global_config.toml"
        global_path.write_text("[tool.pyfltr]\narchive-max-age-days = 5\n", encoding="utf-8")
        project_dir = tmp_path / "project_no_pyproject"
        project_dir.mkdir()
        config = pyfltr.config.config.load_config(config_dir=project_dir, global_config_path=global_path)
        assert config["archive-max-age-days"] == 5

    @pytest.mark.parametrize(
        ("global_text", "warning_fragment"),
        [
            ("[tool.pyfltr]\nunknown-key-xyz = true\n", "unknown-key-xyz"),
            ("[tool.pyfltr]\npreset = 42\n", "preset"),
            ("[tool.pyfltr]\npython = 42\n", "python"),
            ("[tool.pyfltr]\nshfmt-targets = 42\n", "shfmt-targets"),
            ('[tool.pyfltr]\npython-runner = "invalid"\n', "python-runner"),
            ("[tool.pyfltr]\ncustom-commands = 42\n", "custom-commands"),
            (
                '[tool.pyfltr.custom-commands.my-tool]\ntype = "invalid"\n',
                "my-tool",
            ),
        ],
    )
    def test_for_subproject_suppresses_global_only_validation_warning(
        self,
        tmp_path: pathlib.Path,
        global_text: str,
        warning_fragment: str,
    ) -> None:
        global_path, root_dir = self._setup(
            tmp_path,
            global_text=global_text,
            project_text="[tool.pyfltr]\n",
        )
        origin_config = pyfltr.config.config.load_config(config_dir=root_dir, global_config_path=global_path)

        pyfltr.config.config.load_config(
            config_dir=root_dir,
            global_config_path=global_path,
            for_subproject=True,
            suppressed_warning_entries=origin_config.warned_global_only_entries,
        )

        assert _count_config_warnings(warning_fragment) == 1

    def test_for_subproject_keeps_project_validation_warning(self, tmp_path: pathlib.Path) -> None:
        global_path, root_dir = self._setup(
            tmp_path,
            global_text="[tool.pyfltr]\nunknown-key-xyz = true\n",
            project_text="[tool.pyfltr]\n",
        )
        origin_config = pyfltr.config.config.load_config(config_dir=root_dir, global_config_path=global_path)
        sub_dir = tmp_path / "pkg"
        sub_dir.mkdir()
        (sub_dir / "pyproject.toml").write_text("[tool.pyfltr]\nunknown-key-xyz = true\n", encoding="utf-8")

        pyfltr.config.config.load_config(
            config_dir=sub_dir,
            global_config_path=global_path,
            for_subproject=True,
            suppressed_warning_entries=origin_config.warned_global_only_entries,
        )

        assert _count_config_warnings("unknown-key-xyz") == 2

    def test_origin_override_of_invalid_global_value_still_warns_in_subproject(self, tmp_path: pathlib.Path) -> None:
        global_path, root_dir = self._setup(
            tmp_path,
            global_text="[tool.pyfltr]\npreset = 42\n",
            project_text='[tool.pyfltr]\npreset = "latest"\n',
        )
        origin_config = pyfltr.config.config.load_config(config_dir=root_dir, global_config_path=global_path)
        assert _count_config_warnings("preset") == 0
        assert not any(key == "preset" for key, _ in origin_config.warned_global_only_entries)

        sub_dir = tmp_path / "pkg"
        sub_dir.mkdir()
        (sub_dir / "pyproject.toml").write_text("[tool.pyfltr]\n", encoding="utf-8")

        pyfltr.config.config.load_config(
            config_dir=sub_dir,
            global_config_path=global_path,
            for_subproject=True,
            suppressed_warning_entries=origin_config.warned_global_only_entries,
        )

        assert _count_config_warnings("preset") == 1

    def test_origin_override_of_invalid_global_value_warns_once_across_multiple_subprojects(
        self, tmp_path: pathlib.Path
    ) -> None:
        global_path, root_dir = self._setup(
            tmp_path,
            global_text="[tool.pyfltr]\npreset = 42\n",
            project_text='[tool.pyfltr]\npreset = "latest"\n',
        )
        origin_config = pyfltr.config.config.load_config(config_dir=root_dir, global_config_path=global_path)
        already_warned: set[pyfltr.config.model.ConfigWarningEntry] = set(origin_config.warned_global_only_entries)

        for name in ("pkg_a", "pkg_b"):
            sub_dir = tmp_path / name
            sub_dir.mkdir()
            (sub_dir / "pyproject.toml").write_text("[tool.pyfltr]\n", encoding="utf-8")
            sub_config = pyfltr.config.config.load_config(
                config_dir=sub_dir,
                global_config_path=global_path,
                for_subproject=True,
                suppressed_warning_entries=frozenset(already_warned),
            )
            already_warned |= sub_config.warned_global_only_entries

        assert _count_config_warnings("preset") == 1

    def test_flat_override_of_custom_command_derived_key_still_warns(self, tmp_path: pathlib.Path) -> None:
        global_text = (
            "[tool.pyfltr.custom-commands.invalid-a]\n"
            'type = "invalid"\n'
            "[tool.pyfltr.custom-commands.invalid-b]\n"
            'type = "invalid"\n'
            "[tool.pyfltr.custom-commands.my-tool]\n"
            'type = "linter"\n'
        )
        global_path, root_dir = self._setup(
            tmp_path,
            global_text=global_text,
            project_text="[tool.pyfltr]\n",
        )
        origin_config = pyfltr.config.config.load_config(config_dir=root_dir, global_config_path=global_path)
        assert any(key == "custom-commands" for key, _ in origin_config.warned_global_only_entries)
        assert _count_config_warnings("invalid-a") == 1
        assert _count_config_warnings("invalid-b") == 1

        sub_dir = tmp_path / "pkg"
        sub_dir.mkdir()
        (sub_dir / "pyproject.toml").write_text('[tool.pyfltr]\nmy-tool-severity = "invalid"\n', encoding="utf-8")

        pyfltr.config.config.load_config(
            config_dir=sub_dir,
            global_config_path=global_path,
            for_subproject=True,
            suppressed_warning_entries=origin_config.warned_global_only_entries,
        )

        assert _count_config_warnings("my-tool-severity") == 1

    def test_same_key_with_different_cause_is_not_suppressed(self, tmp_path: pathlib.Path) -> None:
        global_path, root_dir = self._setup(
            tmp_path,
            global_text=(
                '[tool.pyfltr]\nmy-tool-severity = "invalid"\n'
                '[tool.pyfltr.custom-commands.my-tool]\ntype = "linter"\ncommandline = ["echo"]\n'
            ),
            project_text="[tool.pyfltr]\n",
        )
        origin_config = pyfltr.config.config.load_config(config_dir=root_dir, global_config_path=global_path)
        assert _count_config_warnings("値が不正") == 1

        sub_dir = tmp_path / "pkg"
        sub_dir.mkdir()
        (sub_dir / "pyproject.toml").write_text(
            '[tool.pyfltr.custom-commands.other-tool]\ntype = "linter"\ncommandline = ["echo"]\n', encoding="utf-8"
        )

        pyfltr.config.config.load_config(
            config_dir=sub_dir,
            global_config_path=global_path,
            for_subproject=True,
            suppressed_warning_entries=origin_config.warned_global_only_entries,
        )

        assert _count_config_warnings("認識できません") == 1
        assert _count_config_warnings("値が不正") == 1

    def test_independent_project_misconfigurations_warn_per_subproject(self, tmp_path: pathlib.Path) -> None:
        global_path, root_dir = self._setup(
            tmp_path,
            global_text="[tool.pyfltr]\n",
            project_text="[tool.pyfltr]\n",
        )
        origin_config = pyfltr.config.config.load_config(config_dir=root_dir, global_config_path=global_path)
        already_warned: set[pyfltr.config.model.ConfigWarningEntry] = set(origin_config.warned_global_only_entries)

        for name in ("pkg_a", "pkg_b"):
            sub_dir = tmp_path / name
            sub_dir.mkdir()
            (sub_dir / "pyproject.toml").write_text("[tool.pyfltr]\nunknown-key-xyz = true\n", encoding="utf-8")
            sub_config = pyfltr.config.config.load_config(
                config_dir=sub_dir,
                global_config_path=global_path,
                for_subproject=True,
                suppressed_warning_entries=frozenset(already_warned),
            )
            already_warned |= sub_config.warned_global_only_entries

        assert _count_config_warnings("unknown-key-xyz") == 2


_count_config_warnings = _testconf.count_config_warnings


class TestErrorMessages:
    @pytest.mark.parametrize(
        "key,expect_suggestion",
        [
            ("python-runer", True),
            ("totally-unrelated-key", False),
        ],
    )
    def test_unknown_key_message_with_or_without_suggestion(
        self, tmp_path: pathlib.Path, key: str, expect_suggestion: bool
    ) -> None:
        (tmp_path / "pyproject.toml").write_text(f"[tool.pyfltr]\n{key} = 1\n")
        pyfltr.config.config.load_config(config_dir=tmp_path)
        message = _find_config_warning(key)
        assert message is not None
        assert f"`{key}`" in message
        assert "pyfltr config list --all" in message
        if expect_suggestion:
            assert "もしかして:" in message
        else:
            assert "もしかして:" not in message

    def test_format_unknown_key_message_public(self) -> None:
        message = pyfltr.config.validation.format_unknown_key_message(
            "python-runer",
            ["python-runner", "js-runner", "bin-runner"],
        )
        assert "`python-runer`" in message
        assert "もしかして: python-runner" in message
        assert "pyfltr config list --all" in message

    @pytest.mark.parametrize(
        "value,expected_actual_label",
        [
            ("'string'", "文字列"),
            ("1", "整数"),
            ('["a"]', "リスト"),
        ],
    )
    def test_type_mismatch_japanese_label(self, tmp_path: pathlib.Path, value: str, expected_actual_label: str) -> None:
        (tmp_path / "pyproject.toml").write_text(f"[tool.pyfltr]\nmypy = {value}\n")
        config = pyfltr.config.config.load_config(config_dir=tmp_path)
        message = _find_config_warning("`mypy`")
        assert message is not None
        assert "期待 真偽値" in message
        assert f"実値 {expected_actual_label}" in message
        assert config["mypy"] is False

    def test_type_mismatch_list_label(self, tmp_path: pathlib.Path) -> None:
        (tmp_path / "pyproject.toml").write_text("[tool.pyfltr]\nmypy-args = 1\n")
        config = pyfltr.config.config.load_config(config_dir=tmp_path)
        message = _find_config_warning("`mypy-args`")
        assert message is not None
        assert "期待 リスト" in message
        assert config["mypy-args"] == []

    def test_parse_config_value_bool_message(self) -> None:
        with pytest.raises(ValueError, match=r"true / false / 1 / 0"):
            pyfltr.config.editing.parse_config_value("mypy", "on")

    def test_parse_config_value_int_message(self) -> None:
        with pytest.raises(ValueError, match="整数を指定してください"):
            pyfltr.config.editing.parse_config_value("jobs", "many")

    def test_parse_config_value_unknown_key_suggestion(self) -> None:
        with pytest.raises(ValueError) as exc_info:
            pyfltr.config.editing.parse_config_value("python-runer", "uv")
        message = str(exc_info.value)
        assert "pyfltr config list --all" in message

    def test_preset_unknown_value_suggests_candidate(self, tmp_path: pathlib.Path) -> None:
        (tmp_path / "pyproject.toml").write_text('[tool.pyfltr]\npreset = "latset"\n')
        pyfltr.config.config.load_config(config_dir=tmp_path)
        message = _find_config_warning("preset")
        assert message is not None
        assert "もしかして: latest" in message
        assert "presetを適用せずに続行しました" in message

    def test_runner_invalid_value_contains_allowed_values(self, tmp_path: pathlib.Path) -> None:
        (tmp_path / "pyproject.toml").write_text('[tool.pyfltr]\npython-runner = "bogus"\n')
        pyfltr.config.config.load_config(config_dir=tmp_path)
        message = _find_config_warning("python-runner")
        assert message is not None
        assert "許容値:" in message
        assert "uv" in message and "uvx" in message and "direct" in message
        assert f"既定値 {pyfltr.config.model.DEFAULT_CONFIG['python-runner']!r} で続行しました" in message

    def test_custom_command_name_collision_guides_alternatives(self, tmp_path: pathlib.Path) -> None:
        (tmp_path / "pyproject.toml").write_text('[tool.pyfltr.custom-commands.mypy]\ntype = "linter"\n', encoding="utf-8")
        config = pyfltr.config.config.load_config(config_dir=tmp_path)
        message = _find_config_warning("カスタムコマンド `mypy`")
        assert message is not None
        assert "登録をスキップしました" in message
        assert "`mypy-args`" in message
        assert config.commands["mypy"].builtin is True

    def test_custom_command_invalid_definition_reports_skip(self, tmp_path: pathlib.Path) -> None:
        (tmp_path / "pyproject.toml").write_text(
            '[tool.pyfltr.custom-commands.mylinter]\ntype = "linter"\nargs = "not-a-list"\n', encoding="utf-8"
        )
        config = pyfltr.config.config.load_config(config_dir=tmp_path)
        message = _find_config_warning("カスタムコマンド `mylinter`")
        assert message is not None
        assert "このカスタムコマンドの登録をスキップしました" in message
        assert "mylinter" not in config.commands

    def test_unreadable_pyproject_reports_path_and_permission(
        self, tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        pyproject = tmp_path / "pyproject.toml"
        pyproject.write_text("[tool.pyfltr]\n", encoding="utf-8")
        original_read_text = pathlib.Path.read_text

        def _raise_for_pyproject(path: pathlib.Path, *args: typing.Any, **kwargs: typing.Any) -> str:
            if path == pyproject:
                raise PermissionError(13, "Permission denied")
            return original_read_text(path, *args, **kwargs)

        monkeypatch.setattr(pathlib.Path, "read_text", _raise_for_pyproject)
        with pytest.raises(ValueError) as exc_info:
            pyfltr.config.config.load_config(config_dir=tmp_path)
        message = str(exc_info.value)
        assert str(pyproject) in message
        assert "読み取り権限を確認してください" in message

    def test_parse_config_value_dict_key_guides_manual_edit(self) -> None:
        with pytest.raises(ValueError, match="pyproject.tomlを直接編集してください"):
            pyfltr.config.editing.parse_config_value("aliases", "x")


def test_prek_invocation_pins_config_path() -> None:
    config = pyfltr.config.config.create_default_config()
    args = pyfltr.command.invocation.build_invocation_argv("prek", config, ["prek"], [], fix_stage=False)
    assert "--config=.pre-commit-config.yaml" in args
