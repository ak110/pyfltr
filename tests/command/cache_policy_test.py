import pathlib

import pyfltr.command.cache_policy
import pyfltr.command.core_
import pyfltr.config.config
import pyfltr.state.cache


def test_is_cacheable_true_for_textlint() -> None:
    """textlint は cacheable=True。"""
    config = pyfltr.config.config.create_default_config()
    assert pyfltr.command.cache_policy.is_cacheable("textlint", config, additional_args=[])


def test_is_cacheable_false_for_mypy() -> None:
    """cacheable=Falseのツール（mypyなど）は対象外。"""
    config = pyfltr.config.config.create_default_config()
    assert not pyfltr.command.cache_policy.is_cacheable("mypy", config, additional_args=[])


def test_is_cacheable_false_with_config_arg() -> None:
    """`--{command}-args`に`--config`を含む場合は対象外。"""
    config = pyfltr.config.config.create_default_config()
    assert not pyfltr.command.cache_policy.is_cacheable("textlint", config, additional_args=["--config", "/tmp/t.json"])
    assert not pyfltr.command.cache_policy.is_cacheable("textlint", config, additional_args=["--config=/tmp/t.json"])


def test_is_cacheable_false_with_ignore_path_arg() -> None:
    """`--{command}-args`に`--ignore-path`を含む場合は対象外。"""
    config = pyfltr.config.config.create_default_config()
    assert not pyfltr.command.cache_policy.is_cacheable("textlint", config, additional_args=["--ignore-path", "/tmp/i"])
    assert not pyfltr.command.cache_policy.is_cacheable("textlint", config, additional_args=["--ignore-path=/tmp/i"])


def test_resolve_config_files_textlint() -> None:
    """textlintのconfig_filesが完全列挙される。"""
    config = pyfltr.config.config.create_default_config()
    files = pyfltr.command.cache_policy.resolve_config_files("textlint", config, base=pathlib.Path("/tmp"))
    assert pathlib.Path("/tmp/.textlintrc") in files
    assert pathlib.Path("/tmp/.textlintignore") in files
    assert pathlib.Path("/tmp/package.json") in files


def test_resolve_config_files_separates_injected_and_implicit_bases(tmp_path: pathlib.Path) -> None:
    """注入設定は実在パス、暗黙設定は実効cwdを基準に列挙する。"""
    start_cwd = tmp_path / "start"
    effective_cwd = tmp_path / "effective"
    start_cwd.mkdir()
    effective_cwd.mkdir()
    injected = start_cwd / ".textlintrc"
    injected.write_text('{"rules": {}}\n', encoding="utf-8")
    config = pyfltr.config.config.create_default_config()

    files = pyfltr.command.cache_policy.resolve_config_files(
        "textlint",
        config,
        base=effective_cwd,
        injected_config_path=injected,
    )

    assert files[0] == injected
    assert effective_cwd / ".textlintignore" in files
    assert effective_cwd / "package.json" in files
    assert effective_cwd / ".textlintrc" not in files
