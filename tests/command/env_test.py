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
    """サプライチェーン対策用の環境変数が既定値で注入される。"""
    monkeypatch.delenv("UV_EXCLUDE_NEWER", raising=False)
    monkeypatch.delenv("NPM_CONFIG_MINIMUM_RELEASE_AGE", raising=False)

    config = pyfltr.config.config.create_default_config()
    env = pyfltr.command.env.build_subprocess_env(config, "pytest")

    assert env["UV_EXCLUDE_NEWER"] == "1 day"
    assert env["NPM_CONFIG_MINIMUM_RELEASE_AGE"] == "1440"


def test_build_subprocess_env_sets_python_utf8_mode() -> None:
    """サブプロセスはPython UTF-8モードで動く。"""
    config = pyfltr.config.config.create_default_config()
    env = pyfltr.command.env.build_subprocess_env(config, "pytest")

    assert env["PYTHONUTF8"] == "1"


def test_build_subprocess_env_preserves_existing_supply_chain_values(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """ユーザーが既に環境変数を設定している場合は既存値を尊重する。"""
    monkeypatch.setenv("UV_EXCLUDE_NEWER", "1 week")
    monkeypatch.setenv("NPM_CONFIG_MINIMUM_RELEASE_AGE", "10080")

    config = pyfltr.config.config.create_default_config()
    env = pyfltr.command.env.build_subprocess_env(config, "pytest")

    assert env["UV_EXCLUDE_NEWER"] == "1 week"
    assert env["NPM_CONFIG_MINIMUM_RELEASE_AGE"] == "10080"


def test_build_subprocess_env_via_mise_strips_mise_tool_paths(monkeypatch: pytest.MonkeyPatch) -> None:
    """`via_mise=True`のとき、PATHからmise toolパスが除外される。"""
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
    # mise本体バイナリディレクトリと無関係エントリは保持される
    assert "/home/u/.local/share/mise/bin" in entries
    assert "/usr/bin" in entries


def test_build_subprocess_env_default_keeps_mise_tool_paths(monkeypatch: pytest.MonkeyPatch) -> None:
    """既定（`via_mise=False`）ではmise toolパスを除外しない。"""
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
    """実効runnerが`uv`なら継承した`VIRTUAL_ENV`を子プロセスへ渡さない。

    uvは実効cwdからプロジェクト環境を解決するため、実効cwdと結び付かない
    親の`VIRTUAL_ENV`が残ると環境不一致警告が出る。
    """
    monkeypatch.setenv("VIRTUAL_ENV", "/repo/.venv")

    config = pyfltr.config.config.create_default_config()
    env = pyfltr.command.env.build_subprocess_env(config, "pytest", effective_runner="uv")

    assert "VIRTUAL_ENV" not in env


@pytest.mark.parametrize("effective_runner", [None, "direct", "uvx", "mise", "pnpm"])
def test_build_subprocess_env_keeps_virtual_env_for_other_runners(
    monkeypatch: pytest.MonkeyPatch, effective_runner: str | None
) -> None:
    """`uv`以外の実効runnerでは`VIRTUAL_ENV`の継承を維持する。"""
    monkeypatch.setenv("VIRTUAL_ENV", "/repo/.venv")

    config = pyfltr.config.config.create_default_config()
    env = pyfltr.command.env.build_subprocess_env(config, "pytest", effective_runner=effective_runner)

    assert env["VIRTUAL_ENV"] == "/repo/.venv"


def test_build_subprocess_env_npm_config_actually_effective(monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path) -> None:
    """注入したNPM_CONFIG_MINIMUM_RELEASE_AGEが実際にnpm互換ツールに反映されることを確認する。

    環境変数名がtypoしたり、仕様変更で適用されなくなったりした場合に検知する。
    既定値（1440）は実行環境のグローバル設定と区別できないため、
    ユーザー既定値優先（setdefault）の動作を利用して非標準値4321を注入し検証する。

    検証にはnpmを使用する。pnpmはインストール方法やバージョンにより
    NPM_CONFIG_*環境変数の読み取り動作が不安定なため（pnpm config getが
    env varを無視するケースがある）、npmのconfig getで代替する。
    npmはNPM_CONFIG_*規約の本家であり、動作が安定している。
    """
    # npmの設定ファイル読込を避けるため、隔離したHOMEを用意する。
    # XDG_CONFIG_HOMEも明示的に隔離してグローバル設定の干渉を排除する。
    original_home = pathlib.Path(os.environ.get("HOME") or os.environ["USERPROFILE"])
    mise_config = original_home / ".config" / "mise" / "config.toml"
    monkeypatch.setenv("MISE_TRUSTED_CONFIG_PATHS", str(mise_config))
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / ".config"))
    # 非標準値を設定し、build_subprocess_envがそのまま通すことを利用する。
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
    """Windows（`os.name == "nt"`）では`Path`キーも`PATH`として採用される。"""
    monkeypatch.setattr("pyfltr.command.env.os.name", "nt")
    assert pyfltr.command.env.get_env_path({"Path": "/tmp/bin"}) == "/tmp/bin"
    assert pyfltr.command.env.get_env_path({"path": "/tmp/bin"}) == "/tmp/bin"
    # PATH大文字が存在する場合も取れる
    assert pyfltr.command.env.get_env_path({"PATH": "/tmp/bin"}) == "/tmp/bin"


def test_get_env_path_posix_strict_key(monkeypatch) -> None:
    """POSIXでは`env.get("PATH")`のみを使い、`Path`キーは採用しない。

    `env={"Path": "/tmp/bin", "PATH": "/usr/bin"}`で解決側とPopen実行時側のPATHが
    不一致になる事故を防ぐ設計。
    """
    monkeypatch.setattr("pyfltr.command.env.os.name", "posix")
    assert pyfltr.command.env.get_env_path({"Path": "/tmp/bin"}) is None
    assert pyfltr.command.env.get_env_path({"PATH": "/usr/bin"}) == "/usr/bin"
    # 両方あってもPATHのみを採用する
    assert pyfltr.command.env.get_env_path({"Path": "/tmp/bin", "PATH": "/usr/bin"}) == "/usr/bin"


def test_dedupe_environ_path_posix_trailing_slash_treated_as_duplicate(monkeypatch) -> None:
    """POSIXでは末尾スラッシュのみ差のエントリを重複扱いし、初出（末尾スラッシュなし）を保持する。"""
    monkeypatch.setattr("pyfltr.command.env.os.name", "posix")
    monkeypatch.setattr("pyfltr.command.env.os.pathsep", ":")
    env: dict[str, str] = {"PATH": ":".join(["/usr/bin", "/opt/bin", "/usr/bin", "/usr/local/bin", "/opt/bin/"])}
    assert pyfltr.command.env.dedupe_environ_path(env) is True
    assert env["PATH"] == ":".join(["/usr/bin", "/opt/bin", "/usr/local/bin"])


def test_dedupe_environ_path_posix_case_sensitive(monkeypatch) -> None:
    """POSIXでは大文字小文字差があるエントリは別エントリとして扱う。"""
    monkeypatch.setattr("pyfltr.command.env.os.name", "posix")
    monkeypatch.setattr("pyfltr.command.env.os.pathsep", ":")
    env: dict[str, str] = {"PATH": ":".join(["/usr/bin", "/USR/Bin"])}
    # 大文字小文字が異なるため重複扱いされず両方残る
    assert pyfltr.command.env.dedupe_environ_path(env) is False
    assert env["PATH"] == ":".join(["/usr/bin", "/USR/Bin"])


def test_dedupe_environ_path_windows_case_insensitive(monkeypatch) -> None:
    """Windowsでは大文字小文字差・パス区切り差を吸収して重複扱いし、初出の表記を保持する。"""
    monkeypatch.setattr("pyfltr.command.env.os.name", "nt")
    monkeypatch.setattr("pyfltr.command.env.os.pathsep", ";")
    env: dict[str, str] = {"PATH": ";".join(["C:\\Tools\\Mise\\bin", "c:/tools/mise/bin", "C:\\Windows"])}
    assert pyfltr.command.env.dedupe_environ_path(env) is True
    assert env["PATH"] == ";".join(["C:\\Tools\\Mise\\bin", "C:\\Windows"])


def test_dedupe_environ_path_windows_trailing_backslash_treated_as_duplicate(monkeypatch) -> None:
    """Windowsでは末尾区切り差のエントリも重複扱いする（境界値ケース）。"""
    monkeypatch.setattr("pyfltr.command.env.os.name", "nt")
    monkeypatch.setattr("pyfltr.command.env.os.pathsep", ";")
    env: dict[str, str] = {"PATH": ";".join(["C:/Tools/Mise/bin", "c:\\tools\\mise\\bin\\"])}
    assert pyfltr.command.env.dedupe_environ_path(env) is True
    # 初出の表記が保持される
    assert env["PATH"] == "C:/Tools/Mise/bin"


def test_dedupe_environ_path_keeps_empty_entry_only_once(monkeypatch) -> None:
    """空エントリ（POSIXでcwd相当）も最初の1回のみ残す。"""
    monkeypatch.setattr("pyfltr.command.env.os.name", "posix")
    monkeypatch.setattr("pyfltr.command.env.os.pathsep", ":")
    env: dict[str, str] = {"PATH": "/usr/bin::/opt/bin:"}
    assert pyfltr.command.env.dedupe_environ_path(env) is True
    assert env["PATH"] == ":".join(["/usr/bin", "", "/opt/bin"])


def test_dedupe_environ_path_writes_back_with_same_key(monkeypatch) -> None:
    """書き戻しは検出したPATHキー名を保持する（`Path` / `PATH`揺れ対応）。"""
    monkeypatch.setattr("pyfltr.command.env.os.name", "nt")
    monkeypatch.setattr("pyfltr.command.env.os.pathsep", ";")
    env: dict[str, str] = {"Path": ";".join(["c:/tools", "C:/Tools"])}
    assert pyfltr.command.env.dedupe_environ_path(env) is True
    assert "Path" in env
    assert "PATH" not in env
    assert env["Path"] == "c:/tools"


def test_dedupe_environ_path_no_change_when_unique(monkeypatch) -> None:
    """重複が無ければ書き換え不要として`False`を返す。"""
    monkeypatch.setattr("pyfltr.command.env.os.name", "posix")
    monkeypatch.setattr("pyfltr.command.env.os.pathsep", ":")
    env: dict[str, str] = {"PATH": "/usr/bin:/opt/bin"}
    assert pyfltr.command.env.dedupe_environ_path(env) is False
    assert env["PATH"] == "/usr/bin:/opt/bin"


def test_dedupe_environ_path_returns_false_when_path_missing() -> None:
    """PATH未設定なら何もせず`False`。"""
    env: dict[str, str] = {}
    assert pyfltr.command.env.dedupe_environ_path(env) is False
    assert not env


def test_build_mise_subprocess_env_strips_installs_and_dotnet_root_and_shims(monkeypatch) -> None:
    """installs/・dotnet-root・shimsの3種は除外され、mise/binと無関係エントリは保持される。"""
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
    # mise/bin（本体バイナリディレクトリ）と無関係エントリは保持される
    assert "/home/u/.local/share/mise/bin" in entries
    assert "/usr/bin" in entries


def test_build_mise_subprocess_env_unrelated_path_preserved(monkeypatch) -> None:
    """miseと無関係なパスや空エントリは除外されない。"""
    monkeypatch.setattr("pyfltr.command.env.os.name", "posix")
    monkeypatch.setattr("pyfltr.command.env.os.pathsep", ":")
    src: dict[str, str] = {"PATH": ":".join(["/usr/bin", "", "/opt/bin"])}
    new = pyfltr.command.env.build_mise_subprocess_env(src)
    assert new["PATH"] == "/usr/bin::/opt/bin"


def test_build_mise_subprocess_env_windows_case_and_separator(monkeypatch) -> None:
    """Windowsでは大文字混在・`\\`区切りでもmise toolパスを除外し、mise/binは保護する。"""
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
    """`build_mise_subprocess_env`は入力envを破壊しない（純関数）。"""
    monkeypatch.setattr("pyfltr.command.env.os.name", "posix")
    monkeypatch.setattr("pyfltr.command.env.os.pathsep", ":")
    src: dict[str, str] = {"PATH": "/home/u/.local/share/mise/installs/dotnet/10.0:/usr/bin"}
    new = pyfltr.command.env.build_mise_subprocess_env(src)

    # 入力辞書は変更されない
    assert src == {"PATH": "/home/u/.local/share/mise/installs/dotnet/10.0:/usr/bin"}
    # 戻り値はmise toolパスを除外したPATHを持つ
    assert new["PATH"] == "/usr/bin"


def test_build_mise_subprocess_env_handles_missing_path() -> None:
    """PATH未設定時は単にコピーを返す。"""
    src: dict[str, str] = {"FOO": "bar"}
    new = pyfltr.command.env.build_mise_subprocess_env(src)
    assert new == src
    assert new is not src
