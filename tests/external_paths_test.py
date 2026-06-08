"""外部パス指定時のツール別挙動のテスト。

外部パス（起点cwd配下にない絶対パス）を3分類で扱う実装の境界確認。

- 注入対象（`config_arg_template`指定）: `markdownlint` / `textlint`
- 除外対象（`allows_external_paths=False`）: `pre-commit` / `pytest` / `vitest` /
  `cargo-test` / `dotnet-test` / `gitleaks` / `semgrep` /
  `uv-audit` / `pnpm-audit` / `npm-audit` / `yarn-audit`
- 素通し対象（既定）: 上記以外（`ruff-check` を代表として確認）

テストは`execute_command`経由で実行し、`run_subprocess_with_timeout`をfakeで差し替えて
subprocess起動を回避する。
"""

import argparse
import pathlib
import typing

import pytest

import pyfltr.command.core_
import pyfltr.command.dispatcher
import pyfltr.command.process
import pyfltr.command.runner
import pyfltr.command.subprojects
import pyfltr.config.config
import pyfltr.warnings_
from tests import conftest as _testconf

_DEFAULT_PREFIXES: dict[str, list[str]] = {
    "markdownlint": ["pnpx", "markdownlint-cli2"],
    "textlint": ["pnpx", "textlint"],
    "pre-commit": ["pre-commit", "run"],
    "pytest": ["pytest"],
    "vitest": ["pnpx", "vitest"],
    "cargo-test": ["cargo", "test"],
    "dotnet-test": ["dotnet", "test"],
    "gitleaks": ["gitleaks"],
    "semgrep": ["semgrep"],
    "ruff-check": ["ruff", "check"],
}


def _patch_build_commandline(monkeypatch: pytest.MonkeyPatch) -> None:
    """`build_commandline` / `ensure_mise_available` を副作用なしに固定するヘルパー。

    実際のツール解決はテストの安定性を下げるため、`_DEFAULT_PREFIXES` から
    `commandline_prefix` を組み立てて返す`ResolvedCommandline`を返す代用関数に差し替える。
    """

    def _fake_build_commandline(
        command: str,
        config: pyfltr.config.config.Config,
        *,
        allow_side_effects: bool = False,
        cwd: pathlib.Path | None = None,
    ) -> pyfltr.command.runner.ResolvedCommandline:
        del config, allow_side_effects, cwd
        prefix = _DEFAULT_PREFIXES.get(command, [command])
        return pyfltr.command.runner.ResolvedCommandline(
            executable=prefix[0],
            prefix=prefix[1:],
            runner="direct",
            runner_source="default",
            effective_runner="direct",
        )

    def _fake_ensure_mise_available(
        resolved: pyfltr.command.runner.ResolvedCommandline,
        config: pyfltr.config.config.Config,
        *,
        command: str,
        cwd: pathlib.Path | None = None,
    ) -> pyfltr.command.runner.ResolvedCommandline:
        del config, command, cwd
        return resolved

    monkeypatch.setattr(pyfltr.command.runner, "build_commandline", _fake_build_commandline)
    monkeypatch.setattr(pyfltr.command.runner, "ensure_mise_available", _fake_ensure_mise_available)


def _patch_subprocess(monkeypatch: pytest.MonkeyPatch) -> list[list[str]]:
    """`run_subprocess_with_timeout` を成功返却するfakeに差し替え、呼び出されたcommandlineを捕捉する。

    戻り値は呼び出し順にcommandlineを蓄積するリスト。
    """
    captured: list[list[str]] = []

    def _fake_run_subprocess(
        commandline: list[str],
        env: dict[str, str],
        on_output: typing.Callable[[str], None] | None = None,
        **kwargs: object,
    ) -> pyfltr.command.process.CompletedProcessWithTimeoutInfo:
        del env, on_output, kwargs
        captured.append(list(commandline))
        return pyfltr.command.process.CompletedProcessWithTimeoutInfo(
            args=commandline,
            returncode=0,
            stdout="",
            timeout_exceeded=False,
        )

    monkeypatch.setattr(pyfltr.command.process, "run_subprocess_with_timeout", _fake_run_subprocess)
    return captured


def _enable(config: pyfltr.config.config.Config, commands: typing.Iterable[str]) -> None:
    for c in commands:
        config.values[c] = True


def _make_external(tmp_path: pathlib.Path) -> pathlib.Path:
    """起点cwd外の絶対パス（一時ディレクトリ外）を1件作成して返す。"""
    parent = tmp_path.parent / f"external-{tmp_path.name}"
    parent.mkdir(parents=True, exist_ok=True)
    target = parent / "plan.md"
    target.write_text("# plan\n", encoding="utf-8")
    return target.resolve()


@pytest.fixture
def _ext_workspace(tmp_path: pathlib.Path) -> tuple[pathlib.Path, pathlib.Path, pathlib.Path]:
    """起点cwd・内部対象ファイル・外部対象ファイルの3点セットを返すフィクスチャ。"""
    internal = tmp_path / "doc.md"
    internal.write_text("# internal\n", encoding="utf-8")
    external = _make_external(tmp_path)
    return tmp_path, internal, external


# --- _is_external_path の境界を execute_command 経由で検証 -----------------------
# allows_external_paths=False のツール（pytest）を使い、
# 相対パス・内部絶対パス・外部絶対パスの3分岐を確認する。


def test_is_external_path_relative_is_internal(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: pathlib.Path,
) -> None:
    """相対パスは起点cwd配下扱いとなり、外部フィルタで除去されない。"""
    relative = pathlib.Path("a_test.py")
    # 実ファイルは不要（targets が globs に通るよう実ファイルを用意する）
    (tmp_path / "a_test.py").write_text("", encoding="utf-8")

    config = pyfltr.config.config.create_default_config()
    _enable(config, ["pytest"])
    _patch_build_commandline(monkeypatch)
    _patch_subprocess(monkeypatch)

    ctx = _testconf.make_execution_context(config, [relative], start_cwd=tmp_path)
    pyfltr.command.dispatcher.execute_command("pytest", _testconf.make_args(), ctx)

    # 相対パスは外部フィルタされていない
    assert pyfltr.warnings_.filtered_direct_files(reason="external") == []


def test_is_external_path_absolute_inside_is_internal(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: pathlib.Path,
) -> None:
    """起点cwd配下の絶対パスは外部扱いされず、フィルタされない。"""
    inside = tmp_path / "inside_test.py"
    inside.write_text("", encoding="utf-8")

    config = pyfltr.config.config.create_default_config()
    _enable(config, ["pytest"])
    _patch_build_commandline(monkeypatch)
    _patch_subprocess(monkeypatch)

    ctx = _testconf.make_execution_context(config, [inside.resolve()], start_cwd=tmp_path)
    pyfltr.command.dispatcher.execute_command("pytest", _testconf.make_args(), ctx)

    assert pyfltr.warnings_.filtered_direct_files(reason="external") == []


def test_is_external_path_absolute_outside_is_external(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: pathlib.Path,
) -> None:
    """起点cwd外の絶対パスは外部扱いされ、フィルタと警告が発行される。"""
    ext_dir = tmp_path.parent / f"ext-{tmp_path.name}-iep"
    ext_dir.mkdir(parents=True, exist_ok=True)
    outside = (ext_dir / "ext_test.py").resolve()
    outside.write_text("", encoding="utf-8")

    config = pyfltr.config.config.create_default_config()
    _enable(config, ["pytest"])
    _patch_build_commandline(monkeypatch)
    _patch_subprocess(monkeypatch)

    ctx = _testconf.make_execution_context(config, [outside], start_cwd=tmp_path)
    pyfltr.command.dispatcher.execute_command("pytest", _testconf.make_args(), ctx)

    assert str(outside) in pyfltr.warnings_.filtered_direct_files(reason="external")


# --- _resolve_config_inject_path の境界を execute_command 経由で検証 --------------
# markdownlint を使い、設定ファイルの有無・候補順を確認する。


def test_resolve_config_inject_path_returns_first_hit(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: pathlib.Path,
) -> None:
    """候補順に走査し、最初に見つかった設定ファイルが --config に注入される。"""
    # 後候補のみ存在させる（先候補が無い状態で .markdownlint.json が注入される）
    (tmp_path / ".markdownlint.json").write_text("{}", encoding="utf-8")
    internal = tmp_path / "doc.md"
    internal.write_text("# doc\n", encoding="utf-8")

    config = pyfltr.config.config.create_default_config()
    _enable(config, ["markdownlint"])
    _patch_build_commandline(monkeypatch)
    _patch_subprocess(monkeypatch)

    ctx = _testconf.make_execution_context(config, [internal], start_cwd=tmp_path)
    result = pyfltr.command.dispatcher.execute_command("markdownlint", _testconf.make_args(), ctx)

    prefix_len = len(_DEFAULT_PREFIXES["markdownlint"])
    assert result.commandline[prefix_len] == "--config"
    assert result.commandline[prefix_len + 1] == str((tmp_path / ".markdownlint.json").resolve())


def test_resolve_config_inject_path_returns_none_if_absent(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: pathlib.Path,
) -> None:
    """設定ファイルが起点cwd直下に存在しないとき、--config は注入されない。"""
    internal = tmp_path / "doc.md"
    internal.write_text("# doc\n", encoding="utf-8")

    config = pyfltr.config.config.create_default_config()
    _enable(config, ["markdownlint"])
    _patch_build_commandline(monkeypatch)
    _patch_subprocess(monkeypatch)

    ctx = _testconf.make_execution_context(config, [internal], start_cwd=tmp_path)
    result = pyfltr.command.dispatcher.execute_command("markdownlint", _testconf.make_args(), ctx)

    assert "--config" not in result.commandline


# --- _user_overrides_config の境界を execute_command 経由で検証 -------------------
# markdownlint の {command}-args / {command}-extend-args / CLI追加引数で
# --config の各形式が注入スキップを引き起こすことを確認する。


def test_user_overrides_config_detects_separate_form(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: pathlib.Path,
) -> None:
    """`--config <path>`の分離形を{command}-argsで指定すると自動注入がスキップされる。"""
    (tmp_path / ".markdownlint.json").write_text("{}", encoding="utf-8")
    internal = tmp_path / "doc.md"
    internal.write_text("# doc\n", encoding="utf-8")

    config = pyfltr.config.config.create_default_config()
    _enable(config, ["markdownlint"])
    config.values["markdownlint-args"] = ["--config", "/user/path"]
    _patch_build_commandline(monkeypatch)
    _patch_subprocess(monkeypatch)

    ctx = _testconf.make_execution_context(config, [internal], start_cwd=tmp_path)
    result = pyfltr.command.dispatcher.execute_command("markdownlint", _testconf.make_args(), ctx)

    # 利用者指定の --config のみ（自動注入の設定ファイルパスは含まれない）
    assert result.commandline.count("--config") == 1
    assert "/user/path" in result.commandline
    assert str((tmp_path / ".markdownlint.json").resolve()) not in result.commandline


def test_user_overrides_config_detects_equal_form(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: pathlib.Path,
) -> None:
    """`--config=path`の=区切り形を{command}-argsで指定すると自動注入がスキップされる。"""
    (tmp_path / ".markdownlint.json").write_text("{}", encoding="utf-8")
    internal = tmp_path / "doc.md"
    internal.write_text("# doc\n", encoding="utf-8")

    config = pyfltr.config.config.create_default_config()
    _enable(config, ["markdownlint"])
    config.values["markdownlint-args"] = ["--config=/user/path"]
    _patch_build_commandline(monkeypatch)
    _patch_subprocess(monkeypatch)

    ctx = _testconf.make_execution_context(config, [internal], start_cwd=tmp_path)
    result = pyfltr.command.dispatcher.execute_command("markdownlint", _testconf.make_args(), ctx)

    assert "--config=/user/path" in result.commandline
    assert str((tmp_path / ".markdownlint.json").resolve()) not in result.commandline


def test_user_overrides_config_detects_in_extend_args(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: pathlib.Path,
) -> None:
    """`{command}-extend-args`で`--config`を指定しても自動注入がスキップされる。"""
    (tmp_path / ".markdownlint.json").write_text("{}", encoding="utf-8")
    internal = tmp_path / "doc.md"
    internal.write_text("# doc\n", encoding="utf-8")

    config = pyfltr.config.config.create_default_config()
    _enable(config, ["markdownlint"])
    config.values["markdownlint-extend-args"] = ["--config", "/extend/path"]
    _patch_build_commandline(monkeypatch)
    _patch_subprocess(monkeypatch)

    ctx = _testconf.make_execution_context(config, [internal], start_cwd=tmp_path)
    result = pyfltr.command.dispatcher.execute_command("markdownlint", _testconf.make_args(), ctx)

    assert str((tmp_path / ".markdownlint.json").resolve()) not in result.commandline


def test_user_overrides_config_negative(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: pathlib.Path,
) -> None:
    """`--config`を含まない引数を指定しても自動注入は行われる。"""
    (tmp_path / ".markdownlint.json").write_text("{}", encoding="utf-8")
    internal = tmp_path / "doc.md"
    internal.write_text("# doc\n", encoding="utf-8")

    config = pyfltr.config.config.create_default_config()
    _enable(config, ["markdownlint"])
    config.values["markdownlint-args"] = ["--format", "json"]
    _patch_build_commandline(monkeypatch)
    _patch_subprocess(monkeypatch)

    ctx = _testconf.make_execution_context(config, [internal], start_cwd=tmp_path)
    result = pyfltr.command.dispatcher.execute_command("markdownlint", _testconf.make_args(), ctx)

    # --config 自動注入が行われている
    assert "--config" in result.commandline
    assert str((tmp_path / ".markdownlint.json").resolve()) in result.commandline


# --- 注入対象（markdownlint / textlint） -----------------------------------------


@pytest.mark.parametrize(
    "command,config_file_name",
    [
        ("markdownlint", ".markdownlint.json"),
        ("textlint", ".textlintrc.yaml"),
    ],
)
def test_inject_config_arg_when_setting_file_exists(
    monkeypatch: pytest.MonkeyPatch,
    _ext_workspace: tuple[pathlib.Path, pathlib.Path, pathlib.Path],
    command: str,
    config_file_name: str,
) -> None:
    """注入対象ツールで起点cwd直下に設定ファイルが存在するとき、`--config <絶対パス>`が
    `commandline_prefix`直後に挿入される。
    """
    start_cwd, internal, external = _ext_workspace
    config_path = start_cwd / config_file_name
    config_path.write_text("# dummy\n", encoding="utf-8")

    config = pyfltr.config.config.create_default_config()
    _enable(config, [command])
    _patch_build_commandline(monkeypatch)
    captured = _patch_subprocess(monkeypatch)

    ctx = _testconf.make_execution_context(config, [internal, external], start_cwd=start_cwd)
    result = pyfltr.command.dispatcher.execute_command(command, _testconf.make_args(), ctx)

    # subprocessが呼ばれており、commandlineに--configが含まれる
    assert captured, "subprocess should have been called"
    commandline = result.commandline
    prefix = _DEFAULT_PREFIXES[command]
    prefix_len = len(prefix)
    # commandline_prefix直後に注入される
    assert commandline[prefix_len] == "--config"
    assert commandline[prefix_len + 1] == str(config_path.resolve())


@pytest.mark.parametrize("command", ["markdownlint", "textlint"])
def test_inject_skipped_when_no_setting_file(
    monkeypatch: pytest.MonkeyPatch,
    _ext_workspace: tuple[pathlib.Path, pathlib.Path, pathlib.Path],
    command: str,
) -> None:
    """設定ファイルが起点cwd直下に無いとき、注入をスキップする。"""
    start_cwd, internal, _external = _ext_workspace
    config = pyfltr.config.config.create_default_config()
    _enable(config, [command])
    _patch_build_commandline(monkeypatch)
    _patch_subprocess(monkeypatch)

    ctx = _testconf.make_execution_context(config, [internal], start_cwd=start_cwd)
    result = pyfltr.command.dispatcher.execute_command(command, _testconf.make_args(), ctx)

    assert "--config" not in result.commandline


@pytest.mark.parametrize(
    "command,config_file_name",
    [("markdownlint", ".markdownlint.json"), ("textlint", ".textlintrc.yaml")],
)
def test_inject_skipped_when_user_specifies_config(
    monkeypatch: pytest.MonkeyPatch,
    _ext_workspace: tuple[pathlib.Path, pathlib.Path, pathlib.Path],
    command: str,
    config_file_name: str,
) -> None:
    """利用者が`{command}-args`で`--config`を指定している場合は注入をスキップする。"""
    start_cwd, internal, _external = _ext_workspace
    (start_cwd / config_file_name).write_text("# dummy\n", encoding="utf-8")

    config = pyfltr.config.config.create_default_config()
    _enable(config, [command])
    config.values[f"{command}-args"] = ["--config", "/explicit/path"]
    _patch_build_commandline(monkeypatch)
    _patch_subprocess(monkeypatch)

    ctx = _testconf.make_execution_context(config, [internal], start_cwd=start_cwd)
    result = pyfltr.command.dispatcher.execute_command(command, _testconf.make_args(), ctx)

    # 利用者指定の`--config /explicit/path`のみが残り、自動注入は行われない
    assert result.commandline.count("--config") == 1
    assert "/explicit/path" in result.commandline
    assert str((start_cwd / config_file_name).resolve()) not in result.commandline


# --- 除外対象（allows_external_paths=False） -------------------------------------


@pytest.mark.parametrize(
    "command,target_pattern",
    [
        ("pre-commit", "doc.md"),
        ("pytest", "x_test.py"),
        ("gitleaks", "doc.md"),
        ("vitest", "x.test.ts"),
        ("cargo-test", "lib.rs"),
        ("dotnet-test", "Program.cs"),
        ("semgrep", "code.py"),
        ("uv-audit", "pyproject.toml"),
        ("pnpm-audit", "package.json"),
        ("npm-audit", "package.json"),
        ("yarn-audit", "package.json"),
    ],
)
def test_external_path_filtered_with_warning(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: pathlib.Path,
    command: str,
    target_pattern: str,
) -> None:
    """除外対象ツールでは外部パスがフィルタされ、警告と`reason="external"`蓄積が行われる。"""
    internal = tmp_path / target_pattern
    internal.write_text("", encoding="utf-8")
    ext_dir = tmp_path.parent / f"ext-{tmp_path.name}"
    ext_dir.mkdir(parents=True, exist_ok=True)
    external = (ext_dir / target_pattern).resolve()
    external.write_text("", encoding="utf-8")

    config = pyfltr.config.config.create_default_config()
    _enable(config, [command])
    _patch_build_commandline(monkeypatch)
    _patch_subprocess(monkeypatch)

    ctx = _testconf.make_execution_context(config, [internal, external], start_cwd=tmp_path)
    pyfltr.command.dispatcher.execute_command(command, _testconf.make_args(), ctx)

    # 警告蓄積
    filtered = pyfltr.warnings_.filtered_direct_files(reason="external")
    assert str(external) in filtered
    # warningレコードも発行されている
    warnings = pyfltr.warnings_.collected_warnings()
    assert any(command in w["message"] and str(external) in w["message"] for w in warnings)


def test_external_only_results_in_zero_targets(monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path) -> None:
    """除外対象ツールに外部パスのみを渡すと対象0件となり、既存の0件経路へ移行する。"""
    ext_dir = tmp_path.parent / f"ext-{tmp_path.name}-only"
    ext_dir.mkdir(parents=True, exist_ok=True)
    external = (ext_dir / "a_test.py").resolve()
    external.write_text("", encoding="utf-8")

    config = pyfltr.config.config.create_default_config()
    _enable(config, ["pytest"])
    _patch_build_commandline(monkeypatch)
    _patch_subprocess(monkeypatch)

    ctx = _testconf.make_execution_context(config, [external], start_cwd=tmp_path)
    result = pyfltr.command.dispatcher.execute_command("pytest", _testconf.make_args(), ctx)

    # 対象0件→「対象ファイルが見つかりません」経路
    assert result.files == 0


# --- 素通し対象（既定: allows_external_paths=True） -----------------------------


def test_pass_through_command_includes_external(monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path) -> None:
    """素通し対象（ruff-check等）は外部パスを除去せず、警告も発行しない。"""
    internal = tmp_path / "a.py"
    internal.write_text("x = 1\n", encoding="utf-8")
    ext_dir = tmp_path.parent / f"ext-{tmp_path.name}-py"
    ext_dir.mkdir(parents=True, exist_ok=True)
    external = (ext_dir / "ext.py").resolve()
    external.write_text("y = 2\n", encoding="utf-8")

    config = pyfltr.config.config.create_default_config()
    _enable(config, ["ruff-check"])
    _patch_build_commandline(monkeypatch)
    captured = _patch_subprocess(monkeypatch)

    ctx = _testconf.make_execution_context(config, [internal, external], start_cwd=tmp_path)
    result = pyfltr.command.dispatcher.execute_command("ruff-check", _testconf.make_args(), ctx)

    assert captured, "subprocess should have been called"
    # 両パスがcommandlineに含まれる
    assert str(internal) in result.commandline or str(internal.resolve()) in result.commandline
    assert str(external) in result.commandline
    # 注入対象ではないため`--config`は付かない
    assert "--config" not in result.commandline
    assert pyfltr.warnings_.filtered_direct_files(reason="external") == []


# --- モノレポ経路 --------------------------------------------------------------


def _make_monorepo(tmp_path: pathlib.Path) -> tuple[pathlib.Path, pathlib.Path, pathlib.Path]:
    """親 + サブの2階層モノレポを構築し、(start_cwd, sub_a_cwd, sub_b_cwd)を返す。"""
    (tmp_path / "pyproject.toml").write_text("[project]\nname = 'root'\n", encoding="utf-8")
    sub_a = tmp_path / "pkg_a"
    sub_a.mkdir()
    (sub_a / "pyproject.toml").write_text("[project]\nname = 'a'\n", encoding="utf-8")
    sub_b = tmp_path / "pkg_b"
    sub_b.mkdir()
    (sub_b / "pyproject.toml").write_text("[project]\nname = 'b'\n", encoding="utf-8")
    return tmp_path, sub_a, sub_b


def test_monorepo_internal_only_no_external_dispatch(monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path) -> None:
    """モノレポで外部パスが無い場合、外部パス追加実行経路は呼ばれない。"""
    start_cwd, sub_a, sub_b = _make_monorepo(tmp_path)
    file_a = sub_a / "doc.md"
    file_a.write_text("# a\n", encoding="utf-8")
    file_b = sub_b / "doc.md"
    file_b.write_text("# b\n", encoding="utf-8")

    config = pyfltr.config.config.create_default_config()
    _enable(config, ["markdownlint"])
    _patch_build_commandline(monkeypatch)

    subs = pyfltr.command.subprojects.discover_subprojects(start_cwd, config, git_check_ignore=lambda _s, _c: set())
    subproject_files, external_files = pyfltr.command.subprojects.classify_files_by_subproject(
        [pathlib.Path("pkg_a/doc.md"), pathlib.Path("pkg_b/doc.md")], subs, start_cwd
    )
    assert not external_files
    base = pyfltr.command.core_.ExecutionBaseContext(
        config=config,
        all_files=[pathlib.Path("pkg_a/doc.md"), pathlib.Path("pkg_b/doc.md")],
        cache_store=None,
        cache_run_id=None,
        start_cwd=start_cwd,
        subprojects=subs,
        subproject_files=subproject_files,
        external_files=external_files,
        subproject_configs={s.cwd: config for s in subs},
    )
    ctx = pyfltr.command.core_.ExecutionContext(base=base)

    calls: list[pathlib.Path | None] = []

    def _capture(command: str, args: argparse.Namespace, c: pyfltr.command.core_.ExecutionContext) -> object:
        del command, args
        calls.append(c.subproject_cwd)
        return _testconf.make_succeeded_result(command="markdownlint")

    monkeypatch.setattr(pyfltr.command.dispatcher, "_dispatch_command", _capture)
    pyfltr.command.dispatcher.execute_command("markdownlint", _testconf.make_args(), ctx)

    # 各サブプロジェクトに対する呼び出しのみで、起点cwd（None）の追加実行は無い
    assert None not in calls
    assert len(calls) == 2


def test_monorepo_mixed_inject_extra_dispatch(monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path) -> None:
    """モノレポ＋外部パスありで、注入対象ツールは起点cwdで追加実行される。"""
    start_cwd, sub_a, _sub_b = _make_monorepo(tmp_path)
    file_a = sub_a / "doc.md"
    file_a.write_text("# a\n", encoding="utf-8")
    external = _make_external(tmp_path)
    (start_cwd / ".markdownlint.json").write_text("{}", encoding="utf-8")

    config = pyfltr.config.config.create_default_config()
    _enable(config, ["markdownlint"])
    _patch_build_commandline(monkeypatch)

    subs = pyfltr.command.subprojects.discover_subprojects(start_cwd, config, git_check_ignore=lambda _s, _c: set())
    base = pyfltr.command.core_.ExecutionBaseContext(
        config=config,
        all_files=[pathlib.Path("pkg_a/doc.md"), external],
        cache_store=None,
        cache_run_id=None,
        start_cwd=start_cwd,
        subprojects=subs,
        subproject_files={
            next(s.cwd for s in subs if s.relative == "pkg_a"): [pathlib.Path("pkg_a/doc.md")],
            next(s.cwd for s in subs if s.relative == "."): [],
            next(s.cwd for s in subs if s.relative == "pkg_b"): [],
        },
        external_files=[external],
        subproject_configs={s.cwd: config for s in subs},
    )
    ctx = pyfltr.command.core_.ExecutionContext(base=base)

    calls: list[pathlib.Path | None] = []

    def _capture(command: str, args: argparse.Namespace, c: pyfltr.command.core_.ExecutionContext) -> object:
        del command, args
        calls.append(c.subproject_cwd)
        return _testconf.make_succeeded_result(command="markdownlint")

    monkeypatch.setattr(pyfltr.command.dispatcher, "_dispatch_command", _capture)
    pyfltr.command.dispatcher.execute_command("markdownlint", _testconf.make_args(), ctx)

    # pkg_a の1回（サブ）+ 外部パスの起点cwd（None）の1回 = 計2回
    assert None in calls
    assert any(c is not None for c in calls)


def test_monorepo_mixed_excluded_tool_warns_only(monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path) -> None:
    """モノレポ＋外部パスありで、除外対象ツールは追加実行せず警告のみ発行する。"""
    start_cwd, sub_a, _sub_b = _make_monorepo(tmp_path)
    test_file = sub_a / "x_test.py"
    test_file.write_text("def test(): pass\n", encoding="utf-8")
    # 外部パスにテスト対象拡張子の.pyを置く
    ext_dir = tmp_path.parent / f"ext-{tmp_path.name}-pytest"
    ext_dir.mkdir(parents=True, exist_ok=True)
    external_test = (ext_dir / "ext_test.py").resolve()
    external_test.write_text("def test(): pass\n", encoding="utf-8")

    config = pyfltr.config.config.create_default_config()
    _enable(config, ["pytest"])
    _patch_build_commandline(monkeypatch)

    subs = pyfltr.command.subprojects.discover_subprojects(start_cwd, config, git_check_ignore=lambda _s, _c: set())
    sub_a_cwd = next(s.cwd for s in subs if s.relative == "pkg_a")
    base = pyfltr.command.core_.ExecutionBaseContext(
        config=config,
        all_files=[pathlib.Path("pkg_a/x_test.py"), external_test],
        cache_store=None,
        cache_run_id=None,
        start_cwd=start_cwd,
        subprojects=subs,
        subproject_files={s.cwd: ([pathlib.Path("pkg_a/x_test.py")] if s.cwd == sub_a_cwd else []) for s in subs},
        external_files=[external_test],
        subproject_configs={s.cwd: config for s in subs},
    )
    ctx = pyfltr.command.core_.ExecutionContext(base=base)

    calls: list[pathlib.Path | None] = []

    def _capture(command: str, args: argparse.Namespace, c: pyfltr.command.core_.ExecutionContext) -> object:
        del command, args
        calls.append(c.subproject_cwd)
        return _testconf.make_succeeded_result(command="pytest")

    monkeypatch.setattr(pyfltr.command.dispatcher, "_dispatch_command", _capture)
    pyfltr.command.dispatcher.execute_command("pytest", _testconf.make_args(), ctx)

    # pkg_aの1回のみで、外部パス用の追加実行は無い（subproject_cwd=Noneでの呼び出しが無い）
    assert calls == [sub_a_cwd]
    # 警告は1件発行されている
    assert str(external_test) in pyfltr.warnings_.filtered_direct_files(reason="external")
