import os
import pathlib
import shutil
import subprocess

import pytest

import pyfltr.command.core_
import pyfltr.command.dispatcher
import pyfltr.command.env
import pyfltr.command.glab
import pyfltr.command.mise
import pyfltr.command.only_failed
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
import pyfltr.config.model
import pyfltr.paths
import pyfltr.run_options
import pyfltr.state.cache
import pyfltr.state.only_failed
import pyfltr.tools
import pyfltr.warnings_


def test_build_subprocess_env_sets_supply_chain_defaults(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("UV_EXCLUDE_NEWER", raising=False)
    monkeypatch.delenv("NPM_CONFIG_MINIMUM_RELEASE_AGE", raising=False)

    config = pyfltr.config.config.create_default_config()
    env = pyfltr.command.env.build_subprocess_env(config, "pytest")

    assert env["UV_EXCLUDE_NEWER"] == "1 day"
    assert env["NPM_CONFIG_MINIMUM_RELEASE_AGE"] == "1440"


def test_build_subprocess_env_sets_python_utf8_mode() -> None:
    config = pyfltr.config.config.create_default_config()
    env = pyfltr.command.env.build_subprocess_env(config, "pytest")

    assert env["PYTHONUTF8"] == "1"


def test_build_subprocess_env_preserves_existing_supply_chain_values(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("UV_EXCLUDE_NEWER", "1 week")
    monkeypatch.setenv("NPM_CONFIG_MINIMUM_RELEASE_AGE", "10080")

    config = pyfltr.config.config.create_default_config()
    env = pyfltr.command.env.build_subprocess_env(config, "pytest")

    assert env["UV_EXCLUDE_NEWER"] == "1 week"
    assert env["NPM_CONFIG_MINIMUM_RELEASE_AGE"] == "10080"


def test_build_subprocess_env_via_mise_strips_mise_tool_paths(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(
        "PATH",
        os.pathsep.join(
            [
                "/home/u/.local/share/mise/installs/dotnet/10.0.0",
                "/home/u/.local/share/mise/dotnet-root",
                "/home/u/.local/share/mise/shims",
                "/home/u/.local/share/mise/bin",
                "/usr/bin",
            ]
        ),
    )

    config = pyfltr.config.config.create_default_config()
    env = pyfltr.command.env.build_subprocess_env(config, "dotnet-build", via_mise=True)
    entries = env["PATH"].split(os.pathsep)

    assert "/home/u/.local/share/mise/installs/dotnet/10.0.0" not in entries
    assert "/home/u/.local/share/mise/dotnet-root" not in entries
    assert "/home/u/.local/share/mise/shims" not in entries
    assert "/home/u/.local/share/mise/bin" in entries
    assert "/usr/bin" in entries


def test_build_subprocess_env_default_keeps_mise_tool_paths(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(
        "PATH",
        os.pathsep.join(["/home/u/.local/share/mise/installs/dotnet/10.0.0", "/usr/bin"]),
    )

    config = pyfltr.config.config.create_default_config()
    env = pyfltr.command.env.build_subprocess_env(config, "ruff-check")
    entries = env["PATH"].split(os.pathsep)

    assert "/home/u/.local/share/mise/installs/dotnet/10.0.0" in entries
    assert "/usr/bin" in entries


def test_build_subprocess_env_uv_runner_drops_virtual_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("VIRTUAL_ENV", "/repo/.venv")

    config = pyfltr.config.config.create_default_config()
    env = pyfltr.command.env.build_subprocess_env(config, "pytest", effective_runner="uv")

    assert "VIRTUAL_ENV" not in env


@pytest.mark.parametrize("effective_runner", [None, "direct", "uvx", "mise", "pnpm"])
def test_build_subprocess_env_keeps_virtual_env_for_other_runners(
    monkeypatch: pytest.MonkeyPatch, effective_runner: str | None
) -> None:
    monkeypatch.setenv("VIRTUAL_ENV", "/repo/.venv")

    config = pyfltr.config.config.create_default_config()
    env = pyfltr.command.env.build_subprocess_env(config, "pytest", effective_runner=effective_runner)

    assert env["VIRTUAL_ENV"] == "/repo/.venv"


def test_build_subprocess_env_npm_config_actually_effective(monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path) -> None:
    original_home = pathlib.Path(os.environ.get("HOME") or os.environ["USERPROFILE"])
    mise_config = original_home / ".config" / "mise" / "config.toml"
    monkeypatch.setenv("MISE_TRUSTED_CONFIG_PATHS", str(mise_config))
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / ".config"))
    monkeypatch.setenv("NPM_CONFIG_MINIMUM_RELEASE_AGE", "4321")

    config = pyfltr.config.config.create_default_config()
    env = pyfltr.command.env.build_subprocess_env(config, "markdownlint")
    assert env["NPM_CONFIG_MINIMUM_RELEASE_AGE"] == "4321"

    # Windowsではnpmがnpm.cmdとして提供されるため、shutil.whichで完全パスを取得する
    npm_path = shutil.which("npm")
    assert npm_path is not None
    proc = subprocess.run(
        [npm_path, "config", "get", "minimum-release-age"],
        env=env,
        capture_output=True,
        text=True,
        check=True,
    )
    assert proc.stdout.strip() == "4321"


def test_get_env_path_windows_uses_case_insensitive_key(monkeypatch) -> None:
    monkeypatch.setattr("pyfltr.command.env.os.name", "nt")
    assert pyfltr.command.env.get_env_path({"Path": "/tmp/bin"}) == "/tmp/bin"
    assert pyfltr.command.env.get_env_path({"path": "/tmp/bin"}) == "/tmp/bin"
    assert pyfltr.command.env.get_env_path({"PATH": "/tmp/bin"}) == "/tmp/bin"


def test_get_env_path_posix_strict_key(monkeypatch) -> None:
    monkeypatch.setattr("pyfltr.command.env.os.name", "posix")
    assert pyfltr.command.env.get_env_path({"Path": "/tmp/bin"}) is None
    assert pyfltr.command.env.get_env_path({"PATH": "/usr/bin"}) == "/usr/bin"
    assert pyfltr.command.env.get_env_path({"Path": "/tmp/bin", "PATH": "/usr/bin"}) == "/usr/bin"


def test_dedupe_environ_path_posix_trailing_slash_treated_as_duplicate(monkeypatch) -> None:
    monkeypatch.setattr("pyfltr.command.env.os.name", "posix")
    monkeypatch.setattr("pyfltr.command.env.os.pathsep", ":")
    env: dict[str, str] = {"PATH": ":".join(["/usr/bin", "/opt/bin", "/usr/bin", "/usr/local/bin", "/opt/bin/"])}
    assert pyfltr.command.env.dedupe_environ_path(env) is True
    assert env["PATH"] == ":".join(["/usr/bin", "/opt/bin", "/usr/local/bin"])


def test_dedupe_environ_path_posix_case_sensitive(monkeypatch) -> None:
    monkeypatch.setattr("pyfltr.command.env.os.name", "posix")
    monkeypatch.setattr("pyfltr.command.env.os.pathsep", ":")
    env: dict[str, str] = {"PATH": ":".join(["/usr/bin", "/USR/Bin"])}
    assert pyfltr.command.env.dedupe_environ_path(env) is False
    assert env["PATH"] == ":".join(["/usr/bin", "/USR/Bin"])


def test_dedupe_environ_path_windows_case_insensitive(monkeypatch) -> None:
    monkeypatch.setattr("pyfltr.command.env.os.name", "nt")
    monkeypatch.setattr("pyfltr.command.env.os.pathsep", ";")
    env: dict[str, str] = {"PATH": ";".join(["C:\\Tools\\Mise\\bin", "c:/tools/mise/bin", "C:\\Windows"])}
    assert pyfltr.command.env.dedupe_environ_path(env) is True
    assert env["PATH"] == ";".join(["C:\\Tools\\Mise\\bin", "C:\\Windows"])


def test_dedupe_environ_path_windows_trailing_backslash_treated_as_duplicate(monkeypatch) -> None:
    monkeypatch.setattr("pyfltr.command.env.os.name", "nt")
    monkeypatch.setattr("pyfltr.command.env.os.pathsep", ";")
    env: dict[str, str] = {"PATH": ";".join(["C:/Tools/Mise/bin", "c:\\tools\\mise\\bin\\"])}
    assert pyfltr.command.env.dedupe_environ_path(env) is True
    assert env["PATH"] == "C:/Tools/Mise/bin"


def test_dedupe_environ_path_keeps_empty_entry_only_once(monkeypatch) -> None:
    monkeypatch.setattr("pyfltr.command.env.os.name", "posix")
    monkeypatch.setattr("pyfltr.command.env.os.pathsep", ":")
    env: dict[str, str] = {"PATH": "/usr/bin::/opt/bin:"}
    assert pyfltr.command.env.dedupe_environ_path(env) is True
    assert env["PATH"] == ":".join(["/usr/bin", "", "/opt/bin"])


def test_dedupe_environ_path_writes_back_with_same_key(monkeypatch) -> None:
    monkeypatch.setattr("pyfltr.command.env.os.name", "nt")
    monkeypatch.setattr("pyfltr.command.env.os.pathsep", ";")
    env: dict[str, str] = {"Path": ";".join(["c:/tools", "C:/Tools"])}
    assert pyfltr.command.env.dedupe_environ_path(env) is True
    assert "Path" in env
    assert "PATH" not in env
    assert env["Path"] == "c:/tools"


def test_dedupe_environ_path_no_change_when_unique(monkeypatch) -> None:
    monkeypatch.setattr("pyfltr.command.env.os.name", "posix")
    monkeypatch.setattr("pyfltr.command.env.os.pathsep", ":")
    env: dict[str, str] = {"PATH": "/usr/bin:/opt/bin"}
    assert pyfltr.command.env.dedupe_environ_path(env) is False
    assert env["PATH"] == "/usr/bin:/opt/bin"


def test_dedupe_environ_path_returns_false_when_path_missing() -> None:
    env: dict[str, str] = {}
    assert pyfltr.command.env.dedupe_environ_path(env) is False
    assert not env


def test_build_mise_subprocess_env_strips_installs_and_dotnet_root_and_shims(monkeypatch) -> None:
    monkeypatch.setattr("pyfltr.command.env.os.name", "posix")
    monkeypatch.setattr("pyfltr.command.env.os.pathsep", ":")
    src: dict[str, str] = {
        "PATH": ":".join(
            [
                "/home/u/.local/share/mise/installs/dotnet/10.0.0",
                "/home/u/.local/share/mise/dotnet-root",
                "/home/u/.local/share/mise/shims",
                "/home/u/.local/share/mise/bin",
                "/usr/bin",
            ]
        )
    }
    new = pyfltr.command.env.build_mise_subprocess_env(src)
    entries = new["PATH"].split(":")
    assert "/home/u/.local/share/mise/installs/dotnet/10.0.0" not in entries
    assert "/home/u/.local/share/mise/dotnet-root" not in entries
    assert "/home/u/.local/share/mise/shims" not in entries
    assert "/home/u/.local/share/mise/bin" in entries
    assert "/usr/bin" in entries


def test_build_mise_subprocess_env_unrelated_path_preserved(monkeypatch) -> None:
    monkeypatch.setattr("pyfltr.command.env.os.name", "posix")
    monkeypatch.setattr("pyfltr.command.env.os.pathsep", ":")
    src: dict[str, str] = {"PATH": ":".join(["/usr/bin", "", "/opt/bin"])}
    new = pyfltr.command.env.build_mise_subprocess_env(src)
    assert new["PATH"] == "/usr/bin::/opt/bin"


def test_build_mise_subprocess_env_windows_case_and_separator(monkeypatch) -> None:
    monkeypatch.setattr("pyfltr.command.env.os.name", "nt")
    monkeypatch.setattr("pyfltr.command.env.os.pathsep", ";")
    src: dict[str, str] = {
        "PATH": ";".join(
            [
                "C:\\Users\\u\\AppData\\Local\\MISE\\Installs\\Dotnet\\10.0",
                "C:\\Users\\u\\AppData\\Local\\mise\\dotnet-root",
                "C:\\Users\\u\\AppData\\Local\\mise\\shims",
                "C:\\Users\\u\\AppData\\Local\\mise\\bin",
                "C:\\Windows\\system32",
            ]
        )
    }
    new = pyfltr.command.env.build_mise_subprocess_env(src)
    entries = new["PATH"].split(";")
    assert "C:\\Users\\u\\AppData\\Local\\MISE\\Installs\\Dotnet\\10.0" not in entries
    assert "C:\\Users\\u\\AppData\\Local\\mise\\dotnet-root" not in entries
    assert "C:\\Users\\u\\AppData\\Local\\mise\\shims" not in entries
    assert "C:\\Users\\u\\AppData\\Local\\mise\\bin" in entries
    assert "C:\\Windows\\system32" in entries


def test_build_mise_subprocess_env_does_not_mutate_input(monkeypatch) -> None:
    monkeypatch.setattr("pyfltr.command.env.os.name", "posix")
    monkeypatch.setattr("pyfltr.command.env.os.pathsep", ":")
    src: dict[str, str] = {"PATH": "/home/u/.local/share/mise/installs/dotnet/10.0:/usr/bin"}
    new = pyfltr.command.env.build_mise_subprocess_env(src)

    assert src == {"PATH": "/home/u/.local/share/mise/installs/dotnet/10.0:/usr/bin"}
    assert new["PATH"] == "/usr/bin"


def test_build_mise_subprocess_env_handles_missing_path() -> None:
    src: dict[str, str] = {"FOO": "bar"}
    new = pyfltr.command.env.build_mise_subprocess_env(src)
    assert new == src
    assert new is not src
