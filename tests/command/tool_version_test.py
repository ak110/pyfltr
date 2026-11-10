import typing

import pytest

import pyfltr.command.core_
import pyfltr.command.dispatcher
import pyfltr.command.env
import pyfltr.command.glab
import pyfltr.command.invocation
import pyfltr.command.markdownlint_env
import pyfltr.command.mise
import pyfltr.command.only_failed
import pyfltr.command.process
import pyfltr.command.runner
import pyfltr.command.slow_tests
import pyfltr.command.subprojects
import pyfltr.command.targets
import pyfltr.command.tool_resolution
import pyfltr.command.tool_version
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


def test_probe_tool_version_text_drops_virtual_env_for_uv_runner(monkeypatch: pytest.MonkeyPatch) -> None:
    """uv経路のversion probeも`VIRTUAL_ENV`を除いた環境で起動する。"""
    monkeypatch.setenv("VIRTUAL_ENV", "/repo/.venv")
    captured: dict[str, dict[str, str]] = {}

    def _fake_run(
        commandline: list[str], env: dict[str, str], **_kwargs: typing.Any
    ) -> pyfltr.command.process.CompletedProcessWithTimeoutInfo:
        captured["env"] = env
        return pyfltr.command.process.CompletedProcessWithTimeoutInfo(
            args=list(commandline), returncode=0, stdout="arid 1.0.0", timeout_exceeded=False
        )

    monkeypatch.setattr(pyfltr.command.process, "run_process_loop", _fake_run)
    config = pyfltr.config.config.create_default_config()
    resolved = pyfltr.command.runner.ResolvedCommandline("uv", ["run", "--frozen", "arid"], "uv", "explicit", "uv")

    assert pyfltr.command.tool_version.probe_tool_version_text(resolved, config, "arid") == "arid 1.0.0"
    assert "VIRTUAL_ENV" not in captured["env"]


def test_ensure_package_manager_version_drops_virtual_env_for_uv_runner(monkeypatch: pytest.MonkeyPatch) -> None:
    """uv経路の最低版検査も`VIRTUAL_ENV`を除いた環境で起動する。"""
    pyfltr.command.tool_version._get_tool_version.cache_clear()  # pylint: disable=protected-access  # テスト間の隔離
    monkeypatch.setenv("VIRTUAL_ENV", "/repo/.venv")
    captured: dict[str, dict[str, str]] = {}

    def _fake_run(
        commandline: list[str], env: dict[str, str], **_kwargs: typing.Any
    ) -> pyfltr.command.process.CompletedProcessWithTimeoutInfo:
        captured["env"] = env
        return pyfltr.command.process.CompletedProcessWithTimeoutInfo(
            args=list(commandline), returncode=0, stdout="uv 0.11.7", timeout_exceeded=False
        )

    monkeypatch.setattr(pyfltr.command.process, "run_process_loop", _fake_run)
    config = pyfltr.config.config.create_default_config()
    resolved = pyfltr.command.runner.ResolvedCommandline("uv", ["run", "--frozen", "uv"], "uv", "explicit", "uv")

    pyfltr.command.tool_version.ensure_package_manager_version(resolved, config, "uv-audit")

    assert "VIRTUAL_ENV" not in captured["env"]


@pytest.mark.parametrize(
    ("version_output", "expect_error"),
    [
        ("uv 0.10.8 (x86_64-unknown-linux-gnu)", True),
        ("uv 0.10.9 (x86_64-unknown-linux-gnu)", True),
        ("uv 0.10.10 (x86_64-unknown-linux-gnu)", True),
        ("uv 0.11.1 (x86_64-unknown-linux-gnu)", True),
        ("uv 0.11.2 (x86_64-unknown-linux-gnu)", False),
        ("uv 0.11.7 (x86_64-unknown-linux-gnu)", False),
    ],
)
def test_ensure_package_manager_version_rejects_below_minimum(
    monkeypatch: pytest.MonkeyPatch, version_output: str, expect_error: bool
) -> None:
    """uv-auditは最低版未満のuvを拒否し、最低版以降は通す。"""
    pyfltr.command.tool_version._get_tool_version.cache_clear()  # pylint: disable=protected-access  # テスト間の隔離

    def _fake_run(
        commandline: list[str], *_args: typing.Any, **_kwargs: typing.Any
    ) -> pyfltr.command.process.CompletedProcessWithTimeoutInfo:
        return pyfltr.command.process.CompletedProcessWithTimeoutInfo(
            args=list(commandline), returncode=0, stdout=version_output, timeout_exceeded=False
        )

    monkeypatch.setattr(pyfltr.command.process, "run_process_loop", _fake_run)
    config = pyfltr.config.config.create_default_config()
    resolved = pyfltr.command.runner.ResolvedCommandline("/usr/bin/uv", [], "direct", "default", "direct")
    if expect_error:
        with pytest.raises(ValueError, match="0.11.2以降"):
            pyfltr.command.tool_version.ensure_package_manager_version(resolved, config, "uv-audit")
    else:
        pyfltr.command.tool_version.ensure_package_manager_version(resolved, config, "uv-audit")


@pytest.mark.parametrize("version_output", ["", "unexpected output", "uv"])
def test_ensure_package_manager_version_rejects_unparsable(monkeypatch: pytest.MonkeyPatch, version_output: str) -> None:
    """版を取得できない場合と版文字列を解釈できない場合も拒否する。"""
    pyfltr.command.tool_version._get_tool_version.cache_clear()  # pylint: disable=protected-access  # テスト間の隔離

    def _fake_run(
        commandline: list[str], *_args: typing.Any, **_kwargs: typing.Any
    ) -> pyfltr.command.process.CompletedProcessWithTimeoutInfo:
        return pyfltr.command.process.CompletedProcessWithTimeoutInfo(
            args=list(commandline), returncode=0, stdout=version_output, timeout_exceeded=False
        )

    monkeypatch.setattr(pyfltr.command.process, "run_process_loop", _fake_run)
    config = pyfltr.config.config.create_default_config()
    resolved = pyfltr.command.runner.ResolvedCommandline("/usr/bin/uv", [], "direct", "default", "direct")
    with pytest.raises(ValueError, match="判別できません"):
        pyfltr.command.tool_version.ensure_package_manager_version(resolved, config, "uv-audit")


def test_ensure_package_manager_version_skips_commands_without_requirement(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """最低版要件を持たない監査コマンドでは版取得を行わない。"""

    def _fail(*args: typing.Any, **kwargs: typing.Any) -> typing.NoReturn:
        raise AssertionError((args, kwargs))

    monkeypatch.setattr(pyfltr.command.process, "run_process_loop", _fail)
    config = pyfltr.config.config.create_default_config()
    resolved = pyfltr.command.runner.ResolvedCommandline("/usr/bin/npm", [], "direct", "default", "direct")
    pyfltr.command.tool_version.ensure_package_manager_version(resolved, config, "npm-audit")


def test_get_tool_version_caches_per_executable(monkeypatch: pytest.MonkeyPatch) -> None:
    """版取得は同じ実行ファイルへ再問い合わせせず、別の実行ファイルでは再取得する。"""
    pyfltr.command.tool_version._get_tool_version.cache_clear()  # pylint: disable=protected-access  # テスト間の隔離
    calls: list[str] = []

    def _fake_run(
        commandline: list[str], *_args: typing.Any, **_kwargs: typing.Any
    ) -> pyfltr.command.process.CompletedProcessWithTimeoutInfo:
        calls.append(commandline[0])
        return pyfltr.command.process.CompletedProcessWithTimeoutInfo(
            args=list(commandline), returncode=0, stdout="uv 0.11.7", timeout_exceeded=False
        )

    monkeypatch.setattr(pyfltr.command.process, "run_process_loop", _fake_run)
    config = pyfltr.config.config.create_default_config()
    for executable in ("/usr/bin/uv", "/usr/bin/uv", "/opt/bin/uv"):
        resolved = pyfltr.command.runner.ResolvedCommandline(executable, [], "direct", "default", "direct")
        pyfltr.command.tool_version.ensure_package_manager_version(resolved, config, "uv-audit")
    assert calls == ["/usr/bin/uv", "/opt/bin/uv"]


def test_ensure_package_manager_version_rejects_on_version_command_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`--version`が非ゼロ終了した場合も要件未達と同じ扱いで拒否する。"""
    pyfltr.command.tool_version._get_tool_version.cache_clear()  # pylint: disable=protected-access  # テスト間の隔離

    def _fake_run(
        commandline: list[str], *_args: typing.Any, **_kwargs: typing.Any
    ) -> pyfltr.command.process.CompletedProcessWithTimeoutInfo:
        return pyfltr.command.process.CompletedProcessWithTimeoutInfo(
            args=list(commandline), returncode=1, stdout="failure", timeout_exceeded=False
        )

    monkeypatch.setattr(pyfltr.command.process, "run_process_loop", _fake_run)
    config = pyfltr.config.config.create_default_config()
    resolved = pyfltr.command.runner.ResolvedCommandline("/usr/bin/uv", [], "direct", "default", "direct")
    with pytest.raises(ValueError, match="判別できません"):
        pyfltr.command.tool_version.ensure_package_manager_version(resolved, config, "uv-audit")


@pytest.mark.parametrize(
    "output,expected",
    [
        # 一般的な`<名前> <版>`形式。
        ("uv 0.11.7 (x86_64-unknown-linux-gnu)", (0, 11, 7)),
        ("pnpm 10.2.0", (10, 2, 0)),
        ("yarn 1.22.22", (1, 22, 22)),
        # 版行より前に警告行が出る実装。
        ("warning: something\nuv 0.11.0", (0, 11, 0)),
        # ビルドメタデータ・プレリリースは数値要素のみへ切り詰める。
        ("uv 0.10.10+build", (0, 10, 10)),
        ("uv 0.12.0-rc1", (0, 12, 0)),
        # 版のみ・接頭辞`v`付きの形式。
        ("2026.7.1 linux-x64", (2026, 7, 1)),
        ("v1.2.3", (1, 2, 3)),
        # 版として解釈できない出力。
        ("", None),
        ("unexpected output", None),
        ("uv", None),
    ],
)
def test_extract_tool_version_handles_tool_specific_formats(output: str, expected: tuple[int, ...] | None) -> None:
    """版の抽出はツールごとの出力形式と警告行の混入に耐える。

    行頭固定・2トークン目固定では、警告行が先に出る実装やビルドメタデータ付きの版で
    取りこぼしや誤った切り詰めが起きるため、全行走査で最初の版らしいトークンを採用する。
    """
    assert pyfltr.command.tool_version._extract_tool_version(output) == expected  # pylint: disable=protected-access


def test_ensure_package_manager_version_probes_resolved_commandline(monkeypatch: pytest.MonkeyPatch) -> None:
    """版の問い合わせは`prefix`を含む解決済みコマンドラインへ行う。

    実行ファイルだけへ問い合わせると、ラッパー経由で解決された場合に
    ラッパー自身の版を測ってしまうため。
    """
    pyfltr.command.tool_version._get_tool_version.cache_clear()  # pylint: disable=protected-access  # テスト間の隔離
    captured: list[list[str]] = []

    def _fake_run(
        commandline: list[str], *_args: typing.Any, **_kwargs: typing.Any
    ) -> pyfltr.command.process.CompletedProcessWithTimeoutInfo:
        captured.append(list(commandline))
        return pyfltr.command.process.CompletedProcessWithTimeoutInfo(
            args=list(commandline), returncode=0, stdout="uv 0.11.7", timeout_exceeded=False
        )

    monkeypatch.setattr(pyfltr.command.process, "run_process_loop", _fake_run)
    config = pyfltr.config.config.create_default_config()
    resolved = pyfltr.command.runner.ResolvedCommandline("mise", ["exec", "--", "uv"], "mise", "explicit", "mise")
    pyfltr.command.tool_version.ensure_package_manager_version(resolved, config, "uv-audit")
    assert captured == [["mise", "exec", "--", "uv", "--version"]]


@pytest.mark.parametrize(
    ("stdout", "expected"),
    [
        ("ruff 0.14.2\n", "ruff 0.14.2"),
        ("\n\n  shellcheck 0.11.0  \n", "shellcheck 0.11.0"),
        ("ShellCheck - shell script analysis tool\nversion: 0.11.0\n", "version: 0.11.0"),
        ("warning: runtime 3.14.0 is unsupported\nruff 0.14.2\n", "ruff 0.14.2"),
        ("warning: runtime 3.14.0 is unsupported\n", None),
        ("ready\n", None),
        ("", None),
    ],
)
def test_probe_tool_version_text_prefers_line_with_numeric_version(
    monkeypatch: pytest.MonkeyPatch, stdout: str, expected: str | None
) -> None:
    """数値版を含む最初の非警告行を採用し、該当しなければ`None`を返す。"""

    def _fake_run(
        commandline: list[str], *_args: typing.Any, **_kwargs: typing.Any
    ) -> pyfltr.command.process.CompletedProcessWithTimeoutInfo:
        return pyfltr.command.process.CompletedProcessWithTimeoutInfo(
            args=list(commandline), returncode=0, stdout=stdout, timeout_exceeded=False
        )

    monkeypatch.setattr(pyfltr.command.process, "run_process_loop", _fake_run)
    config = pyfltr.config.config.create_default_config()
    resolved = pyfltr.command.runner.ResolvedCommandline("ruff", [], "direct", "explicit", "direct")

    assert pyfltr.command.tool_version.probe_tool_version_text(resolved, config, "ruff-check") == expected


def test_probe_tool_version_text_returns_none_on_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    """`--version`が非0終了した場合は`None`を返す。"""

    def _fake_run(
        commandline: list[str], *_args: typing.Any, **_kwargs: typing.Any
    ) -> pyfltr.command.process.CompletedProcessWithTimeoutInfo:
        return pyfltr.command.process.CompletedProcessWithTimeoutInfo(
            args=list(commandline), returncode=1, stdout="error", timeout_exceeded=False
        )

    monkeypatch.setattr(pyfltr.command.process, "run_process_loop", _fake_run)
    config = pyfltr.config.config.create_default_config()
    resolved = pyfltr.command.runner.ResolvedCommandline("ruff", [], "direct", "explicit", "direct")

    assert pyfltr.command.tool_version.probe_tool_version_text(resolved, config, "ruff-check") is None


def test_probe_tool_version_text_strips_mise_tool_paths_for_mise_runner(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """mise経路ではPATHからmise toolエントリを除外したenvを渡す。"""
    captured: dict[str, typing.Any] = {}

    def _fake_env(
        _config: pyfltr.config.model.Config,
        _command: str,
        *,
        via_mise: bool = False,
        effective_runner: str | None = None,
    ) -> dict[str, str]:
        captured["via_mise"] = via_mise
        captured["effective_runner"] = effective_runner
        return {"PATH": "/clean"}

    def _fake_run(
        commandline: list[str], env: dict[str, str], **_kwargs: typing.Any
    ) -> pyfltr.command.process.CompletedProcessWithTimeoutInfo:
        captured["env"] = env
        return pyfltr.command.process.CompletedProcessWithTimeoutInfo(
            args=list(commandline), returncode=0, stdout="shellcheck 0.11.0", timeout_exceeded=False
        )

    monkeypatch.setattr(pyfltr.command.tool_version, "build_subprocess_env", _fake_env)
    monkeypatch.setattr(pyfltr.command.process, "run_process_loop", _fake_run)
    config = pyfltr.config.config.create_default_config()
    resolved = pyfltr.command.runner.ResolvedCommandline(
        "mise", ["exec", "shellcheck@latest", "--", "shellcheck"], "mise", "explicit", "mise"
    )

    result = pyfltr.command.tool_version.probe_tool_version_text(resolved, config, "shellcheck")

    assert result == "shellcheck 0.11.0"
    assert captured == {"via_mise": True, "effective_runner": "mise", "env": {"PATH": "/clean"}}
