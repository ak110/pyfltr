import os
import pathlib
import subprocess
import sys
import typing

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

_EXPECTED_BIN_TOOLS = (
    "ec",
    "shellcheck",
    "shfmt",
    "actionlint",
    "pinact",
    "glab-ci-lint",
    "taplo",
    "hadolint",
    "gitleaks",
    "lychee",
    "cargo-fmt",
    "cargo-clippy",
    "cargo-check",
    "cargo-test",
    "cargo-deny",
    "dotnet-format",
    "dotnet-build",
    "dotnet-test",
)


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

    assert pyfltr.command.runner.probe_tool_version_text(resolved, config, "arid") == "arid 1.0.0"
    assert "VIRTUAL_ENV" not in captured["env"]


def test_ensure_package_manager_version_drops_virtual_env_for_uv_runner(monkeypatch: pytest.MonkeyPatch) -> None:
    """uv経路の最低版検査も`VIRTUAL_ENV`を除いた環境で起動する。"""
    pyfltr.command.runner._get_tool_version.cache_clear()  # pylint: disable=protected-access  # テスト間の隔離
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

    pyfltr.command.runner.ensure_package_manager_version(resolved, config, "uv-audit")

    assert "VIRTUAL_ENV" not in captured["env"]


@pytest.mark.parametrize(
    ("command", "package_spec", "bin_name"),
    [
        ("textlint", "textlint@<15.5.3 || >15.5.3", "textlint"),
        ("markdownlint", "markdownlint-cli2", "markdownlint-cli2"),
        ("eslint", "eslint", "eslint"),
        ("prettier", "prettier", "prettier"),
        ("biome", "@biomejs/biome", "biome"),
        ("vitest", "vitest", "vitest"),
        ("oxlint", "oxlint", "oxlint"),
        ("tsc", "typescript", "tsc"),
        ("designmd", "@google/design.md", "design.md"),
    ],
)
def test_build_commandline_pnpx_js_tools(command: str, package_spec: str, bin_name: str) -> None:
    """pnpx論理runnerは全JSツールをpnpm本体のdlx形式へ解決する。"""
    config = pyfltr.config.config.create_default_config()
    config.values["js-runner"] = "pnpx"
    config.values[f"{command}-packages"] = []

    resolved = pyfltr.command.runner.build_commandline(command, config)

    assert pathlib.PurePath(resolved.executable).stem == "pnpm"
    assert resolved.prefix == ["--package", package_spec, "dlx", bin_name]
    assert resolved.effective_runner == "pnpx"


def test_build_commandline_pnpx_with_textlint_packages() -> None:
    """pnpx論理runnerでは設定と全パッケージがdlxより前に展開される。"""
    config = pyfltr.config.config.create_default_config()
    config.values["textlint-runner"] = "pnpx"

    resolved = pyfltr.command.runner.build_commandline("textlint", config)

    assert pathlib.PurePath(resolved.executable).stem == "pnpm"
    assert resolved.prefix == [
        "--config.enableGlobalVirtualStore=false",
        "--package",
        "textlint@<15.5.3 || >15.5.3",
        "--package",
        "textlint-rule-preset-ja-technical-writing",
        "--package",
        "textlint-rule-preset-jtf-style",
        "--package",
        "textlint-rule-ja-no-abusage",
        "--package",
        "textlint-rule-preset-ai-words-ja",
        "dlx",
        "textlint",
    ]
    assert resolved.effective_runner == "pnpx"


def test_build_commandline_pnpm_ignores_packages() -> None:
    """pnpm runnerではtextlint-packagesは無視される（package.json側で管理前提）。"""
    config = pyfltr.config.config.create_default_config()
    config.values["js-runner"] = "pnpm"
    config.values["textlint-packages"] = ["textlint-rule-preset-ja-technical-writing"]

    resolved = pyfltr.command.runner.build_commandline("textlint", config)

    assert pathlib.PurePath(resolved.executable).stem == "pnpm"
    assert resolved.prefix == ["exec", "textlint"]


def test_build_commandline_markdownlint_uses_cli2_binary() -> None:
    """markdownlintコマンドの実体はmarkdownlint-cli2。"""
    config = pyfltr.config.config.create_default_config()
    config.values["js-runner"] = "pnpm"

    resolved = pyfltr.command.runner.build_commandline("markdownlint", config)

    assert pathlib.PurePath(resolved.executable).stem == "pnpm"
    assert resolved.prefix == ["exec", "markdownlint-cli2"]


def test_build_commandline_pnpm_prettier() -> None:
    """pnpm runnerでprettierがpnpm exec prettierになる。"""
    config = pyfltr.config.config.create_default_config()
    config.values["js-runner"] = "pnpm"

    resolved = pyfltr.command.runner.build_commandline("prettier", config)

    assert pathlib.PurePath(resolved.executable).stem == "pnpm"
    assert resolved.prefix == ["exec", "prettier"]


def test_build_commandline_pnpm_biome() -> None:
    """pnpm runnerでbiomeがpnpm exec biomeになる（スコープ無効）。"""
    config = pyfltr.config.config.create_default_config()
    config.values["js-runner"] = "pnpm"

    resolved = pyfltr.command.runner.build_commandline("biome", config)

    assert pathlib.PurePath(resolved.executable).stem == "pnpm"
    assert resolved.prefix == ["exec", "biome"]


def test_build_commandline_npx() -> None:
    """npx runnerでは-pでパッケージを指定する。"""
    config = pyfltr.config.config.create_default_config()
    config.values["js-runner"] = "npx"
    config.values["textlint-packages"] = ["textlint-rule-preset-ja-technical-writing"]

    resolved = pyfltr.command.runner.build_commandline("textlint", config)

    assert pathlib.PurePath(resolved.executable).stem == "npx"
    assert resolved.prefix == [
        "--no-install",
        "-p",
        "textlint-rule-preset-ja-technical-writing",
        "--",
        "textlint",
    ]


def test_build_commandline_direct_missing_raises(tmp_path: pathlib.Path) -> None:
    """direct runnerでnode_modules/.bin/<cmd>が無ければFileNotFoundError。"""
    config = pyfltr.config.config.create_default_config()
    config.values["js-runner"] = "direct"

    original_cwd = pathlib.Path.cwd()
    try:
        os.chdir(tmp_path)
        with pytest.raises(FileNotFoundError):
            pyfltr.command.runner.build_commandline("textlint", config)
    finally:
        os.chdir(original_cwd)


def test_build_commandline_direct_found(tmp_path: pathlib.Path) -> None:
    """direct runnerでnode_modules/.bin/<cmd>があればpathを返す。"""
    bin_dir = tmp_path / "node_modules" / ".bin"
    bin_dir.mkdir(parents=True)
    (bin_dir / "textlint").write_text("#!/bin/sh\necho stub\n")

    config = pyfltr.config.config.create_default_config()
    config.values["js-runner"] = "direct"

    original_cwd = pathlib.Path.cwd()
    try:
        os.chdir(tmp_path)
        resolved = pyfltr.command.runner.build_commandline("textlint", config)
        assert resolved.executable.endswith("textlint")
        assert not resolved.prefix
    finally:
        os.chdir(original_cwd)


def test_build_commandline_per_tool_direct_overrides_global_js_runner(tmp_path: pathlib.Path) -> None:
    """per-tool `textlint-runner = "direct"` × global `js-runner = "pnpm"` で
    node_modules/.bin/textlintに解決される（per-tool指定がglobalに優先）。
    """
    bin_dir = tmp_path / "node_modules" / ".bin"
    bin_dir.mkdir(parents=True)
    (bin_dir / "textlint").write_text("#!/bin/sh\necho stub\n")

    config = pyfltr.config.config.create_default_config()
    # globalはpnpm経路だが、per-toolでdirect指定しているため node_modules/.bin/<bin> 解決が実行される。
    config.values["js-runner"] = "pnpm"
    config.values["textlint-runner"] = "direct"

    original_cwd = pathlib.Path.cwd()
    try:
        os.chdir(tmp_path)
        resolved = pyfltr.command.runner.build_commandline("textlint", config)
        assert resolved.runner == "direct"
        assert resolved.effective_runner == "direct"
        assert pathlib.Path(resolved.executable).name == "textlint"
        assert not resolved.prefix
    finally:
        os.chdir(original_cwd)


@pytest.mark.parametrize(
    "command,expected_bin",
    [
        ("uv-audit", "uv"),
        ("pnpm-audit", "pnpm"),
        ("npm-audit", "npm"),
        ("yarn-audit", "yarn"),
    ],
)
def test_build_commandline_package_manager_audit_resolves_direct(
    monkeypatch: pytest.MonkeyPatch, command: str, expected_bin: str
) -> None:
    """依存脆弱性監査ツールは既定のdirect経路でパッケージマネージャーのバイナリへ解決される。

    `PACKAGE_MANAGER_TOOL_BIN`の対応付け（uv-audit→uv等）と
    `_resolve_direct_runner_commandline`先頭分岐の到達を確認する。
    環境にバイナリが無くても判定できるよう`shutil.which`を差し替える。
    """

    def _fake_which(name: str, **kwargs: typing.Any) -> str:
        del kwargs  # shutil.which の mode / path 引数を無視する。
        return f"/usr/bin/{name}"

    monkeypatch.setattr("pyfltr.command.runner.shutil.which", _fake_which)
    config = pyfltr.config.config.create_default_config()
    resolved = pyfltr.command.runner.build_commandline(command, config)
    assert resolved.runner == "direct"
    assert resolved.runner_source == "default"
    assert resolved.effective_runner == "direct"
    assert pathlib.Path(resolved.executable).stem == expected_bin
    assert not resolved.prefix


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
    pyfltr.command.runner._get_tool_version.cache_clear()  # pylint: disable=protected-access  # テスト間の隔離

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
            pyfltr.command.runner.ensure_package_manager_version(resolved, config, "uv-audit")
    else:
        pyfltr.command.runner.ensure_package_manager_version(resolved, config, "uv-audit")


@pytest.mark.parametrize("version_output", ["", "unexpected output", "uv"])
def test_ensure_package_manager_version_rejects_unparsable(monkeypatch: pytest.MonkeyPatch, version_output: str) -> None:
    """版を取得できない場合と版文字列を解釈できない場合も拒否する。"""
    pyfltr.command.runner._get_tool_version.cache_clear()  # pylint: disable=protected-access  # テスト間の隔離

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
        pyfltr.command.runner.ensure_package_manager_version(resolved, config, "uv-audit")


def test_ensure_package_manager_version_skips_commands_without_requirement(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """最低版要件を持たない監査コマンドでは版取得を行わない。"""

    def _fail(*args: typing.Any, **kwargs: typing.Any) -> typing.NoReturn:
        raise AssertionError((args, kwargs))

    monkeypatch.setattr(pyfltr.command.process, "run_process_loop", _fail)
    config = pyfltr.config.config.create_default_config()
    resolved = pyfltr.command.runner.ResolvedCommandline("/usr/bin/npm", [], "direct", "default", "direct")
    pyfltr.command.runner.ensure_package_manager_version(resolved, config, "npm-audit")


def test_get_tool_version_caches_per_executable(monkeypatch: pytest.MonkeyPatch) -> None:
    """版取得は同じ実行ファイルへ再問い合わせせず、別の実行ファイルでは再取得する。"""
    pyfltr.command.runner._get_tool_version.cache_clear()  # pylint: disable=protected-access  # テスト間の隔離
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
        pyfltr.command.runner.ensure_package_manager_version(resolved, config, "uv-audit")
    assert calls == ["/usr/bin/uv", "/opt/bin/uv"]


def test_ensure_package_manager_version_rejects_on_version_command_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`--version`が非ゼロ終了した場合も要件未達と同じ扱いで拒否する。"""
    pyfltr.command.runner._get_tool_version.cache_clear()  # pylint: disable=protected-access  # テスト間の隔離

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
        pyfltr.command.runner.ensure_package_manager_version(resolved, config, "uv-audit")


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
    assert pyfltr.command.runner._extract_tool_version(output) == expected  # pylint: disable=protected-access


def test_ensure_package_manager_version_probes_resolved_commandline(monkeypatch: pytest.MonkeyPatch) -> None:
    """版の問い合わせは`prefix`を含む解決済みコマンドラインへ行う。

    実行ファイルだけへ問い合わせると、ラッパー経由で解決された場合に
    ラッパー自身の版を測ってしまうため。
    """
    pyfltr.command.runner._get_tool_version.cache_clear()  # pylint: disable=protected-access  # テスト間の隔離
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
    pyfltr.command.runner.ensure_package_manager_version(resolved, config, "uv-audit")
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

    assert pyfltr.command.runner.probe_tool_version_text(resolved, config, "ruff-check") == expected


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

    assert pyfltr.command.runner.probe_tool_version_text(resolved, config, "ruff-check") is None


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

    monkeypatch.setattr(pyfltr.command.runner, "build_subprocess_env", _fake_env)
    monkeypatch.setattr(pyfltr.command.process, "run_process_loop", _fake_run)
    config = pyfltr.config.config.create_default_config()
    resolved = pyfltr.command.runner.ResolvedCommandline(
        "mise", ["exec", "shellcheck@latest", "--", "shellcheck"], "mise", "explicit", "mise"
    )

    result = pyfltr.command.runner.probe_tool_version_text(resolved, config, "shellcheck")

    assert result == "shellcheck 0.11.0"
    assert captured == {"via_mise": True, "effective_runner": "mise", "env": {"PATH": "/clean"}}


def test_expanduser_expands_path_in_build_commandline(monkeypatch: pytest.MonkeyPatch) -> None:
    """`{command}-path` の `~` がbuild_commandlineで展開される。

    path-override経路で `~/foo/bar` が `<HOME>/foo/bar` へ展開されたexecutableが返ることを確認する。
    """
    # Windowsの`ntpath.expanduser`は`USERPROFILE`を優先するため両方上書きする。
    monkeypatch.setenv("HOME", "/tmp/fake-home")
    monkeypatch.setenv("USERPROFILE", "/tmp/fake-home")
    config = pyfltr.config.config.create_default_config()
    config.values["mypy-path"] = "~/bin/mypy"
    resolved = pyfltr.command.runner.build_commandline("mypy", config)
    assert resolved.runner_source == "path-override"
    assert resolved.executable == "/tmp/fake-home/bin/mypy"


def test_expanduser_expands_args_in_build_invocation_argv(monkeypatch: pytest.MonkeyPatch) -> None:
    """`{command}-args` 各要素の `~` がbuild_invocation_argvで展開される。

    展開規則は2点。要素先頭の `~` / `~user` を `os.path.expanduser` で展開する。
    要素内の最初の `=` 直後の `~` も展開する（`--config=~/cfg.toml` 形式の利便のため）。
    """
    # Windowsの`ntpath.expanduser`は`USERPROFILE`を優先するため両方上書きする。
    monkeypatch.setenv("HOME", "/tmp/fake-home")
    monkeypatch.setenv("USERPROFILE", "/tmp/fake-home")
    config = pyfltr.config.config.create_default_config()
    config.values["mypy-args"] = ["--config", "~/cfg.toml", "~/extra/path.py", "--cache-dir=~/cache"]
    argv = pyfltr.command.runner.build_invocation_argv(
        "mypy", config, commandline_prefix=["mypy"], additional_args=[], fix_stage=False
    )
    assert "/tmp/fake-home/cfg.toml" in argv
    assert "/tmp/fake-home/extra/path.py" in argv
    assert "--cache-dir=/tmp/fake-home/cache" in argv


def test_expanduser_expands_fix_args(monkeypatch: pytest.MonkeyPatch) -> None:
    """`{command}-fix-args` 各要素の `~` がfix_stage=Trueの組み立て時に展開される。"""
    # Windowsの`ntpath.expanduser`は`USERPROFILE`を優先するため両方上書きする。
    monkeypatch.setenv("HOME", "/tmp/fake-home")
    monkeypatch.setenv("USERPROFILE", "/tmp/fake-home")
    config = pyfltr.config.config.create_default_config()
    config.values["ruff-check-fix-args"] = ["--fix", "--config", "~/ruff.toml"]
    argv = pyfltr.command.runner.build_invocation_argv(
        "ruff-check", config, commandline_prefix=["ruff"], additional_args=[], fix_stage=True
    )
    assert "/tmp/fake-home/ruff.toml" in argv


def test_expanduser_expands_lint_args(monkeypatch: pytest.MonkeyPatch) -> None:
    """`{command}-lint-args` 各要素の `~` がfix-args未指定の通常段で展開される。

    fix_stage=Trueでもfix-args未定義のコマンドではlint-argsが結合経路に残る。
    """
    monkeypatch.setenv("HOME", "/tmp/fake-home")
    monkeypatch.setenv("USERPROFILE", "/tmp/fake-home")
    config = pyfltr.config.config.create_default_config()
    config.values["mypy-lint-args"] = ["--strict", "--config=~/strict.toml"]
    argv = pyfltr.command.runner.build_invocation_argv(
        "mypy", config, commandline_prefix=["mypy"], additional_args=[], fix_stage=False
    )
    assert "--config=/tmp/fake-home/strict.toml" in argv


def test_build_invocation_argv_without_extend_args() -> None:
    """`{command}-extend-args`未設定時は従来と同じargvを返す。"""
    config = pyfltr.config.config.create_default_config()
    # mypyのauto_args（mypy-unused-awaitable）を無効化し、検証対象を{command}-argsの結合に限定する
    config.values["mypy-unused-awaitable"] = False
    config.values["mypy-args"] = ["--strict"]
    argv = pyfltr.command.runner.build_invocation_argv(
        "mypy", config, commandline_prefix=["mypy"], additional_args=["-p", "pkg"], fix_stage=False
    )
    assert argv == ["mypy", "--strict", "-p", "pkg"]


def test_build_invocation_argv_appends_extend_args() -> None:
    """`{command}-extend-args`は`{command}-args`の直後に結合される。"""
    config = pyfltr.config.config.create_default_config()
    config.values["mypy-unused-awaitable"] = False
    config.values["mypy-args"] = ["--strict"]
    config.values["mypy-extend-args"] = ["--no-warn-unused-ignores"]
    argv = pyfltr.command.runner.build_invocation_argv(
        "mypy", config, commandline_prefix=["mypy"], additional_args=["-p", "pkg"], fix_stage=False
    )
    assert argv == ["mypy", "--strict", "--no-warn-unused-ignores", "-p", "pkg"]


@pytest.mark.parametrize(
    ("configured_args", "expected_prefix"),
    [
        (["--format", "text"], ["arid", "--project-root", "."]),
        (["--format=text"], ["arid", "--project-root", "."]),
        (["--json", "--project-root", "src"], ["arid", "--project-root", ".", "--project-root", "src"]),
    ],
)
def test_build_invocation_argv_applies_arid_json(configured_args: list[str], expected_prefix: list[str]) -> None:
    """aridの競合する出力指定を除去し、JSON形式を1件だけ注入する。"""
    config = pyfltr.config.config.create_default_config()
    config.values["arid-args"] = ["--project-root", ".", *configured_args]

    argv = pyfltr.command.runner.build_invocation_argv(
        "arid", config, commandline_prefix=["arid"], additional_args=[], fix_stage=False
    )

    assert argv == [*expected_prefix, "--format=json"]


def test_build_invocation_argv_fix_stage_includes_extend_args() -> None:
    """fix段でも`{command}-extend-args`が`args`直後に結合される。"""
    config = pyfltr.config.config.create_default_config()
    # ruff-check-json由来の--output-format=json注入は別経路の検査対象のため、ここでは無効化する
    config.values["ruff-check-json"] = False
    config.values["ruff-check-args"] = ["check"]
    config.values["ruff-check-extend-args"] = ["--config=pyproject.toml"]
    argv = pyfltr.command.runner.build_invocation_argv(
        "ruff-check", config, commandline_prefix=["ruff"], additional_args=[], fix_stage=True
    )
    # ruff-check-fix-argsは既定で ["--fix", "--unsafe-fixes"]
    assert argv == ["ruff", "check", "--config=pyproject.toml", "--fix", "--unsafe-fixes"]


def test_expanduser_expands_extend_args(monkeypatch: pytest.MonkeyPatch) -> None:
    """`{command}-extend-args`各要素の`~`がbuild_invocation_argvで展開される。"""
    monkeypatch.setenv("HOME", "/tmp/fake-home")
    monkeypatch.setenv("USERPROFILE", "/tmp/fake-home")
    config = pyfltr.config.config.create_default_config()
    config.values["mypy-extend-args"] = ["--config=~/mypy.toml"]
    argv = pyfltr.command.runner.build_invocation_argv(
        "mypy", config, commandline_prefix=["mypy"], additional_args=[], fix_stage=False
    )
    assert "--config=/tmp/fake-home/mypy.toml" in argv


def test_build_invocation_argv_auto_args_pylint_pydantic() -> None:
    """pylint-pydantic=trueの場合に自動引数が挿入される。"""
    config = pyfltr.config.config.create_default_config()
    argv = pyfltr.command.runner.build_invocation_argv(
        "pylint", config, commandline_prefix=["pylint"], additional_args=[], fix_stage=False
    )
    assert "--load-plugins=pylint_pydantic" in argv


def test_build_invocation_argv_auto_args_mypy_unused_awaitable() -> None:
    """mypy-unused-awaitable=trueの場合に自動引数が挿入される。"""
    config = pyfltr.config.config.create_default_config()
    argv = pyfltr.command.runner.build_invocation_argv(
        "mypy", config, commandline_prefix=["mypy"], additional_args=[], fix_stage=False
    )
    assert "--enable-error-code=unused-awaitable" in argv


def test_build_invocation_argv_auto_value_args_lychee() -> None:
    """lychee-max-concurrencyの既定値が`--max-concurrency=4`として挿入される。"""
    config = pyfltr.config.config.create_default_config()
    argv = pyfltr.command.runner.build_invocation_argv(
        "lychee", config, commandline_prefix=["lychee"], additional_args=[], fix_stage=False
    )
    assert "--max-concurrency=4" in argv


def test_build_invocation_argv_auto_value_args_disabled() -> None:
    """lychee-max-concurrencyが0以下なら`--max-concurrency`を挿入しない。"""
    config = pyfltr.config.config.create_default_config()
    config.values["lychee-max-concurrency"] = 0
    argv = pyfltr.command.runner.build_invocation_argv(
        "lychee", config, commandline_prefix=["lychee"], additional_args=[], fix_stage=False
    )
    assert not any(arg.startswith("--max-concurrency") for arg in argv)


def test_build_invocation_argv_auto_value_args_user_override() -> None:
    """利用者が`--max-concurrency`を指定済みなら自動追加しない。"""
    config = pyfltr.config.config.create_default_config()
    config.values["lychee-extend-args"] = ["--max-concurrency=16"]
    argv = pyfltr.command.runner.build_invocation_argv(
        "lychee", config, commandline_prefix=["lychee"], additional_args=[], fix_stage=False
    )
    assert [arg for arg in argv if arg.startswith("--max-concurrency")] == ["--max-concurrency=16"]


def test_build_invocation_argv_auto_args_disabled() -> None:
    """自動オプションをfalseにすると引数が挿入されない。"""
    config = pyfltr.config.config.create_default_config()
    config.values["pylint-pydantic"] = False
    argv = pyfltr.command.runner.build_invocation_argv(
        "pylint", config, commandline_prefix=["pylint"], additional_args=[], fix_stage=False
    )
    assert "--load-plugins=pylint_pydantic" not in argv


def test_build_invocation_argv_auto_args_dedup_with_user_args() -> None:
    """ユーザーが既に同じ引数を指定している場合はスキップする。"""
    config = pyfltr.config.config.create_default_config()
    config.values["pylint-args"] = ["--load-plugins=pylint_pydantic", "--jobs=4"]
    argv = pyfltr.command.runner.build_invocation_argv(
        "pylint", config, commandline_prefix=["pylint"], additional_args=[], fix_stage=False
    )
    # pylint-args設定値に含まれるため、自動引数による重複挿入がされないことを確認する
    assert argv.count("--load-plugins=pylint_pydantic") == 1


def test_build_invocation_argv_auto_args_no_match() -> None:
    """`AUTO_ARGS`に定義されていないコマンドは自動引数が挿入されない。"""
    config = pyfltr.config.config.create_default_config()
    # ruff-check-jsonを無効化して余分な--output-formatが入らないようにする
    config.values["ruff-check-json"] = False
    argv = pyfltr.command.runner.build_invocation_argv(
        "ruff-check", config, commandline_prefix=["ruff"], additional_args=[], fix_stage=False
    )
    # ruff-checkはAUTO_ARGS未定義であり、AUTO_ARGS由来フラグが含まれないことを確認する
    assert "ruff-check" not in pyfltr.tools.AUTO_ARGS
    all_auto_args_flags = [flag for entries in pyfltr.tools.AUTO_ARGS.values() for _, args in entries for flag in args]
    for flag in all_auto_args_flags:
        assert not any(a == flag or a.startswith(flag + "=") for a in argv), (
            f"AUTO_ARGS由来フラグ {flag} が意図せず含まれています"
        )


# --- bin-runnerテスト ---


def _resolve_bin_commandline_via_two_step(
    command: str,
    config: pyfltr.config.model.Config,
) -> tuple[str, list[str]]:
    """テスト用ヘルパー: `build_commandline` + `ensure_mise_available` の2段呼び出し。

    副作用検証テスト（mise未導入時のdirectフォールバック・未信頼config時のtrustリトライ・
    `mise exec --version` 経由の環境加工）で共通利用する。
    `build_commandline(allow_side_effects=True)` と `ensure_mise_available(...)` を分離した経路で
    検証することでmise副作用の発生有無を観測できる構造になっており、互換ラッパー風の単段呼び出しを
    新規テストへ復活させない方針。新規の副作用検証テストは本ヘルパーを再利用する。
    """
    resolved = pyfltr.command.runner.build_commandline(command, config, allow_side_effects=True)
    resolved = pyfltr.command.runner.ensure_mise_available(resolved, config, command=command)
    return resolved.executable, list(resolved.prefix)


def test_resolve_bin_commandline_direct_found(mocker) -> None:
    """directモードでwhichが成功した場合、解決されたパスを返す。"""
    mocker.patch("shutil.which", return_value="/usr/local/bin/shellcheck")

    config = pyfltr.config.config.create_default_config()
    config.values["bin-runner"] = "direct"

    resolved = pyfltr.command.runner.build_commandline("shellcheck", config)

    assert resolved.executable == "/usr/local/bin/shellcheck"
    assert not resolved.prefix


def test_resolve_bin_commandline_direct_not_found(mocker) -> None:
    """directモードでwhichが失敗した場合、FileNotFoundErrorを送出する。"""
    mocker.patch("shutil.which", return_value=None)

    config = pyfltr.config.config.create_default_config()
    config.values["bin-runner"] = "direct"

    with pytest.raises(FileNotFoundError, match="shellcheck"):
        pyfltr.command.runner.build_commandline("shellcheck", config)


@pytest.mark.real_mise_subprocess
def test_resolve_bin_commandline_mise_success(mocker) -> None:
    """miseモードでツールが利用可能な場合、mise exec形式のコマンドラインを返す。"""
    mocker.patch("shutil.which", return_value="/usr/local/bin/mise")
    mock_run = mocker.patch(
        "subprocess.run",
        return_value=subprocess.CompletedProcess(
            ["mise", "exec", "shellcheck@latest", "--", "shellcheck", "--version"],
            returncode=0,
            stdout="shellcheck 0.9.0",
            stderr="",
        ),
    )

    config = pyfltr.config.config.create_default_config()
    config.values["bin-runner"] = "mise"

    path, prefix = _resolve_bin_commandline_via_two_step("shellcheck", config)

    assert path == "mise"
    assert prefix == ["exec", "shellcheck@latest", "--", "shellcheck"]
    assert mock_run.call_count == 1
    assert mock_run.call_args.args[0] == ["mise", "exec", "shellcheck@latest", "--", "shellcheck", "--version"]


@pytest.mark.real_mise_subprocess
def test_resolve_bin_commandline_mise_custom_version(mocker) -> None:
    """miseモードでカスタムバージョンが指定された場合のテスト。"""
    mocker.patch("shutil.which", return_value="/usr/local/bin/mise")
    mock_run = mocker.patch(
        "subprocess.run",
        return_value=subprocess.CompletedProcess(
            ["mise"],
            returncode=0,
            stdout="",
            stderr="",
        ),
    )

    config = pyfltr.config.config.create_default_config()
    config.values["bin-runner"] = "mise"
    config.values["shellcheck-version"] = "0.9.0"

    path, prefix = _resolve_bin_commandline_via_two_step("shellcheck", config)

    assert path == "mise"
    assert prefix == ["exec", "shellcheck@0.9.0", "--", "shellcheck"]
    assert mock_run.call_count == 1
    assert mock_run.call_args.args[0] == ["mise", "exec", "shellcheck@0.9.0", "--", "shellcheck", "--version"]


def test_resolve_bin_commandline_mise_not_installed_fallback(mocker) -> None:
    """miseモードでmise未導入かつ対象バイナリがPATHにある場合、direct相当でフォールバックする。"""

    def fake_which(name: str) -> str | None:
        if name == "mise":
            return None
        if name == "actionlint":
            return "/usr/local/bin/actionlint"
        return None

    mocker.patch("shutil.which", side_effect=fake_which)

    config = pyfltr.config.config.create_default_config()
    config.values["bin-runner"] = "mise"

    path, prefix = _resolve_bin_commandline_via_two_step("actionlint", config)

    assert path == "/usr/local/bin/actionlint"
    assert not prefix


def test_resolve_bin_commandline_mise_not_installed_no_fallback(mocker) -> None:
    """miseモードでmise未導入かつ対象バイナリもPATHに無い場合、FileNotFoundErrorを送出する。"""
    mocker.patch("shutil.which", return_value=None)

    config = pyfltr.config.config.create_default_config()
    config.values["bin-runner"] = "mise"

    with pytest.raises(FileNotFoundError, match="actionlint"):
        _resolve_bin_commandline_via_two_step("actionlint", config)


@pytest.mark.real_mise_subprocess
def test_resolve_bin_commandline_glab_ci_lint_mise(mocker) -> None:
    """glab-ci-lintはmiseバックエンド経由でglabバイナリを解決する。

    `ci lint`サブコマンドはargs既定値側に持たせる設計のため、bin-runner解決の
    プレフィクスにはサブコマンドが含まれない（commandline組み立て段で付与される）。
    """
    mocker.patch("shutil.which", return_value="/usr/local/bin/mise")
    mock_run = mocker.patch(
        "subprocess.run",
        return_value=subprocess.CompletedProcess(["mise"], returncode=0, stdout="", stderr=""),
    )

    config = pyfltr.config.config.create_default_config()
    config.values["bin-runner"] = "mise"

    path, prefix = _resolve_bin_commandline_via_two_step("glab-ci-lint", config)

    assert path == "mise"
    assert prefix == ["exec", "glab@latest", "--", "glab"]
    assert mock_run.call_count == 1
    assert mock_run.call_args.args[0] == ["mise", "exec", "glab@latest", "--", "glab", "--version"]
    # args既定値にサブコマンドが含まれていることを確認（明示path指定時にも有効化させるため）
    assert config["glab-ci-lint-args"] == ["ci", "lint"]


def test_resolve_bin_commandline_glab_ci_lint_direct(mocker) -> None:
    """directモードではPATH上のglabバイナリを解決する。"""
    mocker.patch("shutil.which", return_value="/usr/local/bin/glab")

    config = pyfltr.config.config.create_default_config()
    config.values["bin-runner"] = "direct"

    resolved = pyfltr.command.runner.build_commandline("glab-ci-lint", config)

    assert resolved.executable == "/usr/local/bin/glab"
    assert not resolved.prefix


@pytest.mark.real_mise_subprocess
def test_resolve_bin_commandline_mise_tool_not_installed(mocker) -> None:
    """miseモードでツールが未インストールの場合、FileNotFoundErrorをstderr付きで送出する。"""
    mocker.patch("shutil.which", return_value="/usr/local/bin/mise")
    mocker.patch(
        "subprocess.run",
        return_value=subprocess.CompletedProcess(
            ["mise"],
            returncode=1,
            stdout="",
            stderr="tool not found",
        ),
    )

    config = pyfltr.config.config.create_default_config()
    config.values["bin-runner"] = "mise"

    with pytest.raises(FileNotFoundError, match="tool not found"):
        _resolve_bin_commandline_via_two_step("ec", config)


@pytest.mark.real_mise_subprocess
def test_resolve_bin_commandline_mise_untrusted_auto_trust_success(mocker) -> None:
    """未信頼エラー→trust成功→再チェック成功の3段で最終的に通常成功扱いになる。"""
    mocker.patch("shutil.which", return_value="/usr/local/bin/mise")
    mock_run = mocker.patch(
        "subprocess.run",
        side_effect=[
            # 1回目の事前チェック: config未信頼で失敗
            subprocess.CompletedProcess(
                ["mise", "exec"],
                returncode=1,
                stdout="",
                stderr="mise ERROR Config files in /path/to/mise.toml are not trusted.",
            ),
            # mise trust --yes --all: 成功
            subprocess.CompletedProcess(
                ["mise", "trust", "--yes", "--all"],
                returncode=0,
                stdout="",
                stderr="",
            ),
            # 2回目の事前チェック（リトライ）: 成功
            subprocess.CompletedProcess(
                ["mise", "exec"],
                returncode=0,
                stdout="shellcheck 0.9.0",
                stderr="",
            ),
        ],
    )

    config = pyfltr.config.config.create_default_config()
    config.values["bin-runner"] = "mise"
    config.values["mise-auto-trust"] = True

    path, prefix = _resolve_bin_commandline_via_two_step("shellcheck", config)

    assert path == "mise"
    assert prefix == ["exec", "shellcheck@latest", "--", "shellcheck"]
    # 事前チェック→trust→リトライの3回が実際に発生したことを確認
    assert mock_run.call_count == 3


@pytest.mark.real_mise_subprocess
def test_resolve_bin_commandline_mise_untrusted_auto_trust_disabled(mocker) -> None:
    """mise-auto-trust=Falseのときtrustが呼ばれず、stderr含むエラーメッセージで失敗する。"""
    mocker.patch("shutil.which", return_value="/usr/local/bin/mise")
    mock_run = mocker.patch(
        "subprocess.run",
        return_value=subprocess.CompletedProcess(
            ["mise", "exec"],
            returncode=1,
            stdout="",
            stderr="mise ERROR Config files in /path/to/mise.toml are not trusted.",
        ),
    )

    config = pyfltr.config.config.create_default_config()
    config.values["bin-runner"] = "mise"
    config.values["mise-auto-trust"] = False

    with pytest.raises(FileNotFoundError, match="not trusted"):
        _resolve_bin_commandline_via_two_step("shellcheck", config)

    # trustコマンドは呼ばれていないことを確認（subprocess.runの呼び出しは1回のみ）
    assert mock_run.call_count == 1


@pytest.mark.real_mise_subprocess
def test_resolve_bin_commandline_mise_other_error_no_retry(mocker) -> None:
    """未信頼以外のエラー（plugin not found等）ではtrustを呼ばずそのまま失敗する。"""
    mocker.patch("shutil.which", return_value="/usr/local/bin/mise")
    mock_run = mocker.patch(
        "subprocess.run",
        return_value=subprocess.CompletedProcess(
            ["mise", "exec"],
            returncode=1,
            stdout="",
            stderr="mise ERROR plugin not found: shellcheck",
        ),
    )

    config = pyfltr.config.config.create_default_config()
    config.values["bin-runner"] = "mise"
    config.values["mise-auto-trust"] = True

    with pytest.raises(FileNotFoundError, match="plugin not found"):
        _resolve_bin_commandline_via_two_step("shellcheck", config)

    # trustコマンドは呼ばれていないことを確認（subprocess.runの呼び出しは1回のみ）
    assert mock_run.call_count == 1


@pytest.mark.real_mise_subprocess
def test_resolve_bin_commandline_mise_untrusted_auto_trust_retry_failure(mocker) -> None:
    """trust後の再チェックも失敗する場合、リトライが1回で打ち切られ通常失敗扱いになる。"""
    mocker.patch("shutil.which", return_value="/usr/local/bin/mise")
    mocker.patch(
        "subprocess.run",
        side_effect=[
            # 1回目の事前チェック: config未信頼で失敗
            subprocess.CompletedProcess(
                ["mise", "exec"],
                returncode=1,
                stdout="",
                stderr="mise ERROR Config files in /path/to/mise.toml are not trusted.",
            ),
            # mise trust --yes --all: 成功
            subprocess.CompletedProcess(
                ["mise", "trust", "--yes", "--all"],
                returncode=0,
                stdout="",
                stderr="",
            ),
            # 2回目の事前チェック（リトライ）: 再度失敗
            subprocess.CompletedProcess(
                ["mise", "exec"],
                returncode=1,
                stdout="",
                stderr="mise ERROR some other failure after trust",
            ),
        ],
    )

    config = pyfltr.config.config.create_default_config()
    config.values["bin-runner"] = "mise"
    config.values["mise-auto-trust"] = True

    with pytest.raises(FileNotFoundError, match="some other failure after trust"):
        _resolve_bin_commandline_via_two_step("shellcheck", config)


@pytest.mark.real_mise_subprocess
def test_resolve_bin_commandline_mise_untrusted_auto_trust_trust_failure(mocker) -> None:
    """mise trustコマンド自体が失敗した場合、trust.stderrを含むエラーで即座に失敗する。"""
    mocker.patch("shutil.which", return_value="/usr/local/bin/mise")
    mocker.patch(
        "subprocess.run",
        side_effect=[
            # 事前チェック: config未信頼で失敗
            subprocess.CompletedProcess(
                ["mise", "exec"],
                returncode=1,
                stdout="",
                stderr="mise ERROR Config files in /path/to/mise.toml are not trusted.",
            ),
            # mise trust --yes --all: 失敗（権限不足等）
            subprocess.CompletedProcess(
                ["mise", "trust", "--yes", "--all"],
                returncode=1,
                stdout="",
                stderr="mise ERROR permission denied: /path/to/mise.toml",
            ),
        ],
    )

    config = pyfltr.config.config.create_default_config()
    config.values["bin-runner"] = "mise"
    config.values["mise-auto-trust"] = True

    with pytest.raises(FileNotFoundError, match="permission denied") as exc_info:
        _resolve_bin_commandline_via_two_step("shellcheck", config)

    # ツール不在ではなくmise設定の信頼が原因と分かる文面にし、手動の信頼・設定・direct切り替えを案内する
    message = pyfltr.command.tool_resolution.format_tool_resolution_failure("shellcheck", str(exc_info.value), config)
    assert "ツールが見つかりません" not in message
    assert "mise trust --yes --all" in message
    assert "permission denied" in message
    assert "`mise-auto-trust`" in message
    assert 'shellcheck-runner = "direct"' in message


@pytest.mark.real_mise_subprocess
def test_ensure_mise_available_passes_stripped_env_to_subprocess(mocker, monkeypatch) -> None:
    """`mise exec --version`呼び出し時にPATHからmise toolパスが除外されたenvが渡る。

    miseが親PATHに自身のtoolエントリを見つけるとtools解決をスキップしてPATH解決へ
    フォールバックする挙動を回避するためのガード。
    """
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
    mocker.patch("shutil.which", return_value="/usr/local/bin/mise")
    mock_run = mocker.patch(
        "subprocess.run",
        return_value=subprocess.CompletedProcess(["mise"], returncode=0, stdout="", stderr=""),
    )

    config = pyfltr.config.config.create_default_config()
    config.values["bin-runner"] = "mise"
    _resolve_bin_commandline_via_two_step("shellcheck", config)

    assert mock_run.call_count == 1
    passed_env = mock_run.call_args.kwargs["env"]
    entries = passed_env["PATH"].split(os.pathsep)
    # mise toolパスは除外、mise/binと通常エントリは保持される
    assert "/home/u/.local/share/mise/installs/dotnet/10.0.0" not in entries
    assert "/home/u/.local/share/mise/dotnet-root" not in entries
    assert "/home/u/.local/share/mise/shims" not in entries
    assert "/home/u/.local/share/mise/bin" in entries
    assert "/usr/bin" in entries


@pytest.mark.real_mise_subprocess
def test_ensure_mise_available_resolution_failure_includes_direct_hint(mocker) -> None:
    """`mise exec`解決失敗時のエラー文面に`{command}-runner = "direct"`への切替案内が含まれる。

    mise registryからツールが消失した場合などにユーザーが回避策へ自力で辿り着けるよう、
    エラー文面でdirect経路への切替案内を提示する。
    """
    mocker.patch("shutil.which", return_value="/usr/local/bin/mise")
    mocker.patch(
        "subprocess.run",
        return_value=subprocess.CompletedProcess(
            ["mise", "exec"],
            returncode=1,
            stdout="",
            stderr="mise ERROR plugin not found: cargo-deny",
        ),
    )

    config = pyfltr.config.config.create_default_config()
    config.values["bin-runner"] = "mise"

    with pytest.raises(FileNotFoundError) as excinfo:
        _resolve_bin_commandline_via_two_step("cargo-deny", config)

    message = str(excinfo.value)
    # 既存のmise由来情報（ERROR内容）は引き続き含まれる。
    assert "plugin not found" in message
    # 回避策の案内が一文として含まれる。
    assert 'cargo-deny-runner = "direct"' in message


@pytest.mark.real_mise_subprocess
def test_ensure_mise_available_passes_stripped_env_to_trust(mocker, monkeypatch) -> None:
    """`mise trust`呼び出し時にもPATHからmise toolパスが除外されたenvが渡る。"""
    monkeypatch.setenv(
        "PATH",
        os.pathsep.join(
            [
                "/home/u/.local/share/mise/installs/dotnet/10.0.0",
                "/home/u/.local/share/mise/shims",
                "/usr/bin",
            ]
        ),
    )
    mocker.patch("shutil.which", return_value="/usr/local/bin/mise")
    mock_run = mocker.patch(
        "subprocess.run",
        side_effect=[
            # 事前チェック: 未信頼で失敗
            subprocess.CompletedProcess(
                ["mise", "exec"],
                returncode=1,
                stdout="",
                stderr="mise ERROR Config files in /path/to/mise.toml are not trusted.",
            ),
            # mise trust --yes --all: 成功
            subprocess.CompletedProcess(
                ["mise", "trust", "--yes", "--all"],
                returncode=0,
                stdout="",
                stderr="",
            ),
            # 再チェック: 成功
            subprocess.CompletedProcess(["mise"], returncode=0, stdout="", stderr=""),
        ],
    )

    config = pyfltr.config.config.create_default_config()
    config.values["bin-runner"] = "mise"
    config.values["mise-auto-trust"] = True
    _resolve_bin_commandline_via_two_step("shellcheck", config)

    # 3回すべてのsubprocess.run呼び出しにmise toolパス除外済みenvが渡る
    assert mock_run.call_count == 3
    for call in mock_run.call_args_list:
        passed_env = call.kwargs["env"]
        entries = passed_env["PATH"].split(os.pathsep)
        assert "/home/u/.local/share/mise/installs/dotnet/10.0.0" not in entries
        assert "/home/u/.local/share/mise/shims" not in entries
        assert "/usr/bin" in entries


def test_bin_tool_spec_all_tools_defined() -> None:
    """全bin系ツールが`build_commandline`経由で解決可能（`mise` runner登録済み）。"""
    # mise runner登録済みのツール一覧。これらすべてに build_commandline を呼んでエラーが出ないことで
    # _BIN_TOOL_SPEC への登録完全性を確認する。
    config = pyfltr.config.config.create_default_config()
    # bin-runner既定がmiseのため、各ツールをmise runner経路でbuild_commandlineが通ることを確認する。
    for tool in _EXPECTED_BIN_TOOLS:
        config.values[f"{tool}-runner"] = "mise"
        result = pyfltr.command.runner.build_commandline(tool, config)
        assert result.executable == "mise", f"{tool}: mise経路で解決されるべき"


def test_bin_tool_spec_structure() -> None:
    """`build_commandline`経由でec・shellcheck・cargo-denyのコマンドライン構造を検証する。"""
    config = pyfltr.config.config.create_default_config()

    # ecはGitHub backendを使い、v4以降の実行ファイル名editorconfig-checkerを起動する。
    config.values["ec-runner"] = "mise"
    result_ec = pyfltr.command.runner.build_commandline("ec", config)
    assert result_ec.executable == "mise"
    assert any("editorconfig-checker" in p for p in result_ec.prefix)
    assert result_ec.prefix[-1] == "editorconfig-checker"

    # shellcheckはmise_backendなしのため、tool specに"shellcheck"が含まれる。
    config.values["shellcheck-runner"] = "mise"
    result_sc = pyfltr.command.runner.build_commandline("shellcheck", config)
    assert result_sc.executable == "mise"
    assert result_sc.prefix[-1] == "shellcheck"

    # cargo-denyはaquaレジストリ経由のため、tool specに"aqua:EmbarkStudios/cargo-deny"が含まれる。
    config.values["cargo-deny-runner"] = "mise"
    result_cd = pyfltr.command.runner.build_commandline("cargo-deny", config)
    assert result_cd.executable == "mise"
    assert any("aqua:EmbarkStudios/cargo-deny" in p for p in result_cd.prefix)
    assert result_cd.prefix[-1] == "cargo-deny"


# --- {command}-runner per-tool解決のテスト ---


def test_resolve_runner_default_for_existing_bin_tools() -> None:
    """既存のbin-runner対応8ツール・lychee・cargo / dotnet系の{command}-runner既定値は"bin-runner"。"""
    config = pyfltr.config.config.create_default_config()
    for command in _EXPECTED_BIN_TOOLS:
        runner, source = pyfltr.command.runner.resolve_runner(command, config)
        assert runner == "bin-runner", f"{command}のrunnerは'bin-runner'であるべき"
        assert source == "default"


def test_resolve_runner_default_for_js_tools() -> None:
    """JS系ツール（eslint / prettier / biome / oxlint / tsc / vitest / markdownlint / textlint / designmd）。

    既定は"js-runner"（カテゴリ委譲値）。
    """
    config = pyfltr.config.config.create_default_config()
    for command in ("eslint", "prettier", "biome", "oxlint", "tsc", "vitest", "markdownlint", "textlint", "designmd"):
        runner, source = pyfltr.command.runner.resolve_runner(command, config)
        assert runner == "js-runner", f"{command}のrunnerは'js-runner'であるべき"
        assert source == "default"


def test_resolve_runner_default_for_direct_tools() -> None:
    """typos / yamllintの既定は"direct"。"""
    config = pyfltr.config.config.create_default_config()
    for command in ("typos", "yamllint"):
        runner, source = pyfltr.command.runner.resolve_runner(command, config)
        assert runner == "direct", f"{command}のrunnerは'direct'であるべき"
        assert source == "default"


def test_resolve_runner_default_for_python_tools() -> None:
    """本体依存のPython系ツールとbanditの既定は"python-runner"（カテゴリ委譲値）。"""
    config = pyfltr.config.config.create_default_config()
    for command in (
        "mypy",
        "pylint",
        "pyright",
        "ty",
        "arid",
        "ruff-check",
        "ruff-format",
        "pytest",
        "uv-sort",
        "bandit",
    ):
        runner, source = pyfltr.command.runner.resolve_runner(command, config)
        assert runner == "python-runner", f"{command}のrunnerは'python-runner'であるべき"
        assert source == "default"


def test_resolve_runner_default_for_uvx_tools() -> None:
    """本体依存から分離したsemgrepとsqlfluffの既定は"uvx"（直接指定値）。"""
    config = pyfltr.config.config.create_default_config()
    for command in ("semgrep", "sqlfluff"):
        runner, source = pyfltr.command.runner.resolve_runner(command, config)
        assert runner == "uvx", f"{command}のrunnerは'uvx'であるべき"
        assert source == "default"


def test_build_commandline_cargo_fmt_via_mise() -> None:
    """cargo-fmtの既定設定（bin-runner=mise）でmise exec形式のコマンドラインが組まれる。"""
    config = pyfltr.config.config.create_default_config()
    resolved = pyfltr.command.runner.build_commandline("cargo-fmt", config)
    assert resolved.commandline == ["mise", "exec", "rust@latest", "--", "cargo"]
    assert resolved.runner == "bin-runner"
    assert resolved.effective_runner == "mise"


def test_build_commandline_cargo_fmt_runner_direct(mocker) -> None:
    """`{command}-runner = "direct"`を明示するとdirect経路で解決される。"""
    mocker.patch("shutil.which", return_value="/usr/local/bin/cargo")
    config = pyfltr.config.config.create_default_config()
    config.values["cargo-fmt-runner"] = "direct"
    resolved = pyfltr.command.runner.build_commandline("cargo-fmt", config)
    assert resolved.commandline == ["/usr/local/bin/cargo"]
    assert resolved.effective_runner == "direct"
    assert resolved.runner_source == "explicit"


def test_build_commandline_dotnet_format_via_mise() -> None:
    """dotnet-formatの既定設定でmise dotnet backend形式になる。"""
    config = pyfltr.config.config.create_default_config()
    resolved = pyfltr.command.runner.build_commandline("dotnet-format", config)
    assert resolved.commandline == ["mise", "exec", "dotnet@latest", "--", "dotnet"]


def test_build_commandline_cargo_deny_via_mise_uses_aqua_backend() -> None:
    """cargo-denyの既定設定でaqua backend経由のtool specが組まれる。

    mise registryからcargo-denyが消失したため、本家aquaレジストリ経由の
    `aqua:EmbarkStudios/cargo-deny`を既定backendとして採用する。
    """
    config = pyfltr.config.config.create_default_config()
    resolved = pyfltr.command.runner.build_commandline("cargo-deny", config)
    assert resolved.commandline == ["mise", "exec", "aqua:EmbarkStudios/cargo-deny@latest", "--", "cargo-deny"]


def test_build_commandline_version_with_at_sign_used_as_full_tool_spec() -> None:
    """`{command}-version`値が`@`を含むときtool spec全体として扱われる。

    既定backendを上書きしたい利用者向けの拡張。例えばcargo-deny-versionに
    `cargo-deny@latest`を与えると、mise registry経由の旧挙動を再現できる。
    """
    config = pyfltr.config.config.create_default_config()
    config.values["cargo-deny-version"] = "cargo-deny@latest"
    resolved = pyfltr.command.runner.build_commandline("cargo-deny", config)
    # 既定backend（aqua:EmbarkStudios/cargo-deny）は適用されず、valueがそのまま渡る。
    assert resolved.commandline == ["mise", "exec", "cargo-deny@latest", "--", "cargo-deny"]


def test_build_commandline_version_with_colon_used_as_full_tool_spec() -> None:
    """`{command}-version`値が`:`を含むとき任意backend指定として扱われる。"""
    config = pyfltr.config.config.create_default_config()
    config.values["cargo-deny-version"] = "aqua:EmbarkStudios/cargo-deny@0.16.0"
    resolved = pyfltr.command.runner.build_commandline("cargo-deny", config)
    assert resolved.commandline == ["mise", "exec", "aqua:EmbarkStudios/cargo-deny@0.16.0", "--", "cargo-deny"]


def test_build_commandline_version_simple_keeps_legacy_format() -> None:
    """単純バージョン文字列の場合は従来通り`<tool>@<version>`で組み立てられる。"""
    config = pyfltr.config.config.create_default_config()
    config.values["shellcheck-version"] = "0.10.0"
    resolved = pyfltr.command.runner.build_commandline("shellcheck", config)
    assert resolved.commandline == ["mise", "exec", "shellcheck@0.10.0", "--", "shellcheck"]


def test_build_commandline_explicit_mise_for_existing_bin_tool() -> None:
    """`{command}-runner = "mise"`明示時もグローバルbin-runnerと独立に動作する。"""
    config = pyfltr.config.config.create_default_config()
    config.values["bin-runner"] = "direct"
    config.values["shellcheck-runner"] = "mise"
    config.values["shellcheck-version"] = "0.10.0"
    resolved = pyfltr.command.runner.build_commandline("shellcheck", config)
    assert resolved.commandline == ["mise", "exec", "shellcheck@0.10.0", "--", "shellcheck"]
    assert resolved.effective_runner == "mise"


def test_build_commandline_mise_on_unregistered_tool_raises() -> None:
    """backend未登録ツールにmise明示しpath未指定の場合はエラー。"""
    config = pyfltr.config.config.create_default_config()
    config.values["typos-runner"] = "mise"
    with pytest.raises(ValueError, match="miseバックエンド"):
        pyfltr.command.runner.build_commandline("typos", config)


def test_build_commandline_js_runner_on_non_js_tool_raises() -> None:
    """js-runner非対応ツールにjs-runner明示しpath未指定の場合はエラー。"""
    config = pyfltr.config.config.create_default_config()
    config.values["typos-runner"] = "js-runner"
    with pytest.raises(ValueError, match="js-runner"):
        pyfltr.command.runner.build_commandline("typos", config)


def test_build_commandline_path_override_wins() -> None:
    """`{command}-path`が非空ならその値でdirect実行する（path-override）。"""
    config = pyfltr.config.config.create_default_config()
    config.values["cargo-fmt-path"] = "/opt/rust/bin/cargo"
    resolved = pyfltr.command.runner.build_commandline("cargo-fmt", config)
    assert resolved.commandline == ["/opt/rust/bin/cargo"]
    assert resolved.runner_source == "path-override"
    assert resolved.effective_runner == "direct"


def test_build_commandline_dotnet_root_priority(monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path) -> None:
    """directモードのdotnet解決ではDOTNET_ROOT環境変数がPATHより優先される。"""
    candidate = tmp_path / "dotnet"
    candidate.write_text("#!/bin/sh\necho stub\n")
    candidate.chmod(0o755)
    monkeypatch.setenv("DOTNET_ROOT", str(tmp_path))

    config = pyfltr.config.config.create_default_config()
    config.values["dotnet-format-runner"] = "direct"
    resolved = pyfltr.command.runner.build_commandline("dotnet-format", config)
    assert resolved.commandline == [str(candidate)]
    assert resolved.effective_runner == "direct"


def test_build_commandline_dotnet_root_ignored_in_mise_mode(monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path) -> None:
    """miseモードではDOTNET_ROOTは参照されず、mise exec形式のままとなる。"""
    candidate = tmp_path / "dotnet"
    candidate.write_text("#!/bin/sh\n")
    candidate.chmod(0o755)
    monkeypatch.setenv("DOTNET_ROOT", str(tmp_path))

    config = pyfltr.config.config.create_default_config()
    # 既定bin-runner=mise → effective=mise
    resolved = pyfltr.command.runner.build_commandline("dotnet-format", config)
    assert resolved.commandline[:2] == ["mise", "exec"]


def test_command_runner_validation_warns_for_unknown_value(tmp_path: pathlib.Path) -> None:
    """`{command}-runner`に不正値を与えるとload_configが警告を発行する。"""
    (tmp_path / "pyproject.toml").write_text('[tool.pyfltr]\ntypos-runner = "bogus"\n')
    pyfltr.config.config.load_config(config_dir=tmp_path)
    messages = [w["message"] for w in pyfltr.warnings_.collected_warnings() if w["source"] == "config"]
    assert any("typos-runner" in m for m in messages)


# --- mise設定記述判定によるtool spec省略仕様 ---


def test_build_commandline_omits_tool_spec_when_mise_config_has_rust(monkeypatch: pytest.MonkeyPatch) -> None:
    """mise設定に `rust` 記述ありかつversion既定値ならtool spec省略形を返す。"""
    monkeypatch.setattr(
        "pyfltr.command.mise.get_mise_active_tools",
        lambda config, *, allow_side_effects=False, cwd=None: pyfltr.command.mise.MiseActiveToolsResult(
            status="ok", tools={"rust": [{"version": "1.83.0"}]}
        ),
    )
    config = pyfltr.config.config.create_default_config()
    resolved = pyfltr.command.runner.build_commandline("cargo-fmt", config)
    # tool spec省略形: `mise exec -- cargo` で起動し、mise設定の解決済み内容に従わせる。
    assert resolved.commandline == ["mise", "exec", "--", "cargo"]
    assert resolved.effective_runner == "mise"
    assert resolved.tool_spec_omitted is True


def test_build_commandline_omits_tool_spec_when_mise_config_has_aqua_cargo_deny(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """mise設定にaqua表記の `aqua:EmbarkStudios/cargo-deny` 記述ありなら省略形になる。"""
    monkeypatch.setattr(
        "pyfltr.command.mise.get_mise_active_tools",
        lambda config, *, allow_side_effects=False, cwd=None: pyfltr.command.mise.MiseActiveToolsResult(
            status="ok", tools={"aqua:EmbarkStudios/cargo-deny": [{"version": "0.16.0"}]}
        ),
    )
    config = pyfltr.config.config.create_default_config()
    resolved = pyfltr.command.runner.build_commandline("cargo-deny", config)
    assert resolved.commandline == ["mise", "exec", "--", "cargo-deny"]
    assert resolved.tool_spec_omitted is True


def test_build_commandline_keeps_tool_spec_when_mise_config_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    """判定辞書が空（記述なし）の場合は従来形 `<backend>@latest` を組み立てる。"""
    monkeypatch.setattr(
        "pyfltr.command.mise.get_mise_active_tools",
        lambda config, *, allow_side_effects=False, cwd=None: pyfltr.command.mise.MiseActiveToolsResult(status="ok"),
    )
    config = pyfltr.config.config.create_default_config()
    resolved = pyfltr.command.runner.build_commandline("cargo-fmt", config)
    assert resolved.commandline == ["mise", "exec", "rust@latest", "--", "cargo"]
    assert resolved.tool_spec_omitted is False


def test_build_commandline_keeps_tool_spec_when_version_explicit(monkeypatch: pytest.MonkeyPatch) -> None:
    """`{command}-version` を具体値で指定した場合は判定結果に関わらず従来形を組み立てる。"""
    # 判定辞書には `rust` 記述があるが、versionが明示されているので利用者の意図を尊重する。
    monkeypatch.setattr(
        "pyfltr.command.mise.get_mise_active_tools",
        lambda config, *, allow_side_effects=False, cwd=None: pyfltr.command.mise.MiseActiveToolsResult(
            status="ok", tools={"rust": [{"version": "1.83.0"}]}
        ),
    )
    config = pyfltr.config.config.create_default_config()
    config.values["cargo-fmt-version"] = "1.84.0"
    resolved = pyfltr.command.runner.build_commandline("cargo-fmt", config)
    assert resolved.commandline == ["mise", "exec", "rust@1.84.0", "--", "cargo"]
    assert resolved.tool_spec_omitted is False


@pytest.mark.parametrize(
    ("target_platform", "architecture", "expected_tool"),
    [
        (
            "linux",
            "x86_64",
            "github:lycheeverse/lychee[asset_pattern=lychee-x86_64-unknown-linux-musl.tar.gz]@latest",
        ),
        ("linux", "aarch64", "github:lycheeverse/lychee@latest"),
        ("darwin", "x86_64", "github:lycheeverse/lychee@latest"),
    ],
)
def test_lychee_mise_asset_selection(mocker, target_platform: str, architecture: str, expected_tool: str) -> None:
    """既定のLinux x64だけmiseのmusl配布物を選ぶ。"""
    mocker.patch.object(sys, "platform", target_platform)
    mocker.patch("pyfltr.command.runner.platform.machine", return_value=architecture)
    mocker.patch(
        "pyfltr.command.mise.get_mise_active_tools",
        return_value=pyfltr.command.mise.MiseActiveToolsResult(status="ok"),
    )
    config = pyfltr.config.config.create_default_config()
    result = pyfltr.command.runner.build_commandline("lychee", config)
    assert result.commandline == ["mise", "exec", expected_tool, "--", "lychee"]


def test_lychee_mise_keeps_complete_version_and_configured_tool(mocker) -> None:
    """完全指定と利用者mise設定を優先する。"""
    mocker.patch.object(sys, "platform", "linux")
    mocker.patch("pyfltr.command.runner.platform.machine", return_value="x86_64")
    active_tools = mocker.patch("pyfltr.command.mise.get_mise_active_tools")
    config = pyfltr.config.config.create_default_config()
    config.values["lychee-version"] = "github:lycheeverse/lychee@0.24.2"
    explicit = pyfltr.command.runner.build_commandline("lychee", config)
    assert explicit.commandline == ["mise", "exec", "github:lycheeverse/lychee@0.24.2", "--", "lychee"]

    config.values["lychee-version"] = "latest"
    active_tools.return_value = pyfltr.command.mise.MiseActiveToolsResult(
        status="ok", tools={"github:lycheeverse/lychee": [{"version": "0.24.2"}]}
    )
    configured = pyfltr.command.runner.build_commandline("lychee", config)
    assert configured.commandline == ["mise", "exec", "--", "lychee"]


def test_build_commandline_allow_side_effects_propagates_to_active_tools(monkeypatch: pytest.MonkeyPatch) -> None:
    """`build_commandline` の `allow_side_effects` が `get_mise_active_tools` に連動して渡る。"""
    received: list[bool] = []

    def fake(
        config: pyfltr.config.model.Config, *, allow_side_effects: bool = False, cwd: object = None
    ) -> pyfltr.command.mise.MiseActiveToolsResult:
        del config, cwd
        received.append(allow_side_effects)
        return pyfltr.command.mise.MiseActiveToolsResult(status="ok")

    monkeypatch.setattr("pyfltr.command.mise.get_mise_active_tools", fake)
    config = pyfltr.config.config.create_default_config()
    pyfltr.command.runner.build_commandline("cargo-fmt", config, allow_side_effects=True)
    pyfltr.command.runner.build_commandline("cargo-fmt", config, allow_side_effects=False)
    pyfltr.command.runner.build_commandline("cargo-fmt", config)  # 既定値はFalse
    assert received == [True, False, False]


# --- ensure_mise_available のtool spec有無分岐 ---


@pytest.mark.real_mise_subprocess
def test_ensure_mise_available_check_args_with_tool_spec(mocker) -> None:
    """tool spec組立形は `mise exec <tool_spec> -- <bin> --version` を呼び出す。"""
    mocker.patch("shutil.which", return_value="/usr/local/bin/mise")
    mock_run = mocker.patch(
        "subprocess.run",
        return_value=subprocess.CompletedProcess(["mise"], returncode=0, stdout="", stderr=""),
    )
    resolved = pyfltr.command.runner.ResolvedCommandline(
        executable="mise",
        prefix=["exec", "rust@latest", "--", "cargo"],
        runner="bin-runner",
        runner_source="default",
        effective_runner="mise",
    )
    config = pyfltr.config.config.create_default_config()
    pyfltr.command.runner.ensure_mise_available(resolved, config, command="cargo-fmt")
    assert mock_run.call_args.args[0] == ["mise", "exec", "rust@latest", "--", "cargo", "--version"]


@pytest.mark.real_mise_subprocess
def test_ensure_mise_available_check_args_without_tool_spec(mocker) -> None:
    """tool spec省略形は `mise exec -- <bin> --version` を呼び出す。"""
    mocker.patch("shutil.which", return_value="/usr/local/bin/mise")
    mock_run = mocker.patch(
        "subprocess.run",
        return_value=subprocess.CompletedProcess(["mise"], returncode=0, stdout="", stderr=""),
    )
    resolved = pyfltr.command.runner.ResolvedCommandline(
        executable="mise",
        prefix=["exec", "--", "cargo"],
        runner="bin-runner",
        runner_source="default",
        effective_runner="mise",
    )
    config = pyfltr.config.config.create_default_config()
    pyfltr.command.runner.ensure_mise_available(resolved, config, command="cargo-fmt")
    assert mock_run.call_args.args[0] == ["mise", "exec", "--", "cargo", "--version"]


@pytest.mark.real_mise_subprocess
def test_ensure_mise_available_error_message_without_tool_spec(mocker) -> None:
    """tool spec省略形での失敗時、エラー文面が `mise exec -- <bin>: ...` 形になる。"""
    mocker.patch("shutil.which", return_value="/usr/local/bin/mise")
    mocker.patch(
        "subprocess.run",
        return_value=subprocess.CompletedProcess(["mise"], returncode=1, stdout="", stderr="mise ERROR could not resolve tool"),
    )
    resolved = pyfltr.command.runner.ResolvedCommandline(
        executable="mise",
        prefix=["exec", "--", "cargo"],
        runner="bin-runner",
        runner_source="default",
        effective_runner="mise",
    )
    config = pyfltr.config.config.create_default_config()
    with pytest.raises(FileNotFoundError) as excinfo:
        pyfltr.command.runner.ensure_mise_available(resolved, config, command="cargo-fmt")
    message = str(excinfo.value)
    assert "mise exec -- cargo" in message
    assert "could not resolve tool" in message


def test_ensure_mise_available_sets_runner_fallback_when_mise_missing(mocker) -> None:
    """mise本体不在時のdirectフォールバックでは `runner_fallback="mise->direct"` がセットされる。

    `effective_runner` も同時に `direct` に上書きされ、後段のJSONL出力で
    fallback判定キーとして3フィールドをまとめて出力する根拠になる。
    """

    # `shutil.which("mise")` のみNoneを返し、他のbin解決は通常通り行われるよう振り分ける。
    def fake_which(name: str) -> str | None:
        if name == "mise":
            return None
        return f"/usr/bin/{name}"

    mocker.patch("pyfltr.command.runner.shutil.which", side_effect=fake_which)
    resolved = pyfltr.command.runner.ResolvedCommandline(
        executable="mise",
        prefix=["exec", "rust@latest", "--", "cargo"],
        runner="bin-runner",
        runner_source="default",
        effective_runner="mise",
    )
    config = pyfltr.config.config.create_default_config()
    after = pyfltr.command.runner.ensure_mise_available(resolved, config, command="cargo-fmt")
    assert after.executable == "/usr/bin/cargo"
    assert after.prefix == []
    assert after.effective_runner == "direct"
    assert after.runner_fallback == "mise->direct"


# --- get_mise_active_tool_key 公開API ---


def test_get_mise_active_tool_key_for_cargo_fmt() -> None:
    """rust backendを使うcargo-fmtは `rust` を返す。"""
    assert pyfltr.command.runner.get_mise_active_tool_key("cargo-fmt") == "rust"


def test_get_mise_active_tool_key_for_cargo_deny() -> None:
    """cargo-denyは `aqua:EmbarkStudios/cargo-deny` を返す（mise.toml記述に合わせた形）。"""
    assert pyfltr.command.runner.get_mise_active_tool_key("cargo-deny") == "aqua:EmbarkStudios/cargo-deny"


def test_get_mise_active_tool_key_for_simple_tool() -> None:
    """`mise_backend` 未設定のツールは `bin_name` をそのまま返す。"""
    assert pyfltr.command.runner.get_mise_active_tool_key("actionlint") == "actionlint"


def test_get_mise_active_tool_key_for_unknown_command() -> None:
    """mise backend未登録のコマンドは `None` を返す。"""
    assert pyfltr.command.runner.get_mise_active_tool_key("ruff-check") is None
    assert pyfltr.command.runner.get_mise_active_tool_key("not-registered") is None


def test_build_commandline_python_tool_uv_with_lock_and_uv_present(setup_uv_runner) -> None:
    """uv.lockありかつuv利用可能な場合は `uv run --frozen <bin>` を返す。"""
    setup_uv_runner(uv_lock=True, uv_available=True)
    config = pyfltr.config.config.create_default_config()
    resolved = pyfltr.command.runner.build_commandline("mypy", config)
    assert resolved.commandline == ["uv", "run", "--frozen", "mypy"]
    # per-tool値は新スキーマでカテゴリ委譲値`python-runner`が既定。
    assert resolved.runner == "python-runner"
    assert resolved.effective_runner == "uv"
    assert resolved.runner_source == "default"


def test_build_commandline_python_tool_uv_falls_back_when_uv_missing(setup_uv_runner) -> None:
    """uv不在の場合はdirect（shutil.which）へフォールバックし、`runner_fallback="uv->direct"` が付く。"""
    setup_uv_runner(uv_lock=True, uv_available=False, python_bin_dir="/fake/bin")
    config = pyfltr.config.config.create_default_config()
    resolved = pyfltr.command.runner.build_commandline("mypy", config)
    assert resolved.commandline == ["/fake/bin/mypy"]
    assert resolved.effective_runner == "direct"
    assert resolved.runner_fallback == "uv->direct"


def test_build_commandline_python_tool_uv_falls_back_when_lock_missing(setup_uv_runner) -> None:
    """uv.lock不在の場合はdirect（shutil.which）へフォールバックし、`runner_fallback="uv->direct"` が付く。"""
    setup_uv_runner(uv_lock=False, uv_available=True, python_bin_dir="/fake/bin")
    config = pyfltr.config.config.create_default_config()
    resolved = pyfltr.command.runner.build_commandline("mypy", config)
    assert resolved.commandline == ["/fake/bin/mypy"]
    assert resolved.effective_runner == "direct"
    assert resolved.runner_fallback == "uv->direct"


def test_build_commandline_python_tool_uv_no_fallback_when_normal(setup_uv_runner) -> None:
    """通常経路（uv指定通りuv解決）では `runner_fallback` は None のまま。"""
    setup_uv_runner(uv_lock=True, uv_available=True)
    config = pyfltr.config.config.create_default_config()
    resolved = pyfltr.command.runner.build_commandline("mypy", config)
    assert resolved.effective_runner == "uv"
    assert resolved.runner_fallback is None


def test_build_commandline_python_tool_uv_path_override_wins(setup_uv_runner) -> None:
    """`mypy-path` 明示指定時はuv経由にならずpath値が採用される。

    path-override由来のdirectは退行ではないため `runner_fallback` は None のまま。
    """
    setup_uv_runner(uv_lock=True, uv_available=True)
    config = pyfltr.config.config.create_default_config()
    config.values["mypy-path"] = "/usr/bin/mypy-custom"
    resolved = pyfltr.command.runner.build_commandline("mypy", config)
    assert resolved.commandline == ["/usr/bin/mypy-custom"]
    assert resolved.runner_source == "path-override"
    assert resolved.effective_runner == "direct"
    assert resolved.runner_fallback is None


def test_build_commandline_ruff_format_resolves_ruff_bin(setup_uv_runner) -> None:
    """`ruff-format` でuv経路のbin名は `ruff` になる（コマンド名と異なる点が重要）。"""
    setup_uv_runner(uv_lock=True, uv_available=True)
    config = pyfltr.config.config.create_default_config()
    resolved = pyfltr.command.runner.build_commandline("ruff-format", config)
    assert resolved.commandline == ["uv", "run", "--frozen", "ruff"]
    assert resolved.effective_runner == "uv"


def test_build_commandline_ruff_check_resolves_ruff_bin(setup_uv_runner) -> None:
    """`ruff-check` でuv経路のbin名は `ruff` になる。"""
    setup_uv_runner(uv_lock=True, uv_available=True)
    config = pyfltr.config.config.create_default_config()
    resolved = pyfltr.command.runner.build_commandline("ruff-check", config)
    assert resolved.commandline == ["uv", "run", "--frozen", "ruff"]
    assert resolved.effective_runner == "uv"


def test_build_commandline_python_tool_direct_uses_python_tool_bin_map(monkeypatch) -> None:
    """`mypy-runner = "direct"` 明示時は `_resolve_python_tool_direct` 経由でshutil.whichを使う。"""
    monkeypatch.setattr("pyfltr.command.runner.shutil.which", lambda name: f"/custom/bin/{name}" if name == "mypy" else None)
    config = pyfltr.config.config.create_default_config()
    config.values["mypy-runner"] = "direct"
    resolved = pyfltr.command.runner.build_commandline("mypy", config)
    assert resolved.commandline == ["/custom/bin/mypy"]
    assert resolved.effective_runner == "direct"
    assert resolved.runner_source == "explicit"


def test_build_commandline_uv_runner_on_non_python_tool_raises() -> None:
    """`typos-runner = "uv"` × path未指定でエラー。"""
    config = pyfltr.config.config.create_default_config()
    config.values["typos-runner"] = "uv"
    with pytest.raises(pyfltr.command.runner.RunnerMismatchError, match="Python系"):
        pyfltr.command.runner.build_commandline("typos", config)


def test_build_commandline_uvx_runner_on_non_python_tool_raises() -> None:
    """`typos-runner = "uvx"` × path未指定でエラー。"""
    config = pyfltr.config.config.create_default_config()
    config.values["typos-runner"] = "uvx"
    with pytest.raises(pyfltr.command.runner.RunnerMismatchError, match="Python系"):
        pyfltr.command.runner.build_commandline("typos", config)


def test_build_commandline_python_tool_uvx_with_uvx_present(monkeypatch: pytest.MonkeyPatch) -> None:
    """`mypy-runner = "uvx"` ×uvx shim利用可能で `uvx <bin>` 形を返す。

    uvx経路は`uv.lock`を参照せず、`{command}-version`設定とも連動しない仕様を持つため、
    setup_uv_runner（`cwd_has_uv_lock`差し替えあり）には依存させない。
    """
    monkeypatch.setattr(pyfltr.command.runner, "ensure_uvx_available", lambda: True)
    config = pyfltr.config.config.create_default_config()
    config.values["mypy-runner"] = "uvx"
    resolved = pyfltr.command.runner.build_commandline("mypy", config)
    assert resolved.commandline == ["uvx", "mypy"]
    assert resolved.runner == "uvx"
    assert resolved.effective_runner == "uvx"
    assert resolved.runner_source == "explicit"


def test_build_commandline_python_tool_uvx_falls_back_when_uvx_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    """uvx不在の場合はdirect（shutil.which）へフォールバックし、`runner_fallback="uvx->direct"` が付く。"""
    monkeypatch.setattr(pyfltr.command.runner, "ensure_uvx_available", lambda: False)
    monkeypatch.setattr(
        "pyfltr.command.runner.shutil.which",
        lambda name: f"/fake/bin/{name}" if name in pyfltr.command.runner.PYTHON_TOOL_BIN.values() else None,
    )
    config = pyfltr.config.config.create_default_config()
    config.values["mypy-runner"] = "uvx"
    resolved = pyfltr.command.runner.build_commandline("mypy", config)
    assert resolved.commandline == ["/fake/bin/mypy"]
    assert resolved.effective_runner == "direct"
    assert resolved.runner_fallback == "uvx->direct"


def test_build_commandline_python_runner_delegation_to_uvx(monkeypatch: pytest.MonkeyPatch) -> None:
    """`mypy-runner = "python-runner"` × グローバル`python-runner = "uvx"` でuvx経路へ解決される。"""
    monkeypatch.setattr(pyfltr.command.runner, "ensure_uvx_available", lambda: True)
    config = pyfltr.config.config.create_default_config()
    config.values["python-runner"] = "uvx"
    resolved = pyfltr.command.runner.build_commandline("mypy", config)
    assert resolved.commandline == ["uvx", "mypy"]
    # per-tool値はカテゴリ委譲値、effective値は委譲先の直接指定値。
    assert resolved.runner == "python-runner"
    assert resolved.effective_runner == "uvx"


def test_build_commandline_python_runner_delegation_to_direct(monkeypatch: pytest.MonkeyPatch) -> None:
    """`mypy-runner = "python-runner"` × グローバル`python-runner = "direct"` でdirect経路へ解決される。"""
    monkeypatch.setattr(
        "pyfltr.command.runner.shutil.which",
        lambda name: f"/fake/bin/{name}" if name == "mypy" else None,
    )
    config = pyfltr.config.config.create_default_config()
    config.values["python-runner"] = "direct"
    resolved = pyfltr.command.runner.build_commandline("mypy", config)
    assert resolved.commandline == ["/fake/bin/mypy"]
    assert resolved.runner == "python-runner"
    assert resolved.effective_runner == "direct"


def test_build_commandline_js_effective_runner_has_no_prefix() -> None:
    """JSランナー解決後の `effective_runner` は `js-` プレフィクスを持たず、直接指定値そのものになる。

    対称12値刷新でプレフィクス付き値は廃止しており、純粋な直接指定値だけが取り得る値となる。
    """
    config = pyfltr.config.config.create_default_config()
    # textlintはJS_TOOL_BINに登録されたツール。pnpx既定でJS_RUNNERS既定値`pnpx`を採用する。
    resolved_pnpx = pyfltr.command.runner.build_commandline("textlint", config)
    assert resolved_pnpx.effective_runner == "pnpx"

    config.values["js-runner"] = "pnpm"
    resolved_pnpm = pyfltr.command.runner.build_commandline("textlint", config)
    assert resolved_pnpm.effective_runner == "pnpm"


def test_build_commandline_js_per_tool_direct_value() -> None:
    """per-tool直接指定値（例: `textlint-runner = "pnpm"`）が委譲経路と同じロジックで解決される。"""
    config = pyfltr.config.config.create_default_config()
    # グローバルはpnpx（既定）のままで、per-toolだけpnpmに切り替える。
    config.values["textlint-runner"] = "pnpm"
    resolved = pyfltr.command.runner.build_commandline("textlint", config)
    assert resolved.commandline == ["pnpm", "exec", "textlint"]
    assert resolved.runner == "pnpm"
    assert resolved.effective_runner == "pnpm"


def test_build_commandline_uv_runner_with_path_override_skips_validation() -> None:
    """`{command}-runner = "uv"` × 未登録ツール × `{command}-path` 指定は エラー扱いせずpath値を採用する。

    F1の挙動変更: 明示runner × 未登録ツールでも `{command}-path` 指定時はpath-override経路で
    direct実行に解決される（利用者が明示的にパスを示している以上、利用者の意図を優先する判断）。
    `{command}-path` 未指定の場合のみ後段の分岐でエラー化する（path未指定の `mise` / `js-runner` ケースは
    既存の `test_build_commandline_*_on_unregistered_tool_raises` 系テストで担保済み）。
    """
    config = pyfltr.config.config.create_default_config()
    config.values["typos-runner"] = "uv"
    config.values["typos-path"] = "/opt/typos/bin/typos"
    resolved = pyfltr.command.runner.build_commandline("typos", config)
    assert resolved.commandline == ["/opt/typos/bin/typos"]
    assert resolved.runner_source == "path-override"
    assert resolved.effective_runner == "direct"


def test_build_commandline_direct_unregistered_falls_back_to_path_resolution(monkeypatch: pytest.MonkeyPatch) -> None:
    """登録テーブル未登録のツール×runner=direct×path未指定はコマンド名のPATH解決にフォールバックする。

    `typos` / `yamllint` のように `_BIN_TOOL_SPEC` / `PYTHON_TOOL_BIN` / `JS_TOOL_BIN` の
    いずれにも登録されていないツールは、コマンド名そのものを `shutil.which` で解決する経路を通る。
    既定のtypos / yamllint設定（runner=direct、path空文字列）で本フォールバックが発動する。
    """
    monkeypatch.setattr(
        "pyfltr.command.runner.shutil.which",
        lambda name: f"/usr/local/bin/{name}" if name in ("typos", "yamllint") else None,
    )
    config = pyfltr.config.config.create_default_config()
    resolved_typos = pyfltr.command.runner.build_commandline("typos", config)
    assert resolved_typos.commandline == ["/usr/local/bin/typos"]
    assert resolved_typos.effective_runner == "direct"
    resolved_yamllint = pyfltr.command.runner.build_commandline("yamllint", config)
    assert resolved_yamllint.commandline == ["/usr/local/bin/yamllint"]
    assert resolved_yamllint.effective_runner == "direct"


def test_build_commandline_direct_unregistered_raises_when_not_on_path(monkeypatch: pytest.MonkeyPatch) -> None:
    """フォールバック経路でPATH解決に失敗した場合は `FileNotFoundError` を送出する。"""
    monkeypatch.setattr("pyfltr.command.runner.shutil.which", lambda _name: None)
    config = pyfltr.config.config.create_default_config()
    with pytest.raises(FileNotFoundError, match="typos"):
        pyfltr.command.runner.build_commandline("typos", config)


def test_build_commandline_uv_finds_workspace_root_lock(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: pathlib.Path,
) -> None:
    """uv runnerはmember cwdからworkspace rootのlockへ到達する。"""
    workspace_root = tmp_path / "workspace"
    member = workspace_root / "packages" / "app"
    member.mkdir(parents=True)
    (workspace_root / "uv.lock").write_text("", encoding="utf-8")
    monkeypatch.setattr(pyfltr.command.runner, "ensure_uv_available", lambda: True)
    config = pyfltr.config.config.create_default_config()

    resolved = pyfltr.command.runner.build_commandline(
        "mypy",
        config,
        cwd=member,
        uv_workspace_root=workspace_root,
    )

    assert resolved.commandline == ["uv", "run", "--frozen", "mypy"]


def test_build_commandline_direct_js_uses_explicit_cwd(tmp_path: pathlib.Path) -> None:
    """JS direct runnerは明示cwd配下のローカル実行ファイルを使う。"""
    work_dir = tmp_path / "project"
    local_bin = work_dir / "node_modules" / ".bin" / "textlint"
    local_bin.parent.mkdir(parents=True)
    local_bin.write_text("", encoding="utf-8")
    config = pyfltr.config.config.create_default_config()
    config.values["textlint-runner"] = "direct"

    resolved = pyfltr.command.runner.build_commandline("textlint", config, cwd=work_dir)

    assert resolved.commandline == [str(local_bin)]


def test_build_invocation_argv_disables_textlint_cache_only_in_fix_stage() -> None:
    """textlintの修正段は利用者引数の後ろへ`--no-cache`を置き、通常段は利用者のキャッシュ指定を保つ。"""
    config = pyfltr.config.config.create_default_config()
    config.values["textlint-args"] = ["--cache"]

    fix_argv = pyfltr.command.runner.build_invocation_argv("textlint", config, ["textlint"], ["--quiet"], fix_stage=True)
    lint_argv = pyfltr.command.runner.build_invocation_argv("textlint", config, ["textlint"], [], fix_stage=False)

    assert fix_argv[-1] == "--no-cache"
    assert fix_argv.index("--cache") < fix_argv.index("--quiet") < fix_argv.index("--no-cache")
    assert "--cache" in lint_argv
    assert "--no-cache" not in lint_argv
