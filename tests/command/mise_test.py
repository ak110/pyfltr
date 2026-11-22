import os
import pathlib
import sys

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
from tests import conftest as _testconf


def test_get_mise_active_tools_cache_key_differs_by_cwd(monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path) -> None:
    monkeypatch.setattr("pyfltr.command.mise.get_mise_active_tools", _testconf.real_get_mise_active_tools)
    monkeypatch.setattr("pyfltr.command.mise._MISE_ACTIVE_TOOLS_CACHE", {}, raising=True)
    call_log: list[str] = []

    def fake_query(
        config: pyfltr.config.model.Config, *, allow_side_effects: bool, cwd: object = None
    ) -> pyfltr.command.mise.MiseActiveToolsResult:
        del config, allow_side_effects, cwd
        call_log.append(os.getcwd())
        return pyfltr.command.mise.MiseActiveToolsResult(status="ok")

    monkeypatch.setattr("pyfltr.command.mise._query_mise_active_tools", fake_query)
    config = pyfltr.config.config.create_default_config()

    cwd_a = tmp_path / "a"
    cwd_b = tmp_path / "b"
    cwd_a.mkdir()
    cwd_b.mkdir()

    monkeypatch.chdir(cwd_a)
    pyfltr.command.mise.get_mise_active_tools(config)
    monkeypatch.chdir(cwd_a)
    pyfltr.command.mise.get_mise_active_tools(config)
    monkeypatch.chdir(cwd_b)
    pyfltr.command.mise.get_mise_active_tools(config)
    assert len(call_log) == 2


def test_get_mise_active_tools_cache_key_differs_by_env(monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path) -> None:
    monkeypatch.setattr("pyfltr.command.mise.get_mise_active_tools", _testconf.real_get_mise_active_tools)
    monkeypatch.setattr("pyfltr.command.mise._MISE_ACTIVE_TOOLS_CACHE", {}, raising=True)
    monkeypatch.chdir(tmp_path)
    call_count = [0]

    def fake_query(
        config: pyfltr.config.model.Config, *, allow_side_effects: bool, cwd: object = None
    ) -> pyfltr.command.mise.MiseActiveToolsResult:
        del config, allow_side_effects, cwd
        call_count[0] += 1
        return pyfltr.command.mise.MiseActiveToolsResult(status="ok")

    monkeypatch.setattr("pyfltr.command.mise._query_mise_active_tools", fake_query)
    config = pyfltr.config.config.create_default_config()

    monkeypatch.delenv("MISE_CONFIG_FILE", raising=False)
    pyfltr.command.mise.get_mise_active_tools(config)
    monkeypatch.setenv("MISE_CONFIG_FILE", "/tmp/other.toml")  # noqa: S108  # テスト内のダミーパス（実ファイルアクセスなし）
    pyfltr.command.mise.get_mise_active_tools(config)
    assert call_count[0] == 2


def test_get_mise_active_tools_cache_key_differs_by_allow_side_effects(
    monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path
) -> None:
    monkeypatch.setattr("pyfltr.command.mise.get_mise_active_tools", _testconf.real_get_mise_active_tools)
    monkeypatch.setattr("pyfltr.command.mise._MISE_ACTIVE_TOOLS_CACHE", {}, raising=True)
    monkeypatch.chdir(tmp_path)

    received_flags: list[bool] = []

    def fake_query(
        config: pyfltr.config.model.Config, *, allow_side_effects: bool, cwd: object = None
    ) -> pyfltr.command.mise.MiseActiveToolsResult:
        del config, cwd
        received_flags.append(allow_side_effects)
        if allow_side_effects:
            return pyfltr.command.mise.MiseActiveToolsResult(status="ok", tools={"rust": []})
        return pyfltr.command.mise.MiseActiveToolsResult(status="ok")

    monkeypatch.setattr("pyfltr.command.mise._query_mise_active_tools", fake_query)
    config = pyfltr.config.config.create_default_config()

    result_off = pyfltr.command.mise.get_mise_active_tools(config, allow_side_effects=False)
    result_on = pyfltr.command.mise.get_mise_active_tools(config, allow_side_effects=True)
    assert not result_off.tools
    assert result_on.tools == {"rust": []}
    assert received_flags == [False, True]
    pyfltr.command.mise.get_mise_active_tools(config, allow_side_effects=True)
    assert received_flags == [False, True]


def test_get_mise_active_tools_status_mise_not_found(monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path) -> None:
    monkeypatch.setattr("pyfltr.command.mise.get_mise_active_tools", _testconf.real_get_mise_active_tools)
    monkeypatch.setattr("pyfltr.command.mise._MISE_ACTIVE_TOOLS_CACHE", {}, raising=True)
    monkeypatch.setattr("pyfltr.command.mise.shutil.which", lambda name: None if name == "mise" else "/bin/" + name)
    config = pyfltr.config.config.create_default_config()
    result = pyfltr.command.mise.get_mise_active_tools(config, allow_side_effects=False, cwd=tmp_path)
    assert result.status == "mise-not-found"
    assert result.tools == {}


def test_get_mise_active_tools_status_untrusted_no_side_effects(
    monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path
) -> None:
    monkeypatch.setattr("pyfltr.command.mise.get_mise_active_tools", _testconf.real_get_mise_active_tools)
    monkeypatch.setattr("pyfltr.command.mise._MISE_ACTIVE_TOOLS_CACHE", {}, raising=True)
    monkeypatch.setattr("pyfltr.command.mise.shutil.which", lambda name: f"/usr/local/bin/{name}")

    def fake_run_with_trust(
        args: list[str],
        mise_env: dict[str, str],
        config: pyfltr.config.model.Config,
        *,
        allow_side_effects: bool,
        cwd: object = None,
    ) -> tuple[int, str, str, bool]:
        del args, mise_env, config, allow_side_effects, cwd
        return 1, "", "Error: config /home/u/mise.toml is not trusted", False

    monkeypatch.setattr("pyfltr.command.mise.run_mise_with_trust", fake_run_with_trust)
    config = pyfltr.config.config.create_default_config()
    result = pyfltr.command.mise.get_mise_active_tools(config, allow_side_effects=False, cwd=tmp_path)
    assert result.status == "untrusted-no-side-effects"
    assert result.tools == {}


def test_get_mise_active_tools_status_trust_failed(monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path) -> None:
    monkeypatch.setattr("pyfltr.command.mise.get_mise_active_tools", _testconf.real_get_mise_active_tools)
    monkeypatch.setattr("pyfltr.command.mise._MISE_ACTIVE_TOOLS_CACHE", {}, raising=True)
    monkeypatch.setattr("pyfltr.command.mise.shutil.which", lambda name: f"/usr/local/bin/{name}")

    def fake_run_with_trust(
        args: list[str],
        mise_env: dict[str, str],
        config: pyfltr.config.model.Config,
        *,
        allow_side_effects: bool,
        cwd: object = None,
    ) -> tuple[int, str, str, bool]:
        del args, mise_env, config, allow_side_effects, cwd
        return 2, "", "trust rejected by user", True

    monkeypatch.setattr("pyfltr.command.mise.run_mise_with_trust", fake_run_with_trust)
    config = pyfltr.config.config.create_default_config()
    result = pyfltr.command.mise.get_mise_active_tools(config, allow_side_effects=True, cwd=tmp_path)
    assert result.status == "trust-failed"
    assert result.tools == {}


def test_get_mise_active_tools_status_exec_error_oserror(monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path) -> None:
    monkeypatch.setattr("pyfltr.command.mise.get_mise_active_tools", _testconf.real_get_mise_active_tools)
    monkeypatch.setattr("pyfltr.command.mise._MISE_ACTIVE_TOOLS_CACHE", {}, raising=True)
    monkeypatch.setattr("pyfltr.command.mise.shutil.which", lambda name: f"/usr/local/bin/{name}")

    def fake_run_with_trust(
        args: list[str],
        mise_env: dict[str, str],
        config: pyfltr.config.model.Config,
        *,
        allow_side_effects: bool,
        cwd: object = None,
    ) -> tuple[int, str, str, bool]:
        del args, mise_env, config, allow_side_effects, cwd
        raise OSError("mise binary missing executable bit")

    monkeypatch.setattr("pyfltr.command.mise.run_mise_with_trust", fake_run_with_trust)
    config = pyfltr.config.config.create_default_config()
    result = pyfltr.command.mise.get_mise_active_tools(config, allow_side_effects=False, cwd=tmp_path)
    assert result.status == "exec-error"
    assert result.tools == {}


def test_get_mise_active_tools_status_json_parse_error(monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path) -> None:
    monkeypatch.setattr("pyfltr.command.mise.get_mise_active_tools", _testconf.real_get_mise_active_tools)
    monkeypatch.setattr("pyfltr.command.mise._MISE_ACTIVE_TOOLS_CACHE", {}, raising=True)
    monkeypatch.setattr("pyfltr.command.mise.shutil.which", lambda name: f"/usr/local/bin/{name}")

    def fake_run_with_trust(
        args: list[str],
        mise_env: dict[str, str],
        config: pyfltr.config.model.Config,
        *,
        allow_side_effects: bool,
        cwd: object = None,
    ) -> tuple[int, str, str, bool]:
        del args, mise_env, config, allow_side_effects, cwd
        return 0, "this is not json", "", False

    monkeypatch.setattr("pyfltr.command.mise.run_mise_with_trust", fake_run_with_trust)
    config = pyfltr.config.config.create_default_config()
    result = pyfltr.command.mise.get_mise_active_tools(config, allow_side_effects=False, cwd=tmp_path)
    assert result.status == "json-parse-error"
    assert result.tools == {}


def test_get_mise_active_tools_status_unexpected_shape(monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path) -> None:
    monkeypatch.setattr("pyfltr.command.mise.get_mise_active_tools", _testconf.real_get_mise_active_tools)
    monkeypatch.setattr("pyfltr.command.mise._MISE_ACTIVE_TOOLS_CACHE", {}, raising=True)
    monkeypatch.setattr("pyfltr.command.mise.shutil.which", lambda name: f"/usr/local/bin/{name}")

    def fake_run_with_trust(
        args: list[str],
        mise_env: dict[str, str],
        config: pyfltr.config.model.Config,
        *,
        allow_side_effects: bool,
        cwd: object = None,
    ) -> tuple[int, str, str, bool]:
        del args, mise_env, config, allow_side_effects, cwd
        return 0, "[]", "", False

    monkeypatch.setattr("pyfltr.command.mise.run_mise_with_trust", fake_run_with_trust)
    config = pyfltr.config.config.create_default_config()
    result = pyfltr.command.mise.get_mise_active_tools(config, allow_side_effects=False, cwd=tmp_path)
    assert result.status == "unexpected-shape"
    assert result.tools == {}


def test_get_mise_active_tools_status_ok_with_tools(monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path) -> None:
    monkeypatch.setattr("pyfltr.command.mise.get_mise_active_tools", _testconf.real_get_mise_active_tools)
    monkeypatch.setattr("pyfltr.command.mise._MISE_ACTIVE_TOOLS_CACHE", {}, raising=True)
    monkeypatch.setattr("pyfltr.command.mise.shutil.which", lambda name: f"/usr/local/bin/{name}")

    def fake_run_with_trust(
        args: list[str],
        mise_env: dict[str, str],
        config: pyfltr.config.model.Config,
        *,
        allow_side_effects: bool,
        cwd: object = None,
    ) -> tuple[int, str, str, bool]:
        del args, mise_env, config, allow_side_effects, cwd
        return 0, '{"shellcheck": [{"version": "0.9.0"}]}', "", False

    monkeypatch.setattr("pyfltr.command.mise.run_mise_with_trust", fake_run_with_trust)
    config = pyfltr.config.config.create_default_config()
    result = pyfltr.command.mise.get_mise_active_tools(config, allow_side_effects=False, cwd=tmp_path)
    assert result.status == "ok"
    assert result.tools == {"shellcheck": [{"version": "0.9.0"}]}


@pytest.mark.real_mise_subprocess
def test_run_mise_with_trust_decodes_utf8_output(tmp_path: pathlib.Path) -> None:
    config = pyfltr.config.config.create_default_config()
    script = "import sys; sys.stdout.buffer.write('日本語の出力'.encode('utf-8') + bytes([255]))"
    returncode, stdout, _stderr, trust_failed = pyfltr.command.mise.run_mise_with_trust(
        [sys.executable, "-c", script], dict(os.environ), config, allow_side_effects=False, cwd=tmp_path
    )
    assert returncode == 0
    assert stdout == r"日本語の出力\xff"
    assert not trust_failed
